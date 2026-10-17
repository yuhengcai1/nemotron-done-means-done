import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from integrations.student_runtime.cli import drive, main, snapshot_intact
from recursive_redteam.contracts import Completion, ProviderError
from recursive_redteam.teaching import TeachingRunner
from tests.test_teaching import FakeSuite, Planner, Worker, Reviewer


class Suite(FakeSuite):
    def describe(self,split):
        return {**super().describe(split),'execution_kind':'deterministic_local_recreation'}


class Student:
    max_retries=0
    def __init__(self, throttle=False):self.calls=0;self.throttle=throttle
    def complete(self,messages,**kwargs):
        self.calls+=1
        if self.throttle and self.calls==1:raise ProviderError('Provider HTTP 429 after 1 attempt(s)')
        system=messages[0]['content'];data=json.loads(messages[-1]['content'])
        if 'You plan' in system:value={'strategy':'check','worker_task':'experiment'}
        elif 'Independently review' in system:value={'verdict':'verified','reasoning':'receipts','evidence_trial_ids':[t['id'] for t in data['evidence']['trials']]}
        elif 'probe_id' in data:value={'action':'submit','probe_id':data['probe_id'],'signature':'state_mismatch','reasoning':'observed'}
        else:value={'action':'probe','hypothesis':'transition','probe':{'value':2}}
        return Completion(content=json.dumps(value),model='fixture')


class StudentRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.run=Path(self.tmp.name);self.now=100.0
        source=self.run/'frozen/source.py';source.parent.mkdir();source.write_text('# fixed test snapshot\n')
        self.write('source-manifest.json',{'source.py':hashlib.sha256(source.read_bytes()).hexdigest()})
        self.write('config.json',{'planner':{'model':'fixture'}})
        self.protocol={'max_attempts':8,'burst_attempts':1,'min_interval':1,'error_cooldown':5,
                       'deadline_epoch':200,'condition':'taught','episode_id':'one','max_steps':5}
        self.write('protocol.json',self.protocol)
    def tearDown(self):self.tmp.cleanup()
    def write(self,path,data):(self.run/path).write_text(json.dumps(data))
    def sleep(self,seconds):self.now+=seconds
    def execute(self,model):
        return drive(self.run,model,Suite(),clock=lambda:self.now,sleep=self.sleep)

    def test_driver_self_continues_across_bursts_and_stops_completed(self):
        model=Student();result=self.execute(model)
        self.assertEqual(result['status'],'completed');self.assertTrue(result['episode']['verified'])
        self.assertEqual(model.calls,4)
        journal=json.loads((self.run/'model-journal.json').read_text())
        self.assertEqual(journal['model_usage']['actual_http_attempts'],4)
        self.assertGreater(result['replayed_completions'],0)
        self.assertEqual(self.execute(model)['new_api_attempts'],0)
        self.assertEqual(model.calls,4)
        self.assertTrue(json.loads((self.run/'signals.json').read_text())['evidence']['verified_novel_discovery'])

    def test_throttle_cooldown_keeps_same_attempt_budget(self):
        model=Student(throttle=True);result=self.execute(model)
        self.assertEqual(result['status'],'completed');self.assertEqual(model.calls,5)
        self.assertGreaterEqual(self.now,108)
        self.assertEqual(result['budget']['attempts'],5)

    def test_budget_exhaustion_preserves_prior_probes_and_resume_cannot_reset(self):
        self.protocol['max_attempts']=2;self.write('protocol.json',self.protocol)
        model=Student();result=self.execute(model)
        self.assertEqual(result['reason'],'persistent_attempt_budget_exhausted')
        partial=json.loads((self.run/'episode.json').read_text())
        self.assertEqual(len(partial['observations']),1)
        self.assertFalse(partial['verified'])
        self.assertEqual(self.execute(model)['reason'],'persistent_attempt_budget_exhausted')
        self.assertEqual(model.calls,2)

    def test_deadline_stops_before_next_request(self):
        self.protocol['deadline_epoch']=101;self.write('protocol.json',self.protocol)
        model=Student();result=self.execute(model)
        self.assertEqual(result['reason'],'campaign_wall_budget_exhausted');self.assertEqual(model.calls,1)

    def test_unknown_response_stops_without_looping(self):
        class TimedOut(Student):
            def complete(self,*a,**k):self.calls+=1;raise ProviderError('Provider connection failed or timed out')
        model=TimedOut();result=self.execute(model)
        self.assertIn('unknown',result['reason']);self.assertEqual(model.calls,1)
        self.execute(model);self.assertEqual(model.calls,1)

    def test_frozen_sources_and_model_identity_are_enforced(self):
        (self.run/'frozen/source.py').write_text('# modified')
        self.assertFalse(snapshot_intact(self.run))
        with self.assertRaises(ValueError):self.execute(Student())

    def test_invalid_worker_json_can_be_corrected_by_student(self):
        class Repair(Worker):
            def __init__(self):super().__init__();self.calls=0
            def complete(self,*a,**k):
                self.calls+=1
                if self.calls==1:return Completion(content='not JSON')
                return super().complete(*a,**k)
        worker=Repair()
        result=TeachingRunner(Planner(),worker,Reviewer(),max_steps=4,repair_invalid_worker=True).episode(FakeSuite(),lesson=None,condition='baseline')
        self.assertTrue(result['verified']);self.assertEqual(result['actions'][0]['error'],'invalid_worker_json')

    def test_non_object_worker_json_survives_checkpoint_and_is_repaired(self):
        class BadFirstWorker(Student):
            bad=True
            def complete(self,messages,**kwargs):
                if 'delegated reliability-test worker' in messages[0]['content'] and self.bad:
                    self.bad=False;self.calls+=1
                    return Completion(content='[]',model='fixture')
                return super().complete(messages,**kwargs)
        model=BadFirstWorker();result=self.execute(model)
        self.assertEqual(result['status'],'completed');self.assertTrue(result['episode']['verified'])
        self.assertEqual(model.calls,5)

    def test_completed_cli_resume_never_loads_credentials_or_calls_api(self):
        self.write('report.json',{'status':'completed'})
        with patch('integrations.student_runtime.cli.load_credentials') as credentials:
            self.assertEqual(main(['--out',str(self.run),'--frozen-entry','--resume']),0)
            credentials.assert_not_called()


if __name__=='__main__':unittest.main()
