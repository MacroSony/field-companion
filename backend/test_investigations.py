"""Deterministic background task lifecycle and nonblocking voice integration."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from investigations import InvestigationQueue, InvestigationSettings
import server as s


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(.005)


def snapshot():
    return {"id": "original", "status": "open", "fused": [
        {"sci": "Cyanocitta cristata", "en": "Blue Jay", "p": .8}],
        "detections": [{"id": "frame1", "kind": "image", "candidates": []}]}


class InvestigationQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.release = asyncio.Event()
        self.order, self.changed, self.done = [], [], []

        async def runner(question, observation, progress):
            self.order.append((question, observation))
            await progress("analyzing")
            await self.release.wait()
            return {"ok": True, "answer": question + " answer", "status": "completed"}

        self.queue = InvestigationQueue(runner, self.change, self.finish)

    async def change(self, payload):
        self.changed.append(deepcopy(payload))

    async def finish(self, job):
        self.done.append(job.id)

    async def asyncTearDown(self):
        await self.queue.close()

    async def test_serial_execution_snapshot_isolation_and_progress(self):
        source = snapshot()
        first = await self.queue.submit("First?", source)
        second = await self.queue.submit("Second?", source)
        source["fused"][0]["sci"] = "Changed later"
        await until(lambda: len(self.order) == 1)
        self.assertEqual(first.status, "running")
        self.assertEqual(first.stage, "analyzing")
        self.assertEqual(second.status, "queued")
        self.assertEqual(self.queue.payload()["items"][1]["queue_position"], 1)
        self.assertEqual(first.observation["fused"][0]["sci"], "Cyanocitta cristata")
        self.release.set()
        await until(lambda: second.status == "completed")
        self.assertEqual([q for q, _ in self.order], ["First?", "Second?"])
        self.assertEqual(self.done, [first.id, second.id])
        self.assertTrue(any(p["items"][0]["stage"] == "analyzing" for p in self.changed))
        payload = self.queue.payload()
        payload["items"][0]["result"]["answer"] = "tampered"
        self.assertEqual(first.result["answer"], "First? answer")

    async def test_cancel_queued_and_running_promotes_next(self):
        first = await self.queue.submit("First?", snapshot())
        queued = await self.queue.submit("Cancel this?", snapshot())
        last = await self.queue.submit("Last?", snapshot())
        await until(lambda: bool(self.order))
        self.assertTrue(await self.queue.cancel(queued))
        self.assertTrue(await self.queue.cancel(first))
        await until(lambda: len(self.order) == 2)
        self.assertEqual(self.order[1][0], "Last?")
        self.release.set()
        await until(lambda: last.status == "completed")
        self.assertEqual(self.done, [last.id])
        self.assertEqual(first.status, "cancelled")
        self.assertEqual(queued.status, "cancelled")

    async def test_timeout_and_failure_do_not_block_later_jobs(self):
        self.queue.settings = InvestigationSettings(timeout=.02)
        first = await self.queue.submit("Slow?", snapshot())
        await until(lambda: first.status == "timed_out")
        self.assertIn(first.id, self.done)

        async def broken(*args):
            raise RuntimeError("SECRET-MARKER")
        self.queue.runner = broken
        with self.assertLogs("field-companion.investigations", level="WARNING") as logs:
            second = await self.queue.submit("Failure?", snapshot())
            await until(lambda: second.status == "failed")
        self.assertNotIn("SECRET-MARKER", " ".join(logs.output))
        self.assertEqual(second.result["status"], "unavailable")
        self.queue.runner = AsyncMock(return_value={"ok": True, "answer": "Recovered"})
        last = await self.queue.submit("Next?", snapshot())
        await until(lambda: last.status == "completed")

    async def test_bounded_queue_history_dedup_and_retry_snapshot(self):
        self.queue.settings = InvestigationSettings(capacity=2, history=3)
        first = await self.queue.submit("First?", snapshot())
        duplicate = await self.queue.submit("First?", snapshot())
        self.assertIs(first, duplicate)
        second = await self.queue.submit("Second?", snapshot())
        with self.assertRaises(ValueError):
            await self.queue.submit("Too many?", snapshot())
        await self.queue.cancel(second)
        self.release.set()
        await until(lambda: first.status == "completed")
        retry = await self.queue.submit(first.question, first.observation, parent_id=first.id)
        await until(lambda: retry.status == "completed")
        self.assertEqual(retry.parent_id, first.id)
        fourth = await self.queue.submit("New?", snapshot())
        await until(lambda: fourth.status == "completed")
        self.assertEqual(len(self.queue.jobs), 3)
        self.assertNotIn(first.id, self.queue.jobs)

    async def test_shutdown_cancels_work_without_completion_notice(self):
        first = await self.queue.submit("First?", snapshot())
        await until(lambda: bool(self.order))
        await self.queue.close()
        self.assertEqual(first.status, "cancelled")
        self.assertFalse(self.done)
        self.assertTrue(self.queue.worker.done())

    async def test_late_success_after_cancel_cannot_replace_cancelled_status(self):
        async def stubborn(question, observation, progress):
            try:
                await asyncio.sleep(20)
            except asyncio.CancelledError:
                return {"ok": True, "answer": "late result"}
        self.queue.runner = stubborn
        job = await self.queue.submit("Cancel?", snapshot())
        await until(lambda: job.status == "running")
        await asyncio.sleep(.01)
        await self.queue.cancel(job)
        await until(lambda: self.queue.worker.done())
        self.assertEqual(job.status, "cancelled")
        self.assertIsNone(job.result)
        self.assertFalse(self.done)


class InvestigationSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.c = s.Session("background-test")
        self.events, self.generated = [], []
        self.release = asyncio.Event()
        self.c.phone_json = AsyncMock(side_effect=lambda event: self.events.append(event))
        self.c.rt_send = AsyncMock()
        self.c.allowed_species = lambda: set()
        self.c.expert = SimpleNamespace(available=True, answer=AsyncMock(side_effect=self.slow))
        self.obs = s.Observation("original", fused=snapshot()["fused"],
            detections=[s.Detection("frame1", "image", s.now_iso())])
        self.c.observations[self.obs.id] = self.obs
        self.c.focus = self.obs.id

    async def slow(self, question, observation, **kwargs):
        await kwargs["on_progress"]("analyzing")
        await self.release.wait()
        return {"ok": True, "status": "completed", "action": "request_more_evidence",
                "answer": "Please photograph the original bird's bill.", "hint": "Original bill"}

    async def asyncTearDown(self):
        await self.c.close()

    async def test_reset_cancels_old_jobs_and_clears_offers_before_new_outing(self):
        await self.c.tool("ask_expert", {"question": "Original question?"})
        await self.c.tool("ask_expert", {"question": "Queued question?"})
        await until(lambda: self.c.investigations.running_task is not None)
        old_queue = self.c.investigations
        self.c.investigation_offer_ids = ["R1"]
        self.c.auto_speech_paused = True
        await self.c.reset()
        self.assertTrue(old_queue.closed)
        self.assertTrue(all(job.status == "cancelled" for job in old_queue.jobs.values()))
        self.assertFalse(self.c.investigations.closed)
        self.assertIs(self.c.investigations.settings, old_queue.settings)
        self.assertFalse(self.c.investigations.jobs or self.c.pending or self.c.investigation_offer_ids)
        self.assertFalse(self.c.auto_speech_paused)
        self.assertTrue(any(e["type"] == "investigations" and not e["items"] for e in self.events))
        self.release.set()
        await asyncio.sleep(.02)
        self.assertFalse(self.c.pending)
        self.assertEqual((await self.c.tool("get_investigations", {}))["investigations"], [])

    async def test_slow_expert_releases_voice_worker_for_more_questions_and_jobs(self):
        self.c.connected = True

        async def generate(phase):
            self.generated.append((phase, self.c.expert_readout))
            if phase == "route":
                text = self.c.turn_question
                name = "get_checklist" if text == "How many species?" else (
                    "get_investigations" if text == "Progress?" else "ask_expert")
                return {"status": "completed", "output": [{"type": "function_call",
                    "name": name, "call_id": str(len(self.generated)),
                    "arguments": json.dumps({"question": "Router paraphrase"} if name == "ask_expert" else {})}]}
            return {"status": "completed"}

        self.c.generate = generate
        worker = asyncio.create_task(self.c.turn_worker())
        self.c.tasks.append(worker)
        for text in ("First investigation?", "How many species?", "Progress?", "Second investigation?"):
            before = len(self.generated)
            await self.c.handle_phone({"type": "text", "text": text})
            await until(lambda: len(self.generated) > before and not self.c.active and not self.c.pending)
        self.assertFalse(self.release.is_set())
        self.assertEqual(len(self.c.investigations.jobs), 2)
        self.assertEqual(self.c.investigations.jobs["R1"].status, "running")
        self.assertEqual(self.c.investigations.jobs["R2"].status, "queued")
        self.assertEqual(self.c.expert.answer.await_count, 1)
        self.assertEqual(self.c.expert.answer.call_args.args[0], "First investigation?")
        self.assertTrue(any("is analyzing evidence" in (text or "") for _, text in self.generated))

    async def test_schedule_ack_is_only_one_short_sentence_in_both_languages(self):
        first = await self.c.tool("ask_expert", {"question": "What does it eat?"})
        second = await self.c.tool("ask_expert", {"question": "它住在哪里？"})
        self.assertEqual(first["answer"], "Investigation R1 is scheduled.")
        self.assertEqual(second["answer"], "调查 R2 已安排。")
        await self.c.tool("manage_investigation", {"action": "cancel", "id": "R1"})
        self.c.turn_question = "Try R1 again"
        retry = await self.c.tool("manage_investigation", {"action": "retry", "id": "R1"})
        self.assertEqual(retry["answer"], "Investigation R3 is scheduled.")

    async def test_stop_leaves_ready_results_silent_until_another_user_turn(self):
        self.c.connected = True
        self.c.generate = AsyncMock(return_value={"status": "completed", "output": []})
        await self.c.handle_rt({"type": "conversation.item.input_audio_transcription.completed", "transcript": "Stop."})
        await self.c.tool("ask_expert", {"question": "First?"})
        self.release.set()
        await until(lambda: self.c.investigations.jobs["R1"].status == "completed")
        self.c.tasks.append(asyncio.create_task(self.c.turn_worker()))
        self.c.work_ready.set()
        await asyncio.sleep(.02)
        self.c.generate.assert_not_awaited()
        self.assertTrue(self.c.auto_speech_paused)
        await self.c.handle_phone({"type": "text", "text": "How many species?"})
        await until(lambda: self.c.generate.await_count >= 1)
        self.assertFalse(self.c.auto_speech_paused)

    async def test_focus_and_new_input_preserve_result_without_hint_leaking(self):
        result = await self.c.tool("ask_expert", {"question": "Could it be another species?"})
        job = self.c.investigations.jobs[result["investigation_id"]]
        await until(lambda: self.c.expert.answer.await_count == 1)
        self.c.input_revision += 20
        self.obs.detections.append(s.Detection("new-frame", "image", s.now_iso()))
        new = await self.c.new_focus()
        self.release.set()
        await until(lambda: job.status == "completed")
        self.assertEqual(job.observation["id"], self.obs.id)
        self.assertEqual(len(job.observation["detections"]), 1)
        self.assertEqual(new.hint, "")
        self.assertEqual(self.obs.hint, "")
        self.assertTrue(any(p[:2] == ("investigation", job.id) for p in self.c.pending))
        self.assertIn("Blue Jay", self.c.investigation_answer(job))
        self.assertEqual(self.c.items, [])

    async def test_hint_applies_only_to_unchanged_current_evidence(self):
        await self.c.tool("ask_expert", {"question": "Could it be another species?"})
        self.release.set()
        await until(lambda: self.c.investigations.jobs["R1"].status == "completed")
        self.assertEqual(self.obs.hint, "Original bill")
        self.assertEqual(self.obs.status, "open")
        self.assertEqual(self.c.items, [])

    async def test_notice_waits_for_user_playback_quiet_and_prioritizes_user_turn(self):
        result = await self.c.tool("ask_expert", {"question": "First?"})
        self.release.set()
        await until(lambda: self.c.investigations.jobs["R1"].status == "completed")
        job = self.c.investigations.jobs[result["investigation_id"]]
        self.c.connected = True
        self.c.generate = AsyncMock(return_value={"status": "completed", "output": []})
        worker = asyncio.create_task(self.c.turn_worker())
        self.c.tasks.append(worker)
        for guard in ("user_speaking", "awaiting_transcript", "quiet"):
            setattr(self.c, guard, True)
            self.c.work_ready.set()
            await asyncio.sleep(.01)
            self.c.generate.assert_not_awaited()
            setattr(self.c, guard, False)
        self.c.play_until = s.time.monotonic() + 5
        self.c.work_ready.set()
        await asyncio.sleep(.01)
        self.c.generate.assert_not_awaited()
        self.c.play_until = 0
        client = s.Client(SimpleNamespace(send_json=AsyncMock()), self.c)
        client.earphones, client.playback_active = True, True
        self.c.clients.add(client)
        self.c.mic = client
        self.c.work_ready.set()
        await asyncio.sleep(.01)
        self.c.generate.assert_not_awaited()
        client.playback_active = False
        self.c.pending.append(("text", "Urgent next question", None))
        self.c.work_ready.set()
        await until(lambda: self.c.generate.await_count >= 2)
        self.assertEqual(self.c.generate.call_args_list[0].args[0], "route")
        self.assertEqual(self.c.generate.call_args_list[1].args[0], "answer")
        self.assertTrue(job.announced)

    async def test_query_cancel_retry_and_reconnect_use_original_job(self):
        await self.c.tool("ask_expert", {"question": "Original question?"})
        await until(lambda: self.c.expert.answer.await_count == 1)
        self.c.turn_question = "第一个调查到哪一步了？"
        status = await self.c.tool("get_investigations", {"selection": "first"})
        self.assertIn("正在分析证据", status["answer"])
        await self.c.handle_phone({"type": "investigation_action", "id": "R1", "action": "cancel"})
        self.assertEqual(self.c.investigations.jobs["R1"].status, "cancelled")
        await self.c.new_focus("Insecta")
        retry = await self.c.tool("manage_investigation", {"action": "retry", "id": "R1"})
        self.assertEqual(retry["investigation_id"], "R2")
        self.assertEqual(self.c.investigations.jobs["R2"].observation["id"], "original")
        self.assertEqual(self.c.investigations.jobs["R2"].question, "Original question?")
        self.events.clear()
        await self.c.handle_phone({"type": "hello"})
        replay = next(e for e in self.events if e["type"] == "investigations")
        self.assertEqual([j["id"] for j in replay["items"]], ["R1", "R2"])
        with self.assertRaises(ValueError):
            await self.c.tool("manage_investigation", {"action": "cancel", "id": "missing"})

    async def test_cancel_first_never_cancels_a_later_active_task(self):
        await self.c.tool("ask_expert", {"question": "First?"})
        self.release.set()
        await until(lambda: self.c.investigations.jobs["R1"].status == "completed")
        self.release.clear()
        await self.c.tool("ask_expert", {"question": "Second?"})
        result = await self.c.tool("manage_investigation", {"action": "cancel", "selection": "first"})
        self.assertEqual(result["cancelled"], [])
        self.assertIn(self.c.investigations.jobs["R2"].status, ("queued", "running"))

    async def test_user_input_during_notice_publication_defers_notice(self):
        await self.c.tool("ask_expert", {"question": "First?"})
        self.release.set()
        await until(lambda: self.c.investigations.jobs["R1"].status == "completed")
        job = self.c.investigations.jobs["R1"]
        self.c.generate = AsyncMock()
        async def user_arrives(event):
            self.c.input_revision += 1
        self.c.phone_json.side_effect = user_arrives
        await self.c.investigation_notice(job)
        self.c.generate.assert_not_awaited()
        self.assertFalse(job.announced)

    async def test_notice_failure_preserves_result_and_has_bounded_retry(self):
        await self.c.tool("ask_expert", {"question": "First?"})
        self.release.set()
        await until(lambda: self.c.investigations.jobs["R1"].status == "completed")
        job = self.c.investigations.jobs["R1"]
        self.c.rt_send.side_effect = ConnectionError("Disconnected")
        self.c.pending.clear()
        for _ in range(2):
            with self.assertRaises(ConnectionError):
                await self.c.investigation_notice(job)
        self.assertEqual(job.notice_attempts, 2)
        self.assertEqual(len(self.c.pending), 1)
        self.assertEqual(job.status, "completed")
        self.assertTrue(job.result["ok"])

    async def test_completed_jobs_offer_once_together_without_reading_findings(self):
        for question in ("First?", "Second?"):
            await self.c.tool("ask_expert", {"question": question})
        self.release.set()
        await until(lambda: all(j.status == "completed" for j in self.c.investigations.jobs.values()))
        self.c.connected = True
        spoken = []
        async def generate(phase, **kwargs):
            spoken.append(self.c.expert_readout)
            return {"status": "completed"}
        self.c.generate = generate
        self.c.tasks.append(asyncio.create_task(self.c.turn_worker()))
        self.c.work_ready.set()
        await until(lambda: len(spoken) == 1 and not self.c.active)
        await asyncio.sleep(.02)
        self.assertEqual(len(spoken), 1)
        self.assertIn("R1", spoken[0])
        self.assertIn("R2", spoken[0])
        self.assertIn("Which would you like me to explain?", spoken[0])
        self.assertNotIn("bird's bill", spoken[0])
        self.assertNotIn("bird's bill", str(self.c.rt_send.call_args_list))
        self.assertEqual(self.c.investigation_offer_ids, ["R1", "R2"])
        ambiguous = await self.c.tool("get_investigations", {"explain": True})
        self.assertTrue(ambiguous["requires_selection"])
        self.assertNotIn("bird's bill", ambiguous["answer"])
        explicit = await self.c.tool("get_investigations", {"id": "R1", "explain": True})
        self.assertIn("bird's bill", explicit["answer"])

    async def test_status_only_offers_explanation_and_yes_reads_the_offered_job(self):
        await self.c.tool("ask_expert", {"question": "First?"})
        self.release.set()
        await until(lambda: self.c.investigations.jobs["R1"].status == "completed")
        status = await self.c.tool("get_investigations", {"id": "R1"})
        self.assertNotIn("bird's bill", status["answer"])
        self.assertIsNone(status["investigations"][0]["result"])
        self.assertIn("Would you like me to explain?", status["answer"])
        # A newer background job must not change what accepting the offer means.
        self.release.clear()
        await self.c.tool("ask_expert", {"question": "Second?"})
        result = await self.c.tool("get_investigations", {"explain": True})
        self.assertIn("Investigation R1", result["answer"])
        self.assertIn("bird's bill", result["answer"])
        self.assertEqual(self.c.investigation_offer_ids, [])

    async def test_next_notice_waits_for_phone_to_drain_previous_short_offer(self):
        phone = SimpleNamespace(send_json=AsyncMock(), send_bytes=AsyncMock())
        client = s.Client(phone, self.c)
        client.earphones, client.playback_protocol = True, 1
        self.c.clients.add(client)
        self.c.mic = client
        self.c.connected = True
        spoken = []
        async def generate(phase, **kwargs):
            spoken.append(self.c.expert_readout)
            rid = "offer-" + str(len(spoken))
            self.c.phase, self.c.audio_allowed = "answer", True
            self.c.response_id, self.c.first_audio = rid, False
            await self.c.handle_rt({"type": "response.audio.delta", "response_id": rid, "delta": "AAA="})
            await self.c.handle_rt({"type": "response.done", "response": {"id": rid, "status": "completed"}})
            return {"status": "completed"}
        self.c.generate = generate
        self.c.tasks.append(asyncio.create_task(self.c.turn_worker()))
        await self.c.tool("ask_expert", {"question": "First?"})
        self.release.set()
        await until(lambda: len(spoken) == 1 and not self.c.active)
        await self.c.tool("ask_expert", {"question": "Second?"})
        await until(lambda: self.c.investigations.jobs["R2"].status == "completed")
        self.c.play_until = self.c.echo_estimate = 0
        self.c.work_ready.set()
        await asyncio.sleep(.02)
        self.assertEqual(len(spoken), 1)
        self.assertFalse(self.c.investigations.jobs["R2"].announced)
        await self.c.handle_phone({"type": "playback", "active": False, "drained": True, "response_id": "offer-1"}, client)
        await until(lambda: len(spoken) == 2)
        self.assertNotIn("bird's bill", " ".join(spoken))

    async def test_user_speech_gets_its_next_reply_before_ready_offer(self):
        client = s.Client(SimpleNamespace(send_json=AsyncMock(), send_bytes=AsyncMock()), self.c)
        client.earphones, client.playback_protocol = True, 1
        self.c.clients.add(client)
        self.c.mic = client
        self.c.connected = True
        self.c.user_speaking = self.c.awaiting_transcript = True
        spoken = []
        async def generate(phase, **kwargs):
            if phase == "route":
                return {"status": "completed", "output": [{"type": "function_call",
                    "name": "get_checklist", "call_id": "count", "arguments": "{}"}]}
            spoken.append(self.c.expert_readout or "Normal reply to the user")
            rid = "speech-" + str(len(spoken))
            self.c.phase, self.c.audio_allowed, self.c.first_audio = "answer", True, False
            self.c.response_id = rid
            await self.c.handle_rt({"type": "response.audio.delta", "response_id": rid, "delta": "AAA="})
            await self.c.handle_rt({"type": "response.done", "response": {"id": rid, "status": "completed"}})
            return {"status": "completed"}
        self.c.generate = generate
        self.c.tasks.append(asyncio.create_task(self.c.turn_worker()))
        await self.c.tool("ask_expert", {"question": "First?"})
        self.release.set()
        await until(lambda: self.c.investigations.jobs["R1"].status == "completed")
        await asyncio.sleep(.02)
        self.assertEqual(spoken, [])
        await self.c.handle_rt({"type": "conversation.item.input_audio_transcription.completed",
                               "transcript": "How many species today?"})
        await until(lambda: len(spoken) == 1 and not self.c.active)
        self.assertEqual(spoken, ["Normal reply to the user"])
        await self.c.handle_phone({"type": "playback", "active": False, "drained": True, "response_id": "speech-1"}, client)
        await until(lambda: len(spoken) == 2)
        self.assertIn("Would you like me to explain?", spoken[1])
        self.assertNotIn("bird's bill", spoken[1])
