# Published historical recording

`nemotron-self-debug-20261017/` contains selected, unchanged files from the first
real Crusoe Nemotron code-repair experiment. `recording-manifest.json` lists their
SHA-256 hashes and original location. It includes the report, model journal and
source files needed to bind the proposed edit to model output and rerun checks.
It excludes provider configuration, credentials and SQLite state.

This attempt remains **inconclusive: 15/22 checks passed, no qualified repair or
verified memory**. It is not evidence of a successful Version N+1 upgrade.
The original report pins the target/checker files but lacks the later runner's
orchestrator hash. The stricter showcase therefore keeps its full-snapshot check
false. Publication does not invent that missing historical provenance; local
verification can still rerun the retained target and checker.

From the repository root:

```sh
python3 lab.py demo
python3 lab.py repair --verify-run recursive-redteam/demo/recordings/nemotron-self-debug-20261017 --json
```

Both commands can inspect or verify the recording without a model call. Treat
this bundle as immutable evidence, not as a directory to resume. New runs belong
in ignored `runs/` directories. Source hashes bind the historical implementation,
which may differ from current runner code.
