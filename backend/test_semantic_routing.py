"""Offline action/state regressions for model-selected camera and status tools."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import numpy as np

import server as s
from expert import NaturalistExpert, Settings
from investigations import Investigation
from test_server import FakePhone


class SemanticRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.c = s.Session("semantic-routing-test")
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
        self.c.generate = AsyncMock(return_value={"status": "completed", "output": []})
        self.c.speak_expert = AsyncMock(return_value={"status": "completed"})

    async def asyncTearDown(self):
        await self.c.close()

    async def ready_photo(self, group="Aves"):
        obs = await self.c.new_focus(group)
        sci, common = {"Aves": ("Haliaeetus leucocephalus", "Bald Eagle"),
                       "Plantae": ("Quercus rubra", "Red Oak"),
                       "Insecta": ("Danaus plexippus", "Monarch")}[group]
        candidate = {"sci": sci, "en": common, "p": .95}
        obs.fused = [dict(candidate)]
        obs.detections = [s.Detection("old-image", "image", s.now_iso(), [dict(candidate)])]
        obs.analysis_status = obs.analysis_stage = "ready"
        obs.analysis_request_id, obs.image_revision = "old-photo-request", 1
        obs.hint = "Details of the previous subject"
        self.phone.events.clear()
        return obs

    async def route(self, text, name, args=None):
        self.c.turn_question = text
        self.c.input_acknowledged = False
        await self.c.finish_route({"status": "completed", "output": [{
            "type": "function_call", "name": name, "call_id": "semantic-action",
            "arguments": json.dumps(args or {})}]})

    def assert_fresh_focus(self, old, group):
        obs = self.c.observations[self.c.focus]
        self.assertNotEqual(obs.id, old.id)
        self.assertEqual(obs.group, group)
        self.assertEqual(obs.fused, [])
        self.assertEqual(obs.detections, [])
        self.assertEqual(obs.image_revision, 0)
        self.assertNotEqual(obs.hint, old.hint)
        self.assertIsNone(obs.count)
        self.assertEqual(obs.analysis_status, "awaiting_confirmation")
        self.assertEqual(self.c.camera_request["obs"], obs.id)
        self.assertTrue(any(e.get("type") == "camera_prepare" and e.get("obs") == obs.id
                            for e in self.phone.events))
        self.assertFalse(any(e.get("type") == "capture" for e in self.phone.events))
        return obs

    def assert_no_camera_commands(self):
        self.assertFalse(any(e.get("type") in ("camera_prepare", "camera_cancelled", "capture")
                             for e in self.phone.events))

    async def test_new_observation_honors_semantic_plant_and_insect_requests(self):
        for text, group in (("I found a plant. Could you have a look?", "Plantae"),
                            ("帮我看看手边这盆小草。", "Plantae"),
                            ("The tiny thing near my boot needs a closer look.", "Insecta")):
            with self.subTest(text=text, group=group):
                old = await self.ready_photo()
                await self.route(text, "new_observation", {"group": group})
                self.assert_fresh_focus(old, group)
                self.assertEqual(old.status, "discarded")
        self.c.generate.assert_not_awaited()

    async def test_look_new_target_creates_fresh_evidence_in_the_selected_group(self):
        old = await self.ready_photo()
        await self.route("This leafy one by my hand.", "look", {
            "target": "new", "group": "Plantae", "hint": "Leaf shape"})
        fresh = self.assert_fresh_focus(old, "Plantae")
        self.assertEqual(fresh.hint, "Leaf shape")
        self.assertNotIn("Haliaeetus", json.dumps(fresh.payload()))
        self.assertEqual(old.fused[0]["sci"], "Haliaeetus leucocephalus")

    async def test_look_current_without_group_preserves_the_plant_group_and_evidence(self):
        obs = await self.ready_photo("Plantae")
        evidence = deepcopy((obs.fused, obs.detections))
        await self.route("A closer view of the edge, please.", "look", {
            "target": "current", "hint": "Leaf margin"})
        self.assertEqual(self.c.focus, obs.id)
        self.assertEqual(obs.group, "Plantae")
        self.assertEqual((obs.fused, obs.detections), evidence)
        self.assertNotEqual(obs.analysis_request_id, "old-photo-request")
        self.assertEqual(obs.analysis_status, "awaiting_confirmation")
        self.assertEqual(self.c.camera_request["obs"], obs.id)
        self.assertFalse(any(e.get("type") == "capture" for e in self.phone.events))

    async def test_look_category_change_creates_a_new_focus_even_with_current_target(self):
        old = await self.ready_photo()
        await self.route("The leaves need checking.", "look", {
            "target": "current", "group": "Plantae"})
        self.assert_fresh_focus(old, "Plantae")
        self.assertEqual(old.group, "Aves")
        self.assertEqual(old.fused[0]["sci"], "Haliaeetus leucocephalus")

    async def test_manual_category_change_also_starts_with_unmixed_evidence(self):
        old = await self.ready_photo()
        await self.c.request_look(group="Plantae")
        self.assert_fresh_focus(old, "Plantae")
        self.assertEqual(old.group, "Aves")

    async def test_recent_bird_sound_is_not_attached_when_capturing_a_plant(self):
        old = await self.ready_photo()
        bird = s.Detection("recent-bird", "sound", s.now_iso(), [
            {"sci": "Cardinalis cardinalis", "en": "Northern Cardinal", "p": .9}])
        self.c.sound_windows.append((s.time.monotonic(), bird))
        await self.route("Please look at this sprout.", "look", {"target": "new", "group": "Plantae"})
        plant = self.assert_fresh_focus(old, "Plantae")
        request = self.c.camera_request
        await self.c.camera_ready({"request_id": request["id"]})
        await self.c.camera_confirmation("Start.")
        self.assertEqual(request["phase"], "capturing")
        self.assertEqual(plant.detections, [])
        self.assertEqual(plant.fused, [])
        self.assertEqual(list(self.c.sound_windows)[0][1], bird)

    async def test_background_bird_suggestions_continue_without_polluting_plant_focus(self):
        plant = await self.ready_photo("Plantae")
        before = deepcopy(plant.payload())
        sci = "Cardinalis cardinalis"
        self.c.names[sci] = {"sci": sci, "en": "Northern Cardinal", "zh": "北美红雀"}
        self.c.analyze = lambda _: [{"scientific_name": sci,
                                   "common_name": "Northern Cardinal", "confidence": .8}]
        await self.c.bird_window(np.zeros(1), 3 * s.SR)
        self.assertEqual(plant.payload(), before)
        self.assertEqual(self.c.focus, plant.id)
        self.assertIn(sci, self.c.suggestions)
        self.assertTrue(any(e.get("type") == "heard" and e.get("sci") == sci for e in self.phone.events))
        self.assertEqual(self.c.sound_windows[-1][1].candidates[0]["sci"], sci)
        self.assertFalse(self.c.items)

    async def test_new_focus_does_not_modify_a_confirmed_checklist_entry(self):
        old = await self.ready_photo()
        old.status, old.count = "confirmed", 2
        before = deepcopy(old.payload())
        await self.route("Check the one on my left.", "new_observation", {"group": "Plantae"})
        self.assert_fresh_focus(old, "Plantae")
        self.assertEqual(old.payload(), before)
        self.assertEqual([(r["sci"], r["count"]) for r in self.c.items],
                         [("Haliaeetus leucocephalus", 2)])

    async def test_confirmed_plant_retake_preserves_category_and_the_original_record(self):
        old = await self.ready_photo("Plantae")
        old.status, old.count = "confirmed", 2
        before = deepcopy(old.payload())
        await self.route("A closer view would help.", "look", {"target": "current"})
        fresh = self.assert_fresh_focus(old, "Plantae")
        self.assertEqual(old.payload(), before)
        self.assertEqual([(r["sci"], r["count"]) for r in self.c.items], [("Quercus rubra", 2)])
        self.assertEqual(fresh.group, "Plantae")

    async def test_status_tool_remains_read_only_even_when_text_contains_photo_action_words(self):
        obs = await self.ready_photo()
        vision = ("vision", {"obs": obs.id, "image_revision": 1,
                            "request_id": obs.analysis_request_id}, None)
        self.c.pending.append(vision)
        before = deepcopy(obs.payload())
        await self.route("Take another photo? I am asking where the process stands.",
                         "get_task_status", {"scope": "camera"})
        self.assertEqual(obs.payload(), before)
        self.assertEqual(self.c.focus, obs.id)
        self.assertIn(vision, self.c.pending)
        self.assertIsNone(self.c.camera_request)
        self.assert_no_camera_commands()
        self.c.generate.assert_not_awaited()
        self.c.speak_expert.assert_awaited_once()

    async def test_model_clarification_is_not_rewritten_into_a_new_camera_action(self):
        obs = await self.ready_photo()
        before = deepcopy(obs.payload())
        await self.route("I saw another animal.", "speak", {
            "reason": "Ask the user what help they want"})
        self.assertEqual(obs.payload(), before)
        self.assertEqual(self.c.focus, obs.id)
        self.assertIsNone(self.c.camera_request)
        self.assert_no_camera_commands()
        self.c.generate.assert_awaited_once_with("answer")

    async def test_model_look_requires_a_valid_structured_target(self):
        obs = await self.ready_photo()
        before = deepcopy(obs.payload())
        for args in ({"group": "Plantae"}, {"target": "unknown"}, {"target": None}):
            with self.subTest(args=args):
                await self.route("Please check this.", "look", args)
                self.assertEqual(self.c.focus, obs.id)
                self.assertEqual(obs.payload(), before)
                self.assertIsNone(self.c.camera_request)
                self.assert_no_camera_commands()
        self.c.play_feedback.assert_not_awaited()
        self.c.generate.assert_not_awaited()

    async def test_internal_look_without_target_is_backward_compatible_current(self):
        obs = await self.ready_photo("Plantae")
        await self.c.tool("look", {})
        self.assertEqual(self.c.focus, obs.id)
        self.assertEqual(obs.group, "Plantae")
        self.assertEqual(self.c.camera_request["obs"], obs.id)
        self.assertEqual(obs.analysis_status, "awaiting_confirmation")

    async def test_stale_camera_actions_cannot_mutate_the_newer_focus(self):
        await self.ready_photo()
        self.c.turn_context = self.c.input_context()
        newer = await self.c.new_focus("Plantae")
        self.phone.events.clear()
        before = deepcopy(newer.payload())
        for name, args in (("look", {"target": "new", "group": "Insecta"}),
                           ("look", {"target": "current", "group": "Plantae"}),
                           ("new_observation", {"group": "Mammalia"})):
            with self.subTest(name=name, args=args):
                await self.route("Could you check this one for me?", name, args)
                self.assertEqual(self.c.focus, newer.id)
                self.assertEqual(newer.payload(), before)
                self.assertIsNone(self.c.camera_request)
                self.assert_no_camera_commands()
        self.c.play_feedback.assert_not_awaited()
        self.c.generate.assert_not_awaited()

    async def test_new_input_during_camera_acknowledgement_prevents_the_old_action(self):
        obs = await self.ready_photo()
        self.c.turn_context = self.c.input_context()
        self.c.turn_revision = self.c.input_revision
        before = deepcopy(obs.payload())
        async def user_replied_during_feedback(*args, **kwargs):
            self.c.input_revision += 1
            return True
        self.c.play_feedback.side_effect = user_replied_during_feedback
        await self.route("Please look over here.", "look", {"target": "new", "group": "Plantae"})
        self.c.play_feedback.assert_awaited_once()
        self.assertEqual(self.c.focus, obs.id)
        self.assertEqual(obs.payload(), before)
        self.assertIsNone(self.c.camera_request)
        self.assert_no_camera_commands()
        self.c.generate.assert_not_awaited()

    async def test_new_camera_context_during_acknowledgement_is_not_replaced(self):
        await self.ready_photo()
        self.c.turn_context = self.c.input_context()
        self.c.turn_revision = self.c.input_revision
        newer = []
        async def ui_changed_focus(*args, **kwargs):
            newer.append(await self.c.new_focus("Plantae"))
            self.c.input_revision += 1
            return True
        self.c.play_feedback.side_effect = ui_changed_focus
        await self.route("The insect could use a closer view.", "new_observation", {"group": "Insecta"})
        self.assertEqual(len(newer), 1)
        self.assertEqual(self.c.focus, newer[0].id)
        self.assertEqual(newer[0].group, "Plantae")
        self.assertEqual(newer[0].detections, [])
        self.assertIsNone(self.c.camera_request)
        self.assert_no_camera_commands()

    async def test_prior_capture_finishing_during_routing_does_not_invalidate_the_requested_action(self):
        for target in ("current", "new"):
            with self.subTest(target=target):
                obs = await self.ready_photo()
                await self.c.request_look()
                completed = self.c.camera_request
                completed["phase"] = "classifying"
                obs.analysis_status, obs.analysis_stage = "analyzing", "recognizing"
                self.c.turn_context = self.c.input_context()
                self.c.turn_revision = self.c.input_revision
                self.c.camera_request = None
                obs.analysis_status = obs.analysis_stage = "ready"
                obs.image_revision += 1
                self.phone.events.clear()
                args = {"target": target, **({"group": "Plantae"} if target == "new" else {})}
                await self.route("Could we take a closer look?", "look", args)
                self.assertIsNotNone(self.c.camera_request)
                self.assertNotEqual(self.c.camera_request["id"], completed["id"])
                if target == "current":
                    self.assertEqual(self.c.focus, obs.id)
                    self.assertEqual(obs.image_revision, 2)
                    self.assertEqual(obs.group, "Aves")
                else:
                    self.assert_fresh_focus(obs, "Plantae")

    async def test_prior_capture_finishing_during_acknowledgement_allows_the_current_action(self):
        for target in ("current", "new"):
            with self.subTest(target=target):
                obs = await self.ready_photo()
                await self.c.request_look()
                completed = self.c.camera_request
                completed["phase"] = "classifying"
                obs.analysis_status, obs.analysis_stage = "analyzing", "recognizing"
                self.c.turn_context = self.c.input_context()
                self.c.turn_revision = self.c.input_revision
                self.phone.events.clear()
                async def classification_finished(*args, **kwargs):
                    self.c.camera_request = None
                    obs.analysis_status = obs.analysis_stage = "ready"
                    obs.image_revision += 1
                    return True
                self.c.play_feedback.side_effect = classification_finished
                args = {"target": target, **({"group": "Plantae"} if target == "new" else {})}
                await self.route("Could we take a closer look?", "look", args)
                self.assertIsNotNone(self.c.camera_request)
                self.assertNotEqual(self.c.camera_request["id"], completed["id"])
                if target == "current":
                    self.assertEqual(self.c.focus, obs.id)
                    self.assertEqual(obs.image_revision, 2)
                else:
                    self.assert_fresh_focus(obs, "Plantae")

    async def test_automatic_expert_schedule_ack_has_its_own_input_before_speech(self):
        obs = await self.ready_photo()
        obs.uncertain = True
        self.c.expert = SimpleNamespace(available=True)
        self.c.ask_expert = AsyncMock(return_value={"ok": True, "investigation_id": "R1",
            "answer": "Investigation R1 is scheduled.",
            "result": {"answer": "PRIVATE-RESEARCH-FINDINGS"}})
        events = []
        self.c.inject = AsyncMock(side_effect=lambda text: events.append(("input", text)))
        async def speak(answer, *, guard=None):
            self.assertIsNotNone(guard)
            self.assertTrue(guard())
            events.append(("speech", answer))
            return {"status": "completed"}
        self.c.speak_expert.side_effect = speak
        await self.c.answer_vision({"obs": obs.id, "image_revision": 1,
                                    "request_id": obs.analysis_request_id})
        self.assertEqual([kind for kind, _ in events], ["input", "speech"])
        self.assertTrue(events[0][1].startswith("[VISION_INVESTIGATION_SCHEDULED] "))
        payload = json.loads(events[0][1].split(" ", 1)[1])
        self.assertEqual((payload["observation_id"], payload["image_revision"]), (obs.id, 1))
        self.assertEqual(payload["answer"], "Investigation R1 is scheduled.")
        self.assertNotIn("PRIVATE-RESEARCH-FINDINGS", json.dumps(events))
        self.assertEqual((obs.status, obs.count), ("open", None))
        self.assertFalse(self.c.items)
        self.c.generate.assert_not_awaited()

    async def test_new_input_during_schedule_injection_suppresses_the_old_acknowledgement(self):
        obs = await self.ready_photo()
        obs.uncertain = True
        self.c.expert = SimpleNamespace(available=True)
        self.c.ask_expert = AsyncMock(return_value={"ok": True, "answer": "Investigation R1 is scheduled."})
        async def user_replied(*args):
            self.c.input_revision += 1
        self.c.inject = AsyncMock(side_effect=user_replied)
        await self.c.answer_vision({"obs": obs.id, "image_revision": 1,
                                    "request_id": obs.analysis_request_id})
        self.c.ask_expert.assert_awaited_once()
        self.c.inject.assert_awaited_once()
        self.c.speak_expert.assert_not_awaited()
        self.c.generate.assert_not_awaited()
        self.assertEqual((obs.status, obs.count), ("open", None))

    async def test_invalid_groups_are_rejected_without_camera_or_focus_changes(self):
        obs = await self.ready_photo()
        for name, args in (("new_observation", {"group": "not-a-taxon"}),
                           ("look", {"target": "new", "group": "not-a-taxon"})):
            with self.subTest(name=name):
                await self.route("A closer look at this one.", name, args)
                self.assertEqual(self.c.focus, obs.id)
                self.assertIsNone(self.c.camera_request)
                self.assert_no_camera_commands()
        self.c.play_feedback.assert_not_awaited()

    async def test_model_camera_action_still_requires_ready_preview_then_start(self):
        old = await self.ready_photo()
        await self.route("Please check the small plant here.", "look", {
            "target": "new", "group": "Plantae"})
        fresh = self.assert_fresh_focus(old, "Plantae")
        request = self.c.camera_request
        await self.c.camera_confirmation("Start.")
        self.assertFalse(request["confirmed"])
        self.assertFalse(any(e.get("type") == "capture" for e in self.phone.events))
        await self.c.camera_ready({"request_id": request["id"]})
        self.assertEqual(request["phase"], "ready")
        self.assertFalse(any(e.get("type") == "capture" for e in self.phone.events))
        await self.c.camera_confirmation("Start.")
        captures = [e for e in self.phone.events if e.get("type") == "capture"]
        self.assertEqual([(e["obs"], e["request_id"]) for e in captures],
                         [(fresh.id, request["id"])])

    async def test_status_tool_does_not_read_or_announce_research_findings(self):
        job = Investigation("R1", "What is the plant?", {"id": "original", "fused": []},
            status="completed", stage="completed", result={"ok": True, "answer": "PRIVATE-FINDINGS"})
        self.c.investigations.jobs[job.id] = job
        await self.route("Any news about the first job?", "get_task_status", {"id": "R1"})
        self.assertFalse(job.announced)
        self.assertNotIn("PRIVATE-FINDINGS", self.c.speak_expert.await_args.args[0])
        self.assert_no_camera_commands()
        self.c.generate.assert_not_awaited()

    async def test_ambiguous_checklist_write_still_requires_clear_user_intent(self):
        obs = await self.ready_photo()
        await self.route("Second.", "confirm_observation", {"count": 1})
        self.assertEqual(obs.status, "open")
        self.assertIsNone(obs.count)
        self.assertFalse(self.c.items)
        self.assert_no_camera_commands()
        self.c.generate.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
