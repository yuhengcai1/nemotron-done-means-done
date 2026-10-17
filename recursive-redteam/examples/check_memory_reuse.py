#!/usr/bin/env python3
"""Check exact-input memory reuse from a completed run without calling a model.

This briefly acquires and releases retryable input leases in the existing memory
database. It never appends an observation or changes the source run. Output omits
payloads, hypotheses, and raw evidence. Run from any directory:
    python examples/check_memory_reuse.py --run runs/example --out reuse.json
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recursive_redteam.contracts import Candidate
from recursive_redteam.memory import CONCLUSIVE, ExperimentMemory, payload_digest
from recursive_redteam.provenance import memory_scope


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _identifier(value):
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:/+-]{1,200}", value) and "INTERNAL_CANARY_" not in value:
        return value
    return "redacted"


def _read_run(root):
    path = root / "state.sqlite"
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as database:
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA query_only=ON")
        database.execute("BEGIN")
        metadata = {row["key"]: json.loads(row["value"]) for row in database.execute("SELECT key,value FROM metadata")}
        tasks = {row["id"]: dict(json.loads(row["data"]), status=row["status"])
                 for row in database.execute("SELECT id,status,data FROM tasks")}
        candidates = [Candidate(**json.loads(row["data"])) for row in database.execute("SELECT data FROM candidates ORDER BY rowid")]
        model_calls = database.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0]
    return metadata, tasks, candidates, model_calls


def _memory_snapshot(path, scope):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as database:
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA query_only=ON")
        database.execute("BEGIN")
        keys = [dict(row) for row in database.execute("SELECT * FROM experiment_keys WHERE scope=?", (scope,))]
        observations = [dict(row) for row in database.execute("""SELECT o.* FROM experiment_observations o
            JOIN experiment_keys k ON k.id=o.key_id WHERE k.scope=? ORDER BY o.id""", (scope,))]
    return keys, observations


def _evidence_checks(key, observations, candidate, original_campaign, root, configured_ttl):
    evidence_hashes, observation_hashes, expiry_bounds = True, True, True
    linked = False
    for observation in observations:
        evidence = json.loads(observation["evidence"])
        evidence_hashes &= _hash(_canonical(evidence)) == observation["evidence_hash"]
        envelope = {"scope": key["scope"], "family": key["family"], "payload_digest": key["payload_digest"],
                    "candidate_id": observation["candidate_id"], "hypothesis": observation["hypothesis"],
                    "campaign_id": observation["campaign_id"], "outcome": observation["outcome"],
                    "evidence_hash": observation["evidence_hash"], "source_run": observation["source_run"],
                    "observed_at": observation["observed_at"], "expires_at": observation["expires_at"]}
        observation_hashes &= _hash(_canonical(envelope)) == observation["observation_hash"]
        if observation["outcome"] in CONCLUSIVE:
            expires = observation["expires_at"]
            expiry_bounds &= (isinstance(expires, (int, float)) and
                              0 <= expires - observation["observed_at"] <= configured_ttl + 0.001)
        else:
            expiry_bounds &= observation["expires_at"] is None
        linked |= (observation["campaign_id"] == original_campaign and observation["candidate_id"] == candidate.id
                   and Path(observation["source_run"]).resolve() == root)
    return {"evidence_hashes_valid": bool(observations) and bool(evidence_hashes),
            "observation_hashes_valid": bool(observations) and bool(observation_hashes),
            "ttl_bounds_valid": bool(observations) and bool(expiry_bounds),
            "source_run_observation_present": bool(linked)}


def check_memory_reuse(run, *, scope_kind="original"):
    root = Path(run).resolve()
    metadata, tasks, candidates, calls_before = _read_run(root)
    if metadata.get("status") != "completed":
        raise ValueError("memory reuse requires a completed source run")
    if scope_kind not in {"original", "recovery"}:
        raise ValueError("scope must be original or recovery")
    prefix = "recovery_" if scope_kind == "recovery" else ""
    scope, identity = metadata.get(prefix + "memory_scope"), metadata.get(prefix + "memory_identity")
    if not isinstance(scope, str) or not isinstance(identity, dict) or memory_scope(identity) != scope:
        raise ValueError("saved memory scope and identity do not match")
    settings = metadata.get("config", {}).get("memory", {})
    if not settings.get("enabled") or not isinstance(settings.get("path"), str):
        raise ValueError("source run did not configure persistent memory")
    memory_path = Path(settings["path"]).expanduser().resolve()
    if not memory_path.is_file():
        raise ValueError("saved memory database does not exist")
    original_campaign = metadata.get("campaign_id")
    if not isinstance(original_campaign, str) or not original_campaign:
        raise ValueError("source campaign identity is missing")
    campaign = "memory-reuse-check-" + uuid.uuid4().hex
    selected = [candidate for candidate in candidates if tasks.get(candidate.id, {}).get("outcome")
                in CONCLUSIVE | {"inconclusive", "error", "incomplete"}]
    if not selected:
        raise ValueError("source run has no evaluated candidates to check")
    memory = ExperimentMemory(memory_path, conclusion_ttl_seconds=settings["conclusion_ttl_seconds"],
                              lease_seconds=settings["lease_seconds"])
    results = []
    try:
        keys_before, observations_before = _memory_snapshot(memory_path, scope)
        for candidate in selected:
            digest = payload_digest(candidate.payload)
            key = next((key for key in keys_before if key["family"] == candidate.family and key["payload_digest"] == digest
                        and key.get("digest_version") == "raw-utf8-v1"), None)
            base = {"candidate_id": _identifier(candidate.id), "payload_digest": digest,
                    "source_task_outcome": _identifier(tasks[candidate.id].get("outcome"))}
            if key is None:
                results.append(dict(base, passed=False, error="exact_input_memory_missing"))
                continue
            observations = [observation for observation in observations_before if observation["key_id"] == key["id"]]
            checks = _evidence_checks(key, observations, candidate, original_campaign, root,
                                      settings["conclusion_ttl_seconds"])
            latest = observations[-1] if observations else None
            contested = len({observation["outcome"] for observation in observations if observation["outcome"] in CONCLUSIVE}) > 1
            current = time.time()
            fresh = bool(latest and latest["outcome"] in CONCLUSIVE and latest["expires_at"] > current and not contested)
            pending = key["owner"] is not None and key["lease_until"] > current
            claim, released = None, True
            try:
                claim = memory.claim(scope, candidate, campaign)
            finally:
                # Also clean up if an interruption arrives after SQLite commits
                # but before the claim return value is assigned locally.
                did_release = memory.release(scope, candidate, campaign)
                if claim and claim["claimed"]:
                    released = did_release
            checks["expected_claim_decision"] = (not pending and bool(claim) and
                ((fresh and not claim["claimed"] and claim["reason"] == "conclusive") or
                 (not fresh and claim["claimed"])))
            checks["acquired_lease_released"] = released
            results.append(dict(base, outcome=latest["outcome"] if latest else None,
                observation_count=len(observations), contested=contested,
                fresh_conclusive=fresh, remaining_ttl_seconds=round(max(0, latest["expires_at"] - current), 3)
                    if latest and latest["expires_at"] is not None else None,
                evidence_reference=f"observation:{latest['id']}" if latest else None,
                evidence_hash=latest["evidence_hash"] if latest else None,
                claimed=claim["claimed"], reason=claim["reason"], lease_released=released,
                checks=checks, passed=all(checks.values())))
        keys_after, observations_after = _memory_snapshot(memory_path, scope)
    finally:
        memory.close()
    _, _, _, calls_after = _read_run(root)
    owned_leases = [key for key in keys_after if key["owner"] == campaign]
    fresh_suppressed = sum(result.get("fresh_conclusive", False) and result.get("reason") == "conclusive" for result in results)
    retryable = [result for result in results if result.get("outcome") in {"inconclusive", "error", "incomplete"}]
    checks = {"different_campaign": campaign != original_campaign,
              "all_candidate_checks_passed": all(result["passed"] for result in results),
              "fresh_conclusive_duplicate_suppressed": fresh_suppressed > 0,
              "observation_history_unchanged": observations_before == observations_after,
              "no_owned_leases_left": not owned_leases,
              "source_model_call_count_unchanged": calls_before == calls_after}
    return {"schema_version": 1, "checked_at": datetime.now(timezone.utc).isoformat(),
            "scope_kind": scope_kind, "scope": scope, "source_campaign_id": _identifier(original_campaign),
            "check_campaign_id": campaign, "checks": checks, "passed": all(checks.values()),
            "counts": {"candidates_checked": len(results), "fresh_conclusive_suppressed": fresh_suppressed,
                       "retryable_candidates_checked": len(retryable),
                       "retryable_candidates_claimed_and_released": sum(result.get("claimed", False) and
                           result.get("lease_released", False) for result in retryable),
                       "source_model_calls_before": calls_before, "source_model_calls_after": calls_after,
                       "new_model_calls": 0},
            "retryable_check": "checked" if retryable else "not_applicable_no_retryable_candidates_in_scope",
            "candidates": results,
            "meaning": "Existing evidence was checked and exact inputs were claimed under a different campaign. "
                       "No target was evaluated and no new safety finding was produced."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--scope", choices=("original", "recovery"), default="original")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        result = check_memory_reuse(args.run, scope_kind=args.scope)
        encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(encoded, encoding="utf-8")
        print(encoded, end="")
        return 0 if result["passed"] else 2
    except (OSError, ValueError, sqlite3.Error, TypeError, KeyError) as exc:
        print(json.dumps({"error": "Memory reuse check could not complete", "error_class": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
