"""Recall an exact-source repair from verified memory, then retest it without inference."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from examples.self_debug import tests as run_checks, validate_edit


def digest(value):
    return hashlib.sha256(value).hexdigest()


def recall(run: Path):
    run = Path(run).resolve()
    database = run.parent / "self-debug-memory.sqlite"
    result = {"schema_version": 1, "run_id": run.name, "matched": False, "reusable": False,
              "chain_valid": None, "hosted_model_calls": 0, "checks": [], "source_hashes": {},
              "mode": "durable memory recall with local verification",
              "scope": "Exact original source and retained contract checks only",
              "checked_at": datetime.now(timezone.utc).isoformat(),
              "memory_source": str(database), "recalled_source_origin": None}
    try:
        report = json.loads((run / "report.json").read_text())
        if not isinstance(report, dict) or any(not isinstance(report.get(key, {}), dict)
                                             for key in ("memory", "verification", "after", "artifact_hashes")):
            raise ValueError("The recorded report has malformed evidence fields")
        receipt = report.get("memory", {})
        if report.get("status") != "verified" or report.get("verification", {}).get("verified") is not True:
            raise ValueError("The recorded run has no verified repair")
        if receipt.get("stored") is not True:
            raise ValueError("The run has no stored memory receipt")
        entry_id, event_hash = receipt.get("entry_id"), receipt.get("event_hash")
        if type(entry_id) is not int or entry_id < 1 or not isinstance(event_hash, str):
            raise ValueError("The memory receipt is incomplete")
        # The location is fixed relative to the supplied run; do not follow a path in report data.
        db = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        try:
            rows = db.execute("SELECT id,body,previous_hash,event_hash FROM repair_memory ORDER BY id").fetchall()
        finally:
            db.close()
        result["chain_valid"] = False
        previous, selected = "", None
        for row_id, body, prior, current in rows:
            if not all(isinstance(v, str) for v in (body, prior, current)):
                raise ValueError("Memory chain contains malformed values")
            if prior != previous or digest((prior + body).encode()) != current:
                raise ValueError("Memory hash chain is invalid")
            previous = current
            if row_id == entry_id:
                if current != event_hash:
                    raise ValueError("Memory event hash does not match the run receipt")
                selected = json.loads(body)
        result["chain_valid"] = True
        if not isinstance(selected, dict):
            raise ValueError("The memory entry referenced by the run is missing")
        if selected.get("run_id") != run.name or report.get("run_id") != run.name:
            raise ValueError("Memory run identity does not match")
        if selected.get("model") != report.get("model"):
            raise ValueError("Memory model identity does not match")
        if selected.get("scope") != "Exact source repair and retained contract checks":
            raise ValueError("Memory scope is not supported")
        if (not isinstance(selected.get("evidence_ids"), list) or len(selected["evidence_ids"]) < 2
                or selected.get("evidence_ids") != report.get("verification", {}).get("evidence_ids")):
            raise ValueError("Memory verification receipts do not match")
        if "entry" in receipt and selected != receipt["entry"]:
            raise ValueError("Stored memory body does not match its recorded receipt")
        required = ("before/targets.py", "before/contracts.py", "before/__init__.py", "repair_checks.py")
        manifest = report.get("artifact_hashes", {})
        retained = {relative: (run / relative).read_bytes() for relative in required}
        for relative in required:
            actual = digest(retained[relative])
            result["source_hashes"][relative] = actual
            if manifest.get(relative) != actual:
                raise ValueError("Retained artifact hash mismatch: " + relative)
        original = retained["before/targets.py"].decode("utf-8")
        if selected.get("before_sha256") != digest(original.encode()):
            raise ValueError("Memory original-source hash does not match")
        if selected.get("test_checker_sha256") != digest(retained["repair_checks.py"]):
            raise ValueError("Memory test-checker hash does not match")
        # The proposed repair is retrieved from SQLite, never from report.after.source.
        recalled_source = selected.get("after_source")
        result["recalled_source_origin"] = "repair_memory.body.after_source"
        if not isinstance(recalled_source, str) or not recalled_source:
            raise ValueError("Memory has no retained repair source")
        after_hash = digest(recalled_source.encode())
        result["source_hashes"]["recalled_after_source"] = after_hash
        if selected.get("after_sha256") != after_hash or report.get("after", {}).get("source_sha256") != after_hash:
            raise ValueError("Memory repair-source hash does not match")
        validate_edit(original, recalled_source)
        result["matched"] = True
        result["entry_id"], result["event_hash"] = entry_id, event_hash
        with tempfile.TemporaryDirectory(prefix="recursive-memory-recall-") as directory:
            package = Path(directory) / "recursive_redteam"
            package.mkdir()
            for name in ("__init__.py", "contracts.py"):
                (package / name).write_bytes(retained["before/" + name])
            checker = Path(directory) / "repair_checks.py"
            checker.write_bytes(retained["repair_checks.py"])
            source = package / "targets.py"
            source.write_text(recalled_source)
            result["checks"] = [run_checks(source, checker), run_checks(source, checker)]
        result["reusable"] = all(check.get("failed") == 0 and check.get("total", 0) > 0
                                 and check.get("passed") == check.get("total") for check in result["checks"])
        if not result["reusable"]:
            result["error"] = "The recalled repair did not pass both fresh checks"
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, subprocess.SubprocessError, SyntaxError, StopIteration) as exc:
        result["error"] = str(exc) or type(exc).__name__
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.out and (args.out.resolve() == args.run.resolve() / "report.json"
                     or args.out.resolve() == args.run.resolve().parent / "self-debug-memory.sqlite"):
        parser.error("--out must not overwrite the source report or memory database")
    result = recall(args.run)
    content = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(content)
    print(content, end="")
    return 0 if result["reusable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
