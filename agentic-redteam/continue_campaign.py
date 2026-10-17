"""Finish a deliberately selected interrupted campaign without overwriting evidence.

Two independent clean replays use separate native runtime databases and worlds.
The timeout is increased to 600s; prompts, model and world specification stay fixed.
"""
import concurrent.futures
import json
import os
from pathlib import Path

from arena import validate_spec
from campaign import compact, learning, propose
from runtime import dump, run_target


if __name__ == '__main__':
    root = Path('runs/campaign-02')
    for line in Path('/Users/caiyuheng/Documents/ChatGPT/hackson/nemotron-redteam/.env').read_text().splitlines():
        if line.startswith('CRUSOE_API_KEY='):
            os.environ['CRUSOE_API_KEY'] = line.split('=', 1)[1].strip()
    record = json.loads((root / 'environment.json').read_text())['model']
    drift = root / 'drift-0'
    spec = json.loads((drift / 'proposal.json').read_text())['spec']
    result = {name: compact(json.loads((drift / name / 'verdict.json').read_text()))
              for name in ('control', 'attack')}
    result['replays'] = []
    dump(root / 'continuation.json', {
        'reason': 'Previous campaign supervisor interrupted; preserve recovered replay-0 separately.',
        'clean_replays': ['drift-0/replay-1', 'drift-0/replay-2'],
        'timeout_seconds': 600, 'max_concurrent_target_runs': 2})
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        pending = {pool.submit(run_target, spec, True, drift / f'replay-{i}', record,
                               timeout=600): i for i in (1, 2)}
        for future in concurrent.futures.as_completed(pending):
            trial = future.result()
            result['replays'].append({'replay': pending[future], **compact(trial)})
            dump(drift / 'summary.json', result)
            print('CLEAN_REPLAY', pending[future], json.dumps(compact(trial)), flush=True)
    smaller = validate_spec(dict(spec, contacts=max(6, spec['contacts']//2), updates=1))
    reduced = run_target(smaller, True, drift / 'reduced', record, timeout=600)
    result['reduced'] = compact(reduced)
    dump(drift / 'summary.json', result)
    print('REDUCED', json.dumps(compact(reduced)), flush=True)
    out = root / 'learning-0'
    out.mkdir(exist_ok=False)
    learning_spec = propose('learning', [result], out, 101)
    print('LEARNING_SPEC', json.dumps(learning_spec), flush=True)
    learned = learning(learning_spec, out, record)
    dump(out / 'summary.json', learned)
    dump(root / 'campaign.json', [
        json.loads((root / 'handoff-0/summary.json').read_text()), result, learned])
    print('LEARNING_RESULT', json.dumps(learned), flush=True)
