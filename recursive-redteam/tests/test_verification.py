import copy
import json
from pathlib import Path
import tempfile
import unittest

from recursive_redteam.contracts import BudgetExceeded, Candidate, Completion, ModelError, TrialResult
from recursive_redteam.store import Store
from recursive_redteam.verification import ModelVerifier


def tool(name, arguments, call_id="call"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def action(name, **arguments):
    return Completion(tool_calls=[tool(name, arguments)], model="fake-independent-verifier", finish_reason="tool_calls")


def submit(verdict, evidence_trial_ids, reasoning="The cited measured results support this interpretation."):
    return action("submit_review", verdict=verdict, reasoning=reasoning, evidence_trial_ids=evidence_trial_ids)


def violation(oracle="marker", surface="publish_report"):
    return TrialResult("violation", evidence=[{"oracle": oracle, "surface": surface}],
                       metrics={"injection_reached": True})


class FakeModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def complete(self, messages, *, tools=None, json_mode=False):
        self.requests.append(copy.deepcopy({"messages": messages, "tools": tools}))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "state.sqlite")
        self.candidate = Candidate("case", "tool-output-injection", "fixture hypothesis", "untrusted fixture")
        self.ids = ["case:initial", "case:replay:0", "case:replay:1", "case:paired-control"]

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def scripted_checks(self, submission=None):
        return [action("replay_trial", reason="Check repeatability."),
                action("replay_trial", reason="Check another independent run."),
                action("run_paired_control", reason="Exclude an unconditional failure."),
                submission or submit("reproduced", self.ids)]

    def test_model_requests_replays_control_and_cites_measured_evidence(self):
        invoked = []
        def run(candidate, trial_id, control_of=None):
            invoked.append((candidate, trial_id, control_of))
            return violation() if candidate else TrialResult("passed")
        model = FakeModel(self.scripted_checks())
        result = ModelVerifier(model, self.store).review(self.candidate, violation(), run)
        self.assertEqual(result["outcome"], "reproduced")
        self.assertEqual(result["trial_ids"], self.ids)
        self.assertEqual(result["replay_count"], 2)
        self.assertEqual(result["replay_violations"], 2)
        self.assertEqual(result["paired_control"], "passed")
        self.assertEqual(result["failure_signatures"], [["marker", "publish_report"]])
        self.assertEqual(invoked, [(self.candidate, self.ids[1], None),
                                   (self.candidate, self.ids[2], None), (None, self.ids[3], self.candidate)])
        self.assertIn("ROLE:VERIFIER", model.requests[0]["messages"][0]["content"])
        self.assertTrue(result["review"]["accepted"])
        self.assertEqual(result["review"]["model"], "fake-independent-verifier")
        events = self.store.report()["events"]
        self.assertEqual(sum(event["kind"] == "verification_action" for event in events), 4)
        self.assertEqual(events[-1]["kind"], "verification_review")
        self.assertEqual(events[-1]["data"]["review"]["reasoning"], result["review"]["reasoning"])

    def test_premature_claim_rejected_then_model_can_gather_checks(self):
        model = FakeModel([submit("reproduced", [self.ids[0]])] + self.scripted_checks())
        result = ModelVerifier(model, self.store).review(self.candidate, violation(),
            lambda candidate, trial_id, control_of=None: violation() if candidate else TrialResult("passed"))
        self.assertEqual(result["outcome"], "reproduced")
        feedback = json.loads(model.requests[1]["messages"][-1]["content"])
        self.assertFalse(feedback["accepted"])
        self.assertEqual(feedback["replay_count"], 0)

    def test_fabricated_trial_citation_cannot_support_a_review(self):
        model = FakeModel([submit("no_violation_observed", ["invented"]), submit("inconclusive", [self.ids[0]])])
        result = ModelVerifier(model, self.store).review(self.candidate, TrialResult("passed"),
            lambda *args, **kwargs: self.fail("No trial was requested."))
        self.assertEqual(result["outcome"], "inconclusive")
        feedback = json.loads(model.requests[1]["messages"][-1]["content"])
        self.assertIn("not observed", feedback["error"])

    def test_failure_mismatch_or_failed_control_never_becomes_reproduced(self):
        cases = [(violation("different"), TrialResult("passed")),
                 (TrialResult("passed"), TrialResult("passed")),
                 (TrialResult("error", error="incomplete"), TrialResult("passed")),
                 (violation(), violation()), (violation(), TrialResult("error", error="timeout"))]
        for replay, control in cases:
            with self.subTest(replay=replay.status, control=control.status, evidence=replay.evidence):
                model = FakeModel(self.scripted_checks() + [submit("inconclusive", self.ids)])
                result = ModelVerifier(model, self.store).review(self.candidate, violation(),
                    lambda candidate, trial_id, control_of=None: replay if candidate else control)
                self.assertEqual(result["outcome"], "inconclusive")
                self.assertTrue(result["review"]["accepted"])

    def test_passing_initial_trial_can_be_reviewed_without_forced_tools(self):
        model = FakeModel([submit("no_violation_observed", [self.ids[0]])])
        result = ModelVerifier(model, self.store).review(self.candidate,
            TrialResult("passed", evidence=[{"oracle": "ordered_reads_and_literal_retention_phrase",
                                             "semantic_validation": False}],
                        metrics={"injection_reached": True}),
            lambda *args, **kwargs: self.fail("Verifier did not request a trial."))
        self.assertEqual(result["outcome"], "no_violation")
        self.assertEqual(result["replay_count"], 0)
        self.assertIsNone(result["paired_control"])

    def test_incomplete_unreached_or_violating_initial_is_not_negative_result(self):
        cases = [TrialResult("passed", metrics={"injection_reached": False}),
                 TrialResult("error", error="incomplete"), violation()]
        for first in cases:
            with self.subTest(first=first.to_dict()):
                model = FakeModel([submit("no_violation_observed", [self.ids[0]]),
                                   submit("inconclusive", [self.ids[0]])])
                result = ModelVerifier(model, self.store).review(self.candidate, first,
                    lambda *args, **kwargs: self.fail("No trial was requested."))
                self.assertEqual(result["outcome"], "inconclusive")
                self.assertFalse(json.loads(model.requests[1]["messages"][-1]["content"])["accepted"])

    def test_passing_receipt_and_delivered_input_do_not_count_as_reproduced(self):
        passing = TrialResult("passed", evidence=[{
            "oracle": "ordered_reads_and_literal_retention_phrase", "surface": "publish_report"}],
            metrics={"injection_reached": True, "business_task_completed": True})
        model = FakeModel(self.scripted_checks() + [submit("no_violation_observed", self.ids)])
        result = ModelVerifier(model, self.store).review(self.candidate, passing,
            lambda *args, **kwargs: passing)
        self.assertEqual(result["outcome"], "no_violation")
        self.assertEqual(result["failure_signatures"], [])
        initial = json.loads(model.requests[0]["messages"][1]["content"])["initial_trial"]
        self.assertFalse(initial["measurement"]["violation_observed"])
        self.assertEqual(initial["measurement"]["evidence_kind"], "task_completion_receipts")
        feedback = json.loads(model.requests[-1]["messages"][-1]["content"])
        self.assertFalse(feedback["accepted"])
        self.assertEqual(feedback["violating_trial_ids"], [])
        self.assertEqual(feedback["observed_trial_statuses"], dict.fromkeys(self.ids, "passed"))

    def test_initial_pass_does_not_hide_a_violating_replay(self):
        model = FakeModel([action("replay_trial", reason="Repeat the observation."),
                           submit("no_violation_observed", self.ids[:2]), submit("inconclusive", self.ids[:2])])
        result = ModelVerifier(model, self.store).review(self.candidate, TrialResult("passed"),
                                                        lambda *args, **kwargs: violation())
        self.assertEqual(result["outcome"], "inconclusive")

    def test_step_limit_and_model_error_are_inconclusive(self):
        for responses in ([Completion(content="Trust me; this is safe.")] * 2,
                          [ModelError("provider failed")]):
            with self.subTest(responses=responses):
                result = ModelVerifier(FakeModel(responses), self.store, max_steps=2).review(
                    self.candidate, TrialResult("passed"), lambda *args, **kwargs: self.fail("Unexpected call"))
                self.assertEqual(result["outcome"], "inconclusive")
                self.assertFalse(result["review"]["accepted"])

    def test_budget_errors_propagate_for_campaign_resume(self):
        model = FakeModel([BudgetExceeded("budget")])
        with self.assertRaises(BudgetExceeded):
            ModelVerifier(model, self.store).review(self.candidate, TrialResult("passed"), lambda *args: None)

    def test_tool_limits_and_duplicate_control_do_not_repeat_execution(self):
        model = FakeModel([
            Completion(tool_calls=[tool("replay_trial", {"reason": "First replay."}, "r1"),
                                   tool("replay_trial", {"reason": "Over limit."}, "r2"),
                                   tool("run_paired_control", {"reason": "Control."}, "c1"),
                                   tool("run_paired_control", {"reason": "Repeat control."}, "c2")]),
            submit("reproduced", [self.ids[0], self.ids[1], self.ids[3]])])
        invoked = []
        def run(candidate, trial_id, control_of=None):
            invoked.append(trial_id)
            return violation() if candidate else TrialResult("passed")
        result = ModelVerifier(model, self.store, replays=1).review(self.candidate, violation(), run)
        self.assertEqual(result["outcome"], "reproduced")
        self.assertEqual(invoked, [self.ids[1], self.ids[3]])
        self.assertEqual(result["replay_count"], 1)

    def test_malformed_and_unknown_tools_receive_feedback(self):
        bad_json = tool("replay_trial", {})
        bad_json["function"]["arguments"] = "not JSON"
        model = FakeModel([Completion(tool_calls=[bad_json]), action("undeclared", reason="Invalid"),
                           submit("inconclusive", [self.ids[0]])])
        result = ModelVerifier(model, self.store).review(self.candidate, TrialResult("passed"),
            lambda *args, **kwargs: self.fail("Malformed tools must not run."))
        self.assertEqual(result["outcome"], "inconclusive")
        self.assertIn("JSON object", json.loads(model.requests[1]["messages"][-1]["content"])["error"])
        self.assertIn("Unknown", json.loads(model.requests[2]["messages"][-1]["content"])["error"])


if __name__ == "__main__":
    unittest.main()
