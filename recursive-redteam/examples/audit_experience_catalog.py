"""Check imported observations and their actual planner/worker input wiring.

This is an offline integration audit. It never invokes a provider or evaluates a
target, and does not promote historical observations into verified conclusions.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from recursive_redteam.config import load_config
from recursive_redteam.contracts import Completion, Limits
from recursive_redteam.engine import Engine
from recursive_redteam.store import Store

CONFIGS = {"hermes_retry": "hermes-retry.json", "reasoning_handoff": "reasoning-handoff.json",
           "dynamic_state": "dynamic-state.json"}


class CaptureModel:
    simulated = True

    def __init__(self, parameters=None):
        self.request_parameters = parameters or {}
        self.requests = []

    def complete(self, messages, **kwargs):
        self.requests.append(json.loads(json.dumps(messages)))
        data = json.loads(messages[-1]["content"])
        if data["action"] == "plan":
            content = {"tasks": [{"family": data["target"]["families"][0],
                "hypothesis": "Offline input wiring check", "worker_id": "worker-1", "parent_id": None}]}
        elif data["action"] == "propose":
            content = {"family": data["task"]["family"], "hypothesis": "Offline input wiring check",
                       "payload": "Offline wiring placeholder; never evaluated"}
        else:
            raise AssertionError("Integration audit must not evaluate a target")
        return Completion(content=json.dumps(content), model="offline-input-capture")


def audit(root=ROOT):
    root = Path(root).resolve()
    rows = []
    for capability, filename in CONFIGS.items():
        config = load_config(root / "examples" / filename)
        package_path = root / "integrations" / capability / "experience.json"
        package = json.loads(package_path.read_text())
        observations = package["observations"]
        ids = [item["id"] for item in observations]
        schema_valid = (package.get("schema_version") == 1 and bool(package.get("source_thread_id"))
            and bool(observations) and len(set(ids)) == len(ids)
            and all(item.get("classification") in {"external_observation", "hypothesis"}
                    and item.get("summary") and item.get("conditions") and item.get("limitations")
                    and item.get("sources") for item in observations))
        sources = {}
        for item in observations:
            for source in item["sources"]:
                path = Path(source["path"])
                if not path.is_absolute():
                    path = root / path
                path = path.resolve()
                actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
                sources[(str(path), source["sha256"])] = {"path": str(path),
                    "expected_sha256": source["sha256"], "available": actual is not None,
                    "hash_matches": actual == source["sha256"]}
        target_config = config["target"]
        module, factory_name = target_config["factory"].split(":")
        target_model = CaptureModel(target_config.get("model", {}).get("request_parameters"))
        target = getattr(importlib.import_module(module), factory_name)(
            options=target_config["options"], model=target_model)
        planner = CaptureModel()
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "wiring.sqlite")
            try:
                engine = Engine(planner, target, store, Limits(max_candidates=1, parallelism=1))
                engine._plan(0, [])
                engine._candidate(store.tasks()[0])
            finally:
                store.close()
        model_inputs = [json.loads(messages[-1]["content"]) for messages in planner.requests]
        exposed = {}
        for data in model_inputs:
            prior = data["target"].get("prior_observations", [])
            if isinstance(prior, dict):
                prior = prior.get("observations", [])
            exposed[data["action"]] = [item["id"] for item in prior]
        checks = {"experience_schema_valid": bool(schema_valid),
                  "all_source_hashes_match": all(item["hash_matches"] for item in sources.values()),
                  "planner_receives_observations": exposed.get("plan") == ids,
                  "worker_receives_observations": exposed.get("propose") == ids,
                  "target_not_evaluated": not target_model.requests}
        rows.append({"capability": capability, "configuration": str(root / "examples" / filename),
            "source_thread_id": package["source_thread_id"], "observations": len(ids),
            "experience_sha256": hashlib.sha256(package_path.read_bytes()).hexdigest(),
            "model_input_observation_ids": exposed, "sources": list(sources.values()),
            "checks": checks, "passed": all(checks.values())})
    return {"schema_version": 1, "classification": "offline_input_alignment_audit",
            "new_provider_calls": 0, "new_verified_findings": 0,
            "passed": all(row["passed"] for row in rows), "capabilities": rows,
            "meaning": "Checks provenance and actual framework message construction, not behavioral equivalence or new model experiments."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    result = audit()
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"passed": result["passed"], "new_provider_calls": 0,
        "capabilities": [{"capability": row["capability"], "observations": row["observations"],
                          "checks": row["checks"]} for row in result["capabilities"]]}, indent=2))
    raise SystemExit(0 if result["passed"] else 2)
