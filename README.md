# Nemotron Red-Teams Its Own Agents — Recursive Agents Hackathon

Built on October 17, 2026 at The Recursive Agents Hackathon. All agents at runtime are
NVIDIA Nemotron models served by **Crusoe Managed Inference**. Framework code was written
with AI coding assistants during the event; no project code predates the hackathon.

**Track: Done Means Done.** Agents often report success they have not earned. We built
infrastructure that runs real agent runtimes on Nemotron, records every tool call
and side effect independently of the agent's own report, and checks whether
"done" was actually true. We then let Nemotron try to repair the failures it found.

## What is in this repository

| Folder | Built today | Status |
|---|---|---|
| [`nemotron-redteam/`](nemotron-redteam) | Drives OpenCode, Hermes and OpenHands with live Nemotron; probes retries, loops, memory transfer and false completion | Real runs with recorded evidence |
| [`agentic-redteam/`](agentic-redteam) | Real multi-agent OpenCode hand-offs; dynamic state checks; skill extraction from traces, loaded by a fresh team | Skill loop ran; equal-budget evaluation timed out (no claimed gain) |
| [`recursive-redteam/`](recursive-redteam) | Task allocation, experiment generation, independent reproduction, controls, SQLite ledger, budgets; Nemotron self-debugging demo | Demo replays real recorded Crusoe calls (`python3 demo/server.py`) |

`nemotron-redteam/upstream/` holds a small excerpt of third-party OpenHands code used for
reference; it is not our work.

## Evidence so far (honest summary)

| Experiment | What we observed | What we do **not** claim |
|---|---|---|
| Native OpenCode multi-agent | In a fixed dynamic scenario, 2 of 3 full runs published a history violation while reporting completion | A general failure rate |
| Native Hermes memory | Memory was really written, transferred and read; some tasks repeated side effects | That memory is always harmful, or a reliable fix |
| Native OpenHands | 12/12 autonomous recovery tasks completed; some sampled tasks misreported counts | A reproduced infinite loop |
| Nemotron self-repair v1–v3 | Real model edits to a frozen framework revision; best result 15/22 checks; no repair qualified | A completed self-improvement (Version N+1) |

The independent checks are the point: a repair that failed 7/22 checks was **not**
accepted, even though the model proposed it confidently.

## Not done yet

- The accepted-change → next-generation loop is not closed: repairs are stored and re-checked,
  but not yet installed into the next agent run.
- No NemoClaw/OpenShell or Megatron integration.
- Small sample sizes; results are per-scenario evidence, not benchmarks.

## Run the demo

```sh
cd recursive-redteam
python3 demo/server.py   # open http://127.0.0.1:8765/?stage=1
```

Crusoe key (only for new live runs): `export CRUSOE_API_KEY='...'`.
