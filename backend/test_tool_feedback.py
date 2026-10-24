"""Offline ordering, cancellation and speech-policy tests for pre-tool feedback."""
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import server as s
from expert import NaturalistExpert, Settings
from tool_feedback import CLIPS, recording


class FeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = []
        self.phone = SimpleNamespace(
            send_json=AsyncMock(side_effect=lambda e: self.events.append(e)),
            send_bytes=AsyncMock(side_effect=lambda pcm: self.events.append({"type": "pcm", "bytes": len(pcm)})))
        self.c = s.Session("feedback-test")
        self.c.expert = NaturalistExpert(Settings(enabled=False))
        self.c.connected = True
        self.c.rt_send = AsyncMock()
        self.client = s.Client(self.phone, self.c)
        self.client.playback_protocol = 1
        self.c.clients.add(self.client)
        self.c.mic = self.client

    async def asyncTearDown(self):
        await self.c.close()

    async def test_feedback_arrives_before_slow_tool_finishes_without_model_request(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def tool(name, args):
            self.events.append({"type": "tool_started"})
            started.set()
            await release.wait()
            return {"ok": True, "waiting_for_frames": True}
        self.c.tool = tool
        response = {"status": "completed", "output": [{"type": "function_call", "name": "look",
                    "call_id": "look-1", "arguments": '{"target":"current"}'}]}
        work = asyncio.create_task(self.c.finish_route(response))
        try:
            await asyncio.wait_for(started.wait(), 1)
            self.assertFalse(work.done())
            types = [e["type"] for e in self.events]
            self.assertLess(types.index("audio_start"), types.index("pcm"))
            self.assertLess(types.index("pcm"), types.index("tool_started"))
            self.assertLess(types.index("audio_end"), types.index("tool_started"))
            self.assertTrue(self.client.playback_pending)
            self.assertEqual(next(e["text"] for e in self.events if e["type"] == "transcript"), "Let me take a look.")
            self.c.rt_send.assert_not_awaited()
        finally:
            release.set()
            await work

    async def test_chinese_feedback_uses_question_language_and_is_echo_filtered(self):
        self.c.turn_question = "这是什么？"
        await self.c.acknowledge_tool("look")
        self.assertTrue(any(e.get("text") == "好的，我看一下。" for e in self.events))
        self.assertTrue(self.c.transcript_is_echo("好的，我看一下。"))
        self.assertFalse(self.c.transcript_is_echo("好的。"))
        self.assertFalse(self.c.transcript_is_echo("停一下。"))

    async def test_quiet_speech_pending_playback_and_fast_actions_do_not_get_extra_voice(self):
        for field, value in [("quiet", True), ("user_speaking", True),
                             ("awaiting_transcript", True), ("auto_speech_paused", True),
                             ("connected", False), ("play_until", s.time.monotonic() + 10)]:
            original = getattr(self.c, field)
            setattr(self.c, field, value)
            await self.c.acknowledge_tool("look")
            setattr(self.c, field, original)
        self.client.playback_pending = True
        await self.c.acknowledge_tool("look")
        self.client.playback_pending = False
        for name in ("ignore", "speak", "add_observation", "ask_expert", "get_investigations"):
            await self.c.acknowledge_tool(name)
        self.assertFalse(self.events)

    async def test_interrupt_before_pcm_drops_feedback_and_releases_pending_playback(self):
        async def interrupt(event):
            self.events.append(event)
            if event["type"] == "audio_start":
                self.c.input_revision += 1
                self.c.user_speaking = True
        self.phone.send_json.side_effect = interrupt
        await self.c.acknowledge_tool("look")
        self.phone.send_bytes.assert_not_awaited()
        self.assertFalse(self.client.playback_pending)
        self.assertIn({"type": "flush"}, self.events)

    async def test_cancel_while_client_send_waits_cannot_leak_old_pcm(self):
        audible = True
        await self.client.lock.acquire()
        work = asyncio.create_task(self.client.send_audio(b"old", guard=lambda: audible))
        await asyncio.sleep(0)
        audible = False
        self.client.lock.release()
        await work
        self.phone.send_bytes.assert_not_awaited()

    async def test_expert_keeps_only_existing_scheduled_confirmation(self):
        self.c.tool = AsyncMock(return_value={"ok": True, "answer": "Investigation R1 is scheduled."})
        self.c.speak_expert = AsyncMock()
        await self.c.finish_route({"status": "completed", "output": [{"type": "function_call",
            "name": "ask_expert", "call_id": "expert", "arguments": '{"question":"What does it eat?"}'}]})
        self.phone.send_bytes.assert_not_awaited()
        self.c.speak_expert.assert_awaited_once_with("Investigation R1 is scheduled.")

    async def test_voice_waits_for_routing_even_when_another_tool_owns_worker(self):
        self.c.active = True
        self.c.phase = "route"
        self.c.generate = AsyncMock()
        await self.c.handle_rt({"type": "conversation.item.input_audio_transcription.completed",
            "transcript": "How many species have I seen today?", "item_id": "question-2"})
        self.phone.send_bytes.assert_not_awaited()
        self.c.generate.assert_not_awaited()
        self.assertFalse(self.c.pending[-1][1].acknowledged)
        self.assertEqual(self.c.pending[-1][1].item_ids, {"question-2"})

    async def test_typed_request_gets_one_task_specific_cue_after_routing(self):
        await self.c.handle_phone({"type": "text", "text": "这里有哪些鸟？"}, self.client)
        self.phone.send_bytes.assert_not_awaited()
        self.c.turn_question = self.c.pending[-1][1]
        self.c.tool = AsyncMock(return_value={"ok": True})
        self.c.generate = AsyncMock()
        await self.c.finish_route({"status": "completed", "output": [{"type": "function_call",
            "name": "nearby_species", "call_id": "check", "arguments": "{}"}]})
        self.assertEqual([e.get("text") for e in self.events if e["type"] == "transcript" and e.get("role") == "assistant"], ["好的，我查一下。"])
        self.assertTrue(self.c.transcript_is_echo("好的，我查一下。"))
        await self.c.acknowledge_tool("nearby_species")
        self.phone.send_bytes.assert_awaited_once()
        self.c.tool.assert_awaited_once()

    async def test_receipt_preserves_quiet_stop_filler_and_echo_policies(self):
        self.c.quiet = True
        await self.c.handle_rt({"type": "conversation.item.input_audio_transcription.completed",
            "transcript": "What does it eat?", "item_id": "quiet-1"})
        self.phone.send_bytes.assert_not_awaited()
        self.c.quiet = False
        self.c.pending.clear()
        await self.c.handle_rt({"type": "conversation.item.input_audio_transcription.completed",
            "transcript": "stop", "item_id": "stop-1"})
        self.phone.send_bytes.assert_not_awaited()
        self.assertFalse(self.c.pending)
        self.c.remember_spoken(("received", "en"), "I heard you.")
        await self.c.handle_rt({"type": "conversation.item.input_audio_transcription.completed",
            "transcript": "I heard you.", "item_id": "echo-1"})
        self.phone.send_bytes.assert_not_awaited()
        self.assertFalse(self.c.pending)

    async def route(self, text, name, arguments="{}", status="completed"):
        self.c.turn_question = text
        await self.c.finish_route({"status": status, "output": [{"type": "function_call",
            "name": name, "call_id": "intent-check", "arguments": arguments}]})

    async def test_logged_phantom_yes_and_okay_do_not_acknowledge_or_start_tasks(self):
        self.c.tool = AsyncMock()
        self.c.generate = AsyncMock()
        for text, name in (("Yes.", "look"), ("Okay.", "get_observation"),
                           ("Okay.", "look"), ("好的。", "get_checklist"),
                           ("Yes.", "ask_expert"), ("Okay.", "manage_investigation")):
            await self.c.handle_rt({"type": "conversation.item.input_audio_transcription.completed",
                "transcript": text, "item_id": text})
            self.phone.send_bytes.assert_not_awaited()
            self.c.pending.clear()
            await self.route(text, name, '{"target":"current"}' if name == "look" else "{}")
        self.phone.send_bytes.assert_not_awaited()
        self.c.tool.assert_not_awaited()
        self.c.generate.assert_not_awaited()
        self.assertFalse(self.c.pending)

    async def test_allowed_query_acknowledges_before_its_tool_but_ignore_and_chat_do_not(self):
        self.c.tool = AsyncMock(side_effect=lambda *_: self.events.append({"type": "tool_started"}) or {"ok": True})
        self.c.generate = AsyncMock()
        await self.route("How many species today?", "get_checklist")
        self.assertLess(next(i for i, e in enumerate(self.events) if e["type"] == "pcm"),
                        next(i for i, e in enumerate(self.events) if e["type"] == "tool_started"))
        self.assertEqual([e.get("text") for e in self.events if e.get("role") == "assistant"], ["I heard you."])
        await self.c.flush_audio()
        self.events.clear()
        self.c.input_acknowledged = False
        await self.route("I was talking to my friend.", "ignore")
        await self.route("Hello!", "speak", '{"reason":"Greeting addressed to assistant"}')
        self.assertFalse(any(e["type"] == "pcm" for e in self.events))
        self.c.generate.assert_awaited()  # The greeting is still answered normally.

    async def test_rejected_failed_and_invalid_routes_do_not_play_receipts(self):
        self.c.tool = AsyncMock()
        self.c.generate = AsyncMock()
        for name, args in (("look", "null"), ("look", "[]"), ("look", "broken-json"),
                           ("look", '{"target":"current","group":"invalid"}'), ("describe", "{}"),
                           ("recent_sounds", '{"seconds":"garbage"}'),
                           ("recent_sounds", '{"seconds":null}'),
                           ("recent_sounds", '{"seconds":NaN}'),
                           ("unknown_tool", "{}"),
                           ("confirm_observation", "{}"),
                           ("speak", '{"reason":"unintelligible filler; ignore per routing rules"}')):
            await self.route("Second.", name, args)
        for status in ("cancelled", "failed", "deferred"):
            await self.route("What is that?", "look", '{"target":"current"}', status=status)
        self.phone.send_bytes.assert_not_awaited()
        self.c.tool.assert_not_awaited()

    async def test_outdated_route_does_not_acknowledge_under_a_newer_input_revision(self):
        self.c.tool = AsyncMock(return_value={"ok": True})
        self.c.generate = AsyncMock()
        self.c.turn_revision = self.c.input_revision
        self.c.input_revision += 1  # Another transcript arrived during routing.
        await self.route("What is that?", "look", '{"target":"current"}')
        self.phone.send_bytes.assert_not_awaited()
        self.c.turn_revision = self.c.input_revision
        self.c.turn_stopped_at = s.time.monotonic() - 13
        await self.route("How many species today?", "get_checklist")
        self.phone.send_bytes.assert_not_awaited()

    async def test_okay_can_accept_latest_investigation_offer_without_extra_receipt(self):
        self.c.tool = AsyncMock(return_value={"ok": True, "answer": "The requested result."})
        self.c.speak_expert = AsyncMock()
        # A retained offer id alone does not make a later unrelated Okay consent.
        self.c.investigation_offer_ids = ["R1"]
        self.c.last_assistant_text = "Camera ready. Say start to take a photo."
        await self.route("Okay.", "get_investigations", '{"explain":true}')
        self.c.tool.assert_not_awaited()
        for text, offer in (("Okay.", "Also, investigation R1 is ready. Would you like me to explain?"),
                            ("好的。", "另外，调查 R1 已完成。要我讲一下结果吗？")):
            self.c.last_assistant_text = offer
            await self.route(text, "get_investigations", '{"explain":true}')
        self.assertEqual(self.c.speak_expert.await_count, 2)
        self.phone.send_bytes.assert_not_awaited()

    def test_all_bundled_clips_are_short_valid_pcm(self):
        for key, text in CLIPS.items():
            actual, pcm = recording(*key)
            self.assertEqual(actual, text)
            self.assertEqual(len(pcm) % 2, 0)
            self.assertTrue(0 < len(pcm) <= 6 * 48000)


if __name__ == "__main__":
    unittest.main()
