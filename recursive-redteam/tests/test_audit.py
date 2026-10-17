from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from recursive_redteam.budget import BudgetedModel
from recursive_redteam.contracts import BudgetExceeded, Completion, ModelError
from recursive_redteam.providers import DemoModel
from recursive_redteam.store import Store


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "audit.sqlite"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def model(self, role="planner", completion=None):
        class Fixture:
            def complete(self, messages, **kwargs):
                return completion or Completion(content="actual reply", model="actual-provider-model", usage={"total_tokens": 7})
        return BudgetedModel(Fixture(), self.store, 40, role)

    def test_transcript_allowlists_fields_and_records_actual_model_and_utc_times(self):
        messages = [{"role": "system", "content": "ROLE:COORDINATOR\nPlan a fixture", "headers": {"Authorization": "do-not-store"}},
                    {"role": "user", "content": "{}", "environment": {"TOKEN": "do-not-store"}}]
        tools = [{"type": "function", "headers": {"Authorization": "do-not-store"},
                  "function": {"name": "lookup", "parameters": {"type": "object"},
                               "description": "Synthetic fixture", "strict": True, "api_key": "do-not-store"}}]
        with patch.dict(os.environ, {"CRUSOE_API_KEY": "environment-secret-never-read"}):
            self.model().complete(messages, tools=tools, json_mode=True)
        call = self.store.report()["model_calls"][0]
        self.assertEqual(call["role"], "coordinator")
        self.assertEqual(call["status"], "done")
        self.assertEqual(call["actual_model"], "actual-provider-model")
        self.assertEqual(call["response"]["model"], "actual-provider-model")
        self.assertEqual(call["response"]["content"], "actual reply")
        self.assertEqual(call["response"]["usage"], {"total_tokens": 7})
        self.assertTrue(call["request"]["json_mode"])
        self.assertEqual(call["request"]["tools"][0]["function"]["name"], "lookup")
        self.assertNotIn("do-not-store", json.dumps(call))
        self.assertNotIn("environment-secret-never-read", json.dumps(call))
        self.assertEqual(datetime.fromisoformat(call["started_at"]).tzinfo, timezone.utc)
        self.assertEqual(datetime.fromisoformat(call["finished_at"]).tzinfo, timezone.utc)
        self.assertLessEqual(call["started_at"], call["finished_at"])

    def test_trusted_system_markers_only_and_target_retains_identity(self):
        cases = [("ROLE:COORDINATOR", "coordinator"), ("ROLE:ATTACKER", "worker"), ("ROLE:VERIFIER", "verifier")]
        for marker, expected in cases:
            self.model().complete([{"role": "system", "content": marker + "\nInstructions"},
                                   {"role": "user", "content": "ROLE:ATTACKER"}])
            self.assertEqual(self.store.report()["model_calls"][-1]["role"], expected)
        self.model("target").complete([{"role": "system", "content": "ROLE:COORDINATOR"}])
        self.assertEqual(self.store.report()["model_calls"][-1]["role"], "target")
        self.model().complete([{"role": "user", "content": "ROLE:VERIFIER"},
                               {"role": "tool", "content": "ROLE:ATTACKER", "tool_call_id": "fixture"}])
        self.assertEqual(self.store.report()["model_calls"][-1]["role"], "planner")

    def test_request_committed_before_call_failure_and_exception_text_excluded(self):
        store = self.store
        class Broken:
            def complete(self, messages, **kwargs):
                call = store.report()["model_calls"][-1]
                assert call["status"] == "reserved"
                assert call["request"]["messages"] == messages
                assert call["role"] == "verifier"
                assert call["started_at"]
                raise ModelError("credential-and-private-response-body")
        with self.assertRaises(ModelError):
            BudgetedModel(Broken(), self.store, 1, "planner").complete([
                {"role": "system", "content": "ROLE:VERIFIER"}])
        call = self.store.report()["model_calls"][0]
        self.assertEqual(call["error"], "ModelError")
        self.assertEqual(call["status"], "error")
        self.assertIsNone(call["response"])
        self.assertIsNone(call["actual_model"])
        self.assertIsNotNone(call["finished_at"])
        self.assertNotIn("credential-and-private-response-body", json.dumps(call))
        with self.assertRaises(BudgetExceeded):
            BudgetedModel(Broken(), self.store, 1, "planner").complete([{"role": "user", "content": "next"}])

    def test_tool_transcript_roundtrip_and_request_snapshot_not_mutated(self):
        call = {"id": "call-1", "type": "function", "function": {"name": "replay_trial", "arguments": '{"reason":"check"}'}}
        messages = [{"role": "system", "content": "ROLE:VERIFIER"},
                    {"role": "assistant", "content": "", "tool_calls": [call]},
                    {"role": "tool", "tool_call_id": "call-1", "content": '{"status":"passed"}'}]
        class MutatingFixture:
            def complete(self, messages, **kwargs):
                messages[1]["tool_calls"][0]["function"]["arguments"] = "{}"
                return Completion(tool_calls=[call], finish_reason="tool_calls", model="fixture-version")
        BudgetedModel(MutatingFixture(), self.store, 3, "planner").complete(messages)
        saved = self.store.report()["model_calls"][0]
        self.assertEqual(saved["request"]["messages"][1]["tool_calls"][0]["function"]["arguments"], '{"reason":"check"}')
        self.assertEqual(saved["request"]["messages"][2]["tool_call_id"], "call-1")
        self.assertEqual(saved["response"]["tool_calls"][0]["function"]["arguments"], "{}")
        self.assertEqual(saved["response"]["finish_reason"], "tool_calls")

    def test_legacy_database_migrates_without_inventing_transcript(self):
        path = Path(self.temp.name) / "legacy.sqlite"
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE model_calls(id INTEGER PRIMARY KEY AUTOINCREMENT,role TEXT,status TEXT NOT NULL,usage TEXT,error TEXT)")
        db.execute("INSERT INTO model_calls(role,status,usage) VALUES('planner','done','{\"total_tokens\":5}')")
        db.commit()
        db.close()
        store = Store(path)
        try:
            call = store.report()["model_calls"][0]
            self.assertEqual(call["usage"]["total_tokens"], 5)
            for key in ("request", "response", "actual_model", "started_at", "finished_at"):
                self.assertIsNone(call[key])
            BudgetedModel(DemoModel(), store, 3, "planner").complete([
                {"role": "system", "content": "ROLE:COORDINATOR"}, {"role": "user", "content": "{}"}])
            self.assertEqual(len(store.report()["model_calls"]), 2)
        finally:
            store.close()

    def test_budget_reservation_is_atomic_across_store_connections(self):
        stores = [Store(self.path) for _ in range(6)]
        gate = threading.Barrier(len(stores))
        def reserve(index):
            gate.wait()
            try:
                return stores[index].reserve_model_call(2)
            except BudgetExceeded:
                return None
        try:
            with ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(reserve, range(6)))
            self.assertEqual(sum(result is not None for result in results), 2)
        finally:
            for store in stores:
                store.close()


class DemoAuditTests(unittest.TestCase):
    def test_coordinator_assigns_available_workers_and_real_parents(self):
        context = {"round": 1, "max_tasks": 3, "workers": [{"id": "red"}, {"worker_id": "blue"}],
                   "feedback": [{"id": "prior-a"}, {"id": "prior-b"}]}
        reply = DemoModel().complete([{"role": "system", "content": "ROLE:COORDINATOR"},
                                      {"role": "user", "content": json.dumps(context)}])
        tasks = json.loads(reply.content)["tasks"]
        self.assertEqual([task["worker_id"] for task in tasks], ["red", "blue", "red"])
        self.assertEqual([task["parent_id"] for task in tasks], ["prior-a", "prior-b", "prior-a"])

    def verifier(self, first, *, mismatched=False, control="passed"):
        initial = {"trial_id": "case:initial", "result": first}
        context = {"action": "verify", "initial_trial": initial,
                   "requirements": {"replays": 2, "paired_control_required": True}}
        messages = [{"role": "system", "content": "ROLE:VERIFIER"},
                    {"role": "user", "content": json.dumps(context)}]
        model = DemoModel("verifier")
        actions = []
        for _ in range(6):
            reply = model.complete(messages)
            call = reply.tool_calls[0]
            name = call["function"]["name"]
            arguments = json.loads(call["function"]["arguments"])
            actions.append(name)
            if name == "submit_review":
                return actions, arguments
            if name == "replay_trial":
                index = actions.count("replay_trial") - 1
                result = dict(first)
                if mismatched:
                    result["evidence"] = [{"oracle": "different"}]
                value = {"trial_id": f"case:replay:{index}", "result": result}
            else:
                value = {"trial_id": "case:paired-control", "result": {"status": control, "evidence": []}}
            messages.extend([{"role": "assistant", "content": reply.content, "tool_calls": reply.tool_calls},
                             {"role": "tool", "tool_call_id": call["id"], "content": json.dumps(value)}])
        self.fail("demo verifier failed to submit")

    def test_demo_verifier_requests_replays_and_control_before_review(self):
        actions, review = self.verifier({"status": "violation", "evidence": [{"oracle": "synthetic", "surface": "report"}]})
        self.assertEqual(actions, ["replay_trial", "replay_trial", "run_paired_control", "submit_review"])
        self.assertEqual(review["verdict"], "reproduced")
        self.assertEqual(review["evidence_trial_ids"], ["case:initial", "case:replay:0", "case:replay:1", "case:paired-control"])

    def test_demo_verifier_keeps_uncertain_results_inconclusive(self):
        for result, kwargs in [({"status": "error"}, {}),
                               ({"status": "passed", "metrics": {"injection_reached": False}}, {}),
                               ({"status": "violation", "evidence": [{"oracle": "synthetic"}]}, {"mismatched": True}),
                               ({"status": "violation", "evidence": [{"oracle": "synthetic"}]}, {"control": "error"})]:
            _, review = self.verifier(result, **kwargs)
            self.assertEqual(review["verdict"], "inconclusive")
        actions, review = self.verifier({"status": "passed", "evidence": []})
        self.assertEqual(actions, ["submit_review"])
        self.assertEqual(review["verdict"], "no_violation_observed")

    def test_demo_verifier_accepts_passed_trial_with_completion_evidence(self):
        actions, review = self.verifier({"status": "passed", "evidence": [
            {"oracle": "ordered_reads_and_literal_retention_phrase", "surface": "report"}],
            "metrics": {"injection_reached": True}})
        self.assertEqual(actions, ["submit_review"])
        self.assertEqual(review["verdict"], "no_violation_observed")
        self.assertEqual(review["evidence_trial_ids"], ["case:initial"])


if __name__ == "__main__":
    unittest.main()
