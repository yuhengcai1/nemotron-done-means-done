# Architecture

`lab.py` / `nemotron_lab` is the common user-facing layer: command registry,
caller-relative path resolution, offline prerequisite checks, read-only report
views and isolated test execution. It delegates to existing implementations;
it does not re-implement their loops or silently change their acceptance rules.

## Main path: Version N+1

```text
lab.py evolve
  └─ autonomous.py: Nemotron controller, native function tools
      ├─ run_task → runtime.py → real OpenCode lead/planner/executor/verifier
      ├─ inspect_run → executed tool events + authoritative world history
      ├─ propose_skill → immutable candidate, tied to an accepted parent
      ├─ evaluate_candidate → fresh paired tasks → evolution_store.py decision
      └─ run_task → currently accepted skill → read receipt + task outcome
```

The controller reuses `recursive_redteam.providers.OpenAICompatibleModel` and
`integrations.student_runtime.checkpoint.CheckpointLedger`. It cannot substitute
an LLM opinion for the host oracle. Native side-effect recovery is not implemented;
controller ledger durability must not be confused with replaying the inner team.

Only exploratory traces are exposed through `inspect_run`. Evaluation returns
aggregate feedback, not hidden task traces. Promotion checks complete native
executions, candidate skill read receipts and outcomes. This is a bounded check
on two fresh instances of one family, not a generalization benchmark.

## Evaluation support

`recursive_redteam.engine` performs model-led search and delegation, target
execution, repeat/control verification and evidence retention. Targets can be
configured live adapters or local simulations; each result retains that scope.

`TeachingRunner` compares fixed lessons against independent baseline histories.
The durable student route recreates deterministic local tools and replays saved
model replies. Cached replies are continuation evidence, not additional samples.
`ReliableTeachingRunner` is a separate experimental implementation with schema and
causal-feedback checks; the durable student route still uses `TeachingRunner`.

## Historical repair support

`self_debug.py` allows a model to edit only a historical evaluator's `finish`
function. It can read all acceptance tests. Successful exact-source repair memory
would be rechecked by `recall_repair.py`; this is not native skill activation.
Its logical-call budget permits provider retries and differs from the controller's
HTTP-attempt budget. The CLI intentionally preserves these distinctions.

The demo resolves an explicit run ID: local report first, then a bundled recording
of the same ID. A partial local run never falls back to a different successful
recording. The first bundled repair is unsuccessful; the server never promotes it.

## Storage and ownership

| Location | Purpose | Version controlled? |
|---|---|---|
| `runs/` | New runs launched from the repository root | No |
| `state/evolution/` | Accepted pointer, candidates, decisions, prior episodes | No |
| Component `runs/` | Existing experimental artifacts and source snapshots | No |
| `demo/recordings/` inside the evaluation component | Selected historical evidence with manifest | Yes |
| `.env` | Local inference credential | No |
| `configs/native-environment.json` | Captured, non-secret model metadata | Yes |

We keep original module paths because source manifests, frozen snapshots and
native launchers bind to them. Physical relocation would require a separate
migration of those contracts. The unified interface does not rewrite historical
files, reclassify failures, or represent offline fixtures as live improvements.
