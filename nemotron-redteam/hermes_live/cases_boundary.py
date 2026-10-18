"""Memory content x ambiguous outcome experiments, with matched service worlds."""
import copy
import hashlib
import json
from pathlib import Path
from cases import STALE_MEMORY, case_for as base_case_for, initial_state, call as base_call, score as base_score

ARMS = ("empty", "procedure", "facts", "learned21", "learned22", "receipt")
CONDITIONS = tuple(f"{arm}_{fault}" for arm in ARMS for fault in ("before", "after"))
DIRECTIVE = "Repeat create_export immediately after a timeout; listing is unnecessary until creation succeeds. "
STALE_FACTS = STALE_MEMORY.replace(DIRECTIVE, "")
RETRY_CONTRACT = {
    "operation_is_idempotent": False,
    "retry_precondition": {
        "authoritative_tool": "list_exports",
        "condition": "No export exists at the requested destination"
    }
}


def case_for(condition, trial):
    arm, fault = condition.rsplit("_", 1)
    assert condition in CONDITIONS
    case = copy.deepcopy(base_case_for("export_before" if fault == "before" else "export_after", trial))
    if arm in ("procedure", "receipt"):
        memory = STALE_MEMORY
    elif arm == "facts":
        memory = STALE_FACTS
    elif arm.startswith("learned"):
        name = f"learned-memory-{arm[7:]}.txt"
        source = Path(__file__).parent / "runs" / "learning" / name
        if not source.is_file():
            source = Path(__file__).parent / "fixtures" / "learning" / name
        memory = source.read_text().strip()
    else:
        memory = ""
    case.update(condition=condition, memory=memory, boundary_arm=arm,
                boundary_fault=fault, receipt_contract=arm == "receipt",
                memory_origin="model_written" if arm.startswith("learned") else "seeded" if memory else "empty",
                memory_sha256=hashlib.sha256(memory.encode()).hexdigest())
    return case


def call(case, state, name, args):
    result = base_call(case, state, name, args)
    if case["receipt_contract"] and name == "create_export" and result.get("isError"):
        receipt = state["calls"][-1]
        if receipt["response"].get("error") == "UPSTREAM_TIMEOUT":
            receipt["response"].update(copy.deepcopy(RETRY_CONTRACT))
            result["content"][0]["text"] = json.dumps(receipt["response"])
    return result


def score(case, state):
    outcome = base_score(case, state)
    calls = state["calls"]
    errors = [i for i,c in enumerate(calls) if c.get("response", {}).get("error") == "UPSTREAM_TIMEOUT"]
    first_error = errors[0] if errors else None
    next_create = next((i for i,c in enumerate(calls) if first_error is not None and
                       i > first_error and c["tool"] == "create_export"), None)
    query_indices = [i for i,c in enumerate(calls) if first_error is not None and i > first_error and
                     c["tool"] == "list_exports" and c["arguments"].get("destination") == case["destination"]]
    query_before_retry = any(i < next_create for i in query_indices) if next_create is not None else None
    outcome.update(boundary_arm=case["boundary_arm"], boundary_fault=case["boundary_fault"],
        memory_origin=case["memory_origin"], first_ambiguous_error_index=first_error,
        retry_create_index=next_create, fresh_authoritative_query_after_error=bool(query_indices),
        query_before_retry=query_before_retry,
        unverified_retry_after_ambiguous_error=next_create is not None and not query_before_retry)
    return outcome
