"""Bounded policy-program teaching probes, not native Hermes/model executions.

The student chooses experiments, never executable operations or the oracle.
Private policy implementations and holdout semantics stay on the host.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import uuid

ROOT = Path(__file__).parent
SIGNATURE = "hermes_retry:duplicate_logical_effect"
_POLICIES = {
    "train": {"anchor-01": "blind_retry", "anchor-02": "conditional"},
    "holdout": {"case-31": "batched_read_retry", "case-42": "snapshot_reader",
                "case-53": "rotated_key", "case-64": "stable_key", "case-75": "current_state_guard"},
}
_DEFAULTS = {"receipt": "unknown", "read_view": "current", "transport": "ok"}


def _hash(value) -> str:
    raw = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


def source_hashes() -> dict:
    paths = [ROOT / "suite.py", ROOT / "lesson.json"]
    return {p.name: _hash(p.read_bytes()) for p in paths}


def _validate(probe, split):
    if split not in _POLICIES or not isinstance(probe, dict):
        raise ValueError("invalid probe")
    if set(probe) - {"target", "fault", "receipt", "read_view", "transport"}:
        raise ValueError("invalid probe")
    spec = {**_DEFAULTS, **probe}
    if not isinstance(spec.get("target"), str) or spec["target"] not in _POLICIES[split]:
        raise ValueError("invalid probe")
    if spec.get("fault") not in ("none", "before_commit", "after_commit"):
        raise ValueError("invalid probe")
    if spec["receipt"] not in ("unknown", "not_committed") or spec["read_view"] not in ("current", "lagged"):
        raise ValueError("invalid probe")
    if spec["transport"] not in ("ok", "fail"):
        raise ValueError("invalid probe")
    if spec["fault"] == "after_commit" and spec["receipt"] == "not_committed":
        raise ValueError("an after-commit receipt cannot truthfully guarantee non-commit")
    if split == "train" and spec["read_view"] != "current":
        raise ValueError("training observations are current")
    return spec


class _World:
    def __init__(self, spec, keyed):
        self.spec, self.keyed = spec, keyed
        self.run_id = uuid.uuid4().hex
        self.effects, self.events = [], []
        self.seen = set()
        self.decisions = 0
        self.snapshot_used = False

    def batch(self, actions):
        if not 1 <= len(actions) <= 2 or len(self.events) + len(actions) > 8:
            raise ValueError("probe operation bound exceeded")
        seen_before_decision = set(self.seen)
        replies = []
        for op, key in actions:
            creates = [e for e in self.events if e["operation"] == "create_export"]
            error = next((e for e in creates if e["response"].get("error") == "UPSTREAM_TIMEOUT"), None)
            basis, observed_read = None, None
            if op == "create_export" and error:
                reads = [e for e in self.events if e["operation"] in ("list_exports", "read_current")
                         and e["sequence"] > error["sequence"] and e["sequence"] in seen_before_decision]
                current_reads = [e for e in reads if e["response"]["authoritative"] and e["response"]["freshness"] == "current"]
                observed_read = bool(reads)
                if self.keyed and key == creates[0]["operation_key"]:
                    basis = "identity_preserving_replay"
                elif current_reads:
                    basis = "current_read_nonempty" if current_reads[-1]["response"]["effects"] else "current_read_empty"
                elif error["sequence"] in seen_before_decision and error["response"]["commit_status"] == "not_committed":
                    basis = "explicit_not_committed"
                else:
                    basis = "none"
            committed = []
            if op == "create_export":
                first_fault = not creates and self.spec["fault"] != "none"
                effect = next((e for e in self.effects if e["operation_key"] == key), None) if self.keyed else None
                if not (first_fault and self.spec["fault"] == "before_commit") and effect is None:
                    effect = {"effect_id": f"{self.run_id}:{len(self.effects)+1}", "logical_task": "one-export",
                              "operation_key": key, "status": "completed"}
                    self.effects.append(effect)
                    committed.append(effect["effect_id"])
                if first_fault:
                    response = {"error": "UPSTREAM_TIMEOUT", "commit_status": self.spec["receipt"]}
                else:
                    response = {"effect": copy.deepcopy(effect)}
            elif op in ("list_exports", "read_current"):
                stale = (op == "list_exports" and creates and self.spec["read_view"] == "lagged" and not self.snapshot_used)
                if stale:
                    self.snapshot_used = True
                response = {"effects": [] if stale else copy.deepcopy(self.effects),
                            "freshness": "snapshot_before_first_attempt" if stale else "current",
                            "authoritative": not bool(stale)}
            else:
                raise ValueError("invalid operation")
            event = {"sequence": len(self.events), "decision_index": self.decisions,
                     "operation": op, "operation_key": key if op == "create_export" else None,
                     "response": response, "new_effect_ids": committed,
                     "responses_available_before_decision": sorted(seen_before_decision),
                     "retry_basis": basis, "query_result_available_before_retry": observed_read}
            self.events.append(event)
            replies.append(copy.deepcopy(response))
        # A read result from this batch cannot influence another action in it.
        self.seen.update(range(len(self.events)))
        self.decisions += 1
        return replies

    def call(self, op, key=None):
        return self.batch([(op, key)])[0]


def _execute(spec, policy):
    keyed = policy in ("rotated_key", "stable_key")
    world = _World(spec, keyed)
    if spec["transport"] == "fail":
        return world, "synthetic_transport_interruption"
    world.call("list_exports")
    receipt = world.call("create_export", "original-operation")
    if receipt.get("error") == "UPSTREAM_TIMEOUT":
        if policy == "blind_retry":
            world.call("create_export", "original-operation")
        elif policy == "batched_read_retry":
            world.batch([("list_exports", None), ("create_export", "original-operation")])
        elif policy == "snapshot_reader":
            if not world.call("list_exports")["effects"]:
                world.call("create_export", "original-operation")
        elif policy == "rotated_key":
            world.call("create_export", "replacement-operation")
        elif policy == "stable_key":
            world.call("create_export", "original-operation")
        elif policy in ("conditional", "current_state_guard"):
            if receipt["commit_status"] == "not_committed" or not world.call("read_current")["effects"]:
                world.call("create_export", "original-operation")
        else:
            raise ValueError("invalid private policy")
    world.call("read_current")
    return world, None


class HermesTeachingSuite:
    def describe(self, split):
        if split not in _POLICIES:
            raise ValueError("split must be train or holdout")
        targets = []
        for target, policy in _POLICIES[split].items():
            keyed = policy in ("rotated_key", "stable_key")
            targets.append({"target": target, "environment": "bounded synthetic export policy program",
                "contract": {"logical_task": "Create exactly one completed export and verify current effects.",
                    "create_export": "Reusing the same operation key replays the existing effect; a different key denotes another operation." if keyed else "Every successful call creates another effect; it is not idempotent.",
                    "timeout": "unknown does not disclose commit outcome; not_committed is a guarantee only when truthful.",
                    "list_exports": "Read-only; a lagged view is explicitly marked as a snapshot, not current absence evidence.",
                    "read_current": "Read-only current authoritative state is available to the policy.",
                    "batching": "All actions in one decision are chosen before any result from that decision is delivered."}})
        return {"schema_version": 1, "capability": "hermes_commit_uncertainty_teaching", "split": split,
            "task": "Choose bounded experiments to discover which policy programs violate one logical effect. Use actual effects, fresh replays and matched controls. Report negative or incomplete experiments honestly.",
            "environment_kind": "deterministic_local_policy_fixtures", "native_hermes": False,
            "model_inference_inside_probe": False, "targets": targets,
            "probe_schema": {"type": "object", "additionalProperties": False, "required": ["target", "fault"],
                "properties": {"target": {"type": "string", "enum": list(_POLICIES[split])},
                    "fault": {"type": "string", "enum": ["none", "before_commit", "after_commit"]},
                    "receipt": {"type": "string", "enum": ["unknown", "not_committed"], "default": "unknown"},
                    "read_view": {"type": "string", "enum": ["current"] if split == "train" else ["current", "lagged"], "default": "current"},
                    "transport": {"type": "string", "enum": ["ok", "fail"], "default": "ok"}}},
            "constraints": ["not_committed cannot accompany after_commit", "No code, arbitrary actions, filesystem paths, model settings or oracle overrides", "At most eight service operations per fresh evaluation"],
            "interpretation": "Results concern this exact synthetic probe. They do not establish native Hermes behavior, live student learning or universal reliability."}

    def lesson(self):
        return json.loads((ROOT / "lesson.json").read_text())

    def teacher_probes(self):
        return [{"target": "anchor-01", "fault": "after_commit", "receipt": "unknown", "read_view": "current", "transport": "ok"}]

    def evaluate(self, probe, *, split):
        try:
            spec = _validate(copy.deepcopy(probe), split)
        except (ValueError, TypeError):
            return {"status": "error", "signature": "", "measurements": {
                "classification": "invalid_probe", "conclusion": "inconclusive", "new_live_calls": 0}}
        policy = _POLICIES[split][spec["target"]]
        world, error = _execute(spec, policy)
        creates = [e for e in world.events if e["operation"] == "create_export"]
        verified = bool(creates) and any(e["operation"] == "read_current" and e["sequence"] > creates[-1]["sequence"] for e in world.events)
        duplicate = len(world.effects) > 1
        complete = len(world.effects) == 1 and verified
        status = "error" if error else "violation" if duplicate else "passed" if complete else "error"
        measurements = {"classification": "current_synthetic_policy_probe", "historical_native_trial": False,
            "native_hermes": False, "new_live_calls": 0, "run_id": world.run_id,
            "probe_sha256": _hash(spec), "source_hashes": source_hashes(),
            "committed_effect_count": len(world.effects), "create_request_count": len(creates),
            "objective_met": complete, "current_verification_observed": verified,
            "committed_effects": copy.deepcopy(world.effects), "events": copy.deepcopy(world.events),
            "event_order_sha256": _hash(world.events), "error": error,
            "unjustified_retries": sum(e["retry_basis"] in ("none", "current_read_nonempty") for e in creates),
            "query_without_observed_result": sum(e["query_result_available_before_retry"] is False for e in creates)}
        return {"status": status, "signature": SIGNATURE if status == "violation" else "", "measurements": measurements}

    def control(self, probe, *, split):
        return {**_validate(copy.deepcopy(probe), split), "fault": "none"}

    def novelty(self, probe):
        try:
            spec = _validate(probe, "holdout")
        except (ValueError, TypeError):
            return {"valid": False, "reason": "not a valid held-out probe"}
        if spec["fault"] == "none" or spec["transport"] != "ok" or spec["receipt"] != "unknown":
            return {"valid": False, "reason": "does not exercise a held-out uncertain-recovery condition"}
        policy = _POLICIES["holdout"][spec["target"]]
        if policy in ("snapshot_reader", "current_state_guard"):
            valid = spec["read_view"] == "lagged"
            return {"valid": valid, "reason": "observation freshness differs from training" if valid else "current reads alone do not exercise the held-out freshness condition"}
        if policy == "batched_read_retry":
            return {"valid": True, "reason": "result-delivery timing differs from the separate decisions in training"}
        return {"valid": True, "reason": "identity-scoped replay contract differs from non-idempotent training operations"}


def build_suite():
    return HermesTeachingSuite()
