# Evaluation and verification components

This directory is part of **[Nemotron Lab — Version N+1](../README.md)**.
Use the repository's single entry point from its root:

```sh
python3 lab.py evaluate demo --out runs/offline-check
python3 lab.py student --help
python3 lab.py teach --help
python3 lab.py repair --help
python3 lab.py demo
python3 lab.py test --suite core
```

- `recursive_redteam/`: shared model adapter, evaluation engine, evidence gate,
  SQLite store, request budget and experiment memory.
- `integrations/student_runtime/`: strict persistent HTTP-attempt accounting and
  transcript recovery for deterministic local student experiments.
- `integrations/teacher_student/`: historical teaching and transfer suites.
- `examples/self_debug.py`: bounded repair of a disclosed historical evaluator
  defect. Its known tests and exact-source memory do not demonstrate unseen-task
  improvement or installation into the native team's next run.
- `demo/`: replay of that historical repair attempt and fresh local verification.
  The bundled attempt remains unqualified (15/22 checks).

The main native evolution loop lives in `../agentic-redteam/autonomous.py`, reached
through `lab.py evolve`. It is distinct from the historical maintenance demo.

Existing direct scripts still work. [Detailed research notes](RESEARCH_NOTES.md)
retain historical commands and results; links into ignored `runs/` directories
refer to local evidence that is not included in a clean clone. See the root
[command reference](../docs/COMMANDS.md) for supported entry points.
