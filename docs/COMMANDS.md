# Commands

Run `python3 lab.py catalog` for the complete registry. The root entry point also
works as `python3 -m nemotron_lab`. No installation is required for core commands.
Use `python3 lab.py <component> --help` for that component's actual options.

| Command | Replaces | Inference |
|---|---|---|
| `evolve` | `agentic-redteam/autonomous.py` | Live controller and native agents |
| `evaluate` | `recursive-redteam` package CLI | `demo` offline; `run`/`resume` depend on config |
| `student` | `examples/teach.py --student-resume` | Live student, local deterministic target |
| `teach` | `examples/teach.py` | Live unless `--offline` |
| `repair` | `examples/self_debug.py` | Live except `--verify-run` |
| `demo` | `demo/server.py` | None |
| `monitor` | `agentic-redteam/live_monitor.py` | None; only reads existing receipts |
| `native-check` | `agentic-redteam/campaign.py` | Live |
| `hermes-check` | `hermes_live/run_live.py` | Live; separate Hermes installation |
| `openhands-check` | `openhands_live/run_live.py` | Live; separate OpenHands SDK installation |
| `inspect PATH` | Manual report reading | None; does not resume or rewrite |
| `doctor` | Checking local prerequisites by hand | None; does not read key values |
| `test` | Separate unittest invocations | None; localhost HTTP stubs may be used |

## Paths and credentials

Explicit path options such as `--out`, `--env-file`, `--config` and `--memory` are
resolved from the caller's current directory before invoking the component.
`demo --run` remains a recording ID, not a path. Existing component defaults are
preserved except `evolve`, whose defaults are the root `.env`,
`configs/native-environment.json`, and `state/evolution/`.

`lab.py` never reads or prints credentials. Each live component loads its own
named key. For legacy native probes, pass `--env-file /absolute/path/to/.env`
explicitly; do not rely on a previous developer's directory layout.

A separate runtime interpreter can be selected before the component:

```sh
python3 lab.py --python /path/to/hermes-venv/bin/python hermes-check --help
```

OpenCode is a separate executable. Export `OPENCODE_BIN=/path/to/opencode` to
select it. `doctor` checks presence only, not authentication or model readiness.

## Examples

```sh
# Entirely local engine demonstration
python3 lab.py evaluate demo --out runs/offline-01
python3 lab.py inspect runs/offline-01

# Fixed historical recording; no model requests
python3 lab.py demo --port 8765
python3 lab.py repair --verify-run recursive-redteam/demo/recordings/nemotron-self-debug-20261017 --json

# Durable local-target student experiment; explicit live request cap
python3 lab.py student --out runs/student-01 --env-file .env --max-attempts 24
python3 lab.py student --resume --out runs/student-01 --env-file .env

# Native Version N+1 experiment; task runs use separate wall/step/output bounds
python3 lab.py evolve --out runs/evolution-01 --native-runs 8 --turns 16 --timeout 420

# All suites, or selected suites
python3 lab.py test
python3 lab.py test --suite entrypoint --suite core --suite native
```

Legacy historical-replay tests are skipped when their unpublished adjacent native
artifacts are missing. Core fixture tests still run. Passing implementation tests
does not establish an observed live learning gain.

The CLI preserves subprocess exit codes. A successful `repair --verify-run` process
can print `verified: false`: this means the check executed successfully but the
repair failed. Read the evidence fields; process exit alone is not a success oracle.
