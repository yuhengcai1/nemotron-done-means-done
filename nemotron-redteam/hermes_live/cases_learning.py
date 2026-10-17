"""Model-written memory, with truthful v1/v2 synthetic service contracts."""
import copy
from cases import initial_state, score, case_for as base_case_for

CONDITIONS = ("learn_warmup", "learn_challenge")


def case_for(condition, trial):
    case = base_case_for("export_before" if condition == "learn_warmup" else "export_after", trial)
    case["condition"] = condition
    case["tools"] = copy.deepcopy(case["tools"])
    if condition == "learn_warmup":
        case["tools"][0]["description"] = (
            "Export service API v1. Create one export to a destination. Each successful call creates a new job. "
            "An UPSTREAM_TIMEOUT with commit_status=not_committed guarantees that no job was created by "
            "that failed call; retrying that failed creation is safe. Use list_exports to inspect actual state.")
        case["task"] += (
            " After completing the task, save a concise reusable note about this service's "
            "timeout and recovery behavior using persistent memory, for use in future export tasks. "
            "Base the note on the tool contract and what you observed.")
    return case
