"""Compare real native transcripts with the independent service ledger."""
import json
from pathlib import Path
from collections import defaultdict


def analyze(path):
    row = json.loads((path / "result.json").read_text())
    native = json.loads((path / "native-result.json").read_text())
    case = json.loads((path / "case.json").read_text())
    prompt_path = path / "system-prompt.txt"
    # Native result.messages excludes the system prompt. An absent saved prompt
    # cannot establish that native memory was absent from the actual request.
    if case["memory"]:
        row["memory_fixture_in_system_prompt"] = (
            case["memory"] in prompt_path.read_text() if prompt_path.exists() else None)
    row["memory_origin"] = case.get("memory_origin", (
        "model_written" if case["condition"] == "learn_challenge" else "seeded"
    ) if case["memory"] else "empty")
    messages = native.get("messages", [])
    tool_messages = [m for m in messages if m.get("role") == "tool"]
    notices = []
    for i, message in enumerate(tool_messages):
        content = str(message.get("content", ""))
        start = content.find("[hermes note:")
        if start >= 0:
            notices.append({"tool_message_index": i, "notice": content[start:][:1000]})
    row.update(evidence=str(path.resolve()), guardrail_notices=notices,
        native_exit_reason=native.get("turn_exit_reason"), served_model=native.get("served_model"),
        prompt_tokens=native.get("prompt_tokens", 0), completion_tokens=native.get("completion_tokens", 0),
        cost_status=native.get("cost_status"), estimated_cost_usd=native.get("estimated_cost_usd"),
        model_routing={k:native.get(k) for k in ("provider", "base_url", "requested_model", "model", "served_model")})
    if case["family"] == "mechanism":
        # These are requested repetitions, not evidence of autonomous non-progress.
        row["mechanism_only"] = True
        row["stopped_on_native_guardrail"] = native.get("turn_exit_reason") == "tool_guardrail"
    return row


def main():
    root = Path(__file__).parent / "runs"
    rows = [analyze(p.parent) for p in sorted(root.rglob("result.json"))]
    groups = defaultdict(list)
    for row in rows:
        label = row["condition"]
        if label == "learn_challenge":
            label += "/" + row["memory_origin"]
        groups[label].append(row)
    summary = {"total_trials": len(rows), "trials": rows, "aggregate": {condition: {
        "trials": len(group), "objective_met": sum(r["objective_met"] for r in group),
        "duplicate_effect": sum(bool(r.get("duplicate_effect")) for r in group),
        "autonomous_stuck_candidates": sum(bool(r.get("autonomous_stuck_candidate")) for r in group),
        "exceptions": sum(bool(r.get("exception")) for r in group),
        "native_failed": sum(bool(r.get("native_failed")) for r in group),
        "calls": [r["service_calls"] for r in group],
        "memory_in_system": [r["memory_fixture_in_system_prompt"] for r in group],
        "with_guardrail_notice": sum(bool(r["guardrail_notices"]) for r in group)} for condition, group in groups.items()},
        "limitations": ["Native AIAgent and MCP; synthetic services; selected toolsets, not a full default shell-enabled CLI deployment.",
                        "CLI defaults warn but do not hard-stop. No claim of infinite autonomous loops from explicit sampling tasks.",
                        "Seeded historical native memory is a controlled fixture, not evidence of naturally learned memory corruption.",
                        "native_completed means the agent turn finished; it is not the external task oracle.",
                        "Inference is live; any cost_status=unknown estimates are not billed-cost verification."]}
    (root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary["aggregate"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
