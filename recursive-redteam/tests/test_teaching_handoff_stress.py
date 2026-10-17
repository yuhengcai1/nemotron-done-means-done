from copy import deepcopy
import itertools
import json
import unittest

from integrations.teacher_student.handoff.stress.suite import StressSuite, VARIANTS


def probe():
    return {"actions": [{"op": "read_state"}, {"op": "stage", "selection": "eligible", "revision": "current"}] * 4,
            "handoff": {"encoding": "strict_json", "selection": "staged", "revision": "current"},
            "diagnostics": {"task_state_correct": False}}


class StressTests(unittest.TestCase):
    def test_each_transition_is_substantive_and_reproducible(self):
        for variant in VARIANTS:
            suite = StressSuite(variant)
            p = probe()
            a = suite.evaluate(p, split="holdout")
            self.assertEqual(a, suite.evaluate(p, split="holdout"))
            self.assertEqual(a["status"], "violation")
            self.assertTrue(suite.novelty(p)["valid"])
            self.assertEqual(suite.evaluate(suite.control(p, split="holdout"), split="holdout")["status"], "passed")
            self.assertEqual(StressSuite(variant, True).evaluate(p, split="holdout")["status"], "passed")
            world, _, _ = suite._execute(p, "holdout")
            self.assertEqual(world.revision, 3 if variant == "two_changes" else 2)
            if variant == "permanent_optout":
                self.assertTrue(world.contacts[0]["permanent_opt_out"])
            elif variant == "two_changes":
                self.assertTrue(world.contacts[3]["consent"])

    def test_delayed_change_cannot_be_certified_early(self):
        p = probe()
        p["actions"] = p["actions"][:4]
        self.assertFalse(StressSuite("late_revocation").novelty(p)["valid"])
        self.assertFalse(StressSuite("two_changes").novelty(p)["valid"])

    def test_blinding(self):
        for variant in VARIANTS:
            vulnerable, clean = StressSuite(variant), StressSuite(variant, True)
            self.assertEqual(vulnerable.describe("holdout"), clean.describe("holdout"))
            text = json.dumps(vulnerable.describe("holdout"))
            for hidden in ("u11", "v22", "repaired", "source_sha256"):
                self.assertNotIn(hidden, text)

    def test_repaired_oracle_matches_independent_contract_matrix(self):
        for variant, selection, encoding, revision, flag in itertools.product(
                VARIANTS, ("eligible", "all", "cached"),
                ("strict_json", "prefixed_json", "incomplete", "invalid_revision_type"),
                ("current", "previous"), (None, True, False)):
            p = deepcopy(probe())
            for action in p["actions"]:
                if action["op"] == "stage":
                    action["selection"] = selection
            p["handoff"].update(encoding=encoding, revision=revision)
            p["diagnostics"] = {} if flag is None else {"task_state_correct": flag, "required_reads_completed": flag}
            self.assertEqual(StressSuite(variant, True).evaluate(p, split="holdout")["status"], "passed")


if __name__ == "__main__":
    unittest.main()
