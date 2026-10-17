import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from recursive_redteam.budget import BudgetedModel, ModelGate
from recursive_redteam.contracts import Completion, ModelError, ProviderError
from recursive_redteam.providers import OpenAICompatibleModel, _retry_delay
from recursive_redteam.store import Store


class ProviderDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "audit.sqlite")
        self.provider = OpenAICompatibleModel("https://provider.example/v1", "fixture-model",
                                             api_key_env="DIAGNOSTIC_TEST_KEY", max_retries=0)
        self.model = BudgetedModel(self.provider, self.store, 20, "target")
        self.messages = [{"role": "user", "content": "Return a fixture."}]

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def response(self, *, finish="stop", content="PRIVATE INVALID BODY", usage=None):
        return {"choices": [{"finish_reason": finish, "message": {"role": "assistant", "content": content}}],
                "usage": usage or {"prompt_tokens": 7, "completion_tokens": 11, "total_tokens": 18}}

    def test_http_status_is_retained_without_body_url_or_credentials(self):
        private = "SECRET-HEADER-AND-BODY"
        failure = HTTPError("https://private.example/?key=" + private, 429, private,
                            {"Retry-After": "0"}, io.BytesIO(private.encode()))
        with patch.dict(os.environ, {"DIAGNOSTIC_TEST_KEY": private}), \
                patch.object(self.provider._opener, "open", side_effect=failure), \
                self.assertRaises(ProviderError):
            self.model.complete(self.messages)
        saved = self.store.report()["model_calls"][0]
        self.assertEqual(saved["error"], "ProviderError")
        self.assertEqual(saved["response"]["diagnostic"], "Provider HTTP 429 after 1 attempt(s)")
        self.assertEqual(saved["usage"], {})
        self.assertNotIn(private, json.dumps(saved))
        self.assertNotIn("private.example", json.dumps(saved))

    def test_transport_error_has_fixed_diagnostic(self):
        with patch.dict(os.environ, {"DIAGNOSTIC_TEST_KEY": "fixture-secret"}), \
                patch.object(self.provider._opener, "open", side_effect=URLError("PRIVATE-REASON")), \
                self.assertRaises(ProviderError):
            self.model.complete(self.messages)
        saved = self.store.report()["model_calls"][0]
        self.assertEqual(saved["response"]["diagnostic"], "Provider connection failed or timed out")
        self.assertNotIn("PRIVATE-REASON", json.dumps(saved))

    def test_truncation_preserves_only_allowlisted_numeric_usage(self):
        usage = {"prompt_tokens": 7, "completion_tokens": 11, "total_tokens": 18,
                 "secret": "PRIVATE-USAGE", "unexpected_count": 42,
                 "prompt_tokens_details": {"cached_tokens": 3, "secret": "PRIVATE-CACHE"},
                 "completion_tokens_details": {"reasoning_tokens": 4, "audio_tokens": False}}
        with patch.object(self.provider, "_request_json", return_value=self.response(finish="length", usage=usage)), \
                self.assertRaises(ProviderError) as caught:
            self.model.complete(self.messages)
        saved = self.store.report()["model_calls"][0]
        self.assertIn("output-token limit", saved["response"]["diagnostic"])
        expected = {"prompt_tokens": 7, "completion_tokens": 11, "total_tokens": 18,
                    "prompt_tokens_details": {"cached_tokens": 3},
                    "completion_tokens_details": {"reasoning_tokens": 4}}
        self.assertEqual(saved["usage"], expected)
        self.assertEqual(caught.exception.usage, expected)
        self.assertNotIn("PRIVATE", json.dumps(saved))
        self.assertNotIn("unexpected_count", json.dumps(saved))
        self.assertEqual(self.store.report()["model_usage"]["reported_tokens"]["total_tokens"], 18)

    def test_filtered_invalid_finish_and_malformed_tool_diagnostics_are_distinct(self):
        malformed_call = self.response(finish="tool_calls", content=None)
        malformed_call["choices"][0]["message"]["tool_calls"] = [{"id": "one", "type": "function",
            "function": {"name": "lookup", "arguments": "PRIVATE NOT JSON"}}]
        cases = [(self.response(finish="content_filter"), "was filtered"),
                 (self.response(finish=["PRIVATE"]), "unsupported finish reason"),
                 (malformed_call, "malformed tool arguments"),
                 ({"choices": [], "usage": {"total_tokens": 13}}, "no completion choices")]
        for response, diagnostic in cases:
            with self.subTest(diagnostic=diagnostic), \
                    patch.object(self.provider, "_request_json", return_value=response), \
                    self.assertRaises(ProviderError):
                self.model.complete(self.messages)
            saved = self.store.report()["model_calls"][-1]
            self.assertIn(diagnostic, saved["response"]["diagnostic"])
            self.assertTrue(saved["usage"])
            self.assertNotIn("PRIVATE", json.dumps(saved))

    def test_json_mode_parse_failure_retains_usage_without_content(self):
        with patch.object(self.provider, "_request_json", return_value=self.response()), \
                self.assertRaises(ProviderError):
            self.model.complete(self.messages, json_mode=True)
        saved = self.store.report()["model_calls"][0]
        self.assertEqual(saved["response"]["diagnostic"], "Provider returned invalid JSON-mode content")
        self.assertEqual(saved["usage"]["total_tokens"], 18)
        self.assertNotIn("PRIVATE INVALID BODY", json.dumps(saved))

    def test_bad_usage_values_are_not_recorded_when_completion_fails(self):
        usage = {"prompt_tokens": -1, "completion_tokens": True, "total_tokens": "PRIVATE", 
                 "prompt_tokens_details": {"cached_tokens": -7}}
        with patch.object(self.provider, "_request_json", return_value=self.response(usage=usage)), \
                self.assertRaises(ProviderError):
            self.model.complete(self.messages)
        saved = self.store.report()["model_calls"][0]
        self.assertEqual(saved["usage"], {})
        self.assertIn("malformed token usage", saved["response"]["diagnostic"])
        self.assertNotIn("PRIVATE", json.dumps(saved))

    def test_generic_model_error_text_remains_unlogged(self):
        class CustomAdapter:
            def complete(self, messages, **kwargs):
                raise ModelError("PRIVATE custom response or credentials")
        with self.assertRaises(ModelError):
            BudgetedModel(CustomAdapter(), self.store, 20, "target").complete(self.messages)
        saved = self.store.report()["model_calls"][0]
        self.assertEqual(saved["error"], "ModelError")
        self.assertIsNone(saved["response"])
        self.assertNotIn("PRIVATE", json.dumps(saved))

    def test_retry_delays_are_longer_but_bounded(self):
        self.assertEqual(_retry_delay(None, 0), 10.0)
        self.assertEqual(_retry_delay(None, 2), 30.0)
        self.assertEqual(_retry_delay("nonsense", 1), 20.0)
        self.assertEqual(_retry_delay(None, 10), 30.0)
        self.assertEqual(_retry_delay("9999", 0), 30.0)

    def test_shared_gate_paces_roles_by_monotonic_start_without_real_sleep(self):
        clock = {"now": 100.0}
        starts, sleeps = [], []
        def sleep(seconds):
            sleeps.append(seconds)
            clock["now"] += seconds
        class Fixture:
            def complete(self, messages, **kwargs):
                starts.append(clock["now"])
                clock["now"] += 0.4
                return Completion(content="ok", model="fixture")
        gate = ModelGate(3)
        models = [BudgetedModel(Fixture(), self.store, 20, role, gate=gate)
                  for role in ("coordinator", "target", "verifier")]
        with patch("recursive_redteam.budget.time.monotonic", side_effect=lambda: clock["now"]), \
                patch("recursive_redteam.budget.time.sleep", side_effect=sleep):
            for model in models:
                model.complete(self.messages)
        self.assertEqual(starts, [100.0, 103.0, 106.0])
        self.assertEqual(len(sleeps), 2)
        self.assertAlmostEqual(sleeps[0], 2.6)
        self.assertAlmostEqual(sleeps[1], 2.6)

    def test_shared_gate_prevents_overlapping_calls(self):
        state = {"active": 0, "maximum": 0}
        lock = threading.Lock()
        class Fixture:
            def complete(self, messages, **kwargs):
                with lock:
                    state["active"] += 1
                    state["maximum"] = max(state["maximum"], state["active"])
                time.sleep(0.005)
                with lock:
                    state["active"] -= 1
                return Completion(content="ok", model="fixture")
        model = BudgetedModel(Fixture(), self.store, 20, "target", gate=ModelGate())
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: model.complete(self.messages), range(4)))
        self.assertEqual(state["maximum"], 1)
        self.assertEqual(len(self.store.report()["model_calls"]), 4)

    def test_gate_releases_after_call_error(self):
        class Broken:
            def complete(self, messages, **kwargs):
                raise ProviderError("Provider request failed")
        gate = ModelGate()
        model = BudgetedModel(Broken(), self.store, 20, "target", gate=gate)
        for _ in range(2):
            with self.assertRaises(ProviderError):
                model.complete(self.messages)
        self.assertEqual(len(self.store.report()["model_calls"]), 2)


if __name__ == "__main__":
    unittest.main()
