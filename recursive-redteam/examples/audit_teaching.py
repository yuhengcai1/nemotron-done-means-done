"""Read-only audit of teaching receipts, snapshots and actual model transcripts."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from recursive_redteam.teaching import digest


def reproduced(item):
    trials = item.get("trials", [])
    return (len(trials) == 3 and all(t["result"]["status"] == "violation" for t in trials[:2])
            and bool(item.get("signature"))
            and all(t["result"].get("signature") == item["signature"] for t in trials[:2])
            and trials[-1]["result"]["status"] == "passed"
            and digest(item.get("probe")) != digest(item.get("control")))


def audit(root):
    report = json.loads((root / "report.json").read_text())
    journal = json.loads((root / "model-journal.json").read_text())
    config = json.loads((root / "config.json").read_text())
    calls = [c for c in journal["model_calls"] if c["status"] == "done"]
    outputs = []
    plans = []
    reviews = []
    for call in calls:
        try:
            output = json.loads(call["response"]["content"])
        except (KeyError, TypeError, ValueError):
            continue
        outputs.append(output)
        if call["role"] == "student_planner":
            context = json.loads(call["request"]["messages"][-1]["content"])
            plans.append((output, context))
        if call["role"] == "student_reviewer":
            context = json.loads(call["request"]["messages"][-1]["content"])
            reviews.append((output, context))
    suites = {}
    for name, result in report["suites"].items():
        identity = json.loads((root / name / "identity.json").read_text())
        lesson = json.loads((root / name / "lesson.json").read_text())
        checks = {
            "scope_matches_identity": digest(identity) == result["scope"],
            "frozen_sources_intact": all(
                hashlib.sha256((root / name / "source_snapshot" / path).read_bytes()).hexdigest() == sha
                for path, sha in identity["source_manifest"].items()),
            "sources_unchanged_during_run": result["sources_unchanged"],
            "lesson_hash_matches": digest(lesson) == result["teacher"]["lesson_hash"],
            "teacher_independently_reproduced": bool(result["teacher"]["discoveries"])
                and all(reproduced(item) for item in result["teacher"]["discoveries"]),
        }
        teacher_confirmed = checks["teacher_independently_reproduced"]
        checks["teacher_status_consistent"] = result["teacher"]["verified"] == teacher_confirmed
        taught = next((e for e in result.get("episodes", []) if e.get("condition") == "taught"), {})
        baseline = next((e for e in result.get("episodes", []) if e.get("condition") == "baseline"), {})
        baseline_comparable = (bool(baseline.get("plan")) and bool(baseline.get("observations"))
                               and not baseline.get("error"))
        teacher_signatures = {d.get("signature") for d in result["teacher"]["discoveries"]}
        supported_transfer = (teacher_confirmed and taught.get("verified") is True
                              and reproduced(taught.get("replay", {}))
                              and taught.get("replay", {}).get("signature") in teacher_signatures
                              and taught.get("novelty", {}).get("valid") is True
                              and taught.get("copied_training_probe") is False)
        checks["transfer_status_supported"] = not result["transfer_verified"] or supported_transfer
        for episode in result.get("episodes", []):
            if "plan" not in episode:
                continue
            condition = episode["condition"]
            expected_lesson = None if condition == "baseline" else lesson
            checks[condition + "_model_delegation_and_lesson"] = any(
                output == episode["plan"] and context.get("lesson") == expected_lesson for output, context in plans)
            if episode["verified"]:
                replay = episode["replay"]
                checks[condition + "_reproduced"] = reproduced(replay)
                checks[condition + "_model_authored_probe_and_claim"] = (episode["claim"] in outputs and any(
                    o.get("action") == "probe" and o.get("probe") == replay["probe"] for o in outputs))
                checks[condition + "_separate_model_review"] = any(
                    output == episode["model_review"] and context.get("evidence") == replay for output, context in reviews)
                checks[condition + "_novelty"] = (episode["novelty"].get("valid") is True
                                                   and not episode["copied_training_probe"])
                checks[condition + "_citations"] = set(episode["model_review"]["evidence_trial_ids"]) == {
                    t["id"] for t in replay["trials"]}
                checks[condition + "_model_verdict"] = (episode["model_review"].get("verdict") == "verified"
                                                       and bool(episode["model_review"].get("reasoning")))
        suites[name] = {"evidence_valid": all(checks.values()), "checks": checks,
                        "teacher_verified": result["teacher"]["verified"],
                        "transfer_verified": result["transfer_verified"],
                        "baseline_verified": result.get("baseline_verified"),
                        "baseline_comparable": baseline_comparable,
                        "paired_outcome_improved": baseline_comparable and result.get("taught_verified") is True
                        and result.get("baseline_verified") is False,
                        "causal_effect_established": False}
    live = (report["live_student"] and bool(calls)
            and all(c["actual_model"] == config["planner"]["model"] for c in calls))
    return {"evidence_valid": bool(suites) and all(s["evidence_valid"] for s in suites.values()),
            "live_student_identity_verified": live, "suites": suites,
            "logical_calls": journal["model_usage"]["logical_calls"],
            "astra_verified_delivery": False,
            "interpretation": "Evidence validity and successful transfer are separate. One paired run does not estimate a causal teaching effect."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    result = audit(args.run)
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        args.out.write_text(text)
    print(text)
