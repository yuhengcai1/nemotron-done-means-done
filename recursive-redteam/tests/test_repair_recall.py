from __future__ import annotations

import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from examples.recall_repair import digest, recall
from examples.self_debug import CHECKER, FROZEN, ROOT, remember


class RepairRecallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.run = self.base / "test-recorded-repair"
        (self.run / "before").mkdir(parents=True)
        shutil.copyfile(FROZEN, self.run / "before/targets.py")
        shutil.copyfile(ROOT / "recursive_redteam/contracts.py", self.run / "before/contracts.py")
        (self.run / "before/__init__.py").write_text("")
        shutil.copyfile(CHECKER, self.run / "repair_checks.py")
        original = (self.run / "before/targets.py").read_text()
        corrected = (ROOT / "recursive_redteam/targets.py").read_text()
        self.entry = {"run_id": self.run.name, "before_sha256": digest(original.encode()),
                      "after_sha256": digest(corrected.encode()), "test_checker_sha256": digest(CHECKER.read_bytes()),
                      "model": "test-model", "note": "test receipt", "evidence_ids": ["a", "b"],
                      "scope": "Exact source repair and retained contract checks", "patch": "retained diff",
                      "after_source": corrected}
        self.db = self.base / "self-debug-memory.sqlite"
        self.receipt = remember(self.db, self.entry)
        self.report = {"run_id": self.run.name, "status": "verified", "model": "test-model",
                       "verification": {"verified": True, "evidence_ids": ["a", "b"]},
                       "memory": self.receipt, "after": {"source": "DO NOT LOAD THIS STRING", "source_sha256": self.entry["after_sha256"]},
                       "artifact_hashes": {name: digest((self.run / name).read_bytes()) for name in
                           ("before/targets.py", "before/contracts.py", "before/__init__.py", "repair_checks.py")}}
        self.save()

    def save(self):
        (self.run / "report.json").write_text(json.dumps(self.report))

    def test_recall_reads_memory_source_and_runs_two_fresh_checks_without_writing_database(self):
        before = self.db.read_bytes()
        result = recall(self.run)
        self.assertTrue(result["matched"], result)
        self.assertTrue(result["reusable"], result)
        self.assertEqual(result["hosted_model_calls"], 0)
        self.assertEqual(len(result["checks"]), 2)
        self.assertTrue(all(check["passed"] == check["total"] == 22 for check in result["checks"]))
        self.assertEqual(before, self.db.read_bytes())
        self.assertEqual(result["recalled_source_origin"], "repair_memory.body.after_source")

    def test_tampered_memory_chain_is_rejected_before_execution(self):
        db = sqlite3.connect(self.db)
        db.execute("DROP TRIGGER repair_memory_no_update")
        db.execute("UPDATE repair_memory SET body='{}' WHERE id=1")
        db.commit()
        db.close()
        with patch("examples.recall_repair.run_checks") as checks:
            result = recall(self.run)
            self.assertFalse(result["reusable"])
            self.assertFalse(result["chain_valid"])
            checks.assert_not_called()

    def test_missing_database_does_not_create_one(self):
        self.db.unlink()
        result = recall(self.run)
        self.assertFalse(result["reusable"])
        self.assertFalse(self.db.exists())

    def test_missing_entry_is_rejected(self):
        self.report["memory"]["entry_id"] = 99
        self.save()
        result = recall(self.run)
        self.assertFalse(result["matched"])
        self.assertIn("missing", result["error"])

    def test_receipt_hash_mismatch_is_rejected(self):
        self.report["memory"]["event_hash"] = "0" * 64
        self.save()
        result = recall(self.run)
        self.assertFalse(result["matched"])
        self.assertIn("event hash", result["error"])

    def test_changed_original_source_is_rejected_before_execution(self):
        source = self.run / "before/targets.py"
        source.write_text(source.read_text() + "\n# changed\n")
        with patch("examples.recall_repair.run_checks") as checks:
            result = recall(self.run)
            self.assertFalse(result["matched"])
            self.assertIn("hash mismatch", result["error"])
            checks.assert_not_called()

    def test_changed_checker_cannot_be_authorized_by_editing_only_report_manifest(self):
        checker = self.run / "repair_checks.py"
        checker.write_text(checker.read_text() + "\n# changed\n")
        self.report["artifact_hashes"]["repair_checks.py"] = digest(checker.read_bytes())
        self.save()
        result = recall(self.run)
        self.assertFalse(result["matched"])
        self.assertIn("test-checker hash", result["error"])

    def test_malformed_report_is_a_failed_receipt(self):
        self.report["verification"] = []
        self.save()
        result = recall(self.run)
        self.assertFalse(result["reusable"])
        self.assertIn("malformed", result["error"])

    def test_unqualified_run_cannot_reuse_memory(self):
        self.report["status"] = "inconclusive"
        self.save()
        result = recall(self.run)
        self.assertFalse(result["matched"])
        self.assertFalse(result["reusable"])

    def test_failing_recalled_tests_do_not_qualify(self):
        with patch("examples.recall_repair.run_checks", return_value={"passed": 21, "failed": 1, "total": 22}):
            result = recall(self.run)
        self.assertTrue(result["matched"])
        self.assertFalse(result["reusable"])
        self.assertEqual(len(result["checks"]), 2)


if __name__ == "__main__":
    unittest.main()
