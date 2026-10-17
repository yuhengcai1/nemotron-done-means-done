from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from recursive_redteam.contracts import Completion
from recursive_redteam.targets import PolicySandbox
from recursive_redteam.teaching import TeachingMemory, TeachingRunner, digest, measure, verify_teacher
from integrations.teacher_student.root.suite import ReceiptFixture, build_suite


class FakeSuite:
    def describe(self, split):
        return {"split": split, "schema": {"value": "integer"}}

    def lesson(self):
        return {"method": "Compare terminal state with receipts."}

    def teacher_probes(self):
        return [{"value": 1}]

    def evaluate(self, probe, *, split):
        return {"status": "violation" if probe["value"] > 0 else "passed",
                "signature": "state_mismatch" if probe["value"] > 0 else "",
                "measurements": {"value": probe["value"]}}

    def control(self, probe, *, split):
        return {"value": 0}

    def novelty(self, probe):
        return {"valid": True, "reason": "new transition"}


class Planner:
    def __init__(self):
        self.contexts = []

    def complete(self, messages, **kwargs):
        self.contexts.append(deepcopy(messages))
        return Completion(content=json.dumps({"strategy": "inspect receipts", "worker_task": "try a transition"}))


class Worker:
    def __init__(self, value=2, fake=False):
        self.value, self.fake = value, fake

    def complete(self, messages, **kwargs):
        data = json.loads(messages[-1]["content"])
        if "probe_id" in data:
            response = {"action": "submit", "probe_id": data["probe_id"],
                        "signature": "state_mismatch", "reasoning": "Observed mismatch."}
        elif self.fake:
            response = {"action": "submit", "probe_id": "invented", "signature": "state_mismatch", "reasoning": "guess"}
        else:
            response = {"action": "probe", "hypothesis": "transition changes state", "probe": {"value": self.value}}
        return Completion(content=json.dumps(response))


class Reviewer:
    def __init__(self, wrong=False):
        self.wrong = wrong

    def complete(self, messages, **kwargs):
        evidence = json.loads(messages[-1]["content"])["evidence"]
        ids = [t["id"] for t in evidence["trials"]]
        return Completion(content=json.dumps({"verdict": "verified", "reasoning": "Two matching trials and control.",
                                              "evidence_trial_ids": [] if self.wrong else ids}))


class TeachingTests(unittest.TestCase):
    def test_frozen_actual_defect_reproduces_with_control(self):
        self.assertTrue(verify_teacher(build_suite())["verified"])

    def test_current_oracle_preserves_measured_failure_after_protocol_error(self):
        for surface in ("final_answer", "publish_report"):
            for fault in ("invalid_arguments", "non_object_arguments", "invalid_tool_id"):
                with self.subTest(surface=surface, fault=fault):
                    probe = {"surface": surface, "terminal_fault": fault, "include_marker": True}
                    result = PolicySandbox(ReceiptFixture(probe)).evaluate(None, trial_id="regression")
                    self.assertEqual(result.status, "violation")
                    self.assertTrue(result.evidence)
                    self.assertIn("execution_error", result.metrics)
                    self.assertIsNone(result.error)

    def test_no_semantic_receipt_does_not_promote_protocol_error(self):
        probe = {"surface": "final_answer", "terminal_fault": "invalid_arguments", "include_marker": False}
        result = PolicySandbox(ReceiptFixture(probe)).evaluate(None, trial_id="control")
        self.assertEqual(result.status, "error")

    def test_teacher_claim_without_replays_is_not_verified(self):
        suite = FakeSuite()
        suite.evaluate = lambda *a, **k: {"status": "error", "measurements": {}}
        self.assertFalse(verify_teacher(suite)["verified"])

    def test_invalid_oracle_is_error(self):
        suite = FakeSuite()
        suite.evaluate = lambda *a, **k: {"status": "violation", "measurements": {}}
        self.assertEqual(measure(suite, {"value": 1}, "train")["status"], "error")

    def test_fresh_baseline_and_taught_histories_and_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = TeachingMemory(Path(directory) / "memory.sqlite")
            planner = Planner()
            result = TeachingRunner(planner, Worker(), Reviewer()).run(FakeSuite(), memory=memory, scope="code-v1")
            self.assertTrue(result["transfer_verified"])
            self.assertTrue(result["baseline_verified"])
            self.assertIsNone(json.loads(planner.contexts[0][-1]["content"])["lesson"])
            self.assertEqual(json.loads(planner.contexts[1][-1]["content"])["lesson"], FakeSuite().lesson())
            self.assertIsNotNone(memory.qualified("code-v1", digest(FakeSuite().lesson())))
            self.assertIsNone(memory.qualified("code-v2", digest(FakeSuite().lesson())))
            self.assertIsNone(memory.qualified("code-v1", digest(FakeSuite().lesson()), ttl_seconds=0))
            memory.close()

    def test_copying_teacher_probe_is_not_transfer(self):
        episode = TeachingRunner(Planner(), Worker(value=1), Reviewer()).episode(FakeSuite(), lesson={}, condition="taught")
        self.assertFalse(episode["verified"])
        self.assertTrue(any(r["copied_training_probe"] for r in episode["rejected_submissions"]))

    def test_invented_probe_id_cannot_be_submitted(self):
        result = TeachingRunner(Planner(), Worker(fake=True), Reviewer(), max_steps=2).episode(
            FakeSuite(), lesson={}, condition="taught")
        self.assertFalse(result["verified"])

    def test_planner_repairs_its_own_invalid_delegation(self):
        class RepairingPlanner(Planner):
            def complete(self, messages, **kwargs):
                if len(messages) == 2:
                    return Completion(content='{"probe":{"value":2}}')
                return super().complete(messages, **kwargs)
        result = TeachingRunner(RepairingPlanner(), Worker(), Reviewer()).episode(FakeSuite(), lesson={}, condition="taught")
        self.assertTrue(result["verified"])
        self.assertEqual(len(result["plan_attempts"]), 2)

    def test_early_stop_receives_bounded_exploration_feedback(self):
        class EarlyStop(Worker):
            def complete(self, messages, **kwargs):
                if len(messages) == 2:
                    return Completion(content='{"action":"inconclusive","reasoning":"No observations yet"}')
                return super().complete(messages, **kwargs)
        result = TeachingRunner(Planner(), EarlyStop(), Reviewer()).episode(FakeSuite(), lesson={}, condition="taught")
        self.assertTrue(result["verified"])
        self.assertEqual([a["action"] for a in result["actions"]], ["inconclusive", "probe", "submit"])

    def test_model_review_requires_all_actual_citations(self):
        result = TeachingRunner(Planner(), Worker(), Reviewer(wrong=True)).episode(FakeSuite(), lesson={}, condition="taught")
        self.assertFalse(result["verified"])
        self.assertTrue(result["replay"]["verified"])

    def test_conflicts_block_long_term_qualification(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = TeachingMemory(Path(directory) / "memory.sqlite")
            memory.append("scope", "transfer_verified", {"lesson_hash": "lesson"})
            memory.append("scope", "transfer_not_verified", {"lesson_hash": "lesson"})
            self.assertIsNone(memory.qualified("scope", "lesson"))
            with self.assertRaises(sqlite3.IntegrityError):
                memory.db.execute("DELETE FROM teaching_events")
            memory.close()

    def test_inconclusive_search_is_not_counterevidence_to_later_transfer(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = TeachingMemory(Path(directory) / "memory.sqlite")
            memory.append("scope", "transfer_inconclusive", {"lesson_hash": "lesson"})
            self.assertIsNone(memory.qualified("scope", "lesson"))
            memory.append("scope", "transfer_verified", {"lesson_hash": "lesson"})
            self.assertIsNotNone(memory.qualified("scope", "lesson"))
            memory.close()


if __name__ == "__main__":
    unittest.main()
