"""Persist dispatch intent before interacting with external terminals."""
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import time

from .domain import LIVE_ASSIGNMENTS, check_budget
from .errors import LoopError, require
from .fingerprint import fingerprint
from .storage import Store, identifier


class AssignmentService:
    def __init__(self, store: Store, runtime):
        self.store = store
        self.runtime = runtime

    def reserve(self, run: dict, role: str, profile: dict, inputs: dict, *, phase: str,
                bug_ids: list[str] | None = None, proposals: list[dict] | None = None,
                slot: str = "worker", instructions: str = "") -> dict:
        assignment_id = identifier("asg")
        assignment = self.store.create("assignments", {
            "id": assignment_id, "workspace_id": run["workspace_id"], "requirement_id": run["requirement_id"],
            "run_id": run["id"], "slot": slot, "role": role, "agent": profile["id"], "profile": profile,
            "phase": phase, "epoch": run["coordinator_epoch"], "status": "DISPATCHING", "round": run["round"],
            "session_name": "htl-" + assignment_id[4:], "fingerprint": inputs,
            "bug_ids": bug_ids or [], "proposal_ids": [p["id"] for p in proposals or []],
            "terminal": None, "first_message_sent": False, "instructions": instructions,
            "environment_revision": run.get("environment_revision", 0),
            "environment_evidence_id": run.get("environment_evidence_id"),
            "cwd": self.store.get("workspaces", run["workspace_id"])["path"],
        })
        self.store.event("assignment_reserved", workspace_id=run["workspace_id"], requirement_id=run["requirement_id"],
                         run_id=run["id"], assignment_id=assignment_id, data={"role": role, "phase": phase})
        return assignment

    def _command(self, assignment: dict) -> str:
        return shlex.join([sys.executable, "-m", "herdr_team_loop", "--data-dir", str(self.store.root),
                           "--actor", assignment["id"]])

    def message(self, assignment: dict) -> str:
        command = self._command(assignment)
        run = self.store.get("runs", assignment["run_id"])
        if assignment["slot"] == "coordinator":
            role = (
                "你是该运行唯一协调者。先领取上下文，读取任务与项目规则，然后持续通过CLI推进已授权目标。"
                "使用 run next 取得允许的下一步；用 run dispatch 派工，用 run watch 跟进。"
                "实际读取报告后才用 report accept 验收，不能把执行者自报通过当业务通过。"
                "发现Bug用bug create/triage；实质性取舍提交decision，等待用户明确答复。"
                "实现细节可按既定需求自行决定，业务规则、范围或方案实质变化重新提交决定。"
                "在SC测试前提交实际环境/部署证据；部署必须已有授权。"
                "终端和状态由CLI管理，不直接调用hc、不直接写数据库、不另建调度。"
            )
        else:
            roles = {
                "developer": "你负责限定范围内的开发或Bug修复、必要单测。保留无关dirty；发现实质方案变化时停止相关修复并上报。",
                "reviewer": "你负责独立代码审查，不修改业务代码。使用OCR_NO_UPDATE=1 ocr delegate preview/rule对比任务基线，并补审授权dirty和排除项。CLI材料成功不等于审查通过。",
                "tester": "你负责指定环境中的测试和复现，不自行修复业务代码。保存完整证据和在途操作，避免重复副作用；环境/版本不明确时上报阻塞。",
            }
            role = roles.get(assignment["role"], "完成指定的只读调查并报告证据。")
            role += " 团队交互只通过本CLI；不直接调用hc、不改状态文件、不启动其他agent。"
        return (
            f"{role}\n工作区：{assignment['cwd']}\n"
            "先读取存在的AGENTS.md/TEAM.md和目标仓规则；项目团队条件不满足时上报阻塞。"
            "测试工具遵循目标项目约定。不要将SC、某个电脑名称或某个agent品牌当作通用限制。\n"
            f"领取任务：{command} assignment claim --assignment {assignment['id']} --json\n"
            f"查看上下文：{command} assignment context --assignment {assignment['id']} --json\n"
            f"命令协议：{command} protocol --json\n"
            f"运行：{run['id']}，需求：{run['requirement_id']}，阶段：{assignment['phase']}。\n"
            "报告须明确通过/失败/阻塞、覆盖和未覆盖项、实际证据；结果文件按协议提交，勿伪造指纹或省略失败。"
            "报告已提交后等待协调者验收，不自行重做业务操作。\n"
            + assignment.get("instructions", "")
        )

    def launch(self, assignment_id: str) -> dict:
        assignment = self.store.get("assignments", assignment_id)
        require(assignment["status"] == "DISPATCHING", "dispatch_exists", "Dispatch already attempted; reconcile the original execution")
        message = assignment.get("initial_message") or self.message(assignment)
        assignment = self.store.update("assignments", assignment_id, {"initial_message": message})
        try:
            result = self.runtime.launch(assignment, assignment["profile"], message, assignment["cwd"])
        except (LoopError, OSError) as error:
            with self.store.transaction():
                current = self.store.get("assignments", assignment_id)
                if current.get("claimed_at") or current["status"] != "DISPATCHING":
                    return current
                self.store.update("assignments", assignment_id, {"status": "UNKNOWN", "launch_error": str(error)})
                self.store.event("dispatch_unknown", run_id=assignment["run_id"], assignment_id=assignment_id,
                                 workspace_id=assignment["workspace_id"], data={"reason": str(error)})
            raise LoopError("delivery_unknown", "Dispatch result is uncertain; reconcile before taking further action",
                            details={"assignment_id": assignment_id}) from error
        return self._bind(assignment_id, result)

    def _bind(self, assignment_id: str, observation: dict) -> dict:
        with self.store.transaction():
            previous = self.store.get("assignments", assignment_id)
            terminal = observation.get("terminal")
            require(isinstance(terminal, str) and terminal, "delivery_unknown", "Runtime returned no terminal address")
            require(previous.get("terminal") in (None, terminal), "terminal_mismatch", "Existing assignment cannot switch terminals")
            sent = previous.get("first_message_sent", False) or observation.get("first_message_sent") is True
            status = "STARTING" if previous["status"] in ("DISPATCHING", "UNKNOWN") else previous["status"]
            assignment = self.store.update("assignments", assignment_id, {
                "terminal": terminal, "first_message_sent": sent, "status": status,
                "runtime_observation": observation.get("observation", {})})
            self.store.event("assignment_bound", run_id=previous["run_id"], assignment_id=assignment_id,
                             workspace_id=previous["workspace_id"], data={"terminal": terminal, "first_message_sent": sent})
            return assignment

    def attach_coordinator(self, assignment_id: str, terminal: str) -> dict:
        assignment = self.store.get("assignments", assignment_id)
        require(assignment["slot"] == "coordinator", "invalid_attachment", "Only a coordinator can attach an existing session")
        observed = self.runtime.observe(terminal)
        require(observed.get("state") != "finished", "terminal_finished", "Coordinator terminal has already finished")
        process = observed.get("foreground") or observed
        expected = assignment["profile"]["command"]
        argv = process.get("argv")
        expected_executable = shutil.which(expected[0])
        require(isinstance(argv, list) and len(argv) >= len(expected) and expected_executable
                and all(isinstance(value, str) and value for value in argv)
                and Path(argv[0]).is_absolute() and Path(argv[0]).resolve() == Path(expected_executable).resolve()
                and argv[1:len(expected)] == expected[1:],
                "terminal_profile_mismatch", "Cannot prove that this terminal runs the configured coordinator; launch a managed coordinator instead")
        require(isinstance(process.get("cwd"), str) and Path(process["cwd"]).resolve() == Path(assignment["cwd"]).resolve(),
                "terminal_workspace_mismatch", "Coordinator terminal is not in the configured workspace")
        bound = self._bind(assignment_id, {"terminal": terminal, "first_message_sent": True, "observation": observed})
        return self.store.update("assignments", bound["id"], {"status": "RUNNING", "claimed_at": time.time(), "attached": True})

    def context(self, assignment_id: str) -> dict:
        assignment = self.store.get("assignments", assignment_id)
        run = self.store.get("runs", assignment["run_id"])
        workspace = self.store.get("workspaces", assignment["workspace_id"])
        scope = self.store.read_artifact(run["scope_artifact_id"]).decode("utf-8")
        return {"assignment": assignment, "run": run, "workspace": workspace, "scope": scope,
                "proposals": [self.store.get("proposals", item) for item in assignment.get("proposal_ids", [])],
                "report_example": {"result": "pass|fail|blocked", "summary": "actual result", "coverage": ["verified case"],
                                   "not_covered": [], "evidence": ["actual command, output or evidence path"],
                                   "input_fingerprint": assignment["fingerprint"]["digest"], "bug_ids": []}}

    def claim(self, assignment_id: str) -> dict:
        assignment = self.store.get("assignments", assignment_id)
        receiver = os.environ.get("HC_TERMINAL_ID")
        if (receiver and assignment["status"] in ("DISPATCHING", "UNKNOWN", "STARTING")
                and not assignment.get("first_message_sent")):
            # hc can deliver the task before its launch RPC returns. A claim from
            # that exact labelled terminal is positive receipt evidence, not a resend.
            observation = self.runtime.reconcile(assignment)
            require(observation and observation.get("terminal") == receiver,
                    "delivery_unconfirmed", "Claiming terminal does not match the managed assignment")
            self._bind(assignment_id, {**observation, "first_message_sent": True})
        with self.store.transaction():
            assignment = self.store.get("assignments", assignment_id)
            run = self.store.get("runs", assignment["run_id"])
            require(run["status"] not in ("DONE", "STOPPED", "STOPPING"), "run_not_active", "Run is not accepting work")
            require(assignment["status"] in ("STARTING", "RUNNING"), "assignment_not_claimable", "Assignment is not ready for claim")
            require(assignment["first_message_sent"], "delivery_unconfirmed", "First task delivery has not been confirmed")
            if assignment["slot"] == "coordinator":
                require(assignment["epoch"] == run["coordinator_epoch"], "stale_coordinator", "Coordinator has been replaced")
            elif not assignment.get("claimed_at"):
                require(run["status"] == "ACTIVE", "run_not_ready", "Run is not ready for a new worker to begin")
                check_budget(run)
                require(fingerprint(run["repos"], run["baseline"], run["scope_digest"]) == assignment["fingerprint"],
                        "input_changed", "Assignment input changed before work began")
                for proposal_id in assignment["proposal_ids"]:
                    proposal = self.store.get("proposals", proposal_id)
                    require(self.store.get("bugs", proposal["bug_id"]).get("current_proposal_id") == proposal_id,
                            "stale_proposal", "Repair proposal changed before work began")
            return self.store.update("assignments", assignment_id, {"status": "RUNNING", "claimed_at": time.time()})

    def reconcile(self, assignment_id: str) -> dict:
        assignment = self.store.get("assignments", assignment_id)
        require(assignment["status"] in LIVE_ASSIGNMENTS, "assignment_finished", "Assignment is no longer active")
        observed = self.runtime.reconcile(assignment)
        if observed is None:
            return {"status": "needs_attention", "assignment": assignment,
                    "reason": "Original terminal not found; absence is not proof that its operations did not happen"}
        return {"status": "observed", "assignment": self._bind(assignment_id, observed)}

    def observe(self, assignment_id: str) -> dict:
        assignment = self.store.get("assignments", assignment_id)
        require(assignment.get("terminal"), "terminal_unknown", "Reconcile the original dispatch before reading its terminal")
        return {"assignment_id": assignment_id, "terminal": assignment["terminal"],
                "observation": self.runtime.observe(assignment["terminal"]),
                "output": self.runtime.read(assignment["terminal"])}

    def confirm_delivery(self, assignment_id: str, evidence: str) -> dict:
        require(isinstance(evidence, str) and evidence.strip(), "missing_evidence", "Describe how the original task delivery was verified")
        with self.store.transaction():
            assignment = self.store.get("assignments", assignment_id)
            require(assignment["status"] in LIVE_ASSIGNMENTS and assignment.get("terminal"),
                    "assignment_not_claimable", "Reconcile an active assignment before confirming task delivery")
            result = self.store.update("assignments", assignment_id, {"first_message_sent": True, "delivery_evidence": evidence})
            self.store.event("delivery_confirmed", run_id=assignment["run_id"], assignment_id=assignment_id,
                             workspace_id=assignment["workspace_id"], data={"evidence": evidence})
            return result

    def _require_unsent(self, assignment: dict) -> dict:
        run = self.store.get("runs", assignment["run_id"])
        require(run["status"] == "ACTIVE", "run_not_ready", "Run is not accepting task delivery")
        require(assignment["status"] == "STARTING" and assignment.get("terminal")
                and not assignment.get("claimed_at") and not assignment.get("first_message_sent"),
                "delivery_not_retryable", "Only an unclaimed, known-unsent task can be delivered")
        require(not assignment.get("delivery_attempt"), "delivery_not_retryable", "Delivery was already attempted; inspect its outcome instead of resending")
        require(assignment.get("runtime_observation", {}).get("first_message", {}).get("sent") is False,
                "delivery_not_retryable", "Initial delivery is not known to be unsent")
        return self._require_current_assignment(assignment, run)

    def _require_current_assignment(self, assignment: dict, run: dict) -> dict:
        if assignment["slot"] == "coordinator":
            require(assignment["id"] == run.get("coordinator_id") and assignment["epoch"] == run["coordinator_epoch"],
                    "stale_coordinator", "Coordinator has been replaced")
        else:
            require(assignment["phase"] == run["phase"] or assignment["phase"] == "investigate" and run["phase"] == "triage",
                    "stale_phase", "Assignment no longer matches this run's phase")
        check_budget(run)
        require(fingerprint(run["repos"], run["baseline"], run["scope_digest"]) == assignment["fingerprint"],
                "input_changed", "Assignment inputs changed before task delivery")
        for proposal_id in assignment["proposal_ids"]:
            proposal = self.store.get("proposals", proposal_id)
            require(self.store.get("bugs", proposal["bug_id"]).get("current_proposal_id") == proposal_id,
                    "stale_proposal", "Repair proposal changed before task delivery")
        return run

    def deliver(self, assignment_id: str, ready_evidence: str = "", ready_turn: int | None = None) -> dict:
        assignment = self.store.get("assignments", assignment_id)
        self._require_unsent(assignment)
        # Reconcile validates ownership labels. Do not send while trust/permission
        # dialogs remain; the normal transport also refuses blocked terminals.
        observed = self.runtime.reconcile(assignment)
        require(observed and observed.get("terminal") == assignment["terminal"],
                "terminal_mismatch", "Original managed terminal could not be verified")
        state = observed.get("observation", {})
        require(state.get("state") == "live" and state.get("first_message", {}).get("sent") is False,
                "delivery_not_retryable", "Current terminal does not prove the original task was unsent")
        raw_status = state.get("status")
        status = raw_status.get("state") if isinstance(raw_status, dict) else raw_status
        readiness = {}
        if "status" in state and raw_status is None:
            # Agents without status hooks require explicit human evidence bound
            # to this observed turn, never an inferred or stale readiness claim.
            require(isinstance(ready_evidence, str) and ready_evidence.strip()
                    and type(ready_turn) is int and ready_turn >= 0
                    and type(state.get("turn")) is int and state["turn"] == ready_turn,
                    "terminal_not_ready", "A terminal without status requires readiness evidence for its current turn")
            readiness = {"ready_evidence": ready_evidence.strip(), "ready_turn": ready_turn}
        else:
            require(status in ("idle", "done"), "terminal_not_ready", "Resolve the terminal's current dialog or work before delivery")
        with self.store.transaction():
            assignment = self.store.get("assignments", assignment_id)
            self._require_unsent(assignment)
            # Older known-unsent records did not snapshot the message. Their frozen
            # assignment/run inputs reconstruct it once; all attempts retain it.
            message = assignment.get("initial_message") or self.message(assignment)
            attempt = {"id": identifier("del"), "status": "SENDING", "started_at": time.time(), **readiness}
            event_data = {"delivery_id": attempt["id"], **readiness}
            self.store.update("assignments", assignment_id, {"initial_message": message, "delivery_attempt": attempt})
            self.store.event("delivery_requested", run_id=assignment["run_id"], assignment_id=assignment_id,
                             workspace_id=assignment["workspace_id"], data=event_data)
        try:
            result = self.runtime.send(assignment["terminal"], message)
            # hc 0.1.19 SendResult.turn records when the text went in. `started`
            # only observes subsequent work and false/null is not an unsent result.
            require(isinstance(result, dict) and "error" not in result
                    and type(result.get("turn")) is int and result["turn"] >= 0,
                    "delivery_unknown", "Herdr did not confirm when the task text was delivered")
        except (LoopError, OSError) as error:
            with self.store.transaction():
                current = self.store.get("assignments", assignment_id)
                received = bool(current.get("claimed_at") or current.get("first_message_sent"))
                changes = {"delivery_attempt": {**attempt, "status": "SENT" if received else "UNKNOWN", "error": str(error)}}
                if not received and current["status"] == "STARTING":
                    changes["status"] = "UNKNOWN"
                current = self.store.update("assignments", assignment_id, changes)
                self.store.event("delivery_received" if received else "delivery_unknown", run_id=assignment["run_id"],
                                 assignment_id=assignment_id, workspace_id=assignment["workspace_id"], data=event_data)
            if received:
                return current
            raise LoopError("delivery_unknown", "Task delivery is uncertain; inspect the original terminal and never resend blindly",
                            details={"assignment_id": assignment_id}) from error
        with self.store.transaction():
            # Claim, completion or stop may have raced the transport response.
            result = self.store.update("assignments", assignment_id, {
                "first_message_sent": True, "delivery_attempt": {**attempt, "status": "SENT", "turn": result["turn"]}})
            self.store.event("delivery_received", run_id=assignment["run_id"], assignment_id=assignment_id,
                             workspace_id=assignment["workspace_id"], data=event_data)
            return result

    def _require_pending_submission(self, assignment: dict, observed_turn: int) -> dict:
        run = self.store.get("runs", assignment["run_id"])
        require(run["status"] == "ACTIVE", "run_not_ready", "Run is not accepting task submission")
        delivery = assignment.get("delivery_attempt")
        require(assignment["status"] == "STARTING" and assignment.get("terminal")
                and not assignment.get("claimed_at") and assignment.get("first_message_sent") is True
                and isinstance(assignment.get("initial_message"), str) and assignment["initial_message"].strip()
                and isinstance(delivery, dict) and delivery.get("status") == "SENT"
                and type(delivery.get("turn")) is int and delivery["turn"] == observed_turn,
                "submission_not_retryable", "Only an unclaimed task delivered at the observed turn can be submitted")
        require(not assignment.get("submission_attempt"), "submission_not_retryable",
                "Submission was already attempted; inspect its outcome instead of pressing Enter again")
        return self._require_current_assignment(assignment, run)

    def submit_pending(self, assignment_id: str, evidence: str, observed_turn: int) -> dict:
        require(isinstance(evidence, str) and evidence.strip(), "missing_evidence",
                "Describe how the original task was verified to remain pasted but unsubmitted")
        require(type(observed_turn) is int and observed_turn >= 0, "invalid_turn", "Observed turn must be a nonnegative integer")
        assignment = self.store.get("assignments", assignment_id)
        self._require_pending_submission(assignment, observed_turn)
        observed = self.runtime.reconcile(assignment)
        require(observed and observed.get("terminal") == assignment["terminal"],
                "terminal_mismatch", "Original managed terminal could not be verified")
        state = observed.get("observation", {})
        raw_status = state.get("status")
        status = raw_status.get("state") if isinstance(raw_status, dict) else raw_status
        require(state.get("state") == "live" and "status" in state
                and (raw_status is None or status in ("idle", "done")),
                "terminal_not_ready", "Original terminal is not ready to submit the pending task")
        require(type(state.get("turn")) is int and state["turn"] == observed_turn,
                "stale_turn", "Terminal turn changed since the pending task was observed")
        with self.store.transaction():
            assignment = self.store.get("assignments", assignment_id)
            self._require_pending_submission(assignment, observed_turn)
            attempt = {"id": identifier("sub"), "status": "SENDING", "started_at": time.time(),
                       "evidence": evidence.strip(), "observed_turn": observed_turn}
            event_data = {"submission_id": attempt["id"], "evidence": attempt["evidence"], "observed_turn": observed_turn}
            self.store.update("assignments", assignment_id, {"submission_attempt": attempt})
            self.store.event("submission_requested", run_id=assignment["run_id"], assignment_id=assignment_id,
                             workspace_id=assignment["workspace_id"], data=event_data)
        try:
            # The task text is already in the original terminal; only Enter is sent.
            result = self.runtime.submit(assignment["terminal"])
            require(isinstance(result, dict) and "error" not in result
                    and type(result.get("turn")) is int and result["turn"] >= 0,
                    "submission_unknown", "Herdr did not confirm when Enter was delivered")
        except (LoopError, OSError) as error:
            with self.store.transaction():
                current = self.store.get("assignments", assignment_id)
                # first_message_sent predates this Enter; only a claim proves receipt.
                received = bool(current.get("claimed_at"))
                changes = {"submission_attempt": {**attempt, "status": "SENT" if received else "UNKNOWN", "error": str(error)}}
                if not received and current["status"] == "STARTING":
                    changes["status"] = "UNKNOWN"
                current = self.store.update("assignments", assignment_id, changes)
                self.store.event("submission_received" if received else "submission_unknown", run_id=assignment["run_id"],
                                 assignment_id=assignment_id, workspace_id=assignment["workspace_id"], data=event_data)
            if received:
                return current
            raise LoopError("submission_unknown", "Task submission is uncertain; inspect the original terminal and do not press Enter again",
                            details={"assignment_id": assignment_id}) from error
        with self.store.transaction():
            # Preserve any claim, completion or stop that raced the transport response.
            updated = self.store.update("assignments", assignment_id, {
                "submission_attempt": {**attempt, "status": "SENT", "turn": result["turn"]}})
            self.store.event("submission_received", run_id=assignment["run_id"], assignment_id=assignment_id,
                             workspace_id=assignment["workspace_id"], data={**event_data, "turn": result["turn"]})
            return updated

    def block(self, assignment_id: str, reason: str, checkpoint: str) -> dict:
        require(reason.strip() and checkpoint.strip(), "missing_checkpoint", "Block requires a reason and checkpoint")
        with self.store.transaction():
            assignment = self.store.get("assignments", assignment_id)
            require(assignment["status"] in LIVE_ASSIGNMENTS, "assignment_finished", "Cannot block an ended assignment")
            artifact = self.store.save_artifact(assignment["workspace_id"], requirement_id=assignment["requirement_id"],
                                                run_id=assignment["run_id"], kind="checkpoints", content=checkpoint)
            result = self.store.update("assignments", assignment_id, {"status": "BLOCKED", "block_reason": reason,
                                                                       "checkpoint_id": artifact["id"]})
            self.store.event("assignment_blocked", run_id=assignment["run_id"], assignment_id=assignment_id,
                             workspace_id=assignment["workspace_id"], data={"reason": reason, "checkpoint_id": artifact["id"]})
            return result

    def verify_stopped(self, assignment: dict) -> None:
        if assignment.get("terminal"):
            try:
                observed = self.runtime.observe(assignment["terminal"])
            except LoopError as error:
                raise LoopError("stop_unverified", "Cannot verify the old terminal stopped; reconcile first") from error
            require(observed.get("state") == "finished", "worker_still_running", "Old terminal is still live; stop it before handoff")
        else:
            observed = self.runtime.reconcile(assignment)
            require(observed is not None, "stop_unverified", "Missing terminal is not proof of termination; reconcile the original execution first")
            require(observed.get("observation", {}).get("state") == "finished", "worker_still_running", "An execution with this assignment is still live")

    def stop_terminal(self, assignment_id: str, *, finalize=True) -> dict:
        assignment = self.store.get("assignments", assignment_id)
        require(assignment.get("terminal"), "terminal_unknown", "Reconcile the assignment before terminating it")
        # The adapter verifies assignment ownership during reconciliation before a destructive terminal action.
        if not assignment.get("attached"):
            found = self.runtime.reconcile(assignment)
            require(found and found.get("terminal") == assignment["terminal"], "terminal_mismatch", "Managed terminal ownership is unverified")
        self.runtime.terminate(assignment["terminal"])
        self.verify_stopped(assignment)
        changes = {"status": "STOPPED", "ended_at": time.time()} if finalize else {"status": "BLOCKED", "block_reason": "Terminal stopped; check in-flight operation outcomes before handoff or retirement"}
        return self.store.update("assignments", assignment_id, changes)
