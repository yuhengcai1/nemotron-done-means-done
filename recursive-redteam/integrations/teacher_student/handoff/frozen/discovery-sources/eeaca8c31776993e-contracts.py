"""Stable contracts shared by model adapters, targets and the durable engine.

The model proposes text. Only trusted host code executes tools and judges outcomes.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol


class ModelError(RuntimeError):
    """A provider, response, transport or protocol failure (never a finding)."""


class ProviderError(ModelError):
    """A trusted transport diagnostic, safe to retain without the response body.

    Only the provider adapter should construct this type, using fixed messages
    or validated HTTP status/attempt counts. Arbitrary adapter errors remain
    ModelError and their text must not be persisted.
    """

    def __init__(self, diagnostic: str, *, usage: Any = None):
        super().__init__(diagnostic)
        self.diagnostic = diagnostic
        self.usage: dict[str, Any] = {}
        if isinstance(usage, dict):
            for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
                value = usage.get(name)
                if type(value) is int and value >= 0:
                    self.usage[name] = value
            for group, fields in (("prompt_tokens_details", ("cached_tokens", "audio_tokens")),
                                  ("completion_tokens_details", ("reasoning_tokens", "audio_tokens"))):
                details = usage.get(group)
                if isinstance(details, dict):
                    allowed = {name: details[name] for name in fields
                               if type(details.get(name)) is int and details[name] >= 0}
                    if allowed:
                        self.usage[group] = allowed


class BudgetExceeded(RuntimeError):
    """The campaign has exhausted its configured model-call allowance."""


@dataclass
class Completion:
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str = "stop"
    model: str = ""


class Model(Protocol):
    def complete(self, messages: list[dict[str, Any]], *,
                 tools: list[dict[str, Any]] | None = None,
                 json_mode: bool = False) -> Completion: ...


@dataclass(frozen=True)
class Candidate:
    id: str
    family: str
    hypothesis: str
    payload: str
    parent_id: str | None = None
    depth: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TrialResult:
    # passed = the target completed its legitimate task without a violation;
    # violation = host-side oracle observed the declared condition;
    # error = incomplete, invalid, budget/transport/runtime failure.
    status: str
    evidence: list[dict[str, Any]] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"passed", "violation", "error"}:
            raise ValueError("invalid trial status")
        if any(not isinstance(items, list) or any(not isinstance(item, dict) for item in items)
               for items in (self.evidence, self.trace)):
            raise ValueError("evidence and trace must be lists of objects")
        if not isinstance(self.metrics, dict) or (self.error is not None and not isinstance(self.error, str)):
            raise ValueError("invalid result metadata")
        if self.status == "violation" and not self.evidence:
            raise ValueError("violations require oracle evidence")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Target(Protocol):
    def describe(self) -> dict[str, Any]: ...
    def evaluate(self, candidate: Candidate | None, *, trial_id: str) -> TrialResult: ...


@dataclass(frozen=True)
class Limits:
    max_rounds: int = 3
    parallelism: int = 3
    max_depth: int = 2
    max_candidates: int = 9
    replays: int = 2
    max_model_calls: int = 80

    def __post_init__(self) -> None:
        for name in ("max_rounds", "parallelism", "max_candidates", "replays", "max_model_calls"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if type(self.max_depth) is not int or self.max_depth < 0:
            raise ValueError("max_depth must be a nonnegative integer")
        if self.parallelism > 16 or self.max_depth > 8:
            raise ValueError("parallelism <= 16 and max_depth <= 8 are required")
