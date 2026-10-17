import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from recursive_redteam.contracts import Candidate
from recursive_redteam.memory import ExperimentMemory
from recursive_redteam.provenance import memory_scope
from recursive_redteam.store import Store


SPEC = importlib.util.spec_from_file_location("check_memory_reuse", Path(__file__).resolve().parents[1] / "examples" / "check_memory_reuse.py")
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


class MemoryReuseHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run = self.root / "run"
        self.run.mkdir()
        self.memory_path = self.root / "memory.sqlite"
        self.store = Store(self.run / "state.sqlite")
        self.identity = {"model": "offline-fixture", "input_identity": "sha256_exact_utf8"}
        self.scope = memory_scope(self.identity)
        self.store.set_metadata("status", "completed")
        self.store.set_metadata("campaign_id", "source-campaign")
        self.store.set_metadata("memory_scope", self.scope)
        self.store.set_metadata("memory_identity", self.identity)
        self.store.set_metadata("config", {"memory": {"enabled": True, "path": str(self.memory_path),
                                "conclusion_ttl_seconds": 86400, "lease_seconds": 30}})
        call_id = self.store.reserve_model_call(10, role="fixture")
        self.store.finish_model_call(call_id, "fixture", {"total_tokens": 0})
        self.memory = ExperimentMemory(self.memory_path, conclusion_ttl_seconds=86400, lease_seconds=30)

    def tearDown(self):
        self.memory.close()
        self.store.close()
        self.temp.cleanup()

    def add_case(self, outcome, index=0, *, scope=None):
        scope = scope or self.scope
        private_text = "INTERNAL_CANARY_payload_hypothesis_evidence_never_emit"
        candidate = Candidate(f"case-{index}", "fixture", private_text, f"{private_text}\r\n{index} ")
        self.store.add_tasks([{"id": candidate.id, "outcome": outcome}], 0)
        self.store.update_task(candidate.id, "done")
        self.store.save_candidate(candidate)
        self.memory.claim(scope, candidate, "source-campaign")
        self.memory.finish(scope, candidate, "source-campaign", outcome, {"oracle": private_text}, str(self.run))
        return candidate

    def test_real_saved_candidates_cross_campaign_and_retryable_cleanup(self):
        self.add_case("no_violation", 0)
        self.add_case("reproduced", 1)
        self.add_case("inconclusive", 2)
        original_report = self.store.report()
        before = self.memory.stats(self.scope)
        with patch("recursive_redteam.providers.OpenAICompatibleModel.complete", side_effect=AssertionError("no provider calls")):
            result = helper.check_memory_reuse(self.run)
        self.assertTrue(result["passed"])
        self.assertNotEqual(result["check_campaign_id"], result["source_campaign_id"])
        self.assertEqual(result["counts"]["fresh_conclusive_suppressed"], 2)
        self.assertEqual(result["counts"]["retryable_candidates_claimed_and_released"], 1)
        self.assertEqual(result["counts"]["new_model_calls"], 0)
        self.assertEqual(result["retryable_check"], "checked")
        self.assertEqual(self.memory.stats(self.scope), before)
        self.assertEqual(self.store.report(), original_report)
        self.assertNotIn("INTERNAL_CANARY_", json.dumps(result))
        self.assertTrue(all(candidate["checks"]["evidence_hashes_valid"] for candidate in result["candidates"]))
        self.assertTrue(all(candidate["checks"]["observation_hashes_valid"] for candidate in result["candidates"]))
        self.assertTrue(all(candidate["checks"]["ttl_bounds_valid"] for candidate in result["candidates"]))

    def test_expired_conclusion_is_claimed_and_released_not_suppressed(self):
        with patch("recursive_redteam.memory.time.time", return_value=100.0):
            self.add_case("no_violation")
        result = helper.check_memory_reuse(self.run)
        self.assertFalse(result["passed"])
        candidate = result["candidates"][0]
        self.assertTrue(candidate["passed"])
        self.assertTrue(candidate["claimed"])
        self.assertTrue(candidate["lease_released"])
        self.assertEqual(candidate["remaining_ttl_seconds"], 0)
        self.assertFalse(result["checks"]["fresh_conclusive_duplicate_suppressed"])
        self.assertEqual(self.memory.stats(self.scope)["pending"], 0)

    def test_no_retryable_cases_are_reported_as_not_applicable(self):
        self.add_case("no_violation")
        result = helper.check_memory_reuse(self.run)
        self.assertTrue(result["passed"])
        self.assertEqual(result["retryable_check"], "not_applicable_no_retryable_candidates_in_scope")

    def test_recovery_scope_is_explicit_and_uses_saved_identity(self):
        identity = dict(self.identity, verifier_output_limit=4096)
        scope = memory_scope(identity)
        self.store.set_metadata("recovery_memory_identity", identity)
        self.store.set_metadata("recovery_memory_scope", scope)
        self.add_case("no_violation", scope=scope)
        result = helper.check_memory_reuse(self.run, scope_kind="recovery")
        self.assertTrue(result["passed"])
        self.assertEqual(result["scope"], scope)
        original = helper.check_memory_reuse(self.run)
        self.assertFalse(original["passed"])
        self.assertEqual(original["candidates"][0]["error"], "exact_input_memory_missing")

    def test_rejects_running_run_and_scope_mismatch_before_claiming(self):
        self.add_case("no_violation")
        self.store.set_metadata("status", "running")
        with patch.object(helper.ExperimentMemory, "claim") as claim:
            with self.assertRaisesRegex(ValueError, "completed"):
                helper.check_memory_reuse(self.run)
            claim.assert_not_called()
        self.store.set_metadata("status", "completed")
        self.store.set_metadata("memory_identity", {"wrong": "identity"})
        with self.assertRaisesRegex(ValueError, "identity"):
            helper.check_memory_reuse(self.run)

    def test_other_campaign_pending_lease_is_preserved_and_check_fails(self):
        candidate = self.add_case("inconclusive")
        self.memory.claim(self.scope, candidate, "other-owner")
        result = helper.check_memory_reuse(self.run)
        self.assertFalse(result["passed"])
        self.assertEqual(result["candidates"][0]["reason"], "pending")
        self.assertTrue(self.memory.renew(self.scope, candidate, "other-owner"))
        self.memory.release(self.scope, candidate, "other-owner")

    def test_claim_is_released_if_call_raises_after_committing(self):
        candidate = self.add_case("inconclusive")
        original_claim = ExperimentMemory.claim
        def interrupted(memory, scope, candidate, campaign):
            original_claim(memory, scope, candidate, campaign)
            raise RuntimeError("simulated interruption after commit")
        with patch.object(helper.ExperimentMemory, "claim", new=interrupted):
            with self.assertRaises(RuntimeError):
                helper.check_memory_reuse(self.run)
        self.assertEqual(self.memory.stats(self.scope)["pending"], 0)
        self.assertTrue(self.memory.claim(self.scope, candidate, "next-owner")["claimed"])
        self.memory.release(self.scope, candidate, "next-owner")

    def test_tampered_evidence_hash_is_detected_without_raw_output(self):
        self.add_case("no_violation")
        with sqlite3.connect(self.memory_path) as database:
            database.execute("DROP TRIGGER experiment_observation_no_update")
            database.execute("UPDATE experiment_observations SET evidence='{}'")
        result = helper.check_memory_reuse(self.run)
        self.assertFalse(result["passed"])
        self.assertFalse(result["candidates"][0]["checks"]["evidence_hashes_valid"])
        self.assertNotIn("INTERNAL_CANARY_", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
