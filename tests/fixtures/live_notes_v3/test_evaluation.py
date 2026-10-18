import copy
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "evaluate_live_notes_v3", ROOT / "scripts" / "evaluate_live_notes_v3.py"
)
EVALUATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EVALUATOR)
FIXTURE_DIR = Path(__file__).resolve().parent


def load_case_candidate(name):
    case = json.loads((FIXTURE_DIR / f"synthetic_{name}.json").read_text(encoding="utf-8"))
    candidate = json.loads((FIXTURE_DIR / f"candidate_{name}.json").read_text(encoding="utf-8"))
    return case, candidate


class LiveNotesV3EvaluationTests(unittest.TestCase):
    def test_synthetic_reference_candidates_pass_without_provider_calls(self):
        for name in ("11m", "23m"):
            with self.subTest(name=name):
                case, candidate = load_case_candidate(name)
                result = EVALUATOR.evaluate(case, candidate)
                self.assertTrue(result["passed"])
                self.assertEqual(result["provider_calls"], 0)
                self.assertEqual(result["rubric_total"], 100)
                self.assertEqual(result["pass_threshold"], 85)
                self.assertTrue(all(cp["score"] >= 85 for cp in result["checkpoint_results"]))

    def test_topic_proliferation_loses_structure_points(self):
        case, candidate = load_case_candidate("23m")
        candidate["snapshots"][1]["topics"].append({
            "id": "duplicate-topic-id", "semantic_key": "topic_shared",
            "title": "Synthetic duplicate", "block_keys": [],
        })
        result = EVALUATOR.evaluate(case, candidate)
        structure = result["checkpoint_results"][1]["categories"]["structure"]
        integration = result["checkpoint_results"][1]["categories"]["topic_integration"]
        self.assertLess(structure["points"], 20)
        self.assertLess(integration["points"], 20)

    def test_answered_question_must_not_remain_open_or_be_claimed_verified(self):
        case, candidate = load_case_candidate("11m")
        end = candidate["snapshots"][-1]
        end["open_question_ids"].append("q_alpha")
        end["question_states"]["q_alpha"] = "verified_against_source"
        end["claims"][0]["epistemic_state"] = "verified_against_source"
        result = EVALUATOR.evaluate(case, candidate)
        state = result["checkpoint_results"][-1]["categories"]["discussion_state"]
        self.assertLess(state["points"], 15)
        self.assertFalse(result["checkpoint_results"][-1]["passed"])

    def test_revisited_topic_id_must_remain_stable(self):
        case, candidate = load_case_candidate("23m")
        candidate["snapshots"][2]["topics"][0]["id"] = "new-topic-id"
        result = EVALUATOR.evaluate(case, candidate)
        checks = result["checkpoint_results"][-1]["categories"]["topic_integration"]["checks"]
        self.assertTrue(any(not check["passed"] and "stable ID" in check["detail"] for check in checks))

    def test_required_claim_and_evidence_loss_is_detected(self):
        case, candidate = load_case_candidate("11m")
        end = candidate["snapshots"][-1]
        end["claims"] = [claim for claim in end["claims"] if claim["key"] != "fact_13"]
        result = EVALUATOR.evaluate(case, candidate)
        final = result["checkpoint_results"][-1]
        self.assertLess(final["categories"]["preservation"]["points"], 10)
        self.assertLess(final["categories"]["evidence"]["points"], 30)

    def test_fixture_rejects_non_synthetic_or_transcript_bearing_input(self):
        case, _ = load_case_candidate("11m")
        case = copy.deepcopy(case)
        case["contains_transcript_text"] = True
        with self.assertRaises(EVALUATOR.EvaluationError):
            EVALUATOR.load_case(case)


if __name__ == "__main__":
    unittest.main()
