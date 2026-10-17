"""Run a predeclared, blinded real Nemotron student stress campaign.

Launch this file from an immutable source_snapshot prepared by the operator.
Every episode gets fresh conversation history, with no previous discoveries.
"""
import argparse
import hashlib
import json
from pathlib import Path
import random
import sys
import time

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from recursive_redteam.budget import BudgetedModel, ModelGate
from recursive_redteam.cli import prepare_model, write_json
from recursive_redteam.config import load_config
from recursive_redteam.credentials import load_credentials
from recursive_redteam.store import Store
from recursive_redteam.teaching import TeachingRunner, verify_teacher
from integrations.teacher_student.handoff.suite import HandoffSuite
from integrations.teacher_student.handoff.stress.suite import StressSuite, VARIANTS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--case-plan", type=Path, help="Optional predeclared transport-retry subset; contains no student discoveries")
    parser.add_argument("--interval", type=float, default=8)
    parser.add_argument("--retries", type=int, default=2)
    args = parser.parse_args()
    if not 8 <= args.interval <= 60 or not 0 <= args.retries <= 5:
        raise ValueError("Pacing and retry bounds invalid")
    out = args.out.resolve()
    if (out / "state.sqlite").exists():
        raise ValueError("Never overwrite a previous live run")
    manifest = json.loads((out / "source-manifest.json").read_text())
    if any(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != sha for name, sha in manifest.items()):
        raise ValueError("Snapshot mismatch before inference")
    config = load_config(ROOT / "examples/crusoe-validation.json")
    config["planner"].update(max_tokens=2048, temperature=0.2, max_retries=args.retries)
    config["request_interval_seconds"] = args.interval
    write_json(out / "config.json", config)
    plan = [{"variant": variant, "repaired": repaired, "lesson": lesson}
            for variant in VARIANTS for repaired in (False, True) for lesson in (False, True)]
    random.Random(20261017).shuffle(plan)
    if args.case_plan:
        plan = json.loads(args.case_plan.read_text())
        if not isinstance(plan, list) or not 1 <= len(plan) <= 12:
            raise ValueError("Invalid bounded retry plan")
        for case in plan:
            if set(case) != {"variant", "repaired", "lesson"} or type(case["lesson"]) is not bool:
                raise ValueError("Invalid retry assignment")
            StressSuite(case["variant"], case["repaired"])
    write_json(out / "predeclared-plan.json", {"cases": plan, "max_steps": 10, "max_calls": 180,
        "fresh_histories": True, "equal_episode_budget": True, "lesson_frozen": True,
        "backend_blinded_to_student": True,
        "meaning": "Near-transfer of one known local bug family to new synthetic transitions; not novel root-cause discovery or a powered causal learning study."})
    report = {"status": "running", "started": time.time(), "cases": [], "live_student": True,
              "teacher": verify_teacher(HandoffSuite())}
    if not report["teacher"]["verified"]:
        raise ValueError("Original teacher evidence failed verification")
    store = Store(out / "state.sqlite")
    model = None
    try:
        load_credentials(args.env_file, config)
        model = prepare_model(config["planner"], "student", store)
        gate = ModelGate(args.interval)
        wrappers = [BudgetedModel(model, store, 180, role, gate=gate)
                    for role in ("student_planner", "student_worker", "student_reviewer")]
        runner = TeachingRunner(*wrappers, max_steps=10)
        for index, case in enumerate(plan):
            suite = StressSuite(case["variant"], case["repaired"])
            case_id = f"case-{index:02d}"
            before = store.report()["model_usage"]["logical_calls"]
            started = time.time()
            print(json.dumps({"case": case_id, "phase": "started", **case}), flush=True)
            try:
                episode = runner.episode(suite, lesson=suite.lesson() if case["lesson"] else None, condition=case_id)
            except Exception as exc:
                episode = {"condition": case_id, "verified": False, "error": type(exc).__name__}
            after = store.report()["model_usage"]["logical_calls"]
            record = {"id": case_id, **case, "call_ids": list(range(before + 1, after + 1)),
                      "started": started, "finished": time.time(), "episode": episode}
            report["cases"].append(record)
            write_json(out / (case_id + ".json"), record)
            write_json(out / "report.json", report)
            write_json(out / "model-journal.json", store.report())
            print(json.dumps({"case": case_id, "verified": episode["verified"],
                              "probes": len(episode.get("observations", {})), "error": episode.get("error")}), flush=True)
        report["status"] = "completed"
    finally:
        report["finished"] = time.time()
        report["sources_unchanged"] = all(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == sha for name, sha in manifest.items())
        journal = store.report()
        report["model_usage"] = journal["model_usage"]
        report["actual_http_attempts_including_catalogue"] = model.request_count if model else 0
        report["actual_models"] = sorted({c["actual_model"] for c in journal["model_calls"] if c.get("actual_model")})
        write_json(out / "model-journal.json", journal)
        write_json(out / "report.json", report)
        store.close()


if __name__ == "__main__":
    main()
