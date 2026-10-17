import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from recursive_redteam.cli import execute, assess_run
from recursive_redteam.budget import BudgetedModel
from recursive_redteam.contracts import Candidate, Completion, Limits, TrialResult
from recursive_redteam.engine import Engine
from recursive_redteam.providers import DemoModel
from recursive_redteam.store import Store
from recursive_redteam.targets import PolicySandbox
from recursive_redteam.memory import ExperimentMemory
from recursive_redteam.config import demo_config, validate_config
from recursive_redteam.credentials import load_credentials
from recursive_redteam.provenance import memory_identity, memory_scope, source_manifest


class WorkflowTests(unittest.TestCase):
    def test_engine_renews_claim_during_slow_target(self):
        contender_results = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            memory = ExperimentMemory(root / 'memory.sqlite', lease_seconds=0.15)
            competitor = ExperimentMemory(root / 'memory.sqlite', lease_seconds=0.15)
            store = Store(root / 'run.sqlite')
            class SlowTarget:
                def describe(self): return {'simulated': True, 'idempotent': True}
                def evaluate(self, candidate, *, trial_id):
                    if candidate is not None:
                        def contend():
                            time.sleep(0.25)
                            other = Candidate('competitor', candidate.family, candidate.hypothesis, candidate.payload)
                            contender_results.append(competitor.claim('scope', other, 'other-campaign'))
                        thread = threading.Thread(target=contend)
                        thread.start()
                        time.sleep(0.4)
                        thread.join()
                    return TrialResult('passed', metrics={'injection_reached': candidate is not None})
            try:
                report = Engine(DemoModel('coordinator'), SlowTarget(), store,
                    Limits(max_rounds=1, max_candidates=1, parallelism=1), memory=memory,
                    memory_scope='scope', source_run=str(root)).run()
                self.assertEqual(report['tasks'][0]['outcome'], 'no_violation')
                self.assertFalse(contender_results[0]['claimed'])
                self.assertEqual(memory.stats('scope')['observations'], 1)
            finally:
                memory.close(); competitor.close(); store.close()

    def test_model_repairs_missing_parent_without_host_invention(self):
        class Repairs:
            simulated = True
            def complete(self, messages, **kwargs):
                last = json.loads(messages[-1]['content']) if messages[-1]['role'] == 'user' else {}
                if last.get('action') == 'repair_plan':
                    return Completion(content=json.dumps({'tasks': [{'family': 'tool-output-injection',
                        'hypothesis': 'model corrected follow-up', 'worker_id': 'worker-1',
                        'parent_id': last['allowed_parent_ids'][0]}]}), model='demo-coordinator')
                reply = DemoModel('coordinator').complete(messages, **kwargs)
                if last.get('action') == 'plan' and last.get('round') == 1:
                    data = json.loads(reply.content)
                    data['tasks'][0]['parent_id'] = None
                    reply.content = json.dumps(data)
                return reply
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'run.sqlite')
            try:
                report = Engine(BudgetedModel(Repairs(), store, 100, 'planner'),
                    PolicySandbox(DemoModel('target')), store,
                    Limits(max_rounds=2, max_candidates=2, parallelism=1, max_model_calls=100)).run()
                self.assertEqual(report['status'], 'completed')
                self.assertEqual(sum(e['kind'] == 'plan_rejected' for e in report['events']), 1)
                self.assertTrue(report['tasks'][1]['parent_id'])
            finally:
                store.close()

    def test_cross_campaign_dedup_and_model_role_transcripts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = demo_config()
            config['memory'] = {'enabled': True, 'path': str(root / 'memory.sqlite')}
            config = validate_config(config)
            first = execute(config, root / 'first')
            second = execute(config, root / 'second')
            self.assertEqual(first['counts']['reproduced'], 4)
            self.assertEqual(second['metadata']['memory_before']['observations'], 4)
            self.assertEqual(second['counts']['trials'], 1)  # control only; no repeated candidates
            self.assertTrue(all(t['outcome'] == 'memory_skipped' for t in second['tasks']))
            self.assertEqual(second['metadata']['memory_after']['observations'], 4)
            roles = {c['role'] for c in first['model_calls']}
            self.assertTrue({'coordinator', 'worker', 'target', 'verifier'} <= roles)
            self.assertTrue(all(c['request'] and c['response'] and c['actual_model'] for c in first['model_calls']))
            self.assertFalse(first['metadata']['acceptance']['passed'])  # offline remains offline
            first['simulated'] = False
            first['metadata']['config']['target']['model'] = {'kind': 'openai-compatible', 'model': 'live'}
            self.assertFalse(assess_run(first)['checks']['live_models'])  # every role must be live
            plans = [c for c in second['model_calls'] if c['role'] == 'coordinator']
            self.assertTrue(json.loads(plans[0]['request']['messages'][1]['content'])['historical_memory'])

    def test_memory_identity_changes_with_policy_model_and_oracle(self):
        config = demo_config()
        sources = source_manifest()
        a = memory_scope(memory_identity(config, {'guarded': False}, sources))
        b = memory_scope(memory_identity(config, {'guarded': True}, sources))
        self.assertNotEqual(a, b)
        sources['targets.py'] = 'changed'
        self.assertNotEqual(a, memory_scope(memory_identity(config, {'guarded': False}, sources)))

    def test_env_file_is_data_and_only_selected_names_loaded(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / '.env'
            path.write_text('CRUSOE_API_KEY="literal$(never-run)"\nUNRELATED=not-loaded\n')
            config = validate_config({'planner': {'base_url': 'https://example.test', 'model': 'test'},
                                      'target': {'kind': 'policy-sandbox'}})
            load_credentials(path, config)
            self.assertEqual(os.environ['CRUSOE_API_KEY'], 'literal$(never-run)')
            self.assertNotIn('UNRELATED', os.environ)
            self.assertNotIn('literal$(never-run)', json.dumps(config))

    def test_external_memory_requires_environment_revision(self):
        raw = {'planner': {'kind': 'demo'}, 'target': {'kind': 'command', 'command': ['python3', 'adapter.py']},
               'memory': {'enabled': True, 'path': '/tmp/test-memory.sqlite'}}
        with self.assertRaises(ValueError):
            validate_config(raw)
        raw['memory']['target_revision'] = 'code-and-data-sha256:example'
        self.assertEqual(validate_config(raw)['memory']['target_revision'], raw['memory']['target_revision'])


if __name__ == '__main__':
    unittest.main()
