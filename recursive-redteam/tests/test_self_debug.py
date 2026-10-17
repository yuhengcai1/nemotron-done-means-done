from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from examples.self_debug import CHECKER, FROZEN, ROOT, ResumeModel, remember, tests as run_checks, validate_edit
from recursive_redteam.budget import _request_snapshot
from recursive_redteam.contracts import Completion


class SelfDebugBoundaries(unittest.TestCase):
    def setUp(self):
        self.original = FROZEN.read_text()
        self.corrected = (ROOT / "recursive_redteam/targets.py").read_text()

    def test_correct_result_aggregation_is_permitted(self):
        validate_edit(self.original, self.corrected)

    def test_other_code_cannot_change(self):
        with self.assertRaises(ValueError):
            validate_edit(self.original, self.corrected.replace('secrets.token_hex(12)', 'secrets.token_hex(8)'))

    def test_new_callable_is_rejected(self):
        altered = self.corrected.replace('return TrialResult(status, result_evidence, trace, dict(metrics), error=error)',
                                         'return eval("1")')
        with self.assertRaises(ValueError):
            validate_edit(self.original, altered)

    def test_evidence_mutation_is_rejected(self):
        altered = self.corrected.replace('result_evidence = list(evidence)', 'evidence.clear()\n            result_evidence = list(evidence)')
        with self.assertRaises(ValueError):
            validate_edit(self.original, altered)

    def test_signature_change_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_edit(self.original, self.corrected.replace('def finish(status: str', 'def finish(status: int'))

    def test_checks_distinguish_failed_repair_and_preserve_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "__init__.py").write_text("")
            shutil.copyfile(ROOT / "recursive_redteam/contracts.py", root / "contracts.py")
            source = root / "targets.py"
            source.write_text(self.original)
            before = run_checks(source, CHECKER)
            self.assertEqual(before["failed"], 7)
            source.write_text(self.corrected)
            after = run_checks(source, CHECKER)
            self.assertEqual(after["failed"], 0)
            self.assertEqual(after["total"], 22)
            self.assertTrue(all(c["passed"] for c in after["cases"][-2:]))

    def test_memory_is_append_only_and_linked(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.sqlite"
            first = remember(path, {"source": "a"})
            second = remember(path, {"source": "b"})
            db = sqlite3.connect(path)
            try:
                self.assertEqual(db.execute("SELECT previous_hash FROM repair_memory WHERE id=2").fetchone()[0], first["event_hash"])
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("UPDATE repair_memory SET body='{}' WHERE id=1")
            finally:
                db.close()
            self.assertTrue(second["chain_valid"])

    def test_memory_detects_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.sqlite"
            remember(path, {"source": "a"})
            db = sqlite3.connect(path)
            db.execute("DROP TRIGGER repair_memory_no_update")
            db.execute("UPDATE repair_memory SET body='{}'")
            db.commit()
            db.close()
            with self.assertRaises(ValueError):
                remember(path, {"source": "b"})

    def test_resume_reuses_only_exact_completed_role_request_once(self):
        class Provider:
            calls = 0
            def complete(self, *args, **kwargs):
                self.calls += 1
                return Completion(content='{"fresh":true}', model="test-model")
        provider = Provider()
        messages = [{"role": "user", "content": "original source"}]
        call = {"id": 1, "role": "repair_worker", "status": "done",
                "request": _request_snapshot(messages, None, True),
                "response": {"content": '{"cached":true}', "model": "test-model", "finish_reason": "stop"}}
        resumed = ResumeModel(provider, [call], "repair_worker")
        self.assertEqual(resumed.complete(messages, json_mode=True).content, '{"cached":true}')
        self.assertEqual(provider.calls, 0)
        self.assertEqual(resumed.hits, 1)
        resumed.complete(messages, json_mode=True)
        self.assertEqual(provider.calls, 1)
        wrong_role = ResumeModel(provider, [call], "repair_reviewer")
        wrong_role.complete(messages, json_mode=True)
        self.assertEqual(provider.calls, 2)
        changed = ResumeModel(provider, [call], "repair_worker")
        changed.complete([{"role": "user", "content": "changed source"}], json_mode=True)
        self.assertEqual(provider.calls, 3)


if __name__ == "__main__":
    unittest.main()
