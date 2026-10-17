"""Summarize objective failures and actual native delegation; never auto-confirm."""
import argparse
import json
from pathlib import Path


def review(path):
    result = json.loads((path / "verdict.json").read_text())
    world = json.loads((path / "final-world.private.json").read_text())
    children=[];tool_errors=[];truncated=[];tool_sequence=[]
    for file in (path / "sessions").glob("*.json"):
        session=json.loads(file.read_text());info=session["info"]
        if info.get("parentID"):
            children.append({"id":info["id"],"parent":info["parentID"],"title":info.get("title")})
        for message in session["messages"]:
            msg_info=message["info"]
            if msg_info.get("finish") == "length":truncated.append(msg_info.get("id"))
            for part in message["parts"]:
                if part.get("type")=="tool":
                    state=part.get("state",{})
                    tool_sequence.append({"session":info["id"],"agent":msg_info.get("agent"),
                                          "tool":part.get("tool"),"status":state.get("status")})
                    if state.get("status")=="error":
                        tool_errors.append({"agent":msg_info.get("agent"),"tool":part.get("tool"),
                                            "error":state.get("error","")[:800]})
    return {"path":str(path.resolve()),"passed":result["passed"],"violations":result["violations"],
        "declared_status":result["declared_status"],"timed_out":result["timed_out"],
        "roles_invoked":result["roles_invoked"],"child_sessions":children,"tool_errors":tool_errors,
        "truncated_messages":truncated,"tool_sequence":tool_sequence,
        "revision_notices":[x for x in world["history"] if x["result"].get("notice")],
        "publications":world["publications"],"world_calls":len(world["history"]),
        "failure_candidate":not result["passed"] and not result["timed_out"] and len(children)>=3,
        "confirmed_failure":False}


if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument("campaign",type=Path);args=parser.parse_args()
    rows=[]
    for file in sorted(args.campaign.glob("*/*/verdict.json")):
        r=review(file.parent);rows.append(r)
        print(file.parent.name, {k:r[k] for k in ("passed","violations","declared_status","world_calls","failure_candidate")},
              "child_sessions",len(r["child_sessions"]))
    (args.campaign/"review.json").write_text(json.dumps(rows,indent=2)+"\n")
