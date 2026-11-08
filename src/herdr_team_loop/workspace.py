"""Workspace configuration and versioned requirements without agent execution."""
import base64
from pathlib import Path

from .agents import discover_agents, validate_profile, validate_roles
from .domain import ROLES, TERMINAL_RUNS
from .errors import LoopError, require
from .storage import Store


def _directory(path: str) -> str:
    require(isinstance(path, str) and bool(path.strip()), "invalid_workspace", "A workspace directory is required")
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
        require(resolved.is_dir(), "invalid_workspace", "Workspace must be an existing directory")
    except (OSError, RuntimeError, ValueError) as error:
        raise LoopError("invalid_workspace", "Workspace directory cannot be resolved") from error
    return str(resolved)


def _text(value: str, field: str) -> str:
    require(isinstance(value, str) and bool(value.strip()), "invalid_requirement", f"Requirement {field} must not be empty")
    return value


def _same_roles(roles: dict, stored: dict) -> bool:
    if not isinstance(roles, dict) or set(roles) - set(ROLES):
        return False
    normalized = {}
    for role, binding in roles.items():
        if binding is None and role != "coordinator":
            continue
        if not isinstance(binding, dict):
            return False
        normalized[role] = {"primary": binding.get("primary"), "fallbacks": binding.get("fallbacks", [])}
    return normalized == stored


class WorkspaceService:
    """Keep physical workspace identities separate from mutable role and scope versions."""

    def __init__(self, store: Store):
        self.store = store

    def profiles(self) -> list[dict]:
        return discover_agents(self.store.list("profiles"))

    def register_profile(self, profile: dict) -> dict:
        normalized = validate_profile(profile)
        with self.store.transaction():
            existing = self.store.list("profiles", id=normalized["id"])
            if existing:
                # Discovery facts can change with PATH; they are not launch configuration.
                transient = {"revision", "status", "created_at", "updated_at", "installed", "executable", "verification"}
                previous_spec = {key: value for key, value in existing[0].items() if key not in transient}
                next_spec = {key: value for key, value in normalized.items() if key not in transient}
                require(previous_spec == next_spec, "conflict", "Agent profile already exists with another specification; register a distinct profile ID",
                        existing_profile_id=normalized["id"])
                return existing[0]
            result = self.store.create("profiles", normalized)
            self.store.event("profile_registered", data={"profile_id": result["id"]})
            return result

    def inspect(self, path: str) -> dict:
        physical_path = _directory(path)
        profiles = self.profiles()
        existing = self.store.list("workspaces", path=physical_path)
        if existing:
            return {"status": "ready", "path": physical_path, "workspace": existing[0], "agents": profiles, "questions": []}
        choices = [profile["id"] for profile in profiles if profile.get("installed")]
        questions = [{"role": role, "required": role == "coordinator",
                      "question": f"Choose the {role} agent" if role == "coordinator" else f"Choose the {role} agent or leave it unassigned",
                      "choices": choices[:]} for role in ROLES]
        return {"status": "needs_configuration", "path": physical_path, "workspace": None,
                "agents": profiles, "questions": questions}

    def initialize(self, path: str, roles: dict) -> dict:
        physical_path = _directory(path)
        with self.store.transaction():
            existing = self.store.list("workspaces", path=physical_path)
            if existing:
                # Reading an unchanged configuration stays possible while an agent is offline.
                require(_same_roles(roles, existing[0]["roles"]), "conflict", "Workspace already has another role mapping; use roles set to change it",
                        workspace_id=existing[0]["id"])
                return existing[0]
            normalized = validate_roles(roles, self.profiles())
            workspace = self.store.create("workspaces", {"path": physical_path, "roles": normalized, "config_revision": 1})
            self.store.event("workspace_initialized", workspace_id=workspace["id"], data={"config_revision": 1})
            return workspace

    def resolve(self, path: str | None = None) -> dict:
        explicit = path is not None
        try:
            physical_path = _directory(path if explicit else str(Path.cwd()))
        except OSError as error:
            raise LoopError("invalid_workspace", "Current workspace directory cannot be resolved") from error
        current = Path(physical_path)
        if explicit:
            candidates = self.store.list("workspaces", path=physical_path)
        else:
            candidates = [workspace for workspace in self.store.list("workspaces")
                          if current.is_relative_to(Path(workspace["path"]))]
            candidates.sort(key=lambda workspace: len(Path(workspace["path"]).parts), reverse=True)
        require(bool(candidates), "workspace_not_registered", "Workspace is not registered; inspect and initialize it first", path=physical_path)
        return candidates[0]

    def roles(self, path: str | None = None) -> dict:
        return self.resolve(path)["roles"]

    def set_role(self, path: str, role: str, primary: str, fallbacks: list) -> dict:
        require(role in ROLES, "invalid_roles", "Unknown workspace role", role=role)
        with self.store.transaction():
            workspace = self.resolve(path)
            roles = dict(workspace["roles"])
            roles[role] = {"primary": primary, "fallbacks": fallbacks}
            normalized = validate_roles(roles, self.profiles())
            workspace = self.store.update("workspaces", workspace["id"],
                                          {"roles": normalized, "config_revision": workspace["config_revision"] + 1},
                                          expected_revision=workspace["revision"])
            self.store.event("workspace_roles_changed", workspace_id=workspace["id"],
                             data={"role": role, "config_revision": workspace["config_revision"]})
            return workspace

    def relocate(self, workspace_id: str, new_path: str) -> dict:
        physical_path = _directory(new_path)
        with self.store.transaction():
            workspace = self.store.get("workspaces", workspace_id)
            active = [run["id"] for run in self.store.list("runs", workspace_id=workspace_id)
                      if run["status"] not in TERMINAL_RUNS]
            require(not active, "active_run", "Workspace cannot be relocated while a run is active", run_ids=active)
            existing = self.store.list("workspaces", path=physical_path)
            require(not existing or existing[0]["id"] == workspace_id, "conflict", "Destination is already a registered workspace",
                    workspace_id=existing[0]["id"] if existing else None)
            if workspace["path"] == physical_path:
                return workspace
            previous_path = workspace["path"]
            workspace = self.store.update("workspaces", workspace_id, {"path": physical_path},
                                          expected_revision=workspace["revision"])
            self.store.event("workspace_relocated", workspace_id=workspace_id,
                             data={"previous_path": previous_path, "path": physical_path})
            return workspace

    def create_requirement(self, workspace_id: str, title: str, scope: str, source: str | None = None) -> dict:
        title = _text(title, "title").strip()
        scope = _text(scope, "scope")
        if source is not None:
            source = _text(source, "source").strip()
        with self.store.transaction():
            self.store.get("workspaces", workspace_id)
            if source is not None:
                existing = self.store.list("requirements", workspace_id=workspace_id, source=source)
                require(not existing, "conflict", "Source is already mapped to a requirement in this workspace",
                        existing_requirement_id=existing[0]["id"] if existing else None, source=source)
            requirement = self.store.create("requirements", {"workspace_id": workspace_id, "title": title,
                                                              "source": source, "scope_revision": 1})
            artifact = self.store.save_artifact(workspace_id, kind="scopes", content=scope, requirement_id=requirement["id"])
            artifact = self.store.update("artifacts", artifact["id"], {"scope_revision": 1})
            requirement = self.store.update("requirements", requirement["id"],
                                            {"scope_artifact_id": artifact["id"], "scope_digest": artifact["sha256"]},
                                            expected_revision=requirement["revision"])
            self.store.event("requirement_created", workspace_id=workspace_id, requirement_id=requirement["id"],
                             data={"scope_revision": 1, "scope_artifact_id": artifact["id"]})
            return requirement

    def list_requirements(self, workspace_id: str) -> list[dict]:
        self.store.get("workspaces", workspace_id)
        return self.store.list("requirements", workspace_id=workspace_id)

    def get_requirement(self, workspace_id: str, requirement_id: str) -> dict:
        self.store.get("workspaces", workspace_id)
        return self.store.get("requirements", requirement_id, workspace_id)

    def revise_requirement(self, workspace_id: str, requirement_id: str, scope: str, reason: str) -> dict:
        scope = _text(scope, "scope")
        reason = _text(reason, "revision reason").strip()
        with self.store.transaction():
            requirement = self.get_requirement(workspace_id, requirement_id)
            artifact = self.store.save_artifact(workspace_id, kind="scopes", content=scope, requirement_id=requirement_id)
            artifact = self.store.update("artifacts", artifact["id"],
                                         {"scope_revision": requirement["scope_revision"] + 1, "reason": reason})
            previous_artifact = requirement["scope_artifact_id"]
            requirement = self.store.update("requirements", requirement_id,
                                            {"scope_artifact_id": artifact["id"], "scope_digest": artifact["sha256"],
                                             "scope_revision": requirement["scope_revision"] + 1},
                                            expected_revision=requirement["revision"])
            self.store.event("requirement_scope_revised", workspace_id=workspace_id, requirement_id=requirement_id,
                             data={"scope_revision": requirement["scope_revision"], "scope_artifact_id": artifact["id"],
                                   "previous_scope_artifact_id": previous_artifact, "reason": reason})
            return requirement

    def export_workspace(self, workspace_id: str) -> dict:
        with self.store.transaction():
            workspace = self.store.get("workspaces", workspace_id)
            result = {"schema_version": 1, "workspace": workspace}
            for table in ("requirements", "runs", "assignments", "bugs", "proposals", "decisions", "run_bugs", "reports", "artifacts"):
                result[table] = self.store.list(table, workspace_id=workspace_id)
            for artifact in result["artifacts"]:
                artifact["content_base64"] = base64.b64encode(self.store.read_artifact(artifact["id"])).decode("ascii")
            return result
