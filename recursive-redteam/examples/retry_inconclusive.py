#!/usr/bin/env python3
"""Retry existing inconclusive reviews once without changing the original run.

The original configuration, sources, candidates, journal and budget remain intact.
An explicit verifier output override creates an audited recovery configuration
and separate memory scope. Completed trials are reused; failed idempotent trials
may be retried by the engine. No planning or new tasks occur.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import uuid

# Run directly from the repository without requiring an editable installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recursive_redteam import cli
from recursive_redteam.budget import BudgetedModel, ModelGate
from recursive_redteam.config import fingerprint, load_config, validate_config
from recursive_redteam.contracts import BudgetExceeded, Limits
from recursive_redteam.credentials import load_credentials
from recursive_redteam.engine import Engine, ManualReviewRequired
from recursive_redteam.memory import ExperimentMemory
from recursive_redteam.provenance import memory_identity, memory_scope, source_manifest
from recursive_redteam.store import Store
from recursive_redteam.verification import ModelVerifier


class _RecoveryStoreView:
    """Keep provider preparation metadata for this segment, not the original."""
    def __init__(self, store, recovery_id):
        self.store, self.recovery_id = store, recovery_id

    def __getattr__(self, name):
        return getattr(self.store, name)

    def set_metadata(self, key, value):
        if key.startswith("model_"):
            self.store.event("review_recovery_model_prepared", {
                "recovery_id": self.recovery_id, "role": key.removeprefix("model_"), "metadata": value})
        else:
            self.store.set_metadata(key, value)


class _RecoveryMemory(ExperimentMemory):
    """A new review must not make old target observations fresh again."""
    def __init__(self, *args, store, **kwargs):
        super().__init__(*args, **kwargs)
        self.run_store = store
        self.configured_ttl = self.conclusion_ttl_seconds

    def finish(self, scope, candidate, campaign_id, outcome, evidence, source_run, *, lease_token=None):
        trial_ids = set(evidence.get("trials", {}))
        times = {}
        for event in self.run_store.report()["events"]:
            if event["kind"] != "trial_finished" or event["data"].get("trial_id") not in trial_ids:
                continue
            try:
                timestamp = datetime.fromisoformat(event["time"])
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                times[event["data"]["trial_id"]] = timestamp
            except (ValueError, TypeError):
                times.pop(event["data"]["trial_id"], None)
        oldest = min(times.values()) if trial_ids and set(times) == trial_ids else None
        reviewed = datetime.now(timezone.utc)
        remaining = max(0, min(self.configured_ttl,
                               self.configured_ttl - (reviewed - oldest).total_seconds())) if oldest else 0
        evidence = dict(evidence, target_observed_at=oldest.isoformat() if oldest else None,
                        reviewed_at=reviewed.isoformat(),
                        freshness_note="Review may reuse cached trials; expiry is capped by the oldest used target observation. "
                                       "Expired or undated observations cannot suppress a fresh test.")
        with self.lock:
            previous_ttl = self.conclusion_ttl_seconds
            self.conclusion_ttl_seconds = remaining
            try:
                super().finish(scope, candidate, campaign_id, outcome, evidence, source_run,
                               lease_token=lease_token)
            finally:
                self.conclusion_ttl_seconds = previous_ttl


def recover_run(root: Path, *, env_file: Path | None = None,
                verifier_max_tokens: int | None = None, all_reviews: bool = False) -> dict:
    root = Path(root).resolve()
    if not (root / "state.sqlite").is_file():
        raise ValueError("run has no state.sqlite")
    with cli.run_lock(root):
        store = Store(root / "state.sqlite")
        memory = None
        try:
            config = load_config(root / "config.json")
            if store.get_metadata("config_sha256") != fingerprint(config):
                raise ValueError("saved configuration changed; recovery requires identical configuration")
            sources = source_manifest()
            if store.get_metadata("source_manifest") != sources:
                raise ValueError("evaluator source changed; recovery requires identical sources")
            effective_config = deepcopy(config)
            if verifier_max_tokens is not None:
                if config["verifier"]["kind"] != "openai-compatible":
                    raise ValueError("verifier output override requires an external model configuration")
                effective_config["verifier"]["max_tokens"] = verifier_max_tokens
                effective_config = validate_config(effective_config, base=root)
            selected = [task for task in store.tasks() if (all_reviews or task.get("outcome") == "inconclusive")
                        and store.candidate(task["id"]) is not None]
            if not selected:
                store.set_metadata("acceptance", cli.assess_run(store.report()))
                store.set_metadata("recovery_acceptance", {"passed": True, "selected_candidates": 0,
                                    "reviewed_candidate_ids": [], "missing_candidate_ids": [], "no_action_needed": True})
                report = store.report()
                cli.write_json(root / "report.json", report)
                return report

            recovery_id = uuid.uuid4().hex
            helper_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
            prior_status = store.get_metadata("status", "created")
            limits = Limits(**config["limits"])
            before_calls = store.report()["model_usage"]["logical_calls"]
            before_event_id = max((event["id"] for event in store.report()["events"]), default=0)
            recovery_config_file = f"recovery-{recovery_id}-config.json"
            cli.write_json(root / recovery_config_file, effective_config)
            store.event("review_recovery_started", {
                "recovery_id": recovery_id, "helper_sha256": helper_sha256,
                "candidate_ids": [task["id"] for task in selected], "prior_status": prior_status,
                "logical_calls_before": before_calls, "max_model_calls": limits.max_model_calls,
                "attempt_limit_per_task": 1, "all_reviews": all_reviews,
                "verifier_max_tokens_override": verifier_max_tokens,
                "effective_verifier_config": effective_config["verifier"],
                "effective_config_sha256": fingerprint(effective_config),
                "effective_config_file": recovery_config_file, "source_manifest": sources,
            })
            attempted = []
            failure = None
            scope = ""
            try:
                if before_calls >= limits.max_model_calls:
                    store.update_task(selected[0]["id"], "queued")
                    raise BudgetExceeded("existing model-call budget exhausted")
                load_credentials(env_file, config)
                interval = config.get("request_interval_seconds", 0)
                gate = ModelGate(interval) if interval else None
                preparation_store = _RecoveryStoreView(store, recovery_id)
                planner = BudgetedModel(cli.prepare_model(config["planner"], "coordinator", preparation_store),
                                        store, limits.max_model_calls, "coordinator-or-worker", gate=gate)
                target = cli.make_target(config["target"], preparation_store, limits.max_model_calls, gate=gate)
                verifier = ModelVerifier(BudgetedModel(
                    cli.prepare_model(effective_config["verifier"], "verifier", preparation_store),
                    store, limits.max_model_calls, "verifier", gate=gate), store, replays=limits.replays)
                memory_config = config.get("memory", {"enabled": False})
                if memory_config["enabled"]:
                    original_identity = memory_identity(config, target.describe(), sources)
                    if (store.get_metadata("memory_scope") != memory_scope(original_identity)
                            or store.get_metadata("memory_identity") != original_identity):
                        raise ValueError("saved memory identity does not match the original evaluator")
                    identity = memory_identity(effective_config, target.describe(), sources)
                    scope = memory_scope(identity)
                    memory = _RecoveryMemory(memory_config["path"], store=store,
                        conclusion_ttl_seconds=memory_config["conclusion_ttl_seconds"],
                        lease_seconds=memory_config["lease_seconds"])
                    before = memory.stats(scope)
                    store.set_metadata("recovery_memory_scope", scope)
                    store.set_metadata("recovery_memory_identity", identity)
                    store.set_metadata("recovery_memory_before", before)
                    store.event("review_recovery_memory", {"recovery_id": recovery_id, "scope": scope,
                                "identity": identity, "before": before})
                engine = Engine(planner, target, store, limits, verifier=verifier, memory=memory,
                                memory_scope=scope, force_retest=all_reviews or memory_config.get("force_retest", False),
                                source_run=str(root))
                engine.resuming = True
                store.set_metadata("status", "running")
                for task in selected:
                    attempted.append(task["id"])
                    store.event("review_recovery_task_started", {"recovery_id": recovery_id,
                                "candidate_id": task["id"], "prior_task": task})
                    engine._work(task)
                    latest = next(item for item in store.tasks() if item["id"] == task["id"])
                    store.event("review_recovery_task_finished", {"recovery_id": recovery_id,
                                "candidate_id": task["id"], "status": latest["status"],
                                "outcome": latest.get("outcome")})
                # Queue completion is distinct from successful review acceptance.
                # A recovery cannot complete planning that an earlier run never did.
                store.set_metadata("status", prior_status)
            except BudgetExceeded:
                failure = "BudgetExceeded"
                store.set_metadata("status", "budget_exhausted")
            except ManualReviewRequired:
                failure = "ManualReviewRequired"
                store.set_metadata("status", "manual_review_required")
            except KeyboardInterrupt:
                failure = "KeyboardInterrupt"
                store.set_metadata("status", "interrupted")
                raise
            except Exception as exc:
                failure = type(exc).__name__
                store.set_metadata("status", prior_status)
                raise
            finally:
                if memory is not None:
                    after = memory.stats(scope)
                    store.set_metadata("recovery_memory_after", after)
                store.set_metadata("acceptance", cli.assess_run(store.report()))
                final = store.report()
                reviewed_ids = {event["data"].get("candidate_id") for event in final["events"]
                                if event["id"] > before_event_id and event["kind"] == "verification_review"
                                and event["data"].get("review", {}).get("accepted") is True
                                and event["data"].get("outcome") in {"no_violation", "reproduced"}}
                conclusive_ids = {task["id"] for task in final["tasks"]
                                  if task.get("outcome") in {"no_violation", "reproduced"}}
                recovered_ids = reviewed_ids & conclusive_ids
                missing_ids = sorted({task["id"] for task in selected} - recovered_ids)
                recovery_acceptance = {"passed": not missing_ids and failure is None,
                    "selected_candidates": len(selected), "reviewed_candidate_ids": sorted(recovered_ids),
                    "missing_candidate_ids": missing_ids, "no_action_needed": False}
                store.set_metadata("recovery_acceptance", recovery_acceptance)
                store.event("review_recovery_finished", {
                    "recovery_id": recovery_id, "helper_sha256": helper_sha256,
                    "attempted_candidate_ids": attempted, "error_class": failure,
                    "logical_calls_after": final["model_usage"]["logical_calls"],
                    "remaining_inconclusive": sum(task.get("outcome") == "inconclusive" for task in final["tasks"]),
                    "recovery_memory_scope": scope,
                    "recovery_memory_after": memory.stats(scope) if memory is not None else None,
                    "acceptance_passed": final["metadata"]["acceptance"]["passed"],
                    "recovery_acceptance": recovery_acceptance,
                    "status": final["status"],
                })
                cli.write_json(root / "report.json", store.report())
            return store.report()
        finally:
            if memory is not None:
                memory.close()
            store.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--verifier-max-tokens", type=int,
                        help="Explicitly change only the verifier output cap in a recorded recovery segment")
    parser.add_argument("--all-reviews", action="store_true",
                        help="Re-review all existing candidates once, reusing their completed trials")
    args = parser.parse_args(argv)
    try:
        report = recover_run(args.run, env_file=args.env_file,
                             verifier_max_tokens=args.verifier_max_tokens, all_reviews=args.all_reviews)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        # A plugin/provider exception can contain credentials or an untrusted body.
        print(json.dumps({"error": "Review recovery could not run", "error_class": type(exc).__name__}))
        return 1
    remaining = sum(task.get("outcome") == "inconclusive" for task in report["tasks"])
    print(json.dumps({"status": report["status"], "remaining_inconclusive": remaining,
                      "acceptance": report["metadata"].get("acceptance"),
                      "recovery_acceptance": report["metadata"].get("recovery_acceptance"),
                      "model_usage": report["model_usage"], "report": str((args.run / "report.json").resolve())}, indent=2))
    return 0 if (not remaining and report["metadata"].get("acceptance", {}).get("passed")
                 and report["metadata"].get("recovery_acceptance", {}).get("passed")) else 2


if __name__ == "__main__":
    raise SystemExit(main())
