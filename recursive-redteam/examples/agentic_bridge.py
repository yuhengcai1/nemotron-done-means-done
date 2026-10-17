"""Bridge the adjacent synthetic contact-export experiment to protocol v1.

This file is an adapter, not a campaign launcher. The parent runner supplies one
bounded case per process. Running it with real credentials invokes the existing
OpenCode target; development tests do not do so.
"""
from contextlib import redirect_stdout
import argparse
import hashlib
import json
from pathlib import Path
import sys


# Import only our owned adapter; never load credentials for offline inspection.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from integrations.dynamic_state.audit import assess_artifact, source_ref
from integrations.dynamic_state.contract import parse_candidate
from recursive_redteam.contracts import Candidate


def convert_verdict(verdict, output, *, origin="historical"):
    return assess_artifact(output, verdict, origin=origin)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--live", action="store_true", help="explicitly invoke external OpenCode; outside gateway")
    parser.add_argument("--inspect-artifact", type=Path, help="offline historical audit, never a protocol trial")
    parser.add_argument("--out-root", type=Path)
    parser.add_argument("--model-record", type=Path)
    args = parser.parse_args()
    if args.inspect_artifact:
        print(json.dumps({"classification": "external_observation", "new_model_calls": 0,
                          "assessment": assess_artifact(args.inspect_artifact)}, ensure_ascii=False))
        return
    if not args.live or not all((args.source_root, args.out_root, args.model_record)):
        raise ValueError("live execution requires explicit opt-in and runtime paths")
    request = json.load(sys.stdin)
    if request.get("protocol_version") != 1 or not isinstance(request.get("trial_id"), str):
        raise ValueError("invalid adapter protocol")
    sources = [args.source_root / name for name in ('arena.py', 'runtime.py')]
    before = [source_ref(path) for path in sources]
    sys.path.insert(0, str(args.source_root.resolve()))
    from runtime import MODEL, run_target
    context = request.get("candidate") or request.get("control_of")
    candidate = Candidate(**context) if context else None
    spec = parse_candidate(candidate)
    record = json.loads(args.model_record.read_text())
    if isinstance(record.get("model"), dict):
        record = record["model"]
    if record.get("id") != MODEL or type(record.get("context_length")) is not int:
        raise ValueError("provide the exact existing runtime model catalogue record")
    output = args.out_root.resolve() / hashlib.sha256(request["trial_id"].encode()).hexdigest()[:24]
    with redirect_stdout(sys.stderr):
        verdict = run_target(spec, request.get("candidate") is not None, output, record)
    after = [source_ref(path) for path in sources]
    provenance = {'before': before, 'after': after, 'adapter': source_ref(__file__),
                  'model_record': source_ref(args.model_record), 'code_unchanged_during_run': before == after}
    (output / 'adapter-provenance.json').write_text(json.dumps(provenance, indent=2) + '\n')
    if before != after:
        raise ValueError('external runtime sources changed during execution')
    print(json.dumps(convert_verdict(verdict, output, origin="live_external_run"), ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(json.dumps({"status": "error", "error": "existing target adapter failed; inspect local runtime setup"}))
