import unittest
from environment import JobService, task_for


class ServiceTests(unittest.TestCase):
    def test_all_stalled_conditions_have_a_repair_path(self):
        for condition in ("stable_observation", "timestamp_observation", "circular_diagnostics"):
            service = JobService("recovery", condition, 1)
            for _ in range(4):
                for op in ("status", "logs", "config"):
                    service.call(op, "job-1")
            self.assertTrue(all(not c["progress"] for c in service.calls))
            service.call("repair", "job-1")
            self.assertEqual(service.call("status", "job-1")["status"], "completed")

    def test_timestamp_does_not_change_world_state(self):
        service = JobService("mechanism", "timestamp_varies", 1)
        a, b = service.call("status", "job-1"), service.call("status", "job-1")
        self.assertNotEqual(a.pop("observed_at"), b.pop("observed_at"))
        self.assertEqual(a, b)
        self.assertFalse(any(c["progress"] for c in service.calls))

    def test_healthy_polling_reaches_completion(self):
        service = JobService("recovery", "progress_control", 1)
        for _ in range(6):
            result = service.call("status", "job-1")
        self.assertEqual(result["status"], "completed")
        self.assertTrue(all(c["progress"] for c in service.calls))

    def test_recovery_prompt_is_identical_across_conditions(self):
        self.assertEqual(task_for("recovery", "stable_observation", 1),
                         task_for("recovery", "circular_diagnostics", 1))


if __name__ == "__main__":
    unittest.main()
