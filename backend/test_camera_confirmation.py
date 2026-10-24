"""Camera consent lifecycle with real session, STT and frame routes; no APIs."""
import asyncio
import io
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

import server as s
from expert import NaturalistExpert, Settings
from test_server import ASGIClient, FakePhone


class CameraConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.c = s.Session("camera-consent-test")
        self.c.expert = NaturalistExpert(Settings(enabled=False))
        self.c.rt_send = AsyncMock()
        self.c.allowed_species = lambda: set()
        self.phone = FakePhone()
        self.client = s.Client(self.phone, self.c)
        self.c.clients.add(self.client)
        self.c.mic = self.client

    async def asyncTearDown(self):
        s.SESSIONS.pop(self.c.id, None)
        await self.c.close()

    def captures(self):
        return [e for e in self.phone.events if e["type"] == "capture"]

    async def ready(self, **kwargs):
        await self.c.request_look(**kwargs)
        request = self.c.camera_request
        await self.c.camera_ready({"request_id": request["id"]})
        return request

    async def capture(self):
        request = await self.ready()
        await self.c.camera_confirmation("Start.")
        self.c.pending.clear()
        await self.c.start_camera_capture(request["id"])
        return request

    async def test_opening_only_prepares_preview_without_lookup_or_capture(self):
        self.c.allowed_species = lambda: self.fail("No range lookup before confirmation")
        result = await self.c.tool("look", {"hint": "Aim at the bill"})
        self.assertTrue(result["waiting_for_confirmation"])
        self.assertEqual(self.phone.events[-1]["type"], "camera_prepare")
        self.assertFalse(self.c.observations[self.c.focus].awaiting_answer)
        self.assertFalse(self.c.pending or self.captures())
        await self.c.camera_ready({"request_id": self.c.camera_request["id"]})
        self.assertEqual(self.c.pending[0][0], "camera_ready")
        self.assertFalse(self.captures())

    async def test_early_confirmation_is_not_saved_and_duplicate_start_captures_once(self):
        await self.c.request_look()
        request = self.c.camera_request
        self.assertTrue(await self.c.camera_confirmation("start"))
        self.assertFalse(request["confirmed"])
        await self.c.camera_ready({"request_id": request["id"]})
        self.assertEqual(request["phase"], "ready")
        await self.c.camera_confirmation("start")
        await self.c.camera_confirmation("start")
        self.assertFalse(any(p[0] == "camera_start" for p in self.c.pending))
        await self.c.start_camera_capture(request["id"])
        await self.c.start_camera_capture(request["id"])
        self.assertEqual(len(self.captures()), 1)

    async def test_exact_english_chinese_and_typed_confirmations_bypass_router(self):
        for text in ("Start.", "开始。", "开始拍照", "take a photo"):
            with self.subTest(text=text):
                request = await self.ready()
                await self.c.handle_rt({"type": "conversation.item.input_audio_transcription.completed",
                    "transcript": text, "item_id": "voice-start"})
                self.assertEqual(request["phase"], "capturing")
                self.assertFalse(any(p[0] == "text" for p in self.c.pending))
        request = await self.ready()
        await self.c.handle_phone({"type": "text", "text": "start"}, self.client)
        self.assertEqual(request["phase"], "capturing")
        self.assertFalse(any(p[0] == "text" for p in self.c.pending))

    async def test_early_and_repeated_start_report_camera_phase_without_generic_receipt(self):
        await self.c.request_look()
        request = self.c.camera_request
        self.c.play_feedback = AsyncMock(return_value=True)
        for phase, kind in (("opening", "camera-opening"), ("starting", "camera-taking"),
                            ("capturing", "camera-taking"), ("classifying", "photo-processing")):
            request["phase"] = phase
            await self.c.camera_confirmation("Start.")
            self.assertEqual(self.c.play_feedback.await_args.args, (kind,))
            self.assertTrue(self.c.play_feedback.await_args.kwargs["guard"]())
        self.assertFalse(request["confirmed"])
        self.assertFalse(self.captures())

    async def test_idle_start_and_unrelated_consent_are_not_camera_commands(self):
        self.assertFalse(await self.c.camera_confirmation("start"))
        request = await self.ready()
        for text in ("yes", "go ahead", "start a new investigation", "what does it eat?"):
            self.assertFalse(await self.c.camera_confirmation(text))
        self.assertEqual(request["phase"], "ready")
        await self.c.handle_phone({"type": "text", "text": "what does it eat?"}, self.client)
        self.assertTrue(any(p[0] == "text" for p in self.c.pending))
        self.assertIs(self.c.camera_request, request)

    async def test_manual_identify_is_confirmation_but_still_requires_ready_video(self):
        await self.c.handle_phone({"type": "look", "confirmed": True}, self.client)
        request = self.c.camera_request
        await self.c.start_camera_capture(request["id"])
        self.assertFalse(self.captures())
        await self.c.camera_ready({"request_id": request["id"]})
        self.assertEqual(request["phase"], "capturing")
        await self.c.start_camera_capture(request["id"])
        self.assertEqual(len(self.captures()), 1)

    async def test_close_cancel_focus_and_expiry_invalidate_pending_confirmation(self):
        for action in ("close", "cancel", "focus", "expire", "clear"):
            with self.subTest(action=action):
                request = await self.ready()
                if action == "close":
                    await self.c.handle_phone({"type": "camera_closed", "request_id": request["id"]}, self.client)
                elif action == "cancel":
                    await self.c.camera_confirmation("取消")
                elif action == "focus":
                    await self.c.new_focus()
                elif action == "expire":
                    self.c.observations[self.c.focus].touched -= s.FOCUS_TTL + 1
                    await self.c.expire_focus()
                else:
                    await self.c.clear_outing()
                self.assertIsNone(self.c.camera_request)
                await self.c.camera_ready({"request_id": request["id"]})
                self.assertFalse(await self.c.camera_confirmation("start"))
                await self.c.start_camera_capture(request["id"])
                self.assertFalse(self.captures())

    async def test_stale_ready_close_and_other_device_cannot_control_new_request(self):
        old = await self.ready()
        current = await self.ready()
        await self.c.camera_ready({"request_id": old["id"]})
        await self.c.handle_phone({"type": "camera_closed", "request_id": old["id"]}, self.client)
        self.assertIs(self.c.camera_request, current)
        with self.assertRaises(ValueError):
            await self.c.handle_phone({"type": "camera_closed", "request_id": current["id"]},
                                      s.Client(FakePhone(), self.c, role="display"))
        self.assertIs(self.c.camera_request, current)

    async def test_manual_open_uses_preview_id_and_close_rejects_late_ready(self):
        await self.c.handle_phone({"type": "camera_ready", "open_id": "preview-1"}, self.client)
        request = self.c.camera_request
        self.assertEqual(request["phase"], "opening")
        await self.c.handle_phone({"type": "camera_closed", "open_id": "preview-1"}, self.client)
        await self.c.camera_ready({"request_id": request["id"]})
        self.assertIsNone(self.c.camera_request)
        self.assertFalse(self.captures())

    async def test_ready_prompt_waits_for_conversation_and_only_runs_after_video_ready(self):
        self.c.connected = True
        self.c.user_speaking = True
        self.c.play_feedback = AsyncMock(return_value=True)
        self.c.prune_history = AsyncMock()
        self.c.turn_task = asyncio.create_task(self.c.turn_worker())
        self.c.tasks.append(self.c.turn_task)
        await self.c.request_look()
        request = self.c.camera_request
        await asyncio.sleep(.02)
        self.c.play_feedback.assert_not_awaited()
        await self.c.camera_ready({"request_id": request["id"]})
        await asyncio.sleep(.02)
        self.c.play_feedback.assert_not_awaited()
        self.c.user_speaking = False
        self.client.playback_pending = True
        await self.c.flush_pending()
        await asyncio.sleep(.02)
        self.c.play_feedback.assert_not_awaited()
        self.client.playback_pending = False
        await self.c.flush_pending()
        async with asyncio.timeout(1):
            while not self.c.play_feedback.await_count:
                await asyncio.sleep(.01)
        self.assertEqual(self.c.play_feedback.call_args.args, ("camera-ready",))
        self.assertFalse(self.captures())

    async def test_start_does_not_wait_for_range_lookup_or_sound_model_loading(self):
        request = await self.ready()
        await self.c.camera_confirmation("start")
        self.c.refusion = AsyncMock(side_effect=AssertionError("Slow lookup before capture"))
        self.c.allowed_species = lambda: self.fail("Cold model should not block camera")
        await asyncio.wait_for(self.c.start_camera_capture(request["id"]), 1)
        self.c.refusion.assert_not_awaited()
        self.assertEqual(len(self.captures()), 1)

    async def test_voice_and_identify_say_taking_photo_before_capture(self):
        self.c.connected = True
        for manual, language in ((False, "en"), (True, "en"), (False, "zh")):
            with self.subTest(manual=manual, language=language):
                self.c.lang = language
                await self.c.cancel_speech()
                self.phone.events.clear()
                request = await self.ready(confirmed=manual)
                if not manual:
                    await self.c.handle_rt({"type": "conversation.item.input_audio_transcription.completed",
                        "transcript": "Start.", "item_id": "start-1"})
                await self.c.start_camera_capture(request["id"])
                caption = "Start taking photo." if language == "en" else "开始拍照。"
                feedback = next(i for i, e in enumerate(self.phone.events) if e.get("text") == caption)
                capture = next(i for i, e in enumerate(self.phone.events) if e["type"] == "capture")
                self.assertLess(feedback, capture)
                self.assertTrue(self.phone.audio)

    async def test_identify_cancels_current_speech_before_acknowledging(self):
        self.c.connected = True
        self.c.play_until = s.time.monotonic() + 10
        self.c.cancel_speech = AsyncMock()
        await self.c.handle_phone({"type": "look", "confirmed": True}, self.client)
        self.c.cancel_speech.assert_awaited_once()

    async def test_photo_progress_precedes_result_and_waits_for_previous_playback(self):
        request = await self.capture()
        self.c.play_until = 0
        self.c.camera_feedback_request = request
        self.c.camera_request = None  # Fast classification already finished.
        self.c.pending = s.deque([("vision", request["obs"], None),
                                  ("camera_photo", request["id"], None)])
        self.c.connected = True
        self.c.prune_history = AsyncMock()
        self.c.answer_vision = AsyncMock()
        self.client.playback_protocol = 1
        self.client.playback_pending = True
        self.c.turn_task = asyncio.create_task(self.c.turn_worker())
        self.c.tasks.append(self.c.turn_task)
        await self.c.flush_pending()
        await asyncio.sleep(.02)
        self.assertFalse(any(e.get("text") == "Photo ready. Starting identification." for e in self.phone.events))
        self.c.answer_vision.assert_not_awaited()
        self.client.playback_pending = False
        await self.c.flush_pending()
        async with asyncio.timeout(1):
            while not any(e.get("text") == "Photo ready. Starting identification." for e in self.phone.events):
                await asyncio.sleep(.01)
        self.c.answer_vision.assert_not_awaited()
        # A matching drained report releases the queued result only after the
        # progress clip has actually ended, even with very fast classification.
        self.c.play_until = 0
        self.client.playback_pending = False
        await self.c.flush_pending()
        async with asyncio.timeout(1):
            while not self.c.answer_vision.await_count:
                await asyncio.sleep(.01)
        self.c.answer_vision.assert_awaited_once_with(request["obs"])
        self.assertIsNone(self.c.camera_feedback_request)

    async def test_new_request_cancels_old_photo_progress(self):
        request = await self.capture()
        self.c.camera_feedback_request = request
        self.c.pending.append(("camera_photo", request["id"], None))
        self.c.pending.append(("vision", request["obs"], None))
        await self.c.request_look()
        self.c.play_feedback = AsyncMock()
        await self.c.announce_camera_photo(request["id"])
        self.c.play_feedback.assert_not_awaited()
        self.assertFalse(any(p[0] == "camera_photo" for p in self.c.pending))
        self.assertFalse(any(p[0] == "vision" for p in self.c.pending))

    async def test_start_ack_is_immediate_while_worker_is_busy_and_is_not_repeated(self):
        request = await self.ready()
        self.c.connected = self.c.active = True
        await self.c.handle_rt({"type": "conversation.item.input_audio_transcription.completed",
            "transcript": "Start.", "item_id": "busy-start"})
        self.assertTrue(any(e.get("text") == "Start taking photo." for e in self.phone.events))
        self.assertTrue(request["start_acknowledged"])
        self.assertEqual(len(self.captures()), 1)  # Capture bypasses the busy worker.
        self.c.active = False
        await self.c.start_camera_capture(request["id"])
        self.assertEqual(len(self.captures()), 1)
        self.assertEqual(sum(e.get("text") == "Start taking photo." for e in self.phone.events), 1)

    async def test_frame_route_rejects_unconfirmed_old_and_consumed_tokens(self):
        s.SESSIONS[self.c.id] = self.c
        jpeg = io.BytesIO()
        Image.new("RGB", (80, 60), "green").save(jpeg, "JPEG")
        frame = jpeg.getvalue()
        request = await self.ready()
        self.c.names["Turdus migratorius"] = {"sci": "Turdus migratorius", "en": "American Robin"}
        result = {"candidates": [{"sci": "Turdus migratorius", "p": .91}]}
        async with ASGIClient() as http:
            with patch.object(s, "classify_frame", return_value=result) as classify:
                async def post(token):
                    return await http.post("/api/frame", params={"session": self.c.id,
                        "obs": self.c.focus, "capture": token}, content=frame)
                for token in ("", request["id"]):
                    self.assertEqual((await post(token)).status_code, 409)
                classify.assert_not_called()
                request = await self.capture()
                self.assertEqual((await post("old-token")).status_code, 409)
                self.assertEqual((await post(request["id"])).status_code, 200)
                self.assertEqual((await post(request["id"])).status_code, 409)
                classify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
