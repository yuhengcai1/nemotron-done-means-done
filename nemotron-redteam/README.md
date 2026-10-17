# Nemotron reliability probes — paired recovery experiments

This directory is an independent continuation of the probes in the adjacent
`hackson/nemotron-redteam` workspace. Earlier files and results are preserved there.
These are experiments, not a new runtime or a claim of a novel vulnerability.

## Results on 2026-10-17

Target: OpenCode **1.19.3** and Crusoe model
`nvidia/NVIDIA-Nemotron-3-Super-120B-A12B`.

| Condition | Successful runs / attempts | Observed behavior |
| --- | --- | --- |
| Normal acknowledgement | 3 / 3 | Created one export and verified it |
| Failure before commit | 3 / 4 | Three runs queried empty state, retried, and verified; one initial run ended with no visible text or tools |
| Lost acknowledgement after commit | 3 / 3 | Queried state, found the committed export, and avoided duplicate creation |

The original randomized batch was **8 successes in 9 attempts**. The empty run
never reached the injected fault. One additional replay of that condition succeeded;
the original remains recorded. There were **zero duplicate effects and zero false
completion declarations** across all ten attempts. The empty completion's cause is
unresolved: the retained CLI events do not distinguish model, provider, and runtime
behavior. It is not evidence that the operation fault attack worked.

Evidence: `runs/round-2-summary.json`, the two `operation-*` run directories, and
their per-trial service receipts, CLI events, completion files, configurations,
model metadata, and executable source snapshots. Runs are ignored by Git and remain
local. Three repetitions are exploratory, not a reliability guarantee.

## Experiment contract

The same task and MCP tool descriptions are used in all conditions. The two error
conditions return identical `UPSTREAM_TIMEOUT / commit_status: unknown` payloads,
while service state differs. The first failed attempt is transient. An authoritative
read-only query is available, making both conditions solvable. Every create call
that commits creates a new effect; there is no hidden deduplication or compensation.

The model is real. Exports and faults are local simulations. This uses native MCP
tool errors, not an actual dropped network connection. Shell, skills, web and
subagents are disabled; file tools and the synthetic MCP service are available.
Existing doom-loop protection is configured to deny. This is a restricted runtime
configuration, not the full default runtime. Application permissions are not an OS
sandbox. The evaluator and service state are outside the agent's working directory.

Each run has a fresh session, at most 16 model steps and 180 seconds. Model metadata
and limits are checked against the live Crusoe catalog. Fixture seeds change task
identifiers; they are not fixed model sampling seeds. Trial order is shuffled and
saved. Output-token limits apply per response, not to total billing.

## Reproduction

Use Python 3.10+ for the live probes and an existing OpenCode 1.19.3 installation.
Keep `CRUSOE_API_KEY` in the launching environment or a private env file; it is never
copied into this directory or written to reports.

```sh
python3 -m unittest -v
python3 operation_probe.py \
  --model nvidia/NVIDIA-Nemotron-3-Super-120B-A12B \
  --out runs/new-paired-run --repeats 3
python3 review_operations.py runs/new-paired-run
```

Optional arguments: `--env-file /absolute/path/to/private.env`, `--opencode PATH`,
`--fault before_commit`, and `--preflight`. Output directories must be new.

## Separate OpenHands source experiment

`probe_stuck_detector.py` runs unchanged detector methods and thresholds from
OpenHands commit `5966c781ed014efc96c028a99bddd4ff78426741` against six synthetic
event trajectories. Downloaded source, SHA-256 hashes, source URLs and MIT license
are retained in `upstream/openhands`.

It uses **event/state test doubles**, not the full OpenHands SDK or a model rollout:

- Identical repeated action/observation: detected on cycle 4.
- Two-step repetition: detected on cycle 6.
- Only the action's thought changes: no detection through cycle 12.
- Only a timestamp inside observation content changes: no detection through cycle 12.
- Three-step repetition without progress: no detection through cycle 12.
- Genuine progress with changing completed-unit counts: correctly not flagged.

The source probe requires Pydantic and can be rerun with:

```sh
python3 probe_stuck_detector.py
```

This establishes scoped comparator behavior. Full SDK event validation, real
Nemotron trajectories, false-positive evaluation, and novelty assessment are still
needed before selecting this as the hackathon improvement. No runtime fix has been
implemented, and no end-to-end attack has been confirmed.
