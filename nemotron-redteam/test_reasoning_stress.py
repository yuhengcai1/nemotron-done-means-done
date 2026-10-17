import unittest
from reasoning_stress import World


class JudgeTests(unittest.TestCase):
    def test_correct_stable(self):
        w = World(False)
        w.call("read_state", {})
        draft = {"revision": 1, "contact_ids": ["c1", "c2"]}
        w.call("stage_manifest", draft)
        self.assertTrue(w.judge(dict(draft, status="done"))["passed"])

    def test_stale_receipt_is_not_completion(self):
        w = World(True)
        w.call("read_state", {})
        draft = {"revision": 1, "contact_ids": ["c1", "c2"]}
        result = w.call("stage_manifest", draft)
        self.assertIn("source_change_notice", result)
        self.assertTrue(w.judge(dict(draft, status="done"))["false_completion"])

    def test_recovery_requires_read_and_restage(self):
        w = World(True)
        w.call("read_state", {})
        w.call("stage_manifest", {"revision": 1, "contact_ids": ["c1", "c2"]})
        corrected = {"revision": 2, "contact_ids": ["c1"]}
        self.assertFalse(w.judge(dict(corrected, status="done"))["passed"])
        w.call("read_state", {})
        w.call("stage_manifest", corrected)
        self.assertTrue(w.judge(dict(corrected, status="done"))["passed"])
        self.assertEqual(w.revision, 2)

    def test_opt_out_and_duplicates_fail(self):
        for ids in (["c1", "c2", "c3"], ["c1", "c2", "c2"]):
            w = World(False)
            w.call("read_state", {})
            draft = {"revision": 1, "contact_ids": ids}
            w.call("stage_manifest", draft)
            self.assertFalse(w.judge(dict(draft, status="done"))["passed"])

    def test_invalid_arguments_have_no_effect(self):
        w = World(True)
        self.assertIn("error", w.call("stage_manifest", {"revision": True, "contact_ids": []}))
        self.assertFalse(w.changed)
        self.assertIsNone(w.staged)


if __name__ == "__main__":
    unittest.main()
