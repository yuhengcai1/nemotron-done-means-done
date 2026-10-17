"""Native Hermes AIAgent + native MCP + direct Crusoe, in an isolated profile."""
import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from cases import CONDITIONS, case_for, initial_state, score

MODEL = "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B"
BASE = "https://api.inference.crusoecloud.com/v1"
SOURCE = Path("/private/tmp/nemotron-hermes-source")


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--condition", choices=(*CONDITIONS, "export_control"), required=True)
    parser.add_argument("--trial", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--platform", choices=("cli", "cron"), default="cli")
    args = parser.parse_args()
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=False)
    key = next(line.split("=", 1)[1].strip() for line in args.env_file.read_text().splitlines()
               if line.startswith("CRUSOE_API_KEY="))
    # Dedicated profile; no user's real Hermes settings/memories/skills are loaded.
    profile = args.out / "profile"
    profile.mkdir()
    os.environ.update(HERMES_HOME=str(profile), OPENAI_API_KEY=key, OPENAI_BASE_URL=BASE)
    for name in list(os.environ):
        if name.startswith(("LMNR_", "OTEL_")):
            os.environ.pop(name)
    case = case_for(args.condition, args.trial)
    dump(args.out / "case.json", case)
    dump(args.out / "service.json", initial_state(case))
    (args.out / "task.txt").write_text(case["task"])
    if case["memory"]:
        (profile / "memories").mkdir()
        (profile / "memories" / "MEMORY.md").write_text(case["memory"] + "\n")
    config = {
        "model": {"provider": "custom", "default": MODEL, "base_url": BASE, "context_length": 262144},
        "providers": {"custom": {"api": BASE, "request_timeout_seconds": 45, "stale_timeout_seconds": 60}},
        "memory": {"provider": "builtin"},
        "mcp_servers": {"probe": {"command": sys.executable,
            "args": [str(Path(__file__).with_name("mcp_service.py")), str(args.out / "case.json"), str(args.out / "service.json")],
            "timeout": 10, "connect_timeout": 15}},
    }
    # JSON is valid YAML; no secrets written to configuration.
    dump(profile / "config.yaml", config)
    from agent.ssl_verify import install_truststore
    install_truststore()
    from tools.mcp_tool_discovery import discover_mcp_tools
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    from run_agent import AIAgent
    from hermes_cli.config import load_config_readonly
    tools_discovered = discover_mcp_tools(allowed_mcp_names=["probe"])
    if not tools_discovered:
        raise RuntimeError("No native MCP tools discovered")
    events = []
    def event(kind, **data):
        item = {"kind": kind, **data}
        events.append(item)
        with (args.out / "events.jsonl").open("a") as handle:
            handle.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
    started = time.monotonic()
    agent, result, error = None, {}, None
    def alarm_handler(signum, frame):
        raise TimeoutError("Experiment wall-clock limit reached")
    signal.signal(signal.SIGALRM, alarm_handler)
    with tempfile.TemporaryDirectory(prefix="nemotron-hermes-work-") as workspace:
        previous_cwd = Path.cwd()
        os.chdir(workspace)
        try:
            agent = AIAgent(model=MODEL, provider="custom", api_mode="chat_completions",
                base_url=BASE, api_key=key, enabled_toolsets=["probe", "memory"],
                max_iterations=18, run_budget_seconds=180, max_tokens=4096,
                request_overrides={"temperature": 1.0, "top_p": 0.95, "seed": 1000 + args.trial},
                platform=args.platform, cwd=workspace, quiet_mode=True,
                skip_background_review=True,
                tool_start_callback=lambda call_id, name, data: event("tool_start", call_id=call_id, name=name, arguments=data),
                tool_complete_callback=lambda call_id, name, data, output: event("tool_complete", call_id=call_id, name=name, arguments=data, output=output),
                notice_callback=lambda *a, **kw: event("notice", args=a, kwargs=kw))
            config_loaded = load_config_readonly()
            dump(args.out / "runtime.json", {
                "runtime": "Hermes native AIAgent", "git_commit": subprocess.check_output(["git", "-C", str(SOURCE), "rev-parse", "HEAD"], text=True).strip(),
                "python": sys.version, "base_url": agent.base_url, "provider": agent.provider,
                "model": agent.model, "api_mode": agent.api_mode, "platform": args.platform,
                "tools_discovered": tools_discovered, "tool_names": [t["function"]["name"] for t in agent.tools],
                "guardrails": asdict(agent._tool_guardrails.config),
                "memory_enabled": agent._memory_enabled, "memory_fixture_seeded": bool(case["memory"]),
                "memory_fixture_kind": "seeded native persistent MEMORY.md; not a claimed prior model learning run",
                "limits": {"iterations": 18, "seconds": 180, "max_output_tokens": 4096},
                "source_hashes": {name: hashlib.sha256((SOURCE / name).read_bytes()).hexdigest()
                                  for name in ("run_agent.py", "agent/tool_guardrails.py", "agent/conversation_loop.py")},
                "harness_hashes": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in
                                   (Path(__file__), Path(__file__).with_name("cases.py"), Path(__file__).with_name("mcp_service.py"))}})
            signal.alarm(180)
            result = agent.run_conversation(case["task"])
        except Exception as exc:
            error = {"type": type(exc).__name__, "detail": str(exc).replace(key, "[REDACTED]")[:1800]}
        finally:
            signal.alarm(0)
            dump(args.out / "native-result.json", result)
            state = json.loads((args.out / "service.json").read_text())
            outcome = score(case, state)
            messages = result.get("messages", [])
            memory_loaded = bool(case["memory"]) and any(case["memory"] in str(m.get("content", "")) for m in messages if m.get("role") == "system")
            outcome.update(condition=args.condition, trial=args.trial, family=case["family"], platform=args.platform,
                exception=error, seconds=round(time.monotonic()-started, 2), native_completed=result.get("completed"),
                native_failed=result.get("failed"), native_error=result.get("error"),
                api_calls=result.get("api_calls"), final_response=result.get("final_response"),
                memory_fixture_in_system_prompt=memory_loaded,
                native_result_keys=list(result))
            dump(args.out / "result.json", outcome)
            if agent:
                agent.close()
            shutdown_mcp_servers()
            os.chdir(previous_cwd)
    print(json.dumps({k:outcome[k] for k in ("condition", "trial", "exception", "seconds", "objective_met", "service_calls", "native_completed")}), flush=True)


if __name__ == "__main__":
    main()
