"""Structured command entrypoint; application services own lifecycle decisions."""
import argparse
import json
import math
from pathlib import Path
import sqlite3
import sys

from .bugs import BugService, CLASSIFICATIONS
from .domain import ROLES
from .errors import LoopError, require
from .storage import Store
from .workspace import WorkspaceService


NEEDS_INPUT = {"needs_input", "needs_decision", "needs_configuration", "workspace_not_registered",
               "agent_unavailable", "missing_role", "missing_roles", "role_missing", "roles_missing",
               "role_unconfigured", "not_authorized", "actor_required"}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise LoopError("invalid_arguments", message)


def _common_parser():
    parser = _Parser(add_help=False, allow_abbrev=False)
    parser.add_argument("--data-dir")
    parser.add_argument("--workspace")
    parser.add_argument("--actor")
    parser.add_argument("--json", action="store_true")
    return parser


def _build_parser():
    parser = _Parser(prog="herdr-team-loop-cli", allow_abbrev=False,
                     description="Workspace-scoped agent teams through one durable CLI protocol.",
                     epilog="Global options --data-dir PATH, --workspace PATH, --actor ASSIGNMENT_ID and --json may appear before or after the command.")
    groups = parser.add_subparsers(dest="command", required=True)
    help_parsers = {(): parser}

    def group(name, description):
        entry = groups.add_parser(name, help=description, description=description, allow_abbrev=False)
        help_parsers[(name,)] = entry
        return entry, entry.add_subparsers(dest="action", required=True)

    def command(name, collection, action, description):
        entry = collection.add_parser(action, help=description, description=description, allow_abbrev=False)
        help_parsers[(name, action)] = entry
        return entry

    def run_id(entry):
        entry.add_argument("--run", required=True)

    def assignment_id(entry):
        entry.add_argument("--assignment", required=True)

    def text_or_file(entry, name, *, required=False, default=""):
        choice = entry.add_mutually_exclusive_group(required=required)
        choice.add_argument("--" + name, default=default)
        choice.add_argument("--" + name + "-file")

    entry = groups.add_parser("init", help="Inspect or explicitly configure this physical workspace", allow_abbrev=False)
    entry.add_argument("--roles-file")
    help_parsers[("init",)] = entry
    _, actions = group("agents", "Passively discover and register agent profiles")
    command("agents", actions, "discover", "List executable candidates without starting them")
    entry = command("agents", actions, "register", "Register an explicit agent launch specification")
    entry.add_argument("--profile-file", required=True)
    _, actions = group("roles", "Read or change workspace role bindings")
    command("roles", actions, "show", "Read the current role mapping")
    entry = command("roles", actions, "set", "Create a new workspace configuration revision")
    entry.add_argument("--role", choices=ROLES, required=True)
    entry.add_argument("--agent", required=True)
    entry.add_argument("--fallback", action="append", default=[])
    _, actions = group("workspace", "Manage registered physical workspace identities")
    command("workspace", actions, "list", "List registered workspaces")
    entry = command("workspace", actions, "relocate", "Rebind a workspace with no active runs")
    entry.add_argument("--workspace-id", required=True)
    entry.add_argument("--new-path", required=True)
    entry = command("workspace", actions, "export", "Export scoped records and verified artifact contents")
    entry.add_argument("--workspace-id")
    _, actions = group("requirement", "Create and revise explicit requirement scope")
    entry = command("requirement", actions, "create", "Create one requirement; titles are not identities")
    entry.add_argument("--title", required=True)
    entry.add_argument("--scope-file", required=True)
    entry.add_argument("--source")
    command("requirement", actions, "list", "List this workspace's requirements")
    entry = command("requirement", actions, "show", "Read a requirement and its current scope")
    entry.add_argument("--requirement", required=True)
    entry = command("requirement", actions, "revise", "Save a new scope version without changing active runs")
    entry.add_argument("--requirement", required=True)
    entry.add_argument("--scope-file", required=True)
    entry.add_argument("--reason", required=True)

    _, actions = group("run", "Start, observe and govern a bounded run")
    entry = command("run", actions, "start", "Start one run with explicit configuration and authorization")
    entry.add_argument("--requirement", required=True)
    entry.add_argument("--mode", choices=("develop", "test", "review", "e2e"), required=True)
    entry.add_argument("--environment", default="local")
    entry.add_argument("--repo", action="append", default=[])
    entry.add_argument("--baseline", default="master")
    entry.add_argument("--authorization-file")
    entry.add_argument("--repair-policy", choices=("report-only", "guarded", "confirm-every-fix"), default="report-only")
    entry.add_argument("--max-rounds", type=int, default=3)
    entry.add_argument("--active-minutes", type=float, default=120)
    entry.add_argument("--no-progress-limit", type=int, default=2)
    entry.add_argument("--deadline", type=float, help="Absolute Unix timestamp; does not reset on resume")
    entry.add_argument("--attach-terminal")
    entry.add_argument("--resource", action="append", default=[])
    entry = command("run", actions, "extend-budget", "Record an authorized increase of total active time without resetting progress")
    run_id(entry)
    entry.add_argument("--active-minutes", type=float, required=True, help="New total active-time limit, not extra minutes")
    entry.add_argument("--reason", required=True)
    entry.add_argument("--authorization-source", required=True)
    for action, description in (("status", "Read current state"), ("next", "Read the coordinator's permitted next action"),
                                ("attach", "Return the original coordinator terminal")):
        run_id(command("run", actions, action, description))
    entry = command("run", actions, "dispatch", "Reserve and dispatch the next permitted assignment")
    run_id(entry)
    entry.add_argument("--bug", action="append", default=[])
    text_or_file(entry, "instructions")
    entry.add_argument("--investigate", action="store_true")
    entry = command("run", actions, "watch", "Wait for bounded run events")
    run_id(entry)
    entry.add_argument("--after", type=int, default=0)
    entry.add_argument("--timeout", type=float, default=30)
    entry = command("run", actions, "plan", "Record the coordinator's scoped execution plan")
    run_id(entry)
    entry.add_argument("--plan-file", required=True)
    entry = command("run", actions, "environment", "Record actual environment and deployment evidence")
    run_id(entry)
    entry.add_argument("--evidence-file", required=True)
    entry = command("run", actions, "finish", "Accept a run after its evidence and Bug gates pass")
    run_id(entry)
    text_or_file(entry, "summary", required=True)
    entry = command("run", actions, "pause", "Pause further work without resetting the run budget")
    run_id(entry)
    entry.add_argument("--reason", required=True)
    entry = command("run", actions, "resume", "Resume the same run with explicit recovery evidence")
    run_id(entry)
    text_or_file(entry, "checkpoint")
    entry.add_argument("--operations-checked", action="store_true")
    entry.add_argument("--old-owner-stopped", action="store_true")
    entry.add_argument("--revalidate", action="store_true")
    entry = command("run", actions, "handoff", "Use a configured fallback only after checking the old execution")
    run_id(entry)
    text_or_file(entry, "checkpoint", required=True)
    entry.add_argument("--operations-checked", action="store_true")
    entry = command("run", actions, "takeover", "Replace only the coordinator identity, preserving workers")
    run_id(entry)
    entry.add_argument("--terminal")
    entry.add_argument("--old-pm-stopped", action="store_true")
    text_or_file(entry, "checkpoint", required=True)
    entry.add_argument("--reason", required=True)
    entry = command("run", actions, "stop", "Stop dispatch and optionally terminate this run's terminals")
    run_id(entry)
    entry.add_argument("--reason", required=True)
    entry.add_argument("--terminate", action="store_true")
    entry.add_argument("--operations-checked", action="store_true")

    _, actions = group("assignment", "Read and report one bound worker assignment")
    for action, description in (("context", "Read the assignment's frozen inputs"), ("claim", "Claim this delivered assignment"),
                                ("reconcile", "Reconcile the original dispatch; never relaunch it"),
                                ("observe", "Read the original terminal's current state and bounded output")):
        assignment_id(command("assignment", actions, action, description))
    entry = command("assignment", actions, "confirm-delivery", "Record observed evidence that the original task was delivered")
    assignment_id(entry)
    entry.add_argument("--evidence", required=True)
    entry = command("assignment", actions, "deliver", "Deliver the original task only when initial delivery is proven unsent")
    assignment_id(entry)
    text_or_file(entry, "ready-evidence")
    entry.add_argument("--ready-turn", type=int, help="Observed original terminal turn; required with manual readiness evidence when status is null")
    entry = command("assignment", actions, "submit-pending", "Submit the observed original pasted task once without pasting it again")
    assignment_id(entry)
    text_or_file(entry, "evidence", required=True)
    entry.add_argument("--observed-turn", type=int, required=True)
    entry = command("assignment", actions, "terminate", "Coordinator stops one worker; operation outcomes still need checking")
    assignment_id(entry)
    text_or_file(entry, "checkpoint", required=True)
    for action, description in (("block", "Record a blocker and checkpoint"), ("retire", "Coordinator retires a checked, stopped execution")):
        entry = command("assignment", actions, action, description)
        assignment_id(entry)
        entry.add_argument("--reason", required=True)
        text_or_file(entry, "checkpoint", required=True)
        if action == "retire":
            entry.add_argument("--operations-checked", action="store_true")
    _, actions = group("report", "Submit worker evidence and record independent acceptance")
    entry = command("report", actions, "submit", "Submit structured evidence for one assignment")
    assignment_id(entry)
    entry.add_argument("--report-file", "--file", required=True)
    entry = command("report", actions, "show", "Read a saved report")
    entry.add_argument("--report", required=True)
    entry = command("report", actions, "accept", "Coordinator accepts evidence after independent readback")
    entry.add_argument("--report", required=True)
    entry.add_argument("--rationale", required=True)

    _, actions = group("bug", "Track defects, proposal versions and applicability")
    entry = command("bug", actions, "create", "Register a finding in a specific run")
    run_id(entry)
    entry.add_argument("--bug-file", help="JSON object with title, evidence, expected, actual and optional stable_key")
    for name in ("title", "evidence", "expected", "actual", "stable-key"):
        entry.add_argument("--" + name)
    run_id(command("bug", actions, "list", "List Bugs linked to this run"))
    entry = command("bug", actions, "triage", "Coordinator records an evidenced proposal and classification")
    run_id(entry)
    entry.add_argument("--bug", required=True)
    entry.add_argument("--classification", choices=sorted(CLASSIFICATIONS), required=True)
    entry.add_argument("--proposal-file", required=True)
    entry.add_argument("--reason", required=True)
    entry = command("bug", actions, "reassess", "Confirm an unchanged proposal still applies to current inputs")
    run_id(entry)
    entry.add_argument("--bug", required=True)
    entry.add_argument("--evidence", required=True)
    entry = command("bug", actions, "applicability", "Assess a historical Bug against this run")
    run_id(entry)
    entry.add_argument("--bug", required=True)
    choice = entry.add_mutually_exclusive_group(required=True)
    choice.add_argument("--applicable", dest="applicable", action="store_true")
    choice.add_argument("--exclude", dest="applicable", action="store_false")
    entry.add_argument("--reason", required=True)
    entry.add_argument("--user-response")
    entry = command("bug", actions, "reopen", "Record failure after a previous disposition or verification")
    run_id(entry)
    entry.add_argument("--bug", required=True)
    entry.add_argument("--reason", required=True)
    _, actions = group("decision", "Read concrete decisions and preserve explicit user answers")
    entry = command("decision", actions, "show", "Read a specific decision and its proposal")
    entry.add_argument("--decision", required=True)
    entry = command("decision", actions, "resolve", "Apply an explicit answer to this exact proposal version")
    entry.add_argument("--decision", required=True)
    entry.add_argument("--option", required=True)
    text_or_file(entry, "response", required=True)
    entry.add_argument("--source", required=True)
    _, actions = group("storage", "Back up the complete local database and referenced artifacts")
    entry = command("storage", actions, "backup", "Write a consistent local backup to a new directory")
    entry.add_argument("--destination", required=True)
    entry = groups.add_parser("protocol", help="Return the machine-readable collaboration protocol", allow_abbrev=False)
    help_parsers[("protocol",)] = entry
    return parser, help_parsers


def _read_text(path: str) -> str:
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError) as error:
        raise LoopError("invalid_file", "Input file must be readable UTF-8 text", details={"path": path}) from error
    require(bool(text.strip()), "invalid_file", "Input file must not be empty", path=path)
    return text


def _read_object(path: str) -> dict:
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate object key")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError("non-finite JSON number")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("non-finite JSON number")
        return number

    text = _read_text(path)
    try:
        value = json.loads(text, object_pairs_hook=unique_pairs, parse_constant=reject_constant, parse_float=finite_float)
    except (ValueError, RecursionError) as error:
        raise LoopError("invalid_file", "Input must contain valid JSON without duplicate keys or non-finite numbers", details={"path": path}) from error
    require(isinstance(value, dict) and bool(value), "invalid_file", "Input must be a nonempty JSON object", path=path)
    return value


def _text_argument(args, name):
    path = getattr(args, name + "_file", None)
    return _read_text(path) if path else getattr(args, name, "")


class _Application:
    def __init__(self, store, args):
        self.store = store
        self.args = args
        self.workspaces = WorkspaceService(store)
        self.bugs = BugService(store)
        self._runs = None
        unscoped = args.command in ("init", "agents", "protocol", "storage")
        self.workspace = self.workspaces.resolve(args.workspace) if args.workspace and not unscoped else None

    @property
    def runs(self):
        if self._runs is None:
            from .runs import RunService
            self._runs = RunService(self.store)
        return self._runs

    def selected_workspace(self):
        return self.workspace or self.workspaces.resolve()

    def object(self, table, object_id):
        return self.store.get(table, object_id, self.workspace["id"] if self.workspace else None)

    def actor_for_run(self, run_id, *, coordinator=False):
        self.object("runs", run_id)
        actor = self.args.actor
        if coordinator:
            require(bool(actor), "actor_required", "This action requires the current coordinator assignment ID via --actor")
            self.runs.require_coordinator(run_id, actor)
        elif actor:
            assignment = self.object("assignments", actor)
            require(assignment["run_id"] == run_id, "actor_mismatch", "Actor is bound to another run")
        return actor

    def assignment(self, assignment_id, *, coordinator=False):
        assignment = self.object("assignments", assignment_id)
        if coordinator:
            self.actor_for_run(assignment["run_id"], coordinator=True)
        elif self.args.actor:
            require(self.args.actor == assignment_id, "actor_mismatch", "A worker may act only on its own assignment")
        return assignment

    def invoke(self):
        args = self.args
        command, action = args.command, getattr(args, "action", None)
        if command == "protocol":
            from .protocol import protocol
            return protocol()
        if command == "agents":
            return self.workspaces.profiles() if action == "discover" else self.workspaces.register_profile(_read_object(args.profile_file))
        if command == "init":
            path = args.workspace or str(Path.cwd())
            if args.roles_file:
                return self.workspaces.initialize(path, _read_object(args.roles_file))
            result = self.workspaces.inspect(path)
            if result["status"] == "needs_configuration" and not args.json and sys.stdin.isatty() and sys.stdout.isatty():
                result = self.interactive_init(path, result)
            return result
        if command == "roles":
            workspace = self.selected_workspace()
            if action == "show":
                return workspace["roles"]
            return self.workspaces.set_role(workspace["path"], args.role, args.agent, args.fallback)
        if command == "workspace":
            if action == "list":
                return [self.workspace] if self.workspace else self.store.list("workspaces")
            workspace = self.object("workspaces", args.workspace_id) if args.workspace_id else self.selected_workspace()
            if action == "relocate":
                return self.workspaces.relocate(workspace["id"], args.new_path)
            return self.workspaces.export_workspace(workspace["id"])
        if command == "requirement":
            if action in ("list", "create"):
                workspace_id = self.selected_workspace()["id"]
                if action == "list":
                    return self.workspaces.list_requirements(workspace_id)
                return self.workspaces.create_requirement(workspace_id, args.title, _read_text(args.scope_file), args.source)
            requirement = self.object("requirements", args.requirement)
            if action == "show":
                return {**requirement, "scope": self.store.read_artifact(requirement["scope_artifact_id"]).decode("utf-8")}
            return self.workspaces.revise_requirement(requirement["workspace_id"], requirement["id"], _read_text(args.scope_file), args.reason)
        if command == "run":
            return self.run_command(action)
        if command == "assignment":
            if action in ("reconcile", "observe", "confirm-delivery", "deliver", "submit-pending"):
                assignment = self.object("assignments", args.assignment)
                if action == "submit-pending":
                    require(not args.actor, "manual_recovery_required", "Submitting an observed pending input requires the initiating operator, not an assignment actor")
                if args.actor and (action in ("confirm-delivery", "deliver") or args.actor != assignment["id"]):
                    self.actor_for_run(assignment["run_id"], coordinator=True)
            else:
                assignment = self.assignment(args.assignment, coordinator=action in ("retire", "terminate"))
            if action == "confirm-delivery":
                return self.runs.assignments.confirm_delivery(assignment["id"], args.evidence)
            if action == "submit-pending":
                return self.runs.assignments.submit_pending(assignment["id"], _text_argument(args, "evidence"), args.observed_turn)
            if action == "deliver":
                ready_evidence = _text_argument(args, "ready_evidence")
                if ready_evidence or args.ready_turn is not None:
                    require(not args.actor, "manual_recovery_required", "Readiness without a status hook requires the initiating operator's observed evidence, not an assignment actor")
                return self.runs.assignments.deliver(assignment["id"], ready_evidence=ready_evidence, ready_turn=args.ready_turn)
            if action == "terminate":
                return self.runs.terminate_assignment(assignment["id"], args.actor, _text_argument(args, "checkpoint"))
            if action == "retire":
                return self.runs.retire_assignment(assignment["id"], args.actor, args.reason, _text_argument(args, "checkpoint"), args.operations_checked)
            if action == "block":
                return self.runs.assignments.block(assignment["id"], args.reason, _text_argument(args, "checkpoint"))
            return getattr(self.runs.assignments, action)(assignment["id"])
        if command == "report":
            if action == "submit":
                self.assignment(args.assignment)
                return self.runs.reports.submit(args.assignment, _read_object(args.report_file))
            report = self.object("reports", args.report)
            if action == "show":
                self.actor_for_run(report["run_id"])
                return self.runs.reports.get(report["id"])
            self.actor_for_run(report["run_id"], coordinator=True)
            return self.runs.accept_report(report["id"], args.actor, args.rationale)
        if command == "bug":
            return self.bug_command(action)
        if command == "decision":
            decision = self.object("decisions", args.decision)
            if action == "show":
                if args.actor:
                    assignment = self.object("assignments", args.actor)
                    require(assignment["workspace_id"] == decision["workspace_id"]
                            and assignment["requirement_id"] == decision["requirement_id"],
                            "actor_mismatch", "Decision is outside the actor's requirement")
                return {**decision, "proposal": self.object("proposals", decision["proposal_id"])}
            if args.actor:
                active = [run for run in self.store.list("runs", workspace_id=decision["workspace_id"],
                                                        requirement_id=decision["requirement_id"])
                          if run["status"] not in ("DONE", "STOPPED")]
                require(len(active) == 1, "no_active_run", "Decision requires one active run in its requirement")
                self.actor_for_run(active[0]["id"], coordinator=True)
            return self.bugs.resolve(decision["id"], args.option, _text_argument(args, "response"), args.source)
        if command == "storage":
            return self.store.backup(Path(args.destination))
        raise LoopError("invalid_arguments", "Unknown command")

    def interactive_init(self, path, result):
        roles = {}
        for question in result["questions"]:
            if not question["choices"]:
                return result
            suffix = "required" if question["required"] else "blank to leave unassigned"
            try:
                choice = input(f"{question['role']} [{', '.join(question['choices'])}] ({suffix}; primary,fallback): ").strip()
            except EOFError:
                return result
            if not choice:
                if question["required"]:
                    return result
                continue
            identifiers = [part.strip() for part in choice.split(",")]
            roles[question["role"]] = {"primary": identifiers[0], "fallbacks": identifiers[1:]}
        return self.workspaces.initialize(path, roles)

    def run_command(self, action):
        args = self.args
        if action == "start":
            requirement = self.object("requirements", args.requirement)
            authorization = _read_object(args.authorization_file) if args.authorization_file else {
                "source": "CLI test request", "code_fix": False, "deploy": False}
            return self.runs.start(requirement["workspace_id"], requirement["id"], mode="e2e" if args.mode == "test" else args.mode,
                                   environment=args.environment, repos=args.repo, baseline=args.baseline,
                                   authorization=authorization, repair_policy=args.repair_policy,
                                   max_rounds=args.max_rounds, active_minutes=args.active_minutes,
                                   no_progress_limit=args.no_progress_limit, deadline=args.deadline,
                                   attach_terminal=args.attach_terminal, resources=args.resource)
        run = self.object("runs", args.run)
        if action == "extend-budget":
            require(not args.actor, "manual_recovery_required", "Only the initiating operator may record an explicitly authorized budget increase")
            return self.runs.extend_budget(run["id"], args.active_minutes, args.reason, args.authorization_source)
        if action in ("status", "watch", "attach"):
            self.actor_for_run(run["id"])
            if action == "status":
                return self.runs.status(run["id"], workspace_id=self.workspace["id"] if self.workspace else None)
            if action == "watch":
                require(args.after >= 0 and 0 <= args.timeout <= 60, "invalid_arguments", "Watch requires after >= 0 and a timeout between 0 and 60 seconds")
                return self.runs.watch(run["id"], after=args.after, timeout=args.timeout)
            if args.json or not sys.stdin.isatty() or not sys.stdout.isatty():
                coordinator = self.object("assignments", run["coordinator_id"])
                require(bool(coordinator.get("terminal")), "terminal_unknown", "Reconcile the coordinator assignment first")
                return {"run_id": run["id"], "assignment_id": coordinator["id"], "terminal": coordinator["terminal"],
                        "attached": False, "next_action": "Run this attach command without --json from an interactive terminal"}
            return {"run_id": run["id"], "attached": True, "exit_code": self.runs.attach(run["id"])}
        if action == "takeover":
            if args.actor:
                self.actor_for_run(run["id"], coordinator=True)
            return self.runs.takeover(run["id"], terminal=args.terminal, old_pm_stopped=args.old_pm_stopped,
                                      checkpoint=_text_argument(args, "checkpoint"), reason=args.reason)
        if action == "stop":
            if args.actor:
                self.actor_for_run(run["id"], coordinator=True)
            return self.runs.stop(run["id"], args.reason, terminate=args.terminate,
                                  operations_checked=args.operations_checked, actor=args.actor)
        self.actor_for_run(run["id"], coordinator=True)
        if action == "next":
            return self.runs.next(run["id"], args.actor)
        if action == "dispatch":
            for bug_id in args.bug:
                self._bug_in_run(bug_id, run)
            return self.runs.dispatch(run["id"], args.actor, bug_ids=args.bug or None,
                                      instructions=_text_argument(args, "instructions"), investigate=args.investigate)
        if action == "plan":
            return self.runs.record_plan(run["id"], args.actor, _read_text(args.plan_file))
        if action == "environment":
            return self.runs.confirm_environment(run["id"], args.actor, _read_object(args.evidence_file))
        if action == "finish":
            return self.runs.finish(run["id"], args.actor, _text_argument(args, "summary"))
        if action == "pause":
            return self.runs.pause(run["id"], args.actor, args.reason)
        if action == "resume":
            return self.runs.resume(run["id"], args.actor, checkpoint=_text_argument(args, "checkpoint"),
                                    operations_checked=args.operations_checked, old_owner_stopped=args.old_owner_stopped,
                                    revalidate=args.revalidate)
        if action == "handoff":
            return self.runs.handoff(run["id"], args.actor, _text_argument(args, "checkpoint"),
                                     operations_checked=args.operations_checked)
        raise LoopError("invalid_arguments", "Unknown run action")

    def _bug_in_run(self, bug_id, run):
        bug = self.object("bugs", bug_id)
        require(bug["workspace_id"] == run["workspace_id"] and bug["requirement_id"] == run["requirement_id"],
                "scope_mismatch", "Bug and run belong to different requirements")
        require(bool(self.store.list("run_bugs", run_id=run["id"], bug_id=bug_id)), "bug_not_linked", "Bug is not linked to this run")
        return bug

    def bug_command(self, action):
        args = self.args
        run = self.object("runs", args.run)
        self.actor_for_run(run["id"], coordinator=action in ("triage", "reassess", "applicability", "reopen"))
        if action == "list":
            return self.bugs.list(run["id"])
        if action == "create":
            supplied = {name: getattr(args, name) for name in ("title", "evidence", "expected", "actual", "stable_key")}
            if args.bug_file:
                require(not any(value is not None for value in supplied.values()), "invalid_arguments", "Use either --bug-file or inline Bug fields")
                supplied = _read_object(args.bug_file)
                require(not set(supplied) - {"title", "evidence", "expected", "actual", "stable_key"}, "invalid_file", "Bug file contains unsupported fields")
            return self.bugs.create(run["id"], *(supplied.get(name) for name in ("title", "evidence", "expected", "actual")),
                                    stable_key=supplied.get("stable_key"))
        bug = self._bug_in_run(args.bug, run)
        if action == "triage":
            # Triage resolves the active run internally; an old run ID must not authorize it.
            active = [item for item in self.store.list("runs", workspace_id=run["workspace_id"], requirement_id=run["requirement_id"])
                      if item["status"] not in ("DONE", "STOPPED")]
            require(len(active) == 1 and active[0]["id"] == run["id"], "scope_mismatch", "Triage must name the current active run")
            return self.bugs.triage(bug["id"], args.classification, _read_object(args.proposal_file), args.reason)
        if action == "reassess":
            return self.bugs.reassess(run["id"], bug["id"], args.evidence)
        if action == "applicability":
            return self.bugs.set_applicability(run["id"], bug["id"], args.applicable, args.reason, args.user_response)
        return self.bugs.reopen(run["id"], bug["id"], args.reason)


def _emit(value, json_mode):
    print(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=None if json_mode else 2))


def _needs_user(data):
    if not isinstance(data, dict):
        return False
    decision = data.get("decision")
    return (data.get("needs_user") is True or bool(data.get("pending_decisions"))
            or isinstance(decision, dict) and decision.get("status") == "PENDING"
            or data.get("status") == "PENDING" and "proposal_id" in data)


def main(argv=None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in raw
    store = None
    try:
        common, remainder = _common_parser().parse_known_args(raw)
        json_mode = common.json
        parser, helps = _build_parser()
        if "--help" in remainder or "-h" in remainder:
            prefix = []
            for word in remainder:
                if word.startswith("-") or len(prefix) == 2:
                    break
                prefix.append(word)
            selected = helps.get(tuple(prefix), helps.get(tuple(prefix[:1]), parser))
            text = selected.format_help()
            if json_mode:
                _emit({"schema_version": 1, "status": "ok", "data": {"help": text}}, True)
            else:
                print(text, end="")
            return 0
        args = parser.parse_args(remainder)
        vars(args).update(vars(common))
        if args.command == "protocol":
            from .protocol import protocol
            data = protocol()
        else:
            store = Store(common.data_dir)
            data = _Application(store, args).invoke()
        status = "needs_configuration" if isinstance(data, dict) and data.get("status") == "needs_configuration" else "ok"
        _emit({"schema_version": 1, "status": status, "data": data}, json_mode)
        return 3 if status == "needs_configuration" or _needs_user(data) else 0
    except LoopError as error:
        needs_input = error.code in NEEDS_INPUT
        _emit({"schema_version": 1, "status": "needs_input" if needs_input else "error",
               "error": {"code": error.code, "message": error.message, "details": error.details}}, json_mode)
        return 3 if needs_input else 2
    except (OSError, sqlite3.Error) as error:
        _emit({"schema_version": 1, "status": "error",
               "error": {"code": "storage_unavailable", "message": "Local state or artifact storage is unavailable", "details": {}}}, json_mode)
        return 2
    except KeyboardInterrupt:
        _emit({"schema_version": 1, "status": "error",
               "error": {"code": "interrupted", "message": "Command interrupted; inspect persisted state before retrying", "details": {}}}, json_mode)
        return 130
    finally:
        if store is not None:
            store.close()
