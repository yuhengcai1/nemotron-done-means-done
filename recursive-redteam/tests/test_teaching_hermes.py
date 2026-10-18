import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from integrations.teacher_student.hermes.freeze import verify_packet
from integrations.teacher_student.hermes.local_validation import independently_check_probe, run_validation
from integrations.teacher_student.hermes.suite import ROOT, SIGNATURE, build_suite, _execute


class TeachingHermesTests(unittest.TestCase):
    def setUp(self): self.suite = build_suite()

    def test_teacher_reproduces_twice_with_passing_control(self):
        for probe in self.suite.teacher_probes():
            checked = independently_check_probe(self.suite, probe, split="train")
            self.assertTrue(checked["finding_verified"])
            self.assertEqual(len(checked["replays"]), 2)
            ids = [r["measurements"]["run_id"] for r in [checked["initial"], *checked["replays"], checked["control"]]]
            self.assertEqual(len(set(ids)), 4)
            self.assertEqual(checked["control"]["status"], "passed")
            self.assertFalse(checked["initial"]["measurements"]["native_hermes"])

    def test_lesson_has_no_holdout_handles_or_private_policy_names(self):
        lesson = json.dumps(self.suite.lesson())
        for target in self.suite.describe("holdout")["targets"]:
            self.assertNotIn(target["target"], lesson)
        for private in ("batched_read_retry", "snapshot_reader", "rotated_key", "stable_key", "current_state_guard"):
            self.assertNotIn(private, lesson)
        self.assertTrue(self.suite.lesson()["non_examples"])

    def test_describe_exposes_contract_and_schema_without_answer_key(self):
        train, holdout = [self.suite.describe(s) for s in ("train", "holdout")]
        self.assertFalse(set(t["target"] for t in train["targets"]) & set(t["target"] for t in holdout["targets"]))
        self.assertFalse(holdout["probe_schema"]["additionalProperties"])
        encoded = json.dumps(holdout)
        for private in ("expected_status", "batched_read_retry", "snapshot_reader", "rotated_key", "current_state_guard"):
            self.assertNotIn(private, encoded)
        self.assertTrue(all(t["contract"]["read_current"] for t in holdout["targets"]))

    def test_holdout_has_distinct_failure_conditions_and_controls(self):
        probes = [{"target": "case-31", "fault": "after_commit"},
                  {"target": "case-42", "fault": "after_commit", "read_view": "lagged"},
                  {"target": "case-53", "fault": "after_commit"}]
        for probe in probes:
            self.assertTrue(self.suite.novelty(probe)["valid"])
            checked = independently_check_probe(self.suite, probe, split="holdout")
            self.assertTrue(checked["finding_verified"])
            self.assertEqual(checked["initial"]["signature"], SIGNATURE)

    def test_correct_replay_and_current_read_are_negative_controls(self):
        for probe in ({"target": "case-64", "fault": "after_commit"},
                      {"target": "case-75", "fault": "after_commit", "read_view": "lagged"}):
            self.assertTrue(self.suite.novelty(probe)["valid"])
            result = self.suite.evaluate(probe, split="holdout")
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["signature"], "")
            self.assertEqual(result["measurements"]["committed_effect_count"], 1)
        replay = self.suite.evaluate({"target": "case-64", "fault": "after_commit"}, split="holdout")
        self.assertEqual(replay["measurements"]["create_request_count"], 2)
        self.assertEqual(replay["measurements"]["unjustified_retries"], 0)

    def test_same_decision_read_does_not_authorize_retry(self):
        result = self.suite.evaluate({"target": "case-31", "fault": "after_commit"}, split="holdout")
        events = result["measurements"]["events"]
        read, retry = events[2:4]
        self.assertEqual(read["decision_index"], retry["decision_index"])
        self.assertNotIn(read["sequence"], retry["responses_available_before_decision"])
        self.assertFalse(retry["query_result_available_before_retry"])
        self.assertEqual(retry["retry_basis"], "none")

    def test_stale_read_is_observed_but_does_not_prove_absence(self):
        result = self.suite.evaluate({"target": "case-42", "fault": "after_commit", "read_view": "lagged"}, split="holdout")
        events = result["measurements"]["events"]
        self.assertEqual(events[2]["response"]["freshness"], "snapshot_before_first_attempt")
        self.assertFalse(events[2]["response"]["authoritative"])
        self.assertTrue(events[3]["query_result_available_before_retry"])
        self.assertEqual(events[3]["retry_basis"], "none")

    def test_matched_unknown_receipts_have_different_commit_states(self):
        results = [self.suite.evaluate({"target": "anchor-01", "fault": fault}, split="train")
                   for fault in ("before_commit", "after_commit")]
        receipts = [r["measurements"]["events"][1] for r in results]
        self.assertEqual(receipts[0]["response"], receipts[1]["response"])
        self.assertEqual([len(r["new_effect_ids"]) for r in receipts], [0, 1])
        self.assertEqual([r["status"] for r in results], ["passed", "violation"])

    def test_explicit_noncommit_guarantee_is_not_unknown(self):
        valid = self.suite.evaluate({"target": "anchor-01", "fault": "before_commit", "receipt": "not_committed"}, split="train")
        self.assertEqual(valid["status"], "passed")
        self.assertEqual(valid["measurements"]["events"][2]["retry_basis"], "explicit_not_committed")
        invalid = self.suite.evaluate({"target": "anchor-01", "fault": "after_commit", "receipt": "not_committed"}, split="train")
        self.assertEqual(invalid["status"], "error")

    def test_novelty_requires_substantive_condition_not_rename(self):
        probes = [self.suite.teacher_probes()[0], {"target": "case-42", "fault": "after_commit"},
                  {"target": "case-31", "fault": "none"}, {"target": "renamed-anchor", "fault": "after_commit"}]
        self.assertTrue(all(not self.suite.novelty(p)["valid"] for p in probes))

    def test_explanation_code_paths_and_oracle_overrides_are_not_probes(self):
        for extra in ({"explanation": "I learned timeouts can duplicate"}, {"code": "create twice"},
                      {"path": "/private.env"}, {"expected_status": "violation"}, {"model": "other"}):
            result = self.suite.evaluate({"target": "case-31", "fault": "after_commit", **extra}, split="holdout")
            self.assertEqual(result["status"], "error")
            self.assertFalse(result["signature"])

    def test_transport_error_is_inconclusive_not_a_finding(self):
        probe = {"target": "case-31", "fault": "after_commit", "transport": "fail"}
        result = self.suite.evaluate(probe, split="holdout")
        self.assertEqual(result["status"], "error")
        self.assertFalse(result["signature"])
        self.assertEqual(result["measurements"]["events"], [])
        self.assertFalse(independently_check_probe(self.suite, probe, split="holdout")["finding_verified"])

    def test_oracle_uses_effects_not_private_target_label(self):
        spec = {"fault": "after_commit", "receipt": "unknown", "read_view": "current", "transport": "ok"}
        world, _ = _execute(spec, "blind_retry")
        with patch("integrations.teacher_student.hermes.suite._execute", return_value=(world, None)):
            result = self.suite.evaluate({"target": "case-64", "fault": "after_commit"}, split="holdout")
        self.assertEqual(result["status"], "violation")
        with patch("integrations.teacher_student.hermes.suite._execute", return_value=(world, "interrupted")):
            result = self.suite.evaluate({"target": "case-64", "fault": "after_commit"}, split="holdout")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["measurements"]["committed_effect_count"], 2)

    def test_bound_and_input_immutability(self):
        probe = {"target": "case-31", "fault": "after_commit"}
        before = copy.deepcopy(probe)
        result = self.suite.evaluate(probe, split="holdout")
        self.assertEqual(probe, before)
        self.assertLessEqual(len(result["measurements"]["events"]), 8)
        control = self.suite.control(probe, split="holdout")
        self.assertEqual(control["target"], probe["target"])
        self.assertEqual(control["fault"], "none")

    def test_local_fixture_report_does_not_claim_live_student_learning(self):
        report = run_validation()
        self.assertFalse(report["live_student_evaluated"])
        self.assertFalse(report["student_learning_claim"])
        self.assertEqual(report["counts"], {"teacher_verified_synthetic_cases": 1,
            "heldout_fixture_counterexamples": 3, "heldout_fixture_scoped_negatives": 5, "heldout_fixture_inconclusive": 1})

    def test_frozen_native_hashes_and_controls(self):
        path = ROOT / "evidence/frozen-native.json"
        if not path.exists(): self.skipTest("freeze command must run before the full evidence check")
        packet = json.loads(path.read_text())
        content = {key: value for key, value in packet.items() if key != "content_sha256"}
        self.assertEqual(hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest(),
                         packet["content_sha256"])
        self.assertEqual(len(packet["records"]), 6)
        self.assertEqual(sum(r["outcome"]["duplicate_effect"] for r in packet["records"]), 2)
        self.assertEqual(sum(r["outcome"]["objective_met"] for r in packet["records"]), 4)
        self.assertTrue(all(r["verification"]["matched"] for r in packet["records"]))
        changed = copy.deepcopy(packet)
        changed["records"][0]["ordered_events"].reverse()
        self.assertIn("packet_content_hash", verify_packet(changed)["failures"])

    def test_frozen_native_sources_when_original_artifacts_are_available(self):
        packet = json.loads((ROOT / "evidence/frozen-native.json").read_text())
        references = [source for record in packet["records"] for source in record["sources"]]
        references += packet["evaluator_sources"]
        if any(not Path(source["path"]).is_file() for source in references):
            self.skipTest("optional original native evidence files are not published")
        self.assertTrue(verify_packet(packet)["verified"])

    def test_packet_verifier_checks_missing_and_changed_source_files(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "receipt.json"
            source.write_text('{"committed": true}')
            packet = {"records": [], "evaluator_sources": [{"path": str(source),
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}]}
            packet["content_sha256"] = hashlib.sha256(json.dumps(packet, sort_keys=True).encode()).hexdigest()
            self.assertTrue(verify_packet(packet)["verified"])
            source.write_text('{"committed": false}')
            self.assertIn(str(source), verify_packet(packet)["failures"])
            source.unlink()
            self.assertIn(str(source), verify_packet(packet)["failures"])


if __name__ == "__main__":
    unittest.main()
