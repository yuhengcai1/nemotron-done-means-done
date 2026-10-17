"""Frozen, resumable student runner. No human intervention inside a campaign.

Entry: python examples/teach.py --student-resume --out RUN --env-file FILE
Continue the same RUN with --resume; budgets and the lifetime deadline persist.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import time

from recursive_redteam.cli import make_model, write_json
from recursive_redteam.credentials import load_credentials
from recursive_redteam.teaching import digest
from .checkpoint import CheckpointLedger
from .episode import resume_local_episode
from .signals import signals

ROOT=Path(__file__).resolve().parents[2]


def snapshot_intact(run):
    manifest=json.loads((run/'source-manifest.json').read_text())
    return all(hashlib.sha256((run/'frozen'/p).read_bytes()).hexdigest()==sha for p,sha in manifest.items())


class RecordedSuite:
    """Persist local measurements before the next API request can fail."""
    def __init__(self, suite, path):
        self.suite,self.path=suite,path
        self.records=json.loads(path.read_text()) if path.exists() else {}
    def __getattr__(self,name):return getattr(self.suite,name)
    def evaluate(self,probe,*,split):
        result=self.suite.evaluate(probe,split=split)
        key=digest({'split':split,'probe':probe})
        old=self.records.get(key)
        if old and old['result']!=result:
            raise ValueError('deterministic target changed during episode replay')
        self.records[key]={'probe':probe,'split':split,'result':result,
                           'local_evaluations':1+(old or {}).get('local_evaluations',0)}
        write_json(self.path,self.records)
        return result


def partial_episode(journal, records, condition):
    """Recover only actual stored measurements, not imaginary executed probes."""
    actions=[];observations={}
    for call in journal['model_calls']:
        if call['role']!='student_worker' or call['status']!='done':continue
        try: action=json.loads(call['response']['content'])
        except (ValueError,KeyError):continue
        if not isinstance(action,dict):
            actions.append({'error':'invalid_worker_json'})
            continue
        actions.append(action)
        if action.get('action')=='probe':
            key=digest({'split':'holdout','probe':action.get('probe')})
            if key in records:
                observations[key]={'probe':records[key]['probe'],'result':records[key]['result']}
    return {'condition':condition,'verified':False,'error':'IncompleteEpisode',
            'actions':actions,'observations':observations}


def drive(run, model, suite, *, clock=time.time, sleep=time.sleep):
    """Resume a single student-owned episode until completion or a durable stop."""
    config=json.loads((run/'config.json').read_text())
    protocol=json.loads((run/'protocol.json').read_text())
    manifest=json.loads((run/'source-manifest.json').read_text())
    identity={'model':config['planner'],
              'source_manifest':{str(run/'frozen'/p):sha for p,sha in manifest.items()}}
    recorded=RecordedSuite(suite,run/'probe-observations.json')
    last={'status':'checkpointed','reason':'not_started'}
    while True:
        if not snapshot_intact(run):raise ValueError('frozen sources changed')
        ledger=CheckpointLedger(run/'student.sqlite',max_attempts=protocol['max_attempts'],
                                per_launch=protocol['burst_attempts'],min_interval=protocol['min_interval'],
                                error_cooldown=protocol['error_cooldown'],deadline=protocol['deadline_epoch'],clock=clock)
        try:
            if clock()>=protocol['deadline_epoch']:
                last={'status':'checkpointed','reason':'campaign_wall_budget_exhausted'}
            else:
                try:
                    last=resume_local_episode(ledger,model,recorded,episode_id=protocol['episode_id'],
                                              identity=identity,condition=protocol['condition'],max_steps=protocol['max_steps'])
                except (ValueError,TypeError,KeyError) as exc:
                    last={'status':'checkpointed','reason':'runner_contract_error','error_type':type(exc).__name__}
            journal=ledger.journal()
            completed=[c for c in journal['model_calls'] if c['status']=='done']
            identity_ok=all(c['actual_model']==config['planner']['model'] for c in completed) if completed else None
            if identity_ok is False:last={'status':'checkpointed','reason':'provider_model_identity_mismatch'}
            episode=last.get('episode') or partial_episode(journal,recorded.records,protocol['condition'])
            last.update(budget=ledger.report(),model_identity_verified=identity_ok,
                        sources_intact=True, human_interventions=0,
                        independent_review_required=bool(episode.get('verified')))
            scope=digest({'sources':manifest,'model':config['planner'],'max_steps':protocol['max_steps'],
                          'max_attempts':protocol['max_attempts']})
            signal=signals(episode,journal,evaluation_scope=scope)
            teacher_signatures={suite.evaluate(p,split='train').get('signature') for p in suite.teacher_probes()}
            signature=episode.get('replay',{}).get('signature')
            last['same_teacher_family']=signature in teacher_signatures if signature else None
            last['teaching_gain_established']=False
            write_json(run/'model-journal.json',journal)
            write_json(run/'episode.json',episode)
            write_json(run/'signals.json',signal)
            write_json(run/'report.json',last)
            event={'at':clock(),'status':last['status'],'reason':last.get('reason'),
                   'attempts':journal['model_usage']['actual_http_attempts'],
                   'replayed_completions':last.get('replayed_completions',0),
                   'new_api_attempts':last.get('new_api_attempts',0)}
            with (run/'events.jsonl').open('a') as handle:
                handle.write(json.dumps(event)+'\n');handle.flush();os.fsync(handle.fileno())
            print(json.dumps(event),flush=True)
        finally:ledger.close()
        if last['status']=='completed':return last
        reason=last.get('reason')
        if reason not in ('transport_cooldown','rate_limit_checkpointed','per_launch_attempt_budget_exhausted'):
            return last
        until=last.get('retry_at') or last['budget']['next_allowed']
        # A burst does not create a new allowance. All attempts remain in SQLite.
        while clock()<until and clock()<protocol['deadline_epoch']:
            sleep(min(30,until-clock(),protocol['deadline_epoch']-clock()))


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--resume',action='store_true')
    p.add_argument('--env-file',type=Path);p.add_argument('--config',type=Path,default=ROOT/'examples/crusoe-validation.json')
    p.add_argument('--suite',choices=['dynamic'],default='dynamic')
    p.add_argument('--max-attempts',type=int,default=24);p.add_argument('--max-steps',type=int,default=12)
    p.add_argument('--wall-seconds',type=int,default=900)
    p.add_argument('--frozen-entry',action='store_true',help=argparse.SUPPRESS)
    a=p.parse_args(argv);run=a.out.resolve()
    if not a.frozen_entry:
        if a.resume:
            if not snapshot_intact(run):raise ValueError('saved snapshot is damaged')
        else:
            if not 1<=a.max_attempts<=100 or not 2<=a.max_steps<=12 or not 60<=a.wall_seconds<=3600:
                p.error('invalid bounded campaign budget')
            run.mkdir(parents=True,exist_ok=False)
            config=json.loads(a.config.read_text())
            config['planner'].update(max_retries=0,max_tokens=2048,temperature=0.2,timeout=60)
            write_json(run/'config.json',config)
            directories=['recursive_redteam','integrations/student_runtime','integrations/teacher_student/dynamic','integrations/dynamic_state']
            paths=[ROOT/'examples/teach.py',ROOT/'examples/audit_teaching.py']
            for directory in directories:
                paths.extend(f for f in (ROOT/directory).rglob('*') if f.suffix in ('.py','.json') and f.name not in ('STATUS.json','validation.json'))
            manifest={}
            for source in sorted(set(paths)):
                relative=source.relative_to(ROOT);destination=run/'frozen'/relative
                destination.parent.mkdir(parents=True,exist_ok=True);destination.write_bytes(source.read_bytes())
                manifest[str(relative)]=hashlib.sha256(destination.read_bytes()).hexdigest()
            write_json(run/'source-manifest.json',manifest)
            write_json(run/'protocol.json',{'version':1,'suite':a.suite,'episode_id':'autonomous-dynamic-1',
                'condition':'taught','max_attempts':a.max_attempts,'max_steps':a.max_steps,
                'burst_attempts':3,'min_interval':15,'error_cooldown':60,
                'deadline_epoch':time.time()+a.wall_seconds,'wall_seconds':a.wall_seconds,
                'target':'deterministic local replica','student':'live Nemotron','human_interventions_allowed':False,
                'objective':'Student independently repairs invalid probes from public feedback and submits reproducible findings.',
                'scope':'No source-code editing tools are exposed to the student.',
                'resume_semantics':'Same episode, source snapshot, conversation and persistent budget. No automatic budget reset.'})
        command=[sys.executable,str(run/'frozen/examples/teach.py'),'--student-resume','--frozen-entry','--resume','--out',str(run)]
        if a.env_file:command+=['--env-file',str(a.env_file.resolve())]
        os.execv(sys.executable,command)
    if not snapshot_intact(run):raise ValueError('frozen sources changed')
    from integrations.teacher_student.dynamic.suite import build_suite
    with (run/'runner.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        old=json.loads((run/'report.json').read_text()) if (run/'report.json').exists() else {}
        if old.get('status')=='completed':
            print(json.dumps({'status':'already_completed','new_api_attempts':0}),flush=True);return 0
        config=json.loads((run/'config.json').read_text())
        load_credentials(a.env_file,config)
        # No unmetered catalogue request. Every completion is budgeted, and the
        # actual returned model ID is checked before continuing the campaign.
        model=make_model(config['planner'],'student')
        result=drive(run,model,build_suite())
        return 0 if result['status']=='completed' else 2
