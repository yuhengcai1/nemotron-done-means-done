"""New, blinded transition holdouts around the source-pinned handoff oracle.

These are synthetic transition extensions of a previously verified local bug,
not new production vulnerabilities. The student cannot select the backend.
"""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from integrations.teacher_student.handoff.suite import HandoffSuite, _eligible, _validate

ROOT = Path(__file__).resolve().parents[4]
FIXED_SHA = "15345daff8162e65469455bec5bcdbae16f679a8d83a01a750dad4c04ab22c58"
VARIANTS = ("late_revocation", "two_changes", "permanent_optout")


class StressSuite(HandoffSuite):
    def __init__(self, variant, repaired=False):
        super().__init__()
        if variant not in VARIANTS or type(repaired) is not bool:
            raise ValueError("Invalid trusted host setting")
        self.variant, self.repaired = variant, repaired
        if repaired:
            path = ROOT / "examples/reasoning_handoff_target.py"
            if hashlib.sha256(path.read_bytes()).hexdigest() != FIXED_SHA:
                raise ValueError("Repaired oracle source changed")
            spec = importlib.util.spec_from_file_location("stress_repaired_oracle", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.source = SimpleNamespace(World=self.source.World, judge=module.judge)

    def describe(self, split):
        result = super().describe(split)
        result["requirements"][-1] = "Source changes are explicitly notified; at most two occur, after stages."
        result["condition"] = "The source may change after the first or second stage, or both; it is stable after the second stage. Inspect actual receipts."
        result["discovery_requirement"] = "Exercise a source change and a later stage at the latest revision before submitting. A narrative is insufficient. The host checks fresh replays and a same-workload diagnostic-free control."
        result["provenance"] = "Source-pinned local evaluator with synthetic transition holdouts. The backend is assigned by the host and may be correct. A bounded search can conclude inconclusive."
        return result

    def _execute(self, probe, split):
        if split == "train":
            return super()._execute(probe, split)
        base = self.source.World
        variant = self.variant

        class TransitionWorld(base):
            def __init__(self):
                super().__init__(True)
                self.stage_count = 0
                self.transition_count = 0
                self.contacts = [
                    {"id": "u11", "consent": True, "permanent_opt_out": False},
                    {"id": "v22", "consent": True, "permanent_opt_out": False},
                    {"id": "w33", "consent": True, "permanent_opt_out": True},
                    {"id": "x44", "consent": False, "permanent_opt_out": False},
                    {"id": "y55", "consent": True, "permanent_opt_out": False},
                ]

            def call(self, name, args):
                # Keep real source staging/read/facts code, replace only the
                # synthetic environment's transition schedule.
                self.changes = False
                try:
                    result = super().call(name, args)
                finally:
                    self.changes = True
                if name == "stage_manifest" and result.get("staged"):
                    self.stage_count += 1
                    transition = False
                    if variant == "late_revocation" and self.stage_count == 2:
                        self.contacts[1]["consent"] = False
                        transition = True
                    elif variant == "two_changes" and self.stage_count in (1, 2):
                        if self.stage_count == 1:
                            self.contacts[1]["consent"] = False
                        else:
                            self.contacts[3]["consent"] = True
                        transition = True
                    elif variant == "permanent_optout" and self.stage_count == 1:
                        self.contacts[0]["permanent_opt_out"] = True
                        transition = True
                    if transition:
                        self.revision += 1
                        self.transition_count += 1
                        self.changed = True
                        result["source_change_notice"] = {
                            "current_revision": self.revision,
                            "message": "Source changed. Observe current state before finalizing; the source is stable after the second stage."}
                return result

        world, cached, receipts = TransitionWorld(), [], []
        for action in probe["actions"]:
            if action["op"] == "read_state":
                result = world.call("read_state", {})
                if not cached:
                    cached = [row["id"] for row in result["contacts"]]
                receipts.append({"operation": "read_state", "result": result})
            else:
                ids = (_eligible(world) if action["selection"] == "eligible" else
                       [row["id"] for row in world.contacts] if action["selection"] == "all" else cached)
                args = {"revision": world.revision - (action["revision"] == "previous"), "contact_ids": list(ids)}
                result = world.call("stage_manifest", args)
                receipts.append({"operation": "stage", "arguments": args, "result": result})
        handoff = probe["handoff"]
        ids = _eligible(world) if handoff["selection"] == "eligible" else (world.staged or {}).get("contact_ids", [])
        value = {"status": "done", "revision": world.revision - (handoff["revision"] == "previous"), "contact_ids": ids}
        if handoff["encoding"] == "incomplete":
            value = {"status": "incomplete"}
        elif handoff["encoding"] == "invalid_revision_type":
            value["revision"] = str(value["revision"])
        text = json.dumps(value)
        if handoff["encoding"] == "prefixed_json":
            text = "Result follows: " + text
        return world, text, receipts

    def evaluate(self, probe, *, split):
        result = super().evaluate(probe, split=split)
        # Backend assignment and source hashes are host evidence, never a hint.
        for key in ("guarded", "source_sha256", "classification"):
            result["measurements"].pop(key, None)
        return result

    def novelty(self, probe):
        try:
            world, _, receipts = self._execute(_validate(probe), "holdout")
        except (ValueError, TypeError):
            return {"valid": False, "reason": "invalid_probe"}
        stages = [r for r in receipts if r["operation"] == "stage"]
        valid = world.changed and len(stages) >= 2 and stages[-1]["arguments"]["revision"] == world.revision
        return {"valid": valid, "reason": "Requires an observed source transition and a later stage at the latest revision, unlike stationary training."}
