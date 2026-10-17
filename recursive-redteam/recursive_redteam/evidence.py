"""Import provenance and narrow result claims without endorsing external findings."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path


def import_evidence(source: str | Path, destination_dir: str | Path) -> dict:
    source = Path(source).resolve()
    if source.suffix.lower() != ".json" or source.name.startswith(".env"):
        raise ValueError("evidence source must be a JSON result artifact")
    if source.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("evidence file exceeds 8 MiB")
    raw = source.read_bytes()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("evidence must be a JSON object")
    digest = hashlib.sha256(raw).hexdigest()
    # Do not copy raw requests, headers, environments or arbitrary nested content.
    claims = {}
    for key in ("status", "passed", "confirmed_failure", "confirmed_attack", "artifact_correct",
                "false_completion", "duplicate_effect", "timed_out", "exit_code", "completed_trials",
                "passed_trials", "false_completions", "duplicate_effects", "review_status"):
        if key in data and isinstance(data[key], (str, bool, int, float, type(None))):
            claims[key] = data[key]
    if not claims:
        raise ValueError("no recognized result claims; provide a result/verdict/summary JSON")
    if isinstance(data.get("violations"), list) and all(isinstance(item, str) and len(item) <= 200 for item in data["violations"]):
        claims["violations"] = data["violations"][:50]
    artifact = {"schema_version": 1, "classification": "external_unverified", "source_path": str(source),
                "source_sha256": digest, "imported_at": datetime.now(timezone.utc).isoformat(),
                "source_claims": claims,
                "note": "Imported claims retain provenance. No replay or independent verification was performed by this framework."}
    destination = Path(destination_dir)
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / (digest[:20] + ".json")
    if not path.exists():
        path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return dict(artifact, artifact_path=str(path.resolve()))
