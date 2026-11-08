"""Worker reports are immutable evidence; acceptance is a separate coordinator action."""
import json

from .errors import require
from .fingerprint import fingerprint


class ReportService:
    def __init__(self, store):
        self.store = store

    def submit(self, assignment_id: str, report: dict) -> dict:
        require(isinstance(report, dict), "invalid_report", "Report must be an object")
        require(report.get("result") in ("pass", "fail", "blocked"), "invalid_report", "Report result must be pass, fail or blocked")
        require(isinstance(report.get("summary"), str) and report["summary"].strip(), "invalid_report", "Report summary is required")
        for field in ("coverage", "not_covered", "evidence", "bug_ids"):
            require(isinstance(report.get(field, []), list) and all(isinstance(item, str) and item.strip() for item in report.get(field, [])),
                    "invalid_report", f"{field} must be a list of nonempty strings")
        require(report.get("evidence"), "missing_evidence", "Report must include actual evidence")
        if report["result"] == "pass":
            require(report.get("coverage"), "missing_coverage", "Passing report must describe verified coverage")
        with self.store.transaction():
            assignment = self.store.get("assignments", assignment_id)
            require(assignment["slot"] == "worker", "invalid_reporter", "Coordinator cannot submit a worker report")
            require(assignment["status"] == "RUNNING" and assignment.get("claimed_at"), "assignment_not_running", "Claim the assigned task before submitting a result")
            run = self.store.get("runs", assignment["run_id"])
            require(run["status"] not in ("DONE", "STOPPED", "STOPPING"), "run_not_active", "Run is not accepting worker results")
            expected = assignment["fingerprint"]["digest"]
            require(report.get("input_fingerprint") == expected, "report_input_mismatch", "Report must identify the assignment input")
            for bug_id in report.get("bug_ids", []):
                bug = self.store.get("bugs", bug_id, run["workspace_id"])
                require(bug["requirement_id"] == run["requirement_id"], "scope_mismatch", "Report contains a Bug from another requirement")
                require(self.store.list("run_bugs", run_id=run["id"], bug_id=bug_id), "scope_mismatch", "Reported Bug has not been linked to this run")
            current = fingerprint(run["repos"], run["baseline"], run["scope_digest"])
            require(assignment["phase"] == "develop" or current == assignment["fingerprint"],
                    "input_changed", "Read-only assignment input changed; preserve evidence and request revalidation")
            if assignment["phase"] == "test":
                require(run.get("environment_revision", 0) == assignment["environment_revision"],
                        "environment_changed", "Test environment evidence changed during execution")
            artifact = self.store.save_artifact(run["workspace_id"], requirement_id=run["requirement_id"], run_id=run["id"],
                                                kind="reports", content=json.dumps(report, ensure_ascii=False, indent=2), suffix=".json")
            record = self.store.create("reports", {"workspace_id": run["workspace_id"], "requirement_id": run["requirement_id"],
                                                    "run_id": run["id"], "assignment_id": assignment_id, "status": "SUBMITTED",
                                                    "artifact_id": artifact["id"], "result": report["result"], "report": report,
                                                    "output_fingerprint": current,
                                                    "environment_revision": assignment["environment_revision"]})
            self.store.update("assignments", assignment_id, {"status": "REPORTED", "report_id": record["id"]})
            self.store.event("report_submitted", workspace_id=run["workspace_id"], requirement_id=run["requirement_id"],
                             run_id=run["id"], assignment_id=assignment_id, data={"report_id": record["id"], "result": report["result"]})
            return record

    def get(self, report_id: str) -> dict:
        report = self.store.get("reports", report_id)
        self.store.read_artifact(report["artifact_id"])
        return report
