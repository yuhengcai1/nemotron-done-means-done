"""Read-only local projection of a real autonomous run. Never starts inference."""
import argparse
import json
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = r'''<!doctype html><meta charset="utf-8"><title>Nemotron · Live evidence</title>
<style>body{background:#f6f4ec;color:#173e32;font:20px system-ui;margin:42px}h1{font-size:44px;margin-bottom:6px}.sub{color:#637168}main{display:grid;grid-template-columns:1fr 1fr;gap:24px}section{background:white;border:1px solid #d9dfd5;padding:24px;border-radius:12px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:16px monospace;max-height:390px;overflow:auto}.result{margin:24px 0;padding:20px;background:#e5ebdf}small{font-size:15px}h2{font-size:24px}.badge{float:right;font-size:18px}footer{margin-top:24px;font-size:16px}</style>
<span class="badge" id="phase">READING RECEIPTS</span><h1>Nemotron · Live evidence</h1>
<p class="sub" id="subtitle">Crusoe inference → native OpenCode delegation → independent verification</p>
<div class="result" id="result">Waiting for an independent verdict. No success assumed.</div>
<main><section><h2>1. Model decisions</h2><small>Actual tool selections from the controller</small><pre id="actions"></pre></section>
<section><h2>2. Native tool events</h2><small>Completed / reported tool events, not simulated animation</small><pre id="events"></pre></section></main>
<section style="margin-top:24px"><h2>3. Independent outcome</h2><pre id="verdict">Not available yet</pre></section>
<footer id="run"></footer><footer>Live means a new API run. A passed task is not evidence of self-improvement.<br>Refresh reads saved receipts only. It never makes model calls.</footer>
<script>async function refresh(){try{const d=await(await fetch('/state',{cache:'no-store'})).json();if(d.repair)document.getElementById('subtitle').textContent='Crusoe inference → real source investigation → model-authored patch → independent tests';document.getElementById('phase').textContent=d.report?'RUN ENDED':'OBSERVING RUN';document.getElementById('actions').textContent=d.actions.join('\n\n')||'Waiting for model response';document.getElementById('events').textContent=d.events.join('\n')||'Waiting for native tool events';document.getElementById('run').textContent='Run: '+d.run+' | Last refresh: '+new Date().toLocaleTimeString();document.getElementById('verdict').textContent=JSON.stringify(d.verdicts,null,2);document.getElementById('result').textContent=d.repair ? (d.repair.status === 'running' ? 'Real code investigation running. No repair accepted yet.' : d.repair.mechanical_repair_evidence ? 'New test fails before and passes after. Independent causal review required.' : 'Repair not verified. Inspect retained evidence.') : d.verdicts.length?d.verdicts.map(v=>v.verified_task_passed?'Verified task completed':v.timed_out?'Timed out — completion NOT verified':'Task completion NOT verified').join(' / '):'Waiting for an independent verdict. No success assumed.';}catch(e){document.getElementById('phase').textContent='RECEIPTS UNAVAILABLE';}}refresh();setInterval(refresh,1500);</script>'''


def read(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def state(root):
    actions, events, verdicts = [], [], []
    for path in sorted((root / 'actions').glob('*.json')):
        action = read(path)
        if action:
            tool = action['tool']
            actions.append(tool['name'] + ' [' + action['status'] + ']\n' + tool['arguments'])
    for task in ([root] if (root / 'repair-report.json').exists() else sorted(root.glob('task-*'))):
        path = task / 'events.jsonl'
        if path.exists():
            for line in path.read_text().splitlines():
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                part = item.get('part', {})
                if item.get('type') == 'tool_use':
                    tool_state = part.get('state', {})
                    role = tool_state.get('input', {}).get('subagent_type')
                    inputs = tool_state.get('input', {})
                    detail = inputs.get('command') or inputs.get('filePath') or ''
                    if inputs.get('filePath'):
                        detail = Path(detail).name
                    events.append(part.get('tool', '?') + (' → '+role if role else '') + ' [' + tool_state.get('status', '?') + '] ' + str(detail)[:240])
        verdict = read(task / 'verdict.json')
        if verdict:
            verdicts.append({k: verdict.get(k) for k in ('verified_task_passed','declared_status','native_multiagent_observed','native_roles_verified','timed_out','violations','seconds')})
    repair = read(root / 'repair-report.json')
    if repair:
        actions = ['OPEN-ENDED CODE REPAIR', 'No preselected defect or supplied patch.', 'Model chooses what to inspect, reproduce and fix.']
        verdicts = [{k: v for k, v in repair.items() if not isinstance(v, dict)}]
    return dict(run=root.name, actions=actions, events=events[-30:], verdicts=verdicts, repair=repair, report=(repair if repair and repair.get('status') != 'running' else read(root/'report.json')))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args()
    root = args.run.resolve()
    if not root.is_dir():
        parser.error('run directory must exist')

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ('/', '/state'):
                self.send_error(404)
                return
            data = PAGE.encode() if self.path == '/' else json.dumps(state(root)).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8' if self.path == '/' else 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    print('http://127.0.0.1:'+str(args.port), flush=True)
    ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()


if __name__ == '__main__':
    main()
