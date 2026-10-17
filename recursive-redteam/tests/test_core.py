from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from recursive_redteam.budget import BudgetedModel
from recursive_redteam.cli import execute, run_lock
from recursive_redteam.config import demo_config, fingerprint, validate_config
from recursive_redteam.contracts import BudgetExceeded, Candidate, Limits, TrialResult
from recursive_redteam.engine import Engine
from recursive_redteam.evidence import import_evidence
from recursive_redteam.providers import DemoModel
from recursive_redteam.store import Store
from recursive_redteam.targets import PolicySandbox


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "test.sqlite")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def engine(self, target=None, **limits):
        settings = Limits(max_rounds=2, max_candidates=4, max_model_calls=100, **limits)
        planner = BudgetedModel(DemoModel("coordinator"), self.store, settings.max_model_calls, "planner")
        target = target or PolicySandbox(BudgetedModel(DemoModel("target"), self.store, settings.max_model_calls, "target"))
        return Engine(planner, target, self.store, settings)

    def test_recursive_end_to_end_and_complete_resume_no_calls(self):
        engine = self.engine()
        report = engine.run()
        self.assertEqual(report["status"], "completed")
        self.assertTrue(report["simulated"])
        self.assertEqual(report["counts"]["reproduced"], 4)
        self.assertEqual(report["counts"]["distinct_oracle_signatures"], 1)
        self.assertTrue(any(t["parent_id"] and t["depth"] == 1 for t in report["tasks"]))
        calls = report["model_usage"]["logical_calls"]
        self.assertEqual(engine.run(resume=True)["model_usage"]["logical_calls"], calls)
        for finding in report["findings"]:
            self.assertEqual(finding["replay_count"], 2)
            self.assertEqual(finding["paired_control"], "passed")

    def test_guarded_fixture_blocks_but_completes(self):
        target = PolicySandbox(DemoModel("target"), guarded=True)
        candidate = Candidate("c", "tool-output-injection", "fixture", "[DEMO_INJECT]")
        result = target.evaluate(candidate, trial_id="guarded")
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.metrics["blocked_calls"], 1)

    def test_control_failure_stops_without_planning(self):
        class FailingControl:
            def describe(self): return {"simulated": True}
            def evaluate(self, candidate, *, trial_id): return TrialResult("error", error="fixture incomplete")
        result = self.engine(FailingControl()).run()
        self.assertEqual(result["status"], "control_failed")
        self.assertEqual(result["tasks"], [])
        self.assertEqual(result["model_usage"]["logical_calls"], 0)

    def test_mismatched_replay_is_not_reproduced(self):
        class DifferentFailure:
            def describe(self): return {"simulated": True, "idempotent": True}
            def evaluate(self, candidate, *, trial_id):
                if candidate is None: return TrialResult("passed")
                oracle = "one" if trial_id.endswith(":initial") else "two"
                return TrialResult("violation", evidence=[{"oracle": oracle}])
        result = self.engine(DifferentFailure()).run()
        self.assertEqual(result["counts"]["reproduced"], 0)
        self.assertEqual(result["counts"]["inconclusive"], 4)

    def test_paired_control_receives_original_case(self):
        received = []
        class PairedTarget:
            def describe(self): return {"simulated": True, "idempotent": True}
            def evaluate(self, candidate, *, trial_id):
                if candidate is None: return TrialResult("passed")
                return TrialResult("violation", evidence=[{"oracle": "fixture"}])
            def evaluate_control(self, candidate, *, trial_id):
                received.append((candidate.id, trial_id))
                return TrialResult("passed")
        result = self.engine(PairedTarget()).run()
        self.assertEqual(len(received), 4)
        self.assertEqual(result["counts"]["reproduced"], 4)
        self.assertTrue(all(trial.startswith(result["metadata"]["campaign_id"] + ":") for _, trial in received))

    def test_budget_is_shared_atomic_and_persistent(self):
        def reserve(_):
            try: return self.store.reserve_model_call(7)
            except BudgetExceeded: return None
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(reserve, range(40)))
        self.assertEqual(sum(x is not None for x in results), 7)
        with self.assertRaises(BudgetExceeded): self.store.reserve_model_call(7)
        self.assertEqual(self.store.report()["model_usage"]["unknown_usage_calls"], 7)

    def test_budget_exhaustion_not_finding(self):
        limits = Limits(max_model_calls=2)
        model = BudgetedModel(DemoModel("target"), self.store, 2, "target")
        result = Engine(model, PolicySandbox(model), self.store, limits).run()
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["model_usage"]["logical_calls"], 2)

    def test_interrupted_non_idempotent_trial_requires_review(self):
        class External:
            def describe(self): return {"idempotent": False}
            def evaluate(self, candidate, *, trial_id): raise AssertionError("must not retry")
        engine = self.engine(External())
        self.store.set_metadata("started", True)
        self.store.set_metadata("target", engine.description)
        self.store.begin_trial("control-initial")
        result = engine.run(resume=True)
        self.assertEqual(result["status"], "manual_review_required")

    def test_interrupted_sandbox_resumes(self):
        engine = self.engine()
        self.store.set_metadata("started", True)
        self.store.set_metadata("target", engine.description)
        self.store.begin_trial("control-initial")
        result = engine.run(resume=True)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["counts"]["reproduced"], 4)

    def test_failed_idempotent_control_can_resume_without_losing_error(self):
        engine = self.engine()
        self.store.set_metadata("started", True)
        self.store.set_metadata("target", engine.description)
        self.store.begin_trial("control-initial")
        self.store.finish_trial("control-initial", TrialResult("error", error="transient fixture failure"))
        report = engine.run(resume=True)
        self.assertEqual(report["status"], "completed")
        errors = [e for e in report["events"] if e["kind"] == "trial_finished" and e["data"]["status"] == "error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["data"]["result"]["error"], "transient fixture failure")

    def test_configured_provider_end_to_end_and_completed_resume_no_preflight(self):
        config = validate_config({"planner": {"base_url": "http://localhost:1234/v1", "model": "fixture"},
                                  "target": {"kind": "policy-sandbox"},
                                  "limits": {"max_rounds": 1, "max_candidates": 1, "max_model_calls": 40}})
        demo = DemoModel("target")
        def fake_request(instance, endpoint, body=None):
            if endpoint == "/models":
                return {"data": [{"id": "fixture", "supported_parameters": ["tools", "tool_choice", "max_tokens", "temperature"]}]}
            response = demo.complete(body["messages"], tools=body.get("tools"))
            return {"model": "fixture", "choices": [{"finish_reason": response.finish_reason,
                    "message": {"role": "assistant", "content": response.content, "tool_calls": response.tool_calls}}], "usage": response.usage}
        root = self.root / "configured-provider"
        with patch("recursive_redteam.providers.OpenAICompatibleModel._request_json", new=fake_request):
            result = execute(config, root)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["counts"]["reproduced"], 1)
        self.assertIn("model_target", result["metadata"])
        with patch("recursive_redteam.providers.OpenAICompatibleModel._request_json", side_effect=AssertionError("no request on completed resume")):
            self.assertEqual(execute(config, root, resume=True)["status"], "completed")

    def test_imported_verified_claim_not_endorsed(self):
        source = self.root / "verdict.json"
        source.write_text(json.dumps({"confirmed_failure": True, "status": "verified", "api_key": "do-not-copy"}))
        result = import_evidence(source, self.root / "imports")
        self.assertEqual(result["classification"], "external_unverified")
        self.assertNotIn("do-not-copy", Path(result["artifact_path"]).read_text())

    def test_missing_oracle_evidence_rejected(self):
        with self.assertRaises(ValueError): TrialResult("violation")

    def test_config_no_inline_key_or_limit_override(self):
        config = demo_config()
        config["planner"] = {"kind": "openai-compatible", "base_url": "http://localhost:1/v1", "model": "fixture", "api_key": "secret"}
        with self.assertRaises(ValueError): validate_config(config)
        config["planner"].pop("api_key")
        config["planner"]["request_parameters"] = {"max_tokens": 100000}
        with self.assertRaises(ValueError): validate_config(config)

    def test_cli_resume_fingerprint_and_file_lock(self):
        root = self.root / "run"
        config = demo_config()
        result = execute(config, root)
        self.assertEqual(result["status"], "completed")
        changed = json.loads(json.dumps(config))
        changed["limits"]["max_candidates"] += 1
        with self.assertRaises(ValueError): execute(changed, root, resume=True)
        self.assertEqual(execute(config, root, resume=True)["status"], "completed")
        with run_lock(root):
            with self.assertRaises(ValueError):
                with run_lock(root): pass


if __name__ == "__main__": unittest.main()
