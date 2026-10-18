import json
import hashlib
from pathlib import Path
import tempfile
import unittest

from autonomous import Workspace, drive
from recursive_redteam.contracts import Completion


def verdict(passed, read=False):
    return dict(verified_task_passed=passed, passed=passed, timed_out=False,
                exit_code=0, event_errors=[], native_multiagent_observed=True,
                declared_status="done", violations=[] if passed else ["wrong membership"],
                skill_read_observed=read)


class AutonomousTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.calls = []

        def runner(spec, attack, out, record, learned_skill=None, timeout=420):
            out.mkdir(parents=True)
            (out / "events.jsonl").write_text('{"tool":"task"}\n')
            (out / "final-world.private.json").write_text(json.dumps({"history": ["actual action"]}))
            self.calls.append((spec, learned_skill, timeout))
            result = verdict(learned_skill is not None, learned_skill is not None)
            result['skill_sha256'] = hashlib.sha256(learned_skill.encode()).hexdigest() if learned_skill else None
            return result

        self.workspace = Workspace(Path(self.temp.name), {"id": "test"}, runner=runner)

    def training(self):
        return self.workspace.dispatch("run_task", dict(family="drift", contacts=6, page_size=2, updates=1))

    def candidate(self):
        run = self.training()
        self.workspace.dispatch("inspect_run", {"run_id": run["run_id"]})
        return self.workspace.dispatch("propose_skill", {"skill": "model-authored test skill", "evidence": [run["run_id"]]})

    def test_promotion_enters_next_actual_task_and_paired_budgets_match(self):
        candidate = self.candidate()
        result = self.workspace.dispatch("evaluate_candidate", {"candidate_id": candidate["id"]})
        self.assertEqual(result["decision"], "promoted")
        next_task = self.training()
        self.assertEqual(next_task["version"], candidate["id"])
        self.assertTrue(self.workspace.post_promotion[0]["read_observed"])
        self.assertEqual(self.calls[1][0], self.calls[2][0])
        self.assertEqual(self.calls[3][0], self.calls[4][0])
        self.assertEqual(len({x[2] for x in self.calls}), 1)

    def test_no_uninspected_evidence_and_no_holdout_inspection(self):
        run = self.training()
        with self.assertRaises(ValueError):
            self.workspace.dispatch("propose_skill", {"skill": "x", "evidence": [run["run_id"]]})
        with self.assertRaises(ValueError):
            self.workspace.dispatch("inspect_run", {"run_id": "evaluation-001/0-candidate"})

    def test_no_partial_evaluation_if_budget_insufficient(self):
        candidate = self.candidate()
        self.workspace.max_runs = 4
        with self.assertRaises(ValueError):
            self.workspace.dispatch("evaluate_candidate", {"candidate_id": candidate["id"]})
        self.assertEqual(len(self.calls), 1)

    def test_shared_memory_rejects_changed_contract_and_never_reuses_seeds(self):
        first = self.workspace.seed()
        second = Workspace(Path(self.temp.name) / "new-run", {"id": "test"},
                           memory_root=self.workspace.memory_root)
        self.assertNotEqual(first, second.seed())
        with self.assertRaises(ValueError):
            Workspace(Path(self.temp.name) / "changed", {"id": "different"},
                      memory_root=self.workspace.memory_root)

    def test_controller_observes_tool_failure_and_selects_next_action(self):
        seen = []

        class Model:
            def complete(self, messages, tools=None):
                seen.append(json.loads(json.dumps(messages)))
                name = "inspect_run" if len(seen) == 1 else "finish"
                args = {"run_id": "missing"} if len(seen) == 1 else {"reason": "stop after feedback"}
                return Completion(tool_calls=[{"id": str(len(seen)), "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)}}])

        report = drive(Model(), self.workspace, 3)
        self.assertEqual(report["status"], "model_stopped")
        self.assertFalse(report["upgrade_loaded"])
        self.assertIn("error", json.loads(seen[1][-1]["content"]))


if __name__ == "__main__":
    unittest.main()
