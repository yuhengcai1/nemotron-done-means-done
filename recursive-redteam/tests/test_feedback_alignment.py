import json
from pathlib import Path
import tempfile
import unittest

from recursive_redteam.contracts import Completion, Limits, TrialResult
from recursive_redteam.engine import Engine
from recursive_redteam.store import Store


class CapturePlanner:
    def complete(self, messages, **kwargs):
        self.messages = messages
        return Completion(content='{"tasks":[]}')


class HandoffTarget:
    def describe(self):
        return {"families": ["state-refresh-handoff"], "objective": "Separate correct state and valid handoff."}


class FeedbackAlignmentTests(unittest.TestCase):
    def test_report_counts_inconclusive_reviews_without_a_finding(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'state.sqlite')
            try:
                store.add_tasks([{"id": "case", "family": "test", "hypothesis": "Check",
                                  "parent_id": None, "depth": 0}], 0)
                store.update_task('case', 'done', outcome='inconclusive')
                report = store.report()
                self.assertEqual(report['findings'], [])
                self.assertEqual(report['counts']['inconclusive'], 1)
            finally:
                store.close()

    def test_changed_plugin_identity_blocks_resume_without_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'state.sqlite')
            try:
                store.set_metadata('started', True)
                store.set_metadata('status', 'interrupted')
                original = dict(HandoffTarget().describe(), adapter_sha256='original')
                store.set_metadata('target', original)
                store.set_metadata('memory_identity', {'scope': 'original-evidence'})
                model = CapturePlanner()
                with self.assertRaisesRegex(ValueError, 'source identity changed'):
                    Engine(model, HandoffTarget(), store, Limits()).run(resume=True)
                self.assertFalse(hasattr(model, 'messages'))
                self.assertEqual(store.get_metadata('target'), original)
                self.assertEqual(store.get_metadata('memory_identity'), {'scope': 'original-evidence'})
                self.assertEqual(store.get_metadata('status'), 'interrupted')
            finally:
                store.close()

    def test_format_failure_and_correct_state_reach_next_model_plan_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'state.sqlite')
            try:
                task = {"id": "case", "family": "state-refresh-handoff", "hypothesis": "Check format",
                        "depth": 0, "outcome": "inconclusive"}
                store.begin_trial('case:initial')
                store.finish_trial('case:initial', TrialResult('error', error='invalid_final_json',
                    metrics={"task_state_correct": True, "final_format_valid": False,
                             "injection_reached": True, "unrelated_private_value": "excluded"}))
                model = CapturePlanner()
                Engine(model, HandoffTarget(), store, Limits())._plan(1, [task])
                feedback = json.loads(model.messages[-1]['content'])['feedback'][0]
                self.assertEqual(feedback['trial_status'], 'error')
                self.assertEqual(feedback['error'], 'invalid_final_json')
                self.assertEqual(feedback['evidence_kind'], 'incomplete_execution')
                self.assertEqual(feedback['evaluation_metrics'], {"injection_reached": True,
                    "task_state_correct": True, "final_format_valid": False})
                self.assertNotIn('excluded', json.dumps(feedback))
                self.assertNotIn('freshly generated private identifier', model.messages[0]['content'])
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
