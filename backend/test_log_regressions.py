"""Replay the phone-test failures without real keys, models or recordings."""
import asyncio
import io
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from PIL import Image

import server as s
from expert import NaturalistExpert, Settings
from route_safety import route_guard
from test_server import FakePhone, ASGIClient


class LoggedFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.c = s.Session("log-regression")
        self.c.expert = NaturalistExpert(Settings(enabled=False))
        self.phone = FakePhone()
        self.client = s.Client(self.phone, self.c)
        self.c.clients.add(self.client)
        self.c.mic = self.client
        self.c.allowed_species = lambda: set()
        self.c.rt_send = AsyncMock()
        self.c.play_feedback = AsyncMock(return_value=True)

    async def asyncTearDown(self):
        await self.c.close()

    async def route(self, text, name, args=None):
        self.c.turn_question = text
        await self.c.finish_route({"status": "completed", "output": [{"type": "function_call",
            "name": name, "call_id": "logged-call", "arguments": s.json.dumps(args or {})}]})

    async def focus(self):
        obs = await self.c.new_focus()
        obs.fused = [{"sci": "Haliaeetus leucocephalus", "en": "Bald Eagle", "p": .99}]
        obs.uncertain = False
        return obs

    async def test_logged_fragments_do_not_write_or_cancel_pending_photo(self):
        obs = await self.focus()
        await self.c.request_look()
        request = self.c.camera_request
        for text in ("Second.", "kõik.", "aktiveerimine", "What do you think?"):
            await self.route(text, "confirm_observation", {"count": 1, "notes": "Bald Eagle confirmed via visual ID"})
            self.assertEqual(obs.status, "open")
            self.assertIsNone(obs.count)
            self.assertEqual(obs.notes, "")
            self.assertIs(self.c.camera_request, request)
        self.assertFalse(self.c.items)
        self.assertTrue(any(p[0] == "feedback" for p in self.c.pending))

    async def test_explicit_add_still_confirms_but_pending_image_cannot_be_confirmed(self):
        obs = await self.focus()
        await self.route("Add it, two of them.", "confirm_observation", {"count": 2})
        self.assertEqual((obs.status, obs.count), ("confirmed", 2))
        obs = await self.focus()
        await self.c.request_look()
        request = self.c.camera_request
        await self.route("Add it.", "confirm_observation", {"count": 1})
        self.assertEqual(obs.status, "open")
        self.assertIs(self.c.camera_request, request)

    async def test_a_write_queued_for_an_older_camera_context_needs_clarification(self):
        obs = await self.focus()
        self.c.turn_context = self.c.input_context()
        await self.c.request_look()
        await self.route("Add it.", "confirm_observation", {"count": 1})
        self.assertEqual(obs.status, "open")
        self.assertIsNotNone(self.c.camera_request)

    async def test_reason_saying_ignore_cannot_start_a_spoken_model_answer(self):
        self.c.generate = AsyncMock()
        await self.route("aktiveerimine", "speak", {
            "reason": "User said 'aktiveerimine' which is unintelligible filler/acknowledgement in Estonian; ignore per routing rules"})
        self.c.generate.assert_not_awaited()
        self.assertFalse(self.c.pending)

    async def test_cancel_without_done_releases_generation_and_ignores_late_completion(self):
        started = asyncio.Event()
        self.c.connected = True
        self.c.update_session = AsyncMock()
        async def send(event):
            if event["type"] == "response.create":
                await self.c.handle_rt({"type": "response.created", "response": {"id": "old-answer"}})
                started.set()
        self.c.rt_send = send
        work = asyncio.create_task(self.c.generate("answer"))
        await asyncio.wait_for(started.wait(), 1)
        await self.c.cancel_speech()
        self.assertEqual((await asyncio.wait_for(work, .3))["status"], "cancelled")
        await self.c.handle_rt({"type": "error", "error": {"code": "response_not_active", "message": "No active response to cancel."}})
        self.assertTrue(self.c.cancel_barrier["future"].done())
        self.c.audio_playback_id = "new-camera-feedback"
        self.c.end_audio = AsyncMock()
        await self.c.handle_rt({"type": "response.done", "response": {"id": "old-answer", "status": "cancelled"}})
        self.c.end_audio.assert_not_awaited()

    async def test_cancel_during_session_setup_never_creates_the_old_response(self):
        setup = asyncio.Event()
        async def updating(config):
            setup.set()
            await asyncio.Event().wait()
        self.c.connected = True
        self.c.update_session = updating
        work = asyncio.create_task(self.c.generate("answer"))
        await asyncio.wait_for(setup.wait(), 1)
        await self.c.cancel_speech()
        self.assertEqual((await asyncio.wait_for(work, .3))["status"], "cancelled")
        self.assertFalse(any(c.args[0]["type"] == "response.create" for c in self.c.rt_send.call_args_list))

    async def test_unknown_cancel_recovers_transport_before_another_generation(self):
        old = SimpleNamespace(close=AsyncMock())
        new = object()
        async def close():
            self.c.rt = new
            self.c.connected = True
        old.close.side_effect = close
        self.c.rt = old
        done = asyncio.get_running_loop().create_future()
        done.set_result(None)
        self.c.cancel_barrier = {"future": done, "unknown": True, "rt": old, "response_id": None}
        await self.c.wait_cancel_boundary({"cancelled": False})
        old.close.assert_awaited_once()
        self.assertIs(self.c.rt, new)
        self.assertIsNone(self.c.cancel_barrier)

    async def test_start_captures_even_while_a_model_generation_is_waiting(self):
        await self.c.request_look()
        request = self.c.camera_request
        await self.c.camera_ready({"request_id": request["id"]})
        self.c.connected = self.c.active = True
        self.c.phase = "answer"
        self.c.response_waiter = asyncio.get_running_loop().create_future()
        await asyncio.wait_for(self.c.camera_confirmation("Start."), .3)
        self.assertTrue(any(e["type"] == "capture" for e in self.phone.events))
        self.assertEqual(request["phase"], "capturing")

    async def test_processing_query_returns_status_without_generating_a_species_answer(self):
        obs = await self.focus()
        obs.analysis_status = "analyzing"
        self.c.generate = AsyncMock()
        await self.route("What do you think?", "get_observation")
        self.c.generate.assert_not_awaited()
        queued = next(p[1] for p in self.c.pending if p[0] == "feedback")
        self.assertEqual(queued["kind"], "photo-processing")
        self.assertTrue(self.c.feedback_current(queued))
        obs.analysis_status = "ready"
        self.assertFalse(self.c.feedback_current(queued))

    async def test_identification_question_during_analysis_does_not_restart_camera(self):
        await self.focus()
        await self.c.request_look()
        request = self.c.camera_request
        request["phase"] = "classifying"
        self.c.observations[self.c.focus].analysis_status = "analyzing"
        await self.route("What is this?", "look")
        self.assertIs(self.c.camera_request, request)
        self.assertEqual(request["phase"], "classifying")
        self.assertTrue(any(p[0] == "feedback" for p in self.c.pending))
        await self.route("Look again, take another photo.", "look")
        self.assertIsNot(self.c.camera_request, request)

    async def test_ready_query_prevents_duplicate_automatic_readout(self):
        obs = await self.focus()
        obs.analysis_status, obs.image_revision, obs.analysis_request_id = "ready", 1, "photo-1"
        self.c.generate = AsyncMock(return_value={"status": "completed"})
        await self.route("What is it?", "get_observation")
        await self.c.answer_vision({"obs": obs.id, "image_revision": 1, "request_id": "photo-1"})
        self.c.generate.assert_awaited_once()

    async def test_old_photo_result_cannot_be_read_as_new_evidence(self):
        obs = await self.focus()
        obs.analysis_status, obs.image_revision, obs.analysis_request_id = "ready", 2, "new"
        self.c.generate = AsyncMock()
        await self.c.answer_vision({"obs": obs.id, "image_revision": 1, "request_id": "old"})
        self.c.generate.assert_not_awaited()

    async def test_new_start_during_result_broadcast_keeps_its_own_answer_flag(self):
        await self.c.request_look()
        old = self.c.camera_request
        await self.c.camera_ready({"request_id": old["id"]})
        await self.c.camera_confirmation("start")
        self.c.names["A a"] = {"sci": "A a", "en": "A"}
        original = self.c.broadcast_observation
        replacement = None
        async def broadcast(obs):
            nonlocal replacement
            if obs.analysis_status == "ready" and replacement is None:
                replacement = "opening"
                await self.c.request_look()
                replacement = self.c.camera_request
                await self.c.camera_ready({"request_id": replacement["id"]})
                await self.c.camera_confirmation("start")
            await original(obs)
        self.c.broadcast_observation = broadcast
        data = io.BytesIO()
        Image.new("RGB", (40, 40), "green").save(data, "JPEG")
        s.SESSIONS[self.c.id] = self.c
        try:
            async with ASGIClient() as http:
                with patch.object(s, "classify_frame", return_value={"candidates": [{"sci": "A a", "p": .9}]}):
                    result = await http.post("/api/frame", params={"session": self.c.id,
                        "obs": old["obs"], "capture": old["id"]}, content=data.getvalue())
            self.assertEqual(result.status_code, 200)
            self.assertIs(self.c.camera_request, replacement)
            self.assertTrue(self.c.observations[self.c.focus].awaiting_answer)
            self.assertEqual(replacement["phase"], "capturing")
        finally:
            s.SESSIONS.pop(self.c.id, None)


class EvidenceAndIntentTests(unittest.TestCase):
    def test_prior_cannot_turn_weak_raw_image_into_certain_identity(self):
        image = s.Detection("photo", "image", s.now_iso(), [
            {"sci": "Haliaeetus leucocephalus", "p": .431907},
            {"sci": "Aquila chrysaetos", "p": .04},
            {"sci": "Phalacrocorax carbo", "p": .069}])
        fused, uncertain = s.fuse([image], priors={"Haliaeetus leucocephalus": 100000})
        self.assertGreater(fused[0]["p"], .99)
        self.assertTrue(uncertain)

    def test_correlated_weak_frames_stay_uncertain_and_clear_latest_frame_can_resolve(self):
        image = s.Detection("p1", "image", s.now_iso(), [{"sci": "A a", "p": .43}, {"sci": "B b", "p": .1}])
        self.assertTrue(s.fuse([image, image, image])[1])
        clear = s.Detection("p2", "image", s.now_iso(), [{"sci": "A a", "p": .95}, {"sci": "B b", "p": .02}])
        self.assertFalse(s.fuse([image, clear])[1])

    def test_explicit_writes_and_read_questions_remain_supported(self):
        examples = [("confirm_observation", {}, "Add it, two of them."),
                    ("confirm_observation", {}, "two of them"),
                    ("confirm_observation", {}, "记录两只"),
                    ("add_observation", {"name": "mallards"}, "Two mallards."),
                    ("set_count", {"count": 4}, "Actually four."),
                    ("remove_observation", {"all": True}, "Clear everything."),
                    ("speak", {"reason": "Explain the word ignore"}, "What does ignore mean?")]
        for name, args, text in examples:
            self.assertEqual(route_guard(name, args, text)[0], "allow", text)
        for text in ("Second.", "kõik.", "Okay.", "What is it?", "Don't add it."):
            self.assertNotEqual(route_guard("confirm_observation", {}, text)[0], "allow", text)
        for text in ("What should I add?", "How do I record this?", "记录了什么？"):
            self.assertNotEqual(route_guard("confirm_observation", {}, text)[0], "allow", text)

    def test_bare_yes_needs_an_actual_offer_and_questions_cannot_invent_sightings(self):
        self.assertEqual(route_guard("accept_suggestions", {}, "yes", suggestions=True)[0], "ignore")
        self.assertEqual(route_guard("accept_suggestions", {}, "yes", suggestions=True, log_offer=True)[0], "allow")
        for text in ("What have I seen?", "Which mallard should I add?", "Can you hear me?"):
            self.assertEqual(route_guard("add_observation", {"name": "mallard"}, text)[0], "clarify")


if __name__ == "__main__":
    unittest.main()
