#!/usr/bin/env python3
"""Read-only, payload-free audit of a report JSON or run state SQLite snapshot.

Usage: python examples/audit_run.py runs/example/report.json
This checks recorded evidence, not provider authenticity or general model safety.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3


ROLES = ("coordinator", "worker", "target", "verifier")
MARKERS = {"COORDINATOR": "coordinator", "ATTACKER": "worker", "VERIFIER": "verifier"}


def object_json(value):
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    value = value.strip()
    if value.startswith("```") and value.endswith("```"):
        value = "\n".join(value.splitlines()[1:-1])
    try:
        decoded = json.loads(value)
    except (ValueError, TypeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def identifier(value):
    """Only emit short machine identifiers, never arbitrary source text."""
    if value is None:
        return None
    if type(value) is int:
        return value
    if (isinstance(value, str) and len(value) <= 200
            and re.fullmatch(r"[A-Za-z0-9_./:+-]+", value)
            and "INTERNAL_CANARY_" not in value):
        return value
    return "redacted"


def load_report(path):
    path = Path(path).resolve()
    if path.suffix not in {".sqlite", ".sqlite3", ".db"}:
        with path.open(encoding="utf-8") as handle:
            report = json.load(handle)
        if not isinstance(report, dict):
            raise ValueError("report must be a JSON object")
        return report
    # Do not instantiate Store: its constructor can migrate or create tables.
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        metadata = {row["key"]: json.loads(row["value"])
                    for row in db.execute("SELECT key,value FROM metadata")}
        calls = [dict(row) for row in db.execute("SELECT * FROM model_calls ORDER BY id")]
        for call in calls:
            for key in ("request", "response", "usage"):
                call[key] = object_json(call.get(key))
        tasks = [dict(json.loads(row["data"]), status=row["status"], round=row["round"])
                 for row in db.execute("SELECT * FROM tasks ORDER BY rowid")]
        trials = [dict(row) for row in db.execute("SELECT * FROM trials ORDER BY rowid")]
        for trial in trials:
            trial["result"] = object_json(trial.get("result"))
        events = [dict(row) for row in db.execute("SELECT * FROM events ORDER BY id")]
        for event in events:
            event["data"] = object_json(event.get("data"))
        return {"status": metadata.get("status", "created"), "metadata": metadata,
                "simulated": metadata.get("target", {}).get("simulated", False),
                "tasks": tasks, "trials": trials, "events": events, "model_calls": calls}


def request_role(call):
    for message in object_json(call.get("request")).get("messages", []):
        if message.get("role") == "system" and isinstance(message.get("content"), str):
            match = re.search(r"(?:^|\s)ROLE:(COORDINATOR|ATTACKER|VERIFIER)(?=\s|$)", message["content"])
            if match:
                return MARKERS[match[1]]
    return "target" if call.get("role") == "target" else None


def user_context(call, action):
    for message in object_json(call.get("request")).get("messages", []):
        value = object_json(message.get("content")) if message.get("role") == "user" else {}
        if value.get("action") == action:
            return value
    return {}


def _numeric_mapping(value):
    return {identifier(key): number for key, number in object_json(value).items()
            if type(number) is int and number >= 0}


def audit_report(report):
    metadata = object_json(report.get("metadata"))
    config = object_json(metadata.get("config"))
    model_config = {"coordinator": object_json(config.get("planner")),
                    "worker": object_json(config.get("planner")),
                    "target": object_json(object_json(config.get("target")).get("model")),
                    "verifier": object_json(config.get("verifier"))}
    calls = report.get("model_calls", [])
    done = [call for call in calls if call.get("status") == "done"]
    roles = {role: sum(call.get("role") == role for call in done) for role in ROLES}
    tasks = report.get("tasks", [])
    task_map = {task["id"]: task for task in tasks if isinstance(task.get("id"), str)}
    trials = {trial["id"]: trial for trial in report.get("trials", []) if isinstance(trial.get("id"), str)}
    events = report.get("events", [])

    bad_identity, bad_request = [], []
    returned_models = set()
    for call in done:
        response = object_json(call.get("response"))
        actual, expected = call.get("actual_model"), model_config.get(call.get("role"), {}).get("model")
        if actual:
            returned_models.add(str(identifier(actual)))
        if not (expected and actual == expected == response.get("model")):
            bad_identity.append(identifier(call.get("id")))
        request = object_json(call.get("request"))
        if not request.get("messages") or request_role(call) != call.get("role"):
            bad_request.append(identifier(call.get("id")))

    assignments = []
    for task in tasks:
        plan_calls, worker_calls = [], []
        for call in done:
            if call.get("role") == "coordinator":
                proposals = object_json(object_json(call.get("response")).get("content")).get("tasks", [])
                if isinstance(proposals, list) and any(isinstance(proposal, dict) and all(
                    proposal.get(key) == task.get(key) for key in ("family", "hypothesis", "worker_id", "parent_id"))
                    for proposal in proposals):
                    plan_calls.append(identifier(call.get("id")))
            if call.get("role") == "worker":
                supplied = object_json(user_context(call, "propose").get("task"))
                if supplied.get("id") == task.get("id") and all(
                        supplied.get(key) == task.get(key) for key in ("worker_id", "parent_id", "family")):
                    worker_calls.append(identifier(call.get("id")))
        assignments.append({"task_id": identifier(task.get("id")), "worker_id": identifier(task.get("worker_id")),
                            "parent_id": identifier(task.get("parent_id")),
                            "model_assigned": task.get("delegation_source") == "model" and bool(plan_calls),
                            "coordinator_call_ids": plan_calls, "worker_call_ids": worker_calls,
                            "outcome": identifier(task.get("outcome"))})
    edges = [{"parent_id": item["parent_id"], "child_id": item["task_id"],
              "recorded_parent_exists": task.get("parent_id") in task_map,
              "model_chose_parent": item["model_assigned"]}
             for task, item in zip(tasks, assignments) if task.get("parent_id")]

    choices = []
    for event in events:
        if event.get("kind") != "verification_action":
            continue
        data = object_json(event.get("data"))
        arguments, result = object_json(data.get("arguments")), object_json(data.get("result"))
        source_calls = []
        for call in done:
            if call.get("role") != "verifier":
                continue
            context = user_context(call, "verify")
            if object_json(context.get("candidate")).get("id") != data.get("candidate_id"):
                continue
            for choice in object_json(call.get("response")).get("tool_calls", []):
                function = object_json(choice.get("function"))
                if function.get("name") == data.get("tool") and object_json(function.get("arguments")) == arguments:
                    source_calls.append(identifier(call.get("id")))
        citations = arguments.get("evidence_trial_ids", [])
        citations = citations if isinstance(citations, list) else []
        valid_citations = all(isinstance(cite, str) and cite in trials and trials[cite].get("status") == "done"
                              for cite in citations)
        if arguments.get("verdict") in {"reproduced", "no_violation_observed"}:
            candidate_id = data.get("candidate_id")
            valid_citations = (valid_citations and isinstance(candidate_id, str)
                               and candidate_id + ":initial" in citations
                               and all(cite.startswith(candidate_id + ":") for cite in citations))
        accepted = result.get("accepted") is True if data.get("tool") == "submit_review" else "error" not in result
        choices.append({"candidate_id": identifier(data.get("candidate_id")), "tool": identifier(data.get("tool")),
                        "verifier_call_ids": source_calls, "accepted": accepted,
                        "trial_id": identifier(result.get("trial_id")),
                        "verdict": identifier(arguments.get("verdict")),
                        "evidence_trial_ids": [identifier(cite) for cite in citations],
                        "citations_exist": valid_citations})

    coverage = []
    for trial_id, trial in trials.items():
        if not (trial_id.endswith(":initial") or ":replay:" in trial_id):
            continue
        result = object_json(trial.get("result"))
        metrics = object_json(result.get("metrics"))
        coverage.append({"trial_id": identifier(trial_id), "status": identifier(result.get("status")),
                         "injection_reached": metrics.get("injection_reached") is True,
                         "business_task_completed": metrics.get("business_task_completed") is True})
    accepted_reviews = [choice for choice in choices if choice["tool"] == "submit_review" and choice["accepted"]]
    evaluated = [task for task in tasks if task.get("outcome") in {"no_violation", "reproduced", "inconclusive"}]
    reviewed = {choice["candidate_id"] for choice in accepted_reviews if choice["verifier_call_ids"] and choice["citations_exist"]}
    checks = {
        "queue_completed": report.get("status") == "completed",
        "live_models_configured": all(value.get("kind") == "openai-compatible" for value in model_config.values())
                                  and report.get("simulated") is False,
        "all_roles_completed_calls": all(roles.values()),
        "recorded_model_identities_match": bool(done) and not bad_identity,
        "request_roles_match": bool(done) and not bad_request,
        "explicit_model_delegation": bool(assignments) and all(item["model_assigned"] and item["worker_call_ids"]
                                                                             for item in assignments),
        "recursive_model_parent_edge": bool(edges) and all(edge["model_chose_parent"] and edge["recorded_parent_exists"] for edge in edges),
        "evaluated_candidates_exposed": bool(coverage) and all(item["injection_reached"] for item in coverage),
        "accepted_model_reviews_for_evaluated_cases": bool(evaluated) and all(identifier(task.get("id")) in reviewed for task in evaluated),
        "no_incomplete_tasks": bool(tasks) and all(task.get("outcome") in {"no_violation", "reproduced", "memory_skipped", "duplicate"} for task in tasks),
        "recovery_complete": (not any(event.get("kind") == "review_recovery_started" for event in events)
                              or object_json(metadata.get("recovery_acceptance")).get("passed") is True),
    }
    usage = Counter()
    for call in calls:
        usage.update(_numeric_mapping(call.get("usage")))
    errors = [call for call in calls if call.get("status") == "error"]
    stored_acceptance = object_json(metadata.get("acceptance"))
    memory = {}
    for key in ("memory_before", "memory_after", "recovery_memory_before", "recovery_memory_after"):
        stats = object_json(metadata.get(key))
        if stats:
            memory[key.removeprefix("memory_")] = {**_numeric_mapping(stats), "outcomes": _numeric_mapping(stats.get("outcomes"))}
    memory["skipped_tasks"] = sum(task.get("outcome") == "memory_skipped" for task in tasks)
    return {"schema_version": 1, "status": identifier(report.get("status")), "simulated": report.get("simulated"),
            "completed_calls_by_role": roles, "returned_model_ids": sorted(returned_models),
            "call_identity_checks": {"done_calls": len(done), "model_mismatch_call_ids": bad_identity,
                                     "request_role_mismatch_call_ids": bad_request},
            "assignments": assignments, "recursive_edges": edges, "verifier_choices": choices,
            "candidate_trials": coverage,
            "usage": {"logical_calls": len(calls), "reported_tokens": dict(usage),
                      "unknown_usage_calls": sum(not call.get("usage") for call in calls)},
            "errors": {"model_calls": len(errors),
                       "diagnostic_records": sum(bool(object_json(call.get("response")).get("diagnostic")) for call in errors),
                       "classes": dict(Counter(identifier(call.get("error")) or "unknown" for call in errors)),
                       "trial_errors": sum(object_json(trial.get("result")).get("status") == "error" for trial in trials.values()),
                       "rejected_plans": sum(event.get("kind") == "plan_rejected" for event in events),
                       "rejected_verifier_actions": sum(not choice["accepted"] for choice in choices)},
            "memory": memory,
            "recovery": {"segments": sum(event.get("kind") == "review_recovery_started" for event in events),
                         "accepted": object_json(metadata.get("recovery_acceptance")).get("passed"),
                         "verifier_output_overrides": [event.get("data", {}).get("verifier_max_tokens_override")
                             for event in events if event.get("kind") == "review_recovery_started"]},
            "acceptance": {"independent_passed": all(checks.values()), "checks": checks,
                           "stored_passed": stored_acceptance.get("passed"),
                           "stored_agrees": stored_acceptance.get("passed") == all(checks.values()) if stored_acceptance else None},
            "meaning": "Recorded evidence of model planning, delegation, local evaluation and review. "
                       "Not a provider authenticity certificate. No observed violation is not a general safety guarantee. "
                       "Token totals include only provider-reported usage."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="report.json or state.sqlite; read-only")
    arguments = parser.parse_args(argv)
    try:
        result = audit_report(load_report(arguments.report))
    except (OSError, ValueError, sqlite3.Error, TypeError, KeyError) as exc:
        print(json.dumps({"error": "Unable to audit run snapshot", "error_class": type(exc).__name__}))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
