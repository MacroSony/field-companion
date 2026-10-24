"""Replay phone speaker feedback without live microphones or model calls."""
import time
import unittest
from unittest.mock import AsyncMock, patch

import server as s
from test_server import FakePhone


class SelfEchoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.c = s.Session("self-echo-test")
        self.c.connected = self.c.active = True
        self.c.phase = "answer"
        self.c.rt_send = AsyncMock()
        self.phone = FakePhone()
        self.client = s.Client(self.phone, self.c)
        self.c.clients.add(self.client)
        self.c.mic = self.client

    async def asyncTearDown(self):
        await self.c.close()

    async def transcript(self, text):
        await self.c.handle_rt({"type": "conversation.item.input_audio_transcription.completed",
                               "transcript": text, "item_id": "speech-input"})

    def cue(self, kind, text):
        self.c.play_until = time.monotonic() + 3
        self.client.playback_id = "playing-cue"
        self.client.playback_pending = True
        self.c.remember_spoken(("playing-cue", "feedback", kind), text)

    async def test_logged_cached_cue_echoes_never_flush_audio_or_start_a_turn(self):
        for kind, spoken, heard in (
                ("camera-taking", "Start taking photo.", "Taking photo."),
                ("received", "I heard you.", "I hurt you."),
                ("photo-ready", "Photo ready. Starting identification.", "Photo ready.")):
            self.cue(kind, spoken)
            with patch.object(self.c, "log") as log:
                await self.transcript(heard)
                log.assert_any_call("echo_dropped", text=heard)
            self.assertTrue(self.client.playback_pending)
            self.assertNotIn({"type": "flush"}, self.phone.events)
            self.assertFalse(self.c.pending)
            self.assertTrue(self.c.active)
            self.assertIn({"type": "conversation.item.delete", "item_id": "speech-input"},
                          [c.args[0] for c in self.c.rt_send.call_args_list])

    def test_short_cue_filter_requires_matching_recent_output_and_speaker_mode(self):
        self.assertFalse(self.c.transcript_is_echo("Photo ready."))
        self.cue("camera-taking", "Start taking photo.")
        self.assertFalse(self.c.transcript_is_echo("Photo ready."))
        self.assertTrue(self.c.transcript_is_echo("Taking photo."))
        self.client.earphones = True
        self.assertFalse(self.c.transcript_is_echo("Taking photo."))
        self.client.earphones = False
        self.client.playback_pending = False
        self.c.play_until = 0
        self.client.playback_end = time.monotonic() - 3
        self.c.spoken.clear()
        self.c.spoken.append((("playing-cue", "feedback", "photo-ready"),
                             "Photo ready. Starting identification.", time.monotonic() - 3))
        self.assertFalse(self.c.transcript_is_echo("Photo ready."))

    async def test_unprompted_yes_okay_and_fillers_are_ignored_before_barge_in(self):
        self.cue("received", "I heard you.")
        for text in ("Yes.", "Okay.", "好的。", "Um."):
            await self.transcript(text)
            self.assertTrue(self.client.playback_pending)
            self.assertNotIn({"type": "flush"}, self.phone.events)
            self.assertFalse(self.c.pending)

    async def test_yes_after_actual_explanation_offer_still_interrupts_and_routes(self):
        self.cue("photo-ready", "Photo ready. Starting identification.")
        self.c.last_assistant_text = "Investigation R1 is ready. Would you like me to explain?"
        self.c.investigation_offer_ids = ["R1"]
        await self.transcript("Yes.")
        self.assertIn({"type": "flush"}, self.phone.events)
        self.assertEqual(self.c.pending[-1][1], "Yes.")

    async def test_yes_after_logging_offer_remains_valid(self):
        self.c.last_assistant_text = "I'm hearing an American Robin, add it?"
        self.assertIsNone(self.c.ignored_input_reason("Yes."))
        self.assertIsNotNone(self.c.ignored_input_reason("Okay."))

    async def test_stop_and_meaningful_questions_keep_voice_interruption(self):
        self.cue("photo-ready", "Photo ready. Starting identification.")
        for text in ("Start.", "开始。", "Stop.", "Is the photo ready?", "I heard two birds."):
            self.assertFalse(self.c.transcript_is_echo(text), text)
            self.assertIsNone(self.c.ignored_input_reason(text), text)
        await self.transcript("What does it eat?")
        self.assertIn({"type": "flush"}, self.phone.events)
        self.assertEqual(self.c.pending[-1][1], "What does it eat?")
        self.c.pending.clear()
        self.phone.events.clear()
        self.cue("received", "I heard you.")
        await self.transcript("Stop.")
        self.assertIn({"type": "flush"}, self.phone.events)
        self.assertTrue(self.c.auto_speech_paused)
        self.assertFalse(self.c.pending)

    async def test_output_mute_is_diagnostic_and_never_pauses_input_or_playback(self):
        self.cue("photo-ready", "Photo ready. Starting identification.")
        with patch.object(self.c, "log") as log:
            await self.c.handle_phone({"type": "output_mute", "muted": True}, self.client)
            log.assert_called_with("output_mute", muted=True, role="mic")
        self.assertTrue(self.c.mic_on)
        self.assertTrue(self.client.playback_pending)
        self.assertNotIn({"type": "flush"}, self.phone.events)
        self.c.rt_send.assert_not_awaited()
        with self.assertRaises(ValueError):
            await self.c.handle_phone({"type": "output_mute", "muted": "true"}, self.client)


if __name__ == "__main__":
    unittest.main()
