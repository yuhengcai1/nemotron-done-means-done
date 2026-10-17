"""Configuration is trusted operator input, never a model-generated artifact."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .contracts import Limits


def _keys(value: dict, allowed: set[str], where: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"unknown {where} keys: {', '.join(sorted(unknown))}")


def _number(value: Any, name: str, low: float, high: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")


def model_config(raw: Any, where: str) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"{where} must be an object")
    _keys(raw, {"kind", "base_url", "model", "api_key_env", "timeout", "max_tokens",
                "temperature", "max_retries", "request_parameters"}, where)
    result = dict(raw)
    kind = result.setdefault("kind", "openai-compatible")
    if kind == "demo":
        if set(result) != {"kind"}:
            raise ValueError("demo model accepts only kind")
        return result
    if kind != "openai-compatible":
        raise ValueError(f"unsupported model kind: {kind}")
    for name in ("base_url", "model"):
        if not isinstance(result.get(name), str) or not result[name].strip():
            raise ValueError(f"{where}.{name} is required")
    result.setdefault("api_key_env", "CRUSOE_API_KEY")
    if not isinstance(result["api_key_env"], str) or not result["api_key_env"].isidentifier():
        raise ValueError("api_key_env must be an environment variable name")
    for name, default, low, high in (("timeout", 60, 1, 300), ("max_tokens", 1024, 1, 32768),
                                     ("temperature", 0.3, 0, 2), ("max_retries", 2, 0, 5)):
        result.setdefault(name, default)
        _number(result[name], f"{where}.{name}", low, high)
    for name in ("max_tokens", "max_retries"):
        if type(result[name]) is not int:
            raise ValueError(f"{where}.{name} must be an integer")
    parameters = result.setdefault("request_parameters", {})
    if not isinstance(parameters, dict):
        raise ValueError("request_parameters must be an object")
    # Credentials must never be persisted as provider parameters.
    for key in parameters:
        if key.lower() in {"api_key", "key", "token", "access_token", "secret", "authorization", "password"}:
            raise ValueError("credential-like request parameter names are not accepted")
        if key in {"model", "messages", "tools", "tool_choice", "stream", "n", "response_format",
                   "max_tokens", "max_completion_tokens", "temperature"}:
            raise ValueError("request_parameters cannot override protocol or output limits")
    return result


def validate_config(raw: Any, *, base: Path | None = None) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("configuration must be an object")
    _keys(raw, {"version", "planner", "verifier", "target", "limits", "memory", "request_interval_seconds"}, "configuration")
    interval = raw.get("request_interval_seconds", 0)
    _number(interval, "request_interval_seconds", 0, 60)
    if raw.get("version", 1) != 1:
        raise ValueError("unsupported configuration version")
    planner = model_config(raw.get("planner"), "planner")
    verifier = model_config(raw.get("verifier", planner), "verifier")
    target = raw.get("target")
    if not isinstance(target, dict):
        raise ValueError("target must be an object")
    target = dict(target)
    kind = target.get("kind", "policy-sandbox")
    target["kind"] = kind
    if kind == "policy-sandbox":
        _keys(target, {"kind", "model", "max_steps", "guarded"}, "target")
        target["model"] = model_config(target.get("model", planner), "target.model")
        target.setdefault("max_steps", 6)
        if type(target["max_steps"]) is not int or not 3 <= target["max_steps"] <= 24:
            raise ValueError("target.max_steps must be an integer in 3..24")
        target.setdefault("guarded", False)
        if type(target["guarded"]) is not bool:
            raise ValueError("target.guarded must be boolean")
    elif kind == "command":
        _keys(target, {"kind", "command", "cwd", "timeout", "idempotent", "env_allowlist", "description"}, "target")
        command = target.get("command")
        if not isinstance(command, list) or not command or any(not isinstance(x, str) or not x or "\x00" in x for x in command):
            raise ValueError("target.command must be a nonempty argv list")
        target.setdefault("timeout", 60)
        _number(target["timeout"], "target.timeout", 1, 600)
        target.setdefault("idempotent", False)
        if type(target["idempotent"]) is not bool:
            raise ValueError("target.idempotent must be boolean")
        target.setdefault("env_allowlist", [])
        if not isinstance(target["env_allowlist"], list) or any(not isinstance(x, str) or not x.isidentifier() for x in target["env_allowlist"]):
            raise ValueError("env_allowlist must contain environment variable names")
        if "description" in target and not isinstance(target["description"], dict):
            raise ValueError("target.description must be an object")
        if "cwd" in target:
            if not isinstance(target["cwd"], str):
                raise ValueError("target.cwd must be a path string")
            path = Path(target["cwd"]).expanduser()
            if not path.is_absolute():
                path = (base or Path.cwd()) / path
            target["cwd"] = str(path.resolve())
    elif kind == "plugin":
        _keys(target, {"kind", "factory", "options", "model"}, "target")
        factory = target.get("factory")
        if not isinstance(factory, str) or factory.count(":") != 1:
            raise ValueError("target.factory must be module:factory")
        target.setdefault("options", {})
        if not isinstance(target["options"], dict):
            raise ValueError("target.options must be an object")
        if "model" in target:
            target["model"] = model_config(target["model"], "target.model")
    else:
        raise ValueError(f"unsupported target kind: {kind}")
    limits = raw.get("limits", {})
    if not isinstance(limits, dict):
        raise ValueError("limits must be an object")
    _keys(limits, set(Limits.__dataclass_fields__), "limits")
    memory = raw.get("memory", {"enabled": False})
    if not isinstance(memory, dict):
        raise ValueError("memory must be an object")
    memory = dict(memory)
    _keys(memory, {"enabled", "path", "conclusion_ttl_seconds", "lease_seconds", "force_retest", "target_revision"}, "memory")
    memory.setdefault("enabled", False)
    memory.setdefault("force_retest", False)
    for field in ("enabled", "force_retest"):
        if type(memory[field]) is not bool:
            raise ValueError(f"memory.{field} must be boolean")
    for field, default, minimum in (("conclusion_ttl_seconds", 86400, 0), ("lease_seconds", 3600, 1)):
        memory.setdefault(field, default)
        _number(memory[field], f"memory.{field}", minimum, 31536000)
    if memory["enabled"] or "path" in memory:
        if not isinstance(memory.get("path"), str) or not memory["path"].strip():
            raise ValueError("enabled memory requires a path")
        path = Path(memory["path"]).expanduser()
        if not path.is_absolute():
            path = (base or Path.cwd()) / path
        memory["path"] = str(path.resolve())
    if memory["enabled"] and kind != "policy-sandbox":
        if not isinstance(memory.get("target_revision"), str) or not memory["target_revision"].strip():
            raise ValueError("external target memory requires target_revision identifying adapter code, oracle and environment")
    return {"version": 1, "planner": planner, "verifier": verifier, "target": target,
            "limits": asdict(Limits(**limits)), "memory": memory, "request_interval_seconds": interval}


def load_config(path: Path) -> dict:
    return validate_config(json.loads(path.read_text(encoding="utf-8")), base=path.resolve().parent)


def fingerprint(config: dict) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def demo_config() -> dict:
    return validate_config({"planner": {"kind": "demo"}, "target": {"kind": "policy-sandbox", "model": {"kind": "demo"}},
                            "limits": {"max_rounds": 2, "max_candidates": 4, "parallelism": 3,
                                       "max_model_calls": 100, "replays": 2, "max_depth": 2}})
