# Nemotron Lab — Version N+1

A Nemotron agent team that attempts to improve its own playbook: execute a task,
inspect its actual history, propose a change, compare versions on fresh tasks,
and continue with the accepted version. Hosted inference uses **Crusoe**.

The repository now has one entry point: **`python3 lab.py`**. The original module
paths remain stable for source-hashed experiments and historical evidence.

**Status:** the native evolution controller and promotion store are implemented.
Implementation and offline tests are not proof of a successful live upgrade.
The bundled demo is an earlier, unsuccessful code-repair experiment; it is
supporting evidence about verification, not a completed Version N+1 result.

## Start here — no API key needed

Python 3.10+, macOS or Linux. The entry point and core use the standard library.

```sh
python3 lab.py catalog                # all components and their roles
python3 lab.py doctor                 # local prerequisites; no network or key reading
python3 lab.py demo                   # http://127.0.0.1:8765/?stage=1
python3 lab.py test                   # offline tests, isolated by component
```

A clean clone includes a selected historical recording and its source snapshots.
Playback and **Verify repair locally** work without hosted inference. Seven of
22 checks still fail in that recording; rejection is the expected result.
Raw experiment directories and credentials are not bundled.

## One project, three responsibilities

| Responsibility | Unified command | Implementation |
|---|---|---|
| Improve the actual native team's playbook | `lab.py evolve` | `agentic-redteam/autonomous.py`, `evolution_store.py`, `runtime.py` |
| Execute and independently check bounded experiments | `lab.py evaluate`, `lab.py student`, `lab.py teach` | `recursive-redteam/recursive_redteam/` and its integrations |
| Inspect earlier behavior and replay evidence | `lab.py demo`, `lab.py repair`, `lab.py inspect` | Recorded repair, native probes, and audit receipts |

The native controller uses the shared model adapter and checkpoint ledger. A
successful promotion changes the active skill. Subsequent native tool receipts
separately establish whether the new skill was actually read and whether that
task passed. Rejected candidates leave the active version unchanged.

The smaller `repair` command edits an isolated historical evaluator function. It
has different budget/resume semantics and does not install its patch into the
native team. We retain it as an experiment, not the main product.

## Run the native evolution experiment

Install OpenCode separately. `OPENCODE_BIN` can select its executable; otherwise
the runner uses `opencode` on PATH (or the original local installation if present).
Copy `.env.example` to `.env` and fill `CRUSOE_API_KEY`, or export that variable.
The default environment metadata in `configs/native-environment.json` was captured
on 2026-10-17; it is not a fresh provider availability check.

```sh
python3 lab.py evolve --out runs/evolution-01 --native-runs 8 --turns 16 --timeout 420
python3 lab.py inspect runs/evolution-01
python3 lab.py monitor --run runs/evolution-01 --port 8766  # read-only receipts
```

This makes real inference calls. The model chooses exploration and candidate
playbooks; the host owns evaluation and promotion. Two fresh task instances of
the same family compare old and candidate versions with identical native settings.
Incomplete runs are inconclusive. Broad transfer is not established by this design.

Accepted versions persist in `state/evolution/`; new run directories are required.
The controller's HTTP budget does not count the native OpenCode team's internal
requests. Native execution is bounded by wall time, per-agent steps and response
length. Native side effects are not automatically replayed after interruption.

## Evidence and documentation

- [Architecture and boundaries](docs/ARCHITECTURE.md)
- [Command reference and migration from old commands](docs/COMMANDS.md)
- [Native evolution implementation and earlier experiments](agentic-redteam/README.md)
- [Historical experiment notes](recursive-redteam/RESEARCH_NOTES.md)
- [Bundled recording and provenance](recursive-redteam/demo/recordings/README.md)

Earlier native OpenCode tests reproduced a historical-publication violation in
2 of 3 complete executions of one fixed scenario. Native Hermes tests observed
memory-related duplicate effects. OpenHands completed all 12 autonomous recovery
trials. Earlier matched-budget skill-transfer runs timed out; they establish no
learning gain. These are scoped observations, not general benchmark results.

Built during the October 17, 2026 Recursive Agents Hackathon. Initial harness code
was authored with coding assistants; runtime experiments use Nemotron. Existing
OpenCode, Hermes and OpenHands capabilities are third-party dependencies. The
small OpenHands reference excerpt under `nemotron-redteam/upstream/` retains its
upstream attribution and license. NemoClaw/OpenShell and Megatron are not integrated.
