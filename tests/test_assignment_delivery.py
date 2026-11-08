"""Delivery races using native-like observations that forget successful sends."""
from copy import deepcopy
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from herdr_team_loop.errors import LoopError
from herdr_team_loop.runs import RunService
import test_workflow as fixtures


class NativeLikeRuntime(fixtures.FakeRuntime):
    """Mirror hc get: after successful delivery, no sent=true field is retained."""

    def __init__(self):
        super().__init__()
        self.on_delivery = None
        self.lose_response = False

    def launch(self, assignment, profile, message, cwd):
        result = super().launch(assignment, profile, message, cwd)
        if self.on_delivery:
            self.on_delivery(assignment, result)
        if self.lose_response:
            raise LoopError("delivery_unknown", "Launch response lost after delivery")
        return result

    def observe(self, terminal):
        result = super().observe(terminal)
        result.pop("first_message_sent", None)
        return result

    def reconcile(self, assignment):
        matches = [(terminal, state) for terminal, state in self.terminals.items()
                   if state["labels"].get("assignment") == assignment["id"]]
        if not matches:
            return None
        terminal, _ = matches[0]
        return {"terminal": terminal, "first_message_sent": False, "delivery_known": False,
                "observation": deepcopy(self.observe(terminal))}


class AssignmentDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.WorkflowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.runtime = NativeLikeRuntime()
        self.fixture.runtime = self.runtime
        self.service = RunService(self.fixture.store, self.runtime)
        self.fixture.service = self.service
        self.store = self.fixture.store
        self.environment = patch.dict(os.environ, {"HC_TERMINAL_ID": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def start(self):
        return self.service.start(self.fixture.workspace["id"], self.fixture.requirement["id"],
                                  mode="review", repos=[str(self.fixture.repo)])

    def start_with_lost_response(self):
        self.runtime.lose_response = True
        with self.assertRaises(LoopError) as error:
            self.start()
        self.assertEqual(error.exception.code, "delivery_unknown")
        assignment = self.store.list("assignments")[0]
        self.assertEqual(assignment["status"], "UNKNOWN")
        return assignment

    def test_worker_receipt_can_claim_before_launch_response_is_bound(self):
        claimed = []
        def receive(assignment, result):
            self.assertEqual(self.store.get("assignments", assignment["id"])["status"], "DISPATCHING")
            with patch.dict(os.environ, {"HC_TERMINAL_ID": result["terminal"]}):
                claimed.append(self.service.assignments.claim(assignment["id"]))
        self.runtime.on_delivery = receive
        run = self.start()
        assignment = self.store.get("assignments", run["coordinator_id"])
        self.assertEqual(claimed[0]["status"], "RUNNING")
        self.assertEqual(assignment["status"], "RUNNING")
        self.assertTrue(assignment["first_message_sent"])
        self.assertEqual(len(self.runtime.launches), 1)

    def test_lost_launch_response_does_not_overwrite_receipt_acknowledgement(self):
        def receive(assignment, result):
            with patch.dict(os.environ, {"HC_TERMINAL_ID": result["terminal"]}):
                self.service.assignments.claim(assignment["id"])
        self.runtime.on_delivery = receive
        self.runtime.lose_response = True
        run = self.start()
        assignment = self.store.get("assignments", run["coordinator_id"])
        self.assertEqual(assignment["status"], "RUNNING")
        self.assertTrue(assignment["claimed_at"])
        self.assertEqual(len(self.runtime.launches), 1)

    def test_reconcile_does_not_invent_delivery_and_explicit_confirmation_recovers(self):
        assignment = self.start_with_lost_response()
        result = self.service.assignments.reconcile(assignment["id"])
        self.assertEqual(result["assignment"]["status"], "STARTING")
        self.assertFalse(result["assignment"]["first_message_sent"])
        with self.assertRaises(LoopError) as error:
            self.service.assignments.claim(assignment["id"])
        self.assertEqual(error.exception.code, "delivery_unconfirmed")
        self.service.assignments.confirm_delivery(assignment["id"], "Original terminal shows the exact assignment and task")
        self.assertEqual(self.service.assignments.claim(assignment["id"])["status"], "RUNNING")
        self.assertEqual(len(self.runtime.launches), 1)

    def test_receiving_terminal_can_recover_lost_response_without_resending(self):
        assignment = self.start_with_lost_response()
        terminal = self.runtime.reconcile(assignment)["terminal"]
        with patch.dict(os.environ, {"HC_TERMINAL_ID": terminal}):
            result = self.service.assignments.claim(assignment["id"])
        self.assertEqual(result["status"], "RUNNING")
        self.assertEqual(result["terminal"], terminal)
        self.assertEqual(len(self.runtime.launches), 1)

    def test_wrong_terminal_cannot_acknowledge_someone_elses_delivery(self):
        assignment = self.start_with_lost_response()
        with patch.dict(os.environ, {"HC_TERMINAL_ID": "unrelated-terminal"}), self.assertRaises(LoopError) as error:
            self.service.assignments.claim(assignment["id"])
        self.assertEqual(error.exception.code, "delivery_unconfirmed")
        current = self.store.get("assignments", assignment["id"])
        self.assertEqual(current["status"], "UNKNOWN")
        self.assertFalse(current["first_message_sent"])

    def test_late_success_cannot_resurrect_a_stopped_assignment(self):
        def stop_during_launch(assignment, result):
            self.store.update("assignments", assignment["id"], {"status": "STOPPED"})
        self.runtime.on_delivery = stop_during_launch
        run = self.start()
        self.assertEqual(self.store.get("assignments", run["coordinator_id"])["status"], "STOPPED")

    def test_late_launch_error_cannot_resurrect_a_stopped_assignment(self):
        def stop_during_launch(assignment, result):
            self.store.update("assignments", assignment["id"], {"status": "STOPPED"})
        self.runtime.on_delivery = stop_during_launch
        self.runtime.lose_response = True
        try:
            self.start()
        except LoopError as error:
            self.assertEqual(error.code, "delivery_unknown")
        assignment = self.store.list("assignments")[0]
        self.assertEqual(assignment["status"], "STOPPED")

    def test_explicit_confirmation_requires_both_a_terminal_and_evidence(self):
        assignment = self.start_with_lost_response()
        with self.assertRaises(LoopError):
            self.service.assignments.confirm_delivery(assignment["id"], "")
        with self.assertRaises(LoopError):
            self.service.assignments.confirm_delivery(assignment["id"], "Saw original task")

    def attach(self, observation):
        self.runtime.terminals["existing"] = {"state": "live", "labels": {}, **observation}
        return self.service.start(self.fixture.workspace["id"], self.fixture.requirement["id"],
                                  mode="review", repos=[str(self.fixture.repo)], attach_terminal="existing")

    def test_attach_rejects_a_different_foreground_agent(self):
        with self.assertRaises(LoopError) as error:
            self.attach({"argv": [sys.executable, "-c", "pass"], "cwd": str(self.fixture.repo),
                         "foreground": {"argv": ["/bin/sh"], "cwd": str(self.fixture.repo)}})
        self.assertEqual(error.exception.code, "terminal_profile_mismatch")

    def test_attach_rejects_a_different_actual_working_directory(self):
        with self.assertRaises(LoopError) as error:
            self.attach({"argv": [sys.executable, "-c", "pass"], "cwd": str(self.fixture.repo),
                         "foreground": {"argv": [sys.executable, "-c", "pass"], "cwd": str(self.fixture.root)}})
        self.assertEqual(error.exception.code, "terminal_workspace_mismatch")

    def test_attach_rejects_a_shell_command_without_process_identity(self):
        with self.assertRaises(LoopError) as error:
            self.attach({"argv": ["/bin/sh", "-c", sys.executable + " -c pass"],
                         "cwd": str(self.fixture.repo)})
        self.assertEqual(error.exception.code, "terminal_profile_mismatch")

    def test_attach_requires_the_configured_startup_arguments(self):
        with self.assertRaises(LoopError) as error:
            self.attach({"argv": [sys.executable, "-c", "different program"], "cwd": str(self.fixture.repo)})
        self.assertEqual(error.exception.code, "terminal_profile_mismatch")

    def test_attach_accepts_verified_matching_foreground_and_directory(self):
        run = self.attach({"argv": ["/bin/sh", "-c", "launcher"], "cwd": str(self.fixture.root),
                           "foreground": {"argv": [str(Path(sys.executable).resolve()), "-c", "pass"],
                                          "cwd": str(self.fixture.repo)}})
        coordinator = self.store.get("assignments", run["coordinator_id"])
        self.assertEqual(coordinator["status"], "RUNNING")
        self.assertTrue(coordinator["attached"])
        self.assertEqual(self.runtime.launches, [])


if __name__ == "__main__":
    unittest.main()
