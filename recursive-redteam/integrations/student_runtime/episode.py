"""Resume the existing student runner over deterministic local experiments."""
import hashlib
from pathlib import Path

from recursive_redteam import teaching
from .checkpoint import Deferred


def resume_local_episode(ledger, model, suite, *, episode_id, identity,
                         condition='taught', max_steps=7):
    """One bounded burst. Return waiting/checkpointed instead of restarting calls.

    identity must bind model configuration and transitive target source hashes.
    Sources are checked on every invocation. The caller may resume after retry_at;
    per-launch and persistent budget exhaustion require an explicit later launch
    or a new authorized budget, respectively. This function never schedules work.
    """
    public = suite.describe('holdout')
    if public.get('execution_kind') != 'deterministic_local_recreation':
        raise ValueError('episode replay requires a deterministic fresh-state local target')
    if condition not in ('baseline', 'taught'):
        raise ValueError('unknown condition')
    if not identity.get('model') or not identity.get('source_manifest'):
        raise ValueError('model configuration and transitive source manifest required')
    for path, expected in identity['source_manifest'].items():
        if not Path(path).is_absolute() or hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected:
            raise ValueError('target source identity does not match saved manifest')
    lesson = suite.lesson() if condition == 'taught' else None
    binding = {**identity, 'condition':condition, 'lesson':lesson, 'public':public, 'max_steps':max_steps,
               'repair_invalid_worker':True,
               'runner_sha256':hashlib.sha256(Path(teaching.__file__).read_bytes()).hexdigest(),
               'adapter_sources': {name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                   for name in ('checkpoint.py','episode.py')}}
    models = [ledger.model(model, episode=episode_id, role=role, identity=binding)
              for role in ('student_planner', 'student_worker', 'student_reviewer')]
    before = ledger.started
    try:
        episode = teaching.TeachingRunner(*models,max_steps=max_steps,repair_invalid_worker=True).episode(suite,lesson=lesson,condition=condition)
        result = {'status':'completed', 'episode':episode}
    except Deferred as exc:
        result = {'status':'checkpointed', 'reason':exc.reason, 'retry_at':exc.retry_at,
                  'learning_outcome':'not_assessed'}
    result.update(new_api_attempts=ledger.started-before, replayed_completions=sum(m.replayed for m in models),
                  budget=ledger.report(), fresh_independent_sample=False,
                  note='Continuation of one named episode. A cached replay does not create a new sample.')
    return result
