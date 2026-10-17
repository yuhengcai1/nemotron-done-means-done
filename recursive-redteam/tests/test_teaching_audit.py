import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from examples.audit_teaching import audit
from recursive_redteam.teaching import digest, verify_teacher
from test_teaching import FakeSuite


class AuditTests(unittest.TestCase):
    def build(self, root, *, transfer=False, episodes=None, baseline=False, taught=False):
        path = root / "course"
        source = path / "source_snapshot/fixture.py"
        source.parent.mkdir(parents=True)
        source.write_text("# pinned fixture\n")
        lesson = FakeSuite().lesson()
        identity = {"source_manifest": {"fixture.py": hashlib.sha256(source.read_bytes()).hexdigest()}, "lesson": digest(lesson)}
        result = {"scope": digest(identity), "sources_unchanged": True, "teacher": verify_teacher(FakeSuite()),
                  "transfer_verified": transfer, "episodes": episodes or [],
                  "baseline_verified": baseline, "taught_verified": taught}
        files = {"course/identity.json": identity, "course/lesson.json": lesson,
                 "report.json": {"live_student": True, "suites": {"course": result}},
                 "model-journal.json": {"model_calls": [], "model_usage": {"logical_calls": 0}},
                 "config.json": {"planner": {"model": "fixture"}}}
        for filename, data in files.items():
            (root / filename).write_text(json.dumps(data))

    def test_unsupported_top_level_transfer_claim_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.build(root, transfer=True)
            result = audit(root)
            self.assertFalse(result["evidence_valid"])
            self.assertFalse(result["suites"]["course"]["checks"]["transfer_status_supported"])

    def test_failed_transport_baseline_cannot_show_paired_improvement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.build(root, taught=True, episodes=[{"condition": "baseline", "verified": False, "error": "ProviderError"}])
            result = audit(root)["suites"]["course"]
            self.assertFalse(result["baseline_comparable"])
            self.assertFalse(result["paired_outcome_improved"])

    def test_modified_frozen_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.build(root)
            (root / "course/source_snapshot/fixture.py").write_text("changed")
            self.assertFalse(audit(root)["evidence_valid"])
