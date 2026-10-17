"""Thread-safe SQLite journal. Reservations and state changes commit before work."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any

from .contracts import BudgetExceeded, Candidate, TrialResult


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,round INTEGER NOT NULL,
                status TEXT NOT NULL,data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS candidates(id TEXT PRIMARY KEY,digest TEXT UNIQUE NOT NULL,data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS trials(id TEXT PRIMARY KEY,status TEXT NOT NULL,result TEXT);
            CREATE TABLE IF NOT EXISTS findings(id TEXT PRIMARY KEY,data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,time TEXT NOT NULL,
                kind TEXT NOT NULL,data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS model_calls(id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT,status TEXT NOT NULL,usage TEXT,error TEXT);
        """)
        self.db.commit()
        # Existing run databases remain readable and resumable. Acquire a write
        # lock before inspecting columns so simultaneous openers migrate once.
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(model_calls)")}
            for name in ("request", "response", "actual_model", "started_at", "finished_at"):
                if name not in columns:
                    self.db.execute(f"ALTER TABLE model_calls ADD COLUMN {name} TEXT")

    def close(self) -> None:
        with self.lock:
            self.db.close()

    def set_metadata(self, key: str, value: Any) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", (key, encode(value)))

    def get_metadata(self, key: str, default: Any = None) -> Any:
        with self.lock:
            row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def event(self, kind: str, data: dict) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT INTO events(time,kind,data) VALUES(?,?,?)", (now(), kind, encode(data)))

    def add_tasks(self, tasks: list[dict], round_number: int) -> None:
        with self.lock, self.db:
            for task in tasks:
                self.db.execute("INSERT INTO tasks VALUES(?,?,?,?)", (task["id"], round_number, "queued", encode(task)))
                self.db.execute("INSERT INTO events(time,kind,data) VALUES(?,?,?)", (now(), "task_spawned", encode(task)))
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", ("active_round", encode(round_number)))

    def tasks(self, *, round_number: int | None = None) -> list[dict]:
        with self.lock:
            if round_number is None:
                rows = self.db.execute("SELECT * FROM tasks ORDER BY rowid").fetchall()
            else:
                rows = self.db.execute("SELECT * FROM tasks WHERE round=? ORDER BY rowid", (round_number,)).fetchall()
            return [dict(json.loads(row["data"]), status=row["status"], round=row["round"]) for row in rows]

    def update_task(self, task_id: str, status: str, **changes: Any) -> None:
        with self.lock, self.db:
            row = self.db.execute("SELECT data FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise ValueError("task does not exist")
            data = json.loads(row[0])
            data.update(changes)
            self.db.execute("UPDATE tasks SET status=?,data=? WHERE id=?", (status, encode(data), task_id))

    def save_candidate(self, candidate: Candidate) -> bool:
        digest = hashlib.sha256(encode([candidate.family, candidate.payload]).encode()).hexdigest()
        with self.lock, self.db:
            try:
                self.db.execute("INSERT INTO candidates VALUES(?,?,?)", (candidate.id, digest, encode(candidate.to_dict())))
            except sqlite3.IntegrityError:
                return False
            return True

    def candidate(self, candidate_id: str) -> Candidate | None:
        with self.lock:
            row = self.db.execute("SELECT data FROM candidates WHERE id=?", (candidate_id,)).fetchone()
            return Candidate(**json.loads(row[0])) if row else None

    def trial(self, trial_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM trials WHERE id=?", (trial_id,)).fetchone()
            return {"status": row["status"], "result": json.loads(row["result"]) if row["result"] else None} if row else None

    def begin_trial(self, trial_id: str) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO trials VALUES(?,?,NULL)", (trial_id, "running"))

    def finish_trial(self, trial_id: str, result: TrialResult) -> None:
        with self.lock, self.db:
            self.db.execute("UPDATE trials SET status='done',result=? WHERE id=?", (encode(result.to_dict()), trial_id))
            self.db.execute("INSERT INTO events(time,kind,data) VALUES(?,?,?)", (now(), "trial_finished",
                            encode({"trial_id": trial_id, "status": result.status, "result": result.to_dict()})))

    def finding(self, candidate_id: str, data: dict) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO findings VALUES(?,?)", (candidate_id, encode(data)))

    def reserve_model_call(self, max_calls: int, *, role: str | None = None,
                           request: dict | None = None) -> int:
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            count = self.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0]
            if count >= max_calls:
                raise BudgetExceeded("campaign model-call budget exhausted")
            cursor = self.db.execute("""INSERT INTO model_calls(role,status,request,started_at)
                VALUES(?,'reserved',?,?)""", (role, encode(request) if request is not None else None, now()))
            return int(cursor.lastrowid)

    def finish_model_call(self, call_id: int, role: str, usage: dict, error: str | None = None,
                          *, response: dict | None = None, actual_model: str | None = None) -> None:
        with self.lock, self.db:
            self.db.execute("""UPDATE model_calls SET role=?,status=?,usage=?,error=?,
                response=?,actual_model=?,finished_at=? WHERE id=?""",
                (role, "error" if error else "done", encode(usage), error,
                 encode(response) if response is not None else None, actual_model, now(), call_id))

    def report(self) -> dict:
        with self.lock:
            metadata = {row[0]: json.loads(row[1]) for row in self.db.execute("SELECT key,value FROM metadata")}
            calls = [dict(row) for row in self.db.execute("SELECT * FROM model_calls ORDER BY id")]
            usage: Counter = Counter()
            for call in calls:
                call["usage"] = json.loads(call["usage"]) if call["usage"] else {}
                call["request"] = json.loads(call["request"]) if call["request"] else None
                call["response"] = json.loads(call["response"]) if call["response"] else None
                for key, value in call["usage"].items():
                    if type(value) is int and value >= 0:
                        usage[key] += value
            findings = [json.loads(row[0]) for row in self.db.execute("SELECT data FROM findings ORDER BY rowid")]
            trials = [{"id": row["id"], "status": row["status"],
                       "result": json.loads(row["result"]) if row["result"] else None}
                      for row in self.db.execute("SELECT * FROM trials ORDER BY rowid")]
            events = [{"id": row["id"], "time": row["time"], "kind": row["kind"], "data": json.loads(row["data"])}
                      for row in self.db.execute("SELECT * FROM events ORDER BY id")]
            return {"schema_version": 1, "status": metadata.get("status", "created"),
                    "simulated": metadata.get("target", {}).get("simulated", False), "metadata": metadata,
                    "tasks": self.tasks(), "trials": trials, "findings": findings, "events": events,
                    "model_usage": {"logical_calls": len(calls), "reported_tokens": dict(usage),
                                    "unknown_usage_calls": sum(not x["usage"] for x in calls),
                                    "note": "Logical calls include failures. Provider retries are separately bounded; token totals include only reported usage."},
                    "model_calls": calls,
                    "counts": {"tasks": len(self.tasks()), "trials": len(trials),
                               "reproduced": sum(x["status"] == "reproduced" for x in findings),
                               "distinct_oracle_signatures": len({tuple(s) for x in findings if x["status"] == "reproduced"
                                                                   for s in x.get("failure_signatures", [])}),
                               "inconclusive": sum(x.get("outcome") == "inconclusive" for x in self.tasks())}}
