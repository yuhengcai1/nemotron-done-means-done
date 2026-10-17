from copy import deepcopy
import json
import unittest

from recursive_redteam.contracts import Completion
from recursive_redteam.reliable_student import ReliableTeachingRunner, receipt_state, schema_errors
from integrations.teacher_student.handoff.autonomous.suite import AutonomousSuite, VARIANTS, one_operational_change


def host_fixture(suite):
    key = next(k for k, v in suite._mapping.items() if v == "required_reads_completed")
    return {"actions": [{"op": "stage", "selection": "eligible", "revision": "current"}] * 4 + [{"op": "read_state"}],
            "handoff": {"encoding": "strict_json", "selection": "eligible", "revision": "current"},
            "diagnostics": {key: True}}


def host_explanation(suite, p):
    cf = deepcopy(p)
    cf["actions"].insert(0, {"op": "read_state"})
    return {"status": "explained", "diagnostic_key": next(iter(p["diagnostics"])), "mechanism": "required_observations",
            "facts": suite.facts_for_explanation(p), "receipt_indices": list(range(len(p["actions"]))),
            "predictions": {"single_field": "violation", "no_diagnostics": "passed", "counterfactual": "passed"},
            "counterfactual_probe": cf, "reasoning": "Initial revision was never observed, but the bookkeeping flag replaces the required-observation fact."}


class Scripted:
    def __init__(self, responses):
        self.responses, self.messages = list(responses), []
    def complete(self, messages, **kwargs):
        self.messages.append(deepcopy(messages))
        return Completion(content=json.dumps(self.responses.pop(0)))


class AutonomousTests(unittest.TestCase):
    def test_field_diagnostics_are_public_and_precise(self):
        suite = AutonomousSuite(VARIANTS[0])
        p = host_fixture(suite)
        p['actions'][0] = {'op': 'handoff'}
        p['handoff']['selection'] = ['eligible', 'staged']
        errors = schema_errors(suite.describe('holdout')['probe_schema'], p)
        self.assertIn('$.actions[0].op', [e['path'] for e in errors])
        self.assertIn('$.handoff.selection', [e['path'] for e in errors])
        self.assertNotIn('k81', json.dumps(errors))

    def test_bools_are_not_integer_and_unknown_fields_rejected(self):
        suite = AutonomousSuite(VARIANTS[0]); p = host_fixture(suite)
        p['diagnostics']['elapsed_ms'] = True; p['extra'] = 1
        paths = [e['path'] for e in schema_errors(suite.describe('holdout')['probe_schema'], p)]
        self.assertIn('$.extra', paths); self.assertIn('$.diagnostics.elapsed_ms', paths)

    def test_new_holds_and_repairs_and_complete_causal_chain(self):
        for variant in VARIANTS:
            suite = AutonomousSuite(variant); p = host_fixture(suite)
            first = suite.evaluate(p, split='holdout')
            self.assertEqual(first['status'], 'violation')
            self.assertEqual(first, suite.evaluate(p, split='holdout'))
            self.assertTrue(suite.novelty(p)['valid'])
            self.assertEqual(suite.evaluate(suite.control(p, split='holdout'), split='holdout')['status'], 'passed')
            self.assertEqual(AutonomousSuite(variant, True).evaluate(p, split='holdout')['status'], 'passed')
            self.assertTrue(suite.audit_explanation(p, host_explanation(suite, p), prefix='unit')['verified'])

    def test_previously_wrong_exclusion_explanation_cannot_qualify(self):
        suite = AutonomousSuite(VARIANTS[0]); p = host_fixture(suite)
        claim = host_explanation(suite, p)
        claim['facts']['eligible_ids_at_last_read'].append('n63')
        claim['facts']['initial_observation_complete'] = True
        checked = suite.audit_explanation(p, claim, prefix='unit')
        self.assertFalse(checked['verified'])
        self.assertIn('initial_observation_complete', checked['student_feedback']['factual_fields_inconsistent'])
        self.assertNotIn('expected_facts', checked['student_feedback'])

    def test_wrong_diagnostic_and_predictions_rejected(self):
        suite = AutonomousSuite(VARIANTS[0]); p = host_fixture(suite)
        p['diagnostics']['result_hint'] = True
        claim = host_explanation(suite, p); claim['diagnostic_key'] = 'result_hint'
        self.assertFalse(suite.audit_explanation(p, claim, prefix='unit')['verified'])
        claim = host_explanation(suite, host_fixture(suite)); claim['predictions']['single_field'] = 'passed'
        self.assertFalse(suite.audit_explanation(host_fixture(suite), claim, prefix='unit')['verified'])

    def test_multivariable_counterfactual_rejected(self):
        suite = AutonomousSuite(VARIANTS[0]); p = host_fixture(suite); cf = deepcopy(p)
        cf['actions'].insert(0, {'op': 'read_state'}); cf['handoff']['revision'] = 'previous'
        self.assertFalse(one_operational_change(p, cf))
        claim = host_explanation(suite, p); claim['counterfactual_probe'] = cf
        self.assertFalse(suite.audit_explanation(p, claim, prefix='unit')['verified'])

    def test_nonmonotonic_state_does_not_hide_version_change(self):
        suite = AutonomousSuite('revoke_restore'); p = host_fixture(suite)
        p['actions'] = [{'op': 'read_state'}] + p['actions']
        world, _, receipts = suite._execute(suite._normalize(p), 'holdout')
        reads = [r['result'] for r in receipts if r['operation'] == 'read_state']
        self.assertEqual(reads[0]['contacts'], reads[-1]['contacts'])
        self.assertEqual(world.revision, 3)
        self.assertEqual(receipt_state(suite.evaluate(p, split='holdout'))['latest_observed_revision'], 3)

    def test_early_staging_cannot_pass_new_novelty_gate(self):
        suite = AutonomousSuite(VARIANTS[0]); p = host_fixture(suite)
        p['actions'] = p['actions'][:3] + [{'op': 'read_state'}]
        self.assertFalse(suite.novelty(p)['valid'])
        state = receipt_state(suite.evaluate(p, split='holdout'))
        self.assertTrue(state['last_stage_stale'])

    def test_blind_assignment_and_unchanged_lesson(self):
        from integrations.teacher_student.handoff.suite import HandoffSuite
        for v in VARIANTS:
            a, b = AutonomousSuite(v), AutonomousSuite(v, True)
            self.assertEqual(a.describe('holdout'), b.describe('holdout'))
            self.assertEqual(a.lesson(), HandoffSuite().lesson())
            self.assertNotIn('k81', json.dumps(a.describe('holdout')))
            self.assertNotIn('required_reads_completed', json.dumps(a.describe('holdout')))

    def test_worker_repairs_itself_and_host_does_not_fill_answer(self):
        suite = AutonomousSuite(VARIANTS[0]); good = host_fixture(suite); bad = deepcopy(good)
        bad['actions'][0] = {'op': 'handoff'}
        worker = Scripted([{'action':'probe','hypothesis':'a','probe':bad},
                           {'action':'probe','hypothesis':'b','probe':good},
                           {'action':'submit','probe_id':'unit:probe:1','signature':'handoff_oracle:diagnostics_override_execution_facts','reasoning':'measured'}])
        planner = Scripted([{'strategy':'test','worker_task':'explore'}])
        result = ReliableTeachingRunner(planner, worker, None, max_steps=4).episode(suite, lesson=None, condition='unit')
        self.assertTrue(result['discovery_verified'])
        self.assertFalse(result['verified'])  # causal understanding still pending
        self.assertEqual(result['observations']['unit:probe:0']['probe'], bad)
        feedback = json.loads(worker.messages[1][-1]['content'])
        self.assertEqual(feedback['input_errors'][0]['path'], '$.actions[0].op')
        self.assertNotIn('corrected_probe', feedback)

    def test_blind_reviewer_cannot_override_wrong_facts(self):
        suite = AutonomousSuite(VARIANTS[0]); p = host_fixture(suite); claim = host_explanation(suite,p)
        review = {'facts':suite.facts_for_explanation(p),'mechanism':'required_observations','evidence_trial_ids':['x']}
        self.assertTrue(suite.audit_blind_review(p,claim,review,[{'id':'x'}]))
        review['facts']['initial_observation_complete'] = True
        self.assertFalse(suite.audit_blind_review(p,claim,review,[{'id':'x'}]))


if __name__ == '__main__':
    unittest.main()
