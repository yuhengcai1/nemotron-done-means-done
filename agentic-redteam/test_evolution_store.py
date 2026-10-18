import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

from evolution_store import Store


class EvolutionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.genesis = self.store.active()

    def verdict(self, passed=True, **changes):
        value = {"exit_code": 0, "timed_out": False, "event_errors": [],
                 "native_multiagent_observed": True, "declared_status": "done",
                 "verified_task_passed": passed, "skill_read_observed": True, "violations": []}
        value.update(changes)
        return value

    def pairs(self, candidate=None, baseline=None, skill="Procedure"):
        pairs = [{"baseline": baseline or self.verdict(False), "candidate": candidate or self.verdict()},
                 {"baseline": self.verdict(), "candidate": self.verdict()}]
        active_skill = self.store.active()["skill"]
        for pair in pairs:
            pair["baseline"].setdefault("skill_sha256", hashlib.sha256(active_skill.encode("utf-8")).hexdigest())
            pair["candidate"].setdefault("skill_sha256", hashlib.sha256(skill.encode("utf-8")).hexdigest())
        return pairs

    def test_genesis_and_content_addressed_candidate_survive_reopening(self):
        candidate = self.store.candidate("Read actual outcomes before finishing.", [{"run": "training-1"}])
        self.assertEqual(candidate["parent"], self.genesis["id"])
        self.assertEqual(self.store.active()["id"], self.genesis["id"])
        self.assertEqual(Store(self.root).get(candidate["id"]), candidate)
        self.assertEqual(self.store.candidate(candidate["skill"], candidate["evidence"])["id"], candidate["id"])

    def test_promotion_retains_all_evidence_and_moves_only_active_pointer(self):
        candidate = self.store.candidate("A model-authored procedure", [{"trace": "run-a"}])
        pairs = self.pairs(skill=candidate["skill"])
        decision = self.store.decide(candidate["id"], pairs)
        self.assertEqual(decision["status"], "promoted")
        self.assertEqual(Store(self.root).active()["id"], candidate["id"])
        self.assertEqual(self.store.get(self.genesis["id"]), self.genesis)
        saved = json.loads((self.root / "decisions" / (decision["id"] + ".json")).read_text())
        self.assertEqual(saved["pairs"], pairs)
        self.assertEqual(json.loads((self.root / "active.json").read_text())["decision_id"], decision["id"])

    def test_empty_and_single_comparisons_are_inconclusive(self):
        candidate = self.store.candidate("Procedure", [])
        for pairs in ([], self.pairs()[:1]):
            self.assertEqual(self.store.decide(candidate["id"], pairs)["status"], "inconclusive")
        self.assertEqual(self.store.active()["id"], self.genesis["id"])

    def test_interrupted_or_errored_baseline_is_not_an_improvement(self):
        candidate = self.store.candidate("Procedure", [])
        for changes in ({"timed_out": True}, {"exit_code": 1}, {"event_errors": ["RateLimitError"]},
                        {"native_multiagent_observed": False}, {"declared_status": None},
                        {"tool_errors": ["broken"]}, {"exit_code": None}, {"exit_code": False},
                        {"declared_status": []}, {"execution_complete": False}):
            with self.subTest(changes=changes):
                pairs = self.pairs(baseline=self.verdict(False, **changes))
                self.assertEqual(self.store.decide(candidate["id"], pairs)["status"], "inconclusive")

    def test_incomplete_declaration_can_be_a_clean_baseline_failure(self):
        candidate = self.store.candidate("Procedure", [])
        result = self.store.decide(candidate["id"], self.pairs(baseline=self.verdict(False, declared_status="incomplete")))
        self.assertEqual(result["status"], "promoted")

    def test_candidate_must_pass_every_task_and_read_the_skill(self):
        candidate = self.store.candidate("Procedure", [])
        result = self.store.decide(candidate["id"], self.pairs(candidate=self.verdict(False)))
        self.assertEqual(result["status"], "rejected")
        result = self.store.decide(candidate["id"], self.pairs(candidate=self.verdict(skill_read_observed=False)))
        self.assertEqual(result["status"], "inconclusive")
        result = self.store.decide(candidate["id"], self.pairs(candidate=self.verdict(declared_status="incomplete")))
        self.assertEqual(result["status"], "rejected")

    def test_historical_violations_override_incorrect_success_flag(self):
        candidate = self.store.candidate("Procedure", [])
        result = self.store.decide(candidate["id"], self.pairs(candidate=self.verdict(violations=["ineligible recipient published"])))
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(self.store.active()["id"], self.genesis["id"])

    def test_no_baseline_failure_means_no_promotion(self):
        candidate = self.store.candidate("Procedure", [])
        result = self.store.decide(candidate["id"], self.pairs(baseline=self.verdict()))
        self.assertEqual(result["status"], "rejected")

    def test_stale_parent_cannot_replace_new_active(self):
        first = self.store.candidate("First candidate", [])
        stale = self.store.candidate("Concurrent candidate", [])
        self.assertEqual(self.store.decide(first["id"], self.pairs(skill=first["skill"]))["status"], "promoted")
        result = self.store.decide(stale["id"], self.pairs())
        self.assertEqual(result["status"], "stale")
        self.assertEqual(self.store.active()["id"], first["id"])

    def test_concurrent_promotion_has_one_winner(self):
        first = self.store.candidate("Candidate A", [])
        second = self.store.candidate("Candidate B", [])
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(Store(self.root).decide, item["id"], self.pairs(skill=item["skill"])) for item in (first, second)]
            results = [future.result()["status"] for future in futures]
        self.assertEqual(sorted(results), ["promoted", "stale"])

    def test_candidate_hash_must_match_exact_utf8_skill(self):
        candidate = self.store.candidate("\u6267\u884c\u524d\u786e\u8ba4\u5b9e\u9645\u72b6\u6001。\n", [])
        pairs = self.pairs(skill=candidate["skill"])
        pairs[0]["candidate"]["skill_sha256"] = hashlib.sha256(candidate["skill"].strip().encode("utf-8")).hexdigest()
        result = self.store.decide(candidate["id"], pairs)
        self.assertEqual(result["status"], "inconclusive")
        self.assertEqual(result["reason"], "candidate_skill_identity_not_observed")
        pairs = self.pairs(skill=candidate["skill"])
        del pairs[1]["candidate"]["skill_sha256"]
        self.assertEqual(self.store.decide(candidate["id"], pairs)["status"], "inconclusive")
        self.assertEqual(self.store.decide(candidate["id"], self.pairs(skill=candidate["skill"]))["status"], "promoted")

    def test_nonempty_baseline_must_read_the_current_version_with_matching_hash(self):
        first = self.store.candidate("Version one", [])
        self.assertEqual(self.store.decide(first["id"], self.pairs(skill=first["skill"]))["status"], "promoted")
        second = self.store.candidate("Version two", [])
        for changes in ({"skill_read_observed": False}, {"skill_sha256": "0" * 64}, {"skill_sha256": None}):
            with self.subTest(changes=changes):
                pairs = self.pairs(skill=second["skill"], baseline=self.verdict(False, **changes))
                result = self.store.decide(second["id"], pairs)
                self.assertEqual(result["status"], "inconclusive")
                self.assertEqual(result["reason"], "baseline_skill_identity_not_observed")
                self.assertEqual(self.store.active()["id"], first["id"])
        self.assertEqual(self.store.decide(second["id"], self.pairs(skill=second["skill"]))["status"], "promoted")

    def test_empty_genesis_does_not_require_a_skill_read(self):
        candidate = self.store.candidate("Procedure", [])
        pairs = self.pairs()
        for pair in pairs:
            pair["baseline"]["skill_read_observed"] = False
            pair["baseline"].pop("skill_sha256")
        self.assertEqual(self.store.decide(candidate["id"], pairs)["status"], "promoted")

    def test_hash_tampering_and_path_traversal_are_rejected(self):
        candidate = self.store.candidate("Procedure", [])
        path = self.root / "versions" / (candidate["id"] + ".json")
        value = json.loads(path.read_text())
        value["skill"] = "tampered"
        path.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            self.store.get(candidate["id"])
        with self.assertRaises(ValueError):
            self.store.get("../active")

    def test_caller_mutation_cannot_rewrite_retained_evidence(self):
        evidence = [{"trace": "original"}]
        candidate = self.store.candidate("Procedure", evidence)
        evidence[0]["trace"] = "changed"
        candidate["evidence"][0]["trace"] = "also changed"
        self.assertEqual(self.store.get(candidate["id"])["evidence"], [{"trace": "original"}])


if __name__ == "__main__":
    unittest.main()
