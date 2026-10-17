from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
import multiprocessing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from recursive_redteam.contracts import Candidate
from recursive_redteam.memory import ExperimentMemory, normalize_payload, payload_digest


CASE = Candidate("case-1", "fixture", "Check a synthetic boundary", "A synthetic fixture\nwith spacing")


def _process_claim(path, gate, ready, results, owner):
    memory = ExperimentMemory(path)
    ready.put(True)
    gate.wait(10)
    try:
        results.put(memory.claim("scope", CASE, owner)["claimed"])
    finally:
        memory.close()


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "memory.sqlite"
        self.memory = ExperimentMemory(self.path, conclusion_ttl_seconds=100, lease_seconds=10)

    def tearDown(self):
        self.memory.close()
        self.temp.cleanup()

    def finish(self, outcome="no_violation", *, campaign="one", candidate=CASE, scope="scope"):
        self.memory.finish(scope, candidate, campaign, outcome,
                           {"oracle": "synthetic", "trial": "trial-1"}, "runs/example")

    def test_conclusive_dedup_survives_new_connection_and_campaign(self):
        self.assertTrue(self.memory.claim("scope", CASE, "one")["claimed"])
        self.finish()
        other = ExperimentMemory(self.path)
        try:
            duplicate = replace(CASE, id="other-id", hypothesis="Another wording")
            result = other.claim("scope", duplicate, "two")
            self.assertFalse(result["claimed"])
            self.assertEqual(result["reason"], "conclusive")
            self.assertEqual(result["record"]["observation_count"], 1)
            self.assertEqual(other.stats()["observations"], 1)
        finally:
            other.close()

    def test_concurrent_connections_have_one_claim_winner(self):
        memories = [ExperimentMemory(self.path) for _ in range(8)]
        gate = threading.Barrier(len(memories))
        def claim(index):
            gate.wait()
            return memories[index].claim("scope", CASE, f"owner-{index}")
        try:
            with ThreadPoolExecutor(max_workers=len(memories)) as pool:
                results = list(pool.map(claim, range(len(memories))))
            self.assertEqual(sum(result["claimed"] for result in results), 1)
            self.assertTrue(all(result["reason"] == "pending" for result in results if not result["claimed"]))
        finally:
            for memory in memories:
                memory.close()

    def test_concurrent_processes_have_one_claim_winner(self):
        context = multiprocessing.get_context("spawn")
        gate, ready, results = context.Event(), context.Queue(), context.Queue()
        processes = [context.Process(target=_process_claim,
                     args=(str(self.path), gate, ready, results, f"process-{i}")) for i in range(3)]
        try:
            for process in processes:
                process.start()
            for _ in processes:
                ready.get(timeout=15)
            gate.set()
            claims = [results.get(timeout=15) for _ in processes]
            self.assertEqual(sum(claims), 1)
            for process in processes:
                process.join(10)
                self.assertEqual(process.exitcode, 0)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                process.join(10)
            ready.close()
            results.close()

    def test_scopes_and_families_are_isolated(self):
        self.memory.claim("model-a-policy-1", CASE, "one")
        self.finish(scope="model-a-policy-1")
        self.assertTrue(self.memory.claim("model-b-policy-1", CASE, "two")["claimed"])
        self.assertTrue(self.memory.claim("model-a-policy-2", CASE, "three")["claimed"])
        self.assertTrue(self.memory.claim("model-a-policy-1", replace(CASE, family="other-entry"), "four")["claimed"])
        self.assertEqual(self.memory.stats()["experiments"], 4)
        self.assertEqual(self.memory.stats("model-b-policy-1")["observations"], 0)

    def test_exact_digest_keeps_all_executed_input_variants_distinct(self):
        self.assertEqual(normalize_payload(" \r\nCafe\u0301\rfoo\r\n "), "Caf\u00e9\nfoo")
        self.assertEqual(payload_digest("Cafe\u0301"), hashlib.sha256("Cafe\u0301".encode("utf-8")).hexdigest())
        self.assertNotEqual(payload_digest("Cafe\u0301"), payload_digest("Caf\u00e9"))
        self.assertNotEqual(payload_digest("a\r\nb"), payload_digest("a\nb"))
        self.assertNotEqual(payload_digest("a\rb"), payload_digest("a\nb"))
        self.assertNotEqual(payload_digest(" a "), payload_digest("a"))
        self.assertNotEqual(payload_digest("A B"), payload_digest("a b"))
        self.assertNotEqual(payload_digest("a  b"), payload_digest("a b"))
        self.assertNotEqual(payload_digest("a\tb"), payload_digest("a b"))
        self.assertNotEqual(payload_digest("a\n\nb"), payload_digest("a\nb"))

    def test_untested_normalized_variants_are_not_suppressed(self):
        self.memory.claim("scope", CASE, "one")
        self.finish()
        for index, payload in enumerate((" " + CASE.payload, CASE.payload + " ", CASE.payload.replace("\n", "\r\n"))):
            candidate = replace(CASE, id=f"variant-{index}", payload=payload)
            self.assertTrue(self.memory.claim("scope", candidate, "two")["claimed"])

    def test_same_campaign_different_candidates_have_one_concurrent_winner(self):
        memories = [ExperimentMemory(self.path) for _ in range(5)]
        candidates = [replace(CASE, id=f"candidate-{index}") for index in range(5)]
        gate = threading.Barrier(len(memories))
        def claim(index):
            gate.wait()
            return memories[index].claim("scope", candidates[index], "one")
        try:
            with ThreadPoolExecutor(max_workers=len(memories)) as pool:
                results = list(pool.map(claim, range(len(memories))))
            self.assertEqual(sum(result["claimed"] for result in results), 1)
            winner = next(index for index, result in enumerate(results) if result["claimed"])
            self.assertEqual(memories[winner].claim("scope", candidates[winner], "one")["reason"], "recovered")
            loser = (winner + 1) % len(memories)
            self.assertFalse(memories[loser].renew("scope", candidates[loser], "one"))
            self.assertFalse(memories[loser].release("scope", candidates[loser], "one"))
            with self.assertRaises(ValueError):
                memories[loser].finish("scope", candidates[loser], "one", "no_violation", {"oracle": "synthetic"}, "run")
            self.assertTrue(memories[winner].renew("scope", candidates[winner], "one"))
        finally:
            for memory in memories:
                memory.close()

    def test_renew_extends_original_lease_duration_and_expired_finish_is_rejected(self):
        with patch("recursive_redteam.memory.time.time", return_value=100):
            self.memory.claim("scope", CASE, "one", ttl_seconds=20)
        with patch("recursive_redteam.memory.time.time", return_value=115):
            self.assertTrue(self.memory.renew("scope", CASE, "one"))
        with patch("recursive_redteam.memory.time.time", return_value=134):
            self.assertFalse(self.memory.claim("scope", CASE, "two")["claimed"])
        with patch("recursive_redteam.memory.time.time", return_value=135):
            self.assertFalse(self.memory.renew("scope", CASE, "one"))
            with self.assertRaises(ValueError):
                self.finish()
            self.assertEqual(self.memory.stats()["observations"], 0)
            other = replace(CASE, id="case-2")
            self.assertEqual(self.memory.claim("scope", other, "one")["reason"], "reclaimed")
            self.assertFalse(self.memory.renew("scope", CASE, "one"))
            self.assertFalse(self.memory.release("scope", CASE, "one"))
            with self.assertRaises(ValueError):
                self.finish()
            self.finish(candidate=other)
            self.assertFalse(self.memory.renew("scope", other, "one"))

    def test_renew_and_finish_after_lease_extension(self):
        with patch("recursive_redteam.memory.time.time", return_value=100):
            self.assertFalse(self.memory.renew("scope", CASE, "one"))
            self.memory.claim("scope", CASE, "one")
        with patch("recursive_redteam.memory.time.time", return_value=109):
            self.assertTrue(self.memory.renew("scope", CASE, "one"))
        with patch("recursive_redteam.memory.time.time", return_value=118):
            self.finish()
            self.assertEqual(self.memory.stats()["observations"], 1)

    def test_legacy_normalized_keys_migrate_without_suppressing_raw_inputs(self):
        legacy_path = Path(self.temp.name) / "legacy.sqlite"
        database = sqlite3.connect(legacy_path)
        database.execute("""CREATE TABLE experiment_keys(
            id INTEGER PRIMARY KEY AUTOINCREMENT,scope TEXT NOT NULL,family TEXT NOT NULL,
            payload_digest TEXT NOT NULL,hypothesis TEXT NOT NULL,created_at REAL NOT NULL,
            owner TEXT,lease_until REAL,UNIQUE(scope,family,payload_digest))""")
        database.execute("INSERT INTO experiment_keys VALUES(1,?,?,?,?,?,?,?)",
                         ("scope", CASE.family, payload_digest(CASE.payload), CASE.hypothesis, 100, "one", 99999999999))
        database.commit()
        database.close()
        memory = ExperimentMemory(legacy_path)
        try:
            records = memory.recall("scope")
            self.assertEqual(records[0]["digest_version"], "normalized-v1")
            self.assertFalse(records[0]["dedup_eligible"])
            self.assertEqual(records[0]["payload_digest"], payload_digest(CASE.payload))
            self.assertTrue(memory.claim("scope", CASE, "one")["claimed"])
            self.assertEqual(memory.stats()["experiments"], 2)
        finally:
            memory.close()

    def test_pending_recovery_expiry_and_fencing(self):
        with patch("recursive_redteam.memory.time.time", return_value=100):
            self.memory.claim("scope", CASE, "one", ttl_seconds=10)
        with patch("recursive_redteam.memory.time.time", return_value=105):
            self.assertEqual(self.memory.claim("scope", CASE, "one", ttl_seconds=10)["reason"], "recovered")
            self.assertFalse(self.memory.claim("scope", CASE, "two", force_retest=True)["claimed"])
            self.assertFalse(self.memory.release("scope", CASE, "two"))
        with patch("recursive_redteam.memory.time.time", return_value=114):
            self.assertFalse(self.memory.claim("scope", CASE, "two")["claimed"])
        with patch("recursive_redteam.memory.time.time", return_value=115):
            self.assertEqual(self.memory.claim("scope", CASE, "two")["reason"], "reclaimed")
            with self.assertRaises(ValueError):
                self.finish(campaign="one")
            self.finish(campaign="two")
        self.assertEqual(self.memory.stats()["observations"], 1)

    def test_conclusion_ttl_retains_observations(self):
        with patch("recursive_redteam.memory.time.time", return_value=100):
            self.memory.claim("scope", CASE, "one")
            self.finish()
        with patch("recursive_redteam.memory.time.time", return_value=199):
            self.assertFalse(self.memory.claim("scope", CASE, "two")["claimed"])
        with patch("recursive_redteam.memory.time.time", return_value=200):
            self.assertTrue(self.memory.claim("scope", CASE, "two")["claimed"])
            self.assertEqual(self.memory.recall("scope")[0]["observation_count"], 1)

    def test_retryable_outcomes_release_lease_without_claiming_safety(self):
        for index, outcome in enumerate(("error", "incomplete", "inconclusive")):
            owner = f"owner-{index}"
            self.assertTrue(self.memory.claim("scope", CASE, owner)["claimed"])
            self.finish(outcome, campaign=owner)
            record = self.memory.recall("scope")[0]
            self.assertEqual(record["outcome"], outcome)
            self.assertFalse(record["fresh"])
            self.assertFalse(record["pending"])
        self.assertEqual(self.memory.stats()["observations"], 3)
        self.assertEqual(self.memory.stats()["fresh_conclusive"], 0)
        self.assertTrue(self.memory.claim("scope", CASE, "last")["claimed"])

    def test_explicit_release_does_not_invent_observations(self):
        self.memory.claim("scope", CASE, "one")
        self.assertTrue(self.memory.release("scope", CASE, "one"))
        self.assertEqual(self.memory.stats()["observations"], 0)
        self.assertEqual(self.memory.recall("scope")[0]["outcome"], "incomplete")
        self.assertTrue(self.memory.claim("scope", CASE, "two")["claimed"])

    def test_force_retest_conflict_preserved_and_never_latest_wins(self):
        self.memory.claim("scope", CASE, "one")
        self.finish()
        self.assertEqual(self.memory.claim("scope", CASE, "two", force_retest=True)["reason"], "force_retest")
        self.finish("reproduced", campaign="two")
        record = self.memory.recall("scope")[0]
        self.assertEqual(record["outcome"], "contested")
        self.assertEqual(record["observed_outcomes"], ["no_violation", "reproduced"])
        self.assertEqual([x["outcome"] for x in record["observations"]], ["no_violation", "reproduced"])
        self.assertEqual(self.memory.claim("scope", CASE, "three")["reason"], "contested")
        self.finish("no_violation", campaign="three")
        self.assertEqual(self.memory.recall("scope")[0]["outcome"], "contested")
        self.assertEqual(self.memory.stats()["contested"], 1)
        self.assertEqual(self.memory.stats()["fresh_conclusive"], 0)

    def test_failed_forced_retest_remains_retryable(self):
        self.memory.claim("scope", CASE, "one")
        self.finish()
        self.memory.claim("scope", CASE, "two", force_retest=True)
        self.finish("error", campaign="two")
        self.assertTrue(self.memory.claim("scope", CASE, "three")["claimed"])
        self.assertEqual(self.memory.stats()["observations"], 2)

    def test_evidence_hashes_and_append_only_history(self):
        self.memory.claim("scope", CASE, "one")
        self.finish()
        record = self.memory.recall("scope")[0]
        evidence = json.dumps({"oracle": "synthetic", "trial": "trial-1"}, sort_keys=True, separators=(",", ":"))
        self.assertEqual(record["evidence_hash"], hashlib.sha256(evidence.encode()).hexdigest())
        self.assertEqual(len(record["observations"][0]["observation_hash"]), 64)
        self.assertEqual(record["source_run"], "runs/example")
        self.assertNotIn("payload", record)
        self.assertNotIn("evidence", record)
        with self.assertRaises(sqlite3.IntegrityError):
            self.memory.db.execute("UPDATE experiment_observations SET outcome='error'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.memory.db.execute("DELETE FROM experiment_observations")
        self.assertEqual(self.memory.stats()["observations"], 1)

    def test_unowned_or_invalid_finish_cannot_create_a_conclusion(self):
        with self.assertRaises(ValueError):
            self.finish()
        self.memory.claim("scope", CASE, "one")
        for outcome in ("safe", "verified", "candidate", "", None):
            with self.assertRaises(ValueError):
                self.finish(outcome)
        with self.assertRaises(ValueError):
            self.memory.finish("scope", CASE, "one", "no_violation", {"x": float("nan")}, "run")
        with self.assertRaises(ValueError):
            self.memory.finish("scope", CASE, "one", "no_violation", [], "run")
        with self.assertRaises(ValueError):
            self.memory.finish("scope", CASE, "one", "reproduced", {}, "run")
        self.assertEqual(self.memory.stats()["observations"], 0)
        self.assertEqual(self.memory.stats()["pending"], 1)

    def test_invalid_claim_parameters_and_recall_limit(self):
        for ttl in (0, -1, float("nan"), float("inf"), True):
            with self.assertRaises(ValueError):
                self.memory.claim("scope", CASE, "one", ttl_seconds=ttl)
        with self.assertRaises(ValueError):
            self.memory.claim("", CASE, "one")
        with self.assertRaises(ValueError):
            self.memory.claim("scope", CASE, "")
        with self.assertRaises(ValueError):
            self.memory.claim("scope", CASE, "one", force_retest="yes")
        self.memory.claim("scope", CASE, "one")
        self.assertEqual(self.memory.recall("scope", limit=0), [])
        with self.assertRaises(ValueError):
            self.memory.recall("scope", limit=1001)


if __name__ == "__main__":
    unittest.main()
