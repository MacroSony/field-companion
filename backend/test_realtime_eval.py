"""Offline checks for the paired benchmark's labels, isolation and grading."""
import asyncio
import json
import unittest

import server as backend
from realtime_eval import (AudioTranscripts, MISSING, collect_audio_transcripts, fixture, grade,
                           matches, speech_errors, summarize, valid_schema)
from realtime_eval_cases import CASES, MALLARD
from realtime_provider import RealtimeConfig


def response(name, arguments):
    return {"status": "completed", "output": [{"type": "function_call", "name": name,
                                               "arguments": json.dumps(arguments)}]}


class GraderTests(unittest.TestCase):
    def setUp(self):
        self.cases = {c.id: c for c in CASES}

    def test_case_ids_unique_and_every_label_uses_a_real_tool(self):
        self.assertEqual(len(self.cases), len(CASES))
        tools = {t["name"] for t in backend.TOOLS}
        for case in CASES:
            self.assertTrue(case.expected)
            self.assertTrue(all(v["tool"] in tools for v in case.expected))

    def test_equivalent_single_item_batch_and_plural_names_are_accepted(self):
        case = self.cases["log-two"]
        self.assertTrue(grade(case, response("add_observation", {
            "name": "Mallards", "count": 2, "group": "Aves", "notes": ""}))["passed"])
        self.assertTrue(grade(case, response("add_observations", {
            "items": [{"name": "Anas platyrhynchos", "count": 2}]}))["passed"])

    def test_missing_count_and_wrong_count_fail(self):
        case = self.cases["log-two"]
        for arguments in ({"name": "Mallard"}, {"name": "Mallard", "count": 4, "group": "Aves", "notes": ""}):
            self.assertFalse(grade(case, response("add_observation", arguments))["passed"])

    def test_wrong_plant_group_and_camera_target_fail(self):
        case = self.cases["plant-after-eagle"]
        self.assertFalse(grade(case, response("look", {"target": "current", "group": "Plantae"}))["passed"])
        self.assertFalse(grade(case, response("look", {"target": "new", "group": "Aves"}))["passed"])
        self.assertTrue(grade(case, response("new_observation", {"group": "Plantae"}))["passed"])

    def test_matching_tool_is_insufficient_if_it_speaks_or_emits_multiple_calls(self):
        case = self.cases["mic-check"]
        output = response("speak", {"reason": "mic check"})
        self.assertFalse(grade(case, output, route_text="I heard you.")["passed"])
        self.assertFalse(grade(case, output, audio_bytes=2)["passed"])
        output["output"] *= 2
        self.assertFalse(grade(case, output)["passed"])

    def test_final_message_cannot_hide_forbidden_routing_text_when_deltas_are_missing(self):
        case = self.cases["mic-check"]
        output = response("speak", {"reason": "mic check"})
        output["output"].insert(0, {"type": "message", "content": [{"type": "output_text", "text": "I heard you."}]})
        result = grade(case, output)
        self.assertTrue(result["arguments_correct"])
        self.assertFalse(result["route_silent"])

    def test_guard_interception_is_not_credited_to_model(self):
        case = self.cases["suggest-okay"]
        result = grade(case, response("accept_suggestions", {}))
        self.assertFalse(result["passed"])
        self.assertEqual(result["forbidden_attempts"], ["accept_suggestions"])
        self.assertEqual(result["guard"][0]["decision"], "ignore")

    def test_status_does_not_allow_unsolicited_explanation(self):
        case = self.cases["done-not-explain"]
        self.assertFalse(grade(case, response("get_investigations", {"id": "R1", "explain": True}))["passed"])
        self.assertTrue(grade(case, response("get_investigations", {"id": "R1"}))["passed"])
        self.assertTrue(grade(case, response("get_task_status", {"id": "R1"}))["passed"])
        self.assertTrue(grade(self.cases["photo-progress"], response("get_task_status", {"scope": "all"}))["passed"])

    def test_explanation_offer_cannot_silently_select_a_different_result(self):
        case = self.cases["offer-okay"]
        self.assertFalse(grade(case, response("get_investigations", {"explain": True, "id": "R2"}))["passed"])
        self.assertTrue(grade(case, response("get_investigations", {"explain": True}))["passed"])

    def test_equivalent_all_task_selectors_are_accepted(self):
        case = self.cases["all-progress"]
        for args in ({"scope": "all"}, {"scope": "all", "selection": "all"}, {"selection": "all"}):
            self.assertTrue(grade(case, response("get_task_status", args))["passed"])
        self.assertFalse(grade(case, response("get_task_status", {"scope": "all", "id": "R1"}))["passed"])

    def test_contradictory_task_ids_do_not_pass_by_matching_selection_alone(self):
        case = self.cases["research-progress"]
        self.assertFalse(grade(case, response("get_task_status", {
            "scope": "investigations", "selection": "first", "id": "R2"}))["passed"])
        case = self.cases["photo-progress"]
        self.assertFalse(grade(case, response("get_task_status", {"id": "R1"}))["passed"])

    def test_multispecies_order_is_irrelevant_but_omissions_fail(self):
        case = self.cases["log-many"]
        items = [{"name": "American Robin", "count": 3}, {"name": "Mallard", "count": 2}]
        self.assertTrue(grade(case, response("add_observations", {"items": items}))["passed"])
        self.assertFalse(grade(case, response("add_observations", {"items": items[:1]}))["passed"])

    def test_schema_rejects_bool_as_count_invalid_enum_and_nonfinite(self):
        self.assertFalse(valid_schema(True, {"type": "integer", "minimum": 1}))
        self.assertFalse(valid_schema(float("nan"), {"type": "number"}))
        self.assertFalse(valid_schema("newer", {"type": "string", "enum": ["new", "current"]}))
        self.assertTrue(matches(MISSING, {"$default": False}))
        self.assertTrue(matches("mallards", MALLARD))

    def test_technical_failure_is_counted_in_total_not_success_latency(self):
        sample = {"provider": "higgs", "model": "fixture", "category": "camera", "passed": False,
                  "technical_success": False, "attempts": [{}, {}]}
        result = summarize([sample])["higgs"]
        self.assertEqual((result["samples"], result["completed"], result["passed"], result["retries"]), (1, 0, 0, 1))
        self.assertIsNone(result["route_median_ms"])

    def test_speech_metric_ignores_punctuation_and_handles_chinese(self):
        self.assertTrue(speech_errors('Two mallards.', 'two mallards!')["exact_normalized"])
        self.assertEqual(speech_errors('我看到两只鸟', '我看到三只鸟')["edit_errors"], 1)
        self.assertEqual(speech_errors('Two mallards', '')["edit_errors"], 2)

    def test_audio_guard_uses_actual_transcript_instead_of_known_script(self):
        case = self.cases["log-two"]
        output = response("add_observation", {"name": "Mallard", "count": 2, "group": "Aves", "notes": ""})
        result = grade(case, output, input_text='Do not add two mallards.')
        self.assertEqual(result["guard"][0]["reason"], "negated_write")

    def test_expert_translation_is_not_a_wrong_action_when_backend_uses_original_turn(self):
        case = self.cases["expert-habitat-zh"]
        self.assertTrue(grade(case, response("ask_expert", {"question": "What habitat does this bird live in?"}))["passed"])
        self.assertFalse(grade(case, response("get_observation", {}))["passed"])


class FixtureTests(unittest.IsolatedAsyncioTestCase):
    async def test_split_transcripts_are_ordered_and_collection_does_not_stop_at_first(self):
        events = asyncio.Queue()
        for item_id, start in (("first", 0), ("second", 1500)):
            events.put_nowait({"type": "input_audio_buffer.speech_started", "item_id": item_id, "audio_start_ms": start})
        events.put_nowait({"type": "conversation.item.input_audio_transcription.completed", "item_id": "second", "transcript": "Can you identify it?"})
        events.put_nowait({"type": "conversation.item.input_audio_transcription.completed", "item_id": "first", "transcript": "I see a plant."})
        collector = AudioTranscripts()
        text = await collect_audio_transcripts(events.get, collector, timeout=.3, settle=.01)
        self.assertEqual(text, "I see a plant. Can you identify it?")
        self.assertEqual(len(collector.segments()), 2)

    async def test_missing_transcript_is_a_technical_failure_not_a_silent_omission(self):
        events = asyncio.Queue()
        events.put_nowait({"type": "input_audio_buffer.speech_started", "item_id": "missing", "audio_start_ms": 0})
        collector = AudioTranscripts()
        with self.assertRaises(TimeoutError):
            await collect_audio_transcripts(events.get, collector, timeout=.02, settle=.005)

    async def test_provider_contexts_are_identical_and_findings_never_leak(self):
        configs = (RealtimeConfig(), RealtimeConfig("openai", "gpt-realtime-2.1", "gpt-transcribe", "marin"))
        for case in CASES:
            sessions = [fixture(case, c) for c in configs]
            try:
                left, right = (s.config("route") for s in sessions)
                for key in ("instructions", "tools", "tool_choice", "output_modalities"):
                    self.assertEqual(left[key], right[key], (case.id, key))
                self.assertNotIn("Fixture findings", left["instructions"])
                self.assertFalse(any(s.tasks for s in sessions))
            finally:
                for session in sessions:
                    await session.close()


if __name__ == "__main__":
    unittest.main()
