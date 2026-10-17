import importlib.util
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from recursive_redteam import cli
from recursive_redteam.config import demo_config, validate_config
from recursive_redteam.contracts import ModelError
from recursive_redteam.providers import DemoModel
from recursive_redteam.store import Store


SPEC = importlib.util.spec_from_file_location("retry_inconclusive", Path(__file__).resolve().parents[1] / "examples" / "retry_inconclusive.py")
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run = self.root / "run"

    def tearDown(self):
        self.temp.cleanup()

    def config(self, *, external_fixture=False, memory=False):
        config = demo_config()
        config["limits"].update(max_rounds=1, parallelism=1, max_candidates=1, replays=1, max_model_calls=60)
        if external_fixture:
            model = {"kind": "openai-compatible", "base_url": "https://fixture.invalid/v1", "model": "fixture/nemotron", "max_tokens": 1024}
            config.update(planner=model, verifier=model)
            config["target"]["model"] = model
        if memory:
            config["memory"].update(enabled=True, path=str(self.root / "memory.sqlite"))
        return validate_config(config)

    @staticmethod
    def fixture_preparer():
        failures = []

        class FailFirstSubmission(DemoModel):
            def complete(self, messages, **kwargs):
                response = super().complete(messages, **kwargs)
                if not failures and any(call["function"]["name"] == "submit_review" for call in response.tool_calls):
                    failures.append("first submission failed")
                    raise ModelError("fixture response truncated, never output raw text")
                return response

        def prepare(config, role, store):
            # All roles remain offline fixtures; no catalogue or HTTP calls occur.
            model = FailFirstSubmission(role) if role == "verifier" else DemoModel(role)
            if config["kind"] == "openai-compatible":
                model.model = config["model"]
                store.set_metadata("model_" + role, {"fixture": True, "max_tokens": config["max_tokens"]})
            return model
        return prepare

    def test_recovers_once_reuses_trials_and_retains_failed_history(self):
        prepare = self.fixture_preparer()
        with patch.object(cli, "prepare_model", side_effect=prepare):
            before = cli.execute(self.config(memory=True), self.run)
            self.assertEqual(before["tasks"][0]["outcome"], "inconclusive")
            after = recovery.recover_run(self.run)
        self.assertEqual(after["tasks"][0]["outcome"], "reproduced")
        self.assertTrue(after["metadata"]["recovery_acceptance"]["passed"])
        self.assertEqual(after["tasks"][0]["id"], before["tasks"][0]["id"])
        self.assertEqual(after["trials"], before["trials"])
        self.assertEqual(after["model_calls"][:len(before["model_calls"])], before["model_calls"])
        self.assertEqual(after["events"][:len(before["events"])], before["events"])
        additional = after["model_calls"][len(before["model_calls"]):]
        self.assertTrue(additional)
        self.assertTrue(all(call["role"] == "verifier" for call in additional))
        starts = [event for event in after["events"] if event["kind"] == "review_recovery_started"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["data"]["attempt_limit_per_task"], 1)
        self.assertEqual(len(starts[0]["data"]["helper_sha256"]), 64)
        self.assertEqual(after["metadata"]["memory_before"], before["metadata"]["memory_before"])
        self.assertEqual(after["metadata"]["memory_after"], before["metadata"]["memory_after"])
        self.assertGreater(after["metadata"]["recovery_memory_after"]["observations"], after["metadata"]["recovery_memory_before"]["observations"])

    def test_source_mismatch_denied_before_provider_or_journal_changes(self):
        with patch.object(cli, "prepare_model", side_effect=self.fixture_preparer()):
            before = cli.execute(self.config(), self.run)
        saved_report = (self.run / "report.json").read_bytes()
        with patch.object(recovery, "source_manifest", return_value={"changed.py": "different"}), patch.object(cli, "prepare_model") as prepare:
            with self.assertRaisesRegex(ValueError, "source changed"):
                recovery.recover_run(self.run)
            prepare.assert_not_called()
        self.assertEqual((self.run / "report.json").read_bytes(), saved_report)
        with StoreContext(self.run / "state.sqlite") as store:
            self.assertEqual(store.report()["events"], before["events"])

    def test_completed_run_needing_no_recovery_makes_zero_provider_calls(self):
        before = cli.execute(self.config(), self.run)
        self.assertEqual(before["tasks"][0]["outcome"], "reproduced")
        with patch.object(cli, "prepare_model") as prepare:
            after = recovery.recover_run(self.run)
            prepare.assert_not_called()
        self.assertEqual(after["model_calls"], before["model_calls"])
        self.assertEqual(after["events"], before["events"])

    def test_original_budget_is_shared_and_never_reset(self):
        config = self.config()
        with patch.object(cli, "prepare_model", side_effect=self.fixture_preparer()):
            before = cli.execute(config, self.run)
        with StoreContext(self.run / "state.sqlite") as store:
            while store.report()["model_usage"]["logical_calls"] < config["limits"]["max_model_calls"]:
                store.reserve_model_call(config["limits"]["max_model_calls"], role="fixture-reservation")
        with patch.object(cli, "prepare_model") as prepare:
            after = recovery.recover_run(self.run)
            prepare.assert_not_called()
        self.assertEqual(after["status"], "budget_exhausted")
        self.assertEqual(after["tasks"][0]["status"], "queued")
        self.assertEqual(after["tasks"][0]["outcome"], "inconclusive")
        self.assertEqual(after["model_usage"]["logical_calls"], config["limits"]["max_model_calls"])
        self.assertEqual(after["model_calls"][:len(before["model_calls"])], before["model_calls"])

    def test_explicit_cap_override_has_new_config_and_memory_scope(self):
        config = self.config(external_fixture=True, memory=True)
        with patch.object(cli, "prepare_model", side_effect=self.fixture_preparer()):
            before = cli.execute(config, self.run)
            original_config = (self.run / "config.json").read_bytes()
            after = recovery.recover_run(self.run, verifier_max_tokens=4096, all_reviews=True)
        self.assertEqual(after["tasks"][0]["outcome"], "reproduced")
        self.assertEqual((self.run / "config.json").read_bytes(), original_config)
        for key in ("config", "config_sha256", "source_manifest", "memory_identity", "memory_scope", "model_verifier"):
            self.assertEqual(after["metadata"][key], before["metadata"][key])
        self.assertNotEqual(after["metadata"]["recovery_memory_scope"], before["metadata"]["memory_scope"])
        start = next(event["data"] for event in after["events"] if event["kind"] == "review_recovery_started")
        self.assertEqual(start["effective_verifier_config"]["max_tokens"], 4096)
        self.assertTrue(start["all_reviews"])
        effective = json.loads((self.run / start["effective_config_file"]).read_text())
        self.assertEqual(effective["verifier"]["max_tokens"], 4096)
        effective["verifier"]["max_tokens"] = 1024
        self.assertEqual(effective, config)
        self.assertEqual(after["trials"], before["trials"])

    def test_all_reviews_retries_a_conclusive_candidate_once(self):
        before = cli.execute(self.config(memory=True), self.run)
        after = recovery.recover_run(self.run, all_reviews=True)
        self.assertEqual(after["tasks"][0]["outcome"], "reproduced")
        self.assertEqual(after["trials"], before["trials"])
        starts = [event for event in after["events"] if event["kind"] == "review_recovery_task_started"]
        self.assertEqual(len(starts), 1)
        self.assertGreater(after["model_usage"]["logical_calls"], before["model_usage"]["logical_calls"])

    def test_memory_skip_is_not_accepted_as_a_new_review(self):
        with patch.object(cli, "prepare_model", side_effect=self.fixture_preparer()):
            cli.execute(self.config(memory=True), self.run)
        with patch.object(recovery._RecoveryMemory, "claim", return_value={"claimed": False, "reason": "pending", "record": {}}):
            after = recovery.recover_run(self.run)
        self.assertEqual(after["tasks"][0]["outcome"], "memory_skipped")
        self.assertFalse(after["metadata"]["recovery_acceptance"]["passed"])
        self.assertEqual(after["metadata"]["recovery_acceptance"]["missing_candidate_ids"], [after["tasks"][0]["id"]])

    def test_aged_cached_trials_do_not_receive_a_new_freshness_window(self):
        aged = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        with patch.object(cli, "prepare_model", side_effect=self.fixture_preparer()), patch("recursive_redteam.store.now", return_value=aged):
            cli.execute(self.config(memory=True), self.run)
        after = recovery.recover_run(self.run)
        self.assertEqual(after["tasks"][0]["outcome"], "reproduced")
        self.assertEqual(after["metadata"]["recovery_memory_after"]["fresh_conclusive"], 0)
        with sqlite3.connect(self.root / "memory.sqlite") as db:
            encoded, expires = db.execute("SELECT evidence,expires_at FROM experiment_observations ORDER BY id DESC LIMIT 1").fetchone()
        evidence = json.loads(encoded)
        self.assertEqual(evidence["target_observed_at"], aged)
        self.assertGreater(datetime.fromisoformat(evidence["reviewed_at"]), datetime.fromisoformat(aged))
        self.assertLessEqual(expires, datetime.now(timezone.utc).timestamp())


class StoreContext:
    def __init__(self, path):
        self.store = Store(path)

    def __enter__(self):
        return self.store

    def __exit__(self, *args):
        self.store.close()


if __name__ == "__main__":
    unittest.main()
