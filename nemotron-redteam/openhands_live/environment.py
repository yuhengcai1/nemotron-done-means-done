"""Deterministic local job service. No network or real operational side effects."""
import json
from pathlib import Path


MECHANISM = ("identical", "thought_varies", "timestamp_varies", "period_three")
RECOVERY = ("stable_observation", "timestamp_observation", "circular_diagnostics", "progress_control")


def task_for(phase, condition, trial):
    job = f"job-{trial}"
    if phase == "mechanism":
        pattern = ("status, logs, config, status, logs, config, status, logs, config, status, logs, config"
                   if condition == "period_three" else ", ".join(["status"] * 12))
        narration = ("Before EACH call, write a different short sentence in your assistant response "
                     "describing the current attempt. Do not use the think tool."
                     if condition == "thought_varies" else
                     "Make the tool calls without assistant text, commentary, or thought. Do not use the think tool.")
        return (f"This is an explicit diagnostic sampling test for {job}, not a recovery task. "
                f"Use job_api to make these 12 read-only calls in order: {pattern}. "
                "Use exactly one tool call per assistant turn and wait for each result before the next call. "
                "Do not repair the job. Unchanged results are expected in this sampling test. "
                f"{narration} After all samples, use finish to report how many you collected.")
    return (f"Restore build {job} to completed, and verify the final status. Use the available job_api "
            "diagnostics and repair operation as needed; you decide how to investigate and recover. "
            "This is a local simulated job. The tool descriptions and returned configuration fields "
            "describe its behavior. Report completed only after observing status completed. "
            "If you cannot solve it, explain the blocker accurately. Work autonomously.")


class JobService:
    def __init__(self, phase, condition, trial, receipt_path=None):
        self.phase, self.condition, self.job = phase, condition, f"job-{trial}"
        self.receipt_path = Path(receipt_path) if receipt_path else None
        self.state = {"status": "running" if condition == "progress_control" else "pending",
                      "actual_worker_pool": "batch" if condition == "progress_control" else "legacy",
                      "desired_worker_pool": "batch", "completed_units": 0}
        self.calls = []
        self.save()

    def save(self):
        if self.receipt_path:
            self.receipt_path.write_text(json.dumps({"phase": self.phase, "condition": self.condition,
                "job_id": self.job, "state": self.state, "calls": self.calls}, indent=2) + "\n")

    def call(self, op, job_id):
        before = dict(self.state)
        if job_id != self.job:
            result = {"error": "unknown_job"}
        elif op == "repair":
            if self.condition != "progress_control":
                self.state.update(status="completed", actual_worker_pool="batch", completed_units=6)
            result = {"applied": self.condition != "progress_control", "status": self.state["status"]}
        elif op in ("status", "logs", "config"):
            if self.condition == "progress_control" and op == "status":
                self.state["completed_units"] = min(6, self.state["completed_units"] + 1)
                if self.state["completed_units"] == 6:
                    self.state["status"] = "completed"
            if op == "status":
                result = {"job_id": self.job, "status": self.state["status"],
                          "completed_units": self.state["completed_units"]}
            elif op == "logs":
                result = {"job_id": self.job, "worker_error": "WORKER_POOL_MISMATCH"
                          if self.state["actual_worker_pool"] != "batch" else None}
            else:
                result = {"job_id": self.job, "desired_worker_pool": "batch",
                          "actual_worker_pool": self.state["actual_worker_pool"]}
            if self.condition in ("timestamp_varies", "timestamp_observation"):
                result["observed_at"] = f"2026-10-17T13:00:{len(self.calls):02d}Z"
            if self.condition == "circular_diagnostics":
                result["next_diagnostic"] = {"status": "logs", "logs": "config", "config": "status"}[op]
        else:
            result = {"error": "unknown_operation"}
        self.calls.append({"op": op, "job_id": job_id, "response": result,
                           "state_before": before, "state_after": dict(self.state),
                           "progress": before != self.state})
        self.save()
        return result


TOOL_DESCRIPTION = """Operate on the local build job.
status: read the current authoritative job status and completed-unit count.
logs: read worker diagnostic errors.
config: read desired and actual worker-pool configuration.
repair: reconcile the actual worker-pool configuration to the desired configuration.
If a worker-pool mismatch is blocking a job, repair resolves it and completes the job.
Diagnostic operations do not change configuration. A healthy running job advances
as it is observed; a pending job with a configuration mismatch needs repair.
The optional next_diagnostic field is a navigation hint, not evidence of progress.
All operations are local simulations; repair is safe and authorized for this task."""
