"""Bounded independent real-SDK trials; no model-output injection."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import time
import urllib.request
from environment import MECHANISM, RECOVERY


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--first", type=int, default=1)
    parser.add_argument("--last", type=int, default=1)
    parser.add_argument("--skip-pilot", action="store_true")
    args = parser.parse_args()
    assert 1 <= args.first <= args.last <= 3
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=False)
    for line in args.env_file.read_text().splitlines():
        if line.startswith("CRUSOE_API_KEY="):
            os.environ["CRUSOE_API_KEY"] = line.split("=", 1)[1].strip()
    req = urllib.request.Request("https://api.inference.crusoecloud.com/v1/models",
              headers={"Authorization": "Bearer " + os.environ["CRUSOE_API_KEY"]})
    with urllib.request.urlopen(req, timeout=30) as response:
        catalog = json.load(response)
    record = next(m for m in catalog["data"] if m["id"] == "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B")
    assert "tools" in record["supported_parameters"]
    (args.out / "model.json").write_text(json.dumps(record, indent=2) + "\n")
    cases = [(phase, condition, trial) for trial in range(args.first, args.last + 1)
             for phase, conditions in (("mechanism", MECHANISM), ("recovery", RECOVERY))
             for condition in conditions
             if not (args.skip_pilot and (phase, condition, trial) == ("mechanism", "identical", 1))]
    random.Random(20261017 + args.first).shuffle(cases)
    manifest = {"cases": cases, "concurrency": 2, "python": sys.version,
                "source_hashes": {name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
                                  for name in ("environment.py", "run_live.py", "batch.py", "experiment-plan.json")}}
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    for name in manifest["source_hashes"]:
        shutil.copyfile(name, args.out / name)
    freeze = subprocess.check_output([sys.executable, "-m", "pip", "freeze"], text=True)
    (args.out / "requirements.lock.txt").write_text(freeze)

    def run(case):
        phase, condition, trial = case
        name = f"{phase}-{condition}-{trial}"
        folder = args.out / name
        command = [sys.executable, "run_live.py", "--phase", phase, "--condition", condition,
                   "--trial", str(trial), "--out", str(folder)]
        print("START " + name, flush=True)
        started = time.monotonic()
        with (args.out / (name + ".log")).open("w") as log:
            try:
                process = subprocess.run(command, stdout=log, stderr=log, timeout=210)
                exit_code = process.returncode
            except subprocess.TimeoutExpired:
                exit_code = "outer_timeout"
        row = {"case": name, "exit_code": exit_code, "elapsed": round(time.monotonic()-started, 2)}
        if (folder / "result.json").exists():
            result = json.loads((folder / "result.json").read_text())
            row.update({k: result[k] for k in ("execution_status", "service_calls", "world_completed",
                         "native_detector_stopped", "autonomous_stuck_candidate", "exception")})
        print("RESULT " + json.dumps(row), flush=True)
        return row

    results = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in as_completed([pool.submit(run, case) for case in cases]):
            results.append(future.result())
            (args.out / "batch-results.json").write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    main()
