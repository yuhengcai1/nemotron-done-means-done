"""A single front door; execution contracts and recorded evidence remain separate."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from .registry import COMPONENTS, ROOT, TEST_SUITES
from .reports import summarize


def resolve_paths(arguments, flags, caller):
    """Resolve caller-supplied paths before switching to a legacy module's cwd."""
    result = []
    pending = False
    for argument in arguments:
        if pending:
            if argument.startswith("--"):
                raise ValueError("a path option is missing its value")
            result.append(str((caller / Path(argument).expanduser()).resolve()))
            pending = False
        elif argument in flags:
            result.append(argument)
            pending = True
        elif "=" in argument and argument.split("=", 1)[0] in flags:
            name, value = argument.split("=", 1)
            if not value:
                raise ValueError("a path option is missing its value")
            result.append(name + "=" + str((caller / Path(value).expanduser()).resolve()))
        else:
            result.append(argument)
    if pending:
        raise ValueError("a path option is missing its value")
    return result


def command_for(name, arguments, *, caller=None, python=None):
    component = COMPONENTS[name]
    arguments = list(arguments)
    if arguments[:1] == ["--"]:
        arguments.pop(0)
    resolved = resolve_paths(arguments, component.path_flags, caller or Path.cwd())
    if name == "evaluate" and resolved[:1] == ["import-evidence"] and len(resolved) > 1 and not resolved[1].startswith("-"):
        resolved[1] = str(((caller or Path.cwd()) / Path(resolved[1]).expanduser()).resolve())
    if name == "evolve" and not any(a in ("-h", "--help") for a in resolved):
        supplied = {a.split("=", 1)[0] for a in resolved}
        defaults = {"--environment": ROOT / "configs/native-environment.json",
                    "--memory": ROOT / "state/evolution",
                    "--env-file": ROOT / ".env"}
        for flag, value in defaults.items():
            if flag not in supplied:
                resolved += [flag, str(value)]
    return [python or sys.executable, *component.arguments, *resolved], ROOT / component.directory


def doctor():
    executable = os.environ.get("OPENCODE_BIN") or shutil.which("opencode")
    historical = Path("/private/tmp/nemotron-redteam-runtime/node_modules/.bin/opencode")
    if not executable and historical.is_file():
        executable = str(historical)
    return {"repository": str(ROOT), "python": sys.executable,
            "python_supported": sys.version_info >= (3, 10),
            "opencode": executable, "credential_environment_present": bool(os.environ.get("CRUSOE_API_KEY")),
            "credential_file_present": (ROOT / ".env").is_file(),
            "evolution_entrypoint_present": (ROOT / "agentic-redteam/autonomous.py").is_file(),
            "recorded_demo_present": (ROOT / "recursive-redteam/demo/recordings/nemotron-self-debug-20261017/report.json").is_file(),
            "note": "Offline presence checks only. No credentials read and no model catalogue requested."}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Nemotron Lab — Version N+1 experiments and independent verification",
        epilog="Forwarded options belong to each component: python3 lab.py evolve --help")
    parser.add_argument("--python", help="Python executable for a separately installed runtime environment")
    parser.add_argument("command", nargs="?", choices=[*COMPONENTS, "catalog", "doctor", "inspect", "test"])
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    try:
        if args.command == "catalog":
            for name, component in COMPONENTS.items():
                print(f"{name:17} {component.description}")
            return 0
        if args.command == "doctor":
            if args.arguments:
                raise ValueError("doctor takes no options")
            print(json.dumps(doctor(), indent=2))
            return 0
        if args.command == "inspect":
            command = argparse.ArgumentParser(prog="lab.py inspect", description="Read a report without resuming it")
            command.add_argument("run", type=Path)
            options = command.parse_args(args.arguments)
            print(json.dumps(summarize(options.run), indent=2, ensure_ascii=False))
            return 0
        if args.command == "test":
            command = argparse.ArgumentParser(prog="lab.py test", description="Offline suites; no live inference")
            command.add_argument("--suite", action="append", choices=TEST_SUITES)
            options = command.parse_args(args.arguments)
            results = {}
            for suite in options.suite or TEST_SUITES:
                directory, start, pattern = TEST_SUITES[suite]
                print(f"\n[{suite}]", flush=True)
                results[suite] = subprocess.run([args.python or sys.executable, "-B", "-m", "unittest",
                    "discover", "-s", start, "-p", pattern, "-q"], cwd=ROOT / directory).returncode
            print(json.dumps({"suite_exit_codes": results, "live_model_calls": 0}))
            return 1 if any(results.values()) else 0
        command, cwd = command_for(args.command, args.arguments, python=args.python)
        if not cwd.is_dir() or (not command[1].startswith("-") and not (cwd / command[1]).is_file()):
            raise ValueError("component is not present in this checkout; see lab.py catalog")
        return subprocess.run(command, cwd=cwd).returncode
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        # Do not print raw report contents or provider output on parse failure.
        print(f"Cannot run {args.command}: {type(exc).__name__}", file=sys.stderr)
        return 2
