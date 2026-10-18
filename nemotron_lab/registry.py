"""One registry of runnable components; historical source locations stay stable."""
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Component:
    directory: str
    arguments: tuple[str, ...]
    description: str
    path_flags: tuple[str, ...] = ("--out", "--env-file", "--config", "--memory", "--environment")


COMPONENTS = {
    "evolve": Component("agentic-redteam", ("autonomous.py",),
        "Version N+1: native team, candidate skills, paired evaluation and promotion (live inference)"),
    "evaluate": Component("recursive-redteam", ("-m", "recursive_redteam"),
        "Shared evaluation engine: demo/run/resume/report/validate/doctor",
        ("--out", "--env-file", "--config", "--run")),
    "student": Component("recursive-redteam", ("examples/teach.py", "--student-resume"),
        "Durable student experiment loop; local target, live student"),
    "teach": Component("recursive-redteam", ("examples/teach.py",),
        "Historical teaching comparisons; --offline verifies local teacher cases"),
    "repair": Component("recursive-redteam", ("examples/self_debug.py",),
        "Bounded historical code-repair experiment; not the evolution product",
        ("--out", "--env-file", "--verify-run")),
    "demo": Component("recursive-redteam", ("demo/server.py",),
        "Offline recorded repair demo and fresh local checks; no model calls", ()),
    "monitor": Component("agentic-redteam", ("live_monitor.py",),
        "Read-only projection of native evolution receipts; does not start inference", ("--run",)),
    "native-check": Component("agentic-redteam", ("campaign.py",),
        "Historical native multi-agent reliability campaign (live inference)"),
    "hermes-check": Component("nemotron-redteam/hermes_live", ("run_live.py",),
        "Native Hermes experiments; requires the separately installed Hermes environment"),
    "openhands-check": Component("nemotron-redteam/openhands_live", ("run_live.py",),
        "Native OpenHands experiments; requires the separately installed SDK"),
}

# Each suite runs in a separate process: legacy modules such as `runtime`,
# `cases`, and `tests` otherwise collide in Python's import cache.
TEST_SUITES = {
    "entrypoint": (".", "lab_tests", "test_*.py"),
    "core": ("recursive-redteam", "tests", "test_*.py"),
    "native": ("agentic-redteam", ".", "test_*.py"),
    "probes": ("nemotron-redteam", ".", "test_*.py"),
    "hermes": ("nemotron-redteam/hermes_live", ".", "test_*.py"),
    "openhands": ("nemotron-redteam/openhands_live", ".", "test_*.py"),
}
