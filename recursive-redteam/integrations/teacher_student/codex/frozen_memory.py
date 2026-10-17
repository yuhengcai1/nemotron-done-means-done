"""Durable, scoped experiment observations; generated text is never an instruction.

This memory stores provenance and exact-input identities, not arbitrary lessons.
Callers supply a scope that identifies the target, model and policy versions.
``no_violation`` means only that the recorded trial observed no violation; it is
not a safety guarantee. Conflicting observations stay visible permanently.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
import unicodedata
from typing import Any, Iterator

from .contracts import Candidate


CONCLUSIVE = frozenset({"no_violation", "reproduced"})
OUTCOMES = CONCLUSIVE | {"inconclusive", "error", "incomplete"}


def normalize_payload(payload: str) -> str:
    """Optional display helper; never used to identify executed inputs."""
    return unicodedata.normalize("NFC", payload.replace("\r\n", "\n").replace("\r", "\n")).strip()


def payload_digest(payload: str) -> str:
    """Identify the exact UTF-8 input passed to a target without normalization."""
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")


def _duration(value: float, field: str, *, allow_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{field} must be finite seconds")
    if value < 0 or (not allow_zero and value == 0):
        raise ValueError(f"{field} must be {'nonnegative' if allow_zero else 'positive'}")
    return float(value)


class ExperimentMemory:
    """SQLite memory shared by threads/processes and independent campaigns.

    ``claim(..., ttl_seconds=...)`` overrides the pending lease duration.
    ``conclusion_ttl_seconds`` controls how long a conclusive observation avoids
    duplicate work. Expiry never removes evidence. A forced retest bypasses a
    conclusion, but cannot steal another candidate's active lease. Ownership is
    the campaign/candidate pair; long trials must renew before the lease expires.
    """

    def __init__(self, path: str | Path, *, conclusion_ttl_seconds: float = 86400,
                 lease_seconds: float = 600):
        self.path = Path(path)
        self.conclusion_ttl_seconds = _duration(conclusion_ttl_seconds, "conclusion_ttl_seconds", allow_zero=True)
        self.lease_seconds = _duration(lease_seconds, "lease_seconds")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS experiment_keys(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL, family TEXT NOT NULL, payload_digest TEXT NOT NULL,
                hypothesis TEXT NOT NULL, created_at REAL NOT NULL,
                owner TEXT, owner_candidate_id TEXT, lease_until REAL, lease_duration REAL,
                digest_version TEXT NOT NULL DEFAULT 'raw-utf8-v1',
                UNIQUE(scope,family,payload_digest)
            );
            CREATE TABLE IF NOT EXISTS experiment_observations(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key_id INTEGER NOT NULL REFERENCES experiment_keys(id),
                campaign_id TEXT NOT NULL, candidate_id TEXT NOT NULL,
                hypothesis TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK(outcome IN
                    ('no_violation','reproduced','inconclusive','error','incomplete')),
                evidence TEXT NOT NULL, evidence_hash TEXT NOT NULL,
                source_run TEXT NOT NULL, observed_at REAL NOT NULL,
                expires_at REAL, observation_hash TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS experiment_observation_key
                ON experiment_observations(key_id,id);
            CREATE TRIGGER IF NOT EXISTS experiment_observation_no_update
                BEFORE UPDATE ON experiment_observations BEGIN
                SELECT RAISE(ABORT,'experiment observations are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS experiment_observation_no_delete
                BEFORE DELETE ON experiment_observations BEGIN
                SELECT RAISE(ABORT,'experiment observations are append-only'); END;
        """)
        with self._write():
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(experiment_keys)")}
            for name, sql_type in (("owner_candidate_id", "TEXT"), ("lease_duration", "REAL")):
                if name not in columns:
                    self.db.execute(f"ALTER TABLE experiment_keys ADD COLUMN {name} {sql_type}")
            if "digest_version" not in columns:
                self.db.execute("ALTER TABLE experiment_keys ADD COLUMN digest_version TEXT")
                # Legacy digests hashed normalized text, whose original bytes
                # were not retained. Preserve their history without allowing
                # them to suppress any new raw-input experiment.
                self.db.execute("""UPDATE experiment_keys
                    SET payload_digest='legacy-normalized:' || payload_digest,
                        digest_version='normalized-v1',owner=NULL,owner_candidate_id=NULL,
                        lease_until=NULL,lease_duration=NULL""")

    @contextmanager
    def _write(self) -> Iterator[None]:
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.db.rollback()
                raise
            else:
                self.db.commit()

    @contextmanager
    def _read(self) -> Iterator[None]:
        # A WAL snapshot keeps keys and their observations consistent even when
        # another process finishes an experiment between our SELECT statements.
        with self.lock:
            self.db.execute("BEGIN")
            try:
                yield
            finally:
                self.db.rollback()

    @staticmethod
    def _identity(scope: str, candidate: Candidate, campaign_id: str | None = None) -> tuple[str, str, str]:
        _text(scope, "scope")
        if not isinstance(candidate, Candidate):
            raise ValueError("candidate must be a Candidate")
        for field in ("id", "family", "hypothesis", "payload"):
            _text(getattr(candidate, field), f"candidate.{field}")
        if campaign_id is not None:
            _text(campaign_id, "campaign_id")
        return scope, candidate.family, payload_digest(candidate.payload)

    def _row(self, identity: tuple[str, str, str]) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM experiment_keys WHERE scope=? AND family=? AND payload_digest=?",
                               identity).fetchone()

    def _summary(self, row: sqlite3.Row, current: float) -> dict:
        observations = self.db.execute(
            "SELECT * FROM experiment_observations WHERE key_id=? ORDER BY id", (row["id"],)).fetchall()
        latest = observations[-1] if observations else None
        conclusions = sorted({obs["outcome"] for obs in observations if obs["outcome"] in CONCLUSIVE})
        contested = len(conclusions) > 1
        pending = row["owner"] is not None and row["lease_until"] > current
        outcome = "contested" if contested else latest["outcome"] if latest else "pending" if pending else "incomplete"
        fresh = bool(latest and latest["expires_at"] is not None and latest["expires_at"] > current)
        return {
            "scope": row["scope"], "family": row["family"],
            "hypothesis": latest["hypothesis"] if latest else row["hypothesis"],
            "payload_digest": row["payload_digest"].removeprefix("legacy-normalized:"),
            "digest_version": row["digest_version"], "dedup_eligible": row["digest_version"] == "raw-utf8-v1",
            "outcome": outcome,
            "observed_outcomes": sorted({obs["outcome"] for obs in observations}),
            "contested": contested, "fresh": fresh, "pending": pending,
            "lease_until": _stamp(row["lease_until"]) if pending else None,
            "observation_count": len(observations),
            "time": _stamp(latest["observed_at"] if latest else row["created_at"]),
            "expires_at": _stamp(latest["expires_at"]) if latest and latest["expires_at"] is not None else None,
            "evidence_hash": latest["evidence_hash"] if latest else None,
            "evidence_reference": f"observation:{latest['id']}" if latest else None,
            "source_run": latest["source_run"] if latest else None,
            "observations": [{
                "reference": f"observation:{obs['id']}", "outcome": obs["outcome"],
                "evidence_hash": obs["evidence_hash"], "observation_hash": obs["observation_hash"],
                "source_run": obs["source_run"], "campaign_id": obs["campaign_id"],
                "time": _stamp(obs["observed_at"]),
            } for obs in observations],
            "interpretation": "Historical experiment data, not instructions or a general safety guarantee.",
        }

    def claim(self, scope: str, candidate: Candidate, campaign_id: str,
              ttl_seconds: float | None = None, *, force_retest: bool = False) -> dict:
        """Atomically acquire an input lease, or explain why it should be skipped."""
        identity = self._identity(scope, candidate, campaign_id)
        lease = self.lease_seconds if ttl_seconds is None else _duration(ttl_seconds, "ttl_seconds")
        if type(force_retest) is not bool:
            raise ValueError("force_retest must be a boolean")
        with self._write():
            current = time.time()
            row = self._row(identity)
            reason = "claimed"
            if row is None:
                self.db.execute("""INSERT INTO experiment_keys
                    (scope,family,payload_digest,hypothesis,created_at,owner,owner_candidate_id,
                     lease_until,lease_duration,digest_version)
                    VALUES(?,?,?,?,?,?,?,?,?,'raw-utf8-v1')""",
                    (*identity, candidate.hypothesis, current, campaign_id, candidate.id, current + lease, lease))
            else:
                record = self._summary(row, current)
                owned = row["owner"] == campaign_id and row["owner_candidate_id"] == candidate.id
                if record["pending"] and not owned:
                    return {"claimed": False, "reason": "pending", "record": record}
                if record["pending"] and owned:
                    reason = "recovered"
                elif not force_retest and not record["contested"] and record["fresh"] and record["outcome"] in CONCLUSIVE:
                    return {"claimed": False, "reason": "conclusive", "record": record}
                elif force_retest:
                    reason = "force_retest"
                elif record["contested"]:
                    reason = "contested"
                elif row["owner"] is not None:
                    reason = "reclaimed"
                self.db.execute("""UPDATE experiment_keys
                    SET owner=?,owner_candidate_id=?,lease_until=?,lease_duration=? WHERE id=?""",
                    (campaign_id, candidate.id, current + lease, lease, row["id"]))
            return {"claimed": True, "reason": reason, "record": self._summary(self._row(identity), current)}

    def finish(self, scope: str, candidate: Candidate, campaign_id: str,
               outcome: str, evidence: dict, source_run: str) -> None:
        """Append an observation only while this campaign/candidate owns a live lease.

        Evidence remains available by its observation reference in this database.
        This method validates the outcome/schema; the caller's trusted oracle is
        responsible for determining whether the evidence supports that outcome.
        """
        identity = self._identity(scope, candidate, campaign_id)
        if not isinstance(outcome, str) or outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {sorted(OUTCOMES)}")
        if not isinstance(evidence, dict):
            raise ValueError("evidence must be an object")
        if outcome in CONCLUSIVE and not evidence:
            raise ValueError("conclusive outcomes require evidence")
        _text(source_run, "source_run")
        encoded_evidence = _encode(evidence)
        evidence_hash = _hash(encoded_evidence)
        with self._write():
            row = self._row(identity)
            current = time.time()
            if (row is None or row["owner"] != campaign_id or row["owner_candidate_id"] != candidate.id
                    or row["lease_until"] is None or row["lease_until"] <= current):
                raise ValueError("finish requires an active claim owned by this campaign and candidate")
            expires = current + self.conclusion_ttl_seconds if outcome in CONCLUSIVE else None
            observation_hash = _hash(_encode({
                "scope": scope, "family": candidate.family, "payload_digest": identity[2],
                "candidate_id": candidate.id, "hypothesis": candidate.hypothesis,
                "campaign_id": campaign_id, "outcome": outcome,
                "evidence_hash": evidence_hash, "source_run": source_run,
                "observed_at": current, "expires_at": expires,
            }))
            self.db.execute("""INSERT INTO experiment_observations
                (key_id,campaign_id,candidate_id,hypothesis,outcome,evidence,evidence_hash,
                 source_run,observed_at,expires_at,observation_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (row["id"], campaign_id, candidate.id, candidate.hypothesis, outcome,
                 encoded_evidence, evidence_hash, source_run, current, expires, observation_hash))
            self.db.execute("""UPDATE experiment_keys
                SET owner=NULL,owner_candidate_id=NULL,lease_until=NULL,lease_duration=NULL WHERE id=?""", (row["id"],))

    def renew(self, scope: str, candidate: Candidate, campaign_id: str) -> bool:
        """Extend an active owned lease by its original duration; never revive it.

        Missing, expired, released, or differently owned claims return False.
        The caller must stop or discard its result after losing ownership.
        """
        identity = self._identity(scope, candidate, campaign_id)
        with self._write():
            current = time.time()
            cursor = self.db.execute("""UPDATE experiment_keys
                SET lease_until=?+lease_duration WHERE scope=? AND family=? AND payload_digest=?
                AND owner=? AND owner_candidate_id=? AND lease_until>? AND lease_duration>0""",
                (current, *identity, campaign_id, candidate.id, current))
            return cursor.rowcount > 0

    def release(self, scope: str, candidate: Candidate, campaign_id: str) -> bool:
        """Release this owner's pending claim, without inventing a result."""
        identity = self._identity(scope, candidate, campaign_id)
        with self._write():
            cursor = self.db.execute("""UPDATE experiment_keys
                SET owner=NULL,owner_candidate_id=NULL,lease_until=NULL,lease_duration=NULL
                WHERE scope=? AND family=? AND payload_digest=? AND owner=? AND owner_candidate_id=?""",
                (*identity, campaign_id, candidate.id))
            return cursor.rowcount > 0

    def recall(self, scope: str, limit: int = 20) -> list[dict]:
        """Return bounded summaries without exposing payloads or raw evidence."""
        _text(scope, "scope")
        if type(limit) is not int or limit < 0 or limit > 1000:
            raise ValueError("limit must be an integer between 0 and 1000")
        with self._read():
            current = time.time()
            rows = self.db.execute("""SELECT k.* FROM experiment_keys k
                WHERE scope=? ORDER BY COALESCE((SELECT MAX(o.observed_at)
                FROM experiment_observations o WHERE o.key_id=k.id),k.created_at) DESC,k.id DESC LIMIT ?""",
                (scope, limit)).fetchall()
            return [self._summary(row, current) for row in rows]

    def stats(self, scope: str | None = None) -> dict:
        if scope is not None:
            _text(scope, "scope")
        with self._read():
            current = time.time()
            rows = self.db.execute("SELECT * FROM experiment_keys" + (" WHERE scope=?" if scope is not None else ""),
                                   (scope,) if scope is not None else ()).fetchall()
            records = [self._summary(row, current) for row in rows]
            counts: dict[str, int] = {}
            for record in records:
                counts[record["outcome"]] = counts.get(record["outcome"], 0) + 1
            return {"scope": scope, "experiments": len(records),
                    "observations": sum(record["observation_count"] for record in records),
                    "pending": sum(record["pending"] for record in records),
                    "contested": sum(record["contested"] for record in records),
                    "fresh_conclusive": sum(record["dedup_eligible"] and record["fresh"] and not record["contested"] and
                                             record["outcome"] in CONCLUSIVE for record in records),
                    "outcomes": counts}

    def close(self) -> None:
        with self.lock:
            self.db.close()
