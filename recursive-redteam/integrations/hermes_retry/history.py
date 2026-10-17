"""Offline native-operation replay. No inference or promotion of historical claims."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from .scenario import CREATE_DESCRIPTION, LIST_DESCRIPTION, digest, source_hashes, strict_object, task_for
from .service import ExportWorld


def operations(message: dict) -> list[dict]:
    result = []
    if message.get("role") != "assistant":
        return result
    for tc in message.get("tool_calls", []):
        fn = tc.get("function", {})
        name = fn.get("name", "")
        if name != "tool_call" and not name.endswith(("create_export", "list_exports")):
            continue
        if not isinstance(tc.get("id"), str) or not tc["id"]:
            raise ValueError("missing native tool-call identity")
        args = strict_object(fn.get("arguments", "{}"))
        targets = args.get("calls", []) if name == "tool_call" else [{"name": name, "arguments": args}]
        for item in targets:
            tool = item.get("name", "").rsplit("__", 1)[-1]
            if tool in ("create_export", "list_exports"):
                result.append({"tool": tool, "arguments": item.get("arguments", {}), "outer_id": tc.get("id")})
    return result


def replay_record(case: dict, native: dict, state: dict, claimed: dict) -> dict:
    world = ExportWorld(case["destination"], case["fault"],
        before_commit_status="not_committed" if case["condition"] == "learn_warmup" else "unknown",
        receipt_contract=case.get("receipt_contract", False))
    seen, by_call_id, sequence = set(), {}, []
    protocol_error = False
    try:
        for index, message in enumerate(native.get("messages", [])):
            if message.get("role") == "tool":
                seen.update(by_call_id.get(message.get("tool_call_id"), []))
            for operation in operations(message):
                receipt = world.call(operation["tool"], operation["arguments"], decision=index,
                                     call_id=operation["outer_id"], observed=set(seen))
                by_call_id.setdefault(operation["outer_id"], []).append(receipt["sequence"])
                sequence.append({"sequence": receipt["sequence"], "message_index": index,
                    "tool": operation["tool"], "model_call_id": operation["outer_id"],
                    "response_sha256": digest(json.dumps(receipt["response"], sort_keys=True)),
                    "job_count_after": receipt["job_count_after"], "retry_basis": receipt["retry_basis"],
                    "query_response_observed_before_retry": receipt["query_response_observed_before_retry"]})
    except (ValueError, TypeError, KeyError, AttributeError):
        protocol_error = True
    fields = ("tool", "arguments", "response", "is_error", "job_count_after")
    actual = [{k: r.get(k) for k in fields} for r in world.ledger]
    expected = [{k: r.get(k) for k in fields} for r in state.get("calls", [])]
    outcome = world.outcome()
    ledger_match = actual == expected and world.jobs == state.get("jobs")
    oracle_match = all(outcome[k] == claimed.get(k) for k in ("objective_met", "duplicate_effect"))
    incomplete = (bool(claimed.get("exception") or claimed.get("native_failed") or native.get("failed"))
                  or native.get("completed") is not True
                  or any(m.get("finish_reason") == "length" for m in native.get("messages", [])))
    matched = ledger_match and oracle_match and not protocol_error
    return {"classification": "external_observation", "validation_kind": "offline_historical_operation_replay",
        "new_inference_calls": 0, "framework_verified": False,
        "replay_status": "matched" if matched and not incomplete else "inconclusive",
        "ledger_match": ledger_match, "oracle_match": oracle_match, "protocol_error": protocol_error,
        "historical_incomplete": incomplete,
        "historical_outcome": "inconclusive" if incomplete else "duplicate_effect" if claimed.get("duplicate_effect") else "passed" if claimed.get("objective_met") else "incomplete_task",
        "replayed_outcome": outcome, "event_order": sequence,
        "event_order_sha256": digest(json.dumps(sequence, sort_keys=True)),
        "narrative_is_not_ledger": True,
        "input_alignment": {"task_exact": case["task"] == task_for(case["destination"]),
            "current_contract_exact": case["tools"][0]["description"] == CREATE_DESCRIPTION and case["tools"][1]["description"] == LIST_DESCRIPTION,
            "native_runtime_equivalence": False}}


def audit_history(source: Path) -> dict:
    source = source.resolve()
    result_paths = sorted((source / "runs").rglob("result.json"))
    if not result_paths:
        raise ValueError("no historical result records found")
    rows = []
    for path in result_paths:
        folder = path.parent
        names = ["case.json", "result.json", "native-result.json", "service.json"]
        provenance = [{"path": str(folder / n), "sha256": digest((folder / n).read_bytes())}
                      for n in [*names, "runtime.json"] if (folder / n).is_file()]
        row = {"trial": str(folder.relative_to(source / "runs")), "sources": provenance,
               "framework_verified": False, "new_inference_calls": 0}
        try:
            case, claimed, native, state = [json.loads((folder / n).read_text()) for n in names]
            row["condition"] = case["condition"]
            if case["family"] != "export":
                row.update(replay_status="out_of_scope", classification="external_observation",
                           reason="loop/recovery task outside this export capability")
            else:
                row.update(replay_record(case, native, state, claimed))
                memory = case.get("memory", "")
                prompt = folder / "system-prompt.txt"
                learned = case.get("memory_origin") == "model_written" or case["condition"] == "learn_challenge" and bool(memory)
                row["memory"] = {"text_sha256": digest(memory),
                    "origin": case.get("memory_origin", "model_written" if learned else "seeded" if memory else "empty"),
                    "in_actual_prompt": memory in prompt.read_text() if memory and prompt.exists() else None,
                    "applicability": {"commit_status": "not_committed"} if learned else {"status": "see historical case contract"}}
                if prompt.exists():
                    row["sources"].append({"path": str(prompt), "sha256": digest(prompt.read_bytes())})
        except (ValueError, TypeError, KeyError, OSError) as exc:
            row.update(replay_status="inconclusive", classification="external_observation", error=type(exc).__name__)
        rows.append(row)
    return {"schema_version": 1, "classification": "external_unverified", "status": "historical_replay_only",
            "source_root": str(source), "completed_trials": len(rows), "new_inference_calls": 0,
            "framework_verified": False, "counts": {key: sum(r["replay_status"] == key for r in rows)
                for key in ("matched", "inconclusive", "out_of_scope")},
            "adapter_source_hashes": source_hashes(),
            "historical_duplicates": sum(r.get("historical_outcome") == "duplicate_effect" for r in rows),
            "historical_passed": sum(r.get("historical_outcome") == "passed" for r in rows),
            "records": rows,
            "limits": ["Replays old actions; does not sample new model decisions or native runtime execution.",
                       "No historical conclusion is promoted into current campaign memory/dedup state.",
                       "Native memory writes, loop guards, tool discovery, system prompt and MCP wrapping are outside this compatible target."]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = audit_history(args.source)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k not in ("records", "limits", "adapter_source_hashes")}, ensure_ascii=False))
    return 1 if report["counts"]["inconclusive"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
