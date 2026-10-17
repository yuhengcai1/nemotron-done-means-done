"""Reproduce teacher evidence and exercise the student protocol entirely offline."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from integrations.teacher_student.handoff.suite import HERE, SOURCE_SHA256, build_suite


def ref(path):
    return {"path": str(path.relative_to(ROOT)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def verify_teacher():
    suite = build_suite()
    rows = []
    for probe in suite.teacher_probes():
        repeats = [suite.evaluate(probe, split="train") for _ in range(2)]
        control_probe = suite.control(probe, split="train")
        control = suite.evaluate(control_probe, split="train")
        guarded = build_suite(guarded=True).evaluate(probe, split="train")
        verified = (all(r["status"] == "violation" for r in repeats)
                    and repeats[0]["signature"] == repeats[1]["signature"]
                    and control["status"] == guarded["status"] == "passed")
        rows.append({"probe": probe, "verified": verified, "replays": repeats,
                     "negative_control_probe": control_probe, "negative_control": control,
                     "owned_guarded_adapter": guarded})
    # Verify that the originally discovered source still matches, independently
    # of the frozen copy. Never silently call a changed source the same defect.
    current_source = ROOT / "examples/reasoning_handoff_target.py"
    current_matches = hashlib.sha256(current_source.read_bytes()).hexdigest() == SOURCE_SHA256
    current_checks = []
    if current_matches:
        from examples.reasoning_handoff_target import World, judge
        for _ in range(2):
            world = World(False)
            world.call("read_state", {})
            world.call("stage_manifest", {"revision": 1, "contact_ids": ["c1", "c3"]})
            text = json.dumps({"status": "done", "revision": 1, "contact_ids": ["c1", "c2"]})
            actual = judge(world, text, metrics={"task_state_correct": True})
            negative = judge(world, text)
            current_checks.append({"actual_staged_ids": list(world.staged["contact_ids"]),
                                   "with_collision": actual.status, "without_collision": negative.status})
    sources = [current_source, HERE / "frozen/reasoning_handoff_target.py", HERE / "suite.py",
               ROOT / "recursive_redteam/contracts.py", HERE / "lesson.json"]
    return {"schema_version": 1, "classification": "current_real_code_defect" if current_matches else "frozen_real_code_regression",
            "verified": all(r["verified"] for r in rows), "new_live_calls": 0,
            "current_source_matches_frozen": current_matches, "current_source_reproductions": current_checks,
            "invariant": "Diagnostic metadata must not change a judge's decision when authoritative execution state and final handoff are unchanged.",
            "teacher": {"kind": "Codex source inspection plus deterministic host execution", "model_id": "not_exposed", "model_checkpoint_hash": None},
            "target": {"kind": "local_python_judge_api", "model_invoked": False, "model_checkpoint_hash": None},
            "scope_limit": "Direct judge API collision; normal plugin metadata currently does not contain these reserved fields. No remote or Nemotron failure was established.",
            "sources": [ref(p) for p in sources], "training_cases": rows,
            "repro_command": "PYTHONDONTWRITEBYTECODE=1 python3 integrations/teacher_student/handoff/verify.py --out /tmp/handoff-teacher-verification.json"}


def offline_student_smoke():
    """A scripted exploration fixture, never called a live student or learning result."""
    suite = build_suite()
    public = suite.describe("holdout")
    # Exercise a later action after the notified source change. The diagnostic
    # search uses schema-declared fields, without reading private oracle labels.
    base = {"actions": [{"op": "read_state"},
                        {"op": "stage", "selection": "eligible", "revision": "current"},
                        {"op": "stage", "selection": "eligible", "revision": "current"}],
            "handoff": {"encoding": "strict_json", "selection": "eligible", "revision": "current"},
            "diagnostics": {}}
    keys = [key for key, spec in public["probe_schema"]["properties"]["diagnostics"]["properties"].items()
            if spec["type"] == "boolean"]
    attempts = []
    found = None
    for diagnostics in [{}] + [{key: True} for key in keys]:
        probe = dict(base, diagnostics=diagnostics)
        result = suite.evaluate(probe, split="holdout")
        attempts.append({"probe": probe, "result": result})
        if result["status"] == "violation":
            found = suite.verify_student(probe)
            break
    return {"classification": "offline_student_action_fixture", "student_model_called": False,
            "live_student_validated": False, "new_live_calls": 0,
            "attempts": attempts, "host_validation": found,
            "interpretation": "Exercises discovery/submission/replay plumbing; it does not measure Nemotron learning or discover a new model failure."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=HERE / "verification.json")
    parser.add_argument("--freeze", action="store_true", help="Write immutable teacher evidence once; refuse overwrite")
    args = parser.parse_args()
    teacher = verify_teacher()
    student = offline_student_smoke()
    result = {"teacher": teacher, "student_protocol": student, "new_live_calls": 0}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    if args.freeze:
        with (HERE / "frozen-evidence.json").open("x") as handle:
            handle.write(json.dumps(teacher, indent=2) + "\n")
    print(json.dumps({"teacher_verified": teacher["verified"],
                      "current_source_matches": teacher["current_source_matches_frozen"],
                      "offline_student_probe_verified": bool(student["host_validation"] and student["host_validation"]["verified"]),
                      "live_student_validated": False, "new_live_calls": 0, "output": str(args.out)}))
    if not teacher["verified"] or not student["host_validation"] or not student["host_validation"]["verified"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
