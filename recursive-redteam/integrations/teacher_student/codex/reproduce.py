"""Independently replay the frozen finding and compare repaired public APIs."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from recursive_redteam import memory
from recursive_redteam.teaching import verify_teacher
from .suite import build_suite, exercise


def reproduce():
    suite = build_suite()
    frozen = verify_teacher(suite)
    current = [exercise(memory, probe) for probe in suite.teacher_probes()]
    fixed = all(not item["stale_operation_accepted"] and item["replacement_lease_live_after_action"]
                and item["observations_written"] == 0 for item in current)
    return {"schema_version": 1, "teacher": "Codex collaborative agent /root/codex_teacher",
            "actual_teacher_execution": "This finding was produced and reproduced by the collaborative Codex session; no Astra authorship is asserted.",
            "frozen_finding_verified": frozen["verified"], "teacher_verification": frozen,
            "current_source_sha256": hashlib.sha256(Path(memory.__file__).read_bytes()).hexdigest(),
            "current_source_rejects_stale_finish": fixed, "current_measurements": current,
            "scope": "Local framework correctness; deterministic clock, real SQLite transactions and public APIs. No remote target or hosted model calls."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = reproduce()
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded)
    print(encoded, end="")
    return 0 if report["frozen_finding_verified"] and report["current_source_rejects_stale_finish"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
