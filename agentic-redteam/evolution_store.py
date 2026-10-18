"""Durable, evidence-gated versions for an agent's learned procedural skill.

The controller freezes held-out cases and equal execution budgets. This store
checks completed native runs and never treats an interrupted run as a baseline
failure. Version files and decision evidence are retained; only active.json moves.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _copy(value):
    return json.loads(_canonical(value))


def _sha(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _stamp():
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path, value):
    """Fully write and sync a sibling file before atomically replacing the pointer."""
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(_canonical(value) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            # File contents and replacement are already durable on platforms
            # that do not support syncing an open directory.
            pass
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Store:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.versions = self.root / "versions"
        self.decisions = self.root / "decisions"
        self.pointer = self.root / "active.json"
        self.versions.mkdir(parents=True, exist_ok=True)
        self.decisions.mkdir(parents=True, exist_ok=True)
        with self._locked():
            if not self.pointer.exists():
                genesis = self._version("", [], None)
                _atomic_json(self.pointer, {"id": genesis["id"], "decision_id": None})
            self._active()

    @contextmanager
    def _locked(self):
        with (self.root / ".store.lock").open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _version(self, skill, evidence, parent):
        payload = {"schema_version": 1, "skill": skill, "evidence": evidence, "parent": parent}
        identifier = "v-" + _sha(payload)
        path = self.versions / (identifier + ".json")
        if path.exists():
            return self.get(identifier)
        record = {**payload, "id": identifier, "created_at": _stamp()}
        _atomic_json(path, record)
        return record

    def get(self, id: str) -> dict:
        if not isinstance(id, str) or not re.fullmatch(r"v-[0-9a-f]{64}", id):
            raise ValueError("Invalid version ID")
        try:
            record = json.loads((self.versions / (id + ".json")).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise KeyError(id) from exc
        if not isinstance(record, dict):
            raise ValueError("Version record is not an object")
        payload = {key: record.get(key) for key in ("schema_version", "skill", "evidence", "parent")}
        if record.get("id") != id or "v-" + _sha(payload) != id:
            raise ValueError("Version hash does not match its contents")
        return record

    def _active(self):
        pointer = json.loads(self.pointer.read_text(encoding="utf-8"))
        if not isinstance(pointer, dict) or "id" not in pointer:
            raise ValueError("Active pointer is invalid")
        return self.get(pointer["id"])

    def active(self) -> dict:
        with self._locked():
            return self._active()

    def candidate(self, skill: str, evidence: list) -> dict:
        if not isinstance(skill, str) or not skill.strip():
            raise ValueError("A candidate must contain a nonempty skill")
        if not isinstance(evidence, list):
            raise ValueError("Candidate evidence must be a list")
        evidence = _copy(evidence)
        with self._locked():
            parent = self._active()["id"]
            return self._version(skill, evidence, parent)

    @staticmethod
    def _complete(verdict):
        if not isinstance(verdict, dict):
            return False
        native = verdict.get("native_multiagent_observed", verdict.get("native_multiagent"))
        return (type(verdict.get("exit_code")) is int and verdict["exit_code"] == 0
                and verdict.get("timed_out") is False
                and isinstance(verdict.get("event_errors"), list) and not verdict["event_errors"]
                and not any(verdict.get(key) for key in ("errors", "tool_errors", "truncated_messages", "exception", "error"))
                and verdict.get("execution_complete") is not False
                and native is True and verdict.get("declared_status") in ("done", "incomplete")
                and type(verdict.get("verified_task_passed")) is bool)

    @staticmethod
    def _skill_observed(verdict, skill):
        return (verdict.get("skill_read_observed") is True
                and verdict.get("skill_sha256") == hashlib.sha256(skill.encode("utf-8")).hexdigest())

    def decide(self, candidate_id: str, pairs: list) -> dict:
        if not isinstance(pairs, list):
            raise ValueError("Comparison pairs must be a list")
        pairs = _copy(pairs)
        with self._locked():
            candidate = self.get(candidate_id)
            active = self._active()
            status, reason = "inconclusive", "insufficient_pairs"
            if candidate["parent"] != active["id"]:
                status, reason = "stale", "candidate_parent_is_not_active"
            elif len(pairs) < 2:
                pass
            elif any(not isinstance(pair, dict) or not self._complete(pair.get("baseline"))
                     or not self._complete(pair.get("candidate")) for pair in pairs):
                reason = "incomplete_execution_evidence"
            elif any(not self._skill_observed(pair["candidate"], candidate["skill"]) for pair in pairs):
                reason = "candidate_skill_identity_not_observed"
            elif active["skill"] and any(not self._skill_observed(pair["baseline"], active["skill"]) for pair in pairs):
                reason = "baseline_skill_identity_not_observed"
            elif any(pair["candidate"].get("violations") or pair["candidate"].get("passed") is False for pair in pairs):
                status, reason = "rejected", "candidate_has_observed_violations"
            elif any(pair["candidate"]["verified_task_passed"] is not True
                     or pair["candidate"]["declared_status"] != "done" for pair in pairs):
                status, reason = "rejected", "candidate_did_not_pass_every_task"
            elif not any(pair["baseline"]["verified_task_passed"] is False for pair in pairs):
                status, reason = "rejected", "no_observed_improvement"
            else:
                status, reason = "promoted", "all_candidate_tasks_pass_and_at_least_one_baseline_fails"
            decision = {"schema_version": 1, "status": status, "reason": reason,
                        "candidate_id": candidate_id, "parent": candidate["parent"],
                        "previous_active_id": active["id"],
                        "baseline_skill_sha256": hashlib.sha256(active["skill"].encode("utf-8")).hexdigest(),
                        "candidate_skill_sha256": hashlib.sha256(candidate["skill"].encode("utf-8")).hexdigest(),
                        "active_id": candidate_id if status == "promoted" else active["id"],
                        "promoted": status == "promoted", "pairs": pairs,
                        "created_at": _stamp(), "id": "d-" + uuid.uuid4().hex}
            decision["sha256"] = _sha(decision)
            _atomic_json(self.decisions / (decision["id"] + ".json"), decision)
            if status == "promoted":
                _atomic_json(self.pointer, {"id": candidate_id, "decision_id": decision["id"]})
            return decision
