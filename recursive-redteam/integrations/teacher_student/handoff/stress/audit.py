"""Independent post-run audit: saved model outputs plus fresh local replays.

Loads only the run's frozen suite/runtime, never whatever the main thread later
edits. This program performs zero model or network calls.
"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    root = args.run.resolve()
    snapshot = root / "source_snapshot"
    sys.path.insert(0, str(snapshot))
    from integrations.teacher_student.handoff.stress.suite import StressSuite
    from recursive_redteam.teaching import digest, verify_probe

    report = json.loads((root / "report.json").read_text())
    journal = json.loads((root / "model-journal.json").read_text())
    manifest = json.loads((root / "source-manifest.json").read_text())
    plan = json.loads((root / "predeclared-plan.json").read_text())
    checks = []
    def check(label, passed):
        checks.append({"check": label, "passed": bool(passed)})
    check("campaign_completed", report["status"] == "completed")
    check("all_predeclared_cases_retained", len(report["cases"]) == len(plan["cases"]))
    check("source_manifest_intact", all(hashlib.sha256((snapshot / name).read_bytes()).hexdigest() == sha for name, sha in manifest.items()))
    check("original_teacher_verified", report["teacher"]["verified"])
    calls = {c["id"]: c for c in journal["model_calls"]}
    check("all_completed_calls_are_real_requested_model", all(c.get("actual_model") == "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B" for c in calls.values() if c["status"] == "done"))
    check("provider_is_live", journal["metadata"]["model_student"]["provider"]["simulated"] is False)
    check("call_accounting_partitions_journal", sorted(i for case in report["cases"] for i in case["call_ids"]) == sorted(calls))
    summaries, reruns = [], 0
    for index, case in enumerate(report["cases"]):
        name, episode = case["id"], case["episode"]
        check(name + ":predeclared_assignment", {k: case[k] for k in ("variant", "repaired", "lesson")} == plan["cases"][index])
        suite = StressSuite(case["variant"], case["repaired"])
        case_calls = [calls[i] for i in case["call_ids"]]
        planner = next(c for c in case_calls if c["role"] == "student_planner")
        initial = planner["request"]["messages"]
        context = json.loads(initial[1]["content"])
        check(name + ":fresh_public_only_planner_context", len(initial) == 2 and context["task"] == suite.describe("holdout") and context["lesson"] == (suite.lesson() if case["lesson"] else None))
        check(name + ":no_initial_holdout_answers", not any(x in json.dumps(initial) for x in ("u11", "v22", "w33", "x44", "y55")))
        workers = [c for c in case_calls if c["role"] == "student_worker"]
        raw_actions = []
        for call in workers:
            if call["status"] == "done":
                try:
                    raw_actions.append(json.loads(call["response"]["content"]))
                except ValueError:
                    pass
        if "error" not in episode:
            check(name + ":recorded_actions_are_raw_model_outputs", raw_actions == episode.get("actions"))
        if workers:
            first = workers[0]["request"]["messages"]
            ctx = json.loads(first[1]["content"])
            check(name + ":fresh_delegated_worker", len(first) == 2 and ctx["task"] == context["task"] and ctx["lesson"] == context["lesson"] and ctx["max_actions"] == plan["max_steps"])
            if "plan" in episode:
                check(name + ":delegation_matches_planner", ctx["delegation"] == episode["plan"])
        observed = deepcopy(episode.get("observations", {}))
        recovered = {}
        # A transport exception can discard the runner's in-memory episode
        # object. Recover only measurements actually returned by the host in
        # saved requests, not merely model-proposed or newly replayed inputs.
        for call in workers:
            messages = call["request"]["messages"]
            for pos, msg in enumerate(messages):
                if pos == 0 or msg["role"] != "user" or messages[pos - 1]["role"] != "assistant":
                    continue
                try:
                    feedback = json.loads(msg["content"])
                    action = json.loads(messages[pos - 1]["content"])
                except (ValueError, TypeError):
                    continue
                if "probe_id" in feedback and "result" in feedback and action.get("action") == "probe":
                    recovered[feedback["probe_id"]] = {"probe": action["probe"], "result": feedback["result"]}
        if "error" not in episode:
            check(name + ":feedback_matches_episode_measurements", all(observed.get(k) == v for k, v in recovered.items()))
        else:
            observed.update(recovered)
        statuses = {key: 0 for key in ("passed", "violation", "error")}
        for probe_id, observation in observed.items():
            p, saved = observation["probe"], observation["result"]
            check(probe_id + ":model_authored", any(a.get("action") == "probe" and a.get("probe") == p for a in raw_actions))
            fresh = suite.evaluate(deepcopy(p), split="holdout")
            reruns += 1
            check(probe_id + ":fresh_measurement_matches", fresh == saved)
            statuses[saved["status"]] += 1
            if not case["repaired"] and saved["status"] == "violation":
                fixed = StressSuite(case["variant"], True).evaluate(deepcopy(p), split="holdout")
                reruns += 1
                check(probe_id + ":disappears_on_real_repair", fixed["status"] == "passed")
        if "replay" in episode:
            replay = episode["replay"]
            fresh = verify_probe(suite, replay["probe"], split="holdout", prefix=episode["claim"]["probe_id"], expected_signature=episode["claim"]["signature"])
            reruns += 3
            check(name + ":two_replays_and_control_match", replay == fresh)
            check(name + ":control_only_removes_diagnostics", replay["control"] == {**replay["probe"], "diagnostics": {}})
            check(name + ":substantive_novelty", suite.novelty(replay["probe"])["valid"] and digest(replay["probe"]) not in {digest(p) for p in suite.teacher_probes()})
            reviewers = [c for c in case_calls if c["role"] == "student_reviewer" and c["status"] == "done"]
            check(name + ":review_is_actual_model_output", len(reviewers) == 1 and json.loads(reviewers[0]["response"]["content"]) == episode["model_review"])
            ids = [t["id"] for t in replay["trials"]]
            accepted = episode["model_review"].get("verdict") == "verified" and sorted(episode["model_review"].get("evidence_trial_ids", [])) == sorted(ids)
            check(name + ":review_citations_match", accepted == episode["model_review_accepted"])
            check(name + ":success_label_supported", episode["verified"] == (fresh["verified"] and accepted and suite.novelty(replay["probe"])["valid"] and not episode["copied_training_probe"]))
        else:
            check(name + ":no_success_without_host_replay", episode["verified"] is False)
        submits = [a for a in raw_actions if a.get("action") == "submit"]
        unsupported = [a for a in submits if a.get("probe_id") not in observed or observed[a["probe_id"]]["result"]["status"] != "violation" or a.get("signature") != observed[a["probe_id"]]["result"].get("signature")]
        summaries.append({"id": name, "variant": case["variant"], "repaired": case["repaired"], "lesson": case["lesson"],
                          "verified": episode["verified"], "error": episode.get("error"), "probe_statuses": statuses,
                          "valid_measured_probes": statuses["passed"] + statuses["violation"],
                          "observations_recovered_from_journal": len(recovered) if "error" in episode else 0,
                          "raw_probe_actions": sum(a.get("action") == "probe" for a in raw_actions),
                          "unique_generated_probes": len({digest(a.get("probe")) for a in raw_actions if a.get("action") == "probe"}),
                          "raw_submissions": len(submits), "unsupported_submissions": len(unsupported),
                          "rejected_novelty_submissions": len(episode.get("rejected_submissions", [])),
                          "logical_calls": len(case_calls), "fresh_student_histories": True})
    groups = {}
    for repaired in (False, True):
        for lesson in (False, True):
            key = ("repaired" if repaired else "vulnerable") + ("_taught" if lesson else "_baseline")
            rows = [c for c in summaries if c["repaired"] == repaired and c["lesson"] == lesson]
            groups[key] = {"episodes": len(rows), "verified_findings": sum(c["verified"] for c in rows),
                           "episode_errors": sum(c["error"] is not None for c in rows),
                           "episodes_with_two_valid_probes": sum(c["valid_measured_probes"] >= 2 for c in rows),
                           "unsupported_submissions": sum(c["unsupported_submissions"] for c in rows)}
    audit_source = Path(__file__).read_bytes()
    (root / "audit-source.py").write_bytes(audit_source)
    output = {"audit_passed": all(c["passed"] for c in checks), "audit_source_sha256": hashlib.sha256(audit_source).hexdigest(),
              "checks": checks, "cases": summaries, "groups": groups,
              "fresh_local_reruns": reruns, "new_model_calls_in_audit": 0,
              "model_usage": report["model_usage"], "actual_http_attempts_including_catalogue": report["actual_http_attempts_including_catalogue"],
              "limits": ["Synthetic new transition holdouts around one frozen, known local defect family.",
                         "Three pairs per backend are insufficient to establish a population teaching effect.",
                         "A clean bounded search is not proof that a backend is universally bug-free.",
                         "Model identity is provider-reported; no immutable hosted weight revision is available."]}
    (root / "independent-audit.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: output[k] for k in ("audit_passed", "groups", "fresh_local_reruns", "model_usage")}, ensure_ascii=False, indent=2))
    if not output["audit_passed"]:
        print(json.dumps([c for c in checks if not c["passed"]]))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
