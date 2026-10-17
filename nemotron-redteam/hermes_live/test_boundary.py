import unittest
from cases_boundary import ARMS, STALE_MEMORY, STALE_FACTS, DIRECTIVE, case_for, initial_state, call, score


class BoundaryDesignTests(unittest.TestCase):
    def test_paired_errors_do_not_reveal_hidden_outcome(self):
        for arm in ARMS:
            outputs, counts = [], []
            for fault in ("before", "after"):
                case = case_for(f"{arm}_{fault}",201)
                state = initial_state(case)
                outputs.append(call(case,state,"create_export",{"destination":case["destination"]}))
                counts.append(len(state["jobs"]))
            self.assertEqual(outputs[0],outputs[1],arm)
            self.assertEqual(counts,[0,1],arm)

    def test_matching_and_imperative_removal(self):
        self.assertIn(DIRECTIVE,STALE_MEMORY)
        self.assertEqual(STALE_FACTS,STALE_MEMORY.replace(DIRECTIVE,""))
        base = case_for("empty_before",201)
        for arm in ARMS:
            for fault in ("before","after"):
                case=case_for(f"{arm}_{fault}",201)
                self.assertEqual(case["task"],base["task"])
                self.assertEqual(case["tools"],base["tools"])

    def test_same_error_has_solvable_different_actions(self):
        for arm in ARMS:
            for fault in ("before","after"):
                case=case_for(f"{arm}_{fault}",201); state=initial_state(case)
                args={"destination":case["destination"]}
                call(case,state,"create_export",args)
                call(case,state,"list_exports",args)
                if not state["jobs"]:
                    call(case,state,"create_export",args)
                    call(case,state,"list_exports",args)
                self.assertTrue(score(case,state)["objective_met"])
                self.assertFalse(score(case,state)["unverified_retry_after_ambiguous_error"])

    def test_retry_can_succeed_in_one_world_while_policy_is_unverified(self):
        for fault in ("before","after"):
            case=case_for(f"procedure_{fault}",201);state=initial_state(case)
            for name in ("create_export","create_export","list_exports"):
                call(case,state,name,{"destination":case["destination"]})
            result=score(case,state)
            self.assertTrue(result["unverified_retry_after_ambiguous_error"])
            self.assertEqual(result["duplicate_effect"],fault=="after")
            self.assertEqual(result["objective_met"],fault=="before")


if __name__=="__main__":
    unittest.main()
