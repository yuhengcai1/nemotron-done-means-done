from __future__ import annotations

from email.message import Message
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "demo"))
import build_data
import server


def request(method, path, body=b"", headers=None, run_id=server.RUN_ID):
    handler = object.__new__(server.DemoHandler)
    handler.server = SimpleNamespace(server_port=8765, run_id=run_id)
    handler.headers = Message()
    fields = {"Host": "127.0.0.1:8765", "Content-Length": str(len(body))}
    fields.update(headers or {})
    for key, value in fields.items():
        handler.headers[key] = value
    handler.rfile, handler.wfile = BytesIO(body), BytesIO()
    handler.path, handler.command = path, method
    handler.request_version = "HTTP/1.1"
    handler.requestline = method + " " + path + " HTTP/1.1"
    handler.log_message = lambda *args: None
    getattr(handler, "do_" + method)()
    head, content = handler.wfile.getvalue().split(b"\r\n\r\n", 1)
    return int(head.split(b" ")[1]), head, content


class DemoServerTests(unittest.TestCase):
    def test_clean_clone_resolves_bundled_evidence_and_validates_authorship(self):
        bundle = ROOT / "demo/recordings" / build_data.RUN_ID
        with tempfile.TemporaryDirectory() as folder:
            clean_root = Path(folder)
            destination = clean_root / "demo/recordings" / build_data.RUN_ID
            import shutil
            shutil.copytree(bundle, destination)
            with patch.object(build_data, "ROOT", clean_root):
                self.assertEqual(build_data.recording_path(), destination)
                data = build_data.build_data()
                self.assertEqual(data["run"]["status"], "inconclusive")
                # This early report did not pin the orchestrator. Retain that
                # limitation; publishing it must not invent a complete manifest.
                self.assertFalse(data["showcase"]["source_hashes_valid"])
                manifest = json.loads((destination / "recording-manifest.json").read_text())["files"]
                self.assertTrue(all(build_data.hashlib.sha256((destination / name).read_bytes()).hexdigest() == digest
                                    for name, digest in manifest.items()))
                self.assertTrue(data["showcase"]["patch_authorship_verified"])
                self.assertFalse(data["showcase"]["verified"])
                self.assertGreater(len(data["events"]), 0)
                # An unfinished local run must take precedence over the bundled recording.
                local = clean_root / "runs" / build_data.RUN_ID
                local.mkdir(parents=True)
                (local / "report.json").write_text(json.dumps({"run_id": build_data.RUN_ID, "status": "running"}))
                self.assertEqual(build_data.recording_path(), local)
                self.assertEqual(build_data.build_data()["run"]["status"], "running")

    def test_unbundled_run_does_not_silently_select_a_different_recording(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(build_data, "ROOT", Path(folder)):
            data = build_data.build_data(run_id="nemotron-self-debug-20261017-v99")
        self.assertEqual(data["run"]["status"], "pending")
        self.assertFalse(data["verification"]["available"])

    def test_run_selection_accepts_only_bounded_retained_ids(self):
        self.assertEqual(build_data.checked_run_id("nemotron-self-debug-20261017-v3"), "nemotron-self-debug-20261017-v3")
        for invalid in ("../other", "/tmp/run", "nemotron-self-debug-20261017/../x", "other-run", "x;echo unsafe"):
            with self.assertRaises(ValueError):
                build_data.checked_run_id(invalid)
        self.assertIn("nemotron-self-debug-20261017-v3", server.replay_command("nemotron-self-debug-20261017-v3")[3])

    def test_post_can_verify_only_the_explicitly_selected_run(self):
        selected = "nemotron-self-debug-20261017-v3"
        with patch.object(server, "local_replay", return_value=(200, {"ok": False})) as replay:
            status, _, _ = request("POST", "/api/verify", json.dumps({"run_id": server.RUN_ID}).encode(),
                                   {"Content-Type": "application/json"}, run_id=selected)
            self.assertEqual(status, 400)
            replay.assert_not_called()
            status, _, _ = request("POST", "/api/verify", b"{}", {"Content-Type": "application/json"}, run_id=selected)
            self.assertEqual(status, 200)
            replay.assert_called_once_with(selected)

    def test_status_label_alone_does_not_green_showcase_or_memory(self):
        report = {"run_id": build_data.RUN_ID, "status": "verified", "verification": {"verified": True},
                  "memory": {"stored": True}, "events": []}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "report.json"
            path.write_text(json.dumps(report))
            data = build_data.build_data(path)
        self.assertFalse(data["showcase"]["verified"])
        self.assertFalse(data["memory"]["stored"])
        self.assertFalse(data["evidence"]["after"]["verified"])

    def test_showcase_and_recall_require_matching_success_evidence(self):
        passing = {"passed": 22, "failed": 0, "total": 22}
        required = ("repair_checks.py", "orchestrator.py", "before/targets.py", "before/contracts.py", "before/__init__.py",
                    "workspace/recursive_redteam/targets.py", "workspace/recursive_redteam/contracts.py", "workspace/recursive_redteam/__init__.py")
        with tempfile.TemporaryDirectory() as folder, patch.object(build_data, "audit_model_actions",
                return_value={"valid": True, "reviewer_bound": True, "source": "fixture journal"}):
            root = Path(folder)
            manifest = {}
            for name in required:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture source")
                manifest[name] = build_data.hashlib.sha256(path.read_bytes()).hexdigest()
            report = {"run_id": build_data.RUN_ID, "status": "verified", "teacher_in_runtime": False,
                      "before": {"source": "fixture source", "source_sha256": manifest["before/targets.py"], "tests": {"failed": 7}},
                      "after": {"source": "fixture source", "source_sha256": manifest["workspace/recursive_redteam/targets.py"], "tests": passing},
                      "verification": {"verified": True, "model_identity_verified": True, "evidence_ids": ["a", "b"],
                          "model_review": {"verdict": "verified", "evidence_ids": ["a", "b"]}, "replays": [passing, passing]},
                      "artifact_hashes": manifest, "memory": {"stored": True, "entry_id": 1, "event_hash": "a" * 64}, "events": []}
            (root / "report.json").write_text(json.dumps(report))
            receipt = {"run_id": build_data.RUN_ID, "matched": True, "reusable": True, "chain_valid": True,
                       "hosted_model_calls": 0, "entry_id": 1, "event_hash": "a" * 64,
                       "source_hashes": {"recalled_after_source": report["after"]["source_sha256"],
                           "before/targets.py": manifest["before/targets.py"], "repair_checks.py": manifest["repair_checks.py"]},
                       "checks": [passing, passing]}
            (root / "memory-recall.json").write_text(json.dumps(receipt))
            data = build_data.build_data(root / "report.json")
            self.assertTrue(data["showcase"]["verified"])
            self.assertTrue(data["memory"]["recall"]["verified"])
            receipt["event_hash"] = "b" * 64
            (root / "memory-recall.json").write_text(json.dumps(receipt))
            data = build_data.build_data(root / "report.json")
            self.assertTrue(data["showcase"]["verified"])
            self.assertFalse(data["memory"]["recall"]["verified"])

    def test_patch_authorship_binds_raw_output_and_reconstructs_source(self):
        before, after = "before\n", "after\n"
        action = {"action": "replace_text", "old_text": before, "new_text": after, "reason": "Test fixture"}
        diff = "".join(build_data.difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                                      fromfile="before/targets.py", tofile="after/targets.py"))
        digest = build_data.hashlib.sha256(after.encode()).hexdigest()
        report = {"run_id": build_data.RUN_ID, "model": "test-model", "model_usage": {"logical_calls": 1},
                  "after": {"source": after, "source_sha256": digest}, "events": [
                      {"id": "patch-1", "kind": "patch", "action": action,
                       "result": {"applied": True, "diff": diff, "source_sha256": digest}}]}
        journal = {"model_usage": {"logical_calls": 1}, "model_calls": [
            {"id": 1, "status": "done", "role": "repair_worker", "actual_model": "test-model",
             "response": {"content": json.dumps(action)}}]}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "before").mkdir()
            (root / "workspace/recursive_redteam").mkdir(parents=True)
            (root / "before/targets.py").write_text(before)
            (root / "workspace/recursive_redteam/targets.py").write_text(after)
            (root / "model-journal.json").write_text(json.dumps(journal))
            result = build_data.audit_model_actions(report, root)
            self.assertTrue(result["valid"], result)
            self.assertEqual(result["patches"][0]["model_call_id"], 1)
            self.assertEqual(result["effective_patch_count"], 1)
            journal["model_calls"][0]["response"]["content"] = json.dumps({**action, "new_text": "different"})
            (root / "model-journal.json").write_text(json.dumps(journal))
            self.assertFalse(build_data.audit_model_actions(report, root)["valid"])

    def test_fixed_static_allowlist_denies_traversal_and_credentials(self):
        for path in ("/../.env", "/%2e%2e/.env", "/runs/teaching-memory.sqlite", "/examples/self_debug.py"):
            status, _, _ = request("GET", path)
            self.assertEqual(status, 404)

    def test_dns_rebinding_host_rejected(self):
        status, _, _ = request("GET", "/api/demo", headers={"Host": "attacker.example:8765"})
        self.assertEqual(status, 403)

    def test_foreign_origin_and_simple_form_cannot_run_verifier(self):
        with patch.object(server, "local_replay") as replay:
            status, _, _ = request("POST", "/api/verify", b"{}", {
                "Origin": "https://attacker.example", "Content-Type": "application/json"})
            self.assertEqual(status, 403)
            status, _, _ = request("POST", "/api/verify", b"{}", {"Content-Type": "text/plain"})
            self.assertEqual(status, 415)
            replay.assert_not_called()

    def test_user_cannot_choose_command_or_path(self):
        with patch.object(server, "local_replay") as replay:
            for body in ({"command": "echo wrong"}, {"run_id": "../../other"}, []):
                status, _, _ = request("POST", "/api/verify", json.dumps(body).encode(), {"Content-Type": "application/json"})
                self.assertEqual(status, 400)
            replay.assert_not_called()

    def test_same_origin_verification_returns_fixed_result(self):
        with patch.object(server, "local_replay", return_value=(200, {"ok": True, "mode": "local-replay"})) as replay:
            status, headers, body = request("POST", "/api/verify", b"{}", {
                "Origin": "http://127.0.0.1:8765", "Content-Type": "application/json"})
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)["ok"])
            self.assertNotIn(b"Access-Control-Allow-Origin", headers)
            replay.assert_called_once_with(server.RUN_ID)

    def test_replay_uses_fixed_command_and_does_not_forward_key(self):
        child = subprocess.CompletedProcess(server.REPLAY_COMMAND, 0, '{"verified":true}', "")
        with patch.object(server, "demo_data", return_value={"verification": {"available": True}}), \
                patch.object(server.subprocess, "run", return_value=child) as run, \
                patch.dict(server.os.environ, {"CRUSOE_API_KEY": "test-secret", "OPENAI_API_KEY": "other-secret"}):
            status, result = server.local_replay()
            self.assertEqual(status, 200)
            self.assertTrue(result["ok"])
            self.assertEqual(run.call_args.args[0], server.REPLAY_COMMAND)
            self.assertNotIn("CRUSOE_API_KEY", run.call_args.kwargs["env"])
            self.assertNotIn("OPENAI_API_KEY", run.call_args.kwargs["env"])
            self.assertNotIn("shell", run.call_args.kwargs)

    def test_recall_uses_selected_fixed_command_and_fails_closed_for_unqualified_run(self):
        output = {"matched": False, "reusable": False, "hosted_model_calls": 0,
                  "error": "The recorded run has no verified repair"}
        child = subprocess.CompletedProcess(server.recall_command(), 1, json.dumps(output), "")
        with patch.object(server, "demo_data", return_value={"verification": {"available": True}}), \
                patch.object(server.subprocess, "run", return_value=child) as run:
            status, result = server.local_recall("nemotron-self-debug-20261017-v3")
        self.assertEqual(status, 200)
        self.assertFalse(result["ok"])
        self.assertEqual(result["mode"], "local-recall")
        self.assertEqual(run.call_args.args[0], server.recall_command("nemotron-self-debug-20261017-v3"))
        self.assertEqual(result["result"]["hosted_model_calls"], 0)

    def test_foreign_origin_or_payload_path_cannot_trigger_recall(self):
        with patch.object(server, "local_recall") as recall:
            status, _, _ = request("POST", "/api/recall", b"{}", {
                "Origin": "https://attacker.example", "Content-Type": "application/json"})
            self.assertEqual(status, 403)
            status, _, _ = request("POST", "/api/recall", b'{"path":"/tmp/other"}', {"Content-Type": "application/json"})
            self.assertEqual(status, 400)
            recall.assert_not_called()

    def test_verified_recall_requires_chain_and_zero_model_calls(self):
        for body, expected in (({"matched": True, "reusable": True, "chain_valid": True, "hosted_model_calls": 0}, True),
                               ({"matched": True, "reusable": True, "chain_valid": False, "hosted_model_calls": 0}, False),
                               ({"matched": True, "reusable": True, "chain_valid": True, "hosted_model_calls": 1}, False)):
            body["checks"] = [{"passed": 22, "failed": 0, "total": 22}] * 2
            child = subprocess.CompletedProcess(server.recall_command(), 0, json.dumps(body), "")
            with patch.object(server, "demo_data", return_value={"verification": {"available": True}}), \
                    patch.object(server.subprocess, "run", return_value=child):
                _, result = server.local_recall()
                self.assertEqual(result["ok"], expected)

    def test_replay_failed_verification_is_not_success(self):
        child = subprocess.CompletedProcess(server.REPLAY_COMMAND, 0, '{"verified":false}', "")
        with patch.object(server, "demo_data", return_value={"verification": {"available": True}}), \
                patch.object(server.subprocess, "run", return_value=child):
            _, result = server.local_replay()
            self.assertFalse(result["ok"])

    def test_pending_report_does_not_make_up_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            data = build_data.build_data(Path(folder) / "absent.json")
        self.assertEqual(data["run"]["status"], "pending")
        self.assertEqual(data["metrics"], [])
        self.assertEqual(data["events"], [])
        self.assertFalse(data["verification"]["available"])

    def test_running_report_cannot_trigger_replay_and_unknown_calls_stay_unknown(self):
        report = {"run_id": build_data.RUN_ID, "status": "running", "events": [],
                  "before": {}, "after": {}, "memory": {"stored": False}}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "report.json"
            path.write_text(json.dumps(report))
            data = build_data.build_data(path)
        self.assertFalse(data["verification"]["available"])
        self.assertIsNone(data["run"]["model_calls"])
        self.assertNotIn("Model calls", [item["label"] for item in data["metrics"]])

    def test_unchanged_edit_is_not_presented_as_a_repair(self):
        report = {"run_id": build_data.RUN_ID, "status": "inconclusive", "events": [
            {"id": "event-noop", "kind": "patch", "title": "Edited source", "summary": "original claim",
             "action": {"old_text": "same", "new_text": "same"}, "result": {"applied": True, "diff": ""}}]}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "report.json"
            path.write_text(json.dumps(report))
            data = build_data.build_data(path)
        self.assertEqual(len(data["events"]), 1)
        self.assertEqual(data["events"][0]["derived_classification"], "no_source_change")
        self.assertEqual(data["events"][0]["title"], "Nemotron proposes an unchanged edit")
        self.assertIn("No source change occurred", data["events"][0]["summary"])

    def test_adapter_uses_recorded_counts_and_discards_private_configuration(self):
        report = {"run_id": build_data.RUN_ID, "status": "verified", "model": "test-model",
                  "teacher_in_runtime": False, "provider_config": {"api_key": "must-not-appear"},
                  "model_usage": {"logical_calls": 7},
                  "before": {"source": "before code", "tests": {"passed": 1, "failed": 2, "total": 3}},
                  "after": {"source": "after code", "tests": {"passed": 3, "failed": 0, "total": 3}},
                  "verification": {"verified": True, "replays": [{}, {}]}, "memory": {"stored": True},
                  "events": [{"id": "e1", "kind": "patch", "title": "Edit", "summary": "repair", "action": {"op": "replace"}}]}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "report.json"
            path.write_text(json.dumps(report))
            data = build_data.build_data(path)
        self.assertEqual(data["run"]["model_calls"], 7)
        self.assertEqual([m["value"] for m in data["metrics"]], [7, 1, 3, 2, 0])
        self.assertEqual(data["metrics"][1]["total"], 3)
        self.assertEqual(data["code"]["after"]["text"], "after code")
        self.assertEqual(data["evidence"]["before"]["status"], "failed")
        self.assertEqual(data["evidence"]["after"]["status"], "passed")
        self.assertNotIn("must-not-appear", json.dumps(data))


if __name__ == "__main__":
    unittest.main()
