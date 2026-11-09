"""One Enter may recover a verified paste; uncertain submission is never retried."""
import os
import unittest
from unittest.mock import patch

from herdr_team_loop.errors import LoopError
from herdr_team_loop.runs import RunService
from test_assignment_redelivery import UnsentRuntime
import test_workflow as fixtures


class PendingRuntime(UnsentRuntime):
    def __init__(self):
        super().__init__()
        self.submissions = []
        self.on_submit = None
        self.submit_error = None
        self.submit_result = {"turn": 1, "status": None}

    def submit(self, terminal):
        self.submissions.append(terminal)
        if self.on_submit:
            self.on_submit(terminal)
        if self.submit_error:
            raise self.submit_error
        return self.submit_result


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.WorkflowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.runtime = PendingRuntime()
        self.service = RunService(self.store, self.runtime)
        environment = patch.dict(os.environ, {"HC_TERMINAL_ID": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.run = self.service.start(self.fixture.workspace["id"], self.fixture.requirement["id"],
                                      mode="review", repos=[str(self.fixture.repo)])
        self.service.assignments.deliver(self.run["coordinator_id"])
        self.service.assignments.claim(self.run["coordinator_id"])
        worker = self.service.dispatch(self.run["id"], self.run["coordinator_id"])
        self.assignment = self.service.assignments.deliver(worker["id"])
        self.terminal = self.runtime.terminals[self.assignment["terminal"]]
        self.terminal.update(status=None, turn=0)
        self.launches_before = list(self.runtime.launches)
        self.sends_before = list(self.runtime.sends)
        self.evidence = "Original terminal shows the initial task pasted at the input prompt, not submitted"

    def submit(self, **arguments):
        return self.service.assignments.submit_pending(self.assignment["id"],
                                                       arguments.get("evidence", self.evidence), arguments.get("observed_turn", 0))

    def reject(self, code, **arguments):
        with self.assertRaises(LoopError) as error:
            self.submit(**arguments)
        self.assertEqual(error.exception.code, code)
        self.assertEqual(self.runtime.submissions, [])

    def test_pending_task_gets_one_enter_with_durable_evidence_and_no_resend_or_launch(self):
        def inspect_intent(terminal):
            attempt = self.store.get("assignments", self.assignment["id"])["submission_attempt"]
            self.assertEqual(attempt["status"], "SENDING")
            self.assertEqual(attempt["evidence"], self.evidence)
            self.assertEqual(attempt["observed_turn"], 0)
            self.assertEqual(self.store.events(self.run["id"])[-1]["kind"], "submission_requested")
            with self.assertRaises(LoopError) as error:
                self.submit()
            self.assertEqual(error.exception.code, "submission_not_retryable")
        self.runtime.on_submit = inspect_intent
        result = self.submit()
        self.assertEqual(result["status"], "STARTING")
        self.assertTrue(result["first_message_sent"])
        self.assertEqual(result["submission_attempt"]["status"], "SENT")
        self.assertEqual(result["submission_attempt"]["turn"], 1)
        self.assertEqual(self.runtime.submissions, [self.assignment["terminal"]])
        self.assertEqual(self.runtime.sends, self.sends_before)
        self.assertEqual(self.runtime.launches, self.launches_before)
        with self.assertRaises(LoopError):
            self.submit()
        with self.assertRaises(LoopError):
            self.service.assignments.deliver(self.assignment["id"])
        event = self.store.events(self.run["id"])[-1]
        self.assertEqual(event["data"]["evidence"], self.evidence)
        self.assertEqual(event["data"]["observed_turn"], 0)
        self.assertEqual(event["data"]["turn"], 1)

    def test_claim_wins_over_lost_submission_response(self):
        self.runtime.on_submit = lambda terminal: self.service.assignments.claim(self.assignment["id"])
        self.runtime.submit_error = OSError("response lost")
        result = self.submit()
        self.assertEqual(result["status"], "RUNNING")
        self.assertEqual(result["submission_attempt"]["status"], "SENT")

    def test_success_preserves_concurrent_stop(self):
        self.runtime.on_submit = lambda terminal: self.store.update("assignments", self.assignment["id"], {"status": "STOPPED"})
        self.assertEqual(self.submit()["status"], "STOPPED")

    def test_uncertain_enter_is_not_receipt_even_when_text_delivery_was_confirmed(self):
        self.runtime.submit_error = OSError("response lost")
        with self.assertRaises(LoopError) as error:
            self.submit()
        self.assertEqual(error.exception.code, "submission_unknown")
        result = self.store.get("assignments", self.assignment["id"])
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertTrue(result["first_message_sent"])
        self.assertEqual(result["submission_attempt"]["status"], "UNKNOWN")
        self.service.assignments.reconcile(self.assignment["id"])
        with self.assertRaises(LoopError):
            self.submit()
        with self.assertRaises(LoopError):
            self.service.assignments.deliver(self.assignment["id"])
        self.assertEqual(len(self.runtime.submissions), 1)

    def test_invalid_submission_response_is_unknown(self):
        self.runtime.submit_result = {"turn": True}
        with self.assertRaises(LoopError) as error:
            self.submit()
        self.assertEqual(error.exception.code, "submission_unknown")
        self.assertEqual(self.store.get("assignments", self.assignment["id"])["submission_attempt"]["status"], "UNKNOWN")

    def test_evidence_and_turn_are_required_before_recording_intent(self):
        for evidence in ("", "  ", None):
            with self.subTest(evidence=evidence):
                self.reject("missing_evidence", evidence=evidence)
        for turn in (None, True, -1, 0.0, "0"):
            with self.subTest(turn=turn):
                self.reject("invalid_turn", observed_turn=turn)
        self.assertNotIn("submission_attempt", self.store.get("assignments", self.assignment["id"]))

    def test_delivery_turn_must_match_pending_evidence(self):
        self.reject("submission_not_retryable", observed_turn=1)

    def test_current_terminal_turn_must_still_match(self):
        for turn in (1, True, None, 0.0):
            with self.subTest(turn=turn):
                self.terminal["turn"] = turn
                self.reject("stale_turn")

    def test_unknown_claimed_unsent_or_unproven_tasks_are_not_submitted(self):
        for changes in ({"status": "UNKNOWN"}, {"claimed_at": 1}, {"first_message_sent": False},
                        {"initial_message": ""}, {"delivery_attempt": {"status": "UNKNOWN", "turn": 0}},
                        {"delivery_attempt": {"status": "SENT", "turn": True}}):
            with self.subTest(changes=changes):
                self.store.update("assignments", self.assignment["id"], changes)
                self.reject("submission_not_retryable")
                self.store.update("assignments", self.assignment["id"], {**self.assignment, "claimed_at": None})

    def test_busy_missing_or_nonlive_terminal_is_not_submitted(self):
        for status in ("blocked", "working", "unknown", {}, {"app": "cursor", "state": "blocked"},
                       {"app": "cursor", "state": None}):
            with self.subTest(status=status):
                self.terminal["status"] = status
                self.reject("terminal_not_ready")
        self.terminal.pop("status")
        self.reject("terminal_not_ready")
        self.terminal.update(status=None, state="finished")
        self.reject("terminal_not_ready")

    def test_native_idle_status_is_allowed(self):
        self.terminal["status"] = {"app": "cursor", "state": "idle"}
        self.assertEqual(self.submit()["submission_attempt"]["status"], "SENT")

    def test_original_terminal_labels_are_required(self):
        self.terminal["labels"]["assignment"] = "other-assignment"
        self.reject("terminal_mismatch")

    def test_stopped_run_and_exhausted_budget_reject_before_enter(self):
        self.store.update("runs", self.run["id"], {"status": "STOPPING"})
        self.reject("run_not_ready")
        self.store.update("runs", self.run["id"], {"status": "ACTIVE", "active_limit_seconds": 0})
        self.reject("budget_exhausted")

    def test_changed_input_rejects_before_enter(self):
        self.fixture.source.write_text("new input\n")
        self.reject("input_changed")

    def test_changed_phase_rejects_before_enter(self):
        self.store.update("runs", self.run["id"], {"phase": "test"})
        self.reject("stale_phase")

    def test_current_worker_survives_coordinator_takeover(self):
        self.store.update("runs", self.run["id"], {"coordinator_epoch": 2, "coordinator_id": "replacement"})
        self.assertEqual(self.submit()["submission_attempt"]["status"], "SENT")

    def test_revalidation_after_reconcile_rejects_new_claim_without_enter(self):
        reconcile = self.runtime.reconcile
        def claim_during_reconcile(assignment):
            result = reconcile(assignment)
            self.service.assignments.claim(assignment["id"])
            return result
        self.runtime.reconcile = claim_during_reconcile
        self.reject("submission_not_retryable")


if __name__ == "__main__":
    unittest.main()
