"""Prepare deck facts only from a qualified repair, fresh verification and memory recall.

This does not run inference, edit the framework or export a presentation. Call it
only after the lead agent has selected the verified run and final UI screenshot.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PRESENTATION = ROOT / "demo/presentation"


def load(path):
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict):
        raise ValueError(f"Expected an object: {path}")
    return value


def relative(path):
    return str(Path(path).resolve().relative_to(ROOT))


def passing(check):
    return (isinstance(check, dict) and check.get("total", 0) > 0
            and check.get("failed") == 0 and check.get("passed") == check.get("total"))


def require(condition, description):
    if not condition:
        raise ValueError(description)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--verification", type=Path, required=True)
    parser.add_argument("--recall", type=Path, required=True)
    parser.add_argument("--screenshot", type=Path, required=True)
    parser.add_argument("--fresh-recall", action="store_true", help="Use only after the actual local recall button is browser-verified")
    parser.add_argument("--history", type=Path, action="append", default=[])
    parser.add_argument("--out", type=Path, default=PRESENTATION / "facts.next.json")
    args = parser.parse_args()
    run = args.run.resolve()
    report_path = run / "report.json"
    report, local, recall = load(report_path), load(args.verification), load(args.recall)
    verification, memory = report.get("verification", {}), report.get("memory", {})
    before, after = report.get("before", {}).get("tests", {}), report.get("after", {}).get("tests", {})
    require(report.get("run_id") == run.name, "Run identity mismatch")
    require(report.get("status") == "verified" and verification.get("verified") is True,
            "The selected run has no qualified repair")
    require(verification.get("model_identity_verified") is True, "Model identity is unverified")
    require(report.get("teacher_in_runtime") is False, "Runtime teacher absence is unverified")
    require(before.get("failed", 0) > 0 and passing(after), "Before/after tests do not establish repair")
    replay_checks = verification.get("replays", [])
    require(len(replay_checks) >= 2 and all(passing(x) for x in replay_checks), "Missing passing verification repetitions")
    review = verification.get("model_review", {})
    require(review.get("verdict") == "verified" and
            sorted(review.get("evidence_ids", [])) == sorted(verification.get("evidence_ids", [])) and
            len(verification.get("evidence_ids", [])) >= 2,
            "The model review does not cite the actual verification receipts")
    require(local.get("verified") is True and local.get("source_hashes_valid") is True
            and local.get("hosted_model_calls") == 0, "Fresh independent verification is missing")
    require(len(local.get("replays", [])) >= 2 and all(passing(x) for x in local["replays"]),
            "Fresh independent checks are incomplete")
    require(memory.get("stored") is True and recall.get("run_id") == run.name
            and recall.get("matched") is True and recall.get("reusable") is True
            and recall.get("chain_valid") is True and recall.get("hosted_model_calls") == 0,
            "Verified zero-inference memory recall is missing")
    require(recall.get("entry_id") == memory.get("entry_id")
            and recall.get("event_hash") == memory.get("event_hash"), "Recall references a different memory entry")
    require(len(recall.get("checks", [])) >= 2 and all(passing(x) for x in recall["checks"]),
            "Recalled repair did not pass fresh checks")
    for name, digest in report.get("artifact_hashes", {}).items():
        require(hashlib.sha256((run / name).read_bytes()).hexdigest() == digest,
                f"Retained artifact changed: {name}")
    require(args.screenshot.is_file(), "Final actual UI screenshot is missing")
    calls = report.get("model_usage", {}).get("logical_calls")
    require(type(calls) is int and calls > 0, "Actual logical-call count is missing")
    require(report.get("patch", {}).get("author") == report.get("model"), "Patch author is not the recorded model")
    require(report.get("patch", {}).get("diff"), "The run contains no model-authored source change")
    actions = {event.get("action", {}).get("action") for event in report.get("events", [])
               if isinstance(event, dict) and isinstance(event.get("action"), dict)}
    require({"read_source", "replace_text", "run_tests"} <= actions, "The actual worker action trace is incomplete")
    inspected = "source and tests" if "read_tests" in actions else "source and test receipts"
    history_paths = args.history or [ROOT / "runs/nemotron-self-debug-20261017/report.json",
                                    ROOT / "runs/nemotron-self-debug-20261017-v2/report.json"]
    history = []
    for path in history_paths:
        if path.resolve() == report_path:
            continue
        prior = load(path)
        history.append(f"{prior.get('run_id')}: {prior.get('status')}; "
                       f"{prior.get('model_usage', {}).get('logical_calls')} logical calls; "
                       f"{prior.get('after', {}).get('tests', {}).get('failed')} checks failing. "
                       f"Source: {relative(path)}.")
    failed, total = before["failed"], after["total"]
    memory_demo = (
        "[Select “04 Memory recall”, then click “Recall again, now”.]\n"
        "Now we retrieve the repair from durable memory. The framework checks source and test identities, then runs the recalled code through fresh checks. The receipt shows zero model calls. Recall requires matching identities and valid receipts. The evidence stays attached to the exact repair."
        if args.fresh_recall else
        "[Select “04 Memory recall”.]\n"
        "Here is the recorded memory recall. It retrieved the repair from durable memory, checked source and test identities, then ran the recalled code through fresh checks. The receipt shows zero model calls. Recall requires matching identities and valid receipts. The evidence stays attached to the exact repair."
    )
    script = {
        "opening": "Recursive Lab is a framework that can test and repair itself, with evidence it can reuse. Nemotron plans the work and writes the repair. Independent execution decides what the framework can trust.",
        "problem": f"Our historical evaluator recorded a violation, then a later tool error overwrote the result's status. {failed} checks exposed this defect. We give Nemotron the source and fixed acceptance tests. The model must write the repair itself.",
        "demo": "[Switch to the app's Stage view, with the first story beat selected.]\n"
                f"This is a replay of real Crusoe inference. A Nemotron planner delegates to a repair worker. The worker inspects {inspected}, chooses an edit, and uses execution feedback to decide its next step. Earlier unsuccessful attempts remain in the history.\n\n"
                "[Select “02 Nemotron’s patch”.]\n"
                "Here is the model-authored change. The exact patch stays attached to this run. The revised finalizer preserves the failure status and keeps the diagnostic error. The runner applies that edit to an isolated source copy. The model cannot rewrite the tests to make the patch pass.\n\n"
                "[Select “03 Independent checks”, then click “Verify again, now”.]\n"
                f"All {total} checks now pass. The checks also cover ordinary passing cases and unrelated errors. The repair must preserve those behaviors while fixing the defect. Fresh processes repeat the checks, and a separate Nemotron reviewer cites the actual verification receipts. This button verifies the retained repair again without a new inference call.\n\n"
                + memory_demo,
        "results": f"[Return to slide 4.]\nThe repair reduces failing checks from {failed} to zero. This run used {calls} logical model calls. Independent verification and memory recall both pass with no additional inference. We demonstrated one bounded repair of our own historical framework, starting from Codex-authored code.",
        "close": "Today, verified memory reuses a repair only when source identities and fresh checks agree. Next, we want more modules and retrieval of similar failure patterns, with held-out validation. Later, a persistent maintenance queue could produce isolated changes for review."
    }
    facts = {
        "complete": True, "verified": True, "screenshot": relative(args.screenshot),
        "cover_subtitle": "A framework that tests\nand repairs itself",
        "cover_provider": "Nemotron on Crusoe Managed Inference",
        "result_source": relative(report_path), "target_source": relative(run / "before/targets.py"),
        "memory_source": "examples/recall_repair.py", "additional_sources": [relative(args.verification), relative(args.recall),
            relative(run / "model-journal.json"), relative(run / "repair_checks.py"), *[relative(p) for p in history_paths]],
        "metrics": [{"value": str(failed), "label": "failing checks before"},
                    {"value": "0", "label": "failing checks after"},
                    {"value": "0", "label": "model calls for recall"}],
        "result_title": "Nemotron repaired the evaluator",
        "outcome": f"{total}/{total} checks pass in two fresh verification runs.",
        "scope": f"{calls} logical calls. One historical local revision. Initial framework authored with Codex.",
        "replay_caption": "Real Crusoe inference, followed by fresh local verification and memory recall.",
        "provenance_notes": "Selected verified run: " + run.name + ". " + " ".join(history) +
            " Failed attempts remain in the record. Prior failures and provider errors do not establish a general capability limit. "
            "This result is bounded to one historical evaluator function and the retained checks. Initial framework and tests were authored with Codex. "
            "No teacher lesson or repair is supplied at runtime. Planner, worker and reviewer roles are host-defined, not dynamically invented. "
            "The demonstrated memory reuse matches the exact original source and checker, not a new failure pattern. "
            "Proposed next: repair more framework modules and retrieve similar failure patterns, with every resulting candidate revalidated against held-out tests. "
            "Proposed later: a persistent maintenance queue that creates isolated reviewable changes. These are roadmap items, not demonstrated capabilities.",
        "roadmap": [
            {"when": "Now", "status": "Implemented", "text": "One repaired function with verified memory recall"},
            {"when": "Next", "status": "Proposed", "text": "More modules and similar failure patterns"},
            {"when": "Later", "status": "Proposed", "text": "Persistent queue with reviewable changes"}
        ],
        "roadmap_sources": ["examples/self_debug.py", "examples/recall_repair.py", "DESIGN.md"],
        "timing": {"opening": "00:00–00:15", "problem": "00:15–00:35", "demo": "00:35–02:10", "results": "02:10–02:35", "close": "02:35–03:00"},
        "presenter_setup": "Open http://127.0.0.1:8765/?stage=1 before the talk and select the first story beat. The story replays recorded Crusoe inference. “Verify again, now” executes fresh local tests without a model call. " +
            ("“Recall again, now” retrieves the repair from SQLite and runs fresh checks without a model call." if args.fresh_recall else
             "“04 Memory recall” displays the saved actual recall receipt and does not start another recall."),
        "script": script,
        "verified_inputs": {"report": relative(report_path), "local_verification": relative(args.verification),
                            "memory_recall": relative(args.recall), "new_inference_calls_for_preparation": 0,
                            "sha256": {relative(path): hashlib.sha256(path.read_bytes()).hexdigest()
                                       for path in (report_path, args.verification, args.recall, args.screenshot)}}
    }
    args.out.write_text(json.dumps(facts, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"prepared": str(args.out), "run_id": run.name, "verified": True}))


if __name__ == "__main__":
    main()
