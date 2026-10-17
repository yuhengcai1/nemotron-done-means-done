"""One persistent allowance shared by every model role and concurrent worker."""
from __future__ import annotations

import json
import math
import re
import threading
import time
from contextlib import nullcontext
from typing import Any

from .contracts import BudgetExceeded, Completion, Model, ProviderError


def _tool_call(call: dict) -> dict:
    function = call.get("function", {})
    return {**{key: call[key] for key in ("id", "type") if key in call},
            "function": {key: function[key] for key in ("name", "arguments") if key in function}}


def _request_snapshot(messages: list[dict], tools: list[dict] | None, json_mode: bool) -> dict:
    """Capture model-visible protocol fields only, never transport credentials."""
    transcript = []
    for message in messages:
        item = {key: message[key] for key in ("role", "content", "name", "tool_call_id") if key in message}
        if "tool_calls" in message:
            item["tool_calls"] = [_tool_call(call) for call in message["tool_calls"]]
        transcript.append(item)
    definitions = None
    if tools is not None:
        definitions = []
        for tool in tools:
            function = tool.get("function", {})
            definitions.append({**({"type": tool["type"]} if "type" in tool else {}),
                                "function": {key: function[key] for key in
                                             ("name", "description", "parameters", "strict") if key in function}})
    # Detach nested objects before the model or caller can mutate them.
    return json.loads(json.dumps({"messages": transcript, "tools": definitions, "json_mode": json_mode},
                                 ensure_ascii=False, allow_nan=False))


def _operation_role(default: str, messages: list[dict]) -> str:
    if default == "target":
        return "target"
    roles = {"COORDINATOR": "coordinator", "ATTACKER": "worker", "VERIFIER": "verifier"}
    for message in messages:
        if message.get("role") == "system" and isinstance(message.get("content"), str):
            marker = re.search(r"(?:^|\s)ROLE:(COORDINATOR|ATTACKER|VERIFIER)(?=\s|$)", message["content"])
            if marker:
                return roles[marker[1]]
    return default


class ModelGate:
    """Serialize provider calls across roles and pace their monotonic start times.

    Share one instance across all BudgetedModel wrappers using one provider
    account. The lock remains held throughout the call, including bounded retries.
    """

    def __init__(self, min_interval_seconds: float = 0):
        if (isinstance(min_interval_seconds, bool) or not isinstance(min_interval_seconds, (int, float))
                or not math.isfinite(min_interval_seconds) or min_interval_seconds < 0):
            raise ValueError("min_interval_seconds must be finite and nonnegative")
        self.min_interval_seconds = float(min_interval_seconds)
        self._lock = threading.Lock()
        self._last_start: float | None = None

    def __enter__(self):
        self._lock.acquire()
        try:
            if self._last_start is not None:
                remaining = self.min_interval_seconds - (time.monotonic() - self._last_start)
                if remaining > 0:
                    time.sleep(remaining)
            self._last_start = time.monotonic()
        except BaseException:
            self._lock.release()
            raise
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._lock.release()
        return False


class BudgetedModel:
    def __init__(self, model: Model, store: Any, maximum: int, role: str, *, gate: ModelGate | None = None):
        self.model = model
        self.store = store
        self.maximum = maximum
        self.role = role
        self.gate = gate
        self.simulated = bool(getattr(model, "simulated", False))

    def complete(self, messages: list[dict], *, tools: list[dict] | None = None,
                 json_mode: bool = False) -> Completion:
        with self.gate if self.gate is not None else nullcontext():
            return self._complete(messages, tools=tools, json_mode=json_mode)

    def _complete(self, messages: list[dict], *, tools: list[dict] | None = None,
                  json_mode: bool = False) -> Completion:
        role = _operation_role(self.role, messages)
        request = _request_snapshot(messages, tools, json_mode)
        call_id = self.store.reserve_model_call(self.maximum, role=role, request=request)
        if call_id is None:
            raise BudgetExceeded("campaign model-call budget exhausted")
        try:
            completion = self.model.complete(messages, tools=tools, json_mode=json_mode)
        except ProviderError as exc:
            # Only this trusted adapter type carries deliberately scrubbed text.
            # A generic ModelError may contain response bodies or credentials.
            self.store.finish_model_call(call_id, role, exc.usage, error=type(exc).__name__,
                                         response={"diagnostic": exc.diagnostic, "usage": exc.usage})
            raise
        except BaseException as exc:
            # Exception text can contain endpoint response bodies or credentials.
            self.store.finish_model_call(call_id, role, {}, error=type(exc).__name__)
            raise
        response = {"model": completion.model, "content": completion.content,
                    "tool_calls": [_tool_call(call) for call in completion.tool_calls],
                    "finish_reason": completion.finish_reason, "usage": completion.usage}
        self.store.finish_model_call(call_id, role, completion.usage,
                                     response=response, actual_model=completion.model)
        return completion
