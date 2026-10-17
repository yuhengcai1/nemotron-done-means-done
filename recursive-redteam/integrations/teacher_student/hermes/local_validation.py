"""Teacher reproduction and adversarial protocol checks with no live student."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from .freeze import verify_packet
from .suite import ROOT, build_suite, source_hashes


def independently_check_probe(suite, probe, *, split):
    """Fresh host reruns, never accepting a student's claimed status as evidence."""
    first = suite.evaluate(probe, split=split)
    if first["status"] != "violation":
        return {"finding_verified": False, "initial": first, "replays": [], "control": None,
                "observation": "inconclusive" if first["status"] == "error" else "scoped_negative"}
    replays = [suite.evaluate(probe, split=split) for _ in range(2)]
    control_probe = suite.control(probe, split=split)
    control = suite.evaluate(control_probe, split=split)
    verified = (all(r["status"] == "violation" and r["signature"] == first["signature"] for r in replays)
                and bool(first["signature"]) and control["status"] == "passed")
    return {"finding_verified": verified, "initial": first, "replays": replays,
            "control_probe": control_probe, "control": control,
            "observation": "verified_synthetic_counterexample" if verified else "inconclusive"}


def run_validation(packet_path=None):
    suite = build_suite()
    teacher = [{"probe": probe, **independently_check_probe(suite, probe, split="train")}
               for probe in suite.teacher_probes()]
    # These are fixed local test inputs, not generated student outputs. Keep this
    # host-side file and resulting artifacts out of the student's lesson context.
    fixture_probes = [
        {"target": "case-31", "fault": "after_commit"},
        {"target": "case-42", "fault": "after_commit", "read_view": "lagged"},
        {"target": "case-53", "fault": "after_commit"},
        {"target": "case-64", "fault": "after_commit"},
        {"target": "case-75", "fault": "after_commit", "read_view": "lagged"},
        {"target": "case-31", "fault": "before_commit"},
        {"target": "case-42", "fault": "before_commit", "read_view": "lagged"},
        {"target": "case-53", "fault": "before_commit"},
        {"target": "case-31", "fault": "after_commit", "transport": "fail"},
    ]
    fixtures = []
    for probe in fixture_probes:
        novelty = suite.novelty(probe)
        checked = independently_check_probe(suite, probe, split="holdout")
        fixtures.append({"probe": probe, "actor": "offline_scripted_probe_fixture", "novelty": novelty,
                         **checked, "transfer_claim": False})
    frozen = verify_packet(json.loads(packet_path.read_text())) if packet_path else None
    report = {"schema_version": 1, "classification": "offline_teaching_interface_validation",
        "teacher_role": "Codex-authored and selected local probes", "teacher_checkpoint": "not exposed",
        "new_live_calls": 0, "live_student_evaluated": False, "student_learning_claim": False,
        "source_hashes": source_hashes(),
        "validation_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "frozen_historical_evidence": frozen, "teacher_training": teacher, "offline_fixture_checks": fixtures,
        "counts": {"teacher_verified_synthetic_cases": sum(t["finding_verified"] for t in teacher),
                   "heldout_fixture_counterexamples": sum(f["finding_verified"] for f in fixtures),
                   "heldout_fixture_scoped_negatives": sum(f["observation"] == "scoped_negative" for f in fixtures),
                   "heldout_fixture_inconclusive": sum(f["observation"] == "inconclusive" for f in fixtures)},
        "limits": ["Historical native evidence and current policy fixtures are distinct evidence classes.",
                   "These scripted probe selections test interface and host oracle, not Nemotron learning or transfer.",
                   "Main runner must use fresh model-authored probes, enforce novelty and repeat/control checks, and retain model/source identities."]}
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet", type=Path, default=ROOT / "evidence/frozen-native.json")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = run_validation(args.packet)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x") as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"classification": report["classification"], "new_live_calls": 0,
                      "live_student_evaluated": False, "counts": report["counts"],
                      "frozen_verified": report["frozen_historical_evidence"]["verified"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
