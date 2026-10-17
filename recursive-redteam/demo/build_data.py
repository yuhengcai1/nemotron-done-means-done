"""Build public, credential-free presentation data from one recorded local run."""
from __future__ import annotations

import argparse
import ast
import difflib
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "nemotron-self-debug-20261017"
RUN_PATH = ROOT / "runs" / RUN_ID
REPORT_PATH = RUN_PATH / "report.json"
OUTPUT_PATH = ROOT / "demo" / "data"
KINDS = {"plan", "inspect", "test", "patch", "verify", "memory"}
MODEL = "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B"


def _text(value, limit=32000):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, indent=2)
    return str(value)[:limit]


def checked_run_id(run_id):
    """Only select a retained self-debug run name, never a path or command."""
    if not isinstance(run_id, str) or not re.fullmatch(r"nemotron-self-debug-20261017(?:-v[1-9][0-9]*)?", run_id):
        raise ValueError("Expected a retained Nemotron self-debug run ID")
    path = ROOT / "runs" / run_id
    if path.resolve().parent != (ROOT / "runs").resolve():
        raise ValueError("Run directory must stay inside the retained runs folder")
    return run_id


def pending_data(run_id=RUN_ID):
    return {
        "schema_version": 1,
        "project": {"name": "Recursive Lab", "tagline": "An agent that debugs its own framework."},
        "mode": "recorded",
        "run": {
            "id": run_id, "model": MODEL, "origin": "Crusoe Managed Inference",
            "summary": "Awaiting a saved Nemotron self-debug run. No successful repair is claimed.",
            "status": "pending", "recorded_at": None, "model_calls": None, "memory_entries": None,
        },
        "metrics": [], "events": [],
        "code": {"before": {"label": "Before repair", "text": "", "source": None},
                 "after": {"label": "Model-authored repair", "text": "", "source": None}},
        "evidence": {"before": {"status": "pending", "summary": "Run pending", "output": "", "source": None},
                     "after": {"status": "pending", "summary": "No verification yet", "output": "", "source": None}},
        "verification": {"available": False, "verified": False, "status": "pending", "command": None, "source": None},
        "showcase": {"verified": False, "before_summary": "Checks pending", "patch_summary": "No source change recorded",
                     "verification_summary": "No verified repair", "memory_summary": "No qualified memory",
                     "replay_count": 0, "source_hashes_valid": False},
        "memory": {"stored": False, "entry_count": 0, "recall": {"available": False, "verified": False,
                   "model_calls": None, "source": None, "summary": "No recorded memory recall"}},
        "provenance": {
            "source": "runs/" + run_id + "/report.json",
            "notes": ["The stage walkthrough replays a recorded model run.",
                      "Recheck runs local tests; it does not call the model API.",
                      "Teachers are not part of this runtime."],
        },
    }


def _code_excerpt(source):
    """Show the exact changed function while retaining the complete source in the run."""
    source = _text(source)
    try:
        tree = ast.parse(source)
        function = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "finish")
        return "\n".join(source.splitlines()[function.lineno - 1:function.end_lineno]), function.lineno
    except (SyntaxError, StopIteration):
        return source, None


def _self_debug_view(report, run_id=RUN_ID):
    """Map the runner's evidence report into the deliberately small UI contract."""
    source = "runs/" + run_id + "/report.json"
    before, after = report.get("before", {}), report.get("after", {})
    verification, memory = report.get("verification", {}), report.get("memory", {})
    calls = report.get("model_usage", {}).get("logical_calls")
    status = report.get("status", "pending")
    verified = verification.get("verified") is True
    summary = ("Nemotron inspected an isolated framework snapshot, authored a repair, and verified it with local checks."
               if verified else "Recorded self-debug attempt; inspect the evidence for its current outcome.")
    result = {
        "run": {"id": report.get("run_id", run_id), "model": report.get("model", MODEL),
                "origin": report.get("provider", "Crusoe Managed Inference"),
                "summary": summary, "status": status,
                "recorded_at": report.get("recorded_at", report.get("completed_at", report.get("started_at"))),
                "model_calls": calls, "memory_entries": int(memory.get("stored") is True)},
        "metrics": [], "events": [], "code": {}, "evidence": {},
        "provenance": {"notes": [
            "Target: an isolated copy of a previously identified framework defect; the shipped core was already repaired.",
            "Saved model output and local execution receipts are the evidence; this is not a new live attack.",
            "No teacher participates in this self-debug runtime." if report.get("teacher_in_runtime") is False
            else "Teacher participation is not established by this report."]},
    }
    for label, value, unit, pointer in (
        ("Model calls", calls, "calls", "model_usage.logical_calls"),
        ("Before repair", before.get("tests", {}).get("passed"), "checks", "before.tests.passed"),
        ("After repair", after.get("tests", {}).get("passed"), "checks", "after.tests.passed"),
        ("Independent replays", len(verification["replays"]) if isinstance(verification.get("replays"), list) else None, "replays", "verification.replays"),
        ("Verified memories", int(memory.get("stored") is True), "entries", "memory.stored"),
    ):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            metric = {"label": label, "value": value, "unit": unit, "source": source + "#" + pointer}
            if label in {"Before repair", "After repair"}:
                metric["total"] = (before if label == "Before repair" else after).get("tests", {}).get("total")
            result["metrics"].append(metric)
    for i, item in enumerate(report.get("events", [])):
        event = dict(item)
        event["source"] = source + "#events/" + str(i)
        event["detail"] = {key: item[key] for key in ("action", "result") if key in item}
        action, outcome = item.get("action", {}), item.get("result", {})
        if (item.get("kind") == "patch" and outcome.get("applied") is True
                and outcome.get("diff") == "" and action.get("old_text") == action.get("new_text")):
            event["title"] = "Nemotron proposes an unchanged edit"
            event["summary"] = "The replacement text is identical to the original. No source change occurred; this is not a repair."
            event["derived_classification"] = "no_source_change"
            event["detail"]["derived_classification"] = "No source change: identical replacement and empty diff. Original action and receipt preserved."
        result["events"].append(event)
    for phase, evidence in (("before", before), ("after", after)):
        tests = evidence.get("tests", {})
        failed, passed, total = tests.get("failed"), tests.get("passed"), tests.get("total")
        test_status = "pending"
        if isinstance(total, int) and total > 0 and isinstance(failed, int):
            test_status = "passed" if failed == 0 and passed == total else "failed"
        excerpt, line = _code_excerpt(evidence.get("source", ""))
        result["code"][phase] = {"label": "Isolated snapshot" if phase == "before" else "Nemotron-authored change",
                                 "text": excerpt, "source": source + "#" + phase + ".source" + (":L" + str(line) if line else "")}
        result["evidence"][phase] = {
            "status": test_status,
            "summary": (str(passed) + " / " + str(total) + " checks passed") if isinstance(total, int) else "Tests pending",
            "output": _text(tests), "source": source + "#" + phase + ".tests",
        }
    return result


def _passing_checks(check):
    return (isinstance(check, dict) and type(check.get("total")) is int and check["total"] > 0
            and check.get("failed") == 0 and check.get("passed") == check["total"])


def audit_model_actions(report, run_path):
    """Bind retained edits to raw model outputs and reconstruct the resulting source."""
    result = {"valid": False, "hosted_model_calls": 0, "patches": [], "effective_patch_count": 0,
              "no_op_count": 0, "source_reconstructed": False, "model_identity_valid": False,
              "reviewer_bound": False, "logical_calls": None,
              "source": "runs/" + str(report.get("run_id", "")) + "/model-journal.json"}
    try:
        journal = json.loads((run_path / "model-journal.json").read_text())
        calls = journal["model_calls"]
        completed = [call for call in calls if call.get("status") == "done"]
        result["logical_calls"] = journal.get("model_usage", {}).get("logical_calls")
        result["completed_model_calls"] = len(completed)
        result["failed_model_calls"] = sum(call.get("status") == "error" for call in calls)
        result["model_identity_valid"] = bool(completed) and all(call.get("actual_model") == report.get("model") for call in completed)
        decoded = []
        for index, call in enumerate(calls):
            if call.get("status") != "done" or not isinstance(call.get("response"), dict):
                continue
            try:
                output = json.loads(call["response"]["content"])
            except (ValueError, TypeError, KeyError):
                continue
            decoded.append((index, call, output))
        review = report.get("verification", {}).get("model_review")
        for _, call, output in decoded:
            if not review or call.get("role") != "repair_reviewer" or output != review:
                continue
            try:
                context = json.loads(call["request"]["messages"][-1]["content"])
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            if (context.get("patch") == report.get("patch", {}).get("diff")
                    and context.get("before") == report.get("before", {}).get("tests")
                    and context.get("replays") == report.get("verification", {}).get("replays")
                    and context.get("evidence_ids") == report.get("verification", {}).get("evidence_ids")):
                result["reviewer_bound"] = True
        current = (run_path / "before/targets.py").read_text()
        previous_call_index = -1
        for event in report.get("events", []):
            outcome = event.get("result", {})
            if event.get("kind") != "patch" or outcome.get("applied") is not True:
                continue
            action = event.get("action", {})
            matches = [(index, call) for index, call, output in decoded if index > previous_call_index
                       and call.get("role") == "repair_worker" and output == action]
            if not matches:
                raise ValueError("Applied edit has no matching subsequent model output: " + str(event.get("id")))
            index, call = matches[0]
            previous_call_index = index
            old, new = action.get("old_text"), action.get("new_text")
            if action.get("action") != "replace_text" or not isinstance(old, str) or not old or not isinstance(new, str) or current.count(old) != 1:
                raise ValueError("Applied replacement cannot be reconstructed")
            proposed = current.replace(old, new, 1)
            diff = "".join(difflib.unified_diff(current.splitlines(True), proposed.splitlines(True),
                                               fromfile="before/targets.py", tofile="after/targets.py"))
            source_hash = hashlib.sha256(proposed.encode()).hexdigest()
            if diff != outcome.get("diff") or source_hash != outcome.get("source_sha256"):
                raise ValueError("Applied edit receipt differs from reconstructed change")
            result["patches"].append({"event_id": event.get("id"), "model_call_id": call.get("id"),
                                      "actual_model": call.get("actual_model"), "source_changed": proposed != current,
                                      "source_sha256": source_hash, "diff_sha256": hashlib.sha256(diff.encode()).hexdigest()})
            result["effective_patch_count"] += int(proposed != current)
            result["no_op_count"] += int(proposed == current)
            current = proposed
        retained_after = (run_path / "workspace/recursive_redteam/targets.py").read_text()
        result["source_reconstructed"] = (current == retained_after == report.get("after", {}).get("source")
                                          and hashlib.sha256(current.encode()).hexdigest() == report.get("after", {}).get("source_sha256"))
        result["valid"] = bool(result["effective_patch_count"] > 0 and result["source_reconstructed"]
                               and result["model_identity_valid"]
                               and result["logical_calls"] == report.get("model_usage", {}).get("logical_calls"))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        result["error"] = str(exc)
    return result


def _showcase_evidence(report, run_path, run_id):
    """Qualify the stage summary from retained files and explicit receipts, never a label."""
    verification = report.get("verification", {})
    before = report.get("before", {}).get("tests", {})
    after = report.get("after", {}).get("tests", {})
    replays = verification.get("replays", [])
    replays = replays if isinstance(replays, list) else []
    manifest = report.get("artifact_hashes", {})
    required = ("repair_checks.py", "orchestrator.py", "before/targets.py", "before/contracts.py",
                "before/__init__.py", "workspace/recursive_redteam/targets.py",
                "workspace/recursive_redteam/contracts.py", "workspace/recursive_redteam/__init__.py")
    intact = False
    try:
        intact = isinstance(manifest, dict) and all(
            hashlib.sha256((run_path / relative).read_bytes()).hexdigest() == manifest.get(relative)
            for relative in required)
        for phase, relative in (("before", "before/targets.py"), ("after", "workspace/recursive_redteam/targets.py")):
            saved = report.get(phase, {})
            intact = (intact and isinstance(saved.get("source"), str)
                      and hashlib.sha256(saved["source"].encode()).hexdigest() == manifest.get(relative)
                      and saved.get("source_sha256") == manifest.get(relative))
    except OSError:
        pass
    review = verification.get("model_review", {})
    review = review if isinstance(review, dict) else {}
    cited = review.get("evidence_ids", [])
    evidence_ids = verification.get("evidence_ids", [])
    citations_match = (isinstance(cited, list) and len(cited) >= 2 and len(set(cited)) == len(cited)
                       and isinstance(evidence_ids, list) and sorted(cited) == sorted(evidence_ids))
    authorship = audit_model_actions(report, run_path)
    verified = (authorship["valid"] and authorship["reviewer_bound"] and report.get("status") == "verified" and report.get("teacher_in_runtime") is False
                and verification.get("verified") is True
                and verification.get("model_identity_verified") is True and review.get("verdict") == "verified"
                and citations_match and intact and before.get("failed", 0) > 0 and _passing_checks(after)
                and len(replays) >= 2 and all(_passing_checks(check) for check in replays))
    change_count = sum(1 for event in report.get("events", []) if event.get("kind") == "patch"
                       and event.get("result", {}).get("applied") is True and event.get("result", {}).get("diff"))
    stored_receipt = report.get("memory", {})
    stored = (verified and stored_receipt.get("stored") is True and type(stored_receipt.get("entry_id")) is int
              and isinstance(stored_receipt.get("event_hash"), str))
    recall_path = run_path / "memory-recall.json"
    recall = {"available": recall_path.is_file(), "verified": False, "model_calls": None,
              "source": "runs/" + run_id + "/memory-recall.json" if recall_path.is_file() else None,
              "summary": "No recorded memory recall"}
    if recall_path.is_file():
        try:
            receipt = json.loads(recall_path.read_text())
            if not isinstance(receipt, dict):
                raise ValueError("Invalid recall receipt")
            calls = receipt.get("hosted_model_calls")
            recall["model_calls"] = calls if type(calls) is int and calls >= 0 else None
            hashes = receipt.get("source_hashes", {})
            checks = receipt.get("checks", [])
            recall["verified"] = bool(stored and receipt.get("run_id") == run_id
                and receipt.get("matched") is True and receipt.get("reusable") is True
                and receipt.get("chain_valid") is True and calls == 0
                and receipt.get("entry_id") == stored_receipt.get("entry_id")
                and receipt.get("event_hash") == stored_receipt.get("event_hash")
                and isinstance(hashes, dict)
                and hashes.get("recalled_after_source") == report.get("after", {}).get("source_sha256")
                and hashes.get("before/targets.py") == manifest.get("before/targets.py")
                and hashes.get("repair_checks.py") == manifest.get("repair_checks.py")
                and isinstance(checks, list) and len(checks) >= 2 and all(_passing_checks(check) for check in checks))
            recall["summary"] = ("Retrieved from durable memory; two fresh local checks passed with zero model calls."
                                   if recall["verified"] else "The recorded recall did not qualify a reusable repair.")
        except (OSError, ValueError, TypeError):
            recall["summary"] = "The recorded memory receipt could not be validated."
    summary = {
        "verified": bool(verified), "replay_count": len(replays), "source_hashes_valid": bool(intact),
        "patch_authorship_verified": authorship["valid"], "patch_authorship_source": authorship["source"],
        "before_summary": str(before["failed"]) + " failing checks" if type(before.get("failed")) is int else "Checks pending",
        "patch_summary": str(change_count) + " model-authored source change" + ("s" if change_count != 1 else ""),
        "verification_summary": (str(len(replays)) + " fresh local replays; " + str(after["passed"]) + "/" + str(after["total"]) + " checks pass each"
                                   if verified else "Repair remains unqualified; inspect the preserved test receipts"),
        "memory_summary": recall["summary"] if stored else "No unverified repair is promoted to memory",
    }
    return summary, {"stored": bool(stored), "entry_count": int(bool(stored)), "recall": recall}


def build_data(report_path=None, *, run_id=RUN_ID):
    """Only publish explicit presentation fields; never expose provider configuration."""
    run_id = checked_run_id(run_id)
    result = pending_data(run_id)
    report_path = Path(report_path) if report_path is not None else ROOT / "runs" / run_id / "report.json"
    if not report_path.is_file():
        return result
    raw_bytes = report_path.read_bytes()
    if len(raw_bytes) > 8_000_000:
        raise ValueError("Recorded report exceeds the presentation data limit")
    report = json.loads(raw_bytes)
    if not isinstance(report, dict):
        raise ValueError("Recorded report must be an object")
    if report.get("run_id", run_id) != run_id:
        raise ValueError("Recorded run identity does not match the selected run")
    public = report.get("demo", _self_debug_view(report, run_id) if "run_id" in report else report)
    run = public.get("run", public)
    source = "runs/" + run_id + "/report.json"
    result["provenance"]["source_sha256"] = hashlib.sha256(raw_bytes).hexdigest()
    for key in ("id", "model", "origin", "summary", "status", "recorded_at"):
        if key in run:
            result["run"][key] = _text(run[key], 4000) if run[key] is not None else None
    for key in ("model_calls", "memory_entries"):
        value = run.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            result["run"][key] = value
    if isinstance(public.get("project"), dict):
        for key in ("name", "tagline"):
            if key in public["project"]:
                result["project"][key] = _text(public["project"][key], 300)
    for item in public.get("metrics", []):
        if not isinstance(item, dict) or not isinstance(item.get("value"), (int, float)):
            continue
        result["metrics"].append({"label": _text(item.get("label"), 200), "value": item["value"],
                                  "unit": _text(item.get("unit"), 100), "source": _text(item.get("source") or source, 500)})
        if isinstance(item.get("total"), int) and not isinstance(item["total"], bool):
            result["metrics"][-1]["total"] = item["total"]
    for i, event in enumerate(public.get("events", [])):
        if not isinstance(event, dict) or event.get("kind") not in KINDS:
            continue
        clean = {key: _text(event.get(key), 32000 if key == "detail" else 2000)
                 for key in ("id", "kind", "title", "summary", "detail", "source")}
        clean["id"] = clean["id"] or "event-" + str(i + 1)
        clean["source"] = clean["source"] or source
        for key in ("timestamp", "call_id", "status", "actor", "derived_classification"):
            if key in event:
                clean[key] = _text(event[key], 500)
        result["events"].append(clean)
    for section, fields in (("code", ("label", "text", "source")),
                            ("evidence", ("status", "summary", "output", "source"))):
        value = public.get(section, {})
        if isinstance(value, dict):
            for phase in ("before", "after"):
                if isinstance(value.get(phase), dict):
                    for key in fields:
                        if key in value[phase]:
                            result[section][phase][key] = _text(value[phase][key])
    provenance = public.get("provenance", {})
    if isinstance(provenance, dict) and isinstance(provenance.get("notes"), list):
        result["provenance"]["notes"].extend(_text(v, 2000) for v in provenance["notes"])
    result["provenance"]["notes"].extend([
            "First bounded attempt: 11 logical model calls; 7 failures remained (15/22 checks passed). No repair qualified. Source: runs/nemotron-self-debug-20261017/report.json.",
            "Second attempt plus bounded resume: 5 logical model calls, including 2 provider errors (initial timeout, then HTTP 429). No repair qualified. Source: runs/nemotron-self-debug-20261017-v2/report.json and model-journal.json.",
            "Both attempts are retained. Their configurations differ; this comparison does not establish a causal effect. Logical calls include failed calls and are not a count of HTTP requests."
    ])
    result["showcase"], result["memory"] = _showcase_evidence(report, report_path.parent, run_id)
    result["run"]["memory_entries"] = result["memory"]["entry_count"]
    for metric in result["metrics"]:
        if metric["label"] == "Verified memories":
            metric["value"] = result["memory"]["entry_count"]
    if result["run"]["status"] == "verified" and not result["showcase"]["verified"]:
        result["run"]["summary"] = "Recorded repair claim did not pass presentation evidence validation."
    result["verification"]["verified"] = result["showcase"]["verified"]
    result["evidence"]["after"]["verified"] = result["showcase"]["verified"]
    available = ((ROOT / "examples/self_debug.py").is_file()
                 and result["run"]["status"] in {"verified", "inconclusive", "completed", "failed"}
                 and isinstance(report.get("artifact_hashes"), dict) and bool(report["artifact_hashes"]))
    result["verification"].update({"available": available, "status": "available" if available else "pending",
        "command": "python3 examples/self_debug.py --verify-run runs/" + run_id + " --json" if available else None,
        "source": "examples/self_debug.py" if available else None})
    return result


def write_data(result, output_path=OUTPUT_PATH):
    output_path = Path(output_path)
    output_path.mkdir(parents=True, exist_ok=True)
    content = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    (output_path / "demo-data.json").write_text(content, encoding="utf-8")
    # This is a separate JS resource, but escape HTML-sensitive characters as well.
    safe = content.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    (output_path / "demo-data.js").write_text("window.DEMO_DATA = " + safe.rstrip() + ";\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=checked_run_id, default=RUN_ID, help="Explicit retained run ID for the stage demo")
    args = parser.parse_args()
    data = build_data(run_id=args.run)
    write_data(data)
    print(json.dumps({"status": data["run"]["status"], "events": len(data["events"]),
                      "output": "demo/data/demo-data.json"}))
