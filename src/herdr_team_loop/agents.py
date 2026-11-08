"""Discover executable profiles without starting agents or inspecting credentials."""
import os
from pathlib import Path
import re
import shutil

from .errors import require


BUILTINS = {
    "droid": ["droid"],
    "codex": ["codex", "--no-daemon"],
    "cursor": ["cursor-agent"],
    "kimi": ["kimi"],
    "claude": ["claude"],
    "pi": ["pi"],
    "omp": ["omp"],
    "opencode": ["opencode"],
}
ROLES = {"coordinator", "developer", "reviewer", "tester"}


def validate_profile(profile: dict) -> dict:
    """Normalize a launch specification and resolve only its executable on PATH."""
    require(isinstance(profile, dict), "invalid_profile", "Agent profile must be an object")
    identifier = profile.get("id")
    require(isinstance(identifier, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", identifier),
            "invalid_profile", "Profile id must use lowercase letters, digits, hyphens or underscores")
    command = profile.get("command")
    require(isinstance(command, list) and command and all(
        isinstance(part, str) and part and not any(char in part for char in ("\0", "\n", "\r")) for part in command),
        "invalid_profile", "Profile command must be a nonempty argument array", profile=identifier)
    require(not command[0].startswith("-") and command[0].strip(),
            "invalid_profile", "Profile executable must be a command or path", profile=identifier)
    command = list(command)
    if identifier == "codex" or Path(command[0]).name == "codex":
        require("--daemon" not in command, "invalid_profile", "Codex must run without its daemon")
        if "--no-daemon" not in command:
            command.append("--no-daemon")
    executable = shutil.which(command[0])
    if executable:
        executable = os.path.realpath(executable)
    return {
        "id": identifier,
        "command": command,
        "executable": executable,
        "installed": executable is not None,
        "verification": {
            "launch_spec": "configured", "first_message": "unverified", "reporting": "unverified",
            "status": "unverified", "stop": "unverified", "recovery": "unverified",
            "authentication": "unknown", "quota": "unknown",
        },
    }


def discover_agents(custom_profiles: list[dict] | None = None) -> list[dict]:
    """Return built-in candidates plus explicitly registered command overrides."""
    require(custom_profiles is None or isinstance(custom_profiles, list),
            "invalid_profile", "Custom profiles must be a list")
    profiles = {identifier: validate_profile({"id": identifier, "command": command})
                for identifier, command in BUILTINS.items()}
    seen = set()
    for custom in custom_profiles or []:
        profile = validate_profile(custom)
        require(profile["id"] not in seen, "invalid_profile", "Duplicate custom profile", profile=profile["id"])
        seen.add(profile["id"])
        profiles[profile["id"]] = profile
    return list(profiles.values())


def validate_roles(roles: dict, profiles: list[dict]) -> dict:
    """Validate explicit workspace choices; never infer a coordinator or fallback."""
    require(isinstance(roles, dict) and roles.get("coordinator") is not None,
            "invalid_roles", "A coordinator role is required")
    require(not set(roles) - ROLES, "invalid_roles", "Unknown workspace role")
    require(isinstance(profiles, list), "invalid_profile", "Profiles must be a list")
    available = {}
    for item in profiles:
        profile = validate_profile(item)
        require(profile["id"] not in available, "invalid_profile", "Duplicate profile", profile=profile["id"])
        available[profile["id"]] = profile
    result = {}
    for role, binding in roles.items():
        if binding is None and role != "coordinator":
            continue
        require(isinstance(binding, dict), "invalid_roles", "Role binding must be an object", role=role)
        primary, fallbacks = binding.get("primary"), binding.get("fallbacks", [])
        require(isinstance(primary, str) and primary and isinstance(fallbacks, list)
                and all(isinstance(item, str) and item for item in fallbacks),
                "invalid_roles", "Role requires a primary profile and a fallback list", role=role)
        choices = [primary, *fallbacks]
        require(len(choices) == len(set(choices)), "invalid_roles", "Role choices must be distinct", role=role)
        for identifier in choices:
            require(identifier in available, "invalid_roles", "Unknown agent profile", role=role, profile=identifier)
            require(available[identifier]["installed"], "agent_unavailable", "Agent executable is not available",
                    role=role, profile=identifier)
        result[role] = {"primary": primary, "fallbacks": list(fallbacks)}
    return result
