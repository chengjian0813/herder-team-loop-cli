"""Workspace and requirement contracts using local storage and inert profiles."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from herdr_team_loop.errors import LoopError
from herdr_team_loop.storage import Store
from herdr_team_loop.workspace import WorkspaceService


class WorkspaceServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.other = self.root / "other"
        self.project.mkdir()
        self.other.mkdir()
        self.store = Store(self.root / "data")
        self.addCleanup(self.store.close)
        self.service = WorkspaceService(self.store)
        self.profile = {"id": "python-one", "command": [sys.executable, "-c", "pass"]}
        self.service.register_profile(self.profile)
        self.service.register_profile({"id": "python-two", "command": [sys.executable, "-c", "pass"]})
        self.mapping = {"coordinator": {"primary": "python-one"}, "tester": {"primary": "python-two", "fallbacks": ["python-one"]}}

    def initialize(self, path=None):
        return self.service.initialize(str(path or self.project), self.mapping)

    def assert_error(self, code, function, *args):
        with self.assertRaises(LoopError) as caught:
            function(*args)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_inspect_discovers_without_registering_or_launching(self):
        with patch("subprocess.run") as run, patch("subprocess.Popen") as popen:
            result = self.service.inspect(str(self.project))
        run.assert_not_called()
        popen.assert_not_called()
        self.assertEqual(result["status"], "needs_configuration")
        self.assertIsNone(result["workspace"])
        self.assertEqual(self.store.list("workspaces"), [])
        self.assertEqual([question["role"] for question in result["questions"] if question["required"]], ["coordinator"])
        profile = next(profile for profile in result["agents"] if profile["id"] == "python-one")
        self.assertTrue(profile["installed"])
        self.assertEqual(profile["verification"]["quota"], "unknown")

    def test_profile_registration_is_idempotent_and_changes_conflict(self):
        original = self.service.register_profile(self.profile)
        self.assertEqual(self.service.register_profile(self.profile), original)
        self.assert_error("conflict", self.service.register_profile,
                          {"id": "python-one", "command": [sys.executable, "-V"]})
        self.assertEqual(len(self.store.list("profiles")), 2)

    def test_explicit_profile_can_override_a_builtin_without_launching(self):
        self.service.register_profile({"id": "droid", "command": [sys.executable, "-c", "pass"]})
        profiles = self.service.profiles()
        self.assertEqual(len([profile for profile in profiles if profile["id"] == "droid"]), 1)
        self.assertEqual(next(profile for profile in profiles if profile["id"] == "droid")["command"][0], sys.executable)

    def test_initialize_requires_explicit_coordinator(self):
        self.assert_error("invalid_roles", self.service.initialize, str(self.project), {"tester": {"primary": "python-one"}})
        self.assertEqual(self.store.list("workspaces"), [])

    def test_physical_path_and_normalized_mapping_make_initialize_idempotent(self):
        workspace = self.initialize()
        link = self.root / "project-link"
        link.symlink_to(self.project, target_is_directory=True)
        equivalent = {**self.mapping, "coordinator": {"primary": "python-one", "fallbacks": []}, "developer": None}
        self.assertEqual(self.service.initialize(str(link), equivalent), workspace)
        self.assertEqual(workspace["path"], str(self.project.resolve()))
        self.assertEqual(len(self.store.list("workspaces")), 1)
        self.assertEqual(self.service.inspect(str(link))["status"], "ready")

    def test_initialize_never_overwrites_an_existing_mapping(self):
        original = self.initialize()
        error = self.assert_error("conflict", self.service.initialize, str(self.project), {"coordinator": {"primary": "python-two"}})
        self.assertEqual(error.details["workspace_id"], original["id"])
        self.assertIn("roles set", error.message)
        self.assertEqual(self.service.resolve(str(self.project)), original)

    def test_unchanged_initialization_does_not_require_an_agent_to_remain_installed(self):
        original = self.initialize()
        with patch("herdr_team_loop.agents.shutil.which", return_value=None):
            self.assertEqual(self.service.initialize(str(self.project), self.mapping), original)
            self.assertEqual(self.service.inspect(str(self.project))["workspace"], original)
            self.assert_error("agent_unavailable", self.service.initialize, str(self.other), self.mapping)

    def test_paths_must_be_existing_directories(self):
        file = self.root / "file"
        file.write_text("not a workspace")
        for path in (str(file), str(self.root / "missing"), "", "bad\0path"):
            self.assert_error("invalid_workspace", self.service.inspect, path)
        self.assertEqual(self.store.list("workspaces"), [])

    def test_resolve_uses_nearest_ancestor_only_without_explicit_workspace(self):
        outer = self.initialize()
        nested = self.project / "nested"
        child = nested / "src"
        child.mkdir(parents=True)
        inner = self.initialize(nested)
        with patch("herdr_team_loop.workspace.Path.cwd", return_value=child):
            self.assertEqual(self.service.resolve()["id"], inner["id"])
        self.assertEqual(self.service.resolve(str(self.project))["id"], outer["id"])
        self.assert_error("workspace_not_registered", self.service.resolve, str(child))
        self.assert_error("workspace_not_registered", self.service.resolve, str(self.other))

    def test_role_updates_preserve_a_frozen_run_mapping(self):
        workspace = self.initialize()
        requirement = self.service.create_requirement(workspace["id"], "Delivery", "Initial acceptance")
        run = self.store.create("runs", {"workspace_id": workspace["id"], "requirement_id": requirement["id"],
                                         "status": "ACTIVE", "roles": workspace["roles"], "config_revision": 1})
        changed = self.service.set_role(str(self.project), "coordinator", "python-two", [])
        self.assertEqual(changed["config_revision"], 2)
        self.assertEqual(self.service.roles(str(self.project))["coordinator"]["primary"], "python-two")
        self.assertEqual(self.store.get("runs", run["id"]), run)
        self.assertEqual(self.service.set_role(str(self.project), "coordinator", "python-two", [])["config_revision"], 3)
        self.assert_error("invalid_roles", self.service.set_role, str(self.project), "wizard", "python-one", [])

    def test_relocation_requires_no_active_run_and_preserves_identity(self):
        workspace = self.initialize()
        requirement = self.service.create_requirement(workspace["id"], "Task", "Scope")
        run = self.store.create("runs", {"workspace_id": workspace["id"], "requirement_id": requirement["id"], "status": "WAITING_DECISION"})
        error = self.assert_error("active_run", self.service.relocate, workspace["id"], str(self.other))
        self.assertEqual(error.details["run_ids"], [run["id"]])
        self.store.update("runs", run["id"], {"status": "DONE"})
        self.project.rmdir()
        moved = self.service.relocate(workspace["id"], str(self.other))
        self.assertEqual(moved["id"], workspace["id"])
        self.assertEqual(moved["path"], str(self.other.resolve()))
        self.assertEqual(self.service.resolve(str(self.other))["id"], workspace["id"])

    def test_relocation_refuses_another_workspace_path(self):
        first = self.initialize()
        second = self.initialize(self.other)
        self.assert_error("conflict", self.service.relocate, first["id"], str(self.other))
        self.assertEqual(self.service.resolve(str(self.other)), second)

    def test_requirements_are_scoped_and_titles_do_not_deduplicate(self):
        first = self.initialize()
        second = self.initialize(self.other)
        a = self.service.create_requirement(first["id"], "Same title", "First scope")
        b = self.service.create_requirement(first["id"], "Same title", "Second scope")
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual([row["id"] for row in self.service.list_requirements(first["id"])], [a["id"], b["id"]])
        self.assertEqual(self.service.list_requirements(second["id"]), [])
        self.assert_error("scope_mismatch", self.service.get_requirement, second["id"], a["id"])
        self.assert_error("scope_mismatch", self.service.revise_requirement, second["id"], a["id"], "Bad scope", "Other workspace")

    def test_sources_deduplicate_only_inside_the_same_workspace(self):
        first = self.initialize()
        second = self.initialize(self.other)
        requirement = self.service.create_requirement(first["id"], "Original", "Scope", "jira:PAY-1")
        error = self.assert_error("conflict", self.service.create_requirement, first["id"], "Different title", "New scope", "jira:PAY-1")
        self.assertEqual(error.details["existing_requirement_id"], requirement["id"])
        self.service.create_requirement(second["id"], "Same source elsewhere", "Scope", "jira:PAY-1")
        self.assertEqual(len(self.store.list("requirements")), 2)

    def test_concurrent_source_creation_has_one_winner(self):
        workspace = self.initialize()

        def create():
            store = Store(self.root / "data")
            try:
                return WorkspaceService(store).create_requirement(workspace["id"], "Concurrent", "Scope", "jira:PAY-2")["id"]
            except LoopError as error:
                return error.code
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: create(), range(2)))
        self.assertEqual(results.count("conflict"), 1)
        self.assertEqual(len(self.service.list_requirements(workspace["id"])), 1)

    def test_scope_revision_is_independent_and_does_not_change_an_active_run(self):
        workspace = self.initialize()
        requirement = self.service.create_requirement(workspace["id"], "Scope version", "原始验收\n")
        self.assertEqual(requirement["scope_revision"], 1)
        self.assertEqual(requirement["scope_digest"], hashlib.sha256("原始验收\n".encode()).hexdigest())
        run = self.store.create("runs", {"workspace_id": workspace["id"], "requirement_id": requirement["id"],
                                         "scope_revision": 1, "scope_artifact_id": requirement["scope_artifact_id"]})
        changed = self.service.revise_requirement(workspace["id"], requirement["id"], "New acceptance", "User clarified coverage")
        self.assertEqual(changed["scope_revision"], 2)
        self.assertNotEqual(changed["scope_artifact_id"], requirement["scope_artifact_id"])
        self.assertEqual(self.store.read_artifact(requirement["scope_artifact_id"]).decode(), "原始验收\n")
        self.assertEqual(self.store.read_artifact(changed["scope_artifact_id"]), b"New acceptance")
        artifact = self.store.get("artifacts", changed["scope_artifact_id"])
        self.assertEqual(artifact["scope_revision"], 2)
        self.assertEqual(artifact["reason"], "User clarified coverage")
        self.assertEqual(self.store.get("runs", run["id"]), run)

    def test_invalid_scope_and_artifact_failure_do_not_leave_partial_requirements(self):
        workspace = self.initialize()
        for title, scope in (("", "Scope"), ("Title", "  ")):
            self.assert_error("invalid_requirement", self.service.create_requirement, workspace["id"], title, scope)
        with patch.object(self.store, "save_artifact", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.service.create_requirement(workspace["id"], "Task", "Scope")
        self.assertEqual(self.service.list_requirements(workspace["id"]), [])

    def test_export_is_workspace_scoped_and_contains_verified_artifact_bytes(self):
        first = self.initialize()
        second = self.initialize(self.other)
        requirement = self.service.create_requirement(first["id"], "Exported", "Preserved scope")
        self.service.create_requirement(second["id"], "Private other workspace", "Other scope")
        exported = self.service.export_workspace(first["id"])
        self.assertEqual(exported["workspace"], first)
        self.assertEqual([row["id"] for row in exported["requirements"]], [requirement["id"]])
        self.assertEqual(len(exported["artifacts"]), 1)
        self.assertEqual(base64.b64decode(exported["artifacts"][0]["content_base64"]), b"Preserved scope")
        self.assertNotIn("content_base64", self.store.get("artifacts", requirement["scope_artifact_id"]))
        (self.store.root / exported["artifacts"][0]["path"]).write_text("changed")
        self.assert_error("artifact_changed", self.service.export_workspace, first["id"])


if __name__ == "__main__":
    unittest.main()
