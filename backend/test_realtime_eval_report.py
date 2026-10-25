"""Offline report integrity checks; never connect to either provider."""
from copy import deepcopy
import unittest

from realtime_eval import grade
from realtime_eval_cases import CASES, public_case
from realtime_eval_report import regrade, render
from test_realtime_eval import response


class ReportTests(unittest.IsolatedAsyncioTestCase):
    def report(self):
        case = next(c for c in CASES if c.id == "all-progress")
        output = response("get_task_status", {"scope": "all"})
        row = {"case": case.id, "category": case.category, "provider": "higgs", "model": "fixture-model",
               "round": 1, "input": case.text, "state": case.state, "technical_success": True,
               "route_ms": 20, "attempts": [{"response": output, "route_text": "", "audio_bytes": 0}]}
        row.update(grade(case, output))
        row["passed"] = False  # Simulate the earlier redundant-selector scorer bug.
        row["arguments_correct"] = False
        return {"created_at": "2026-10-24", "finished_at": "2026-10-24", "mode": "text",
                "rounds": 1, "cases": [public_case(case)], "samples": [row], "pairing_valid": True}

    async def test_rescoring_preserves_original_evidence_and_documents_correction(self):
        report = self.report()
        original_response = deepcopy(report["samples"][0]["attempts"])
        updated = await regrade(report)
        row = updated["samples"][0]
        self.assertTrue(row["passed"])
        self.assertFalse(row["original_grading"]["passed"])
        self.assertEqual(row["attempts"], original_response)
        self.assertIn("Scoring audit:", render(updated))

    async def test_rescoring_refuses_changed_model_inputs(self):
        report = self.report()
        report["cases"][0]["text"] = "Changed input"
        with self.assertRaises(ValueError):
            await regrade(report)

    async def test_report_separates_correct_actions_and_protocol(self):
        report = await regrade(self.report())
        text = render(report)
        self.assertIn("Correct action | Strict routing", text)
        self.assertIn("No production prompts were tuned", text)
        self.assertNotIn("OPENAI_API_KEY", text)


if __name__ == "__main__":
    unittest.main()
