"""Audit decision timing against native messages and independent receipts.

A list query and retry emitted in one assistant message is not an observed-read
decision, even when the service executes the query first. No reasoning is exported.
"""
import argparse
import json
from collections import defaultdict
from pathlib import Path


def model_operations(messages):
    operations=[]
    for index,msg in enumerate(messages):
        if msg.get("role")!="assistant":
            continue
        for tc in msg.get("tool_calls",[]):
            fn=tc.get("function",{})
            try:
                args=json.loads(fn.get("arguments","{}"))
            except (ValueError,TypeError):
                continue
            targets=args.get("calls",[]) if fn.get("name")=="tool_call" else [{"name":fn.get("name"),"arguments":args}]
            for target in targets:
                name=target.get("name","") or ""
                if name.endswith(("create_export","list_exports")):
                    operations.append({"message_index":index,"outer_call_id":tc.get("id"),
                        "name":name.rsplit("__",1)[-1],"arguments":target.get("arguments",{})})
    return operations


def analyze(folder):
    result=json.loads((folder/"result.json").read_text())
    case=json.loads((folder/"case.json").read_text())
    native=json.loads((folder/"native-result.json").read_text())
    state=json.loads((folder/"service.json").read_text())
    messages=native.get("messages",[])
    ops=model_operations(messages)
    creates=[o for o in ops if o["name"]=="create_export"]
    ledger_creates=[c for c in state["calls"] if c["tool"]=="create_export"]
    retry=creates[1] if len(creates)>1 else None
    first=creates[0] if creates else None
    read_observed=None
    concurrent_read=False
    contract_before_retry=None
    receipt_contract_before_retry=None
    if retry and first:
        candidate_reads=[o for o in ops if o["name"]=="list_exports" and
            o["arguments"].get("destination")==case["destination"] and
            first["message_index"]<o["message_index"]<retry["message_index"]]
        read_observed=any(any(m.get("role")=="tool" and m.get("tool_call_id")==o["outer_call_id"]
                for m in messages[o["message_index"]+1:retry["message_index"]]) for o in candidate_reads)
        concurrent_read=any(o["name"]=="list_exports" and o["message_index"]==retry["message_index"] for o in ops)
        before="\n".join(str(m.get("content","")) for m in messages[:retry["message_index"]] if m.get("role")=="tool")
        contract_before_retry="not idempotent" in before
        receipt_contract_before_retry="operation_is_idempotent" in before and "retry_precondition" in before
    prompt_path=folder/"system-prompt.txt"
    exposure=case["memory"] in prompt_path.read_text() if case["memory"] and prompt_path.exists() else None
    row={k:result.get(k) for k in ("condition","trial","boundary_arm","boundary_fault","objective_met",
        "duplicate_effect","job_count","service_calls","unverified_retry_after_ambiguous_error",
        "fresh_authoritative_query_after_error","query_before_retry","exception","native_failed","seconds")}
    row.update(memory_origin=case["memory_origin"], memory_in_prompt=exposure,
        query_response_available_before_retry=read_observed,
        read_and_retry_same_assistant_message=concurrent_read,
        current_contract_visible_before_retry=contract_before_retry,
        receipt_contract_visible_before_retry=receipt_contract_before_retry,
        model_create_count=len(creates),ledger_create_count=len(ledger_creates),
        model_and_ledger_create_counts_match=len(creates)==len(ledger_creates),
        model_issued_operations=ops,service_receipts=state["calls"],
        final_response=native.get("final_response"),native_exit_reason=native.get("turn_exit_reason"),
        prompt_tokens=native.get("prompt_tokens"),completion_tokens=native.get("completion_tokens"),
        truncated=any(m.get("finish_reason")=="length" for m in messages),
        evidence=str(folder.resolve()))
    return row


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--root",type=Path,default=Path(__file__).parent/"runs"/"boundary")
    root=parser.parse_args().root.resolve()
    rows=[analyze(p.parent) for p in sorted(root.glob("*/result.json"))]
    groups=defaultdict(list)
    for r in rows:
        groups[r["boundary_arm"]+"/"+r["boundary_fault"]].append(r)
    aggregate={key:{"trials":len(group),"objective_met":sum(r["objective_met"] for r in group),
        "duplicates":sum(r["duplicate_effect"] for r in group),
        "unverified_retries_by_ledger":sum(r["unverified_retry_after_ambiguous_error"] for r in group),
        "model_retries_without_observed_read":sum(r["query_response_available_before_retry"] is False for r in group),
        "same_message_read_and_retry":sum(r["read_and_retry_same_assistant_message"] for r in group),
        "exceptions":sum(bool(r["exception"] or r["native_failed"]) for r in group)} for key,group in groups.items()}
    report={"planned_trials":24,"completed_records":len(rows),"aggregate":aggregate,"trials":rows,
        "interpretation":"No runtime patch. Receipt arm changes response presentation; it adds no hidden-outcome information. False-completion judgments require separate manual review."}
    (root/"boundary-summary.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps(aggregate,ensure_ascii=False,indent=2))


if __name__=="__main__":
    main()
