"""Read-only, conservative views over existing report formats. Not a new oracle."""
import json
from pathlib import Path


def summarize(path):
    path = Path(path)
    if path.is_dir():
        path = path / "report.json"
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("report must be a JSON object")
    result = {"source": str(path.resolve()), "status": data.get("status", "unknown"),
              "interpretation": "Recorded claims only; this view does not independently re-audit evidence."}
    if "upgrade_loaded" in data:
        result.update(kind="native_evolution", promoted_versions=len(data.get("promoted", [])),
            upgrade_loaded=data.get("upgrade_loaded") is True,
            upgrade_followup_passed=data.get("upgrade_followup_passed") is True)
    elif "artifact_hashes" in data or "source_origin" in data:
        result.update(kind="historical_repair", repair_verified=data.get("verification", {}).get("verified") is True,
                      memory_stored=data.get("memory", {}).get("stored") is True)
        for phase in ("before", "after"):
            checks = data.get(phase, {}).get("tests", {})
            result[phase] = {key: checks.get(key) for key in ("passed", "failed", "total")}
    elif "episode" in data or "teaching_gain_established" in data:
        result.update(kind="student_experiment", teaching_gain_established=data.get("teaching_gain_established") is True)
    elif "suites" in data:
        result.update(kind="teaching_comparison", suites=list(data["suites"]))
    else:
        result["kind"] = "evaluation_or_historical_report"
        if isinstance(data.get("counts"), dict):
            result["counts"] = {key: data["counts"].get(key) for key in
                                ("tasks", "trials", "reproduced", "inconclusive")}
        if type(data.get("simulated")) is bool:
            result["simulated"] = data["simulated"]
    if isinstance(data.get("model_usage"), dict):
        result["logical_model_calls"] = data["model_usage"].get("logical_calls")
    if type(data.get("http_attempts")) is int:
        result["reported_http_attempts"] = data["http_attempts"]
    if isinstance(data.get("budget"), dict):
        result["request_budget"] = {key: data["budget"].get(key) for key in ("attempts", "maximum")}
    return result
