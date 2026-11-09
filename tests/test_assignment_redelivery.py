"""Known-unsent recovery never repeats an uncertain or received task."""
import os
import unittest
from unittest.mock import patch

from herdr_team_loop.errors import LoopError
from herdr_team_loop.runs import RunService
import test_workflow as fixtures


class UnsentRuntime(fixtures.FakeRuntime):
    def __init__(self):
        super().__init__()
        self.sends = []
        self.on_send = None
        self.response = {"turn": 0, "started": False}
        self.error = None

    def launch(self, assignment, profile, message, cwd):
        result = super().launch(assignment, profile, message, cwd)
        self.terminals[result["terminal"]].update(first_message_sent=False, first_message={"sent": False}, status="idle")
        return {**result, "first_message_sent": False, "observation": self.observe(result["terminal"])}

    def send(self, terminal, message):
        self.sends.append((terminal, message))
        if self.on_send:
            self.on_send(terminal)
        if self.error:
            raise self.error
        return self.response


class RedeliveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.WorkflowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.runtime = UnsentRuntime()
        self.service = RunService(self.store, self.runtime)
        self.environment = patch.dict(os.environ, {"HC_TERMINAL_ID": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.run = self.service.start(self.fixture.workspace["id"], self.fixture.requirement["id"],
                                      mode="review", repos=[str(self.fixture.repo)])
        self.assignment = self.store.get("assignments", self.run["coordinator_id"])

    def deliver(self):
        return self.service.assignments.deliver(self.assignment["id"])

    def test_known_unsent_delivers_same_task_once_even_without_started(self):
        result = self.deliver()
        self.assertTrue(result["first_message_sent"])
        self.assertEqual(result["delivery_attempt"]["status"], "SENT")
        self.assertEqual(self.runtime.sends, [(self.assignment["terminal"], self.assignment["initial_message"])])
        with self.assertRaises(LoopError):
            self.deliver()
        self.assertEqual(len(self.runtime.sends), 1)

    def test_legacy_known_unsent_snapshots_reconstructed_message(self):
        self.store.update("assignments", self.assignment["id"], {"initial_message": None})
        result = self.deliver()
        self.assertEqual(result["initial_message"], self.runtime.sends[0][1])

    def test_unknown_delivery_is_not_retryable(self):
        self.store.update("assignments", self.assignment["id"], {"runtime_observation": {}})
        with self.assertRaises(LoopError):
            self.deliver()
        self.assertEqual(self.runtime.sends, [])

    def test_concurrent_request_is_rejected_before_second_send(self):
        errors = []
        def during_send(terminal):
            try:
                self.deliver()
            except LoopError as error:
                errors.append(error.code)
        self.runtime.on_send = during_send
        self.deliver()
        self.assertEqual(errors, ["delivery_not_retryable"])
        self.assertEqual(len(self.runtime.sends), 1)

    def test_failure_is_unknown_and_reconcile_cannot_enable_retry(self):
        self.runtime.error = OSError("lost response")
        with self.assertRaises(LoopError) as error:
            self.deliver()
        self.assertEqual(error.exception.code, "delivery_unknown")
        self.assertEqual(self.store.get("assignments", self.assignment["id"])["status"], "UNKNOWN")
        self.service.assignments.reconcile(self.assignment["id"])
        with self.assertRaises(LoopError):
            self.deliver()
        self.assertEqual(len(self.runtime.sends), 1)

    def test_invalid_send_result_is_unknown(self):
        self.runtime.response = {"turn": True}
        with self.assertRaises(LoopError) as error:
            self.deliver()
        self.assertEqual(error.exception.code, "delivery_unknown")
        self.assertFalse(self.store.get("assignments", self.assignment["id"])["first_message_sent"])

    def test_claim_during_send_wins_over_lost_response(self):
        def receive(terminal):
            with patch.dict(os.environ, {"HC_TERMINAL_ID": terminal}):
                self.service.assignments.claim(self.assignment["id"])
        self.runtime.on_send = receive
        self.runtime.error = OSError("response lost after receipt")
        self.assertEqual(self.deliver()["status"], "RUNNING")
        self.assertEqual(len(self.runtime.sends), 1)

    def test_late_success_preserves_concurrent_stop(self):
        self.runtime.on_send = lambda terminal: self.store.update("assignments", self.assignment["id"], {"status": "STOPPED"})
        self.assertEqual(self.deliver()["status"], "STOPPED")

    def test_stopping_run_and_blocked_terminal_are_not_sent_to(self):
        self.runtime.terminals[self.assignment["terminal"]]["status"] = "blocked"
        with self.assertRaises(LoopError):
            self.deliver()
        self.runtime.terminals[self.assignment["terminal"]]["status"] = "idle"
        self.store.update("runs", self.run["id"], {"status": "STOPPING"})
        with self.assertRaises(LoopError):
            self.deliver()
        self.assertEqual(self.runtime.sends, [])

    def test_native_status_object_accepts_idle(self):
        self.runtime.terminals[self.assignment["terminal"]]["status"] = {"app": "droid", "state": "idle"}
        self.assertTrue(self.deliver()["first_message_sent"])

    def test_native_status_object_accepts_done(self):
        self.runtime.terminals[self.assignment["terminal"]]["status"] = {"app": "droid", "state": "done"}
        self.assertTrue(self.deliver()["first_message_sent"])

    def test_active_unknown_and_blocked_statuses_are_rejected(self):
        for status in ("blocked", "working", "starting", None, {}, {"app": "droid", "state": "blocked"},
                       {"app": "droid", "state": "working"}, {"app": "droid", "state": "unknown"}):
            with self.subTest(status=status):
                self.runtime.terminals[self.assignment["terminal"]]["status"] = status
                with self.assertRaises(LoopError) as error:
                    self.deliver()
                self.assertEqual(error.exception.code, "terminal_not_ready")
        self.assertEqual(self.runtime.sends, [])


if __name__ == "__main__":
    unittest.main()
