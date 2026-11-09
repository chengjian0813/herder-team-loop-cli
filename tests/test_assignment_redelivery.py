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

    def deliver(self, **readiness):
        return self.service.assignments.deliver(self.assignment["id"], **readiness)

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

    def test_null_status_requires_manual_evidence_and_current_turn_and_records_it_before_send(self):
        self.runtime.terminals[self.assignment["terminal"]].update(status=None, turn=0)
        readiness = {"ready_evidence": "Observed the original Cursor prompt ready for input", "ready_turn": 0}
        def check_pending_delivery(terminal):
            attempt = self.store.get("assignments", self.assignment["id"])["delivery_attempt"]
            self.assertEqual(attempt["status"], "SENDING")
            self.assertEqual({key: attempt[key] for key in readiness}, readiness)
            event = self.store.events(self.run["id"])[-1]
            self.assertEqual(event["kind"], "delivery_requested")
            self.assertEqual({key: event["data"][key] for key in readiness}, readiness)
            with self.assertRaises(LoopError) as error:
                self.deliver(**readiness)
            self.assertEqual(error.exception.code, "delivery_not_retryable")
        self.runtime.on_send = check_pending_delivery
        result = self.deliver(**readiness)
        self.assertTrue(result["first_message_sent"])
        self.assertEqual({key: result["delivery_attempt"][key] for key in readiness}, readiness)
        self.assertEqual(len(self.runtime.sends), 1)
        with self.assertRaises(LoopError):
            self.deliver(**readiness)

    def test_null_status_rejects_missing_invalid_or_stale_manual_readiness(self):
        self.runtime.terminals[self.assignment["terminal"]].update(status=None, turn=2)
        for readiness in ({}, {"ready_evidence": "Observed prompt"}, {"ready_turn": 2},
                          {"ready_evidence": "  ", "ready_turn": 2},
                          {"ready_evidence": "Observed prompt", "ready_turn": True},
                          {"ready_evidence": "Observed prompt", "ready_turn": -1},
                          {"ready_evidence": "Observed prompt", "ready_turn": 1},
                          {"ready_evidence": "Observed prompt", "ready_turn": 2.0}):
            with self.subTest(readiness=readiness):
                with self.assertRaises(LoopError) as error:
                    self.deliver(**readiness)
                self.assertEqual(error.exception.code, "terminal_not_ready")
                self.assertNotIn("delivery_attempt", self.store.get("assignments", self.assignment["id"]))
        self.assertEqual(self.runtime.sends, [])

    def test_manual_readiness_rejects_invalid_observed_turn(self):
        terminal = self.runtime.terminals[self.assignment["terminal"]]
        terminal["status"] = None
        for turn in (True, None, "0", 0.0):
            with self.subTest(turn=turn):
                terminal["turn"] = turn
                with self.assertRaises(LoopError) as error:
                    self.deliver(ready_evidence="Observed prompt", ready_turn=0)
                self.assertEqual(error.exception.code, "terminal_not_ready")
        self.assertEqual(self.runtime.sends, [])

    def test_manual_readiness_cannot_override_other_statuses_or_missing_status(self):
        terminal = self.runtime.terminals[self.assignment["terminal"]]
        terminal["turn"] = 0
        for status in ("blocked", "working", "starting", "error", {},
                       {"app": "cursor", "state": None}, {"app": "cursor", "state": "working"},
                       {"app": "cursor", "state": "blocked"}):
            with self.subTest(status=status):
                terminal["status"] = status
                with self.assertRaises(LoopError) as error:
                    self.deliver(ready_evidence="Observed prompt", ready_turn=0)
                self.assertEqual(error.exception.code, "terminal_not_ready")
        terminal.pop("status")
        with self.assertRaises(LoopError):
            self.deliver(ready_evidence="Observed prompt", ready_turn=0)
        self.assertEqual(self.runtime.sends, [])

    def test_manual_readiness_cannot_override_finished_terminal_or_sent_task(self):
        terminal = self.runtime.terminals[self.assignment["terminal"]]
        terminal.update(status=None, turn=0, state="finished")
        with self.assertRaises(LoopError):
            self.deliver(ready_evidence="Observed prompt", ready_turn=0)
        terminal.update(state="live", first_message={"sent": True})
        with self.assertRaises(LoopError):
            self.deliver(ready_evidence="Observed prompt", ready_turn=0)
        self.assertEqual(self.runtime.sends, [])

    def test_uncertain_manual_delivery_records_evidence_and_cannot_be_retried(self):
        self.runtime.terminals[self.assignment["terminal"]].update(status=None, turn=0)
        self.runtime.error = OSError("lost response")
        readiness = {"ready_evidence": "Observed prompt", "ready_turn": 0}
        with self.assertRaises(LoopError) as error:
            self.deliver(**readiness)
        self.assertEqual(error.exception.code, "delivery_unknown")
        assignment = self.store.get("assignments", self.assignment["id"])
        self.assertEqual(assignment["status"], "UNKNOWN")
        self.assertEqual(assignment["delivery_attempt"]["ready_turn"], 0)
        self.assertEqual(self.store.events(self.run["id"])[-1]["data"]["ready_evidence"], readiness["ready_evidence"])
        self.service.assignments.reconcile(self.assignment["id"])
        with self.assertRaises(LoopError):
            self.deliver(**readiness)
        self.assertEqual(len(self.runtime.sends), 1)


if __name__ == "__main__":
    unittest.main()
