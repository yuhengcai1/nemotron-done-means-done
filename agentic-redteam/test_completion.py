import unittest

from campaign import completed_team_run


class CompletionTests(unittest.TestCase):
    def result(self, **changes):
        result = dict(passed=True, timed_out=False, exit_code=0, event_errors=[],
                      native_multiagent_observed=True, declared_status='done')
        result.update(changes)
        return result

    def test_correct_world_at_timeout_is_not_a_completed_comparison(self):
        self.assertFalse(completed_team_run(self.result(timed_out=True, declared_status=None)))

    def test_completed_native_workflow_is_required(self):
        self.assertTrue(completed_team_run(self.result()))
        self.assertFalse(completed_team_run(self.result(native_multiagent_observed=False)))
        self.assertFalse(completed_team_run(self.result(declared_status=None)))
        self.assertFalse(completed_team_run(self.result(exit_code=None, timed_out=None)))


if __name__ == '__main__':
    unittest.main()
