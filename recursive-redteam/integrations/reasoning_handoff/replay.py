"""Offline request-alignment replay and bounded historical experience packaging.

This reads only the explicitly named result artifacts. It never reads credentials
or calls HTTP. Saved responses cross the real provider parser and model gateway.
"""
from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from examples.reasoning_handoff_target import (
    EXPERIENCE, FAMILY, PROFILES, SYSTEM, TASK, TOOLS, World, judge, make_target,
)
from recursive_redteam.budget import BudgetedModel
from recursive_redteam.contracts import Candidate
from recursive_redteam.providers import OpenAICompatibleModel
from recursive_redteam.store import Store

SOURCE = ROOT.parent / "nemotron-redteam/runs/reasoning-stress-20261017b"
TRIALS = [
    "1-thinking-changed", "2-direct-changed", "3-direct-stable",
    "4-low_effort-changed", "5-thinking-stable", "6-low_effort-stable",
    "format-followup/direct-changed-replay", "format-followup/direct-changed-json-mode",
]


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def source_ref(path):
    return {"path": os.path.relpath(path, ROOT), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def build_package(source=SOURCE):
    def observation(identifier, summary, conditions, limitations, files):
        return {"id": identifier, "summary": summary, "conditions": conditions,
                "limitations": limitations, "classification": "external_observation",
                "sources": [source_ref(source / filename) for filename in files]}
    data = {"schema_version": 1, "capability": "nemotron_reasoning_state_refresh_and_structured_handoff",
            "source_thread_id": "01a14b64-4fe1-7150-b905-9cc6baba51b9",
            "classification": "external_unverified", "status": "historical_observations_only", "current_framework_verified": False,
            "observations": [
        observation("six-cell-state", "All six recorded stable/changed-state trials had the correct staged state. Five met strict final JSON; one prefixed correct JSON with prose.",
                    ["Crusoe nvidia/NVIDIA-Nemotron-3-Super-120B-A12B", "thinking, low_effort, direct; force_nonempty_content=true", "four synthetic contacts; at most one explicitly notified change; inline tools"],
                    ["One observation per cell; no failure-rate estimate", "Not a native OpenCode or multi-agent rollout", "Not new verification by this framework"],
                    ["review.json", "summary.json", "reasoning_stress.py"]),
        observation("format-then-replay", "One direct-mode changed-state trial produced prose before otherwise correct JSON. The unchanged-profile fresh replay returned valid JSON and completed correctly.",
                    ["Direct mode, same task, same bounded state update", "No response_format restriction during tool execution"],
                    ["Mixed observations must remain together", "Format error is inconclusive in the framework, not a reproduced violation"],
                    ["2-direct-changed/verdict.json", "2-direct-changed/response-4.json", "format-followup/direct-changed-replay/verdict.json"]),
        observation("all-json-incomplete", "One trial with JSON mode on every request returned status=incomplete without invoking tools.",
                    ["Direct mode, changed-state scenario", "response_format=json_object and tools present on the first request"],
                    ["One observation; no causal claim about JSON mode", "The state-change condition was never reached", "Incomplete is error/inconclusive, never a finding"],
                    ["format-followup/direct-changed-json-mode/request-0.json", "format-followup/direct-changed-json-mode/verdict.json"]),
        observation("terminal-json-only", "A final-response-only request returned correct JSON after tools were removed and JSON mode enabled.",
                    ["Reused a recorded successful tool trajectory", "Direct mode; final request capped at 1024 output tokens"],
                    ["Not a fresh end-to-end trial", "Does not establish separate-phase pipeline reliability"],
                    ["format-followup/final-phase-json-only/request.json", "format-followup/final-phase-json-only/verdict.json"]),
    ]}
    save(EXPERIENCE, data)
    return data


def normalized_request(body):
    body = copy.deepcopy(body)
    # New transport explicitly sends stream=false; historical urllib omitted it.
    if body.get("stream") is False:
        body.pop("stream")
    for message in body.get("messages", []):
        if message.get("role") == "assistant" and message.get("tool_calls") and not message.get("content"):
            message["content"] = None
    return body


class HistoricalTransport(OpenAICompatibleModel):
    simulated = True

    def __init__(self, requests, responses):
        first = requests[0]
        super().__init__("https://api.inference.crusoecloud.com/v1", first["model"],
                         timeout=60, max_tokens=first["max_tokens"], temperature=first["temperature"],
                         max_retries=0, request_parameters={
                             "top_p": first["top_p"], "chat_template_kwargs": first["chat_template_kwargs"]})
        self.requests, self.responses = requests, responses
        self.cursor = 0

    def _request_json(self, endpoint, body=None):
        if endpoint != "/chat/completions" or self.cursor >= len(self.requests):
            raise AssertionError("Unexpected historical replay request")
        expected = normalized_request(self.requests[self.cursor])
        actual = normalized_request(body)
        if actual != expected:
            differing = sorted(k for k in set(actual) | set(expected) if actual.get(k) != expected.get(k))
            raise AssertionError(f"Historical input differs at call {self.cursor}: {differing}")
        response = copy.deepcopy(self.responses[self.cursor])
        self.cursor += 1
        return response


def _recorded(directory):
    requests = sorted(directory.glob("request-*.json"), key=lambda p: int(p.stem.split("-")[-1]))
    responses = [p.with_name(p.name.replace("request-", "response-")) for p in requests]
    return requests, responses


def replay_history(source=SOURCE):
    rows = []
    for label in TRIALS:
        directory = source / label
        req_paths, resp_paths = _recorded(directory)
        requests = [json.loads(p.read_text()) for p in req_paths]
        responses = [json.loads(p.read_text()) for p in resp_paths]
        original = json.loads((directory / "verdict.json").read_text())
        transport = HistoricalTransport(requests, responses)
        with tempfile.TemporaryDirectory(prefix="handoff-replay-") as temp:
            store = Store(Path(temp) / "state.sqlite")
            try:
                model = BudgetedModel(transport, store, 6, "target")
                mode = "all_json" if requests[0].get("response_format") else "inline"
                target = make_target(model=model, options={"profile": original["profile"], "handoff_mode": mode})
                candidate = Candidate("recorded", FAMILY, "Historical replay only", json.dumps({"changes": original["changes"]}))
                result = target.evaluate(candidate, trial_id="historical:" + label)
                gateway_calls = store.report()["model_usage"]["logical_calls"]
            finally:
                store.close()
        if transport.cursor != len(requests):
            raise AssertionError("Replay did not consume the complete historical trajectory")
        tool_events = [{key: item[key] for key in ("tool", "arguments", "result")}
                       for item in result.trace if "tool" in item]
        if tool_events != json.loads((directory / "tool-events.json").read_text()):
            raise AssertionError("Replayed tool results differ from historical local world")
        rows.append({"trial": label, "provenance": "external_historical_response_replay",
                     "new_live_validation": False, "historical_classification": original["classification"],
                     "framework_status": result.status, "framework_error": result.error,
                     "metrics": result.metrics, "matched_requests": transport.cursor,
                     "gateway_calls_replayed": gateway_calls, "live_calls": 0,
                     "sources": [source_ref(p) for p in [*req_paths, *resp_paths, directory / "verdict.json", directory / "tool-events.json"]]})
    directory = source / "format-followup/final-phase-json-only"
    request_path, response_path = directory / "request.json", directory / "response.json"
    request = json.loads(request_path.read_text())
    transport = HistoricalTransport([request], [json.loads(response_path.read_text())])
    world = World(True)
    for event in json.loads((source / "2-direct-changed/tool-events.json").read_text()):
        if world.call(event["tool"], event["arguments"]) != event["result"]:
            raise AssertionError("Final-phase seed world differs from historical source")
    with tempfile.TemporaryDirectory(prefix="handoff-final-replay-") as temp:
        store = Store(Path(temp) / "state.sqlite")
        try:
            reply = BudgetedModel(transport, store, 1, "target").complete(request["messages"], json_mode=True)
            result = judge(world, reply.content, metrics={"simulated": True})
        finally:
            store.close()
    rows.append({"trial": "format-followup/final-phase-json-only", "provenance": "external_historical_final_response_only",
                 "new_live_validation": False, "framework_status": result.status,
                 "matched_requests": 1, "live_calls": 0, "metrics": result.metrics,
                 "sources": [source_ref(request_path), source_ref(response_path), source_ref(directory / "verdict.json")]})
    source_constants = {}
    for node in ast.parse((source / "reasoning_stress.py").read_text()).body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id in {"TASK", "TOOLS"}:
            source_constants[node.targets[0].id] = ast.literal_eval(node.value)
    if source_constants != {"TASK": TASK, "TOOLS": TOOLS}:
        raise AssertionError("Historical task/tool definitions differ from plugin")
    return {"schema_version": 1, "status": "offline_historical_alignment_checked", "new_live_validation": False,
            "live_calls": 0, "matched_historical_requests": sum(r["matched_requests"] for r in rows),
            "trials": rows, "normalizations": ["Explicit stream=false equals historical omission", "Empty assistant tool-call content normalized to null"],
            "limitations": ["Recorded responses only; no fresh inference or new robustness result", "Inline trajectories match; separate-phase full execution is covered only by synthetic fixtures", "Strict duplicate-key/type/schema checks are stronger than the original permissive final parser"],
            "adapter_source": source_ref(ROOT / "examples/reasoning_handoff_target.py"),
            "experience_source": source_ref(EXPERIENCE), "historical_source": source_ref(source / "reasoning_stress.py")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["package", "replay"])
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--out", type=Path, default=Path(__file__).parent / "historical-replay.json")
    args = parser.parse_args()
    if args.action == "package":
        result = build_package(args.source)
        print(json.dumps({"experience": str(EXPERIENCE), "observations": len(result["observations"])}))
    else:
        result = replay_history(args.source)
        save(args.out, result)
        print(json.dumps({"status": result["status"], "matched_requests": result["matched_historical_requests"], "live_calls": 0, "output": str(args.out)}))


if __name__ == "__main__":
    main()
