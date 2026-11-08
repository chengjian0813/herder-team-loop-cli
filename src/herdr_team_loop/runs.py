"""Application orchestration: semantic judgments remain explicit coordinator inputs."""
import json
import math
import os
from pathlib import Path
import time

from .assignments import AssignmentService
from .bugs import BugService
from .domain import Budget, LIVE_ASSIGNMENTS, TERMINAL_RUNS, active_seconds, authorize, check_budget
from .errors import LoopError, require
from .fingerprint import fingerprint
from .reports import ReportService
from .runtime import HerdrRuntime
from .workspace import WorkspaceService


class RunService:
    def __init__(self, store, runtime=None):
        self.store = store
        self.runtime = runtime or HerdrRuntime()
        self.workspaces = WorkspaceService(store)
        self.bugs = BugService(store)
        self.assignments = AssignmentService(store, self.runtime)
        self.reports = ReportService(store)

    def require_coordinator(self, run_id: str, actor: str | None) -> dict:
        require(actor, "actor_required", "This operation requires the current coordinator assignment ID")
        run = self.store.get("runs", run_id)
        assignment = self.store.get("assignments", actor)
        require(assignment["run_id"] == run_id and assignment["slot"] == "coordinator"
                and actor == run.get("coordinator_id") and assignment["epoch"] == run["coordinator_epoch"]
                and assignment["status"] == "RUNNING",
                "not_coordinator", "Only the current claimed coordinator may advance this run")
        require(run["status"] not in TERMINAL_RUNS, "terminal_run", "Run has already ended")
        return run

    def _event(self, run: dict, kind: str, **data) -> None:
        self.store.event(kind, workspace_id=run["workspace_id"], requirement_id=run["requirement_id"], run_id=run["id"], data=data)

    def _current_input(self, run: dict) -> dict:
        self.store.read_artifact(run["scope_artifact_id"])
        return fingerprint(run["repos"], run["baseline"], run["scope_digest"])

    def _workers(self, run_id: str) -> list[dict]:
        return self.store.list("assignments", run_id=run_id, slot="worker", status=LIVE_ASSIGNMENTS)

    def _profile(self, run: dict, role: str, profile_id: str | None = None) -> dict:
        binding = run["roles"].get(role)
        require(binding, "role_missing", f"Configure the {role} role before starting this mode")
        selected = profile_id or binding["primary"]
        require(selected in [binding["primary"], *binding.get("fallbacks", [])], "profile_not_allowed", "Profile is not in this run's frozen role configuration")
        profile = run["profiles"].get(selected)
        require(profile is not None, "profile_missing", "Run has no frozen launch specification")
        return profile

    def start(self, workspace_id: str, requirement_id: str, *, mode: str, repos: list[str],
              environment="local", baseline="master", authorization=None, repair_policy="report-only", max_rounds=3,
              active_minutes=120, no_progress_limit=2, deadline=None, attach_terminal=None, resources=None) -> dict:
        require(mode in ("develop", "e2e", "review"), "invalid_mode", "Mode must be develop, e2e or review")
        require(repair_policy in ("report-only", "guarded", "confirm-every-fix"), "invalid_policy", "Unknown repair policy")
        require(isinstance(environment, str) and environment.strip(), "invalid_environment", "Environment must be explicit")
        budget = Budget(max_rounds, active_minutes, no_progress_limit)
        budget.validate()
        require(deadline is None or isinstance(deadline, (int, float)) and math.isfinite(deadline) and deadline > time.time(),
                "invalid_budget", "Absolute deadline must be a finite future timestamp")
        authorization = authorization or {"source": "Explicit CLI test/review request", "code_fix": False, "deploy": False}
        require(isinstance(authorization, dict) and isinstance(authorization.get("source"), str) and authorization["source"].strip(),
                "invalid_authorization", "Authorization source is required")
        for permission in ("code_fix", "deploy"):
            require(isinstance(authorization.get(permission, False), bool), "invalid_authorization", "Permissions must be booleans")
        require(repos and isinstance(repos, list), "repositories_required", "Select actual target Git roots with --repo")
        repos = sorted({str(Path(path).expanduser().resolve()) for path in repos})
        for repo in repos:
            require(not self.store.root.is_relative_to(Path(repo)), "data_inside_repository", "Keep CLI runtime data outside target code repositories")
        require(isinstance(resources or [], list) and all(isinstance(item, str) and item.strip() for item in resources or []),
                "invalid_resources", "Resources must be nonempty names")
        workspace = self.store.get("workspaces", workspace_id)
        requirement = self.workspaces.get_requirement(workspace_id, requirement_id)
        self.store.read_artifact(requirement["scope_artifact_id"])
        profiles = {profile["id"]: profile for profile in self.workspaces.profiles()}
        required_roles = {"coordinator", "reviewer" if mode == "review" else "tester"}
        if mode == "develop" or repair_policy != "report-only":
            required_roles.update(("developer", "reviewer", "tester"))
        for role in required_roles:
            require(role in workspace["roles"], "role_missing", f"Missing workspace role: {role}")
        inputs = fingerprint(repos, baseline, requirement["scope_digest"])
        phase = {"develop": "plan", "e2e": "environment", "review": "review"}[mode]
        with self.store.transaction():
            run = self.store.create("runs", {
                "workspace_id": workspace_id, "requirement_id": requirement_id, "status": "ACTIVE", "phase": phase,
                "mode": mode, "environment": environment, "repair_policy": repair_policy, "authorization": authorization,
                "scope_revision": requirement["scope_revision"], "scope_digest": requirement["scope_digest"],
                "scope_artifact_id": requirement["scope_artifact_id"], "repos": repos, "baseline": baseline,
                "roles": workspace["roles"], "config_revision": workspace["config_revision"], "profiles": profiles,
                "round": 0, "max_rounds": max_rounds, "no_progress_limit": no_progress_limit, "failures": {},
                "active_seconds": 0.0, "active_since": time.time(), "active_limit_seconds": active_minutes * 60,
                "deadline": deadline, "coordinator_epoch": 1, "fingerprint": inputs, "review_fingerprint": None,
                "full_cycle": mode == "develop", "environment_revision": 0, "repair_bug_ids": [],
                "resources": ["repo:" + path for path in repos] + ["test:" + value for value in resources or []],
            })
            if mode == "develop" or repair_policy != "report-only":
                authorize(run, "code_fix")
            self.bugs.link_history(run)
            coordinator = self.assignments.reserve(run, "coordinator", self._profile(run, "coordinator"), inputs, phase="coordinate", slot="coordinator")
            self.store.update("runs", run["id"], {"coordinator_id": coordinator["id"]})
            self._event(run, "run_started", mode=mode, coordinator=coordinator["id"])
        if attach_terminal:
            self.assignments.attach_coordinator(coordinator["id"], attach_terminal)
        else:
            self.assignments.launch(coordinator["id"])
        return self.status(run["id"])

    def status(self, run_id: str, workspace_id: str | None = None) -> dict:
        run = self.store.get("runs", run_id, workspace_id)
        return {**run, "active_elapsed_seconds": active_seconds(run),
                "assignments": self.store.list("assignments", run_id=run_id), "bugs": self.bugs.list(run_id),
                "pending_decisions": self.bugs.pending(run_id)}

    def next(self, run_id: str, actor: str) -> dict:
        run = self.require_coordinator(run_id, actor)
        workers = self._workers(run_id)
        if workers:
            actions = ["report accept" if workers[0]["status"] == "REPORTED" else "run watch / assignment reconcile"]
        else:
            actions = {"plan": ["run plan"], "develop": ["run dispatch"], "review": ["run dispatch"],
                       "environment": ["run environment"], "test": ["run dispatch"],
                       "triage": ["bug triage", "decision show", "run dispatch --bug BUG_ID", "run pause"],
                       "acceptance": ["run finish"]}[run["phase"]]
        return {"run_id": run_id, "phase": run["phase"], "status": run["status"], "actions": actions,
                "pending_decisions": self.bugs.pending(run_id), "active_workers": workers}

    def dispatch(self, run_id: str, actor: str, bug_ids=None, instructions="", investigate=False) -> dict:
        with self.store.transaction():
            run = self.require_coordinator(run_id, actor)
            require(run["status"] == "ACTIVE", "run_not_ready", "Resume the run before dispatching work")
            require(not self._workers(run_id), "execution_active", "A worker is already assigned; inspect it instead of redispatching")
            check_budget(run)
            current = self._current_input(run)
            require(current == run["fingerprint"], "input_changed", "Inputs changed; explicitly revalidate before dispatch")
            proposals = []
            ids = list(dict.fromkeys(bug_ids or []))
            phase = run["phase"]
            if investigate:
                require(phase == "triage", "invalid_phase", "Investigation is available during triage")
                phase, role = "investigate", "developer"
                instructions = "只读调查，禁止修改业务代码。\n" + instructions
            elif ids:
                require(phase == "triage", "invalid_phase", "Bug fixes must pass through triage")
                proposals = self.bugs.check_fix(run, ids)
                require(run["round"] + 1 <= run["max_rounds"], "budget_exhausted", "Repair round limit reached")
                for bug_id in ids:
                    failure = run["failures"].get(bug_id, {})
                    require(failure.get("count", 0) < run["no_progress_limit"] or failure.get("fingerprint") != current["digest"],
                            "no_progress", "Repeated failure on unchanged input needs investigation or an explicit budget decision", bug_id=bug_id)
                run = self.store.update("runs", run_id, {"round": run["round"] + 1, "phase": "develop", "full_cycle": True,
                                                         "repair_bug_ids": ids, "review_fingerprint": None,
                                                         "environment_evidence_id": None})
                phase, role = "develop", "developer"
            else:
                require(phase in ("develop", "review", "test"), "invalid_phase", "Current phase needs a coordinator decision before dispatch")
                role = {"develop": "developer", "review": "reviewer", "test": "tester"}[phase]
                if phase == "develop":
                    authorize(run, "code_fix")
                if phase == "test":
                    self._check_environment(run, current)
                if run["full_cycle"] and phase == "test":
                    require(run.get("review_fingerprint") == current, "review_required", "Current code needs an accepted independent review")
                ids = run.get("repair_bug_ids", [])
                proposals = [self.store.get("proposals", self.store.get("bugs", item).get("current_proposal_id")) for item in ids]
            self.store.acquire_resources(run_id, run["resources"])
            assignment = self.assignments.reserve(run, role, self._profile(run, role), current, phase=phase,
                                                   bug_ids=ids, proposals=proposals, instructions=instructions)
            self._event(run, "work_dispatched", assignment_id=assignment["id"], phase=phase, bug_ids=ids)
        return self.assignments.launch(assignment["id"])

    def record_plan(self, run_id: str, actor: str, plan: str) -> dict:
        require(isinstance(plan, str) and plan.strip(), "missing_plan", "Plan or justified lightweight implementation scope is required")
        with self.store.transaction():
            run = self.require_coordinator(run_id, actor)
            require(run["status"] == "ACTIVE", "run_not_ready", "Run is not accepting a plan")
            require(run["phase"] == "plan" and not self._workers(run_id), "invalid_phase", "Run is not waiting for a plan")
            artifact = self.store.save_artifact(run["workspace_id"], requirement_id=run["requirement_id"], run_id=run_id, kind="plans", content=plan)
            self._event(run, "plan_recorded", artifact_id=artifact["id"])
            return self.store.update("runs", run_id, {"phase": "develop", "plan_artifact_id": artifact["id"]})

    def confirm_environment(self, run_id: str, actor: str, evidence: dict) -> dict:
        require(isinstance(evidence, dict), "invalid_environment", "Environment evidence must be an object")
        with self.store.transaction():
            run = self.require_coordinator(run_id, actor)
            require(run["status"] == "ACTIVE", "run_not_ready", "Run is not accepting environment changes")
            require(run["phase"] == "environment" and not self._workers(run_id), "invalid_phase", "Environment can only be confirmed between stages")
            current = self._current_input(run)
            require(current == run["fingerprint"], "input_changed", "Code changed before environment confirmation")
            require(evidence.get("input_fingerprint") == current["digest"], "environment_input_mismatch", "Environment evidence must bind current code inputs")
            require(evidence.get("environment") == run["environment"], "environment_mismatch", "Environment differs from this run")
            require(isinstance(evidence.get("evidence"), list) and evidence["evidence"] and all(isinstance(x, str) and x.strip() for x in evidence["evidence"]),
                    "missing_evidence", "Provide actual environment verification evidence")
            require(isinstance(evidence.get("verified_at"), str) and evidence["verified_at"].strip(), "missing_evidence", "Record the actual verification time")
            if run["environment"] != "local":
                for field in ("project", "config_version"):
                    require(isinstance(evidence.get(field), str) and evidence[field].strip(), "missing_evidence", f"Environment requires {field}")
                require(isinstance(evidence.get("test_objects"), list) and evidence["test_objects"], "missing_evidence", "Test objects must be explicit")
                applications = evidence.get("applications")
                require(isinstance(applications, list) and applications, "missing_deployment", "Record actual deployed applications and source/build mapping")
                for app in applications:
                    require(isinstance(app, dict) and all(isinstance(app.get(field), str) and app[field].strip()
                            for field in ("name", "version", "source_mapping", "deployment_evidence")),
                            "missing_deployment", "Application evidence needs name/version/source_mapping/deployment_evidence")
                if evidence.get("deployment_performed") is True:
                    authorize(run, "deploy")
            artifact = self.store.save_artifact(run["workspace_id"], requirement_id=run["requirement_id"], run_id=run_id,
                                                kind="environment", content=json.dumps(evidence, ensure_ascii=False, indent=2), suffix=".json")
            self._event(run, "environment_confirmed", artifact_id=artifact["id"])
            return self.store.update("runs", run_id, {"phase": "test", "environment_revision": run["environment_revision"] + 1,
                                                       "environment_evidence_id": artifact["id"], "environment_fingerprint": current})

    def _check_environment(self, run: dict, current: dict) -> None:
        require(run.get("environment_evidence_id") and run.get("environment_fingerprint") == current,
                "environment_unverified", "Current code has no matching test environment evidence")
        self.store.read_artifact(run["environment_evidence_id"])

    def accept_report(self, report_id: str, actor: str, rationale: str) -> dict:
        require(isinstance(rationale, str) and rationale.strip(), "acceptance_required", "Coordinator must record independent verification rationale")
        with self.store.transaction():
            report = self.reports.get(report_id)
            run = self.require_coordinator(report["run_id"], actor)
            require(run["status"] == "ACTIVE", "run_not_ready", "Stopped or blocked runs cannot advance through report acceptance")
            assignment = self.store.get("assignments", report["assignment_id"])
            require(report["status"] == "SUBMITTED" and assignment["status"] == "REPORTED", "report_not_pending", "Report was already handled")
            require(assignment["phase"] == run["phase"] or assignment["phase"] == "investigate" and run["phase"] == "triage",
                    "stale_phase", "Report does not match the current phase")
            current = self._current_input(run)
            require(current == report["output_fingerprint"], "input_changed", "Code changed after the worker produced this report")
            for proposal_id in assignment["proposal_ids"]:
                proposal = self.store.get("proposals", proposal_id)
                require(self.store.get("bugs", proposal["bug_id"]).get("current_proposal_id") == proposal_id,
                        "stale_proposal", "Repair proposal changed during execution")
            body = report["report"]
            if body["result"] == "blocked":
                self.store.update("reports", report_id, {"status": "BLOCKED", "rationale": rationale})
                self.store.update("assignments", assignment["id"], {"status": "BLOCKED", "block_reason": body["summary"]})
                return self.store.update("runs", run["id"], {"status": "BLOCKED", "blocked_reason": "external"})
            if assignment["phase"] == "test":
                self._check_environment(run, current)
                require(report["environment_revision"] == run["environment_revision"], "environment_changed", "Environment evidence changed after testing")
            changes = {"fingerprint": current}
            if body["result"] == "fail":
                ids = body.get("bug_ids", [])
                require(ids, "bugs_required", "Business failure must identify recorded Bugs; external failures should be blocked")
                failures = dict(run["failures"])
                for bug_id in ids:
                    self.bugs.reopen(run["id"], bug_id, body["summary"])
                    previous = failures.get(bug_id, {})
                    count = previous.get("count", 0) + 1 if previous.get("fingerprint") == current["digest"] else 1
                    failures[bug_id] = {"count": count, "fingerprint": current["digest"]}
                changes.update(phase="triage", failures=failures, review_fingerprint=None, environment_evidence_id=None)
                if any(failures[item]["count"] >= run["no_progress_limit"] for item in ids):
                    changes.update(status="BLOCKED", blocked_reason="no_progress")
            elif assignment["phase"] == "investigate":
                changes["phase"] = "triage"
            elif assignment["phase"] == "develop":
                changes.update(phase="review", review_fingerprint=None, environment_evidence_id=None)
            elif assignment["phase"] == "review":
                changes.update(phase="environment" if run["full_cycle"] else "acceptance", review_fingerprint=current)
            elif assignment["phase"] == "test":
                changes["phase"] = "acceptance"
                # Bug verification is tied to the actual post-repair test inputs, not its pre-repair decision input.
                self.store.update("runs", run["id"], {"fingerprint": current})
                verified = list(dict.fromkeys(body.get("bug_ids", [])))
                if verified:
                    self.bugs.mark_verified(run["id"], verified, report["artifact_id"])
                # One independent repair must not strand other Bugs at final acceptance.
                try:
                    self.bugs.assert_complete(run["id"])
                except LoopError as error:
                    if error.code not in ("unresolved_bugs", "stale_evidence", "stale_proposal", "stale_scope", "needs_decision"):
                        raise
                    changes["phase"] = "triage"
                changes.update(test_fingerprint=current, test_environment_revision=run["environment_revision"])
            self.store.update("reports", report_id, {"status": "ACCEPTED", "rationale": rationale, "accepted_by": actor})
            self.store.update("assignments", assignment["id"], {"status": "ACCEPTED", "ended_at": time.time()})
            self._event(run, "report_accepted", report_id=report_id, result=body["result"], rationale=rationale)
            return self.store.update("runs", run["id"], changes)

    def finish(self, run_id: str, actor: str, summary: str) -> dict:
        require(isinstance(summary, str) and summary.strip(), "acceptance_required", "Final acceptance summary is required")
        with self.store.transaction():
            run = self.require_coordinator(run_id, actor)
            require(run["status"] == "ACTIVE", "run_not_ready", "Stopped or blocked runs cannot be marked complete")
            require(run["phase"] == "acceptance" and not self._workers(run_id), "acceptance_not_ready", "Required stages have not been accepted")
            current = self._current_input(run)
            require(current == run["fingerprint"], "input_changed", "Inputs changed before final acceptance")
            self.bugs.assert_complete(run_id)
            if run["mode"] != "review" or run["full_cycle"]:
                self._check_environment(run, current)
                require(run.get("test_fingerprint") == current and run.get("test_environment_revision") == run["environment_revision"],
                        "test_required", "Current inputs and environment have no accepted passing test")
            if run["full_cycle"] or run["mode"] == "review":
                require(run.get("review_fingerprint") == current, "review_required", "Current inputs have no accepted review")
            result = self.store.update("runs", run_id, {"status": "DONE", "summary": summary, "ended_at": time.time(),
                                                         "active_seconds": active_seconds(run), "active_since": None})
            self.store.release_resources(run_id)
            self._event(run, "run_completed", summary=summary)
            return result

    def pause(self, run_id: str, actor: str, reason: str) -> dict:
        require(isinstance(reason, str) and reason.strip(), "reason_required", "Pause reason is required")
        with self.store.transaction():
            run = self.require_coordinator(run_id, actor)
            require(not self._workers(run_id), "execution_active", "Cannot pause the execution budget while a worker is active")
            require(self.bugs.pending(run_id), "no_pending_decision", "Budget pause requires a pending user decision")
            for bug in self.bugs.list(run_id):
                try:
                    self.bugs.check_fix(run, [bug["id"]])
                except LoopError:
                    continue
                raise LoopError("work_available", "An independent authorized fix can still proceed")
            self._event(run, "waiting_for_user", reason=reason)
            return self.store.update("runs", run_id, {"status": "WAITING_DECISION", "active_seconds": active_seconds(run),
                                                       "active_since": None, "pause_reason": reason})

    def resume(self, run_id: str, actor: str, checkpoint="", operations_checked=False, old_owner_stopped=False, revalidate=False) -> dict:
        with self.store.transaction():
            run = self.require_coordinator(run_id, actor)
            require(run["status"] in ("BLOCKED", "WAITING_DECISION", "ACTIVE"), "cannot_resume", "Run cannot be resumed")
            require(not self._workers(run_id), "execution_active", "Retire or hand off the old worker before resuming")
            check_budget(run)
            current = self._current_input(run)
            changes = {"status": "ACTIVE", "blocked_reason": None, "active_since": run.get("active_since") or time.time()}
            if current != run["fingerprint"]:
                require(revalidate and checkpoint.strip() and operations_checked and old_owner_stopped,
                        "revalidation_required", "Changed inputs require a checkpoint and confirmed operation/owner checks")
                changes.update(fingerprint=current, review_fingerprint=None, environment_evidence_id=None, test_fingerprint=None)
                changes["phase"] = "review" if run["full_cycle"] or run["mode"] == "review" else "environment"
            elif revalidate:
                require(checkpoint.strip() and operations_checked and old_owner_stopped,
                        "revalidation_required", "Additional validation requires a checkpoint and confirmed operation/owner checks")
                # Retest omitted cases without consuming another repair round or
                # discarding an unchanged, already accepted independent review.
                changes["test_fingerprint"] = None
                changes["phase"] = ("review" if run["mode"] == "review" and not run["full_cycle"] else
                                    "test" if run.get("environment_evidence_id") and run.get("environment_fingerprint") == current else "environment")
            if run.get("blocked_reason") == "no_progress" and current == run["fingerprint"]:
                raise LoopError("no_progress", "Unchanged input cannot bypass the no-progress boundary")
            self._event(run, "run_resumed", revalidated=current != run["fingerprint"], checkpoint=checkpoint)
            return self.store.update("runs", run_id, changes)

    def terminate_assignment(self, assignment_id: str, actor: str, checkpoint: str) -> dict:
        assignment = self.store.get("assignments", assignment_id)
        self.require_coordinator(assignment["run_id"], actor)
        require(assignment["slot"] == "worker" and assignment["status"] in LIVE_ASSIGNMENTS,
                "not_active_worker", "Only an active managed worker can be terminated here")
        require(assignment["status"] != "REPORTED", "report_pending", "Inspect the pending report before terminating its worker")
        self.assignments.block(assignment_id, "Termination requested; operation outcomes need verification", checkpoint)
        result = self.assignments.stop_terminal(assignment_id, finalize=False)
        self._event(self.store.get("runs", assignment["run_id"]), "worker_terminated", assignment_id=assignment_id)
        return result

    def retire_assignment(self, assignment_id: str, actor: str, reason: str, checkpoint: str, operations_checked=False) -> dict:
        assignment = self.store.get("assignments", assignment_id)
        run = self.require_coordinator(assignment["run_id"], actor)
        require(assignment["slot"] == "worker" and assignment["status"] in LIVE_ASSIGNMENTS, "not_active_worker", "Select the active worker")
        require(operations_checked and reason.strip() and checkpoint.strip(), "handoff_checks_required", "Retirement requires operation checks and a checkpoint")
        self.assignments.verify_stopped(assignment)
        with self.store.transaction():
            artifact = self.store.save_artifact(run["workspace_id"], requirement_id=run["requirement_id"], run_id=run["id"],
                                                kind="checkpoints", content=checkpoint)
            result = self.store.update("assignments", assignment_id, {"status": "STOPPED", "ended_at": time.time(), "checkpoint_id": artifact["id"]})
            self._event(run, "assignment_retired", assignment_id=assignment_id, reason=reason)
            return result

    def handoff(self, run_id: str, actor: str, checkpoint: str, operations_checked=False) -> dict:
        run = self.require_coordinator(run_id, actor)
        workers = self._workers(run_id)
        require(len(workers) == 1 and operations_checked and checkpoint.strip(), "handoff_checks_required", "Handoff requires one worker, checked operations and checkpoint")
        old = workers[0]
        require(old["status"] != "REPORTED", "report_pending", "Accept or explicitly retire the pending report before handoff")
        self.assignments.verify_stopped(old)
        with self.store.transaction():
            run = self.require_coordinator(run_id, actor)
            check_budget(run)
            current = self._current_input(run)
            require(current == old["fingerprint"], "input_changed", "Cannot transfer a checkpoint across changed code inputs")
            choices = [run["roles"][old["role"]]["primary"], *run["roles"][old["role"]].get("fallbacks", [])]
            index = choices.index(old["agent"]) + 1
            require(index < len(choices), "fallback_exhausted", "No configured fallback remains")
            artifact = self.store.save_artifact(run["workspace_id"], requirement_id=run["requirement_id"], run_id=run_id,
                                                kind="checkpoints", content=checkpoint)
            self.store.update("assignments", old["id"], {"status": "REPLACED", "ended_at": time.time(), "checkpoint_id": artifact["id"]})
            proposals = [self.store.get("proposals", item) for item in old["proposal_ids"]]
            replacement = self.assignments.reserve(run, old["role"], self._profile(run, old["role"], choices[index]), current,
                                                    phase=old["phase"], bug_ids=old["bug_ids"], proposals=proposals,
                                                    instructions=f"从检查点继续，不重放已完成操作。检查点：{self.store.root / artifact['path']}")
            self.store.update("runs", run_id, {"status": "ACTIVE", "blocked_reason": None})
            self._event(run, "worker_handoff", old_assignment=old["id"], new_assignment=replacement["id"])
        return self.assignments.launch(replacement["id"])

    def takeover(self, run_id: str, *, terminal=None, old_pm_stopped=False, checkpoint="", reason="") -> dict:
        require(old_pm_stopped and checkpoint.strip() and reason.strip(), "takeover_checks_required", "Confirm the old coordinator stopped scheduling and record a checkpoint/reason")
        with self.store.transaction():
            run = self.store.get("runs", run_id)
            require(run["status"] not in TERMINAL_RUNS, "terminal_run", "Ended run cannot be taken over")
            artifact = self.store.save_artifact(run["workspace_id"], requirement_id=run["requirement_id"], run_id=run_id,
                                                kind="checkpoints", content=checkpoint)
            old = self.store.get("assignments", run["coordinator_id"])
            self.store.update("assignments", old["id"], {"status": "REPLACED", "ended_at": time.time()})
            run = self.store.update("runs", run_id, {"coordinator_epoch": run["coordinator_epoch"] + 1})
            new = self.assignments.reserve(run, "coordinator", self._profile(run, "coordinator"), run["fingerprint"],
                                           phase="coordinate", slot="coordinator", instructions=f"接管原运行并保留worker。检查点：{self.store.root / artifact['path']}")
            self.store.update("runs", run_id, {"coordinator_id": new["id"]})
            self._event(run, "coordinator_replaced", old_assignment=old["id"], new_assignment=new["id"], reason=reason)
        if terminal:
            self.assignments.attach_coordinator(new["id"], terminal)
        else:
            self.assignments.launch(new["id"])
        return self.status(run_id)

    def stop(self, run_id: str, reason: str, terminate=False, operations_checked=False, actor=None) -> dict:
        require(reason.strip(), "reason_required", "Stop reason is required")
        with self.store.transaction():
            run = self.require_coordinator(run_id, actor) if actor else self.store.get("runs", run_id)
            require(run["status"] not in TERMINAL_RUNS, "terminal_run", "Run already ended")
            self.store.update("runs", run_id, {"status": "STOPPING", "stop_reason": reason})
            self._event(run, "stop_requested", reason=reason, terminate=terminate)
        if terminate:
            owned = self.store.list("assignments", run_id=run_id, status=LIVE_ASSIGNMENTS)
            for assignment in sorted(owned, key=lambda item: item["slot"] == "coordinator"):
                if assignment.get("attached") or assignment.get("terminal") == os.environ.get("HC_TERMINAL_ID"):
                    continue
                self.assignments.stop_terminal(assignment["id"])
        workers = self._workers(run_id)
        if workers:
            return self.status(run_id)
        # A kill confirms process termination, not the outcome of remote business operations.
        if terminate and not operations_checked:
            return self.status(run_id)
        with self.store.transaction():
            run = self.store.get("runs", run_id)
            result = self.store.update("runs", run_id, {"status": "STOPPED", "ended_at": time.time(),
                                                         "active_seconds": active_seconds(run), "active_since": None})
            self.store.release_resources(run_id)
            self._event(run, "run_stopped", operations_checked=operations_checked)
            return result

    def watch(self, run_id: str, after=0, timeout=30) -> dict:
        require(isinstance(timeout, (int, float)) and math.isfinite(timeout) and 0 <= timeout <= 60,
                "invalid_timeout", "Watch timeout must be between 0 and 60 seconds")
        state = self.status(run_id)
        events = self.store.events(run_id, after)
        if not events and not state["pending_decisions"] and state["status"] not in TERMINAL_RUNS and timeout:
            workers = self._workers(run_id)
            if workers and workers[0].get("terminal"):
                self.runtime.wait(workers[0]["terminal"], timeout=timeout)
                state, events = self.status(run_id), self.store.events(run_id, after)
        return {"run": state, "events": events, "cursor": events[-1]["seq"] if events else after,
                "needs_user": bool(state["pending_decisions"])}

    def attach(self, run_id: str) -> int:
        run = self.store.get("runs", run_id)
        coordinator = self.store.get("assignments", run["coordinator_id"])
        require(coordinator.get("terminal"), "terminal_unknown", "Reconcile the coordinator assignment first")
        return self.runtime.attach(coordinator["terminal"])
