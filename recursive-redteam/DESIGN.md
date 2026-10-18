# Historical maintenance and evaluation design

This document describes the earlier repair experiment and underlying evaluation
engine. The current project is [Nemotron Lab — Version N+1](../README.md); its
native evolution path and shared entry point are documented in
[Architecture](../docs/ARCHITECTURE.md). The maintenance demonstration below is a
supporting experiment, not the project's current end-to-end evolution design.

## Historical maintenance demonstration scope

Nemotron maintains the agent framework through a bounded debugging loop. A
Nemotron planner delegates a repair worker. That worker reads the framework
source, requests tests, proposes an exact code edit and reacts to execution
feedback. A separate Nemotron reviewer examines the patch and fresh test receipts.
The host qualifies the repair only when independent checks support it, then stores
the source hashes, repair and evidence in append-only memory. No teacher agent or
teacher lesson participates in this runtime.

The current implementation demonstrates maintenance of one isolated historical
framework revision. The initial framework and test infrastructure were authored
by Codex. Nemotron authors its own diagnosis and repair. The demonstration does
not establish unrestricted self-improvement or autonomous construction of the
entire framework.

### Maintenance boundaries

- `examples/self_debug.py` owns the maintenance loop and records actual Crusoe
  model requests, replies and tool execution. Planning, editing and review use
  separate Nemotron roles under one call budget.
- The editable artifact is an isolated copy of `PolicySandbox.finish`. AST checks
  preserve the rest of the source, its signature and allowed operations. The model
  cannot change the checks or issue arbitrary shell commands.
- `demo/repair_checks.py` evaluates 22 deterministic contract cases, including
  passing behavior, protocol failures and an unexposed-input control. Fresh
  processes rerun checks after the model submits a repair.
- Invalid actions, unchanged edits and repeated reads return feedback. An exhausted
  budget or a failing check yields an inconclusive run, never a successful repair.
- `runs/self-debug-memory.sqlite` retains verified repair evidence with a hash
  chain. Recall matches source and checker identity and tests the retrieved repair.

### Three-minute demonstration

The deck establishes the failure and the autonomous loop, then hands off to the
local frontend. The frontend replays a captured real Nemotron run with its model
decisions, source diff and test receipts. Its verification button executes new
local tests without an API call. Recorded inference and fresh local verification
have separate labels. The final slides show measured results and memory behavior.
Failed attempts remain in the record and do not count as proof of repair.

The frontend uses local assets and requires no network to replay the saved run.
Crusoe is required only to capture a new model-driven maintenance run.

## Underlying evaluation engine

This project is the hackathon deliverable: external models plan and generate
bounded evaluations, trusted target adapters execute them, and a model verifier
chooses verification steps. The persistent engine validates measured evidence.
CI uses simulated models; the operator can run the same framework against real
Crusoe/Nemotron inference with local synthetic tools. There is no separate product named Axon.

### Evaluation architecture

1. OpenAI-compatible model adapter, configurable for Crusoe or another provider.
2. Coordinator assigns workers and parent tasks; worker calls propose constrained
   payloads. Invalid plans receive bounded feedback for model-authored correction.
3. Target plugins expose `describe()` and `evaluate(candidate, trial_id=...)`.
4. SQLite stores events, candidate ancestry, trial results and final findings.
5. A passing control is required before search. A separate verifier context chooses
   replay/control tools and submits a reasoned verdict citing trial IDs. The host
   requires matching violating replays and a passing control for `reproduced`.
   Infrastructure failures remain errors; unsupported model claims are rejected.
6. Feedback creates bounded child candidates; interrupted queued work can resume.
7. CLI provides doctor, demo, run, resume/report and evidence-import commands.
8. A separate SQLite memory shares exact-input observations across campaigns,
   scoped to model, policy, evaluator and environment revision. Leases prevent
   concurrent duplicates; expiry, conflicts and forced retests prevent permanent
   suppression based on stale or uncertain observations.

The shared Python contracts are in `recursive_redteam/contracts.py`. All modules
use Python 3.10+ standard library unless packaging tooling is requested. API keys
come from named environment variables and are not stored in configuration/artifacts.
In the evaluation engine, only user-configured plugin code/commands may execute;
generated evaluation payloads are data. The separate maintenance demonstration
permits model-authored code edits within the explicit boundaries above.

## Intended module interfaces

- `providers.OpenAICompatibleModel(base_url, model, api_key_env='CRUSOE_API_KEY',
  timeout=60, max_tokens=1024, temperature=0.3, max_retries=2)` implements Model.
  `list_models()` returns catalogue records; `describe()` returns sanitized setup.
- `providers.DemoModel(role)` supports coordinator/attacker/target/verifier roles for an
  explicitly simulated offline demonstration.
- `targets.PolicySandbox(model, max_steps=6)` implements Target with fresh state,
  immutable policy, a synthetic private marker and a tool-result injection point.
- `targets.CommandTarget(command, timeout=60, cwd=None)` exchanges one JSON request
  and response with an explicitly user-configured command (no shell interpolation).
- `store.Store(path)` is a durable SQLite event/task store. Engine and store APIs
  are owned together by the engine contributor.
- `engine.Engine(model, target, store, limits)` exposes `run(resume=False) -> dict`.
  Optional verifier/memory integrations are injected by CLI, which wraps every
  model role in a shared durable call budget and optional request gate.
- `verification.ModelVerifier.review(candidate, first, run_trial)` implements
  the model-directed replay/control/review loop, gated by measured evidence.
- `memory.ExperimentMemory` provides claim, renew, finish, release, recall and
  stats. Evidence is append-only; model-generated text is never a trusted rule.

Each model call stores allowlisted input/output protocol fields, actual returned
model ID and UTC timestamps. Provider failures carry scrubbed diagnostic categories.
Run configuration and runtime source hashes are retained; changed source cannot
resume unfinished experiments under their old identity.

## Evidence labels

`simulated` identifies offline fixtures; `candidate` is an unreplayed violation;
`reproduced` means fresh oracle-confirmed replays plus a passing paired control;
`inconclusive` covers errors or inconsistent replays. External imported claims keep
their original provenance and are never automatically promoted to reproduced.

Persistent recovery guarantees apply to our local sandbox. External adapters must
honor `trial_id` idempotency or declare that interrupted work requires review.
