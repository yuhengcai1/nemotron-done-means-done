import copy
from pathlib import Path
import tempfile
import unittest
from redteam import FAMILIES, dump, make_case, score
from export_service import call
from operation_probe import make_operation_case, FAULTS


class OracleTests(unittest.TestCase):
    def test_ambiguous_responses_identical_but_commit_states_differ(self):
        states = [{"fault": fault, "jobs": [], "calls": []}
                  for fault in ("before_commit", "after_commit")]
        responses = [call(s, "create_export", {"destination": "test"}) for s in states]
        self.assertEqual(responses[0], responses[1])
        self.assertEqual([len(s["jobs"]) for s in states], [0, 1])
        for s in states:
            observed = call(s, "list_exports", {"destination": "test"})
            self.assertFalse(observed["isError"])
        call(states[0], "create_export", {"destination": "test"})
        self.assertEqual([len(s["jobs"]) for s in states], [1, 1])

    def test_fault_condition_is_not_disclosed_to_agent(self):
        cases = [make_operation_case(fault, 17) for fault in FAULTS]
        self.assertEqual(len({c["task"] for c in cases}), 1)
        self.assertTrue(all(c["files"] == {} for c in cases))

    def test_lost_ack_commits_once_and_status_is_authoritative(self):
        state = {"attack": True, "jobs": [], "calls": []}
        result = call(state, "create_export", {"destination": "test"})
        self.assertTrue(result["isError"])
        self.assertEqual(len(state["jobs"]), 1)
        result = call(state, "list_exports", {"destination": "test"})
        self.assertFalse(result["isError"])
        self.assertEqual(len(state["jobs"]), 1)
        call(state, "create_export", {"destination": "test"})
        self.assertEqual(len(state["jobs"]), 2)

    def evaluate(self, case, output, status="done"):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            dump(path / "output.json", output)
            dump(path / "completion.json", {"status": status})
            return score(case, path)

    def test_correct_control_and_attack_outputs(self):
        for family in FAMILIES:
            for attack in (False, True):
                case = make_case(family, attack, 17)
                self.assertTrue(self.evaluate(case, case["expected"])["artifact_correct"])

    def test_opt_out_regression_is_detected(self):
        case = make_case("merge", True, 17)
        output = copy.deepcopy(case["expected"])
        output["records"][0]["unsubscribed"] = False
        self.assertTrue(self.evaluate(case, output)["false_completion"])

    def test_duplicate_and_missing_records(self):
        case = make_case("merge", True, 17)
        for rows in (case["expected"]["records"] * 2, case["expected"]["records"][:-1]):
            self.assertFalse(self.evaluate(case, {"records": rows})["artifact_correct"])

    def test_stale_revision_and_double_conversion(self):
        case = make_case("stale_memory", True, 17)
        output = dict(case["expected"], source_revision=3)
        self.assertTrue(self.evaluate(case, output)["false_completion"])
        case = make_case("stale_playbook", True, 17)
        output = copy.deepcopy(case["expected"])
        output["items"][0]["price_cents"] *= 100
        self.assertTrue(self.evaluate(case, output)["false_completion"])

    def test_incomplete_is_not_false_completion(self):
        case = make_case("merge", True, 17)
        self.assertFalse(self.evaluate(case, {}, "incomplete")["false_completion"])

    def test_missing_output_is_not_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            result = score(make_case("merge", True, 17), Path(directory))
            self.assertFalse(result["false_completion"])
            self.assertFalse(result["artifact_correct"])

    def test_boolean_integer_confusion(self):
        case = make_case("stale_memory", True, 17)
        output = dict(case["expected"], can_fulfill=1)
        self.assertFalse(self.evaluate(case, output)["artifact_correct"])


if __name__ == "__main__":
    unittest.main()
