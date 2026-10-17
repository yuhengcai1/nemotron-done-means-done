"""Two native learning chains, each followed by a matched empty-memory control."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from run_live import dump


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--env-file", type=Path, required=True)
    args = p.parse_args()
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=False)
    sources = args.out / "sources"
    sources.mkdir()
    for f in Path(__file__).parent.glob("*.py"):
        shutil.copyfile(f, sources / f.name)
    shutil.copyfile(Path(__file__).with_name("learning-plan.json"), args.out / "learning-plan.json")

    def run(name, condition, trial, memory=None):
        dest = args.out / name
        command = [sys.executable, str(Path(__file__).with_name("run_learning.py")),
                   "--condition", condition, "--trial", str(trial), "--out", str(dest),
                   "--env-file", str(args.env_file.resolve())]
        if memory:
            command += ["--memory-from", str(memory)]
        print("START " + name, flush=True)
        start = time.monotonic()
        with (args.out / (name + ".log")).open("w") as f:
            proc = subprocess.Popen(command, stdout=f, stderr=f, start_new_session=True)
            timed_out = False
            try:
                proc.wait(timeout=210)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
        result = json.loads((dest / "result.json").read_text()) if (dest / "result.json").exists() else {}
        row = {"case": name, "exit_code": proc.returncode, "outer_timeout": timed_out,
               "elapsed": round(time.monotonic()-start,2), **{k:result.get(k) for k in
                   ("objective_met", "duplicate_effect", "service_calls", "exception", "memory_fixture_in_system_prompt")}}
        print("RESULT " + json.dumps(row), flush=True)
        return result, row

    def chain(trial):
        warm_name = f"warmup-{trial}"
        warm, warm_row = run(warm_name, "learn_warmup", trial)
        memory = args.out / warm_name / "profile" / "memories" / "MEMORY.md"
        events_path = args.out / warm_name / "events.jsonl"
        events = [json.loads(x) for x in events_path.read_text().splitlines()] if events_path.exists() else []
        native_write = any(e.get("kind") == "tool_complete" and e.get("name") == "memory" for e in events)
        eligible = bool(warm.get("objective_met") and native_write and memory.exists() and memory.read_text().strip())
        report = {"warmup_trial": trial, "challenge_trial": trial+100,
                  "eligible": eligible, "native_memory_tool_completed": native_write, "runs": [warm_row]}
        if not eligible:
            report["reason"] = "Warmup or genuine native memory write prerequisite not met; no invented memory inserted."
            dump(args.out / f"chain-{trial}.json", report)
            return report
        learned = memory.read_bytes()
        saved = args.out / f"learned-memory-{trial}.txt"
        saved.write_bytes(learned)
        report["learned_memory_sha256"] = hashlib.sha256(learned).hexdigest()
        # Alternate order across chains; the prompts/service contract/seed match within each pair.
        arms = ("learned", "empty") if trial == 21 else ("empty", "learned")
        for arm in arms:
            _, row = run(f"challenge-{arm}-{trial+100}", "learn_challenge", trial+100,
                         saved if arm == "learned" else None)
            row["arm"] = arm
            report["runs"].append(row)
            dump(args.out / f"chain-{trial}.json", report)
        return report

    reports = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in as_completed([pool.submit(chain, t) for t in (21,22)]):
            reports.append(future.result())
            dump(args.out / "batch-results.json", reports)


if __name__ == "__main__":
    main()
