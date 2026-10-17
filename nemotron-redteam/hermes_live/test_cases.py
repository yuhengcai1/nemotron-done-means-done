import unittest
from cases import call, case_for, initial_state, score


class OracleTests(unittest.TestCase):
    def test_timeout_pair_indistinguishable_but_requires_different_recovery(self):
        records = []
        for mode in ("export_before", "export_after"):
            case = case_for(mode, 1)
            state = initial_state(case)
            reply = call(case, state, "create_export", {"destination": case["destination"]})
            records.append((reply, len(state["jobs"])))
        self.assertEqual(records[0][0], records[1][0])
        self.assertEqual([r[1] for r in records], [0, 1])

    def test_duplicate_is_not_repaired_by_successful_verification(self):
        case = case_for("export_after_stale", 1)
        state = initial_state(case)
        for op in ("create_export", "create_export", "list_exports"):
            call(case, state, op, {"destination": case["destination"]})
        outcome = score(case, state)
        self.assertTrue(outcome["duplicate_effect"])
        self.assertFalse(outcome["objective_met"])

    def test_poll_name_is_only_mechanism_change(self):
        a, b = case_for("sample_status", 1), case_for("sample_poll", 1)
        self.assertEqual(a["task"].replace("job_api", "job_poll"), b["task"])
        sa, sb = initial_state(a), initial_state(b)
        for _ in range(12):
            ra = call(a, sa, "job_api", {"op": "status", "job_id": "job-1"})
            rb = call(b, sb, "job_poll", {"op": "status", "job_id": "job-1"})
            self.assertEqual(ra, rb)
        self.assertTrue(score(a, sa)["objective_met"])
        self.assertTrue(score(b, sb)["objective_met"])

    def test_poll_recovery_is_solvable(self):
        case = case_for("recovery_poll", 1)
        state = initial_state(case)
        for op in ("status", "logs", "config", "repair", "status"):
            call(case, state, "job_poll", {"op": op, "job_id": "job-1"})
        self.assertTrue(score(case, state)["objective_met"])


if __name__ == "__main__":
    unittest.main()
