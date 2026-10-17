"""Real OpenHands Agent/Conversation + Crusoe; no mocked model or detector changes."""
import argparse
import hashlib
import importlib.metadata
import inspect
import json
import os
from pathlib import Path
import signal
import tempfile
import time
from typing import Literal

from environment import JobService, TOOL_DESCRIPTION, MECHANISM, RECOVERY, task_for


def dump(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("mechanism", "recovery"), required=True)
    parser.add_argument("--condition", required=True)
    parser.add_argument("--trial", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--model", default="nvidia/NVIDIA-Nemotron-3-Super-120B-A12B")
    args = parser.parse_args()
    assert args.condition in (MECHANISM if args.phase == "mechanism" else RECOVERY)
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=False)
    if args.env_file:
        for line in args.env_file.read_text().splitlines():
            if line.startswith("CRUSOE_API_KEY="):
                os.environ["CRUSOE_API_KEY"] = line.split("=", 1)[1].strip()
    key = os.environ.get("CRUSOE_API_KEY")
    if not key:
        raise RuntimeError("Missing CRUSOE_API_KEY")
    # Isolate SDK settings and avoid unrelated external telemetry.
    os.environ.update(OPENHANDS_SUPPRESS_BANNER="1", LITELLM_LOCAL_MODEL_COST_MAP="True",
                      OH_PERSISTENCE_DIR=str(args.out / "user-settings"))
    for name in list(os.environ):
        if name.startswith(("LMNR_", "OTEL_")):
            os.environ.pop(name)

    from pydantic import Field, SecretStr
    from openhands.sdk import LLM, Action, Agent, Conversation, Observation, TextContent, ToolDefinition
    from openhands.sdk.tool import Tool, ToolExecutor, register_tool
    from openhands.sdk.event import ActionEvent, ObservationEvent
    from openhands.sdk.conversation.stuck_detector import StuckDetector

    class JobAction(Action):
        op: Literal["status", "logs", "config", "repair"] = Field(description="Operation to perform")
        job_id: str = Field(description="Build job identifier")

    class JobObservation(Observation):
        payload: dict

        @property
        def to_llm_content(self):
            return [TextContent(text=json.dumps(self.payload, sort_keys=True))]

    service = JobService(args.phase, args.condition, args.trial, args.out / "service.json")

    class JobExecutor(ToolExecutor):
        def __call__(self, action, conversation=None):
            return JobObservation(payload=service.call(action.op, action.job_id))

    class JobTool(ToolDefinition[JobAction, JobObservation]):
        name = "job_api"

        @classmethod
        def create(cls, conv_state=None, **kwargs):
            return [cls(description=TOOL_DESCRIPTION, action_type=JobAction,
                        observation_type=JobObservation, executor=JobExecutor())]

    register_tool("ProbeJobTool", JobTool)
    llm = LLM(model="openai/" + args.model, api_key=SecretStr(key),
              base_url="https://api.inference.crusoecloud.com/v1", api_mode="chat",
              usage_id="agent", temperature=1.0, top_p=0.95, seed=1000 + args.trial,
              max_input_tokens=262144, max_output_tokens=4096,
              num_retries=1, timeout=45, reasoning_effort=None,
              native_tool_calling=True, log_completions=True,
              log_completions_folder=str(args.out / "completions"))
    agent = Agent(llm=llm, tools=[Tool(name="ProbeJobTool")])
    task = task_for(args.phase, args.condition, args.trial)
    (args.out / "task.txt").write_text(task)
    detector_path = Path(inspect.getfile(StuckDetector))
    expected_hash = "3bf25f2d722404398e4744ae188f575f628463732b3606a8da6049fb8ff0039e"
    actual_hash = hashlib.sha256(detector_path.read_bytes()).hexdigest()
    assert actual_hash == expected_hash, "Installed detector differs from reviewed source"
    dump(args.out / "config.json", {"sdk_version": importlib.metadata.version("openhands-sdk"),
         "llm": llm.model_dump(mode="json", exclude={"api_key"}), "phase": args.phase,
         "condition": args.condition, "trial": args.trial,
         "stuck_detection": True, "detector_sha256": actual_hash,
         "max_iteration_per_run": 18, "wall_limit_seconds": 180,
         "tools": ["job_api", "think", "finish"], "system_prompt": "SDK default unchanged",
         "source_hashes": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in (Path(__file__), Path(__file__).with_name("environment.py"))}})
    events = []
    detector_checks = []
    holder = {}

    def callback(event):
        record = event.model_dump(mode="json")
        record["event_type"] = type(event).__name__
        events.append(record)
        with (args.out / "events.jsonl").open("a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        conv = holder.get("conversation")
        if conv is not None and isinstance(event, ObservationEvent):
            # Read-only diagnostic; does not replace the runtime's own check.
            check = {"event_count": len(events), "service_calls": len(service.calls),
                     "is_stuck": conv.stuck_detector.is_stuck()}
            detector_checks.append(check)
            with (args.out / "detector-checks.jsonl").open("a") as f:
                f.write(json.dumps(check) + "\n")

    started = time.monotonic()
    error = None
    conv = None
    def alarm_handler(signum, frame):
        raise TimeoutError("Experiment wall-clock limit reached")
    signal.signal(signal.SIGALRM, alarm_handler)
    with tempfile.TemporaryDirectory(prefix="openhands-loop-work-") as workspace:
        try:
            conv = Conversation(agent=agent, workspace=workspace,
                persistence_dir=args.out / "conversation", delete_on_close=False,
                callbacks=[callback], stuck_detection=True,
                max_iteration_per_run=18, visualizer=None)
            holder["conversation"] = conv
            conv.send_message(task)
            signal.alarm(180)
            conv.run()
        except Exception as exc:
            error = {"type": type(exc).__name__, "detail": str(exc).replace(key, "[REDACTED]")[:1500]}
        finally:
            signal.alarm(0)
            status = str(conv.state.execution_status.value) if conv else None
            metrics = conv.conversation_stats.get_combined_metrics().model_dump(mode="json") if conv else {}
            actions = [e for e in events if e["event_type"] == "ActionEvent"]
            errors = [e for e in events if e["event_type"] in ("ConversationErrorEvent", "AgentErrorEvent")]
            finish = [e for e in actions if e.get("tool_name") == "finish"]
            max_stall = streak = 0
            for call in service.calls:
                streak = 0 if call["progress"] else streak + 1
                max_stall = max(streak, max_stall)
            result = {"phase": args.phase, "condition": args.condition, "trial": args.trial,
                "execution_status": status, "seconds": round(time.monotonic()-started, 2),
                "exception": error, "world_completed": service.state["status"] == "completed",
                "service_calls": len(service.calls), "operations": [c["op"] for c in service.calls],
                "max_no_progress_calls": max_stall,
                "native_detector_stopped": status == "stuck",
                "detector_flagged_at_observation": any(c["is_stuck"] for c in detector_checks),
                "finish_actions": finish, "error_events": errors, "metrics": metrics,
                "autonomous_stuck_candidate": args.phase == "recovery" and max_stall >= 12
                    and status != "stuck" and not any(c["is_stuck"] for c in detector_checks)
                    and service.state["status"] != "completed"}
            dump(args.out / "result.json", result)
            if conv:
                conv.close()
    print(json.dumps({k: result[k] for k in ("phase", "condition", "trial", "execution_status",
                     "seconds", "exception", "world_completed", "service_calls",
                     "max_no_progress_calls", "native_detector_stopped", "autonomous_stuck_candidate")}), flush=True)


if __name__ == "__main__":
    main()
