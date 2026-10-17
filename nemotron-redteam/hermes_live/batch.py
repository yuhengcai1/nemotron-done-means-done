"""Bounded, randomized live Hermes trials; two processes, independent profiles."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from cases import CONDITIONS, ROOT
from run_live import BASE, MODEL, SOURCE, dump


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--env-file", type=Path, required=True)
    args = p.parse_args()
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=False)
    key = next(line.split("=", 1)[1].strip() for line in args.env_file.read_text().splitlines() if line.startswith("CRUSOE_API_KEY="))
    request = urllib.request.Request(BASE + "/models", headers={"Authorization": "Bearer " + key})
    with urllib.request.urlopen(request, timeout=30) as response:
        model = next(m for m in json.load(response)["data"] if m["id"] == MODEL)
    assert "tools" in model.get("supported_parameters", [])
    dump(args.out / "model.json", model)
    cases = [(condition, trial) for condition in CONDITIONS for trial in (1, 2)]
    random.Random(20261017).shuffle(cases)
    sources = list(Path(__file__).parent.glob("*.py")) + [ROOT / "export_service.py", ROOT / "openhands_live" / "environment.py"]
    snapshots = args.out / "sources"
    snapshots.mkdir()
    for path in sources:
        shutil.copyfile(path, snapshots / path.name)
    dump(args.out / "manifest.json", {"order": cases, "max_concurrent": 2, "max_trials": len(cases),
        "git_commit": subprocess.check_output(["git", "-C", str(SOURCE), "rev-parse", "HEAD"], text=True).strip(),
        "source_hashes": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}})
    shutil.copyfile(Path(__file__).with_name("experiment-plan.json"), args.out / "experiment-plan.json")
    with (args.out / "requirements.txt").open("w") as f:
        subprocess.run(["/private/tmp/nemotron-hermes-bootstrap/bin/uv", "pip", "freeze", "--python", sys.executable], stdout=f, check=True)

    def run(case):
        condition, trial = case
        name = f"{condition}-{trial}"
        dest = args.out / name
        started = time.monotonic()
        print("START " + name, flush=True)
        with (args.out / (name + ".log")).open("w") as f:
            process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("run_live.py")),
                "--condition", condition, "--trial", str(trial), "--out", str(dest),
                "--env-file", str(args.env_file.resolve())], stdout=f, stderr=f, start_new_session=True)
            timed_out = False
            try:
                process.wait(timeout=210)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        result = json.loads((dest / "result.json").read_text()) if (dest / "result.json").exists() else {}
        row = {"case": name, "exit_code": process.returncode, "outer_timeout": timed_out,
               "elapsed": round(time.monotonic()-started, 2), **{k: result.get(k) for k in
                   ("objective_met", "service_calls", "exception", "native_completed", "duplicate_effect", "autonomous_stuck_candidate")}}
        print("RESULT " + json.dumps(row), flush=True)
        return row

    results = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in as_completed([pool.submit(run, case) for case in cases]):
            results.append(future.result())
            dump(args.out / "batch-results.json", results)


if __name__ == "__main__":
    main()
