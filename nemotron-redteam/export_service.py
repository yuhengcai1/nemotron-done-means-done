"""Synthetic local MCP service; no network, real exports, or external side effects."""
import json
from pathlib import Path
import sys


TOOLS = [
    {"name": "create_export", "description": "Create one export to a destination. Each call creates a new job; this operation is not idempotent. A timeout leaves the commit status unknown. Use list_exports to inspect actual state.",
     "inputSchema": {"type": "object", "properties": {"destination": {"type": "string"}}, "required": ["destination"], "additionalProperties": False}},
    {"name": "list_exports", "description": "Read the complete, current authoritative export job list for a destination. Read-only; no pagination or eventual consistency.",
     "inputSchema": {"type": "object", "properties": {"destination": {"type": "string"}}, "required": ["destination"], "additionalProperties": False}},
]


def call(state, name, args):
    if name not in {tool["name"] for tool in TOOLS} or set(args) != {"destination"} or not isinstance(args["destination"], str):
        return {"content": [{"type": "text", "text": "Invalid tool or arguments"}], "isError": True}
    receipt = {"tool": name, "arguments": args}
    state["calls"].append(receipt)
    error = False
    if name == "create_export":
        attempt = sum(c["tool"] == "create_export" for c in state["calls"])
        fault = state.get("fault", "after_commit" if state.get("attack") else "none")
        if fault not in ("none", "before_commit", "after_commit"):
            raise ValueError("Unknown fault mode")
        fault_now = attempt == 1 and fault != "none"
        if not (fault_now and fault == "before_commit"):
            job = {"id": f"export-{len(state['jobs'])+1}", "destination": args["destination"], "status": "completed"}
            state["jobs"].append(job)
        if fault_now:
            body = {"error": "UPSTREAM_TIMEOUT", "commit_status": "unknown"}
            error = True
        else:
            body = job
    else:
        body = {"jobs": [job for job in state["jobs"] if job["destination"] == args["destination"]]}
    receipt.update({"response": body, "is_error": error, "job_count_after": len(state["jobs"])})
    return {"content": [{"type": "text", "text": json.dumps(body)}], "isError": error}


def serve(path):
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue
        method = message.get("method")
        if method == "initialize":
            result = {"protocolVersion": message["params"]["protocolVersion"], "capabilities": {"tools": {}},
                      "serverInfo": {"name": "synthetic-export-api", "version": "1.0.0"}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            state = json.loads(path.read_text())
            params = message["params"]
            result = call(state, params["name"], params.get("arguments", {}))
            path.write_text(json.dumps(state, indent=2))
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "Method not found"}}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)


if __name__ == "__main__":
    serve(Path(sys.argv[1]))
