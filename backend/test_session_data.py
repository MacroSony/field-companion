"""Field recorder regressions: synthetic PCM/events only, no model or sockets."""
import asyncio
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import wave

import numpy as np

import server as s
from expert import Settings
from session_data import extract_turn_audio
from test_server import FakePhone


class SessionDataTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        (s.ROOT / "out").mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=s.ROOT / "out")
        self.directory = Path(self.tmp.name)
        self.default_record_dir = s.RECORD_DIR
        self.patches = [patch.object(s, "EVENT_LOG_DIR", self.directory / "logs"),
                        patch.object(s, "RECORD_DIR", self.directory / "recordings"),
                        patch.object(Settings, "load", return_value=Settings(enabled=False))]
        for p in self.patches:
            p.start()
        self.c = s.Session("field-test")
        self.c.rt_send = AsyncMock()
        self.c.allowed_species = lambda: set()
        self.c.next_window = 10**12  # No BirdNET workers.
        self.phone = FakePhone()
        self.client = s.Client(self.phone, self.c)
        self.c.clients.add(self.client)
        self.c.mic = self.client
        self.workers = []

    async def asyncTearDown(self):
        for task in self.workers:
            task.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        await self.c.close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    async def feed(self, seconds, value=1000):
        left = round(seconds * s.SR)
        while left:
            n = min(left, s.SR)
            await self.c.ingest_audio(np.full(n, value, dtype="<i2").tobytes())
            left -= n

    async def vad(self, kind, item_id="item"):
        await self.c.handle_rt({"type": "input_audio_buffer.speech_" + kind,
                                "item_id": item_id,
                                "audio_start_ms" if kind == "started" else "audio_end_ms": 999999})

    async def final(self, text, item_id="item"):
        await self.c.handle_rt({"type": "conversation.item.input_audio_transcription.completed",
                                "item_id": item_id, "transcript": text})

    async def turn(self, text=None, item_id="item"):
        await self.feed(1)  # enough history for the 1 s pre-padding
        await self.vad("started", item_id)
        await self.feed(.5, 8192)
        await self.vad("stopped", item_id)
        if text is not None:
            await self.final(text, item_id)
        await self.feed(.3)

    def rows(self):
        path = self.directory / "recordings" / "turns.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    async def run_worker(self):
        self.c.connected = True
        self.c.turn_task = asyncio.create_task(self.c.turn_worker())
        self.workers.append(self.c.turn_task)
        self.c.work_ready.set()
        async def completed():
            while self.c.pending or self.c.turn_in_progress:
                await asyncio.sleep(.001)
        await asyncio.wait_for(completed(), 1)

    async def test_persistent_log_is_jsonl_timestamped_and_redacts_secrets(self):
        with patch.dict("os.environ", {"BOSON_API_KEY": "fake-boson-credential", "OPENAI_API_KEY": "fake-gpt-credential"}):
            self.c.log("diagnostic", api_key="other-secret", headers={"Authorization": "Basic header-secret"},
                       nested=[{"token": "nested-secret"}], text="fake-boson-credential fake-gpt-credential sk-fake-secret Bearer bearer-secret",
                       detail='Authorization: Basic encoded-secret',
                       arguments='{"api_key":"encoded-key-secret"}')
            self.c.log("second")
        paths = list((self.directory / "logs").glob("session-????????.jsonl"))
        self.assertEqual(len(paths), 1)
        raw = paths[0].read_text()
        for secret in ("other-secret", "header-secret", "nested-secret", "fake-boson-credential",
                       "fake-gpt-credential", "sk-fake-secret", "bearer-secret", "encoded-secret", "encoded-key-secret"):
            self.assertNotIn(secret, raw)
        rows = [json.loads(line) for line in raw.splitlines()]
        self.assertEqual([r["event"] for r in rows], ["diagnostic", "second"])
        self.assertEqual(rows[0]["connection"], "field-test")
        self.assertIn("T", rows[0]["timestamp"])

    async def test_log_file_can_be_disabled(self):
        with patch.object(s, "EVENT_LOG_DIR", None):
            self.c.log("stdout_only")
        self.assertFalse((self.directory / "logs").exists())

    async def test_recorder_is_off_by_default(self):
        self.assertIsNone(self.default_record_dir)
        with patch.object(s, "RECORD_DIR", None):
            other = s.Session("default-off")
        try:
            self.assertIsNone(other.recorder)
            other.rt_send = AsyncMock()
            await other.handle_rt({"type": "input_audio_buffer.speech_started", "item_id": "off"})
            await other.handle_rt({"type": "input_audio_buffer.speech_stopped", "item_id": "off"})
        finally:
            await other.close()
        self.assertFalse((self.directory / "recordings").exists())

    async def test_wav_has_exact_padding_boundaries_and_level_features(self):
        # A short fake ring makes this interval cross its circular wrap.
        self.c.ring = np.zeros(s.SR, dtype=np.float32)
        self.c.echo_ring = np.zeros(s.SR, dtype=bool)
        await self.feed(1.2, 4096)
        await self.vad("started")
        await self.feed(.4, 16384)
        await self.vad("stopped")
        await self.final("Uh")
        await self.feed(.3, 4096)
        row, = self.rows()
        # 1 s pre-padding before the VAD start marker, 0.3 s after the stop marker.
        self.assertEqual((row["start_sample"], row["end_sample"]), (round(.2*s.SR), round(1.9*s.SR)))
        self.assertFalse(row["clipped"])
        self.assertEqual(row["duration_s"], 1.7)
        self.assertEqual(row["speech_rms_dbfs"], -6.02)
        self.assertEqual(row["peak_dbfs"], -6.02)
        self.assertEqual(row["noise_floor_dbfs"], -18.06)
        self.assertEqual(row["speech_p90_dbfs"], -6.02)
        with wave.open(str(self.directory / "recordings" / row["wav_filename"]), "rb") as wav:
            self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()), (1, 2, s.SR, round(1.7*s.SR)))
            samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
        np.testing.assert_array_equal(samples, np.concatenate([
            np.full(round(1*s.SR), 4096), np.full(round(.4*s.SR), 16384), np.full(round(.3*s.SR), 4096)]))

    async def test_turn_longer_than_ring_is_preserved_incrementally(self):
        self.c.ring = np.zeros(s.SR, dtype=np.float32)
        self.c.echo_ring = np.zeros(s.SR, dtype=bool)
        await self.feed(1)
        await self.vad("started")
        for _ in range(6):
            await self.feed(.5, 8192)
        # Age out the separate 30 s safety provenance, without a real sleep.
        self.c.input_audio_turns[0]["at"] -= 31
        await self.vad("stopped")
        self.assertFalse(self.c.input_audio_turns)
        await self.final("Uh")
        await self.feed(.3)
        row, = self.rows()
        self.assertFalse(row["clipped"])
        self.assertEqual(row["duration_s"], 4.3)
        with wave.open(str(self.directory / "recordings" / row["wav_filename"]), "rb") as wav:
            self.assertEqual(wav.getnframes(), round(4.3 * s.SR))

    async def test_reusable_extraction_clips_missing_and_overwritten_audio(self):
        ring = np.arange(10, dtype=np.float32)
        samples, bounds = extract_turn_audio(ring, 15, 3, 12, sample_rate=10, padding=.3)
        self.assertTrue(bounds["clipped"])
        self.assertEqual((bounds["start_sample"], bounds["end_sample"]), (5, 15))
        np.testing.assert_array_equal(samples, [5, 6, 7, 8, 9, 0, 1, 2, 3, 4])
        samples, bounds = extract_turn_audio(ring, 2, 0, 2, sample_rate=10, padding=.3)
        self.assertTrue(bounds["clipped"])
        np.testing.assert_array_equal(samples, [0, 1])

    async def test_echo_dropped_and_input_ignored_outcomes(self):
        self.c.transcript_is_echo = lambda text: text == "an assistant echo"
        await self.turn("an assistant echo", "echo")
        await self.turn("Uh", "filler")
        rows = self.rows()
        self.assertEqual([r["outcome"] for r in rows], ["echo_dropped", "input_ignored"])
        self.assertEqual(rows[1]["reason"], "empty_or_filler")
        self.assertEqual([r["transcript"] for r in rows], ["an assistant echo", "Uh"])
        self.assertFalse(any(r["assistant_audio"] for r in rows))

    async def test_routed_outcome_joins_later_worker_audio_and_arguments(self):
        async def generate(phase):
            if phase == "route":
                return {"status": "completed", "output": [{"type": "function_call", "name": "speak", "call_id": "c1",
                    "arguments": json.dumps({"reason": "addressed question", "token": "not-for-disk"})}]}
            self.c.phase = "answer"
            self.c.response_id = "answer"
            # Replay the independent reader task, not the worker's context.
            async def reader():
                self.c.recording_audio_context.set(())
                await self.c.handle_rt({"type": "response.audio.delta", "response_id": "answer",
                                        "delta": base64.b64encode(b"\0\0" * 240).decode()})
            await asyncio.create_task(reader())
            return {"status": "completed"}
        self.c.generate = generate
        await self.turn("Can you hear me?")
        self.assertEqual(self.rows(), [])  # Audio is saved; route is still pending.
        await self.run_worker()
        row, = self.rows()
        self.assertEqual((row["item_id"], row["outcome"], row["routed_tool"]), ("item", "routed", "speak"))
        self.assertEqual(row["routed_arguments"]["reason"], "addressed question")
        self.assertEqual(row["routed_arguments"]["token"], "[redacted]")
        self.assertTrue(row["assistant_audio"])
        self.assertEqual(row["audio_mode"], {"mode": "speaker", "echo_guard": True})

    async def test_show_silently_records_routed_without_assistant_audio(self):
        self.c.generate = AsyncMock(return_value={"status": "completed", "output": [
            {"type": "function_call", "name": "show_silently", "call_id": "silent",
             "arguments": '{"text":"A possibly addressed nature question"}'}]})
        await self.turn("Is that a hawk?")
        await self.run_worker()
        row, = self.rows()
        self.assertEqual((row["transcript"], row["outcome"], row["routed_tool"]),
                         ("Is that a hawk?", "routed", "show_silently"))
        self.assertFalse(row["assistant_audio"])
        self.assertEqual(row["routed_arguments"], {"text": "A possibly addressed nature question"})
        self.assertEqual([e["type"] for e in self.phone.events if e["type"] != "state"],
                         ["transcript", "silent_note"])
        self.c.generate.assert_awaited_once_with("route")
        self.assertFalse(self.phone.audio)

    async def test_reused_item_id_and_merged_turns_keep_distinct_rows(self):
        await self.turn("Can you hear me?", "reused")
        await self.turn("Is this thing working?", "reused")
        self.c.generate = AsyncMock(return_value={"status": "completed", "output": [
            {"type": "function_call", "name": "ignore", "call_id": "merged", "arguments": "{}"}]})
        await self.run_worker()
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({r["turn_id"] for r in rows}), 2)
        self.assertEqual({r["transcript"] for r in rows}, {"Can you hear me?", "Is this thing working?"})
        self.assertTrue(all(r["routed_tool"] == "ignore" for r in rows))

    async def test_guarded_turn_survives_without_transcript_or_route(self):
        self.c.echo_blocked = lambda: True
        await self.turn()
        self.c.recorder.flush("session_closed", self.c.position)
        row, = self.rows()
        self.assertEqual(row["outcome"], "guarded_during_playback")
        self.assertEqual(row["transcript"], "")
        self.assertFalse(self.c.pending)

    async def test_stop_and_camera_confirmation_outcomes(self):
        await self.turn("Stop", "stop")
        async def camera(text):
            await self.c.phone_audio(b"\0\0" * 24)
            return True
        self.c.camera_confirmation = camera
        await self.turn("Start", "camera")
        rows = self.rows()
        self.assertEqual([r["outcome"] for r in rows], ["stop", "camera_confirmation"])
        self.assertTrue(rows[1]["assistant_audio"])

    async def test_timeout_without_route_and_missing_post_padding_are_written_once(self):
        await self.feed(.4)
        await self.vad("started")
        await self.feed(.5)
        await self.vad("stopped")
        await self.final("Can you hear me?")
        turn_id, = self.c.recorder.pending
        self.c.recorder.finish(turn_id, "timeout")
        self.c.recorder.flush("session_closed", self.c.position)
        row, = self.rows()
        self.assertEqual((row["outcome"], row["reason"]), ("no_route", "timeout"))
        self.assertTrue(row["clipped"])

    async def test_reset_flushes_before_ring_is_cleared(self):
        await self.turn()
        await self.c.reset()
        row, = self.rows()
        self.assertEqual(row["reason"], "reset")
        self.assertEqual(row["duration_s"], 1.8)
        self.assertEqual(self.c.position, 0)
        self.assertFalse(self.c.recorder.pending)

    async def test_file_failures_do_not_break_input_or_logging(self):
        obstruction = self.directory / "not-a-directory"
        obstruction.write_text("fixture")
        with patch.object(s, "EVENT_LOG_DIR", obstruction):
            self.c.log("still_running")
        self.c.recorder.directory = obstruction
        await self.turn("Uh")
        self.assertFalse(self.c.recorder.pending)


if __name__ == "__main__":
    unittest.main()
