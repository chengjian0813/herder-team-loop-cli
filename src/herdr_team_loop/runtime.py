"""Herdr transport adapter; no model calls and no business completion decisions."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile

from .agents import validate_profile
from .errors import LoopError, require


class HerdrRuntime:
    """Own native terminal delivery and recovery through bounded hc subprocesses."""

    def __init__(self, executable="hc", env: dict | None = None, timeout=70):
        self.executable = executable
        self.env = {**os.environ, **(env or {})}
        require(isinstance(timeout, (int, float)) and math.isfinite(timeout) and timeout > 0,
                "invalid_runtime", "Runtime timeout must be positive and finite")
        self.timeout = timeout

    def _run(self, arguments: list[str], *, timeout=None) -> subprocess.CompletedProcess:
        try:
            process = subprocess.run([self.executable, *arguments], env=self.env, capture_output=True,
                                     text=True, encoding="utf-8", errors="replace",
                                     timeout=self.timeout if timeout is None else timeout)
        except subprocess.TimeoutExpired as error:
            raise LoopError("runtime_timeout", "Herdr request timed out",
                            details={"operation": arguments[0]}) from error
        except OSError as error:
            raise LoopError("runtime_unavailable", "Cannot execute Herdr",
                            details={"operation": arguments[0]}) from error
        if process.returncode:
            details = {"operation": arguments[0], "exit_code": process.returncode}
            try:
                response = json.loads(process.stderr)
                if isinstance(response, dict) and isinstance(response.get("error"), dict):
                    details["runtime_error"] = response["error"]
            except (ValueError, TypeError):
                pass
            raise LoopError("runtime_error", "Herdr request failed", details=details)
        return process

    def _json(self, arguments: list[str], *, timeout=None) -> dict:
        process = self._run(arguments, timeout=timeout)
        try:
            result = json.loads(process.stdout)
        except ValueError as error:
            raise LoopError("runtime_protocol", "Herdr returned invalid JSON",
                            details={"operation": arguments[0]}) from error
        require(isinstance(result, dict) and "error" not in result,
                "runtime_protocol", "Herdr returned an unexpected JSON response", operation=arguments[0])
        return result

    @staticmethod
    def _terminal(value: str) -> str:
        require(isinstance(value, str) and value and not value.startswith("-") and "\0" not in value,
                "invalid_terminal", "An explicit terminal address is required")
        return value

    @staticmethod
    def _assignment(assignment: dict) -> None:
        require(isinstance(assignment, dict) and all(
            isinstance(assignment.get(key), str) and assignment[key] and "\0" not in assignment[key]
            for key in ("id", "run_id", "workspace_id")), "invalid_assignment", "Assignment identity is required")

    def _template(self, profile: dict) -> tuple[str, str]:
        normalized = validate_profile(profile)
        command = list(normalized["command"])
        executable = shutil.which(command[0], path=self.env.get("PATH", ""))
        require(executable, "agent_unavailable", "Agent executable is not available", profile=normalized["id"])
        command[0] = str(Path(executable).resolve())
        encoded = shlex.join(command)
        name = "htloop-" + hashlib.sha256(encoded.encode()).hexdigest()[:20]
        config_home = Path(self.env.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        require(config_home.is_absolute(), "invalid_runtime", "XDG_CONFIG_HOME must be absolute")
        directory = config_home / "herdr-core" / "templates"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (name + ".md")
        # hc reads this frontmatter value literally; outer YAML/JSON quoting would
        # become part of the shell command and could undo shlex's argument quoting.
        content = ("---\ndescription: Managed interactive launcher for herdr-team-loop-cli.\ncommand: "
                   + encoded + "\n---\n")
        if path.exists() or path.is_symlink():
            require(not path.is_symlink() and path.is_file() and path.read_text() == content,
                    "template_conflict", "Managed template path contains different content", path=str(path))
            return name, str(path)
        # Link an already-fsynced file into place: no partial template and no overwrite.
        descriptor, temporary = tempfile.mkstemp(prefix=".htloop-", dir=directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                require(not path.is_symlink() and path.is_file() and path.read_text() == content,
                        "template_conflict", "Managed template path contains different content", path=str(path))
        finally:
            os.unlink(temporary)
        return name, str(path)

    def launch(self, assignment: dict, profile: dict, message: str, cwd: str) -> dict:
        self._assignment(assignment)
        require(isinstance(assignment.get("session_name"), str)
                and re.fullmatch(r"[a-z0-9_-]{1,32}", assignment["session_name"]),
                "invalid_assignment", "Herdr session name must use 1-32 lowercase safe characters")
        require(isinstance(message, str) and message.strip() and "\0" not in message,
                "invalid_message", "A complete initial task message is required")
        require(isinstance(cwd, str) and Path(cwd).is_absolute() and Path(cwd).is_dir(),
                "invalid_workspace", "Launch directory must be an existing absolute directory")
        try:
            template, template_path = self._template(profile)
        except OSError as error:
            raise LoopError("template_unavailable", "Cannot create the managed launch template") from error
        arguments = ["run", "--json", "--bg", "--no-parent", "--no-notify", "--name", assignment["session_name"],
                     "--cwd", cwd, "--label", "ht_run=" + assignment["run_id"],
                     "--label", "ht_assignment=" + assignment["id"],
                     "--label", "ht_workspace=" + assignment["workspace_id"], template, message]
        try:
            observation = self._json(arguments)
            terminal = observation.get("address")
            require(isinstance(terminal, str) and terminal, "runtime_protocol", "Herdr launch omitted its address")
            first_message = observation.get("first_message")
            require(isinstance(first_message, dict) and isinstance(first_message.get("sent"), bool),
                    "runtime_protocol", "Herdr launch did not establish first-message delivery", terminal=terminal)
        except LoopError as error:
            raise LoopError("delivery_unknown", "Launch outcome is uncertain; reconcile this assignment before retrying",
                            details={**error.details, "cause": error.code, "template_path": template_path}) from error
        return {"terminal": terminal, "first_message_sent": first_message["sent"],
                "observation": observation, "template_path": template_path}

    def observe(self, terminal) -> dict:
        return self._json(["get", "--json", self._terminal(terminal)])

    def reconcile(self, assignment) -> dict | None:
        self._assignment(assignment)
        response = self._json(["ls", "--json", "--all", "--label", "ht_run=" + assignment["run_id"],
                               "--label", "ht_assignment=" + assignment["id"]])
        terminals = response.get("terminals")
        require(isinstance(terminals, list), "runtime_protocol", "Herdr list omitted terminals")
        require(len(terminals) <= 1, "ambiguous_execution", "Multiple terminals match this assignment")
        if not terminals:
            return None
        require(isinstance(terminals[0], dict), "runtime_protocol", "Invalid terminal list entry")
        terminal = self._terminal(terminals[0].get("address"))
        observation = self.observe(terminal)
        expected = {"ht_run": assignment["run_id"], "ht_assignment": assignment["id"]}
        labels = observation.get("labels", {})
        require(isinstance(labels, dict) and all(labels.get(key) == value for key, value in expected.items()),
                "runtime_protocol", "Observed terminal does not match assignment labels")
        first_message = observation.get("first_message")
        known = isinstance(first_message, dict) and isinstance(first_message.get("sent"), bool)
        return {"terminal": terminal, "first_message_sent": first_message["sent"] if known else False,
                "delivery_known": known, "observation": observation}

    def wait(self, terminal, timeout=30) -> dict:
        require(isinstance(timeout, (int, float)) and math.isfinite(timeout) and timeout > 0,
                "invalid_runtime", "Wait timeout must be positive and finite")
        return self._json(["wait", "--json", "--timeout", str(timeout), self._terminal(terminal)],
                          timeout=max(self.timeout, timeout + 5))

    def read(self, terminal, lines=80) -> str:
        require(isinstance(lines, int) and lines >= 0, "invalid_runtime", "Read lines must be nonnegative")
        return self._run(["read", "--lines", str(lines), self._terminal(terminal)]).stdout

    def send(self, terminal, message) -> dict:
        require(isinstance(message, str) and message.strip() and "\0" not in message,
                "invalid_message", "A nonempty message is required")
        try:
            return self._json(["send", "--json", self._terminal(terminal), message])
        except LoopError as error:
            if error.code in ("runtime_timeout", "runtime_protocol"):
                raise LoopError("delivery_unknown", "Message delivery is uncertain; inspect before retrying",
                                details={"terminal": terminal}) from error
            raise

    def interrupt(self, terminal) -> dict:
        return self._json(["interrupt", "--json", self._terminal(terminal)])

    def terminate(self, terminal) -> dict:
        return self._json(["kill", "--json", self._terminal(terminal)])

    def attach(self, terminal) -> int:
        try:
            return subprocess.run([self.executable, "attach", self._terminal(terminal)], env=self.env).returncode
        except OSError as error:
            raise LoopError("runtime_unavailable", "Cannot attach to Herdr terminal") from error
