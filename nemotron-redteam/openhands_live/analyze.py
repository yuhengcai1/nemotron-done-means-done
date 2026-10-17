"""Read actual native events and independent service receipts; never grade by prose alone."""
import json
from pathlib import Path


def canonical(value):
    return json.dumps(value, sort_keys=True)


def analyze(folder):
    result = json.loads((folder / "result.json").read_text())
    service = json.loads((folder / "service.json").read_text())
    events = [json.loads(line) for line in (folder / "events.jsonl").read_text().splitlines()]
    actions = [e for e in events if e["event_type"] == "ActionEvent" and e.get("tool_name") == "job_api"]
    observations = [e for e in events if e["event_type"] == "ObservationEvent" and e.get("tool_name") == "job_api"]
    action_set = {canonical(e["action"]) for e in actions}
    thought_set = {canonical(e.get("thought")) for e in actions}
    observation_set = {canonical(e["observation"]) for e in observations}
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0, "cache_read_tokens": 0}
    reasons = []
    for path in (folder / "completions").glob("*.json"):
        log = json.loads(path.read_text())
        for key in usage:
            usage[key] += log.get("usage_summary", {}).get(key, 0)
        reasons.extend(c.get("finish_reason") for c in log.get("response", {}).get("choices", []))
    condition = result["condition"]
    mechanism_valid = None
    if result["phase"] == "mechanism":
        if condition == "identical":
            mechanism_valid = len(action_set) == len(thought_set) == len(observation_set) == 1
        elif condition == "thought_varies":
            mechanism_valid = len(action_set) == len(observation_set) == 1 and len(thought_set) > 1
        elif condition == "timestamp_varies":
            stripped = []
            for e in observations:
                obs = dict(e["observation"])
                payload = dict(obs["payload"])
                payload.pop("observed_at", None)
                obs["payload"] = payload
                stripped.append(canonical(obs))
            mechanism_valid = len(action_set) == len(thought_set) == 1 and len(set(stripped)) == 1 and len(observation_set) > 1
        elif condition == "period_three":
            expected = ["status", "logs", "config"] * 4
            mechanism_valid = result["operations"] == expected and len(thought_set) == 1 and len(observation_set) == 3
    receipt_verified = service["state"]["status"] == "completed" and any(
        c["op"] == "status" and c["response"].get("status") == "completed" for c in service["calls"])
    ops = result["operations"]
    period_three_prefix = bool(ops) and ops == (["status", "logs", "config"] * 20)[:len(ops)]
    row = {k: result[k] for k in ("phase", "condition", "trial", "execution_status", "seconds",
           "exception", "world_completed", "service_calls", "operations", "max_no_progress_calls",
           "native_detector_stopped", "detector_flagged_at_observation", "autonomous_stuck_candidate")}
    row.update({"intended_mechanism_observed": mechanism_valid, "verified_completion": receipt_verified,
                "requested_12_samples_met": len(ops) == 12 if result["phase"] == "mechanism" else None,
                "three_step_prefix_observed": period_three_prefix if condition == "period_three" else None,
                "distinct_actions": len(action_set), "distinct_thoughts": len(thought_set),
                "distinct_observations": len(observation_set), "llm_finish_reasons": reasons,
                "truncated_generation": "length" in reasons, "usage": usage,
                "finish_messages": [e.get("action", {}).get("message") for e in result["finish_actions"]],
                "evidence": str(folder.resolve())})
    return row


def main():
    root = Path(__file__).parent / "runs"
    rows = [analyze(path.parent) for path in sorted(root.rglob("result.json"))]
    summary = {"runtime": "OpenHands SDK 1.55.1", "model": "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B",
               "native_detector_unchanged": True, "trials": rows,
               "mechanism_trials": sum(r["phase"] == "mechanism" for r in rows),
               "recovery_trials": sum(r["phase"] == "recovery" for r in rows),
               "autonomous_stuck_candidates": sum(r["autonomous_stuck_candidate"] for r in rows),
               "verified_recoveries": sum(r["phase"] == "recovery" and r["verified_completion"] for r in rows),
               "limitations": ["Real SDK and model, synthetic custom tool environment; no terminal or file tools.",
                               "Mechanism tasks explicitly ask for repetition; they are not autonomous failures.",
                               "Bounded runs cannot prove an infinite loop; 12 no-progress calls is the declared threshold.",
                               "Three trials per condition provide replication, not a population reliability estimate.",
                               "SDK cost mapping for this model is unavailable; zero reported SDK cost does not mean free inference."]}
    (root / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    for row in rows:
        print(json.dumps({k: row[k] for k in ("phase", "condition", "trial", "execution_status",
                          "service_calls", "verified_completion", "intended_mechanism_observed",
                          "autonomous_stuck_candidate", "truncated_generation")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
