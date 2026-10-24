"""Offline regressions for camera/research status and unread photo protection."""
import asyncio
from copy import deepcopy
import io
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

import server as s
from expert import NaturalistExpert, Settings
from investigations import Investigation
from test_server import ASGIClient, FakePhone


class TaskStatusTests(unittest.IsolatedAsyncioTestCase):
    NEW_SUBJECT = "I found another animal. Could you help me check it out?"

    async def asyncSetUp(self):
        self.c = s.Session("task-status-test")
        self.c.expert = NaturalistExpert(Settings(enabled=False))
        self.phone = FakePhone()
        self.client = s.Client(self.phone, self.c)
        self.client.playback_protocol = 1
        self.c.clients.add(self.client)
        self.c.mic = self.client
        self.c.connected = True
        self.c.rt_send = AsyncMock()
        self.c.allowed_species = lambda: set()
        self.c.play_feedback = AsyncMock(return_value=True)
        self.c.speak_expert = AsyncMock(return_value={"status": "completed"})
        self.c.generate = AsyncMock(return_value={"status": "completed", "output": []})

    async def asyncTearDown(self):
        s.SESSIONS.pop(self.c.id, None)
        await self.c.close()

    async def photo(self, status="ready"):
        obs = await self.c.new_focus()
        obs.analysis_status = status
        obs.analysis_request_id = "original-photo"
        obs.image_revision = 1
        obs.fused = [{"sci": "Haliaeetus leucocephalus", "en": "Bald Eagle", "p": .99}]
        obs.detections = [s.Detection("image-1", "image", s.now_iso(), [
            {"sci": "Haliaeetus leucocephalus", "en": "Bald Eagle", "p": .43}])]
        obs.uncertain = True
        self.phone.events.clear()
        return obs

    def research(self, task_id, *, status="running", stage="retrieving"):
        job = Investigation(task_id, "What does the original bird eat?", {
            "id": "research-focus", "fused": [{"en": "Blue Jay", "sci": "Cyanocitta cristata"}],
            "detections": []}, status=status, stage=stage)
        if status == "completed":
            job.result = {"ok": True, "answer": "SECRET-RESEARCH-FINDINGS: the bird eats seeds."}
        self.c.investigations.jobs[task_id] = job
        return job

    async def route(self, text, name, args=None):
        self.c.turn_question = text
        await self.c.finish_route({"status": "completed", "output": [{
            "type": "function_call", "name": name, "call_id": "status-route",
            "arguments": json.dumps(args or {})}]})

    def assert_no_camera_commands(self):
        self.assertFalse(any(e.get("type") in ("camera_prepare", "camera_cancelled", "capture")
                             for e in self.phone.events))

    def assert_no_findings(self, payload):
        text = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("SECRET-RESEARCH-FINDINGS", text)
        self.assertNotIn("the bird eats seeds", text)

    def assert_fresh_subject(self, old):
        current = self.c.observations[self.c.focus]
        self.assertNotEqual(current.id, old.id)
        self.assertEqual(current.fused, [])
        self.assertEqual(current.detections, [])
        self.assertNotEqual(current.hint, old.hint)
        self.assertNotIn(old.hint, current.hint)
        self.assertEqual(current.image_revision, 0)
        self.assertIsNone(current.count)
        self.assertEqual(current.analysis_status, "awaiting_confirmation")
        self.assertEqual(current.analysis_stage, "camera_opening")
        self.assertEqual(self.c.camera_request["obs"], current.id)
        self.assertTrue(any(e.get("type") == "camera_prepare" and e.get("obs") == current.id
                            for e in self.phone.events))
        self.assertFalse(any(e.get("type") == "capture" for e in self.phone.events))
        return current

    async def test_invalid_model_status_arguments_clarify_without_starting_work(self):
        for arguments in ('{"scope":"invalid"}', '{invalid-json'):
            await self.c.finish_route({"status": "completed", "output": [{
                "type": "function_call", "name": "get_task_status", "call_id": "bad-status",
                "arguments": arguments}]})
        self.c.speak_expert.assert_not_awaited()
        self.assertFalse(self.c.investigations.jobs)
        self.assert_no_camera_commands()
        self.assertTrue(any(p[0] == "feedback" and p[1]["kind"] == "clarify" for p in self.c.pending))

    async def prepare_upload(self):
        await self.c.request_look()
        request = self.c.camera_request
        await self.c.camera_ready({"request_id": request["id"]})
        await self.c.camera_confirmation("Start.")
        obs = self.c.observations[request["obs"]]
        self.c.names["Haliaeetus leucocephalus"] = {
            "sci": "Haliaeetus leucocephalus", "en": "Bald Eagle"}
        data = io.BytesIO()
        Image.new("RGB", (50, 50), "green").save(data, "JPEG")
        s.SESSIONS[self.c.id] = self.c
        self.phone.events.clear()
        return obs, request, data.getvalue()

    async def upload(self, request, jpeg):
        async with ASGIClient() as http:
            return await http.post("/api/frame", params={"session": self.c.id,
                "obs": request["obs"], "capture": request["id"]}, content=jpeg)

    async def test_no_tasks_returns_an_empty_truthful_status(self):
        result = await self.c.task_status({})
        self.assertTrue(result["ok"])
        self.assertEqual(result["tasks"], [])
        self.assertTrue(result["answer"])
        self.c.generate.assert_not_awaited()

    async def test_snapshot_contains_running_and_queued_work_without_findings(self):
        running = self.research("R1", stage="retrieving")
        queued = self.research("R2", status="queued", stage="queued")
        finished = self.research("R3", status="completed", stage="completed")
        rows = self.c.task_status_snapshot()
        indexed = {r["id"]: r for r in rows}
        self.assertEqual(indexed["R1"]["stage"], "retrieving")
        self.assertEqual(indexed["R2"]["status"], "queued")
        self.assertEqual(indexed["R3"]["status"], "completed")
        self.assert_no_findings(rows)
        self.assertFalse(running.announced or queued.announced or finished.announced)
        self.assertTrue(all("result" not in r or r["result"] is None for r in rows))

    async def test_camera_snapshot_reports_each_real_processing_stage(self):
        obs = await self.photo("analyzing")
        for stage in ("range_lookup", "recognizing", "resolving_species", "fusing"):
            obs.analysis_stage = stage
            result = await self.c.task_status({"scope": "camera"})
            self.assertEqual(len(result["tasks"]), 1)
            row = result["tasks"][0]
            self.assertEqual((row["kind"], row["stage"]), ("camera", stage))
            self.assertEqual(row["id"], "photo:original-photo")
            self.assertEqual(row["request_id"], "original-photo")
            self.assertEqual(row["image_revision"], 1)
            self.assertNotIn("Bald Eagle", result["answer"])
        self.assert_no_camera_commands()

    async def test_explicit_research_selectors_do_not_read_results_or_announce(self):
        first = self.research("R1", status="completed", stage="completed")
        self.research("R2", stage="analyzing")
        self.c.investigation_offer_ids = ["R1"]
        for args, expected in (({"id": "R1"}, "R1"),
                               ({"scope": "investigations", "selection": "first"}, "R1"),
                               ({"id": "R2"}, "R2")):
            result = await self.c.task_status(args)
            self.assertEqual([r["id"] for r in result["tasks"]], [expected])
            self.assert_no_findings(result)
        self.assertFalse(first.announced)
        self.assertEqual(self.c.investigation_offer_ids, ["R1"])
        self.c.speak_expert.assert_not_awaited()

    async def test_first_task_selection_keeps_completed_first_before_running_second(self):
        self.research("R1", status="completed", stage="completed")
        self.research("R2", stage="analyzing")
        result = await self.c.task_status({"selection": "first"})
        self.assertEqual([r["id"] for r in result["tasks"]], ["R1"])
        self.assert_no_findings(result)

    async def test_ambiguous_camera_and_research_status_requests_selection(self):
        await self.photo("analyzing")
        self.research("R1")
        result = await self.c.task_status({})
        self.assertTrue(result.get("requires_selection"))
        self.assertGreaterEqual(len(result["tasks"]), 2)
        self.assert_no_findings(result)
        self.assert_no_camera_commands()

    async def test_ambiguity_accepts_short_task_selection_only_after_its_own_question(self):
        await self.photo("analyzing")
        self.research("R1")
        result = await self.c.speak_task_status({})
        self.c.last_assistant_text = result["answer"]  # Actual final output transcript.
        self.assertEqual(self.c.status_selector("R1"), {"id": "R1"})
        self.assertEqual(self.c.status_selector("Photo."), {"scope": "camera"})
        self.assertEqual(self.c.status_selector("照片"), {"scope": "camera"})
        self.c.last_assistant_text = "Investigation R1 is ready. Would you like me to explain?"
        self.assertIsNone(self.c.status_selector("Yes."))
        self.assertIsNone(self.c.status_selector("R1"))
        self.assert_no_camera_commands()

    async def test_explicit_all_scope_can_summarize_camera_and_research_together(self):
        await self.photo()
        self.research("R1")
        self.research("R2", status="queued", stage="queued")
        result = await self.c.task_status({"scope": "all"})
        self.assertFalse(result.get("requires_selection", False))
        self.assertEqual({r["kind"] for r in result["tasks"]}, {"camera", "investigation"})
        self.assertEqual({r["id"] for r in result["tasks"]}, {"photo:original-photo", "R1", "R2"})
        self.assert_no_findings(result)

    async def test_photo_and_its_own_visual_research_are_one_related_status(self):
        obs = await self.photo()
        self.c.expert = type("AvailableExpert", (), {"available": True})()
        job = self.research("R1")
        job.origin = "vision"
        job.observation = obs.payload()
        result = await self.c.task_status({})
        self.assertFalse(result.get("requires_selection", False))
        camera = next(r for r in self.c.task_status_snapshot() if r["kind"] == "camera")
        self.assertEqual(camera["related_investigation_ids"], ["R1"])
        self.assertEqual(camera["stage"], "expert_retrieving")
        self.assertFalse(job.announced)
        self.assert_no_findings(result)

    async def test_ready_uncertain_photo_reports_waiting_for_expert_before_dispatch(self):
        obs = await self.photo()
        self.c.expert = type("AvailableExpert", (), {"available": True})()
        pending = ("vision", {"obs": obs.id, "image_revision": 1,
                             "request_id": obs.analysis_request_id}, None)
        self.c.pending.append(pending)
        result = await self.c.task_status({"scope": "camera"})
        self.assertEqual(result["tasks"][0]["stage"], "awaiting_expert")
        self.assertIn(result["tasks"][0]["status"], ("queued", "running"))
        self.assertTrue(result["tasks"][0]["pending_result"])
        self.assertIn(pending, self.c.pending)
        self.assertFalse(self.c.investigations.jobs)
        self.c.generate.assert_not_awaited()

    async def test_logged_ready_status_phrase_cannot_restart_camera_or_erase_unread_work(self):
        obs = await self.photo()
        vision = ("vision", {"obs": obs.id, "image_revision": 1,
                            "request_id": obs.analysis_request_id}, None)
        self.c.pending.append(vision)
        for wrong_tool in ("look", "get_observation", "get_investigations", "speak", "ignore"):
            await self.route("Check out the identification process.", wrong_tool)
            self.assertEqual(obs.analysis_status, "ready")
            self.assertEqual(obs.analysis_request_id, "original-photo")
            self.assertEqual(obs.image_revision, 1)
            self.assertIsNone(self.c.camera_request)
            self.assertIn(vision, self.c.pending)
            self.assertNotIn((obs.id, 1), self.c.vision_answered)
            self.assert_no_camera_commands()
        self.c.generate.assert_not_awaited()
        self.assertEqual(self.c.speak_expert.await_count, 5)

    async def test_progress_query_during_photo_inference_preserves_capture_request(self):
        obs = await self.photo("analyzing")
        request = self.c.camera_request = {"id": "original-photo", "obs": obs.id,
            "phase": "classifying", "confirmed": True, "language": "en"}
        await self.route("Check out the identification process.", "look")
        self.assertIs(self.c.camera_request, request)
        self.assertEqual(request["phase"], "classifying")
        self.assertEqual(obs.analysis_status, "analyzing")
        self.assert_no_camera_commands()
        self.c.generate.assert_not_awaited()

    async def test_clear_status_request_bypasses_model_routing_and_preserves_ready_work(self):
        obs = await self.photo()
        vision = ("vision", {"obs": obs.id, "image_revision": 1,
                            "request_id": obs.analysis_request_id}, None)
        self.c.pending.append(vision)
        delivered = asyncio.Event()
        pending_at_delivery = []
        async def speak(*args, **kwargs):
            pending_at_delivery.extend(self.c.pending)
            delivered.set()
            return {"status": "completed"}
        self.c.speak_expert.side_effect = speak
        self.c.answer_vision = AsyncMock()
        self.c.tasks.append(asyncio.create_task(self.c.turn_worker()))
        await self.c.handle_phone({"type": "text", "text": "Check out the identification process."}, self.client)
        await asyncio.wait_for(delivered.wait(), 1)
        self.c.generate.assert_not_awaited()
        self.assertIn(vision, pending_at_delivery)
        self.assertEqual(obs.analysis_status, "ready")
        self.assertEqual(obs.analysis_request_id, "original-photo")
        self.assert_no_camera_commands()

    async def test_changed_stage_during_status_delivery_queues_fresh_status(self):
        obs = await self.photo("analyzing")
        obs.analysis_stage = "recognizing"
        guards = []
        async def change_before_speaking(answer, *, guard=None):
            self.assertIsNotNone(guard)
            obs.analysis_stage = "resolving_species" if not guards else "fusing"
            guards.append(guard())
            return {"status": "deferred"}
        self.c.speak_expert.side_effect = change_before_speaking
        await self.route("What is the identification status?", "look")
        self.assertEqual(guards, [False, False])
        self.assertTrue(any(p[0] == "task_status" for p in self.c.pending))
        latest = await self.c.task_status({"scope": "camera"})
        self.assertEqual(latest["tasks"][0]["stage"], "fusing")
        self.assert_no_camera_commands()

    async def test_elapsed_clock_alone_does_not_invalidate_an_unchanged_stage(self):
        obs = await self.photo("analyzing")
        obs.analysis_stage = "recognizing"
        obs.analysis_started = 100
        now = [101.0]
        guards = []
        async def setup_delay(answer, *, guard=None):
            self.assertIsNotNone(guard)
            now[0] += 1.0  # Model setup took time, but actual work stage is unchanged.
            guards.append(guard())
            return {"status": "completed" if guards[-1] else "deferred"}
        self.c.speak_expert.side_effect = setup_delay
        with patch.object(s, "time", SimpleNamespace(monotonic=lambda: now[0])):
            await self.route("What is the identification status?", "look")
        self.assertEqual(guards, [True])
        self.assertFalse(any(p[0] == "task_status" for p in self.c.pending))

    async def test_ready_unread_identification_reads_existing_photo_until_explicit_retake(self):
        obs = await self.photo()
        await self.route("What is that?", "look")
        self.assertEqual(obs.analysis_status, "ready")
        self.assertEqual(obs.analysis_request_id, "original-photo")
        self.assertIsNone(self.c.camera_request)
        self.assert_no_camera_commands()
        await self.route("Look again, take another photo.", "look")
        self.assertIsNotNone(self.c.camera_request)
        self.assertNotEqual(self.c.camera_request["id"], "original-photo")
        self.assertEqual(obs.analysis_status, "awaiting_confirmation")
        self.assertTrue(any(e.get("type") == "camera_prepare" for e in self.phone.events))

    async def test_mistaken_new_observation_cannot_discard_ready_unread_focus(self):
        obs = await self.photo()
        self.c.turn_question = "What is that?"
        result = await self.c.tool("new_observation", {"group": "Aves"})
        self.assertEqual(self.c.focus, obs.id)
        self.assertEqual(obs.status, "open")
        self.assertEqual(obs.analysis_status, "ready")
        self.assertEqual(obs.analysis_request_id, "original-photo")
        self.assertIsNone(self.c.camera_request)
        self.assert_no_camera_commands()
        self.assertFalse(result["ok"])
        self.assertTrue(result["suppress_answer"])
        self.assertTrue(result["requires_clarification"])
        self.assertTrue(any(p[0] == "feedback" and p[1]["kind"] == "clarify" for p in self.c.pending))
        self.c.generate.assert_not_awaited()

    async def test_unproven_new_observation_route_clarifies_without_reading_old_species(self):
        obs = await self.photo()
        await self.route("What is that?", "new_observation")
        self.assertEqual(self.c.focus, obs.id)
        self.assertEqual(obs.analysis_status, "ready")
        self.assertEqual(obs.image_revision, 1)
        self.assertIsNone(self.c.camera_request)
        self.assert_no_camera_commands()
        self.assertTrue(any(p[0] == "feedback" and p[1]["kind"] == "clarify" for p in self.c.pending))
        self.c.generate.assert_not_awaited()
        self.c.speak_expert.assert_not_awaited()

    async def test_start_without_a_pending_camera_clarifies_instead_of_reading_old_identity(self):
        obs = await self.photo()
        for wrong_tool in ("get_observation", "speak", "new_observation"):
            with self.subTest(wrong_tool=wrong_tool):
                before = len([p for p in self.c.pending if p[0] == "feedback"
                              and p[1]["kind"] == "clarify"])
                await self.route("Start.", wrong_tool, {"reason": "Start requested"} if wrong_tool == "speak" else {})
                self.assertEqual(self.c.focus, obs.id)
                self.assertEqual(obs.analysis_status, "ready")
                self.assertIsNone(self.c.camera_request)
                self.assert_no_camera_commands()
                self.assertFalse(self.c.items)
                after = len([p for p in self.c.pending if p[0] == "feedback"
                             and p[1]["kind"] == "clarify"])
                self.assertGreater(after, before)
        self.c.generate.assert_not_awaited()
        self.c.speak_expert.assert_not_awaited()

    async def test_status_wording_cannot_explain_a_completed_result_even_if_router_requests_it(self):
        job = self.research("R1", status="completed", stage="completed")
        self.c.investigation_offer_ids = ["R1"]
        await self.route("What is the status of R1?", "get_investigations", {"id": "R1", "explain": True})
        self.assertFalse(job.announced)
        self.assertEqual(self.c.investigation_offer_ids, ["R1"])
        self.assert_no_findings(self.c.speak_expert.await_args.args[0])
        self.assertIn("R1", self.c.speak_expert.await_args.args[0])
        self.c.generate.assert_not_awaited()

    async def test_result_consent_keeps_the_existing_explanation_path(self):
        job = self.research("R1", status="completed", stage="completed")
        self.c.last_assistant_text = "Investigation R1 is ready. Would you like me to explain?"
        self.c.investigation_offer_ids = ["R1"]
        await self.route("Yes.", "get_investigations", {"explain": True})
        self.assertIn("SECRET-RESEARCH-FINDINGS", self.c.speak_expert.await_args.args[0])
        self.assertTrue(job.announced)
        self.assertEqual(self.c.investigation_offer_ids, [])

    async def test_logged_new_subject_overrides_old_photo_routes_without_inheriting_evidence(self):
        for wrong_tool in ("look", "get_observation", "speak"):
            with self.subTest(wrong_tool=wrong_tool):
                old = await self.photo()
                old.hint = "Old eagle's bill on the original branch"
                old_vision = ("vision", {"obs": old.id, "image_revision": old.image_revision,
                                         "request_id": old.analysis_request_id}, None)
                self.c.pending.append(old_vision)
                args = {"hint": old.hint} if wrong_tool == "look" else (
                    {"reason": "Help identify the animal"} if wrong_tool == "speak" else {})
                await self.route(self.NEW_SUBJECT, wrong_tool, args)
                fresh = self.assert_fresh_subject(old)
                self.assertNotIn(old_vision, self.c.pending)
                self.assertEqual(old.status, "discarded")
                self.assertEqual(old.fused[0]["sci"], "Haliaeetus leucocephalus")
                self.assertEqual(len(old.detections), 1)
                request = self.c.camera_request
                await self.c.camera_ready({"request_id": request["id"]})
                self.assertFalse(any(e.get("type") == "capture" for e in self.phone.events))
                await self.c.camera_confirmation("Start.")
                self.assertEqual(request["phase"], "capturing")
                self.assertEqual(len([e for e in self.phone.events if e.get("type") == "capture"]), 1)
                self.assertEqual(fresh.fused, [])

    async def test_bare_generic_new_animal_notice_cannot_reuse_the_previous_photo(self):
        for chosen_tool in ("new_observation", "look", "get_observation"):
            with self.subTest(chosen_tool=chosen_tool):
                old = await self.photo()
                old.hint = "Old chicken on the previous screen"
                await self.route("I saw another animal.", chosen_tool)
                self.assert_fresh_subject(old)
                self.assertEqual(old.status, "discarded")
                self.assertEqual(old.image_revision, 1)
        self.c.generate.assert_not_awaited()
        self.c.speak_expert.assert_not_awaited()

    async def test_split_new_animal_notice_then_photo_confirmation_captures_the_fresh_subject(self):
        for confirmation in ("Take the photo for me.", "Take photo for me.", "Start."):
            with self.subTest(confirmation=confirmation):
                old = await self.photo()
                old.hint = "Previously identified animal"
                await self.route("I saw another animal.", "new_observation")
                fresh = self.assert_fresh_subject(old)
                request = self.c.camera_request
                await self.c.camera_ready({"request_id": request["id"]})
                self.assertEqual(request["phase"], "ready")
                self.assertFalse(any(e.get("type") == "capture" for e in self.phone.events))
                await self.c.handle_phone({"type": "text", "text": confirmation}, self.client)
                captures = [e for e in self.phone.events if e.get("type") == "capture"]
                self.assertEqual(len(captures), 1)
                self.assertEqual((captures[0]["obs"], captures[0]["request_id"]),
                                 (fresh.id, request["id"]))
                self.assertEqual(request["phase"], "capturing")
                self.assertFalse(any(p[0] == "text" and str(p[1]) == confirmation for p in self.c.pending))
        self.c.generate.assert_not_awaited()

    async def test_polite_photo_confirmation_does_not_latch_before_preview_is_ready(self):
        old = await self.photo()
        old.hint = "Previous subject"
        await self.route("I saw another animal.", "new_observation")
        request = self.c.camera_request
        self.assertIsNotNone(request)
        await self.c.camera_confirmation("Take the photo for me.")
        self.assertFalse(request["confirmed"])
        self.assertFalse(any(e.get("type") == "capture" for e in self.phone.events))
        await self.c.camera_ready({"request_id": request["id"]})
        self.assertEqual(request["phase"], "ready")
        self.assertFalse(request["confirmed"])
        self.assertFalse(any(e.get("type") == "capture" for e in self.phone.events))
        await self.c.camera_confirmation("Take the photo for me.")
        self.assertEqual(request["phase"], "capturing")
        self.assertEqual(len([e for e in self.phone.events if e.get("type") == "capture"]), 1)

    async def test_direct_photo_request_on_ready_result_prepares_retake_without_old_identity_answer(self):
        for text in ("Take a photo for me.", "Take the photo for me.", "Take photo for me."):
            for wrong_tool in ("look", "get_observation", "speak"):
                with self.subTest(text=text, wrong_tool=wrong_tool):
                    old = await self.photo()
                    await self.route(text, wrong_tool, {"reason": "Photo requested"} if wrong_tool == "speak" else {})
                    self.assertEqual(self.c.focus, old.id)
                    self.assertIsNotNone(self.c.camera_request)
                    self.assertNotEqual(self.c.camera_request["id"], "original-photo")
                    self.assertEqual(self.c.camera_request["phase"], "opening")
                    self.assertEqual(old.analysis_status, "awaiting_confirmation")
                    self.assertTrue(any(e.get("type") == "camera_prepare" for e in self.phone.events))
                    self.assertFalse(any(e.get("type") == "capture" for e in self.phone.events))
        self.c.generate.assert_not_awaited()
        self.c.speak_expert.assert_not_awaited()

    async def test_new_subject_preserves_a_previously_confirmed_checklist_observation(self):
        old = await self.photo()
        old.hint = "The original eagle's tail"
        old.status, old.count = "confirmed", 2
        before = deepcopy(old.payload())
        await self.route(self.NEW_SUBJECT, "look")
        self.assert_fresh_subject(old)
        self.assertEqual(old.payload(), before)
        self.assertEqual(old.status, "confirmed")
        self.assertEqual([(r["sci"], r["count"]) for r in self.c.items],
                         [("Haliaeetus leucocephalus", 2)])

    async def test_new_subject_keeps_running_research_bound_to_its_original_snapshot(self):
        old = await self.photo()
        old.hint = "Original animal behind the tree"
        job = self.research("R1", stage="analyzing")
        job.observation = deepcopy(old.payload())
        snapshot = deepcopy(job.observation)
        await self.route(self.NEW_SUBJECT, "get_observation")
        self.assert_fresh_subject(old)
        self.assertIs(self.c.investigations.jobs["R1"], job)
        self.assertEqual((job.status, job.stage), ("running", "analyzing"))
        self.assertEqual(job.observation, snapshot)
        self.assertEqual(job.observation["id"], old.id)
        self.assertFalse(job.announced)

    async def test_another_named_sighting_logs_without_opening_a_camera(self):
        await self.photo()
        taxon = {"sci": "Haliaeetus leucocephalus", "en": "Bald Eagle", "group": "Aves"}
        with patch.object(s, "resolve", return_value=taxon):
            await self.route("I saw another bald eagle.", "add_observation", {
                "name": "Bald Eagle", "group": "Aves", "count": 1, "notes": ""})
        self.assertIsNone(self.c.camera_request)
        self.assert_no_camera_commands()
        self.assertEqual([(r["sci"], r["count"]) for r in self.c.items],
                         [("Haliaeetus leucocephalus", 1)])

    async def test_new_subject_invalidates_old_inflight_photo_and_preserves_the_new_stage(self):
        old_obs, old_request, jpeg = await self.prepare_upload()
        old_obs.hint = "Old subject's wings"
        started, release = threading.Event(), threading.Event()
        def classify(*args):
            started.set()
            if not release.wait(3):
                raise RuntimeError("Offline fixture was not released")
            return {"candidates": [{"sci": "Haliaeetus leucocephalus", "p": .99}]}
        with patch.object(s, "classify_frame", side_effect=classify) as classifier:
            work = asyncio.create_task(self.upload(old_request, jpeg))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                await self.route(self.NEW_SUBJECT, "look")
                fresh = self.assert_fresh_subject(old_obs)
                newer = self.c.camera_request
                rejected = await self.upload(old_request, jpeg)
                self.assertEqual(rejected.status_code, 409)
                self.assertEqual(classifier.call_count, 1)
            finally:
                release.set()
                result = await asyncio.wait_for(work, 2)
        self.assertEqual(result.status_code, 409)
        self.assertIs(self.c.camera_request, newer)
        self.assertEqual((newer["phase"], fresh.analysis_status, fresh.analysis_stage),
                         ("opening", "awaiting_confirmation", "camera_opening"))
        self.assertEqual(fresh.analysis_request_id, newer["id"])
        self.assertEqual(fresh.fused, [])
        self.assertEqual(fresh.detections, [])
        self.assertEqual(old_obs.image_revision, 0)
        self.assertFalse(any(p[0] == "vision" for p in self.c.pending))

    async def test_real_upload_progress_does_not_cancel_classification_or_lose_its_result(self):
        obs, request, jpeg = await self.prepare_upload()
        started, release = threading.Event(), threading.Event()
        def classify(*args):
            started.set()
            if not release.wait(3):
                raise RuntimeError("Offline fixture was not released")
            return {"candidates": [{"sci": "Haliaeetus leucocephalus", "p": .99}]}
        with patch.object(s, "classify_frame", side_effect=classify):
            work = asyncio.create_task(self.upload(request, jpeg))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                self.assertEqual(obs.analysis_stage, "recognizing")
                self.assertEqual(request["phase"], "classifying")
                await self.route("Check out the identification process.", "look")
                self.assertIs(self.c.camera_request, request)
                status = await self.c.task_status({"scope": "camera"})
                self.assertEqual(status["tasks"][0]["stage"], "recognizing")
                self.assertFalse(any(e.get("type") in ("camera_prepare", "camera_cancelled")
                                     for e in self.phone.events))
            finally:
                release.set()
                result = await asyncio.wait_for(work, 2)
        self.assertEqual(result.status_code, 200)
        self.assertIsNone(self.c.camera_request)
        self.assertEqual((obs.analysis_status, obs.analysis_stage), ("ready", "ready"))
        self.assertEqual(obs.image_revision, 1)
        self.assertEqual(obs.fused[0]["sci"], "Haliaeetus leucocephalus")
        vision = next(p[1] for p in self.c.pending if p[0] == "vision")
        self.assertEqual((vision["obs"], vision["request_id"], vision["image_revision"]),
                         (obs.id, request["id"], 1))

    async def test_classification_http_failure_has_failed_status_instead_of_running_forever(self):
        obs, request, jpeg = await self.prepare_upload()
        with patch.object(s, "classify_frame", side_effect=s.HTTPException(503, "Offline service unavailable")):
            result = await self.upload(request, jpeg)
        self.assertEqual(result.status_code, 503)
        self.assertEqual((obs.analysis_status, obs.analysis_stage), ("failed", "failed"))
        self.assertFalse(obs.awaiting_answer)
        status = await self.c.task_status({"scope": "camera"})
        self.assertEqual((status["tasks"][0]["status"], status["tasks"][0]["stage"]),
                         ("failed", "failed"))
        self.assertFalse(any(p[0] == "vision" for p in self.c.pending))

    async def test_late_classification_cannot_overwrite_an_explicit_new_capture_request(self):
        obs, old, jpeg = await self.prepare_upload()
        started, release = threading.Event(), threading.Event()
        def classify(*args):
            started.set()
            if not release.wait(3):
                raise RuntimeError("Offline fixture was not released")
            return {"candidates": [{"sci": "Haliaeetus leucocephalus", "p": .99}]}
        with patch.object(s, "classify_frame", side_effect=classify):
            work = asyncio.create_task(self.upload(old, jpeg))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                self.c.turn_question = "Look again, take another photo."
                await self.c.tool("look", {})
                newer = self.c.camera_request
                self.assertIsNot(newer, old)
                self.assertEqual(obs.analysis_request_id, newer["id"])
                self.assertEqual(obs.analysis_stage, "camera_opening")
            finally:
                release.set()
                result = await asyncio.wait_for(work, 2)
        self.assertEqual(result.status_code, 409)
        self.assertIs(self.c.camera_request, newer)
        self.assertEqual((newer["phase"], obs.analysis_status, obs.analysis_stage),
                         ("opening", "awaiting_confirmation", "camera_opening"))
        self.assertEqual(obs.analysis_request_id, newer["id"])
        self.assertEqual(obs.image_revision, 0)
        self.assertFalse(any(p[0] == "vision" for p in self.c.pending))


if __name__ == "__main__":
    unittest.main()

