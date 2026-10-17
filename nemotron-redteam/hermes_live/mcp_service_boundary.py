"""Stdio MCP transport over deterministic fixtures; journals actual invocations."""
import json
from pathlib import Path
import sys
from cases_boundary import call


def serve(case_path, state_path):
    case = json.loads(case_path.read_text())
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue
        method = message.get("method")
        if method == "initialize":
            result = {"protocolVersion": message["params"]["protocolVersion"], "capabilities": {"tools": {}},
                      "serverInfo": {"name": "nemotron-hermes-probe", "version": "1.0.0"}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": case["tools"]}
        elif method == "tools/call":
            state = json.loads(state_path.read_text())
            params = message["params"]
            result = call(case, state, params["name"], params.get("arguments", {}))
            state_path.write_text(json.dumps(state, indent=2) + "\n")
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": message["id"],
                              "error": {"code": -32601, "message": "Method not found"}}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)


if __name__ == "__main__":
    serve(Path(sys.argv[1]), Path(sys.argv[2]))
