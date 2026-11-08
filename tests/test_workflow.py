"""Real SQLite/Git workflow contracts with an entirely inert terminal runtime."""
from copy import deepcopy
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from herdr_team_loop.errors import LoopError
from herdr_team_loop.runs import RunService
from herdr_team_loop.storage import Store
from herdr_team_loop.workspace import WorkspaceService


class FakeRuntime:
    """Model uncertain delivery and terminal lifecycle without spawning processes."""

    def __init__(self):
        self.terminals = {}
        self.launches = []
        self.terminated = []
        self.waits = []
        self.fail_next_launch = None

    def launch(self, assignment, profile, message, cwd):
        self.launches.append(assignment["id"])
        failure, self.fail_next_launch = self.fail_next_launch, None
        if failure == "before":
            raise OSError("Launch connection failed before any terminal was observed")
        terminal = "fake:" + str(len(self.launches))
        self.terminals[terminal] = {"state": "live", "labels": {"assignment": assignment["id"]},
                                    "first_message_sent": True, "message": message, "cwd": cwd, "agent": profile["id"]}
        if failure == "after":
            raise OSError("Terminal started but the response was lost")
        return {"terminal": terminal, "first_message_sent": True, "observation": self.observe(terminal)}

    def observe(self, terminal):
        if terminal not in self.terminals:
            raise LoopError("terminal_unknown", "No terminal was observed")
        return deepcopy(self.terminals[terminal])

    def reconcile(self, assignment):
        matches = [(terminal, state) for terminal, state in self.terminals.items()
                   if state["labels"].get("assignment") == assignment["id"]]
        if not matches:
            return None
        terminal, state = matches[0]
        return {"terminal": terminal, "first_message_sent": state["first_message_sent"], "observation": deepcopy(state)}

    def finish(self, terminal):
        self.terminals[terminal]["state"] = "finished"

    def terminate(self, terminal):
        self.terminated.append(terminal)
        self.finish(terminal)

    def wait(self, terminal, timeout=30):
        self.waits.append((terminal, timeout))
        return self.observe(terminal)

    def read(self, terminal, **kwargs):
        return {"text": self.observe(terminal)["message"]}

    def attach(self, terminal):
        self.observe(terminal)
        return 0


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / "project"
        self.repo.mkdir()
        self.git("init", "-b", "master")
        self.git("config", "user.name", "Workflow fixture")
        self.git("config", "user.email", "workflow@example.invalid")
        self.source = self.repo / "behavior.txt"
        self.source.write_text("initial\n")
        self.git("add", "behavior.txt")
        self.git("commit", "-m", "initial fixture")
        self.store = Store(self.root / "state")
        self.addCleanup(self.store.close)
        workspaces = WorkspaceService(self.store)
        for profile_id in ("fixture-primary", "fixture-fallback"):
            workspaces.register_profile({"id": profile_id, "command": [sys.executable, "-c", "pass"]})
        roles = {role: {"primary": "fixture-primary", "fallbacks": ["fixture-fallback"]}
                 for role in ("coordinator", "developer", "reviewer", "tester")}
        self.workspace = workspaces.initialize(str(self.repo), roles)
        self.requirement = workspaces.create_requirement(self.workspace["id"], "Fixture delivery", "Implement the requested behavior and verify all required Bugs.")
        self.runtime = FakeRuntime()
        self.service = RunService(self.store, self.runtime)
        self.run_id = self.actor = None

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True).stdout

    def state(self):
        return self.store.get("runs", self.run_id)

    def start(self, mode="e2e", **kwargs):
        options = {"mode": mode, "repos": [str(self.repo)], "repair_policy": "guarded",
                   "authorization": {"source": "test authorization", "code_fix": True, "deploy": True}, **kwargs}
        run = self.service.start(self.workspace["id"], self.requirement["id"], **options)
        self.run_id, self.actor = run["id"], run["coordinator_id"]
        self.service.assignments.claim(self.actor)
        return self.state()

    def environment(self, **changes):
        run = self.state()
        evidence = {"environment": run["environment"], "input_fingerprint": run["fingerprint"]["digest"],
                    "verified_at": "2026-11-08T12:00:00Z", "evidence": ["Fixture environment checked"]}
        if run["environment"] != "local":
            evidence.update(project="fixture-sc", config_version="configuration-1", test_objects=["fixture-shop"],
                            applications=[{"name": "fixture-app", "version": "build-1", "source_mapping": run["fingerprint"]["digest"],
                                           "deployment_evidence": "fixture deployment manifest"}])
        return self.service.confirm_environment(self.run_id, self.actor, {**evidence, **changes})

    def dispatch(self, bug_ids=None, *, claim=True, **kwargs):
        assignment = self.service.dispatch(self.run_id, self.actor, bug_ids=bug_ids, **kwargs)
        if claim:
            assignment = self.service.assignments.claim(assignment["id"])
        return assignment

    def body(self, assignment, result="pass", bug_ids=None):
        return {"result": result, "summary": "Fixture observed " + result,
                "coverage": ["All explicitly assigned cases"], "not_covered": [], "evidence": ["Fixture command and output"],
                "input_fingerprint": assignment["fingerprint"]["digest"],
                "bug_ids": assignment["bug_ids"] if bug_ids is None and assignment["phase"] == "test" else bug_ids or []}

    def accept(self, assignment, result="pass", bug_ids=None):
        report = self.service.reports.submit(assignment["id"], self.body(assignment, result, bug_ids))
        return self.service.accept_report(report["id"], self.actor, "Independently checked the actual fixture evidence")

    def phase(self, result="pass", bug_ids=None, change=None):
        assignment = self.dispatch()
        if change is not None:
            self.source.write_text(change)
        return self.accept(assignment, result, bug_ids)

    def bug(self, title="Behavior mismatch"):
        return self.service.bugs.create(self.run_id, title, "Actual fixture mismatch", "expected", "actual")

    def triage(self, bug, *, decision=False, summary="Minimal documented fix"):
        proposal = {"summary": summary, "evidence": "Root cause reproduced", "authorization_reference": "test authorization",
                    "impact": "Only the named acceptance condition", "validation_plan": "Focused regression then full acceptance"}
        if decision:
            proposal.update(options=[{"id": "fix", "description": "Approve this behavior", "action": "fix"},
                                     {"id": "reject", "description": "Do not apply it", "action": "reject"}], recommended="fix")
        return self.service.bugs.triage(bug["id"], "NEEDS_DECISION" if decision else "AUTO_ELIGIBLE", proposal, "Evidence-led fixture triage")

    def resolve(self, triage):
        return self.service.bugs.resolve(triage["decision"]["id"], "fix", "Approve this exact proposed change", "test user response")

    def repair(self, bug, change, verified_bug_ids=None):
        worker = self.dispatch([bug["id"]])
        self.source.write_text(change)
        self.assertEqual(self.accept(worker)["phase"], "review")
        self.assertEqual(self.phase()["phase"], "environment")
        self.environment()
        return self.phase(bug_ids=verified_bug_ids)

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(LoopError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_full_develop_review_environment_test_acceptance(self):
        self.assertEqual(self.start("develop")["phase"], "plan")
        self.service.record_plan(self.run_id, self.actor, "Implement the single accepted fixture behavior")
        worker = self.dispatch(claim=False)
        self.error("assignment_not_running", self.service.reports.submit, worker["id"], self.body(worker))
        self.service.assignments.claim(worker["id"])
        self.source.write_text("implemented\n")
        self.assertEqual(self.accept(worker)["phase"], "review")
        self.assertEqual(self.phase()["phase"], "environment")
        self.environment()
        self.assertEqual(self.phase()["phase"], "acceptance")
        self.assertEqual(self.service.finish(self.run_id, self.actor, "All accepted scope verified")["status"], "DONE")
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM resource_locks").fetchone()[0], 0)

    def test_e2e_failure_decision_gate_then_repaired_full_cycle(self):
        self.start()
        self.environment()
        bug = self.bug()
        self.assertEqual(self.phase("fail", [bug["id"]])["phase"], "triage")
        triage = self.triage(bug, decision=True)
        self.error("needs_decision", self.dispatch, [bug["id"]])
        self.resolve(triage)
        self.assertEqual(self.repair(bug, "fixed behavior\n")["phase"], "acceptance")
        self.assertEqual(self.state()["round"], 1)
        self.assertEqual(self.service.finish(self.run_id, self.actor, "Repair independently reviewed and retested")["status"], "DONE")

    def test_independent_bug_continues_while_another_waits_then_both_complete(self):
        self.start()
        self.environment()
        first, second = self.bug("Independent A"), self.bug("Decision B")
        self.phase("fail", [first["id"], second["id"]])
        self.triage(first)
        decision = self.triage(second, decision=True)
        self.error("work_available", self.service.pause, self.run_id, self.actor, "Await B")
        self.assertEqual(self.repair(first, "A fixed\n")["phase"], "triage")
        # A changed unrelated code; retain B's exact proposal and pending user choice.
        self.service.bugs.reassess(self.run_id, second["id"], "B proposal remains applicable after A's independent repair")
        self.assertEqual(len(self.store.list("decisions", run_id=self.run_id)), 1)
        self.resolve(decision)
        self.assertEqual(self.repair(second, "A and B fixed\n", [first["id"], second["id"]])["phase"], "acceptance")
        self.assertEqual(self.service.finish(self.run_id, self.actor, "Both independent bugs verified against final code")["status"], "DONE")

    def test_read_only_input_change_and_post_report_change_are_rejected(self):
        self.start("review")
        reviewer = self.dispatch()
        self.source.write_text("code changed during review\n")
        self.error("input_changed", self.service.reports.submit, reviewer["id"], self.body(reviewer))
        self.source.write_text("initial\n")
        report = self.service.reports.submit(reviewer["id"], self.body(reviewer))
        self.source.write_text("code changed after report\n")
        self.error("input_changed", self.service.accept_report, report["id"], self.actor, "Inspect stale report")
        self.assertEqual(self.store.get("reports", report["id"])["status"], "SUBMITTED")

    def test_uncovered_assigned_bug_is_not_verified_by_a_passing_report(self):
        self.start()
        self.environment()
        first, second = self.bug("A"), self.bug("B")
        self.phase("fail", [first["id"], second["id"]])
        self.triage(first)
        self.triage(second)
        developer = self.dispatch([first["id"], second["id"]])
        self.source.write_text("Both fixes implemented\n")
        self.accept(developer)
        self.phase()
        self.environment()
        tester = self.dispatch()
        body = self.body(tester, bug_ids=[first["id"]])
        body.update(coverage=["A regression passed"], not_covered=["B was not executed"])
        report = self.service.reports.submit(tester["id"], body)
        self.assertEqual(self.service.accept_report(report["id"], self.actor, "Only A was verified")["phase"], "triage")
        self.assertEqual(self.store.get("bugs", first["id"])["status"], "RESOLVED")
        self.assertNotEqual(self.store.get("bugs", second["id"])["status"], "RESOLVED")
        self.error("acceptance_not_ready", self.service.finish, self.run_id, self.actor, "Cannot accept the omitted regression")
        resumed = self.service.resume(self.run_id, self.actor, revalidate=True, checkpoint="A done; run B regression only",
                                      operations_checked=True, old_owner_stopped=True)
        self.assertEqual(resumed["phase"], "test")
        self.assertEqual(resumed["round"], 1)
        self.phase(bug_ids=[second["id"]])
        self.assertEqual(self.service.finish(self.run_id, self.actor, "Both explicit regressions passed on unchanged inputs")["status"], "DONE")

    def test_material_proposal_change_rejects_in_flight_repair_report(self):
        self.start()
        self.environment()
        bug = self.bug()
        self.phase("fail", [bug["id"]])
        self.triage(bug)
        developer = self.dispatch([bug["id"]])
        self.source.write_text("implementation from original plan\n")
        report = self.service.reports.submit(developer["id"], self.body(developer))
        self.triage(bug, decision=True, summary="Materially changed business choice")
        self.error("stale_proposal", self.service.accept_report, report["id"], self.actor, "Old proposal no longer applies")
        self.assertEqual(self.state()["phase"], "develop")

    def test_superseded_repair_cannot_be_claimed_before_execution(self):
        self.start()
        self.environment()
        bug = self.bug()
        self.phase("fail", [bug["id"]])
        self.triage(bug)
        worker = self.dispatch([bug["id"]], claim=False)
        self.triage(bug, decision=True, summary="Changed behavior requires a new user decision")
        self.error("stale_proposal", self.service.assignments.claim, worker["id"])
        self.assertEqual(self.store.get("assignments", worker["id"])["status"], "STARTING")

    def test_code_change_before_developer_claim_cannot_be_attributed_to_worker(self):
        self.start("develop")
        self.service.record_plan(self.run_id, self.actor, "Implement only the authorized fixture change")
        worker = self.dispatch(claim=False)
        self.source.write_text("Changed by another actor before the worker claimed ownership\n")
        self.error("input_changed", self.service.assignments.claim, worker["id"])
        self.assertEqual(self.store.get("assignments", worker["id"])["status"], "STARTING")

    def test_sc_requires_deployment_mapping_and_rejects_changed_environment(self):
        self.start(environment="sc")
        self.error("missing_deployment", self.environment, applications=[])
        self.error("environment_input_mismatch", self.environment, input_fingerprint="other-build")
        self.environment()
        worker = self.dispatch()
        report = self.service.reports.submit(worker["id"], self.body(worker))
        self.store.update("runs", self.run_id, {"environment_revision": self.state()["environment_revision"] + 1})
        self.error("environment_changed", self.service.accept_report, report["id"], self.actor, "Deployment changed after the test")

    def test_coordinator_takeover_preserves_worker_and_rejects_old_actor(self):
        self.start()
        self.environment()
        worker = self.dispatch()
        previous_actor = self.actor
        after = self.service.takeover(self.run_id, old_pm_stopped=True, checkpoint="Worker remains live; no replay", reason="PM disconnected")
        self.actor = after["coordinator_id"]
        self.service.assignments.claim(self.actor)
        self.error("not_coordinator", self.service.next, self.run_id, previous_actor)
        self.assertEqual(self.store.get("assignments", worker["id"])["terminal"], worker["terminal"])
        self.assertEqual(self.accept(worker)["phase"], "acceptance")

    def test_duplicate_dispatch_does_not_create_a_second_terminal(self):
        self.start()
        self.environment()
        worker = self.dispatch()
        before = list(self.runtime.launches)
        self.error("execution_active", self.dispatch)
        self.error("dispatch_exists", self.service.assignments.launch, worker["id"])
        self.assertEqual(self.runtime.launches, before)

    def test_unknown_dispatch_recovers_existing_terminal_without_relaunch(self):
        self.start()
        self.environment()
        self.runtime.fail_next_launch = "after"
        error = self.error("delivery_unknown", self.dispatch)
        assignment_id = error.details["assignment_id"]
        self.assertEqual(self.store.get("assignments", assignment_id)["status"], "UNKNOWN")
        self.error("execution_active", self.dispatch)
        launches = list(self.runtime.launches)
        reconciled = self.service.assignments.reconcile(assignment_id)
        self.assertEqual(reconciled["status"], "observed")
        worker = self.service.assignments.claim(assignment_id)
        self.accept(worker)
        self.assertEqual(self.runtime.launches, launches)

    def test_unobserved_unknown_dispatch_cannot_be_retired_as_if_stopped(self):
        self.start()
        self.environment()
        self.runtime.fail_next_launch = "before"
        error = self.error("delivery_unknown", self.dispatch)
        assignment_id = error.details["assignment_id"]
        self.assertEqual(self.service.assignments.reconcile(assignment_id)["status"], "needs_attention")
        self.error("stop_unverified", self.service.retire_assignment, assignment_id, self.actor,
                   "No terminal found", "Only lookup absence is known", operations_checked=True)
        self.assertEqual(self.store.get("assignments", assignment_id)["status"], "UNKNOWN")

    def test_blocked_worker_handoff_requires_stop_and_uses_fallback(self):
        self.start()
        self.environment()
        old = self.dispatch()
        self.service.assignments.block(old["id"], "Provider quota exhausted", "No request currently in flight")
        self.error("worker_still_running", self.service.handoff, self.run_id, self.actor, "Verified checkpoint", operations_checked=True)
        self.runtime.finish(old["terminal"])
        replacement = self.service.handoff(self.run_id, self.actor, "Verified checkpoint", operations_checked=True)
        self.assertNotEqual(replacement["id"], old["id"])
        self.assertEqual(replacement["agent"], "fixture-fallback")
        self.assertEqual(self.store.get("assignments", old["id"])["status"], "REPLACED")
        self.error("assignment_not_running", self.service.reports.submit, replacement["id"], self.body(replacement))
        self.service.assignments.claim(replacement["id"])
        self.assertEqual(self.accept(replacement)["phase"], "acceptance")

    def test_cli_termination_preserves_blocked_worker_until_checked_handoff(self):
        self.start()
        self.environment()
        old = self.dispatch()
        terminated = self.service.terminate_assignment(old["id"], self.actor, "No completed fixture request will be replayed")
        self.assertEqual(terminated["status"], "BLOCKED")
        self.assertEqual(self.runtime.observe(old["terminal"])["state"], "finished")
        self.error("handoff_checks_required", self.service.handoff, self.run_id, self.actor, "Missing operation verification")
        replacement = self.service.handoff(self.run_id, self.actor, "Confirmed fixture request outcome", operations_checked=True)
        self.assertNotEqual(old["id"], replacement["id"])
        self.assertEqual(replacement["agent"], "fixture-fallback")

    def test_stop_from_coordinator_does_not_kill_its_own_command(self):
        self.start()
        self.environment()
        worker = self.dispatch()
        coordinator = self.store.get("assignments", self.actor)
        with patch.dict("os.environ", {"HC_TERMINAL_ID": coordinator["terminal"]}):
            stopped = self.service.stop(self.run_id, "Stop fixture", terminate=True, operations_checked=True, actor=self.actor)
        self.assertEqual(stopped["status"], "STOPPED")
        self.assertEqual(self.runtime.terminated, [worker["terminal"]])

    def test_stop_termination_preserves_operations_check_boundary(self):
        self.start()
        self.environment()
        worker = self.dispatch()
        self.assertEqual(self.service.stop(self.run_id, "User requested stop", actor=self.actor)["status"], "STOPPING")
        self.assertEqual(self.runtime.observe(worker["terminal"])["state"], "live")
        self.assertEqual(self.service.stop(self.run_id, "Terminate owned processes", terminate=True)["status"], "STOPPING")
        self.assertEqual(self.runtime.observe(worker["terminal"])["state"], "finished")
        self.assertEqual(self.service.stop(self.run_id, "External operations reconciled", operations_checked=True)["status"], "STOPPED")
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM resource_locks").fetchone()[0], 0)

    def test_stopping_run_cannot_accept_pending_success_or_finish(self):
        self.start()
        self.environment()
        worker = self.dispatch()
        report = self.service.reports.submit(worker["id"], self.body(worker))
        self.assertEqual(self.service.stop(self.run_id, "Stop the current work", actor=self.actor)["status"], "STOPPING")
        self.error("run_not_ready", self.service.accept_report, report["id"], self.actor, "Do not overrule the stop request")
        self.error("run_not_ready", self.service.finish, self.run_id, self.actor, "Completion is no longer authorized")
        self.assertEqual(self.state()["status"], "STOPPING")
        self.assertEqual(self.store.get("reports", report["id"])["status"], "SUBMITTED")

    def test_waiting_decision_pauses_active_budget_but_preserves_deadline(self):
        started = time.time()
        with patch("herdr_team_loop.runs.time.time", return_value=started):
            self.start(deadline=started + 100)
            self.environment()
            bug = self.bug()
            self.phase("fail", [bug["id"]])
            decision = self.triage(bug, decision=True)
        with patch("herdr_team_loop.runs.time.time", return_value=started + 5):
            self.service.pause(self.run_id, self.actor, "Waiting only for the material user decision")
            paused = self.service.status(self.run_id)["active_elapsed_seconds"]
        with patch("herdr_team_loop.runs.time.time", return_value=started + 95):
            self.assertEqual(self.service.status(self.run_id)["active_elapsed_seconds"], paused)
            self.assertTrue(self.service.watch(self.run_id, timeout=0)["needs_user"])
            self.resolve(decision)
        with patch("herdr_team_loop.runs.time.time", return_value=started + 101):
            self.error("budget_exhausted", self.service.resume, self.run_id, self.actor)

    def test_repeated_unchanged_failure_blocks_resume(self):
        self.start(no_progress_limit=2)
        self.environment()
        bug = self.bug()
        self.phase("fail", [bug["id"]])
        self.triage(bug)
        self.accept(self.dispatch([bug["id"]]))
        self.phase()
        self.environment()
        state = self.phase("fail", [bug["id"]])
        self.assertEqual((state["status"], state["blocked_reason"]), ("BLOCKED", "no_progress"))
        self.error("no_progress", self.service.resume, self.run_id, self.actor)

    def test_report_only_run_never_dispatches_a_fix(self):
        self.start(repair_policy="report-only", authorization={"source": "Test only", "code_fix": False, "deploy": False})
        self.environment()
        bug = self.bug()
        self.phase("fail", [bug["id"]])
        self.triage(bug)
        self.error("not_authorized", self.dispatch, [bug["id"]])
        self.assertEqual(self.state()["phase"], "triage")


if __name__ == "__main__":
    unittest.main()
