import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from nemotron_lab.cli import command_for, resolve_paths
from nemotron_lab.registry import ROOT
from nemotron_lab.reports import summarize


class EntrypointTests(unittest.TestCase):
    def test_caller_paths_survive_switching_component_directory(self):
        command, cwd = command_for("evaluate", ["run", "--out", "runs/new", "--config=trial.json"],
                                   caller=Path("/tmp/caller"), python="python")
        self.assertEqual(cwd, ROOT / "recursive-redteam")
        self.assertEqual(command[-3:], ["--out", str(Path("/tmp/caller/runs/new").resolve()),
                                       "--config=" + str(Path("/tmp/caller/trial.json").resolve())])

    def test_demo_run_is_an_id_not_a_filesystem_path(self):
        command, _ = command_for("demo", ["--run", "nemotron-self-debug-20261017-v2"])
        self.assertEqual(command[-1], "nemotron-self-debug-20261017-v2")

    def test_missing_path_cannot_swallow_another_option(self):
        with self.assertRaises(ValueError):
            resolve_paths(["--out", "--resume"], ("--out",), Path.cwd())

    def test_live_entrypoint_has_checkout_local_defaults(self):
        command, _ = command_for("evolve", ["--out", "runs/new"])
        self.assertEqual(command[command.index("--memory") + 1], str(ROOT / "state/evolution"))
        self.assertEqual(command[command.index("--env-file") + 1], str(ROOT / ".env"))

    def test_help_is_available_from_outside_repository_without_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run([sys.executable, "-B", str(ROOT / "lab.py"), "student", "--help"],
                cwd=folder, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--max-attempts", result.stdout)

    def test_completed_report_never_implies_successful_upgrade(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "report.json"
            path.write_text(json.dumps({"status": "completed", "upgrade_loaded": False,
                                       "upgrade_followup_passed": False, "promoted": []}))
            result = summarize(path)
        self.assertFalse(result["upgrade_loaded"])
        self.assertFalse(result["upgrade_followup_passed"])

    def test_inspect_does_not_create_or_change_files(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "report.json"
            path.write_text('{"status":"inconclusive","source_origin":"historical"}')
            before = path.read_bytes()
            result = summarize(path)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(Path(folder).iterdir()), [path])
        self.assertEqual(result["kind"], "historical_repair")
        self.assertFalse(result["repair_verified"])
