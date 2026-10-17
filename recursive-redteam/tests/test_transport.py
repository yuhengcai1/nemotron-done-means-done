import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from recursive_redteam.command_target import CommandTarget
from recursive_redteam.contracts import Candidate, ModelError
from recursive_redteam.providers import OpenAICompatibleModel


def completion(content="hello", **changes):
    data = {"model": "test", "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}}
    data.update(changes)
    return data


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"TEST_MODEL_KEY": "test-only-key"})
        self.env.start()
        self.model = OpenAICompatibleModel("http://localhost:1234", "test", api_key_env="TEST_MODEL_KEY", max_retries=1)
        self.model._opener = Mock()

    def tearDown(self): self.env.stop()

    def response(self, value): return io.BytesIO(json.dumps(value).encode())

    def test_request_envelope_and_usage(self):
        self.model._opener.open.return_value = self.response(completion())
        reply = self.model.complete([{"role": "user", "content": "hello"}])
        self.assertEqual(reply.usage["total_tokens"], 6)
        request = self.model._opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://localhost:1234/v1/chat/completions")
        self.assertEqual(json.loads(request.data)["max_tokens"], 1024)
        self.assertNotIn("test-only-key", json.dumps(self.model.describe()))

    def test_retry_429_and_scrub_error(self):
        error = HTTPError("http://localhost", 429, "secret-body", {"Retry-After": "0"}, io.BytesIO(b"test-only-key"))
        self.model._opener.open.side_effect = [error, self.response(completion())]
        with patch("recursive_redteam.providers.time.sleep"):
            self.assertEqual(self.model.complete([{"role": "user", "content": "x"}]).content, "hello")
        self.assertEqual(self.model.request_count, 2)
        self.model._opener.open.side_effect = HTTPError("http://localhost", 401, "secret-body", {}, io.BytesIO(b"test-only-key"))
        with self.assertRaises(ModelError) as raised: self.model.list_models()
        self.assertNotIn("test-only-key", str(raised.exception))
        self.assertNotIn("secret-body", str(raised.exception))

    def test_truncation_and_malformed_calls_are_errors(self):
        for data in ({"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]},
                     {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [{"id": "x"}]}}]}):
            self.model._opener.open.return_value = self.response(data)
            with self.assertRaises(ModelError): self.model.complete([{"role": "user", "content": "x"}])

    def test_catalogue_selects_supported_token_limit(self):
        self.model._opener.open.side_effect = [self.response({"data": [{"id": "test", "supported_parameters": ["max_completion_tokens"]}]}), self.response(completion())]
        self.model.list_models()
        self.model.complete([{"role": "user", "content": "x"}])
        body = json.loads(self.model._opener.open.call_args.args[0].data)
        self.assertEqual(body["max_completion_tokens"], 1024)
        self.assertNotIn("max_tokens", body)
        with self.assertRaises(ModelError): self.model.complete([{"role": "user", "content": "x"}], tools=[{"type": "function"}])

    def test_no_unbounded_generation_if_catalogue_lacks_token_limit(self):
        self.model._catalogue = [{"id": "test", "supported_parameters": ["tools"]}]
        with self.assertRaises(ModelError): self.model.complete([{"role": "user", "content": "x"}])
        self.model._opener.open.assert_not_called()

    def test_no_inline_url_credentials(self):
        with self.assertRaises(ValueError): OpenAICompatibleModel("https://user:secret@example.com/v1", "test")


class CommandTests(unittest.TestCase):
    def test_json_protocol(self):
        code = "import json,sys; r=json.load(sys.stdin); print(json.dumps({'status':'passed','metrics':{'trial':r['trial_id']}}))"
        target = CommandTarget([sys.executable, "-c", code], idempotent=True)
        result = target.evaluate(Candidate("id", "fixture", "test", "$(should-not-execute)"), trial_id="t")
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.metrics["trial"], "t")
        self.assertTrue(target.describe()["resume_safe"])

    def test_timeout_output_limit_and_invalid_result(self):
        cases = [("import time; time.sleep(3)", {"timeout": 0.05}),
                 ("print('x'*5000)", {"max_output_bytes": 64}),
                 ("print('{\"status\":\"violation\"}')", {})]
        for code, options in cases:
            with self.subTest(code=code):
                result = CommandTarget([sys.executable, "-c", code], **options).evaluate(None, trial_id="t")
                self.assertEqual(result.status, "error")

    def test_stderr_secret_not_exposed(self):
        code = "import sys; print('credential-example',file=sys.stderr); sys.exit(2)"
        result = CommandTarget([sys.executable, "-c", code]).evaluate(None, trial_id="t")
        self.assertNotIn("credential-example", json.dumps(result.to_dict()))


if __name__ == "__main__": unittest.main()
