"""Offline shadow scoring/enrollment regressions: no Higgs, model downloads or keys."""
import asyncio
import base64
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch
from http.server import ThreadingHTTPServer

import numpy as np
import requests

import server as s
from expert import Settings
from speaker_client import pcm16_at_16k, speaker_request
from speaker_service import SpeakerEngine, handler
from test_server import FakePhone


class SpeakerServiceTests(unittest.TestCase):
    def setUp(self):
        (s.ROOT / "out").mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=s.ROOT / "out")
        self.directory = Path(self.tmp.name)
        self.embed = lambda x: np.array([np.mean(x), .5, .2], dtype=np.float32)
        self.engine = SpeakerEngine(self.directory, embed=self.embed)
        self.pcm = np.full(16000, 1000, dtype="<i2").tobytes()

    def tearDown(self):
        self.tmp.cleanup()

    def test_resampling_length_and_constant_signal(self):
        for n in (0, 1, 2, 3, 4, 48000, 48001, 960000):
            result = pcm16_at_16k(np.full(n, .25, dtype=np.float32))
            self.assertEqual(len(result) // 2, (n + 2) // 3)
            if n >= 48000:
                np.testing.assert_allclose(np.frombuffer(result, "<i2")[20:-20], 8192, atol=2)

    def test_short_audio_and_invalid_pcm(self):
        for action in ("enroll", "score"):
            result = self.engine.process(action, b"\0\0" * 4799, "owner")
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "audio_too_short")
        self.assertFalse(self.engine.profiles)
        with self.assertRaises(ValueError):
            self.engine.process("score", b"x")

    def test_normalized_mean_append_persistence_permissions_and_reset(self):
        self.engine.process("enroll", self.pcm, "owner")
        second = np.full(16000, -2000, dtype="<i2").tobytes()
        self.engine.process("enroll", second, "owner")
        row = self.engine.profiles["owner"]
        self.assertEqual(row["chunks"], 2)
        self.assertAlmostEqual(np.linalg.norm(row["embedding"]), 1, places=6)
        path = self.directory / "owner.json"
        if os.name == 'posix':
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        else:
            # Windows exposes DOS read/write bits here, not its ACL entries.
            self.assertTrue(os.access(path, os.R_OK | os.W_OK))
        loaded = SpeakerEngine(self.directory, embed=self.embed)
        self.assertEqual(loaded.profiles, self.engine.profiles)
        self.assertIn("owner", loaded.process("score", self.pcm)["scores"])
        loaded.reset("owner")
        self.assertFalse(path.exists())
        self.assertEqual(loaded.process("score", self.pcm)["scores"], {})

    def test_enrollment_chunks_and_unsafe_profile_rejected(self):
        result = self.engine.process("enroll", self.pcm * 20, "owner")
        self.assertEqual(result["chunks"], 4)
        for name in ("../owner", "", "/tmp/test", None, "a" * 65):
            with self.assertRaises(ValueError):
                self.engine.process("enroll", self.pcm, name)

    def test_http_json_raw_health_reset_and_client(self):
        http = ThreadingHTTPServer(("127.0.0.1", 0), handler(self.engine))
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        url = "http://127.0.0.1:" + str(http.server_port)
        try:
            self.assertTrue(requests.get(url + "/health", timeout=1).json()["loaded"])
            self.assertTrue(requests.post(url + "/enroll?profile=owner", data=self.pcm, timeout=1).json()["ok"])
            result = speaker_request(url, "score", np.full(48000, 1000 / 32768, dtype=np.float32))
            self.assertGreater(result["scores"]["owner"], .999)
            self.assertEqual(result["duration_s"], 1)
            self.assertEqual(requests.post(url + "/score", json={"pcm16": "!"}, timeout=1).status_code, 422)
            self.assertEqual(requests.post(url + "/score", json={"pcm16": ""}, timeout=1).json()["error"], "audio_too_short")
            self.assertTrue(requests.delete(url + "/reset?profile=owner", timeout=1).json()["ok"])
            self.assertFalse(self.engine.profiles)
            requests.post(url + "/enroll", json={"profile": "friend", "pcm16": base64.b64encode(self.pcm).decode()}, timeout=1).raise_for_status()
            self.assertTrue(requests.post(url + "/reset", json={"profile": "friend"}, timeout=1).json()["ok"])
        finally:
            http.shutdown()
            http.server_close()
            thread.join()


class ShadowSpeakerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.default_speaker_service = s.SPEAKER_SERVICE
        self.tmp = tempfile.TemporaryDirectory(dir=s.ROOT / "out")
        self.directory = Path(self.tmp.name)
        self.patches = [patch.object(s, "SPEAKER_SERVICE", "http://fake-speaker"),
                        patch.object(s, "RECORD_DIR", self.directory / "recordings"),
                        patch.object(s, "EVENT_LOG_DIR", self.directory / "logs"),
                        patch.object(Settings, "load", return_value=Settings(enabled=False))]
        for p in self.patches:
            p.start()
        self.c = s.Session("shadow-test")
        self.c.next_window = 10**12
        self.c.rt_send = AsyncMock()
        self.phone = FakePhone()
        self.client = s.Client(self.phone, self.c)
        self.c.clients.add(self.client)
        self.c.mic = self.client
        self.c.generate = AsyncMock(return_value={"status": "completed", "output": [
            {"type": "function_call", "name": "show_silently", "call_id": "shadow", "arguments": '{"text":"Possibly addressed"}'}]})
        self.request = patch.object(s, "speaker_request", return_value={"scores": {"owner": .8, "friend": .2}, "ms": 37})
        self.fake = self.request.start()

    async def asyncTearDown(self):
        await self.c.close()
        self.request.stop()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    async def feed(self, seconds, value=1000):
        left = round(seconds * s.SR)
        while left:
            n = min(left, s.SR)
            await self.c.ingest_audio(np.full(n, value, dtype="<i2").tobytes())
            left -= n

    async def vad(self, kind, item="item"):
        await self.c.handle_rt({"type": "input_audio_buffer.speech_" + kind, "item_id": item})

    async def final(self, text="Is that a hawk?", item="item"):
        await self.c.handle_rt({"type": "conversation.item.input_audio_transcription.completed", "item_id": item, "transcript": text})

    async def turn(self, text="Is that a hawk?", item="item"):
        await self.feed(1)
        await self.vad("started", item)
        await self.feed(.5, 8192)
        await self.vad("stopped", item)
        await self.final(text, item)
        await self.feed(.3)

    async def worker(self):
        self.c.connected = True
        self.c.turn_task = asyncio.create_task(self.c.turn_worker())
        self.c.tasks.append(self.c.turn_task)
        self.c.work_ready.set()
        async def completed():
            while self.c.pending or self.c.turn_in_progress:
                await asyncio.sleep(.001)
        await asyncio.wait_for(completed(), .5)

    async def scores_done(self):
        await asyncio.gather(*list(self.c.speaker_tasks))

    def rows(self):
        path = self.directory / "recordings" / "turns.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def logs(self):
        return [json.loads(line) for path in (self.directory / "logs").glob("*.jsonl") for line in path.read_text().splitlines()]

    async def test_off_by_default_and_no_requests(self):
        self.assertIsNone(self.default_speaker_service)
        with patch.object(s, "SPEAKER_SERVICE", None), patch.object(s, "RECORD_DIR", None):
            other = s.Session("off")
        try:
            self.assertIsNone(other.speaker_service)
            self.assertIsNone(other.recorder)
        finally:
            await other.close()
        self.fake.assert_not_called()

    async def test_scores_log_and_join_row_after_route(self):
        await self.turn()
        await self.worker()
        await self.scores_done()
        row, = self.rows()
        self.assertEqual(row["speaker_scores"], {"owner": .8, "friend": .2})
        self.assertEqual(row["speaker_best_profile"], "owner")
        self.assertEqual(row["speaker_ms"], 37)
        self.assertEqual(row["routed_tool"], "show_silently")
        event, = [e for e in self.logs() if e["event"] == "speaker_scores"]
        self.assertEqual(event["turn_id"], row["turn_id"])
        self.assertEqual(event["speaker_scores"], row["speaker_scores"])

    async def test_low_cosine_never_changes_routing(self):
        self.fake.return_value = {"scores": {"stranger": -.95}, "ms": 1}
        await self.turn()
        await self.scores_done()
        await self.worker()
        row, = self.rows()
        self.assertEqual(row["speaker_scores"], {"stranger": -.95})
        self.assertEqual(row["routed_tool"], "show_silently")
        self.assertTrue(any(e.get("type") == "silent_note" for e in self.phone.events))

    async def test_empty_profiles_and_profile_names_are_logged_as_scores(self):
        self.fake.return_value = {"scores": {}, "ms": 1}
        await self.turn("Uh", "empty")
        await self.scores_done()
        self.assertIsNone(self.rows()[0]["speaker_best_profile"])
        self.fake.return_value = {"scores": {"monkey": .7, "secret": .1}, "ms": 1}
        await self.turn("Uh", "named")
        await self.scores_done()
        row = self.rows()[1]
        self.assertEqual(row["speaker_scores"], {"monkey": .7, "secret": .1})
        event = [e for e in self.logs() if e["event"] == "speaker_scores"][-1]
        self.assertEqual(event["speaker_scores"], row["speaker_scores"])

    async def test_playback_guarded_turn_still_scores_without_changing_guard(self):
        self.client.playback_active = True
        await self.turn("Add two mallards")
        await self.scores_done()
        self.fake.assert_called_once()
        row, = self.rows()
        self.assertEqual(row["outcome"], "guarded_during_playback")
        self.assertTrue(self.c.echo_blocked())
        self.assertFalse(self.c.pending)

    async def test_service_failure_ignored_and_routing_unchanged(self):
        self.fake.side_effect = requests.ConnectionError("offline")
        await self.turn()
        await self.worker()
        await self.scores_done()
        self.c.generate.assert_awaited_once_with("route")
        row, = self.rows()
        self.assertEqual(row["routed_tool"], "show_silently")
        self.assertEqual(row["speaker_error"], "ConnectionError")
        self.assertTrue(any(e["event"] == "speaker_score_failed" for e in self.logs()))
        self.assertFalse(any(e.get("type") == "error" for e in self.phone.events))

    async def test_slow_service_never_delays_route_and_timeout_is_ignored(self):
        entered = threading.Event()
        def slow(*args):
            entered.set()
            time.sleep(2)
            return {"scores": {"owner": -1}, "ms": 2000}
        self.fake.side_effect = slow
        await self.turn()
        await asyncio.wait_for(asyncio.to_thread(entered.wait), .5)
        before = time.monotonic()
        await self.worker()
        self.assertLess(time.monotonic() - before, .2)
        self.c.generate.assert_awaited_once_with("route")
        self.assertTrue(self.c.speaker_tasks)  # Still scoring after route completion.
        await self.scores_done()
        row, = self.rows()
        self.assertEqual(row["routed_tool"], "show_silently")
        self.assertEqual(row["speaker_error"], "TimeoutError")

    async def test_scoring_without_record_directory_or_wavs(self):
        await self.c.close()
        with patch.object(s, "RECORD_DIR", None):
            self.c = s.Session("no-wavs")
        self.c.rt_send = AsyncMock()
        self.c.next_window = 10**12
        await self.turn("Uh")
        await self.scores_done()
        self.fake.assert_called_once()
        self.assertTrue(any(e["event"] == "speaker_scores" for e in self.logs()))
        self.assertFalse((self.directory / "recordings").exists())
        self.assertFalse(self.c.recorder.pending)

    async def test_ignored_echo_and_failed_transcription_are_scored(self):
        await self.turn("Uh", "filler")
        self.c.transcript_is_echo = lambda text: True
        await self.turn("An assistant echo", "echo")
        await self.vad("started", "failed")
        await self.feed(.5)
        await self.vad("stopped", "failed")
        await self.c.handle_rt({"type": "conversation.item.input_audio_transcription.failed", "item_id": "failed"})
        await self.feed(.3)
        await self.scores_done()
        self.assertEqual(self.fake.call_count, 3)
        self.assertEqual(len(self.rows()), 3)
        self.assertTrue(all("speaker_scores" in row for row in self.rows()))
        self.c.generate.assert_not_awaited()

    async def test_enrollment_exact_ring_window_and_delayed_final_suppression(self):
        def request(url, action, samples, profile=None):
            return {"ok": True, "duration_s": len(samples) / s.SR} if action == "enroll" else {"scores": {}, "ms": 1}
        self.fake.side_effect = request
        await self.feed(1, -1000)
        await self.c.handle_phone({"type": "enroll_start", "profile": "owner"}, self.client)
        await self.feed(.25, 2000)
        await self.vad("started")
        await self.feed(.5, 4000)
        await self.vad("stopped")
        await self.c.handle_phone({"type": "enroll_stop"}, self.client)
        await self.final("Add two mallards")  # Reading finishes after enroll_stop.
        await self.feed(.3, 8000)
        await self.scores_done()
        calls = [call for call in self.fake.call_args_list if call.args[1] == "enroll"]
        self.assertEqual(len(calls), 1)
        np.testing.assert_array_equal(calls[0].args[2], np.r_[np.full(12000, 2000 / 32768), np.full(24000, 4000 / 32768)])
        self.assertEqual(calls[0].args[3], "owner")
        self.assertFalse(self.c.pending)
        self.c.generate.assert_not_awaited()
        row, = self.rows()
        self.assertEqual((row["outcome"], row["reason"]), ("input_ignored", "enrollment"))
        self.assertTrue(any(e.get("status") == "ok" and e.get("duration_s") == .75 for e in self.phone.events))
        self.assertTrue(any(e["event"] == "input_ignored" and e.get("reason") == "enrollment" for e in self.logs()))
        # The window does not become a general speaker gate.
        await self.turn("Is that a hawk?", "after")
        self.assertTrue(self.c.pending)

    async def test_enrollment_provenance_survives_vad_record_expiry(self):
        await self.c.start_enrollment("owner")
        await self.vad("started")
        await self.feed(.5)
        await self.vad("stopped")
        self.fake.return_value = {"ok": True, "scores": {}, "ms": 1}
        await self.c.stop_enrollment()
        self.c.input_audio_turns[0]["at"] -= 31
        await self.final("Add two mallards")
        await self.feed(.3)
        await self.scores_done()
        self.assertFalse(self.c.pending)
        row, = self.rows()
        self.assertEqual(row["reason"], "enrollment")

    async def test_enrollment_cap_reset_and_display_denied(self):
        with self.assertRaises(ValueError):
            await self.c.handle_phone({"type": "enroll_start"}, s.Client(FakePhone(), self.c, "display"))
        await self.c.start_enrollment("owner")
        self.c.position = 21 * s.SR
        self.fake.return_value = {"ok": True}
        await self.c.stop_enrollment()
        await self.scores_done()
        self.assertEqual(len(self.fake.call_args.args[2]), 20 * s.SR)
        await self.c.start_enrollment("owner")
        window = self.c.enrollment
        await self.c.reset()
        self.assertIsNone(self.c.enrollment)
        self.assertTrue(window["timer"].cancelled())

    async def test_server_auto_stops_enrollment_without_client_stop(self):
        self.fake.return_value = {"ok": True}
        await self.c.start_enrollment("owner")
        timer = self.c.enrollment["timer"]
        self.assertAlmostEqual(timer.when() - asyncio.get_running_loop().time(), 20, delta=.1)
        await self.feed(20, 4000)
        timer._callback()  # Advance only this deadline; no 20-second test sleep.
        await self.scores_done()  # Auto-stop schedules the separate HTTP job.
        await self.scores_done()
        self.assertIsNone(self.c.enrollment)
        self.assertTrue(timer.cancelled())
        self.assertEqual(self.fake.call_args.args[1], "enroll")
        self.assertEqual(len(self.fake.call_args.args[2]), 20 * s.SR)
        self.assertTrue(any(e.get("status") == "ok" for e in self.phone.events))

    async def test_enrollment_failure_reports_seconds_not_routing_error(self):
        self.fake.side_effect = requests.Timeout()
        await self.c.start_enrollment("owner")
        await self.feed(.4)
        await self.c.stop_enrollment()
        await self.scores_done()
        result, = [e for e in self.phone.events if e.get("type") == "enrollment" and e.get("status") == "error"]
        self.assertEqual(result["duration_s"], .4)
        self.assertFalse(self.c.pending)


if __name__ == "__main__":
    unittest.main()
