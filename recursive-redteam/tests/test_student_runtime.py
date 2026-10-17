import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from recursive_redteam.contracts import Completion, ProviderError
from integrations.student_runtime.checkpoint import CheckpointLedger, Deferred
from integrations.student_runtime.episode import resume_local_episode
from integrations.student_runtime.signals import signals, compare
from tests.test_teaching import FakeSuite


class Model:
    max_retries=0
    def __init__(self, outcomes): self.outcomes=iter(outcomes); self.calls=0
    def complete(self,*args,**kwargs):
        self.calls+=1; value=next(self.outcomes)
        if isinstance(value,BaseException): raise value
        return Completion(content=value,model='fixture')


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'calls.sqlite';self.now=100
        self.identity={'model':'fixture','condition':'taught','source':'v1'}
        self.msg=[{'role':'user','content':'one'}]
    def tearDown(self): self.tmp.cleanup()
    def ledger(self,**kwargs):
        return CheckpointLedger(self.path,clock=lambda:self.now,min_interval=0,**kwargs)
    def bind(self,ledger,model,**kwargs):
        return ledger.model(model,episode='e1',role='worker',identity=self.identity,**kwargs)

    def test_resume_replays_without_spending_and_detects_divergence(self):
        model=Model(['first','second']);ledger=self.ledger()
        self.assertEqual(self.bind(ledger,model).complete(self.msg).content,'first');ledger.close()
        ledger=self.ledger();wrapped=self.bind(ledger,model)
        self.assertEqual(wrapped.complete(self.msg).content,'first');self.assertEqual(model.calls,1)
        self.assertEqual(wrapped.complete([{'role':'user','content':'two'}]).content,'second')
        with self.assertRaises(ValueError): self.bind(ledger,model).complete([{'role':'user','content':'changed'}])
        self.assertEqual(model.calls,2);ledger.close()

    def test_error_cooldown_survives_restart_and_budget_counts_failures(self):
        model=Model([ProviderError('Provider HTTP 429 after 1 attempt(s)'),'ok','unused'])
        ledger=self.ledger(max_attempts=2)
        with self.assertRaises(Deferred): self.bind(ledger,model).complete(self.msg)
        ledger.close();ledger=self.ledger(max_attempts=2)
        with self.assertRaises(Deferred) as err: self.bind(ledger,model).complete(self.msg)
        self.assertEqual(err.exception.reason,'transport_cooldown');self.assertEqual(model.calls,1)
        self.now=161;wrapped=self.bind(ledger,model);self.assertEqual(wrapped.complete(self.msg).content,'ok')
        with self.assertRaises(Deferred) as err: wrapped.complete(self.msg)
        self.assertEqual(err.exception.reason,'persistent_attempt_budget_exhausted')
        self.assertEqual(ledger.report()['attempts'],2);ledger.close()

    def test_unknown_interrupted_outcome_never_auto_resubmits(self):
        model=Model([KeyboardInterrupt(),'unused']);ledger=self.ledger()
        with self.assertRaises(KeyboardInterrupt): self.bind(ledger,model).complete(self.msg)
        ledger.close();ledger=self.ledger()
        with self.assertRaises(Deferred) as err:self.bind(ledger,model).complete(self.msg)
        self.assertIn('unknown',err.exception.reason);self.assertEqual(model.calls,1)
        self.assertEqual(ledger.report()['unknown_outcomes'],1);ledger.close()

    def test_timeout_is_unknown_and_auth_rejection_is_not_retried(self):
        for diagnostic in ('Provider connection failed or timed out','Provider HTTP 401 after 1 attempt(s)'):
            with self.subTest(diagnostic=diagnostic), tempfile.TemporaryDirectory() as directory:
                ledger=CheckpointLedger(Path(directory)/'calls.sqlite',min_interval=0)
                model=Model([ProviderError(diagnostic),'unused'])
                with self.assertRaises(Deferred):self.bind(ledger,model).complete(self.msg)
                with self.assertRaises(Deferred):self.bind(ledger,model).complete(self.msg)
                self.assertEqual(model.calls,1);ledger.close()

    def test_budget_and_identity_cannot_silently_reset(self):
        ledger=self.ledger(max_attempts=2);model=Model(['one']);self.bind(ledger,model).complete(self.msg)
        with self.assertRaises(ValueError):ledger.model(model,episode='e1',role='worker',identity={'model':'different'})
        ledger.close()
        with self.assertRaises(ValueError):self.ledger(max_attempts=3)

    def test_hidden_retries_rejected(self):
        ledger=self.ledger();model=Model([]);model.max_retries=4
        with self.assertRaises(ValueError): self.bind(ledger,model)
        ledger.close()

    def test_conditions_never_share_cached_answers(self):
        ledger=self.ledger();model=Model(['baseline','taught'])
        for episode in ('baseline','taught'):
            answer=ledger.model(model,episode=episode,role='worker',identity={'condition':episode}).complete(self.msg)
            self.assertEqual(answer.content,episode)
        self.assertEqual(model.calls,2);ledger.close()

    def test_global_pending_request_blocks_other_connection(self):
        ledger=self.ledger();other=self.ledger();outer=self
        class Callback(Model):
            def complete(self,*a,**k):
                with outer.assertRaises(Deferred):outer.bind(other,Model(['unused'])).complete(outer.msg)
                return Completion(content='ok')
        self.bind(ledger,Callback([])).complete(self.msg)
        self.assertEqual(ledger.report()['attempts'],1);ledger.close();other.close()

    def test_whole_episode_recovers_roles_without_extra_model_calls(self):
        class Suite(FakeSuite):
            def describe(self,split):return {**super().describe(split),'execution_kind':'deterministic_local_recreation'}
        class Student(Model):
            def complete(self,messages,**kwargs):
                self.calls+=1;system=messages[0]['content'];data=json.loads(messages[-1]['content'])
                if 'You plan' in system: value={'strategy':'check','worker_task':'experiment'}
                elif 'Independently review' in system:value={'verdict':'verified','reasoning':'receipts','evidence_trial_ids':[t['id'] for t in data['evidence']['trials']]}
                elif 'probe_id' in data:value={'action':'submit','probe_id':data['probe_id'],'signature':'state_mismatch','reasoning':'observed'}
                else:value={'action':'probe','hypothesis':'transition','probe':{'value':2}}
                return Completion(content=json.dumps(value),model='fixture')
        identity={'model':{'id':'fixture'},'source_manifest':{str(Path(__file__).resolve()):hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}}
        model=Student([]);suite=Suite()
        results=[]
        for _ in range(4):
            ledger=self.ledger(per_launch=1,max_attempts=4)
            results.append(resume_local_episode(ledger,model,suite,episode_id='e1',identity=identity))
            ledger.close()
        self.assertEqual([r['status'] for r in results],['checkpointed']*3+['completed'])
        self.assertTrue(results[-1]['episode']['verified']);self.assertEqual(model.calls,4)
        ledger=self.ledger(per_launch=1,max_attempts=4)
        replay=resume_local_episode(ledger,model,suite,episode_id='e1',identity=identity)
        self.assertEqual(replay['new_api_attempts'],0);self.assertEqual(replay['replayed_completions'],4)
        self.assertEqual(model.calls,4);ledger.close()

    def test_live_side_effect_target_cannot_be_replayed(self):
        with self.assertRaises(ValueError):resume_local_episode(None,None,FakeSuite(),episode_id='x',identity={})


class SignalTests(unittest.TestCase):
    def test_transport_failures_do_not_become_learning_failures(self):
        result=signals({'condition':'taught','error':'ProviderError'},{'model_calls':[{'status':'error','response':{'diagnostic':'Provider HTTP 429 after 5 attempt(s)'}}]},evaluation_scope='same')
        self.assertEqual(result['transport']['rate_limited_calls'],1)
        self.assertIsNone(result['construction']['valid_fraction'])
        self.assertFalse(result['eligible_for_learning_comparison'])
        self.assertIsNone(result['transport']['actual_http_attempts'])

    def test_invalid_input_recovery_and_false_completion_are_separate(self):
        episode={'observations':{'0':{'result':{'status':'error','measurements':{'reason':'invalid bounded probe','receipts':[]}}},
                                 '1':{'result':{'status':'violation','measurements':{'final_state_correct':True,'trajectory_correct':False,'false_completion':True}}}}}
        result=signals(episode,{'model_calls':[{'status':'done'}]})
        self.assertEqual(result['construction']['feedback_recovery']['fraction'],1)
        self.assertEqual(result['evidence']['final_correct_but_trajectory_wrong'],1)
        self.assertFalse(result['evidence']['verified_novel_discovery'])

    def test_claim_alone_never_qualifies_and_changed_scope_cannot_compare(self):
        a=signals({'verified':True},{'model_calls':[{'status':'done'}]},evaluation_scope='a')
        b=signals({}, {'model_calls':[{'status':'done'}]},evaluation_scope='b')
        self.assertTrue(a['evidence']['unsupported_verified_claim']);self.assertFalse(compare(a,b)['comparable'])
        self.assertFalse(compare(a,a)['causal_gain_established'])


if __name__=='__main__': unittest.main()
