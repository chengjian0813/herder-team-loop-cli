"""Deterministic rules independent of SQLite, Herdr and agent brands."""
from dataclasses import dataclass
import math
import time

from .errors import require

ROLES = ("coordinator", "developer", "reviewer", "tester")
TERMINAL_RUNS = {"DONE", "STOPPED"}
LIVE_ASSIGNMENTS = {"DISPATCHING", "STARTING", "RUNNING", "REPORTED", "BLOCKED", "UNKNOWN"}
PHASE_ROLES = {"plan": "coordinator", "develop": "developer", "review": "reviewer",
               "environment": "coordinator", "test": "tester", "triage": "coordinator",
               "acceptance": "coordinator"}


@dataclass(frozen=True)
class Budget:
    max_rounds: int = 3
    active_minutes: float = 120
    no_progress_limit: int = 2

    def validate(self) -> None:
        require(self.max_rounds > 0 and self.no_progress_limit > 0
                and math.isfinite(self.active_minutes) and self.active_minutes > 0,
                "invalid_budget", "Budgets must be positive and finite")


def active_seconds(run: dict, at: float | None = None) -> float:
    at = time.time() if at is None else at
    return run.get("active_seconds", 0.0) + (
        max(0.0, at - run["active_since"]) if run.get("active_since") is not None else 0.0)


def check_budget(run: dict, at: float | None = None) -> None:
    at = time.time() if at is None else at
    require(run.get("round", 0) <= run["max_rounds"], "budget_exhausted", "Repair round limit reached")
    require(active_seconds(run, at) < run["active_limit_seconds"], "budget_exhausted", "Active execution time exhausted")
    require(run.get("deadline") is None or at < run["deadline"], "budget_exhausted", "Absolute deadline reached")


def authorize(run: dict, capability: str) -> None:
    authorization = run.get("authorization", {})
    require(authorization.get(capability) is True and bool(authorization.get("source")),
            "not_authorized", f"Explicit {capability} authorization and its source are required")
