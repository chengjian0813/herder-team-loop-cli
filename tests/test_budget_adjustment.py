"""Explicit budget changes preserve execution history and all other run gates."""
import unittest
from unittest.mock import patch

from herdr_team_loop.errors import LoopError
import test_workflow as fixtures


class BudgetAdjustmentTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.WorkflowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.service = self.fixture.service
        self.fixture.start(active_minutes=20)
        self.run_id = self.fixture.run_id
        self.store.update("runs", self.run_id, {"active_since": 1500.0, "active_seconds": 250.0})

    def extend(self, minutes=60, reason="Continue the recorded local verification", source="Explicit user reply"):
        with patch("herdr_team_loop.runs.time.time", return_value=2000.0):
            return self.service.extend_budget(self.run_id, minutes, reason, source)

    def assert_rejected(self, code, **kwargs):
        before = self.store.get("runs", self.run_id)
        events = self.store.events(self.run_id)
        with self.assertRaises(LoopError) as caught:
            self.extend(**kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.store.get("runs", self.run_id), before)
        self.assertEqual(self.store.events(self.run_id), events)

    def test_extension_changes_only_total_time_and_records_authorization(self):
        self.store.update("runs", self.run_id, {"status": "BLOCKED", "blocked_reason": "waiting for a tool",
                                               "phase": "test", "round": 1, "deadline": 2400.0,
                                               "failures": {"bug-example": {"count": 1}}})
        before = self.store.get("runs", self.run_id)
        assignments = self.store.list("assignments", run_id=self.run_id)
        launches = list(self.fixture.runtime.launches)
        result = self.extend()
        expected = {**before, "active_limit_seconds": 3600.0}
        for key in ("revision", "updated_at"):
            expected[key] = result[key]
        self.assertEqual(result, expected)
        self.assertEqual(self.store.list("assignments", run_id=self.run_id), assignments)
        self.assertEqual(self.fixture.runtime.launches, launches)
        event = self.store.events(self.run_id)[-1]
        self.assertEqual(event["kind"], "budget_extended")
        self.assertEqual(event["data"], {
            "previous_active_limit_seconds": 1200, "active_limit_seconds": 3600.0,
            "active_elapsed_seconds": 750.0, "reason": "Continue the recorded local verification",
            "authorization_source": "Explicit user reply"})

    def test_expired_active_budget_can_be_extended_without_resetting_used_time(self):
        self.store.update("runs", self.run_id, {"active_since": 500.0, "active_seconds": 50.0})
        result = self.extend(minutes=30)
        self.assertEqual(result["active_limit_seconds"], 1800)
        self.assertEqual(result["active_since"], 500)
        self.assertEqual(result["active_seconds"], 50)
        self.assertEqual(self.store.events(self.run_id)[-1]["data"]["active_elapsed_seconds"], 1550)

    def test_paused_run_retains_pause_and_accumulated_time(self):
        self.store.update("runs", self.run_id, {"status": "WAITING_DECISION", "active_since": None,
                                               "active_seconds": 400.0})
        result = self.extend()
        self.assertEqual(result["status"], "WAITING_DECISION")
        self.assertIsNone(result["active_since"])
        self.assertEqual(result["active_seconds"], 400)

    def test_invalid_numeric_budgets_are_rejected_without_mutation(self):
        for value in (True, False, None, "60", 0, -1, float("nan"), float("inf"), float("-inf"), 1e308, 10 ** 1000):
            with self.subTest(value=value):
                self.assert_rejected("invalid_budget", minutes=value)

    def test_new_limit_must_exceed_existing_limit_and_actual_elapsed_time(self):
        for value in (1, 20):
            with self.subTest(value=value):
                self.assert_rejected("invalid_budget", minutes=value)
        self.store.update("runs", self.run_id, {"active_since": 0.0, "active_seconds": 400.0})
        for value in (30, 40):
            with self.subTest(value=value):
                self.assert_rejected("invalid_budget", minutes=value)

    def test_reason_and_authorization_are_required(self):
        for value in ("", " \n", None, True):
            with self.subTest(value=value):
                self.assert_rejected("reason_required", reason=value)
                self.assert_rejected("authorization_required", source=value)

    def test_ended_and_stopping_runs_are_rejected(self):
        for status in ("DONE", "STOPPED", "STOPPING"):
            with self.subTest(status=status):
                self.store.update("runs", self.run_id, {"status": status})
                self.assert_rejected("cannot_extend_budget")

    def test_expired_absolute_deadline_cannot_be_extended(self):
        for deadline in (1999.0, 2000.0):
            with self.subTest(deadline=deadline):
                self.store.update("runs", self.run_id, {"deadline": deadline})
                self.assert_rejected("budget_exhausted")

    def test_repeated_same_total_is_rejected_without_another_event(self):
        self.extend()
        self.assert_rejected("invalid_budget")

    def test_event_failure_rolls_back_the_limit_update(self):
        before = self.store.get("runs", self.run_id)
        events = self.store.events(self.run_id)
        with patch.object(self.store, "event", side_effect=RuntimeError("event write failed")):
            with self.assertRaises(RuntimeError):
                self.extend()
        self.assertEqual(self.store.get("runs", self.run_id), before)
        self.assertEqual(self.store.events(self.run_id), events)


if __name__ == "__main__":
    unittest.main()
