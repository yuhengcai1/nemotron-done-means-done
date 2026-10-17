"""Generate three trusted reasoning-profile configs without reading credentials."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from examples.reasoning_handoff_target import EXPERIENCE, PROFILES
from recursive_redteam.config import validate_config


def validate_profile_binding(config):
    target = config["target"]
    profile = target["options"]["profile"]
    if profile not in PROFILES:
        raise ValueError("unknown target profile")
    expected = dict(PROFILES[profile], force_nonempty_content=True)
    actual = target["model"].get("request_parameters", {}).get("chat_template_kwargs")
    if json.dumps(actual, sort_keys=True) != json.dumps(expected, sort_keys=True):
        raise ValueError("profile label and target request parameters disagree")
    return {"profile": profile, "chat_template_kwargs": actual}


def make_config(profile="low_effort", handoff_mode="separate"):
    if profile not in PROFILES or handoff_mode not in {"inline", "separate", "all_json"}:
        raise ValueError("unknown profile or handoff mode")
    def model(flags, max_tokens):
        return {"kind": "openai-compatible", "base_url": "https://api.inference.crusoecloud.com/v1",
                "model": "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B", "api_key_env": "CRUSOE_API_KEY",
                "timeout": 60, "max_tokens": max_tokens, "temperature": 1.0, "max_retries": 0,
                "request_parameters": {"top_p": 0.95, "chat_template_kwargs": dict(flags, force_nonempty_content=True)}}
    config = {"version": 1, "planner": model(PROFILES["direct"], 2048),
              "verifier": model(PROFILES["direct"], 4096),
              "target": {"kind": "plugin", "factory": "examples.reasoning_handoff_target:make_target",
                         "options": {"profile": profile, "handoff_mode": handoff_mode, "max_steps": 6,
                                     "expected_adapter_sha256": hashlib.sha256((ROOT / "examples/reasoning_handoff_target.py").read_bytes()).hexdigest(),
                                     "expected_experience_sha256": hashlib.sha256(EXPERIENCE.read_bytes()).hexdigest()},
                         "model": model(PROFILES[profile], 4096)},
              "limits": {"max_rounds": 1, "parallelism": 1, "max_depth": 0, "max_candidates": 1,
                         "replays": 1, "max_model_calls": 40},
              "memory": {"enabled": False}, "request_interval_seconds": 8}
    validate_config(copy.deepcopy(config))
    validate_profile_binding(config)
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path(__file__).parent / "configs")
    parser.add_argument("--handoff-mode", choices=["separate", "inline", "all_json"], default="separate")
    parser.add_argument("--write-example", action="store_true")
    parser.add_argument("--validate", type=Path, help="Check a saved config and its profile-to-parameter binding only")
    args = parser.parse_args()
    if args.validate:
        config = validate_config(json.loads(args.validate.read_text()))
        print(json.dumps(validate_profile_binding(config)))
        return
    args.out.mkdir(parents=True, exist_ok=True)
    for profile in PROFILES:
        path = args.out / f"{profile}.json"
        path.write_text(json.dumps(make_config(profile, args.handoff_mode), indent=2) + "\n")
        print(path)
    if args.write_example:
        (ROOT / "examples/reasoning-handoff.json").write_text(json.dumps(make_config("low_effort", args.handoff_mode), indent=2) + "\n")


if __name__ == "__main__":
    main()
