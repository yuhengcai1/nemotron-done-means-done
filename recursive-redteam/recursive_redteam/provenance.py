"""Identify exact local evaluator sources and scope reusable observations."""
from __future__ import annotations

import hashlib
from pathlib import Path

from .config import fingerprint


def source_manifest() -> dict:
    root = Path(__file__).parent
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(root.glob("*.py"))}


def memory_identity(config: dict, target_description: dict, sources: dict) -> dict:
    # A model alias cannot identify a hosted checkpoint. Expiry and explicit
    # retesting remain necessary even when all of these local fields match.
    return {"schema": 2, "target": config["target"], "description": target_description,
            "external_target_revision": config.get("memory", {}).get("target_revision"),
            "input_identity": "sha256_exact_utf8",
            "evaluator_sources": {name: sources[name] for name in
                ("targets.py", "providers.py", "contracts.py", "verification.py", "command_target.py") if name in sources},
            "verification": {"model": config.get("verifier", config["planner"]), "replays": config["limits"]["replays"]},
            "hosted_checkpoint": "not exposed; observations expire"}


def memory_scope(identity: dict) -> str:
    return fingerprint(identity)
