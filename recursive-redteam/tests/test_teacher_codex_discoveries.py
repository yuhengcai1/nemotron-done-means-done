"""Regression evidence for the teacher-discovered lease-generation defect."""
from itertools import product
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from integrations.teacher_student.codex.suite import build_suite, exercise
from recursive_redteam import memory as memory_module
from recursive_redteam.contracts import Candidate
from recursive_redteam.memory import ExperimentMemory
from recursive_redteam.teaching import verify_teacher


CASE = Candidate("candidate", "family", "expired attempts cannot mutate successors", "input")


class TeacherLeaseDiscoveryTests(TestCase):
    def test_frozen_teacher_has_two_replays_and_passing_control(self):
        report = verify_teacher(build_suite())
        self.assertTrue(report["verified"])
        results = report["discoveries"][0]["trials"]
        self.assertEqual([r["result"]["status"] for r in results], ["violation", "violation", "passed"])

    def test_frozen_heldout_changes_lifecycle_and_operation(self):
        suite = build_suite()
        for lifecycle, action in product(["after_release", "after_other_owner"], ["renew", "release"]):
            with self.subTest(lifecycle=lifecycle, action=action):
                probe = {"reacquisition": lifecycle, "stale_action": action,
                         "reuse_identity": True, "lease_seconds": 10}
                self.assertTrue(suite.novelty(probe)["valid"])
                self.assertEqual(suite.evaluate(probe, split="holdout")["status"], "violation")
                self.assertEqual(suite.evaluate(suite.control(probe, split="holdout"), split="holdout")["status"], "passed")
        self.assertFalse(suite.novelty(suite.teacher_probes()[0])["valid"])

    def test_current_source_rejects_stale_operations_across_lifecycles(self):
        for lifecycle, action, same_identity in product(
                ["after_expiry", "after_release", "after_other_owner"], ["finish", "renew", "release"], [True, False]):
            with self.subTest(lifecycle=lifecycle, action=action, same_identity=same_identity):
                result = exercise(memory_module, {"reacquisition": lifecycle, "stale_action": action,
                                                 "reuse_identity": same_identity, "lease_seconds": 10})
                self.assertTrue(result["retired_original_lease"])
                self.assertTrue(result["replacement_claimed"])
                self.assertFalse(result["stale_operation_accepted"])
                self.assertTrue(result["replacement_lease_live_after_action"])
                self.assertEqual(result["observations_written"], 0)

    def test_invalid_probe_is_error_not_finding(self):
        suite = build_suite()
        for bad in [None, {}, {**suite.teacher_probes()[0], "lease_seconds": True},
                    {**suite.teacher_probes()[0], "stale_action": "execute_code"}]:
            with self.subTest(probe=bad):
                self.assertEqual(suite.evaluate(bad, split="train")["status"], "error")

    def test_probe_schema_distinguishes_enum_choices_from_array_values(self):
        suite = build_suite()
        for split in ("train", "holdout"):
            with self.subTest(split=split):
                schema = suite.describe(split)["probe_schema"]
                self.assertEqual(schema["type"], "object")
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(set(schema["required"]), set(schema["properties"]))
                fields = schema["properties"]
                self.assertEqual(fields["reacquisition"]["type"], "string")
                self.assertEqual(fields["stale_action"]["type"], "string")
                self.assertEqual(fields["reuse_identity"], {"type": "boolean"})
                self.assertEqual(fields["lease_seconds"], {"type": "integer", "minimum": 2, "maximum": 60})
                probe = {"reacquisition": fields["reacquisition"]["enum"][0],
                         "stale_action": fields["stale_action"]["enum"][0],
                         "reuse_identity": True, "lease_seconds": 10}
                self.assertEqual(suite.evaluate(probe, split=split)["status"], "violation")
                for field in ("reacquisition", "stale_action"):
                    array_value = {**probe, field: [probe[field]]}
                    self.assertEqual(suite.evaluate(array_value, split=split)["status"], "error")
                for malformed in ({**probe, "reuse_identity": "true"}, {**probe, "lease_seconds": 1},
                                  {**probe, "lease_seconds": 61}, {**probe, "extra": "unexpected"},
                                  {k: v for k, v in probe.items() if k != "stale_action"}):
                    self.assertEqual(suite.evaluate(malformed, split=split)["status"], "error")

    def test_same_instance_requires_explicit_token_for_concurrent_generations(self):
        with TemporaryDirectory() as directory:
            memory = ExperimentMemory(Path(directory) / "memory.sqlite", lease_seconds=10)
            try:
                with patch.object(memory_module.time, "time", return_value=100):
                    old = memory.claim("scope", CASE, "campaign")["lease_token"]
                with patch.object(memory_module.time, "time", return_value=111):
                    new = memory.claim("scope", CASE, "campaign")["lease_token"]
                    self.assertNotEqual(old, new)
                    self.assertFalse(memory.renew("scope", CASE, "campaign", lease_token=old))
                    self.assertFalse(memory.release("scope", CASE, "campaign", lease_token=old))
                    with self.assertRaises(ValueError):
                        memory.finish("scope", CASE, "campaign", "no_violation", {"receipt": "old"}, "run", lease_token=old)
                    self.assertTrue(memory.renew("scope", CASE, "campaign", lease_token=new))
                    memory.finish("scope", CASE, "campaign", "no_violation", {"receipt": "new"}, "run", lease_token=new)
                    self.assertEqual(memory.stats("scope")["observations"], 1)
            finally:
                memory.close()

    def test_recovering_active_same_identity_rotates_token(self):
        with TemporaryDirectory() as directory:
            first = ExperimentMemory(Path(directory) / "memory.sqlite")
            second = ExperimentMemory(Path(directory) / "memory.sqlite")
            try:
                original = first.claim("scope", CASE, "campaign")
                recovered = second.claim("scope", CASE, "campaign")
                self.assertEqual(recovered["reason"], "recovered")
                self.assertNotEqual(original["lease_token"], recovered["lease_token"])
                self.assertFalse(first.renew("scope", CASE, "campaign"))
                self.assertFalse(first.release("scope", CASE, "campaign"))
                with self.assertRaises(ValueError):
                    first.finish("scope", CASE, "campaign", "no_violation", {"receipt": "old"}, "run")
                self.assertTrue(second.renew("scope", CASE, "campaign"))
            finally:
                first.close()
                second.close()

    def test_connection_without_claim_cannot_adopt_names_but_can_use_explicit_token(self):
        with TemporaryDirectory() as directory:
            first = ExperimentMemory(Path(directory) / "memory.sqlite")
            second = ExperimentMemory(Path(directory) / "memory.sqlite")
            try:
                token = first.claim("scope", CASE, "campaign")["lease_token"]
                self.assertFalse(second.renew("scope", CASE, "campaign"))
                self.assertFalse(second.release("scope", CASE, "campaign"))
                with self.assertRaises(ValueError):
                    second.finish("scope", CASE, "campaign", "no_violation", {"receipt": "unknown"}, "run")
                self.assertTrue(second.renew("scope", CASE, "campaign", lease_token=token))
                self.assertTrue(second.release("scope", CASE, "campaign", lease_token=token))
                self.assertFalse(first.renew("scope", CASE, "campaign", lease_token=token))
            finally:
                first.close()
                second.close()
