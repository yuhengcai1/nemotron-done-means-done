"""Run pinned, unchanged upstream detection methods with lightweight event doubles.

This isolates comparator behavior. It does not run the full OpenHands SDK or an LLM.
The event payloads and state interface cover the fields used by the upstream detector.
"""
import ast
from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import Path
from pydantic import BaseModel, Field
from redteam import dump


class Event:
    pass


@dataclass
class ActionEvent(Event):
    source: str
    thought: str
    action: dict
    tool_name: str


class ObservationBaseEvent(Event):
    pass


@dataclass
class ObservationEvent(ObservationBaseEvent):
    source: str
    observation: dict
    tool_name: str


class AgentErrorEvent(ObservationBaseEvent):
    pass


class CondensationSummaryEvent(Event):
    pass


class MessageEvent(Event):
    pass


class ConversationState:
    def __init__(self):
        self.events = []

    def active_branch(self, limit):
        return self.events[-limit:]


def load_detector(root):
    manifest = json.loads((root / "manifest.json").read_text())
    for name, metadata in manifest["files"].items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == metadata["sha256"]
    namespace = dict(globals())
    namespace["logger"] = logging.getLogger("upstream_probe")
    namespace["logger"].disabled = True
    # Only this unchanged class is needed; the types module also uses Python 3.12 syntax.
    threshold_source = (root / "types.py").read_text().split("class StuckDetectionThresholds", 1)[1]
    exec(compile("class StuckDetectionThresholds" + threshold_source, str(root / "types.py"), "exec"), namespace)
    tree = ast.parse((root / "stuck_detector.py").read_text())
    selected = [node for node in tree.body if
                isinstance(node, ast.ClassDef) and node.name == "StuckDetector"
                or isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                and node.target.id == "MAX_EVENTS_TO_SCAN_FOR_STUCK_DETECTION"]
    # Keep every upstream method unchanged; substitute only imports and dependencies.
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(root / "stuck_detector.py"), "exec"), namespace)
    return namespace["StuckDetector"], manifest


def run_case(detector_type, mode, cycles=12):
    state = ConversationState()
    detector = detector_type(state)
    checks = []
    for i in range(cycles):
        command = "check_status"
        if mode == "period_two":
            command = ("check_status", "read_log")[i % 2]
        elif mode == "period_three":
            command = ("check_status", "read_log", "inspect_config")[i % 3]
        thought = f"Checking again, attempt {i}" if mode == "thought_varies" else "Check the job"
        observation = {"status": "blocked", "reason": "configuration_invalid"}
        if mode == "timestamp_varies":
            observation["timestamp"] = f"2026-10-17T12:00:{i:02d}Z"
        elif mode == "real_progress":
            observation = {"status": "running", "completed_units": i + 1}
        state.events.extend([ActionEvent("agent", thought, {"command": command}, "terminal"),
                             ObservationEvent("environment", observation, "terminal")])
        checks.append(detector.is_stuck())
    return {"case": mode, "cycles": cycles, "first_detection_cycle":
            next((i + 1 for i, stuck in enumerate(checks) if stuck), None),
            "detections_by_cycle": checks,
            "ground_truth": "progressing" if mode == "real_progress" else "no_progress"}


def main():
    here = Path(__file__).parent
    detector, manifest = load_detector(here / "upstream/openhands")
    cases = [run_case(detector, mode) for mode in
             ("identical", "thought_varies", "timestamp_varies", "period_two", "period_three", "real_progress")]
    expected = {"identical": 4, "thought_varies": None, "timestamp_varies": None,
                "period_two": 6, "period_three": None, "real_progress": None}
    for case in cases:
        assert case["first_detection_cycle"] == expected[case["case"]], case
    result = {"evidence_level": "source_component_replay_with_event_doubles",
              "upstream": manifest, "cases": cases,
              "limitations": ["Not a full SDK integration or Nemotron rollout.",
                              "Synthetic event objects; upstream detector methods and thresholds unchanged.",
                              "No claim of novel vulnerability; only scoped comparator behavior."]}
    dump(here / "runs/stuck-detector-source/results.json", result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
