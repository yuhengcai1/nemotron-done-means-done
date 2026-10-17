"""Operator CLI. Running with live credentials is always an explicit command."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import importlib
import json
from pathlib import Path
import sys
import tempfile

from .budget import BudgetedModel, ModelGate
from .command_target import CommandTarget
from .config import demo_config, fingerprint, load_config, validate_config
from .contracts import Limits, ModelError
from .credentials import load_credentials
from .engine import Engine, check_target_resume
from .evidence import import_evidence
from .providers import DemoModel, OpenAICompatibleModel
from .memory import ExperimentMemory
from .provenance import source_manifest, memory_identity, memory_scope
from .store import Store
from .targets import PolicySandbox


def write_json(path: Path, value: dict) -> None:
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=path.name + ".", suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def make_model(config: dict, role: str):
    if config["kind"] == "demo":
        return DemoModel(role)
    return OpenAICompatibleModel(**{k: v for k, v in config.items() if k != "kind"})


def prepare_model(config: dict, role: str, store: Store):
    model = make_model(config, role)
    if config["kind"] != "demo":
        records = model.list_models()
        selected = next((record for record in records if record["id"] == config["model"]), None)
        if selected is None:
            raise ValueError("configured model ID is not in the provider catalogue; use doctor")
        fields = ("id", "context_length", "supported_parameters", "pricing")
        store.set_metadata("model_" + role, {"provider": model.describe(),
                           "catalogue_record": {key: selected[key] for key in fields if key in selected}})
    return model


def make_target(config: dict, store: Store, maximum: int, gate=None):
    model = None
    if "model" in config:
        model = BudgetedModel(prepare_model(config["model"], "target", store), store, maximum, "target", gate=gate)
    if config["kind"] == "policy-sandbox":
        return PolicySandbox(model, max_steps=config["max_steps"], guarded=config["guarded"])
    if config["kind"] == "command":
        return CommandTarget(**{k: v for k, v in config.items() if k != "kind"})
    module, name = config["factory"].split(":")
    factory = getattr(importlib.import_module(module), name)
    target = factory(options=config["options"], model=model)
    if not callable(getattr(target, "describe", None)) or not callable(getattr(target, "evaluate", None)):
        raise ValueError("plugin factory must return an object with describe and evaluate")
    return target


@contextmanager
def run_lock(root: Path):
    with (root / ".run.lock").open("a") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("this run is active in another process") from None
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def execute(config: dict, root: Path, *, resume: bool = False) -> dict:
    root = root.resolve()
    if not resume:
        root.mkdir(parents=True, exist_ok=False)
    elif not (root / "state.sqlite").is_file():
        raise ValueError("run has no state.sqlite to resume")
    with run_lock(root):
        store = Store(root / "state.sqlite")
        memory = None
        validated = False
        try:
            expected = fingerprint(config)
            previous = store.get_metadata("config_sha256")
            if previous is not None and previous != expected:
                raise ValueError("saved configuration changed; resume requires identical configuration")
            if previous is None:
                if resume:
                    raise ValueError("run is missing its configuration fingerprint")
                store.set_metadata("config_sha256", expected)
                store.set_metadata("config", config)
                write_json(root / "config.json", config)
            validated = True
            limits = Limits(**config["limits"])
            if resume and store.get_metadata("status") == "completed":
                report = store.report()
                write_json(root / "report.json", report)
                return report
            sources = source_manifest()
            prior_sources = store.get_metadata("source_manifest")
            if prior_sources is not None and prior_sources != sources:
                raise ValueError("evaluator source changed; start a new run to preserve evidence identity")
            store.set_metadata("source_manifest", sources)
            gate = ModelGate(config.get("request_interval_seconds", 0)) if config.get("request_interval_seconds", 0) else None
            planner = BudgetedModel(prepare_model(config["planner"], "coordinator", store), store,
                                    limits.max_model_calls, "coordinator-or-worker", gate=gate)
            target = make_target(config["target"], store, limits.max_model_calls, gate=gate)
            if resume:
                # Check before writing any replacement memory identity.
                check_target_resume(store, target.describe())
            from .verification import ModelVerifier
            verifier = ModelVerifier(BudgetedModel(prepare_model(config.get("verifier", config["planner"]), "verifier", store),
                                     store, limits.max_model_calls, "verifier", gate=gate), store, replays=limits.replays)
            memory_config = config.get("memory", {"enabled": False})
            scope = ""
            if memory_config["enabled"]:
                memory = ExperimentMemory(memory_config["path"],
                    conclusion_ttl_seconds=memory_config["conclusion_ttl_seconds"], lease_seconds=memory_config["lease_seconds"])
                identity = memory_identity(config, target.describe(), sources)
                scope = memory_scope(identity)
                store.set_metadata("memory_identity", identity)
                store.set_metadata("memory_scope", scope)
                store.set_metadata("memory_before", memory.stats(scope))
            try:
                report = Engine(planner, target, store, limits, verifier=verifier, memory=memory,
                    memory_scope=scope, force_retest=memory_config.get("force_retest", False), source_run=str(root)).run(resume=resume)
            except KeyboardInterrupt:
                write_json(root / "report.json", store.report())
                raise
            if memory:
                store.set_metadata("memory_after", memory.stats(scope))
            store.set_metadata("acceptance", assess_run(report))
            report = store.report()
            write_json(root / "report.json", report)
            return report
        except Exception as exc:
            if validated:
                store.set_metadata("status", "setup_error")
                store.event("setup_error", {"error": type(exc).__name__})
                write_json(root / "report.json", store.report())
            raise
        finally:
            if memory is not None:
                memory.close()
            store.close()


def assess_run(report: dict) -> dict:
    """Queue completion and evidence of a real model campaign are separate facts."""
    calls = report["model_calls"]
    role_counts = {role: sum(call.get("role") == role and call.get("status") == "done" for call in calls)
                   for role in ("coordinator", "worker", "target", "verifier")}
    tasks = report["tasks"]
    evaluated = [task for task in tasks if task.get("outcome") in {"no_violation", "reproduced", "inconclusive"}]
    config = report["metadata"].get("config", {})
    models = {"coordinator": config.get("planner", {}), "worker": config.get("planner", {}),
              "target": config.get("target", {}).get("model", {}), "verifier": config.get("verifier", {})}
    live = all(model.get("kind") == "openai-compatible" for model in models.values())
    identities = all(call.get("actual_model") == models[call["role"]].get("model")
                     for call in calls if call.get("role") in models and call.get("status") == "done")
    reviews = {e["data"]["candidate_id"]: e["data"] for e in report["events"] if e["kind"] == "verification_review"}
    checks = {"live_models": live and identities and not report["simulated"], "queue_completed": report["status"] == "completed",
              "all_roles_called": all(role_counts.values()),
              "model_assigned_workers": bool(tasks) and all(t.get("delegation_source") == "model" and t.get("worker_id") for t in tasks),
              "model_chose_followup": any(t.get("parent_id") and t.get("delegation_source") == "model" for t in tasks),
              "evaluated_cases": bool(evaluated),
              "no_invalid_tasks": all(t.get("outcome") in {"no_violation", "reproduced", "memory_skipped", "duplicate"} for t in tasks),
              "verifier_called_for_evaluated_cases": bool(evaluated) and all(reviews.get(t["id"], {}).get("review", {}).get("accepted") for t in evaluated)}
    return {"passed": all(checks.values()), "checks": checks, "role_calls": role_counts,
            "meaning": "Evidence of model planning, delegation and review in the configured environment; not a general safety guarantee."}


def summary(report: dict, root: Path) -> dict:
    return {"status": report["status"], "simulated": report["simulated"], "counts": report["counts"],
            "model_usage": report["model_usage"], "acceptance": report["metadata"].get("acceptance"),
            "report": str((root / "report.json").resolve())}


def render_tree(report: dict) -> str:
    label = "SIMULATED MODEL FIXTURE" if report["simulated"] else "CONFIGURED EXTERNAL TARGET"
    lines = [f"Recursive Red Team | {label}", f"Campaign: {report['status']}"]
    children = {}
    for task in report["tasks"]:
        children.setdefault(task.get("parent_id"), []).append(task)
    def visit(parent, prefix=""):
        rows = children.get(parent, [])
        for index, task in enumerate(rows):
            last = index == len(rows) - 1
            lines.append(prefix + ("└─ " if last else "├─ ") + f"{task['id']} [{task.get('outcome', task['status'])}] {task['family']}")
            visit(task["id"], prefix + ("   " if last else "│  "))
    visit(None)
    lines.append(f"Cases reproduced: {report['counts']['reproduced']}; oracle signatures: {report['counts']['distinct_oracle_signatures']}")
    lines.append(f"Trials: {report['counts']['trials']}; gateway calls: {report['model_usage']['logical_calls']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Recursive model-driven evaluation teams")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="run an offline simulated campaign; no API key or network")
    demo.add_argument("--out", type=Path, required=True)
    demo.add_argument("--guarded", action="store_true", help="enable deterministic execution-boundary protection")
    run = commands.add_parser("run", help="run the configured external models and target")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--env-file", type=Path, help="read only configured credential variables; never execute file contents")
    resume = commands.add_parser("resume", help="resume pending work using the saved configuration and budget")
    resume.add_argument("--run", type=Path, required=True)
    resume.add_argument("--env-file", type=Path)
    report = commands.add_parser("report", help="export current database state, including while a campaign is running")
    report.add_argument("--run", type=Path, required=True)
    report.add_argument("--tree", action="store_true", help="print the delegated task tree")
    doctor = commands.add_parser("doctor", help="read model catalogue and declared capabilities; no evaluation")
    doctor.add_argument("--config", type=Path, required=True)
    doctor.add_argument("--env-file", type=Path)
    validate = commands.add_parser("validate", help="validate configuration offline")
    validate.add_argument("--config", type=Path, required=True)
    importer = commands.add_parser("import-evidence", help="record external result provenance without claiming verification")
    importer.add_argument("source", type=Path)
    importer.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            config = demo_config()
            config["target"]["guarded"] = args.guarded
            result = execute(config, args.out)
            output = summary(result, args.out)
        elif args.command == "run":
            config = load_config(args.config)
            load_credentials(args.env_file, config)
            result = execute(config, args.out)
            output = summary(result, args.out)
        elif args.command == "resume":
            config = load_config(args.run / "config.json")
            load_credentials(args.env_file, config)
            result = execute(config, args.run, resume=True)
            output = summary(result, args.run)
        elif args.command == "report":
            if not (args.run / "state.sqlite").is_file():
                raise ValueError("run has no state.sqlite")
            store = Store(args.run / "state.sqlite")
            try:
                result = store.report()
            finally:
                store.close()
            write_json(args.run / "report.json", result)
            if args.tree:
                print(render_tree(result))
                return 0
            output = summary(result, args.run)
        elif args.command == "doctor":
            config = load_config(args.config)
            load_credentials(args.env_file, config)
            provider = make_model(config["planner"], "coordinator")
            catalogue = provider.list_models()
            fields = ("id", "context_length", "supported_parameters", "pricing")
            output = {"provider": provider.describe(), "models": [{k: m[k] for k in fields if k in m} for m in catalogue],
                      "configured_model_found": any(m.get("id") == config["planner"].get("model") for m in catalogue)}
        elif args.command == "validate":
            config = load_config(args.config)
            output = {"valid": True, "config_sha256": fingerprint(config)}
        else:
            output = import_evidence(args.source, args.out)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        if args.command in {"run", "resume", "demo"}:
            return 0 if output["status"] == "completed" else 2
        return 0
    except KeyboardInterrupt:
        print("Interrupted. State was retained; use resume for this run.", file=sys.stderr)
        return 130
    except (ValueError, OSError, ModelError, ImportError, AttributeError) as exc:
        # ModelError is already scrubbed; other exception messages may contain
        # source content, credentials, or operator-defined command arguments.
        message = str(exc) if isinstance(exc, (ModelError, ValueError)) and not isinstance(exc, json.JSONDecodeError) else type(exc).__name__
        print(f"Cannot complete command: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
