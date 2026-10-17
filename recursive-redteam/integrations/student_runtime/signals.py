"""Local signals with explicit denominators; no API calls and no model grading."""
from collections import Counter
import json
from pathlib import Path
import argparse
from recursive_redteam.teaching import digest


def signals(episode, journal, *, evaluation_scope=None):
    calls = journal.get('model_calls', [])
    observations = list(episode.get('observations', {}).values())
    statuses = Counter(o['result']['status'] for o in observations)
    valid = [o for o in observations if o['result']['status'] in ('passed','violation')]
    errors = [o for o in observations if o['result']['status']=='error']
    invalid = [o for o in errors if not o['result'].get('measurements',{}).get('receipts') and
               any(s in o['result'].get('measurements',{}).get('reason','').lower()
                   for s in ('invalid','novelty','unsupported'))]
    transitions = [(a,b) for a,b in zip(observations, observations[1:]) if a['result']['status']=='error']
    recovered = sum(b['result']['status'] in ('passed','violation') for _,b in transitions)
    trials = episode.get('replay',{}).get('trials',[])
    signature = episode.get('replay',{}).get('signature')
    replay_valid = (len(trials)==3 and bool(signature)
                    and all(t.get('result',{}).get('status')=='violation'
                            and t['result'].get('signature')==signature for t in trials[:2])
                    and trials[2].get('result',{}).get('status')=='passed')
    replay = episode.get('replay',{})
    review = episode.get('model_review',{})
    ids = [t.get('id') for t in trials]
    review_valid = (len(ids)==3 and all(isinstance(x,str) for x in ids) and len(set(ids))==3
                    and review.get('verdict')=='verified' and bool(review.get('reasoning'))
                    and sorted(review.get('evidence_trial_ids',[]))==sorted(ids))
    verified = (replay_valid and review_valid and isinstance(replay.get('probe'),dict)
                and isinstance(replay.get('control'),dict) and digest(replay['probe'])!=digest(replay['control'])
                and episode.get('novelty',{}).get('valid') is True
                and episode.get('copied_training_probe') is False
                and episode.get('model_review_accepted') is True and episode.get('verified') is True)
    error_calls = [c for c in calls if c.get('status') in ('error','rejected')]
    usage = journal.get('model_usage', {})
    complete = not episode.get('error') and bool(calls) and all(c.get('status')=='done' for c in calls)
    return {
        'evaluation_scope':evaluation_scope, 'condition':episode.get('condition'),
        'transport':{'logical_calls':len(calls),'completed_calls':sum(c.get('status')=='done' for c in calls),
                     'error_calls':len(error_calls),'unknown_calls':sum(c.get('status') in ('reserved','pending','unknown') for c in calls),
                     'rate_limited_calls':sum('HTTP 429' in (c.get('response') or {}).get('diagnostic','') for c in error_calls),
                     'actual_http_attempts':usage.get('actual_http_attempts'), 'reported_tokens':usage.get('reported_tokens',{}),
                     'unknown_usage_calls':usage.get('unknown_usage_calls'),
                     'note':('Actual attempts are metered separately; cached replies consume no requests.'
                             if usage.get('actual_http_attempts') is not None else
                             'Successful calls may have hidden provider retries; logical calls are not HTTP attempts.')},
        'construction':{'unique_proposals':len(observations),'valid_experiments':len(valid),
                        'invalid_inputs':len(invalid),'other_incomplete_experiments':len(errors)-len(invalid),
                        'valid_fraction':len(valid)/len(observations) if observations else None,
                        'feedback_recovery':{'recovered':recovered,'opportunities_observed':len(transitions),
                                             'fraction':recovered/len(transitions) if transitions else None}},
        'evidence':{'verified_novel_discovery':bool(verified),'claimed_verified':episode.get('verified') is True,
                    'unsupported_verified_claim':episode.get('verified') is True and not verified,
                    'final_correct_but_trajectory_wrong':sum(
                        o['result'].get('measurements',{}).get('final_state_correct') is True and
                        o['result']['measurements'].get('trajectory_correct') is False for o in observations),
                    'teacher_or_target_false_completion':sum(
                        o['result'].get('measurements',{}).get('false_completion') is True for o in observations)},
        'episode_complete':complete, 'eligible_for_learning_comparison':complete and bool(evaluation_scope),
        'retention':{'assessed':False,'reason':'Requires repeated identical held-out anchors with independent evidence across versions.'},
        'interpretation':'Transport errors are not student failures. Invalid probes are not target defects. Missing evidence is unknown, not zero ability.'}


def compare(before, after):
    if (not before.get('evaluation_scope') or before['evaluation_scope']!=after.get('evaluation_scope')
        or not before['eligible_for_learning_comparison'] or not after['eligible_for_learning_comparison']):
        return {'comparable':False,'reason':'Incomplete episode or changed/unknown model, target, runner or budget scope.'}
    return {'comparable':True,'descriptive_discovery_delta':int(after['evidence']['verified_novel_discovery'])-
            int(before['evidence']['verified_novel_discovery']), 'causal_gain_established':False,
            'retention_established':False,'reason':'Matched scope supports a descriptive contrast; repeated matched anchors are needed for retention.'}


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--episode',type=Path,required=True);p.add_argument('--journal',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--scope')
    a=p.parse_args()
    a.out.write_text(json.dumps(signals(json.loads(a.episode.read_text()),json.loads(a.journal.read_text()),
                                      evaluation_scope=a.scope),ensure_ascii=False,indent=2)+'\n')
