"""Run operator-installed teacher suites against a real Crusoe student model."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from recursive_redteam.budget import BudgetedModel, ModelGate
from recursive_redteam.cli import prepare_model, write_json
from recursive_redteam.config import load_config
from recursive_redteam.credentials import load_credentials
from recursive_redteam.store import Store
from recursive_redteam.teaching import TeachingMemory, TeachingRunner, digest, verify_teacher

SUITES = {name: f"integrations.teacher_student.{name}.suite" for name in
          ("root", "codex", "hermes", "handoff", "dynamic")}


def sources(module):
    # The registry is operator-maintained. Include each course's transitive local
    # dependencies without invalidating it when an unrelated teacher writes files.
    paths = list((ROOT / "recursive_redteam").glob("*.py"))
    paths += [ROOT / "examples/teach.py", ROOT / "examples/audit_teaching.py"]
    directories = [Path(module.__file__).parent]
    if module.__name__ == SUITES["dynamic"]:
        directories.append(ROOT / "integrations/dynamic_state")
    for directory in directories:
        paths += list(directory.rglob("*.py"))
        paths += [path for path in directory.rglob("*.json")
                  if path.name not in {"STATUS.json", "validation.json"}]
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(set(paths))}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", action="append", choices=SUITES, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "examples/crusoe-validation.json")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--memory", type=Path, default=ROOT / "runs/teaching-memory.sqlite")
    parser.add_argument("--offline", action="store_true", help="verify teachers only, without any model calls")
    parser.add_argument("--max-steps", type=int, default=5)
    parser.add_argument("--max-calls", type=int, default=80)
    parser.add_argument("--force-retest", action="store_true", help="bypass fresh, scoped transfer memory")
    args = parser.parse_args(argv)
    if not 1 <= args.max_calls <= 500:
        parser.error("max-calls must be in 1..500")
    args.out.mkdir(parents=True, exist_ok=False)
    config = load_config(args.config)
    # Student explanations/probes need more room than the small campaign planner.
    config["planner"]["max_tokens"] = 2048
    config["planner"]["temperature"] = 0.2
    write_json(args.out / "config.json", config)
    store = Store(args.out / "state.sqlite")
    memory = TeachingMemory(args.memory)
    result = {"schema_version": 1, "status": "running", "live_student": not args.offline,
              "teacher_roles": {"codex": "actual Codex collaboration teacher",
                                "astra": "review interrupted by content filter; no verified Astra delivery"},
              "suites": {}, "max_model_calls": args.max_calls,
              "meaning": "Local bounded reliability curriculum; hosted student inference only. No model weight training."}
    try:
        runner = None
        for name in dict.fromkeys(args.suite):
            module = importlib.import_module(SUITES[name])
            suite = module.build_suite()
            manifest = sources(module)
            identity = {"suite": name, "source_manifest": manifest, "lesson_hash": digest(suite.lesson()),
                        "model": config["planner"], "max_steps": args.max_steps, "verification": "two-replays-control-v1"}
            scope = digest(identity)
            suite_root = args.out / name
            suite_root.mkdir()
            write_json(suite_root / "identity.json", identity)
            write_json(suite_root / "lesson.json", suite.lesson())
            for relative in manifest:
                destination = suite_root / "source_snapshot" / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes((ROOT / relative).read_bytes())
            print(json.dumps({"suite": name, "phase": "teacher_verification"}), flush=True)
            previous = None if args.force_retest or args.offline else memory.qualified(scope, identity["lesson_hash"])
            if args.offline:
                report = {"teacher": verify_teacher(suite), "live_student": False,
                          "transfer_verified": False, "status": "teachers_only"}
            elif previous is not None:
                report = {**previous["body"], "status": "memory_reused", "fresh_inference": False,
                          "memory_event_hash": previous["event_hash"], "verified_at": previous["created"]}
            else:
                if runner is None:
                    load_credentials(args.env_file, config)
                    model = prepare_model(config["planner"], "student", store)
                    gate = ModelGate(config.get("request_interval_seconds", 8))
                    wrappers = [BudgetedModel(model, store, args.max_calls, role, gate=gate)
                                for role in ("student_planner", "student_worker", "student_reviewer")]
                    runner = TeachingRunner(*wrappers, max_steps=args.max_steps)
                report = runner.run(suite, memory=memory, scope=scope, record_qualification=False)
                calls = store.report()["model_calls"]
                identities_match = all(c.get("actual_model") == config["planner"]["model"]
                                       for c in calls if c.get("status") == "done")
                report["student_model_identity_verified"] = identities_match
                if not identities_match:
                    report["transfer_verified"] = False
                report["fresh_inference"] = True
            unchanged = sources(module) == manifest
            report.update({"scope": scope, "sources_unchanged": unchanged})
            if not unchanged:
                report["transfer_verified"] = False
            if not args.offline and previous is None:
                report.setdefault("lesson_hash", identity["lesson_hash"])
                kind = "transfer_verified" if report["transfer_verified"] else "transfer_inconclusive"
                if not unchanged or report.get("student_model_identity_verified") is False:
                    kind = "transfer_not_verified"
                memory.append(scope, kind, report)
            write_json(suite_root / "report.json", report)
            result["suites"][name] = report
            write_json(args.out / "report.json", result)
            print(json.dumps({"suite": name, "teacher_verified": report["teacher"]["verified"],
                              "transfer_verified": report["transfer_verified"]}), flush=True)
        result["status"] = "completed"
        result["memory_chain_valid"] = bool(memory.events()) if not args.offline else True
    except BaseException as exc:
        result["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "error"
        result["error"] = type(exc).__name__
        raise
    finally:
        journal = store.report()
        result["model_usage"] = journal["model_usage"]
        result["actual_student_models"] = sorted({c["actual_model"] for c in journal["model_calls"] if c.get("actual_model")})
        write_json(args.out / "model-journal.json", journal)
        write_json(args.out / "report.json", result)
        memory.close()
        store.close()
    return 0


if __name__ == "__main__":
    if '--student-resume' in sys.argv[1:]:
        from integrations.student_runtime.cli import main as student_main
        raise SystemExit(student_main([a for a in sys.argv[1:] if a != '--student-resume']))
    raise SystemExit(main())
