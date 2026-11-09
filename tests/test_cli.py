"""Exercise the public JSON entrypoint without Herdr or model processes."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from herdr_team_loop.cli import main
from herdr_team_loop.storage import Store
from herdr_team_loop.workspace import WorkspaceService


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.project = self.root / "workspace"
        self.other = self.root / "other"
        self.project.mkdir()
        self.other.mkdir()
        self.profile = self.json_file("profile.json", {"id": "python-fake", "command": [sys.executable, "-c", "pass"]})
        self.roles = self.json_file("roles.json", {role: {"primary": "python-fake"} for role in ("coordinator", "developer", "reviewer", "tester")})
        self.scope = self.root / "scope.md"
        self.scope.write_text("目标与验收\n", encoding="utf-8")
        self.env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"), "PATH": ""}

    def json_file(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def cli(self, *arguments, expected=0, cwd=None):
        process = subprocess.run([sys.executable, "-m", "herdr_team_loop", "--data-dir", str(self.data),
                                  *map(str, arguments), "--json"], capture_output=True, text=True,
                                 encoding="utf-8", env=self.env, cwd=cwd or self.project, timeout=10)
        self.assertEqual(process.returncode, expected, process.stdout + process.stderr)
        self.assertEqual(process.stderr, "")
        self.assertEqual(len(process.stdout.strip().splitlines()), 1, process.stdout)
        payload = json.loads(process.stdout)
        self.assertEqual(payload["schema_version"], 1)
        return payload

    def invoke(self, arguments, expected=0):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--data-dir", str(self.data), *map(str, arguments), "--json"])
        self.assertEqual(code, expected, output.getvalue())
        return json.loads(output.getvalue())

    def initialize(self, path=None):
        self.cli("agents", "register", "--profile-file", self.profile)
        return self.cli("init", "--roles-file", self.roles, "--workspace", path or self.project)["data"]

    def requirement(self, workspace=None):
        return self.cli("requirement", "create", "--workspace", workspace or self.project,
                        "--title", "测试需求", "--scope-file", self.scope)["data"]

    def seed_run(self, workspace, requirement, *, status="ACTIVE"):
        store = Store(self.data)
        try:
            run = store.create("runs", {"workspace_id": workspace["id"], "requirement_id": requirement["id"],
                                         "status": status, "coordinator_epoch": 1, "scope_revision": 1,
                                         "scope_digest": requirement["scope_digest"], "scope_artifact_id": requirement["scope_artifact_id"],
                                         "fingerprint": {"digest": "test-digest"}, "repair_policy": "guarded",
                                         "authorization": {"source": "test instruction", "code_fix": True, "deploy": False}})
            coordinator = store.create("assignments", {"workspace_id": workspace["id"], "requirement_id": requirement["id"],
                                                       "run_id": run["id"], "slot": "coordinator", "status": "RUNNING", "epoch": 1,
                                                       "terminal": "terminal-coordinator", "first_message_sent": True})
            worker = store.create("assignments", {"workspace_id": workspace["id"], "requirement_id": requirement["id"],
                                                  "run_id": run["id"], "slot": "worker", "status": "RUNNING", "epoch": 1,
                                                  "terminal": "terminal-worker", "first_message_sent": True,
                                                  "fingerprint": {"digest": "test-digest"}, "phase": "test"})
            run = store.update("runs", run["id"], {"coordinator_id": coordinator["id"]})
            return run, coordinator, worker
        finally:
            store.close()

    def test_global_options_work_before_after_and_between_command_names(self):
        workspace = self.initialize()
        result = self.cli("--workspace", self.project, "roles", "--actor", "unused-init-identity", "show")
        self.assertEqual(result["data"], workspace["roles"])
        result = self.cli("roles", "show", "--workspace", self.project)
        self.assertEqual(result["data"]["coordinator"]["primary"], "python-fake")

    def test_json_init_returns_questions_without_reading_stdin(self):
        with patch("builtins.input", side_effect=AssertionError("must not prompt")), patch("sys.stdin.isatty", return_value=True):
            result = self.invoke(["init", "--workspace", self.project], expected=3)
        self.assertEqual(result["status"], "needs_configuration")
        self.assertIsNone(result["data"]["workspace"])
        self.assertTrue(result["data"]["questions"])
        self.assertEqual(self.cli("workspace", "list")["data"], [])

    def test_non_tty_init_without_json_does_not_prompt(self):
        output = io.StringIO()
        with redirect_stdout(output), patch("builtins.input", side_effect=AssertionError("must not prompt")), patch("sys.stdin.isatty", return_value=False):
            code = main(["init", "--data-dir", str(self.data), "--workspace", str(self.project)])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(output.getvalue())["status"], "needs_configuration")

    def test_tty_init_can_collect_explicit_roles(self):
        self.cli("agents", "register", "--profile-file", self.profile)
        output = io.StringIO()
        with redirect_stdout(output), patch.object(output, "isatty", return_value=True), patch("sys.stdin.isatty", return_value=True), \
                patch("builtins.input", side_effect=["python-fake", "", "", ""]) as prompt:
            code = main(["init", "--data-dir", str(self.data), "--workspace", str(self.project)])
        self.assertEqual(code, 0)
        self.assertEqual(prompt.call_count, 4)
        self.assertEqual(set(json.loads(output.getvalue())["data"]["roles"]), {"coordinator"})

    def test_discovery_works_for_an_unregistered_workspace(self):
        result = self.cli("agents", "discover", "--workspace", self.project)
        self.assertTrue(any(profile["id"] == "codex" for profile in result["data"]))
        self.assertTrue(all(profile["verification"]["quota"] == "unknown" for profile in result["data"]))

    def test_help_and_protocol_emit_clean_json_without_creating_storage(self):
        result = self.cli("run", "start", "--help")
        self.assertIn("--authorization-file", result["data"]["help"])
        self.assertFalse(self.data.exists())
        result = self.cli("protocol")
        self.assertIn("worker_steps", result["data"])
        self.assertFalse(self.data.exists())

    def test_argument_errors_use_the_json_envelope(self):
        result = self.cli("unknown-command", expected=2)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "invalid_arguments")
        result = self.cli("requirement", "create", "--title", "Incomplete", expected=2)
        self.assertEqual(result["error"]["code"], "invalid_arguments")

    def test_strict_json_files_reject_empty_arrays_duplicates_nan_and_invalid_utf8(self):
        bad = self.root / "bad.json"
        for content in (b"", b"{}", b"[]", b"null", b"{broken", b'{"id":"first","id":"second"}', b'{"quota":NaN}', b'{"quota":1e999}', b"\xff"):
            bad.write_bytes(content)
            result = self.cli("agents", "register", "--profile-file", bad, expected=2)
            self.assertEqual(result["error"]["code"], "invalid_file")
        result = self.cli("agents", "register", "--profile-file", self.root / "absent", expected=2)
        self.assertEqual(result["error"]["code"], "invalid_file")

    def test_requirement_roundtrip_revision_and_source_conflict(self):
        self.initialize()
        requirement = self.cli("requirement", "create", "--workspace", self.project, "--title", "Same title",
                               "--source", "jira:CLI-1", "--scope-file", self.scope)["data"]
        shown = self.cli("requirement", "show", "--requirement", requirement["id"])["data"]
        self.assertEqual(shown["scope"], "目标与验收\n")
        conflict = self.cli("requirement", "create", "--workspace", self.project, "--title", "Another title",
                            "--source", "jira:CLI-1", "--scope-file", self.scope, expected=2)
        self.assertEqual(conflict["error"]["details"]["existing_requirement_id"], requirement["id"])
        revised = self.cli("requirement", "revise", "--workspace", self.project, "--requirement", requirement["id"],
                           "--scope-file", self.scope, "--reason", "用户确认范围")["data"]
        self.assertEqual(revised["scope_revision"], 2)

    def test_unregistered_workspace_returns_needs_input(self):
        result = self.cli("requirement", "list", "--workspace", self.project, expected=3)
        self.assertEqual(result["status"], "needs_input")
        self.assertEqual(result["error"]["code"], "workspace_not_registered")

    def test_explicit_workspace_rejects_foreign_requirement_and_run_ids(self):
        first = self.initialize()
        self.initialize(self.other)
        requirement = self.requirement()
        run, _, _ = self.seed_run(first, requirement)
        for arguments in (("requirement", "show", "--requirement", requirement["id"]), ("run", "status", "--run", run["id"])):
            result = self.cli(*arguments, "--workspace", self.other, expected=2)
            self.assertEqual(result["error"]["code"], "scope_mismatch")

    def test_workspace_export_and_backup_preserve_artifacts(self):
        workspace = self.initialize()
        requirement = self.requirement()
        exported = self.cli("workspace", "export", "--workspace-id", workspace["id"])["data"]
        self.assertEqual(exported["requirements"][0]["id"], requirement["id"])
        destination = self.root / "backup"
        result = self.cli("storage", "backup", "--destination", destination)["data"]
        self.assertEqual(result["artifacts"], 1)
        self.assertTrue((destination / "state.sqlite3").is_file())

    def test_worker_cannot_claim_block_or_submit_as_another_assignment(self):
        workspace = self.initialize()
        requirement = self.requirement()
        run, coordinator, worker = self.seed_run(workspace, requirement)
        report = self.json_file("report.json", {"result": "pass"})
        cases = [("assignment", "claim", "--assignment", coordinator["id"]),
                 ("assignment", "block", "--assignment", coordinator["id"], "--reason", "test", "--checkpoint", "checked"),
                 ("report", "submit", "--assignment", coordinator["id"], "--file", report)]
        for arguments in cases:
            result = self.cli(*arguments, "--actor", worker["id"], expected=2)
            self.assertEqual(result["error"]["code"], "actor_mismatch")
        result = self.cli("run", "next", "--run", run["id"], "--actor", worker["id"], expected=2)
        self.assertEqual(result["error"]["code"], "not_coordinator")

    def test_coordinator_reconcile_observes_worker_without_launching(self):
        workspace = self.initialize()
        requirement = self.requirement()
        _, coordinator, worker = self.seed_run(workspace, requirement)
        with patch("herdr_team_loop.runtime.HerdrRuntime.reconcile", return_value={"terminal": "terminal-worker", "first_message_sent": True}) as reconcile, \
                patch("herdr_team_loop.runtime.HerdrRuntime.launch", side_effect=AssertionError("must not launch")):
            result = self.invoke(["assignment", "reconcile", "--assignment", worker["id"], "--actor", coordinator["id"]])
        reconcile.assert_called_once()
        self.assertEqual(result["data"]["assignment"]["id"], worker["id"])

    def test_json_attach_only_returns_a_terminal_plan(self):
        workspace = self.initialize()
        requirement = self.requirement()
        run, coordinator, _ = self.seed_run(workspace, requirement)
        with patch("herdr_team_loop.runtime.HerdrRuntime.attach", side_effect=AssertionError("must not attach")):
            result = self.invoke(["run", "attach", "--run", run["id"]])
        self.assertEqual(result["data"]["assignment_id"], coordinator["id"])
        self.assertFalse(result["data"]["attached"])

    def test_assignment_observe_allows_own_worker_coordinator_or_manual_read(self):
        workspace = self.initialize()
        requirement = self.requirement()
        _, coordinator, worker = self.seed_run(workspace, requirement)
        with patch("herdr_team_loop.assignments.AssignmentService.observe", create=True,
                   return_value={"state": "working", "output": "bounded fixture output"}) as observe:
            for actor in (worker["id"], coordinator["id"], None):
                arguments = ["assignment", "observe", "--assignment", worker["id"]]
                if actor:
                    arguments += ["--actor", actor]
                result = self.invoke(arguments)
                self.assertEqual(result["data"]["state"], "working")
        self.assertEqual(observe.call_count, 3)
        observe.assert_called_with(worker["id"])

    def test_delivery_confirmation_requires_coordinator_or_manual_operator(self):
        workspace = self.initialize()
        requirement = self.requirement()
        _, coordinator, worker = self.seed_run(workspace, requirement)
        arguments = ["assignment", "confirm-delivery", "--assignment", worker["id"], "--evidence", "Original terminal shows exact task"]
        with patch("herdr_team_loop.assignments.AssignmentService.confirm_delivery", create=True,
                   return_value={"first_message_sent": True}) as confirm:
            result = self.invoke([*arguments, "--actor", worker["id"]], expected=2)
            self.assertEqual(result["error"]["code"], "not_coordinator")
            confirm.assert_not_called()
            self.invoke([*arguments, "--actor", coordinator["id"]])
            self.invoke(arguments)
        self.assertEqual(confirm.call_count, 2)
        confirm.assert_called_with(worker["id"], "Original terminal shows exact task")

    def test_known_unsent_delivery_requires_coordinator_or_manual_operator(self):
        workspace = self.initialize()
        requirement = self.requirement()
        _, coordinator, worker = self.seed_run(workspace, requirement)
        arguments = ["assignment", "deliver", "--assignment", worker["id"]]
        with patch("herdr_team_loop.assignments.AssignmentService.deliver",
                   return_value={"first_message_sent": True}) as deliver:
            result = self.invoke([*arguments, "--actor", worker["id"]], expected=2)
            self.assertEqual(result["error"]["code"], "not_coordinator")
            deliver.assert_not_called()
            self.invoke([*arguments, "--actor", coordinator["id"]])
            self.invoke(arguments)
        self.assertEqual(deliver.call_count, 2)
        deliver.assert_called_with(worker["id"], ready_evidence="", ready_turn=None)

    def test_manual_readiness_evidence_cannot_be_asserted_by_an_assignment_actor(self):
        workspace = self.initialize()
        requirement = self.requirement()
        _, coordinator, worker = self.seed_run(workspace, requirement)
        evidence = self.root / "readiness.md"
        evidence.write_text("Original terminal shows the empty agent prompt after trust; turn 0.")
        arguments = ["assignment", "deliver", "--assignment", worker["id"],
                     "--ready-evidence-file", evidence, "--ready-turn", "0"]
        with patch("herdr_team_loop.assignments.AssignmentService.deliver",
                   return_value={"first_message_sent": True}) as deliver:
            denied = self.invoke([*arguments, "--actor", coordinator["id"]], expected=2)
            self.assertEqual(denied["error"]["code"], "manual_recovery_required")
            deliver.assert_not_called()
            self.invoke(arguments)
            deliver.assert_called_once_with(worker["id"], ready_evidence=evidence.read_text(), ready_turn=0)

    def test_budget_extension_requires_operator_and_preserves_authorization_source(self):
        workspace = self.initialize()
        requirement = self.requirement()
        run, coordinator, worker = self.seed_run(workspace, requirement)
        arguments = ["run", "extend-budget", "--run", run["id"], "--active-minutes", "60",
                     "--reason", "Startup recovery consumed the original limit", "--authorization-source", "Explicit initiating request"]
        with patch("herdr_team_loop.runs.RunService.extend_budget", return_value={"id": run["id"]}) as extend:
            for actor in (coordinator, worker):
                denied = self.invoke([*arguments, "--actor", actor["id"]], expected=2)
                self.assertEqual(denied["error"]["code"], "manual_recovery_required")
            extend.assert_not_called()
            self.invoke(arguments)
            extend.assert_called_once_with(run["id"], 60.0, arguments[-3], arguments[-1])

    def test_pending_input_submission_is_operator_only_and_routes_evidence(self):
        workspace = self.initialize()
        requirement = self.requirement()
        _, coordinator, worker = self.seed_run(workspace, requirement)
        evidence = self.root / "pending-input.md"
        evidence.write_text("Original task is the pending pasted input, observed at turn 0.")
        arguments = ["assignment", "submit-pending", "--assignment", worker["id"],
                     "--evidence-file", evidence, "--observed-turn", "0"]
        with patch("herdr_team_loop.assignments.AssignmentService.submit_pending", return_value={"status": "STARTING"}) as submit:
            for actor in (coordinator, worker):
                denied = self.invoke([*arguments, "--actor", actor["id"]], expected=2)
                self.assertEqual(denied["error"]["code"], "manual_recovery_required")
            submit.assert_not_called()
            self.invoke(arguments)
            submit.assert_called_once_with(worker["id"], evidence.read_text(), 0)

    def test_budget_extension_requires_explicit_reason_and_source(self):
        for suffix in ([], ["--reason", "Recovery"], ["--authorization-source", "User reply"]):
            result = self.cli("run", "extend-budget", "--run", "unused", "--active-minutes", "60", *suffix, expected=2)
            self.assertEqual(result["error"]["code"], "invalid_arguments")

    def test_terminate_routes_checkpoint_to_coordinator_service_without_retiring(self):
        workspace = self.initialize()
        requirement = self.requirement()
        _, coordinator, worker = self.seed_run(workspace, requirement)
        checkpoint = self.root / "checkpoint.md"
        checkpoint.write_text("已保存进度，业务结果仍待核对", encoding="utf-8")
        arguments = ["assignment", "terminate", "--assignment", worker["id"], "--checkpoint-file", checkpoint]
        with patch("herdr_team_loop.runs.RunService.terminate_assignment", create=True,
                   return_value={"id": worker["id"], "status": "BLOCKED"}) as terminate, \
                patch("herdr_team_loop.runs.RunService.retire_assignment", side_effect=AssertionError("must not retire")), \
                patch("herdr_team_loop.runs.RunService.handoff", side_effect=AssertionError("must not handoff")):
            self.invoke(arguments, expected=3)
            result = self.invoke([*arguments, "--actor", worker["id"]], expected=2)
            self.assertEqual(result["error"]["code"], "not_coordinator")
            terminate.assert_not_called()
            result = self.invoke([*arguments, "--actor", coordinator["id"]])
        terminate.assert_called_once_with(worker["id"], coordinator["id"], "已保存进度，业务结果仍待核对")
        self.assertEqual(result["data"]["status"], "BLOCKED")

    def test_start_forwards_explicit_settings_and_test_alias_without_inventing_repos(self):
        self.initialize()
        requirement = self.requirement()
        with patch("herdr_team_loop.runs.RunService") as run_service:
            run_service.return_value.start.return_value = {"id": "run_example"}
            self.invoke(["run", "start", "--requirement", requirement["id"], "--mode", "test"])
        arguments = run_service.return_value.start.call_args.kwargs
        self.assertEqual(arguments["mode"], "e2e")
        self.assertEqual(arguments["repos"], [])
        self.assertEqual(arguments["authorization"], {"source": "CLI test request", "code_fix": False, "deploy": False})
        denied = self.cli("run", "start", "--requirement", requirement["id"], "--mode", "test", expected=2)
        self.assertEqual(denied["error"]["code"], "repositories_required")

    def test_bug_triage_requires_coordinator_and_preserves_pending_decision(self):
        workspace = self.initialize()
        requirement = self.requirement()
        run, coordinator, worker = self.seed_run(workspace, requirement)
        bug = self.cli("bug", "create", "--run", run["id"], "--actor", worker["id"], "--title", "Mismatch",
                       "--evidence", "Observed state", "--expected", "Expected state", "--actual", "Actual state")["data"]
        proposal = self.json_file("proposal.json", {
            "summary": "Change business semantics", "evidence": "Observed state", "authorization_reference": "User task",
            "impact": "Business contract changes", "validation_plan": "Verify outcome", "recommended": "repair",
            "options": [{"id": "repair", "description": "Apply the change", "action": "fix"},
                        {"id": "hold", "description": "Keep current behavior", "action": "defer"}]})
        command = ["bug", "triage", "--run", run["id"], "--bug", bug["id"], "--classification", "NEEDS_DECISION",
                   "--proposal-file", proposal, "--reason", "Business choice"]
        result = self.cli(*command, "--actor", worker["id"], expected=2)
        self.assertEqual(result["error"]["code"], "not_coordinator")
        result = self.cli(*command, "--actor", coordinator["id"], expected=3)
        self.assertEqual(result["status"], "ok")
        decision = result["data"]["decision"]
        self.assertEqual(decision["status"], "PENDING")
        resolved = self.cli("decision", "resolve", "--decision", decision["id"], "--option", "hold",
                            "--response", "先保留原行为", "--source", "direct user reply")["data"]
        self.assertEqual(resolved["status"], "DEFERRED")

    def test_bug_reassess_routes_evidence_only_for_current_coordinator(self):
        workspace = self.initialize()
        requirement = self.requirement()
        run, coordinator, worker = self.seed_run(workspace, requirement)
        bug = self.cli("bug", "create", "--run", run["id"], "--actor", worker["id"], "--title", "Mismatch",
                       "--evidence", "Observed state", "--expected", "Expected state", "--actual", "Actual state")["data"]
        command = ["bug", "reassess", "--run", run["id"], "--bug", bug["id"],
                   "--evidence", "Reviewed the independent change; proposal is unchanged"]
        with patch("herdr_team_loop.bugs.BugService.reassess", return_value={"id": "run_bug_test"}) as reassess:
            missing = self.invoke(command, expected=3)
            self.assertEqual(missing["error"]["code"], "actor_required")
            denied = self.invoke([*command, "--actor", worker["id"]], expected=2)
            self.assertEqual(denied["error"]["code"], "not_coordinator")
            reassess.assert_not_called()
            result = self.invoke([*command, "--actor", coordinator["id"]])
            reassess.assert_called_once_with(run["id"], bug["id"], command[-1])
            self.assertEqual(result["data"]["id"], "run_bug_test")
            store = Store(self.data)
            try:
                store.update("runs", run["id"], {"coordinator_epoch": 2})
            finally:
                store.close()
            denied = self.invoke([*command, "--actor", coordinator["id"]], expected=2)
            self.assertEqual(denied["error"]["code"], "not_coordinator")
            self.assertEqual(reassess.call_count, 1)


if __name__ == "__main__":
    unittest.main()
