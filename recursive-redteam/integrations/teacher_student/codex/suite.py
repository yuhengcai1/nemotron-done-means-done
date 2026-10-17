"""Teach a reproduced lease-generation bug from a frozen production snapshot.

Student probes are bounded JSON; this module executes only trusted local Python
and fresh temporary SQLite stores. The clock is deterministic and connection
identities are held separately from campaign/candidate identities.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from recursive_redteam.contracts import Candidate

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "frozen_memory.py"
SIGNATURE = "stale_lease_generation_accepted"


def _source():
    manifest = json.loads((ROOT / "source_manifest.json").read_text())
    observed = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    if observed != manifest["source_sha256"]:
        raise ValueError("frozen memory source hash mismatch")
    spec = importlib.util.spec_from_file_location("recursive_redteam._teacher_codex_frozen_memory", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, observed


def exercise(module, probe):
    """Exercise public APIs only; usable against both frozen and repaired source."""
    current = [100.0]
    trace = []
    lease = probe["lease_seconds"]
    original = Candidate("attempt", "lease-generation", "A retired attempt cannot mutate a successor lease", "fixed input")
    replacement = Candidate("attempt" if probe["reuse_identity"] else "successor", original.family,
                            original.hypothesis, original.payload)
    intermediate = Candidate("intermediate", original.family, original.hypothesis, original.payload)

    def record(actor, operation, result):
        trace.append({"at": current[0], "actor": actor, "operation": operation,
                      "accepted": result.get("claimed") if isinstance(result, dict) else result})
        return result

    with TemporaryDirectory(prefix="teacher-lease-") as directory:
        with patch.object(module, "time", SimpleNamespace(time=lambda: current[0])):
            old = module.ExperimentMemory(Path(directory) / "memory.sqlite", lease_seconds=lease)
            new = module.ExperimentMemory(Path(directory) / "memory.sqlite", lease_seconds=lease)
            other = module.ExperimentMemory(Path(directory) / "memory.sqlite", lease_seconds=lease)
            try:
                record("old", "claim", old.claim("scope", original, "campaign"))
                retirement = probe["reacquisition"]
                if retirement in {"after_expiry", "after_other_owner"}:
                    current[0] += lease
                    retired = not record("old", "renew_at_expiry", old.renew("scope", original, "campaign"))
                elif retirement == "after_release":
                    current[0] += 1
                    retired = record("old", "release_original", old.release("scope", original, "campaign"))
                else:
                    raise ValueError("unsupported retirement")
                if retirement == "after_other_owner":
                    current[0] += 1
                    record("intermediate", "claim", other.claim("scope", intermediate, "other-campaign"))
                    record("intermediate", "release", other.release("scope", intermediate, "other-campaign"))
                current[0] += 1
                acquired = record("replacement", "claim", new.claim("scope", replacement, "campaign"))["claimed"]
                before = new.recall("scope")[0]
                current[0] += 1
                action = probe["stale_action"]
                if action == "finish":
                    try:
                        old.finish("scope", original, "campaign", "no_violation",
                                   {"origin": "retired worker"}, "retired-worker-run")
                        accepted = True
                    except ValueError:
                        accepted = False
                else:
                    accepted = getattr(old, action)("scope", original, "campaign")
                record("old", action, accepted)
                after = new.recall("scope")[0]
                successor_live = record("replacement", "renew_after_stale_action",
                                        new.renew("scope", replacement, "campaign"))
                return {"retired_original_lease": retired, "replacement_claimed": acquired,
                        "stale_operation_accepted": accepted,
                        "replacement_lease_live_after_action": successor_live,
                        "observations_written": new.stats("scope")["observations"],
                        "lease_until_before": before["lease_until"], "lease_until_after": after["lease_until"],
                        "trace": trace}
            finally:
                old.close()
                new.close()
                other.close()


class Suite:
    def describe(self, split):
        return {"suite": "lease-generation-lifecycle", "split": split,
                "scope": "Frozen real production ExperimentMemory; local deterministic SQLite experiments, no remote target.",
                "invariant": "An attempt whose lease expired or was released cannot renew, finish, or release a later attempt's lease, even if campaign and candidate names are reused.",
                "probe_schema": {
                    "type": "object",
                    "properties": {
                        "reacquisition": {"type": "string", "enum": ["after_expiry"] if split == "train"
                                          else ["after_release", "after_other_owner"]},
                        "stale_action": {"type": "string", "enum": ["finish"] if split == "train"
                                         else ["renew", "release"]},
                        "reuse_identity": {"type": "boolean"},
                        "lease_seconds": {"type": "integer", "minimum": 2, "maximum": 60}},
                    "required": ["reacquisition", "stale_action", "reuse_identity", "lease_seconds"],
                    "additionalProperties": False},
                "experiment": "Two independent connections represent old and replacement workers. The original lease is retired, a replacement claims the same input, and the old connection attempts the selected operation. An optional intermediate owner introduces a separate ownership generation.",
                "oracle": "Measure operation acceptance, persisted observations, and whether the replacement still owns its lease.",
                "source_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest()}

    def lesson(self):
        return {"teacher": "Codex collaborative agent /root/codex_teacher, independently executed Python reproduction",
                "preconditions": ["Durable ownership can expire and later be reacquired.",
                                  "Different executions may reuse the same logical identity."],
                "search_strategy": ["Distinguish the identity of a logical task from the identity of one acquisition attempt.",
                                    "Keep the earlier worker alive after ending its ownership, then acquire a successor lease from another connection.",
                                    "Test whether an operation from the earlier worker changes the successor's state.",
                                    "Use distinct successor identities as a paired control, and compare multiple lifecycle transitions and operations."],
                "oracle": "A retired attempt must not regain mutation rights merely because its logical names reappear. Check persisted state and the successor's ownership, not only a returned status.",
                "nonexamples": ["A still-active attempt renewing its own acquisition is valid.",
                                "A new attempt claiming after expiry is valid; acceptance of a stale attempt is the defect.",
                                "A local deterministic lease test does not establish a hosted model behavior."],
                "scope": "External-memory instruction, no model weight update; frozen pre-fix code."}

    def teacher_probes(self):
        return [{"reacquisition": "after_expiry", "stale_action": "finish", "reuse_identity": True, "lease_seconds": 10}]

    def evaluate(self, probe, *, split):
        schema = self.describe(split)["probe_schema"]
        properties = schema["properties"]
        if (not isinstance(probe, dict) or set(probe) != set(schema["required"])
                or probe.get("reacquisition") not in properties["reacquisition"]["enum"]
                or probe.get("stale_action") not in properties["stale_action"]["enum"]
                or type(probe.get("reuse_identity")) is not bool
                or type(probe.get("lease_seconds")) is not int
                or not properties["lease_seconds"]["minimum"] <= probe["lease_seconds"] <= properties["lease_seconds"]["maximum"]):
            return {"status": "error", "signature": "", "measurements": {"reason": "invalid bounded probe"}}
        module, source_hash = _source()
        result = exercise(module, probe)
        if not result["retired_original_lease"] or not result["replacement_claimed"]:
            return {"status": "error", "signature": "", "measurements": result}
        failed = result["stale_operation_accepted"]
        return {"status": "violation" if failed else "passed", "signature": SIGNATURE if failed else "",
                "measurements": {**result, "snapshot_sha256": source_hash,
                                 "fixture_scope": "deterministic clock and temporary SQLite, actual frozen framework APIs"}}

    def control(self, probe, *, split):
        return {**deepcopy(probe), "reuse_identity": False}

    def novelty(self, probe):
        valid = (probe.get("reacquisition") in {"after_release", "after_other_owner"}
                 and probe.get("stale_action") in {"renew", "release"})
        return {"valid": valid, "reason": "Heldout changes both the ownership lifecycle and the stale operation; training uses expiry followed by finish."}


def build_suite():
    return Suite()
