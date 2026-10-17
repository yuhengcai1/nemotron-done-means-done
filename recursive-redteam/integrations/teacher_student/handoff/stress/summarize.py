"""Combine retained original attempts and a predeclared transport-only retry."""
import argparse
import hashlib
import json
from pathlib import Path


def load(path):
    return json.loads(path.read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("initial", type=Path)
    parser.add_argument("retry", type=Path)
    args = parser.parse_args()
    initial, retry = args.initial.resolve(), args.retry.resolve()
    a, b = load(initial / "report.json"), load(retry / "report.json")
    aa, ba = load(initial / "independent-audit.json"), load(retry / "independent-audit.json")
    selection = load(retry / "retry-selection.json")
    assert a["status"] == b["status"] == "completed"
    assert aa["audit_passed"] and ba["audit_passed"]
    expected = [c["id"] for c in a["cases"] if c["episode"].get("error") == "ProviderError"]
    assert expected == selection["original_case_ids"]
    assert len(b["cases"]) == len(expected)
    retry_map = {old: (case, audited) for old, case, audited in zip(expected, b["cases"], ba["cases"])}
    retained = []
    for original, audited in zip(a["cases"], aa["cases"]):
        selected, stats = original, audited
        transport_retry = original["id"] in retry_map
        if transport_retry:
            selected, stats = retry_map[original["id"]]
            assert all(selected[k] == original[k] for k in ("variant", "repaired", "lesson"))
        retained.append({"original_case_id": original["id"], "transport_retry": transport_retry,
                         "original_attempt_error": original["episode"].get("error"),
                         "final_attempt_run": str(retry if transport_retry else initial),
                         "final_attempt_case_id": selected["id"], **stats})
    groups = {}
    for repaired in (False, True):
        for lesson in (False, True):
            key = ("repaired" if repaired else "vulnerable") + ("_taught" if lesson else "_baseline")
            rows = [c for c in retained if c["repaired"] == repaired and c["lesson"] == lesson]
            groups[key] = {"episodes": len(rows), "verified_findings": sum(c["verified"] for c in rows),
                           "episode_errors": sum(c["error"] is not None for c in rows),
                           "episodes_with_valid_probes": sum(c["valid_measured_probes"] > 0 for c in rows),
                           "episodes_with_two_valid_probes": sum(c["valid_measured_probes"] >= 2 for c in rows),
                           "unsupported_submissions": sum(c["unsupported_submissions"] for c in rows)}
    completion = sum(r["model_usage"]["reported_tokens"].get("completion_tokens", 0) for r in (a, b))
    prompt = sum(r["model_usage"]["reported_tokens"].get("prompt_tokens", 0) for r in (a, b))
    semantic_path = retry / "causal-explanation-review.json"
    semantic = load(semantic_path)
    result = {"status": "completed_and_audited", "cases": retained, "groups": groups,
              "original_attempts": len(a["cases"]), "transport_retry_attempts": len(b["cases"]),
              "logical_model_calls": sum(r["model_usage"]["logical_calls"] for r in (a, b)),
              "actual_http_attempts_including_catalogues": sum(r["actual_http_attempts_including_catalogue"] for r in (a, b)),
              "reported_prompt_tokens": prompt, "reported_completion_tokens": completion,
              "new_local_audit_reruns": aa["fresh_local_reruns"] + ba["fresh_local_reruns"],
              "audit_assertions_passed": len(aa["checks"]) + len(ba["checks"]),
              "all_initial_failures_retained": True, "shared_runtime_modified": False,
              "causal_teaching_gain_established": False,
              "causal_explanation_review": {"path": str(semantic_path),
                   "sha256": hashlib.sha256(semantic_path.read_bytes()).hexdigest(),
                   "reproduction_verified": semantic["reproduction_verified"],
                   "student_causal_explanation_correct": semantic["student_causal_explanation_correct"],
                   "model_reviewer_causal_explanation_correct": semantic["model_reviewer_causal_explanation_correct"],
                   "local_ablation_trials": semantic["local_ablation_trials"]},
              "source_hashes": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for r in (initial, retry) for p in (r / "report.json", r / "independent-audit.json", r / "source-manifest.json")},
              "scope": "Fresh real Crusoe Nemotron histories; three new synthetic transition holdouts around a known, frozen local judge bug. Neither an unknown production defect nor a native platform attack.",
              "limits": ["A transport retry is a new stochastic attempt, not resuming the interrupted model trajectory.",
                         "Completed unsuccessful searches were not retried or replaced.",
                         "Invalid-only negative episodes provide no meaningful behavioral coverage.",
                         "Only three paired scenarios per backend; no population success rate or causal teaching effect established."]}
    target = initial / "combined-summary.json"
    (initial / "summarize-source.py").write_bytes(Path(__file__).read_bytes())
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: result[k] for k in ("status", "groups", "logical_model_calls", "new_local_audit_reruns", "audit_assertions_passed")}, indent=2))


if __name__ == "__main__":
    main()
