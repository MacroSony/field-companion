"""Offline routing/session regressions; no credentials or external services."""
import asyncio
import base64
import json
import io
import math
from types import SimpleNamespace
from urllib.parse import urlencode
import time
import unittest
from unittest.mock import AsyncMock, patch
from PIL import Image

import numpy as np
import server as s
from expert import Settings


class FakePhone:
    def __init__(self):
        self.events, self.audio = [], []

    async def send_json(self, event):
        self.events.append(event)

    async def send_bytes(self, pcm):
        self.audio.append(pcm)


class ASGIClient:
    """Tiny ASGI HTTP harness; avoids adding httpx to the demo venv."""
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def post(self, path, params=None, content=b""):
        messages = []
        async def receive():
            return {"type": "http.request", "body": content, "more_body": False}
        async def send(message):
            messages.append(message)
        await s.app({"type": "http", "http_version": "1.1", "method": "POST" if content else "GET",
                     "scheme": "http", "path": path, "raw_path": path.encode(),
                     "query_string": urlencode(params or {}).encode(), "headers": [],
                     "server": ("test", 80), "client": ("test", 1)}, receive, send)
        body = b"".join(m.get("body", b"") for m in messages)
        return SimpleNamespace(status_code=messages[0]["status"], text=body.decode(errors="replace"),
                               json=lambda: json.loads(body))

    async def get(self, path):
        return await self.post(path)


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.phone = FakePhone()
        self.c = s.Session("test")
        self.c.expert = s.NaturalistExpert(settings=Settings(enabled=False))
        self.client = s.Client(self.phone, self.c)
        self.c.clients.add(self.client)
        self.c.rt_send = AsyncMock()
        self.c.allowed_species = lambda: set()
        self.c.mic = self.client

    async def asyncTearDown(self):
        await self.c.close()

    def seed(self, sci="Cardinalis cardinalis", count=2, session=None):
        session = session or self.c
        obs = s.Observation("ob" + str(len(session.observations)), status="confirmed", count=count,
                            fused=[{"sci": sci, "en": "Northern Cardinal", "photo": "", "p": 1}])
        session.observations[obs.id] = obs
        return obs

    async def route(self, name, args=None):
        await self.c.finish_route({"status": "completed", "output": [{"type": "function_call",
            "name": name, "call_id": "c1", "arguments": json.dumps(args or {})}]})

    async def test_hello_and_vad(self):
        await self.c.handle_phone({"type": "hello", "lang": "zh"})
        self.assertEqual([e["type"] for e in self.phone.events], ["state", "checklist", "suggestions", "investigations"])
        vad = self.c.session()["audio"]["input"]["turn_detection"]
        self.assertFalse(vad["create_response"])
        self.assertFalse(vad["interrupt_response"])  # manual barge-in only
        self.assertEqual(self.c.config("route")["tool_choice"], "required")
        self.assertEqual(self.c.config("answer")["tools"], [])

    async def test_logging_is_silent_with_shared_checklist(self):
        display = FakePhone()
        self.c.clients.add(s.Client(display, self.c, "display"))
        taxon = {"sci": "Cardinalis cardinalis", "en": "Northern Cardinal", "group": "Aves", "photo": "photo"}
        await self.c.handle_rt({"type": "response.output_audio.delta", "delta": "AAA="})
        with patch.object(s, "resolve", return_value=taxon):
            await self.route("add_observation", {"name": "cardinal", "group": "Aves", "count": 2})
        self.assertEqual(self.phone.audio, [])
        self.assertEqual(self.c.items[0]["count"], 2)
        self.assertIn("lat", self.c.items[0])
        self.assertEqual(self.phone.events, display.events)
        self.assertIn({"type": "earcon", "kind": "logged"}, self.phone.events)
        self.assertEqual([c.args[0]["type"] for c in self.c.rt_send.call_args_list], ["conversation.item.create"])
        await self.route("set_count", {"name": "Northern Cardinal", "count": 4})
        self.assertEqual(self.c.items[0]["count"], 4)
        await self.route("remove_observation", {"name": "Northern Cardinal"})
        self.assertFalse(self.c.items)

    async def test_ignore_no_speech_or_followup(self):
        await self.route("ignore")
        self.assertFalse(self.phone.events)
        self.assertFalse(self.phone.audio)
        self.assertEqual(self.c.rt_send.call_args.args[0]["item"]["type"], "function_call_output")

    async def test_read_and_speak_answer_only_when_allowed(self):
        self.c.generate = AsyncMock()
        for name in ("speak", "get_checklist", "recent_sounds"):
            await self.route(name)
        self.assertEqual(self.c.generate.await_count, 3)
        self.c.quiet = True
        await self.route("speak")
        self.assertEqual(self.c.generate.await_count, 3)
        self.c.tap_at = time.monotonic() - 11
        await self.route("get_checklist")
        self.assertEqual(self.c.generate.await_count, 3)
        self.c.tap_at = time.monotonic()
        await self.route("speak")
        self.assertEqual(self.c.generate.await_count, 4)

    async def test_answer_streams_before_response_done(self):
        self.c.phase = "answer"
        self.c.audio_allowed = True
        await self.c.handle_rt({"type": "response.output_audio.delta", "delta": "AAA="})
        self.assertEqual(self.phone.audio, [b"\0\0"])

    async def test_playback_waits_for_matching_drained_ack_even_before_play_starts(self):
        await self.c.handle_phone({"type": "hello", "playback_protocol": 1})
        self.c.phase, self.c.audio_allowed, self.c.response_id = "answer", True, "voice-1"
        await self.c.handle_rt({"type": "response.audio.delta", "response_id": "voice-1", "delta": "AAA="})
        self.assertTrue(self.client.playback_pending)
        self.assertIn({"type": "audio_start", "id": "voice-1"}, self.phone.events)
        # Network buffering or a suspended AudioContext can outlast the estimate.
        self.c.play_until = self.c.echo_estimate = 0
        await self.c.handle_phone({"type": "playback", "response_id": "voice-1", "active": False, "drained": False})
        self.assertTrue(self.c.playback_pending())
        self.assertTrue(self.c.echo_blocked())
        await self.c.handle_rt({"type": "response.done", "response": {"id": "voice-1", "status": "completed"}})
        self.assertIn({"type": "audio_end", "id": "voice-1"}, self.phone.events)
        await self.c.handle_phone({"type": "playback", "response_id": "older-voice", "active": False, "drained": True})
        self.assertTrue(self.c.playback_pending())
        await self.c.handle_phone({"type": "playback", "response_id": "voice-1", "active": False, "drained": True})
        self.assertFalse(self.c.playback_pending())

    async def test_stale_response_audio_and_done_cannot_join_or_finish_new_reply(self):
        self.c.phase, self.c.audio_allowed, self.c.response_id = "answer", True, "new-voice"
        self.c.response_waiter = asyncio.get_running_loop().create_future()
        await self.c.handle_rt({"type": "response.audio.delta", "response_id": "old-voice", "delta": "AAA="})
        await self.c.handle_rt({"type": "response.done", "response": {"id": "old-voice", "status": "completed"}})
        self.assertEqual(self.phone.audio, [])
        self.assertFalse(self.c.response_waiter.done())
        await self.c.handle_rt({"type": "response.done", "response": {"id": "new-voice", "status": "completed"}})
        self.assertTrue(self.c.response_waiter.done())

    async def test_notice_guard_is_rechecked_after_session_update(self):
        ready = True
        async def user_arrives(config):
            nonlocal ready
            ready = False
        self.c.update_session = AsyncMock(side_effect=user_arrives)
        response = await self.c.generate("answer", guard=lambda: ready)
        self.assertEqual(response["status"], "deferred")
        self.c.rt_send.assert_not_awaited()

    async def test_tap_cancels_and_drops_late_audio(self):
        self.c.phase = "answer"
        self.c.connected = self.c.active = True
        self.c.response_id = "r1"
        self.c.play_until = time.monotonic() + 5
        await self.c.handle_phone({"type": "tap"})
        self.assertIn({"type": "flush"}, self.phone.events)
        self.assertIn("response.cancel", [c.args[0]["type"] for c in self.c.rt_send.call_args_list])
        await self.c.handle_rt({"type": "response.output_audio.delta", "response_id": "r1", "delta": "AAA="})
        self.assertFalse(self.phone.audio)

    async def test_echo_guard_blocks_realtime_and_keeps_ring(self):
        display = FakePhone()
        self.c.clients.add(s.Client(display, self.c, "display", True))
        self.c.connected = True
        self.c.play_until = time.monotonic() + 1
        await self.c.ingest_audio(bytes(4800))
        self.assertTrue(self.c.state_payload()["echo_guard"])
        self.assertTrue(self.c.audio_queue.empty())
        self.assertEqual(self.c.position, 2400)
        self.c.play_until = time.monotonic() - .9
        await self.c.ingest_audio(bytes(4800))
        self.assertFalse(self.c.audio_queue.empty())

    async def test_audio_modes_and_display_override(self):
        self.c.play_until = time.monotonic() + 3
        self.assertTrue(self.c.echo_guard)  # speaker is the default, no display needed
        self.assertTrue(self.c.echo_blocked())
        await self.c.handle_phone({"type": "hello", "earphones": True})
        self.assertFalse(self.c.echo_guard)
        self.assertFalse(self.c.echo_blocked())
        display = s.Client(FakePhone(), self.c, "display", True)
        self.c.clients.add(display)
        self.assertTrue(self.c.echo_blocked())
        self.c.clients.remove(display)
        await self.c.handle_phone({"type": "audio_mode", "earphones": False})
        self.assertTrue(self.c.echo_blocked())
        with self.assertRaises(ValueError):
            await self.c.handle_phone({"type": "audio_mode", "earphones": "false"})

    async def test_playback_report_end_and_legacy_fallback(self):
        with patch.object(s, "time", SimpleNamespace(monotonic=lambda: 100)):
            self.c.echo_estimate = 102
            self.assertEqual(self.c.echo_deadline(), 102.8)
            await self.c.handle_phone({"type": "playback", "active": True})
            self.assertTrue(self.c.echo_blocked())
            await self.c.handle_phone({"type": "playback", "active": False})
            self.assertEqual(self.c.echo_deadline(), 102.5)  # early end cannot shorten estimate
        with patch.object(s, "time", SimpleNamespace(monotonic=lambda: 104)):
            await self.c.handle_phone({"type": "playback", "active": False})
            self.assertEqual(self.c.echo_deadline(), 104.5)
            await self.c.cancel_speech()  # flush retains the acoustic tail
            self.assertTrue(self.c.echo_blocked())
        with patch.object(s, "time", SimpleNamespace(monotonic=lambda: 104.51)):
            self.assertFalse(self.c.echo_blocked())

    async def test_guarded_vad_ignored_but_earphones_barge_in(self):
        self.c.play_until = time.monotonic() + 2
        self.c.response_id = "answer"
        with patch.object(self.c, "log") as log:
            await self.c.handle_rt({"type": "input_audio_buffer.speech_started"})
            log.assert_called_with("vad_ignored", reason="assistant_playback")
        self.assertFalse(self.c.user_speaking)
        self.assertFalse(self.c.awaiting_transcript)
        self.assertNotIn({"type": "flush"}, self.phone.events)
        self.c.rt_send.assert_not_awaited()
        await self.c.handle_phone({"type": "audio_mode", "earphones": True})
        await self.c.handle_rt({"type": "input_audio_buffer.speech_started"})
        self.assertTrue(self.c.user_speaking)
        self.assertIn({"type": "flush"}, self.phone.events)

    async def test_birdnet_drops_all_overlapping_windows_including_tail(self):
        self.c.bird_window = AsyncMock()
        self.c.play_until = time.monotonic() + 2
        with patch.object(self.c, "log") as log:
            for _ in range(3):
                await self.c.ingest_audio(bytes(2 * s.SR))
            log.assert_called_with("bird_window_skipped", reason="assistant_playback", stream_s=3.0)
        self.c.play_until = 0
        # Even after playback, windows containing earlier contaminated samples must drop.
        for _ in range(2):
            await self.c.ingest_audio(bytes(2 * s.SR))
        await asyncio.sleep(0)
        self.c.bird_window.assert_not_awaited()
        for _ in range(3):
            await self.c.ingest_audio(bytes(2 * s.SR))
        await asyncio.sleep(0)
        self.c.bird_window.assert_awaited()
        other = s.Session("earphones")
        try:
            client = s.Client(FakePhone(), other)
            client.earphones = True
            other.clients.add(client)
            other.bird_window = AsyncMock()
            other.play_until = time.monotonic() + 10
            for _ in range(3):
                await other.ingest_audio(bytes(2 * s.SR))
            await asyncio.sleep(0)
            other.bird_window.assert_awaited_once()
        finally:
            await other.close()

    async def test_echo_filter_drops_fragment_and_passes_genuine_question(self):
        self.c.phase = "answer"
        await self.c.handle_rt({"type": "response.created", "response": {"id": "r1"}})
        await self.c.handle_rt({"type": "response.output_audio_transcript.delta", "response_id": "r1",
                               "delta": "I appreciate the quiet woods. What would you like to ask?"})
        self.assertTrue(self.c.transcript_is_echo("I appreciate the."))
        self.assertTrue(self.c.transcript_is_echo("What would you like to ask?"))
        # Short replies are never treated as echo (the speaker-mode gate covers those);
        # answers that reuse the prompt's words must pass.
        self.assertFalse(self.c.transcript_is_echo("quiet"))
        self.assertFalse(self.c.transcript_is_echo("Yes, add them."))
        self.assertFalse(self.c.transcript_is_echo("Just the cardinal."))
        self.assertFalse(self.c.transcript_is_echo("Which bird has a red head and black wings?"))
        self.client.earphones = True  # filter is independent of the guard
        with patch.object(self.c, "log") as log:
            await self.c.handle_rt({"type": "conversation.item.input_audio_transcription.completed",
                                    "transcript": "I appreciate the."})
            log.assert_called_with("echo_dropped", text="I appreciate the.")
        self.assertFalse(self.c.pending)
        await self.c.handle_rt({"type": "conversation.item.input_audio_transcription.completed",
                                "transcript": "Which bird has a red head and black wings?"})
        self.assertEqual(len(self.c.pending), 1)
        self.c.spoken.clear()
        self.c.spoken.append(("old", "I appreciate the.", time.monotonic() - 11))
        self.assertFalse(self.c.transcript_is_echo("I appreciate the."))

    async def test_audio_fanout_and_session_isolation(self):
        silent, loud = FakePhone(), FakePhone()
        self.c.clients.update([s.Client(silent, self.c, "display"), s.Client(loud, self.c, "display", True)])
        await self.c.phone_audio(b"pcm")
        self.assertEqual(self.phone.audio, loud.audio)
        self.assertFalse(silent.audio)
        other = s.Session("other")
        try:
            self.seed()
            self.assertFalse(other.items)
        finally:
            await other.close()

    async def test_manual_edit_validation(self):
        obs = self.seed()
        await self.c.handle_phone({"type": "edit", "id": obs.id, "count": 3})
        self.assertEqual(self.c.items[0]["count"], 3)
        with self.assertRaises(ValueError):
            await self.c.handle_phone({"type": "edit", "id": obs.id, "count": -1})
        await self.c.handle_phone({"type": "delete", "id": obs.id})
        self.assertFalse(self.c.items)

    async def test_pcm_phase(self):
        self.c.connected = True
        x = np.arange(101, dtype="<i2").tobytes()
        await self.c.ingest_audio(x[:62])
        await self.c.ingest_audio(x[62:])
        split = self.c.audio_queue.get_nowait() + self.c.audio_queue.get_nowait()
        other = s.Session("other")
        try:
            other.connected = True
            await other.ingest_audio(x)
            self.assertEqual(split, other.audio_queue.get_nowait())
        finally:
            await other.close()

    async def test_birdnet_threshold_and_metadata(self):
        names = {"sci": "Cardinalis cardinalis", "en": "Northern Cardinal", "photo": "photo"}
        self.c.names[names["sci"]] = names
        self.c.analyze = lambda _: [{"scientific_name": names["sci"], "common_name": names["en"], "confidence": .8}]
        await self.c.bird_window(np.zeros(1), 3 * s.SR)
        self.assertFalse(self.c.pending)
        await self.c.bird_window(np.zeros(1), int(4.5 * s.SR))
        self.assertFalse(self.c.pending)
        self.assertEqual(len(self.c.suggestions), 1)
        now = time.monotonic()
        await self.c.suggestion_tick(now + 2.9)
        self.assertFalse(self.c.pending)
        await self.c.suggestion_tick(now + 3.6)
        self.assertEqual(len(self.c.pending), 1)
        self.assertEqual(len(self.c.history), 2)
        heard = next(e for e in self.phone.events if e["type"] == "heard")
        self.assertTrue(all(k in heard for k in ("en", "sci", "photo", "lat", "lng")))

    def seed_suggestions(self, n=2):
        now = time.monotonic()
        for i in range(n):
            sci = f"Bird {i}"
            self.c.suggestions[sci] = {"sci": sci, "en": f"English Bird {i}", "photo": "",
                "conf": .7 + i * .05, "t": s.now_iso(), "first_heard": now - 2,
                "last_heard": now, "buffered_at": now, "prompt_at": None}
            self.c.notified.add(sci)
        return now

    async def test_batch_rate_limit_and_stale_expiry(self):
        now = self.seed_suggestions()
        self.c.last_proactive = now - 10
        await self.c.suggestion_tick(now + 3.6)
        self.assertFalse(self.c.pending)
        await self.c.suggestion_tick(now + 15)
        await self.c.suggestion_tick(now + 16)
        self.assertEqual(len(self.c.pending), 1)
        self.assertEqual([r["sci"] for r in self.c.suggestion_batch()], ["Bird 1", "Bird 0"])
        await self.c.suggestion_tick(now + 61)
        self.assertFalse(self.c.suggestions)

    async def test_prompt_expiry_not_extended_by_hearing(self):
        now = self.seed_suggestions()
        for r in self.c.suggestions.values():
            r.update(prompt_at=now, last_heard=now + 119)
        await self.c.suggestion_tick(now + 119)
        self.assertEqual(len(self.c.suggestions), 2)
        await self.c.suggestion_tick(now + 120)
        self.assertFalse(self.c.suggestions)
        self.assertEqual(len(self.c.notified), 2)

    async def test_accept_all_one_earcon_no_speech(self):
        self.seed_suggestions()
        self.c.generate = AsyncMock()
        await self.route("accept_suggestions", {"counts": {"English Bird 0": 2}})
        self.assertFalse(self.c.suggestions)
        self.assertEqual([r["count"] for r in self.c.items], [2, 1])
        self.assertTrue(all(r["source"] == "birdnet" for r in self.c.items))
        self.assertEqual(sum(e == {"type": "earcon", "kind": "logged"} for e in self.phone.events), 1)
        self.c.generate.assert_not_awaited()

    async def test_subset_dismiss_and_ui_action(self):
        self.seed_suggestions(3)
        await self.route("accept_suggestions", {"species": ["English Bird 1"]})
        self.assertEqual([r["sci"] for r in self.c.items], ["Bird 1"])
        await self.c.handle_phone({"type": "suggestion_action", "sci": "Bird 0", "action": "dismiss"})
        self.assertEqual(list(self.c.suggestions), ["Bird 2"])
        await self.route("dismiss_suggestions")
        self.assertFalse(self.c.suggestions)
        self.assertEqual(len(self.c.items), 1)

    async def test_accept_batch_preserves_open_focus_and_expert_context(self):
        focus = await self.c.new_focus()
        focus.detections.append(s.Detection("camera-evidence", "image", s.now_iso()))
        self.seed_suggestions()
        await self.route("accept_suggestions")
        self.assertEqual(self.c.focus, focus.id)
        self.assertEqual(focus.status, "open")
        self.assertEqual([d.id for d in focus.detections], ["camera-evidence"])
        confirmed = [o for o in self.c.observations.values() if o.status == "confirmed"]
        self.assertEqual(len(confirmed), 2)
        self.assertTrue(all(o.detections[0].kind == "sound" for o in confirmed))
        self.c.expert = SimpleNamespace(available=True, answer=AsyncMock())
        with patch.object(self.c.expert, "answer", new=AsyncMock(return_value={
                "ok": True, "status": "completed", "action": "clarify", "answer": "Please look closer."})) as expert:
            await self.c.tool("ask_expert", {"question": "What is it?"})
            async with asyncio.timeout(2):
                while not expert.await_count:
                    await asyncio.sleep(.005)
            self.assertEqual(expert.call_args.args[1]["id"], focus.id)

    async def test_sound_and_voice_records_share_derived_checklist_edits(self):
        self.seed_suggestions(1)
        await self.c.tool("accept_suggestions", {})
        with patch.object(s, "resolve", return_value={"sci": "Bird 0", "en": "English Bird 0", "group": "Aves"}):
            await self.c.tool("add_observations", {"items": [{"name": "English Bird 0", "count": 2}]})
        self.assertEqual(len(self.c.items), 1)
        self.assertEqual(self.c.items[0]["count"], 3)
        self.assertEqual(len([o for o in self.c.observations.values() if o.status == "confirmed"]), 2)
        row_id = self.c.items[0]["id"]
        await self.c.handle_phone({"type": "edit", "id": row_id, "count": 5})
        self.assertEqual(self.c.items[0]["count"], 5)
        await self.c.handle_phone({"type": "delete", "id": row_id})
        self.assertFalse(self.c.items)
        self.assertTrue(all(o.status == "discarded" for o in self.c.observations.values()))

    async def test_multi_add_atomic_and_one_earcon(self):
        taxons = [{"sci": "Anas platyrhynchos", "en": "Mallard"}, {"sci": "Ardea herodias", "en": "Great Blue Heron"}]
        with patch.object(s, "resolve", side_effect=taxons):
            await self.route("add_observations", {"items": [{"name": "mallard", "count": 2}, {"name": "heron"}]})
        self.assertEqual([r["count"] for r in self.c.items], [2, 1])
        self.assertEqual(sum(e.get("kind") == "logged" for e in self.phone.events), 1)
        with patch.object(s, "resolve", side_effect=[taxons[0], None]):
            result = await self.c.tool("add_observations", {"items": [{"name": "mallard"}, {"name": "unknown"}]})
        self.assertFalse(result["ok"])
        self.assertEqual(self.c.items[0]["count"], 2)

    async def test_worker_batch_guards_and_max_three(self):
        now = self.seed_suggestions(5)
        for r in self.c.suggestions.values():
            r["buffered_at"] = now - 4
        self.c.connected = True
        self.c.generate = AsyncMock(return_value={"status": "completed", "output": []})
        self.c.quiet = True
        self.c.tap_at = now  # taps must not enable proactive speech in quiet mode
        worker = asyncio.create_task(self.c.turn_worker())
        try:
            await self.c.suggestion_tick()
            await asyncio.sleep(.01)
            self.c.generate.assert_not_awaited()
            self.c.quiet = False
            for guard in ("active", "user_speaking", "awaiting_transcript"):
                setattr(self.c, guard, True)
                self.c.work_ready.set()
                await asyncio.sleep(.01)
                self.c.generate.assert_not_awaited()
                setattr(self.c, guard, False)
            self.c.play_until = now + 10
            self.c.work_ready.set()
            await asyncio.sleep(.01)
            self.c.generate.assert_not_awaited()
            self.c.play_until = 0
            self.c.work_ready.set()
            await asyncio.sleep(.01)
            self.c.generate.assert_awaited_once_with("answer")
            text = self.c.rt_send.call_args.args[0]["item"]["content"][0]["text"]
            self.assertIn("English Bird 4", text)
            self.assertNotIn("English Bird 0", text)
            self.assertIn("2 more on screen", text)
            self.assertTrue(all(r["prompt_at"] is not None for r in self.c.suggestions.values()))
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_best_confidence_distinct_windows_and_dedup(self):
        names = {"sci": "Cardinalis cardinalis", "en": "Northern Cardinal", "photo": ""}
        self.c.names[names["sci"]] = names
        detection = {"scientific_name": names["sci"], "common_name": names["en"], "confidence": .95}
        self.c.analyze = lambda _: [detection]
        await self.c.bird_window(np.zeros(1), 3 * s.SR)
        await self.c.bird_window(np.zeros(1), 3 * s.SR)
        self.assertFalse(self.c.suggestions)  # duplicate window is not a second hit
        detection["confidence"] = .55
        await self.c.bird_window(np.zeros(1), int(4.5 * s.SR))
        self.assertEqual(self.c.suggestions[names["sci"]]["conf"], .95)
        self.assertLess(self.c.suggestions[names["sci"]]["first_heard"], self.c.suggestions[names["sci"]]["last_heard"])
        await self.c.tool("dismiss_suggestions", {})
        await self.c.bird_window(np.zeros(1), 6 * s.SR)
        self.assertFalse(self.c.suggestions)

    async def test_invalid_accept_is_atomic_and_pending_context(self):
        self.seed_suggestions()
        self.assertIn("English Bird 0", self.c.config("route")["instructions"])
        with self.assertRaises(ValueError):
            await self.c.tool("accept_suggestions", {"counts": {"English Bird 1": -1}})
        self.assertFalse(self.c.items)
        self.assertEqual(len(self.c.suggestions), 2)
        with self.assertRaises(ValueError):
            await self.c.tool("accept_suggestions", {"species": ["unknown"]})
        self.assertFalse(self.c.items)
        await self.c.handle_phone({"type": "suggestion_action", "sci": "Bird 0", "action": "accept"})
        self.assertEqual([r["sci"] for r in self.c.items], ["Bird 0"])
        self.assertEqual(list(self.c.suggestions), ["Bird 1"])

    async def test_update_waits_for_matching_echo(self):
        config = self.c.config("answer")
        task = asyncio.create_task(self.c.update_session(config))
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        await self.c.handle_rt({"type": "session.updated", "session": config})
        await task

    async def test_transcription_clears_audio_and_enqueues_text(self):
        await self.c.handle_rt({"type": "input_audio_buffer.speech_stopped"})
        await self.c.handle_rt({"type": "conversation.item.input_audio_transcription.completed", "transcript": "two birds"})
        self.c.rt_send.assert_awaited_once_with({"type": "input_audio_buffer.clear"})
        self.assertEqual(self.c.pending[0][:2], ("text", "two birds"))
        self.assertIsNotNone(self.c.pending[0][2])

    async def test_realtime_reconnect_reinjects_checklist(self):
        class FakeRealtime:
            def __init__(inner):
                inner.sent = []
            async def __aenter__(inner):
                return inner
            async def __aexit__(inner, *args):
                return False
            async def send(inner, raw):
                inner.sent.append(json.loads(raw))
            async def recv(inner):
                return json.dumps({"type": "session.created", "session": self.c.session()})
            def __aiter__(inner):
                return inner
            async def __anext__(inner):
                raise StopAsyncIteration
        sessions = []
        def connect(url, **kwargs):
            self.assertIn("?model=" + s.MODEL, url)
            rt = FakeRealtime()
            sessions.append(rt)
            if len(sessions) == 2:
                self.c.closed = True
            return rt
        self.seed()
        self.c.rt_send = s.Session.rt_send.__get__(self.c)
        real_sleep = asyncio.sleep
        async def fast_sleep(delay):
            await real_sleep(0)
        with patch.object(s, "api_key", return_value="offline-placeholder"), \
             patch.object(s.websockets, "connect", side_effect=connect), \
             patch.object(s.asyncio, "sleep", side_effect=fast_sleep):
            await self.c.realtime_loop()
        self.assertEqual(len(sessions), 2)
        for rt in sessions:
            self.assertEqual(rt.sent[0]["session"]["model"], s.MODEL)
            self.assertIn("2 Northern Cardinal", rt.sent[1]["item"]["content"][0]["text"])

    async def test_new_mic_replaces_old_and_retains_session(self):
        class Socket(FakePhone):
            def __init__(self):
                super().__init__()
                self.queue = asyncio.Queue()
                self.closed = False
            async def receive(self):
                return await self.queue.get()
            async def close(self, **kwargs):
                self.closed = True
                await self.queue.put({"type": "websocket.disconnect"})
        self.c.clients.clear()
        self.c.mic = None
        self.seed()
        old, new = Socket(), Socket()
        old_client, new_client = s.Client(old, self.c), s.Client(new, self.c)
        old_task = asyncio.create_task(old_client.run())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        new_task = asyncio.create_task(new_client.run())
        for _ in range(10):
            await asyncio.sleep(0)
        self.assertTrue(old.closed)
        self.assertIs(self.c.mic, new_client)
        self.assertIsNone(self.c.expiry_task)
        self.assertNotIn(old_client, self.c.clients)
        self.assertTrue(any(e.get("items") == self.c.items for e in new.events))
        await new.close()
        await asyncio.gather(old_task, new_task)
        self.assertFalse(self.c.closed)
        self.assertIsNotNone(self.c.expiry_task)
        self.c.expiry_task.cancel()
        await asyncio.gather(self.c.expiry_task, return_exceptions=True)

    async def test_session_expires_after_last_client(self):
        s.SESSIONS[self.c.id] = self.c
        try:
            with patch.object(s, "SESSION_TTL", .001):
                await self.c.expire()  # connected sessions must not expire
                self.assertFalse(self.c.closed)
                self.c.clients.clear()
                await self.c.expire()
            self.assertTrue(self.c.closed)
            self.assertNotIn(self.c.id, s.SESSIONS)
        finally:
            s.SESSIONS.pop(self.c.id, None)

    async def test_update_serialization(self):
        route, answer = self.c.config("route"), self.c.config("answer")
        first = asyncio.create_task(self.c.update_session(route))
        second = asyncio.create_task(self.c.update_session(answer))
        await asyncio.sleep(0)
        self.assertEqual(self.c.rt_send.await_count, 1)
        await self.c.handle_rt({"type": "session.updated", "session": route})
        await first
        await asyncio.sleep(0)
        self.assertEqual(self.c.rt_send.await_count, 2)
        self.assertFalse(second.done())
        await self.c.handle_rt({"type": "session.updated", "session": answer})
        await second

    async def test_focus_capture_expiry_and_confirmed_retention(self):
        await self.route("look", {"hint": "bill"})
        obs = self.c.observations[self.c.focus]
        self.assertIn({"type": "capture", "obs": obs.id, "n": 3, "hint": "bill"}, self.phone.events)
        await self.route("look")
        self.assertEqual(self.c.focus, obs.id)
        obs.touched -= 121
        await self.c.expire_focus()
        self.assertIsNone(self.c.focus)
        self.assertEqual(obs.status, "discarded")
        new = await self.c.new_focus()
        new.status = "confirmed"
        new.touched -= 121
        await self.c.expire_focus()
        self.assertEqual(new.status, "confirmed")
        self.assertFalse(self.phone.audio)

    async def test_new_target_requests_capture_without_old_evidence_or_hint(self):
        saved = self.seed(count=2)
        self.c.focus = saved.id
        await self.route("new_observation", {"group": "Insecta"})
        target = self.c.observations[self.c.focus]
        self.assertNotEqual(target.id, saved.id)
        self.assertEqual(target.group, "Insecta")
        self.assertEqual(target.detections, [])
        self.assertTrue(target.awaiting_answer)
        self.assertEqual(saved.status, "confirmed")
        self.assertEqual(self.c.items[0]["count"], 2)
        self.assertIn({"type": "capture", "obs": target.id, "n": 3, "hint": ""}, self.phone.events)

        target.hint = "Old target's bill"
        self.phone.events.clear()
        await self.route("new_observation", {"group": "Plantae"})
        next_target = self.c.observations[self.c.focus]
        self.assertEqual(target.status, "discarded")
        self.assertEqual(next_target.group, "Plantae")
        self.assertEqual(next_target.hint, "")
        self.assertTrue(next_target.awaiting_answer)
        self.assertIn({"type": "capture", "obs": next_target.id, "n": 3, "hint": ""}, self.phone.events)
        self.assertEqual(len(self.c.items), 1)

        before = list(self.c.observations)
        with self.assertRaises(ValueError):
            await self.c.tool("new_observation", {"group": "invalid"})
        self.assertEqual(list(self.c.observations), before)
        self.assertEqual(self.c.focus, next_target.id)

    async def test_route_context_tracks_visual_focus_and_drops_discarded_focus(self):
        obs = await self.c.new_focus()
        candidate = {"sci": "Cyanocitta cristata", "en": "Blue Jay", "p": .9}
        obs.fused = [candidate]
        obs.detections = [s.Detection("photo-evidence", "image", s.now_iso(), [candidate])]
        context = self.c.config("route")["instructions"].split("\nCurrent focus snapshot (context only, not a new sighting): ")[1]
        snapshot = json.loads(context.splitlines()[0])
        self.assertEqual(snapshot["id"], obs.id)
        self.assertEqual(snapshot["top3"][0]["sci"], "Cyanocitta cristata")
        self.assertEqual(snapshot["image_detections"], 1)
        await self.c.tool("confirm_observation", {"count": 2})
        context = self.c.config("route")["instructions"].split("\nCurrent focus snapshot (context only, not a new sighting): ")[1]
        self.assertEqual(json.loads(context.splitlines()[0])["status"], "confirmed")
        await self.c.tool("discard_observation", {})
        context = self.c.config("route")["instructions"].split("\nCurrent focus snapshot (context only, not a new sighting): ")[1]
        self.assertIsNone(json.loads(context.splitlines()[0]))

    async def test_checklist_aggregation_maps_edits_to_observations(self):
        a = self.seed(count=2)
        b = self.seed(count=3)
        self.assertEqual(len(self.c.items), 1)
        self.assertEqual(self.c.items[0]["count"], 5)
        await self.c.handle_phone({"type": "edit", "id": a.id, "count": 4})
        self.assertEqual(self.c.items[0]["count"], 4)
        self.assertEqual(b.status, "discarded")
        await self.c.handle_phone({"type": "delete", "id": a.id})
        self.assertFalse(self.c.items)
        self.assertEqual(a.status, "discarded")

    async def test_sound_window_attached_once_and_range_filtered(self):
        self.c.names["a"] = {"sci": "a", "en": "A"}
        self.c.analyze = lambda _: [{"scientific_name": "a", "common_name": "A", "confidence": .8}]
        await self.c.request_look()
        obs = self.c.observations[self.c.focus]
        await self.c.bird_window(np.zeros(1), 3 * s.SR)
        self.assertEqual(obs.detections[0].kind, "sound")
        await self.c.request_look()
        self.assertEqual(len(obs.detections), 1)
        obs.touched -= 13
        await self.c.bird_window(np.zeros(1), 4 * s.SR)
        self.assertEqual(len(obs.detections), 1)

    async def test_confirm_focus_and_unconfigured_expert(self):
        obs = self.seed()
        obs.status = "open"
        self.c.focus = obs.id
        await self.route("confirm_observation", {"count": 2, "notes": "pair"})
        self.assertEqual(obs.status, "confirmed")
        self.assertEqual(self.c.items[0]["count"], 2)
        self.assertIn({"type": "earcon", "kind": "logged"}, self.phone.events)
        result = await self.c.tool("ask_expert", {"question": "bill?"})
        self.assertEqual(result["status"], "not_configured")
        self.assertEqual(result["action"], "abstain")
        self.assertTrue(result["answer"])
        await self.c.handle_phone({"type": "observation_action", "id": obs.id, "action": "discard"})
        self.assertFalse(self.c.items)

    async def test_nonbird_focus_uses_open_set_and_description_needs_name(self):
        self.c.allowed_species = lambda: (_ for _ in ()).throw(AssertionError("bird filter used for nonbird"))
        await self.c.tool("look", {"group": "Insecta"})
        obs = self.c.observations[self.c.focus]
        self.assertEqual(obs.group, "Insecta")
        with patch.object(s, "resolve") as resolve:
            await self.c.tool("describe", {"text": "red with black dots"})
            resolve.assert_not_called()
        self.assertEqual(obs.fused, [])
        with patch.object(s, "resolve", return_value={"sci": "Coccinella septempunctata", "en": "Seven-spot Ladybird", "group": "Insecta"}):
            await self.c.tool("describe", {"text": "seven spot ladybird", "name": "seven spot ladybird"})
        self.assertEqual(obs.fused[0]["sci"], "Coccinella septempunctata")

    async def test_sound_does_not_refresh_idle_focus(self):
        await self.c.request_look()
        obs = self.c.observations[self.c.focus]
        touched = obs.touched
        self.c.names["a"] = {"sci": "a", "en": "A"}
        self.c.analyze = lambda _: [{"scientific_name": "a", "common_name": "A", "confidence": .8}]
        await self.c.bird_window(np.zeros(1), 3 * s.SR)
        self.assertEqual(obs.touched, touched)
        self.assertEqual(len(obs.detections), 1)

    async def test_frame_endpoint_mock_service_and_thumbnail(self):
        obs = await self.c.new_focus()
        obs.awaiting_answer = True
        image = io.BytesIO()
        Image.new("RGB", (64, 64), "red").save(image, format="JPEG")
        s.SESSIONS[self.c.id] = self.c
        self.c.names["a"] = {"sci": "a", "en": "A"}
        try:
            # ASGI transport keeps the session on this event loop.
            async with ASGIClient() as client:
                with patch.object(s.requests, "post") as post:
                    post.return_value.json.return_value = {"candidates": [{"sci": "a", "p": .8}], "sharpness": 5, "ms": 10}
                    response = await client.post("/api/frame", params={"session": self.c.id, "obs": obs.id, "x": 0, "y": 0, "w": .5, "h": .5}, content=image.getvalue())
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(post.call_args.kwargs["json"]["crop"], [0, 0, .5, .5])
                    self.assertIsNone(post.call_args.kwargs["json"]["candidates"])
                detection = response.json()["detection"]
                self.assertEqual((await client.get(detection["thumb"])).status_code, 200)
                self.assertEqual(obs.fused[0]["sci"], "a")
                self.assertEqual(self.c.pending[0][:2], ("vision", obs.id))
                self.assertTrue(any(e["type"] == "observation" and e["detections"] for e in self.phone.events))
                with patch.object(s.requests, "post", side_effect=s.requests.ConnectionError):
                    response = await client.post("/api/frame", params={"session": self.c.id, "obs": obs.id}, content=image.getvalue())
                    self.assertEqual(response.status_code, 503)
                    self.assertIn("Vision service unavailable", response.text)
                response = await client.post("/api/frame", params={"session": self.c.id, "obs": obs.id}, content=b"not jpeg")
                self.assertEqual(response.status_code, 422)
                response = await client.post("/api/frame", params={"session": self.c.id, "obs": obs.id, "x": .9, "y": 0, "w": .5, "h": 1}, content=image.getvalue())
                self.assertEqual(response.status_code, 422)
                obs.status = "confirmed"
                response = await client.post("/api/frame", params={"session": self.c.id, "obs": obs.id}, content=image.getvalue())
                self.assertEqual(response.status_code, 409)
        finally:
            s.SESSIONS.pop(self.c.id, None)

    def test_fusion_synthetic_log_weights_priors_and_uncertainty(self):
        def detection(kind, a, b):
            return s.Detection("d", kind, s.now_iso(), [{"sci": "a", "en": "A", "p": a}, {"sci": "b", "en": "B", "p": b}])
        evidence = [detection("image", .8, .2), detection("sound", .1, .9)]
        fused, uncertain = s.fuse(evidence)
        # .8*sqrt(.1) / (.8*sqrt(.1)+.2*sqrt(.9)) = 4/7
        self.assertAlmostEqual(fused[0]["p"], 4 / 7)
        self.assertTrue(uncertain)
        fused, uncertain = s.fuse(evidence, priors={"a": 2})
        self.assertAlmostEqual(fused[0]["p"], .8)
        self.assertFalse(uncertain)
        fused, _ = s.fuse(evidence, allowed={"b"})
        self.assertEqual(fused[0]["sci"], "b")
        description = s.Detection("d2", "description", s.now_iso(), [{"sci": "b", "en": "B", "p": 1}])
        boosted, _ = s.fuse(evidence + [description])
        self.assertEqual(boosted[0]["sci"], "b")
        empty_description = s.Detection("d3", "description", s.now_iso(), text="red bird")
        self.assertEqual(s.fuse(evidence), s.fuse(evidence + [empty_description]))
        self.assertEqual(s.fuse([]), ([], True))

    def test_english_name_fallback(self):
        taxon = {"id": 42, "name": "Test bird", "preferred_common_name": "中文", "default_photo": {"square_url": "square"}}
        with patch.object(s, "inat", return_value={"results": [{"preferred_common_name": "English Bird"}]}) as lookup:
            names = s.taxon_names(taxon)
        self.assertEqual(names["en"], "English Bird")
        self.assertEqual(names["zh"], "中文")
        self.assertEqual(names["photo"], "square")
        lookup.assert_called_once_with("taxa/42", locale="en")


if __name__ == "__main__":
    unittest.main(verbosity=2)
