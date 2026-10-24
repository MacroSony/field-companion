"""Offline speaker-mode isolation, delayed STT and headset regressions."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import numpy as np

import server as s
from expert import NaturalistExpert, Settings
from test_server import FakePhone


class SpeakerModeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 100.0
        self.clock = patch.object(s, "time", SimpleNamespace(monotonic=lambda: self.now))
        self.clock.start()
        self.c = s.Session("speaker-mode-test")
        self.c.expert = NaturalistExpert(Settings(enabled=False))
        self.phone = FakePhone()
        self.client = s.Client(self.phone, self.c)
        self.client.playback_protocol = 1
        self.c.clients.add(self.client)
        self.c.mic = self.client
        self.c.connected = True
        self.c.rt_send = AsyncMock()
        self.c.allowed_species = lambda: set()
        self.senders = []

    async def asyncTearDown(self):
        for task in self.senders:
            task.cancel()
        await asyncio.gather(*self.senders, return_exceptions=True)
        await self.c.close()
        self.clock.stop()

    @staticmethod
    def pcm(samples=481):
        return ((np.arange(samples) % 17 - 8) * 1000).astype("<i2").tobytes()

    async def playback(self, response_id="assistant-answer"):
        await self.c.begin_audio(response_id)
        self.c.play_until = self.c.echo_estimate = self.now + 2
        return response_id

    async def drain(self, response_id="assistant-answer"):
        self.now += 2
        await self.c.end_audio()
        await self.c.handle_phone({"type": "playback", "active": False,
                                   "drained": True, "response_id": response_id}, self.client)

    async def vad(self, kind, item_id="reused-item", **extra):
        await self.c.handle_rt({"type": "input_audio_buffer.speech_" + kind,
                                "item_id": item_id, **extra})

    async def final(self, text, item_id="reused-item"):
        await self.c.handle_rt({"type": "conversation.item.input_audio_transcription.completed",
                                "item_id": item_id, "transcript": text})

    async def run_sender(self):
        task = asyncio.create_task(self.c.audio_sender())
        self.senders.append(task)
        await asyncio.wait_for(self.c.audio_queue.join(), .5)
        return task

    def transcripts(self, role="user"):
        return [e for e in self.phone.events if e.get("type") == "transcript"
                and e.get("role") == role]

    async def test_blocked_audio_updates_environment_and_filter_without_reaching_stt(self):
        await self.playback()
        raw = self.pcm()
        await self.c.ingest_audio(raw)
        self.assertEqual(self.c.position, len(raw) // 2)
        self.assertEqual(self.c.total_audio_in, len(raw))
        np.testing.assert_allclose(self.c.ring[:len(raw) // 2],
                                   np.frombuffer(raw, dtype="<i2") / 32768)
        self.assertTrue(self.c.echo_ring[:len(raw) // 2].all())
        self.assertTrue(np.any(self.c.filter_state))
        self.assertTrue(self.c.audio_queue.empty())
        self.c.rt_send.assert_not_awaited()

    async def test_audio_queued_before_playback_is_dropped_by_sender(self):
        self.c.rt = SimpleNamespace(send=AsyncMock(), close=AsyncMock())
        await self.c.ingest_audio(self.pcm())
        self.assertFalse(self.c.audio_queue.empty())
        await self.playback()
        await self.run_sender()
        self.c.rt.send.assert_not_awaited()

    async def test_sender_rechecks_playback_after_waiting_for_realtime_send_lock(self):
        sent = []
        self.c.rt = SimpleNamespace(send=AsyncMock(side_effect=lambda raw: sent.append(json.loads(raw))),
                                    close=AsyncMock())
        self.c.rt_send = s.Session.rt_send.__get__(self.c)
        await self.c.ingest_audio(self.pcm())
        await self.c.rt_lock.acquire()
        try:
            task = asyncio.create_task(self.c.audio_sender())
            self.senders.append(task)
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            await self.playback()
        finally:
            self.c.rt_lock.release()
        await asyncio.wait_for(self.c.audio_queue.join(), .5)
        self.assertFalse(any(event["type"] == "input_audio_buffer.append" for event in sent))

    async def test_blocked_vad_and_final_cannot_interrupt_caption_or_queue_a_turn(self):
        await self.playback()
        revision = self.c.input_revision
        cancel = self.c.cancel_speech = AsyncMock()
        await self.vad("started", audio_start_ms=10)
        await self.vad("stopped", audio_end_ms=400)
        await self.final("What is that?")
        self.assertEqual(self.c.input_revision, revision)
        self.assertFalse(self.c.user_speaking or self.c.awaiting_transcript)
        cancel.assert_not_awaited()
        self.assertFalse(self.transcripts())
        self.assertFalse(self.c.pending)

    async def test_final_without_vad_stays_blocked_if_playback_drains_during_clear(self):
        await self.playback()
        revision = self.c.input_revision
        self.c.cancel_speech = AsyncMock()
        async def clear_drains_playback(event):
            if event["type"] == "input_audio_buffer.clear":
                await self.drain()
                self.now += .51
        self.c.rt_send = AsyncMock(side_effect=clear_drains_playback)
        await self.final("What is that?")
        self.assertFalse(self.c.echo_blocked())
        self.assertEqual(self.c.input_revision, revision)
        self.c.cancel_speech.assert_not_awaited()
        self.assertFalse(self.c.user_speaking or self.c.awaiting_transcript)
        self.assertFalse(self.transcripts() or self.c.pending)

    async def test_blocked_transcription_failure_does_not_error_or_cancel_playback(self):
        await self.playback()
        revision = self.c.input_revision
        self.c.cancel_speech = AsyncMock()
        await self.vad("started", audio_start_ms=10)
        await self.vad("stopped", audio_end_ms=400)
        await self.c.handle_rt({"type": "conversation.item.input_audio_transcription.failed",
                                "item_id": "reused-item", "error": {"message": "synthetic failure"}})
        self.assertEqual(self.c.input_revision, revision)
        self.c.cancel_speech.assert_not_awaited()
        self.assertFalse(self.c.user_speaking or self.c.awaiting_transcript)
        self.assertFalse(self.transcripts() or self.c.pending)
        self.assertFalse(any(e.get("type") == "error" or e == {"type": "earcon", "kind": "error"}
                             for e in self.phone.events))

    async def test_late_blocked_stop_preserves_newer_speech_with_reused_id(self):
        await self.playback()
        await self.vad("started", audio_start_ms=10)
        await self.drain()
        self.now += .51
        await self.vad("started", audio_start_ms=3000)
        revision = self.c.input_revision
        stopped_at = self.c.speech_stopped_at
        await self.vad("stopped", audio_end_ms=400)
        self.assertEqual(self.c.input_revision, revision)
        self.assertEqual(self.c.speech_stopped_at, stopped_at)
        self.assertTrue(self.c.user_speaking and self.c.awaiting_transcript)
        await self.final("Yes.")
        self.assertTrue(self.c.user_speaking and self.c.awaiting_transcript)
        await self.vad("stopped", audio_end_ms=3800)
        await self.final("What does it eat?")
        self.assertEqual([e["text"] for e in self.transcripts()], ["What does it eat?"])
        self.assertEqual([str(item[1]) for item in self.c.pending], ["What does it eat?"])

    async def test_late_blocked_final_cannot_clear_newer_genuine_turn_with_reused_id(self):
        await self.playback()
        await self.vad("started", audio_start_ms=10)
        await self.vad("stopped", audio_end_ms=400)
        await self.drain()
        self.now += .51
        await self.vad("started", audio_start_ms=3000)
        revision = self.c.input_revision
        self.assertTrue(self.c.user_speaking and self.c.awaiting_transcript)
        self.c.rt_send.reset_mock()
        await self.final("Yes.")
        self.assertEqual(self.c.input_revision, revision)
        self.assertTrue(self.c.user_speaking and self.c.awaiting_transcript)
        self.c.rt_send.assert_not_awaited()  # Neither clear nor delete the reused active item.
        self.assertFalse(self.transcripts() or self.c.pending)
        self.c.rt_send.reset_mock()
        await self.vad("stopped", audio_end_ms=3800)
        await self.final("How many species today?")
        self.assertFalse(self.c.user_speaking or self.c.awaiting_transcript)
        self.assertEqual([e["text"] for e in self.transcripts()], ["How many species today?"])
        self.assertEqual([str(item[1]) for item in self.c.pending], ["How many species today?"])
        self.c.rt_send.assert_awaited_once_with({"type": "input_audio_buffer.clear"})

    async def test_half_second_tail_blocks_audio_then_new_speech_passes(self):
        await self.playback()
        await self.drain()
        self.now += .49
        await self.c.ingest_audio(self.pcm())
        self.assertTrue(self.c.audio_queue.empty())
        self.now += .02
        await self.c.ingest_audio(self.pcm())
        self.assertFalse(self.c.audio_queue.empty())
        await self.vad("started", audio_start_ms=3000)
        await self.vad("stopped", audio_end_ms=3500)
        await self.final("What does it eat?")
        self.assertEqual([e["text"] for e in self.transcripts()], ["What does it eat?"])
        self.assertEqual([str(item[1]) for item in self.c.pending], ["What does it eat?"])

    async def test_earphones_keep_full_duplex_vad_and_question_interruption(self):
        await self.c.handle_phone({"type": "audio_mode", "earphones": True}, self.client)
        await self.playback()
        await self.c.ingest_audio(self.pcm())
        self.assertFalse(self.c.audio_queue.empty())
        self.c.cancel_speech = AsyncMock()
        await self.vad("started", audio_start_ms=10)
        self.c.cancel_speech.assert_awaited_once()
        self.assertTrue(self.c.user_speaking and self.c.awaiting_transcript)
        await self.vad("stopped", audio_end_ms=500)
        await self.final("What does it eat?")
        self.assertEqual([e["text"] for e in self.transcripts()], ["What does it eat?"])
        self.assertEqual([str(item[1]) for item in self.c.pending], ["What does it eat?"])

    async def test_earphones_still_gate_for_audio_display_then_resume_when_disabled(self):
        await self.c.handle_phone({"type": "audio_mode", "earphones": True}, self.client)
        display = s.Client(FakePhone(), self.c, role="display", audio=True)
        display.playback_protocol = 1
        self.c.clients.add(display)
        await self.playback()
        await self.c.ingest_audio(self.pcm())
        self.assertTrue(self.c.audio_queue.empty())
        await self.vad("started", audio_start_ms=10)
        await self.vad("stopped", audio_end_ms=500)
        display.audio = False
        await self.final("Yes.")
        self.assertFalse(self.transcripts() or self.c.pending)
        await self.c.ingest_audio(self.pcm())
        self.assertFalse(self.c.audio_queue.empty())
        await self.vad("started", audio_start_ms=1000)
        await self.vad("stopped", audio_end_ms=1500)
        await self.final("How many species today?")
        self.assertEqual([e["text"] for e in self.transcripts()], ["How many species today?"])

    async def test_tap_cancels_speaker_audio_and_routes_after_acoustic_tail(self):
        await self.playback()
        await self.c.handle_phone({"type": "tap"}, self.client)
        self.assertIn({"type": "flush"}, self.phone.events)
        self.assertFalse(self.c.playback_pending())
        await self.c.ingest_audio(self.pcm())
        self.assertTrue(self.c.audio_queue.empty())
        self.now += .51
        await self.c.ingest_audio(self.pcm())
        self.assertFalse(self.c.audio_queue.empty())
        await self.vad("started", audio_start_ms=1000)
        await self.vad("stopped", audio_end_ms=1500)
        await self.final("What kind of bird is it?")
        self.assertEqual([e["text"] for e in self.transcripts()], ["What kind of bird is it?"])
        self.assertEqual([str(item[1]) for item in self.c.pending], ["What kind of bird is it?"])

    async def test_reset_does_not_carry_blocked_occurrence_into_first_new_turn(self):
        await self.playback()
        await self.vad("started", audio_start_ms=10)
        await self.vad("stopped", audio_end_ms=400)
        await self.c.reset()
        self.c.connected = True
        await self.vad("started", audio_start_ms=1000)
        await self.vad("stopped", audio_end_ms=1500)
        await self.final("How many species today?")
        self.assertEqual([e["text"] for e in self.transcripts()], ["How many species today?"])
        self.assertEqual([str(item[1]) for item in self.c.pending], ["How many species today?"])


if __name__ == "__main__":
    unittest.main()
