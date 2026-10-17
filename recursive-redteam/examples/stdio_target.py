"""Protocol example: a deterministic local data-validation fixture, no inference."""
import json
import sys


request = json.load(sys.stdin)
if request.get("protocol_version") != 1:
    raise SystemExit(2)
candidate = request.get("candidate")
# A real adapter invokes its trusted environment and oracle here. This example
# never claims to verify a live model; it only demonstrates the result protocol.
print(json.dumps({"status": "passed", "evidence": [],
                  "trace": [{"event": "fixture_checked", "trial_id": request["trial_id"],
                             "candidate_present": candidate is not None}],
                  "metrics": {"simulated": True}}))
