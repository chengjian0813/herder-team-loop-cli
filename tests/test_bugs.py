"""Behavioral repair-gate tests using isolated SQLite stores."""
from pathlib import Path
import tempfile
import unittest

from herdr_team_loop.bugs import BugService
from herdr_team_loop.errors import LoopError
from herdr_team_loop.storage import Store


class BugServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "data")
        self.addCleanup(self.store.close)
        self.service = BugService(self.store)
        self.workspace = self.store.create("workspaces", {"path": str(Path(self.temp.name) / "project")})
        self.requirement = self.store.create("requirements", {"workspace_id": self.workspace["id"]})
        self.run = self.new_run()

    def new_run(self, **changes):
        return self.store.create("runs", {"workspace_id": self.workspace["id"], "requirement_id": self.requirement["id"],
                                          "status": "READY", "phase": "triage", "scope_revision": 1, "scope_digest": "scope-one",
                                          "fingerprint": {"digest": "original"}, "repair_policy": "guarded",
                                          "authorization": {"source": "user message 1", "code_fix": True, "deploy": False}, **changes})

    def bug(self, title="Wrong result", **kwargs):
        return self.service.create(self.run["id"], title, "reproduction evidence", "expected result", "actual result", **kwargs)

    def proposal(self, **changes):
        return {"summary": "Correct the documented calculation", "evidence": "reproduced with fixture",
                "authorization_reference": "user message 1", "impact": "only the documented result", "validation_plan": "rerun focused regression", **changes}

    def triage(self, bug, classification="AUTO_ELIGIBLE", **changes):
        return self.service.triage(bug["id"], classification, self.proposal(**changes), "Root cause verified")

    def decision(self, bug, action="fix", **changes):
        return self.triage(bug, "NEEDS_DECISION", options=[{"id": "chosen", "description": "Explicit user choice", "action": action},
                                                            {"id": "other", "description": "Do not approve", "action": "reject"}], recommended="chosen", **changes)

    def resolve(self, result, **changes):
        return self.service.resolve(result["decision"]["id"], changes.get("option", "chosen"),
                                    changes.get("response", "I explicitly choose the proposed option"), changes.get("source", "user message 2"))

    def evidence(self, run=None):
        run = run or self.run
        return self.store.save_artifact(run["workspace_id"], requirement_id=run["requirement_id"], run_id=run["id"],
                                        kind="verification", content="Independent valid test passed")

    def assert_error(self, code, function, *args, **kwargs):
        with self.assertRaises(LoopError) as raised:
            function(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_same_run_stable_key_is_idempotent(self):
        bug = self.bug(stable_key="calculation:wrong-result")
        self.assertEqual(self.bug(stable_key="calculation:wrong-result")["id"], bug["id"])
        self.assertEqual(len(self.service.list(self.run["id"])), 1)

    def test_independent_auto_fix_is_not_blocked_by_other_pending_decision(self):
        ready, waiting = self.bug("Ready"), self.bug("Waiting")
        proposal = self.triage(ready)["proposal"]
        decision = self.decision(waiting)["decision"]
        self.assertEqual(self.service.check_fix(self.run, [ready["id"]])[0]["id"], proposal["id"])
        self.assert_error("needs_decision", self.service.check_fix, self.run, [waiting["id"]])
        self.assertEqual(self.service.pending(self.run["id"])[0]["id"], decision["id"])

    def test_untriaged_or_under_investigation_cannot_be_fixed(self):
        bug = self.bug()
        self.assert_error("untriaged_bug", self.service.check_fix, self.run, [bug["id"]])
        self.triage(bug, "NEEDS_INVESTIGATION")
        self.assert_error("needs_investigation", self.service.check_fix, self.run, [bug["id"]])

    def test_report_only_and_absent_code_fix_authorization_reject_fix(self):
        bug = self.bug()
        self.triage(bug)
        self.store.update("runs", self.run["id"], {"repair_policy": "report-only"})
        self.assert_error("not_authorized", self.service.check_fix, self.run, [bug["id"]])
        self.store.update("runs", self.run["id"], {"repair_policy": "guarded", "authorization": {"source": "test only", "code_fix": False}})
        self.assert_error("not_authorized", self.service.check_fix, self.run, [bug["id"]])

    def test_confirm_every_fix_requires_decision_even_for_auto_eligible(self):
        self.store.update("runs", self.run["id"], {"repair_policy": "confirm-every-fix"})
        bug = self.bug()
        result = self.triage(bug)
        self.assert_error("needs_decision", self.service.check_fix, self.run, [bug["id"]])
        self.service.resolve(result["decision"]["id"], "fix", "Approve this fix", "user message")
        self.assertEqual(self.service.check_fix(self.run, [bug["id"]])[0]["id"], result["proposal"]["id"])

    def test_new_proposal_invalidates_old_decision_atomically(self):
        bug = self.bug()
        old = self.decision(bug)
        self.resolve(old)
        new = self.decision(bug, summary="Materially different proposal")
        self.assert_error("stale_proposal", self.resolve, old)
        self.assert_error("needs_decision", self.service.check_fix, self.run, [bug["id"]])
        self.assertEqual(self.store.get("decisions", old["decision"]["id"])["status"], "SUPERSEDED")
        self.resolve(new)
        self.assertEqual(self.service.check_fix(self.run, [bug["id"]])[0]["id"], new["proposal"]["id"])

    def test_invalid_replacement_proposal_keeps_prior_decision(self):
        bug = self.bug()
        old = self.decision(bug)
        self.resolve(old)
        self.assert_error("invalid_input", self.triage, bug, summary="")
        self.assertEqual(self.service.check_fix(self.run, [bug["id"]])[0]["id"], old["proposal"]["id"])
        self.assertEqual(len(self.store.list("proposals", bug_id=bug["id"])), 1)

    def test_declining_or_deferring_does_not_resolve_the_bug(self):
        for action in ("reject", "defer"):
            with self.subTest(action=action):
                bug = self.bug(action)
                result = self.decision(bug, action)
                self.resolve(result)
                self.assert_error("needs_decision", self.service.check_fix, self.run, [bug["id"]])
                self.assert_error("unresolved_bugs", self.service.assert_complete, self.run["id"])
                self.assertNotEqual(self.store.get("bugs", bug["id"])["status"], "RESOLVED")

    def test_explicit_risk_acceptance_is_disposition_not_verification(self):
        bug = self.bug()
        result = self.decision(bug, "accept-risk")
        self.resolve(result)
        self.service.assert_complete(self.run["id"])
        self.assertEqual(self.store.get("bugs", bug["id"])["status"], "ACCEPTED_RISK")
        self.assert_error("needs_decision", self.service.mark_verified, self.run["id"], [bug["id"]], self.evidence()["id"])

    def test_response_and_source_must_be_explicit_and_cannot_be_rewritten(self):
        result = self.decision(self.bug())
        self.assert_error("invalid_input", self.resolve, result, response=" ")
        self.assert_error("invalid_input", self.resolve, result, source="")
        resolved = self.resolve(result)
        self.assertEqual(self.resolve(result), resolved)
        self.assert_error("decision_closed", self.resolve, result, option="other")

    def test_scope_or_input_changes_reject_old_repair_plan(self):
        bug = self.bug()
        result = self.decision(bug)
        self.resolve(result)
        self.store.update("runs", self.run["id"], {"fingerprint": {"digest": "new-input"}})
        self.assert_error("stale_inputs", self.service.check_fix, self.run, [bug["id"]])
        self.store.update("runs", self.run["id"], {"scope_revision": 2, "scope_digest": "new-scope"})
        self.assert_error("stale_scope", self.service.check_fix, self.run, [bug["id"]])

    def test_independent_fix_allows_evidenced_reassessment_without_repeating_approval(self):
        first, second = self.bug("Independent repair"), self.bug("Approved repair")
        self.triage(first)
        result = self.decision(second)
        decision = self.resolve(result)
        original_proposal = self.store.get("proposals", result["proposal"]["id"])
        self.service.check_fix(self.run, [first["id"]])
        fingerprint = {"digest": "first-bug-fixed"}
        self.store.update("runs", self.run["id"], {"fingerprint": fingerprint})
        self.service.mark_verified(self.run["id"], [first["id"]], self.evidence()["id"])
        self.assert_error("stale_inputs", self.service.check_fix, self.run, [second["id"]])

        link = self.service.reassess(self.run["id"], second["id"], "Reviewed first repair diff; second proposal and impact remain unchanged")

        self.assertEqual(link["assessed_input_fingerprint"], fingerprint)
        self.assertEqual(link["assessed_proposal_id"], original_proposal["id"])
        self.assertEqual(self.service.check_fix(self.run, [second["id"]]), [original_proposal])
        self.assertEqual(self.store.get("proposals", original_proposal["id"]), original_proposal)
        self.assertEqual(self.store.get("decisions", decision["id"]), decision)
        self.assertEqual(len(self.store.list("proposals", bug_id=second["id"])), 1)
        self.assertEqual(self.store.events(self.run["id"])[-1]["kind"], "bug_reassessed")
        self.store.update("runs", self.run["id"], {"fingerprint": {"digest": "another-change"}})
        self.assert_error("stale_inputs", self.service.check_fix, self.run, [second["id"]])

    def test_pending_decision_stays_pending_after_input_reassessment(self):
        bug = self.bug()
        result = self.decision(bug)
        self.store.update("runs", self.run["id"], {"fingerprint": {"digest": "independent-change"}})
        self.assertEqual(self.service.pending(self.run["id"]), [])
        self.service.reassess(self.run["id"], bug["id"], "Current inputs leave all options unchanged")
        self.assertEqual(self.service.pending(self.run["id"]), [result["decision"]])
        self.assert_error("needs_decision", self.service.check_fix, self.run, [bug["id"]])
        self.resolve(result)
        self.assertEqual(self.service.check_fix(self.run, [bug["id"]])[0]["id"], result["proposal"]["id"])

    def test_reassessment_does_not_reuse_approval_for_materially_changed_proposal(self):
        bug = self.bug()
        old = self.decision(bug)
        self.resolve(old)
        self.store.update("runs", self.run["id"], {"fingerprint": {"digest": "changed-input"}})
        self.service.reassess(self.run["id"], bug["id"], "Original proposal is unchanged")
        current = self.decision(bug, summary="Materially different behavior")
        self.store.update("runs", self.run["id"], {"fingerprint": {"digest": "another-input"}})
        self.assert_error("stale_inputs", self.service.check_fix, self.run, [bug["id"]])
        self.service.reassess(self.run["id"], bug["id"], "New proposal still applies, user choice remains pending")
        self.assert_error("needs_decision", self.service.check_fix, self.run, [bug["id"]])
        self.assertEqual(self.service.pending(self.run["id"]), [current["decision"]])
        self.assertEqual(self.store.get("decisions", old["decision"]["id"])["status"], "SUPERSEDED")

    def test_reassessment_cannot_refresh_changed_scope_or_unassessed_history(self):
        bug = self.bug()
        self.resolve(self.decision(bug))
        self.store.update("runs", self.run["id"], {"status": "STOPPED"})
        self.run = self.new_run(fingerprint={"digest": "new-input"})
        self.service.link_history(self.run)
        self.assert_error("bug_not_applicable", self.service.reassess, self.run["id"], bug["id"], "Same plan")
        self.service.set_applicability(self.run["id"], bug["id"], True, "Issue still applies")
        self.service.reassess(self.run["id"], bug["id"], "Original requirement-scoped plan still applies")
        self.service.check_fix(self.run, [bug["id"]])
        self.store.update("runs", self.run["id"], {"scope_revision": 2, "scope_digest": "new-scope"})
        self.assert_error("stale_scope", self.service.reassess, self.run["id"], bug["id"], "Scope changed")
        self.service.set_applicability(self.run["id"], bug["id"], True, "Bug applies to new scope")
        self.assert_error("stale_scope", self.service.reassess, self.run["id"], bug["id"], "Old proposal still has old scope")

    def test_reassessment_cannot_extend_run_scoped_decision(self):
        bug = self.bug()
        self.resolve(self.decision(bug, decision_scope="run"))
        self.store.update("runs", self.run["id"], {"status": "STOPPED"})
        self.run = self.new_run(fingerprint={"digest": "new-input"})
        self.service.link_history(self.run)
        self.service.set_applicability(self.run["id"], bug["id"], True, "Issue still applies")
        self.assert_error("stale_decision", self.service.reassess, self.run["id"], bug["id"], "Cannot expand approval scope")

    def test_reassessment_requires_valid_evidence_and_does_not_refresh_verification(self):
        bug = self.bug()
        self.triage(bug)
        artifact = self.evidence()
        self.service.mark_verified(self.run["id"], [bug["id"]], artifact["id"])
        original = self.service.list(self.run["id"])[0]["run_bug"]["verification"]
        self.store.update("runs", self.run["id"], {"fingerprint": {"digest": "changed-input"}})
        self.assert_error("invalid_input", self.service.reassess, self.run["id"], bug["id"], " ")
        link = self.service.reassess(self.run["id"], bug["id"], self.evidence()["id"])
        self.assertEqual(link["verification"], original)
        self.assert_error("stale_evidence", self.service.assert_complete, self.run["id"])
        (self.store.root / artifact["path"]).write_text("tampered")
        self.assert_error("artifact_changed", self.service.reassess, self.run["id"], bug["id"], artifact["id"])

    def test_expected_fix_changes_do_not_require_reapproving_the_same_plan(self):
        bug = self.bug()
        result = self.decision(bug)
        self.resolve(result)
        self.service.check_fix(self.run, [bug["id"]])
        self.store.update("runs", self.run["id"], {"fingerprint": {"digest": "fixed-code"}})
        self.service.mark_verified(self.run["id"], [bug["id"]], self.evidence()["id"])
        self.service.assert_complete(self.run["id"])
        self.store.update("runs", self.run["id"], {"fingerprint": {"digest": "changed-again"}})
        self.assert_error("stale_evidence", self.service.assert_complete, self.run["id"])

    def test_new_run_must_link_and_assess_history_including_resolved_bugs(self):
        bug = self.bug(stable_key="stable")
        self.triage(bug)
        self.service.mark_verified(self.run["id"], [bug["id"]], self.evidence()["id"])
        self.store.update("runs", self.run["id"], {"status": "DONE"})
        self.run = self.new_run()
        self.assert_error("unresolved_bugs", self.service.assert_complete, self.run["id"])
        self.service.link_history(self.run)
        self.service.link_history(self.run)
        self.assertEqual(len(self.service.list(self.run["id"])), 1)
        self.assertEqual(self.service.list(self.run["id"])[0]["applicability"], "NEEDS_CHECK")
        self.assert_error("unresolved_bugs", self.service.assert_complete, self.run["id"])
        self.assert_error("historical_bug_exists", self.bug, stable_key="stable")
        self.service.set_applicability(self.run["id"], bug["id"], True, "Still in scope")
        self.assert_error("unresolved_bugs", self.service.assert_complete, self.run["id"])
        self.service.mark_verified(self.run["id"], [bug["id"]], self.evidence()["id"])
        self.service.assert_complete(self.run["id"])

    def test_excluding_required_bug_needs_user_basis(self):
        bug = self.bug()
        self.assert_error("invalid_input", self.service.set_applicability, self.run["id"], bug["id"], False, "Skip it")
        self.service.set_applicability(self.run["id"], bug["id"], False, "User narrowed acceptance", "Exclude this Bug from this run")
        self.service.assert_complete(self.run["id"])
        self.assertEqual(self.store.get("bugs", bug["id"])["status"], "OPEN")

    def test_cross_requirement_bug_and_evidence_are_rejected(self):
        bug = self.bug()
        other_requirement = self.store.create("requirements", {"workspace_id": self.workspace["id"]})
        other_run = self.store.create("runs", {**{k: v for k, v in self.run.items() if k not in ("id", "revision", "created_at", "updated_at")},
                                                "requirement_id": other_requirement["id"]})
        self.assert_error("scope_mismatch", self.service.check_fix, other_run, [bug["id"]])
        self.assert_error("scope_mismatch", self.service.create, self.run["id"], "Cross evidence", self.evidence(other_run)["id"], "expected", "actual")

    def test_workspace_isolation_rejects_foreign_bug_ids(self):
        bug = self.bug()
        workspace = self.store.create("workspaces", {"path": str(Path(self.temp.name) / "other-workspace")})
        requirement = self.store.create("requirements", {"workspace_id": workspace["id"]})
        foreign_run = self.store.create("runs", {**{k: v for k, v in self.run.items() if k not in ("id", "revision", "created_at", "updated_at")},
                                                  "workspace_id": workspace["id"], "requirement_id": requirement["id"]})
        self.assert_error("scope_mismatch", self.service.check_fix, foreign_run, [bug["id"]])
        self.assert_error("scope_mismatch", self.service.set_applicability, foreign_run["id"], bug["id"], True, "wrong workspace")

    def test_foreign_run_or_changed_verification_evidence_rejected(self):
        bug = self.bug()
        self.triage(bug)
        artifact = self.evidence()
        self.service.mark_verified(self.run["id"], [bug["id"]], artifact["id"])
        (self.store.root / artifact["path"]).write_text("tampered")
        self.assert_error("artifact_changed", self.service.assert_complete, self.run["id"])

    def test_verification_batch_rolls_back_if_one_bug_is_unapproved(self):
        ready, waiting = self.bug("Ready"), self.bug("Waiting")
        self.triage(ready)
        self.decision(waiting)
        self.assert_error("needs_decision", self.service.mark_verified, self.run["id"], [ready["id"], waiting["id"]], self.evidence()["id"])
        self.assertEqual(self.store.get("bugs", ready["id"])["status"], "OPEN")
        self.assertTrue(all(row["run_bug"]["status"] == "OPEN" for row in self.service.list(self.run["id"])))

    def test_requirement_scoped_approval_can_be_reused_after_explicit_history_check(self):
        bug = self.bug()
        result = self.decision(bug)
        self.resolve(result)
        self.store.update("runs", self.run["id"], {"status": "STOPPED"})
        self.run = self.new_run()
        self.service.link_history(self.run)
        self.service.set_applicability(self.run["id"], bug["id"], True, "Same issue, scope, code and proposal")
        self.assertEqual(self.service.check_fix(self.run, [bug["id"]])[0]["id"], result["proposal"]["id"])

    def test_reopen_verified_bug_revokes_verification_and_old_repair_plan(self):
        bug = self.bug(stable_key="recurrence")
        self.triage(bug)
        self.service.mark_verified(self.run["id"], [bug["id"]], self.evidence()["id"])
        self.assertEqual(self.bug(stable_key="recurrence")["id"], bug["id"])
        self.service.reopen(self.run["id"], bug["id"], "Regression reproduced again")
        self.assert_error("unresolved_bugs", self.service.assert_complete, self.run["id"])
        self.assert_error("untriaged_bug", self.service.check_fix, self.run, [bug["id"]])
        self.assertIsNone(self.service.list(self.run["id"])[0]["run_bug"]["verification"])
        self.assertEqual(self.store.events(self.run["id"])[-1]["kind"], "bug_failure_reported")

    def test_reopen_unverified_bug_preserves_current_triage(self):
        bug = self.bug()
        result = self.triage(bug)
        self.service.reopen(self.run["id"], bug["id"], "Same failure in current report")
        self.assertEqual(self.service.check_fix(self.run, [bug["id"]])[0]["id"], result["proposal"]["id"])

    def test_reopened_bug_cannot_reuse_old_plan_via_new_run(self):
        bug = self.bug()
        self.triage(bug)
        self.service.mark_verified(self.run["id"], [bug["id"]], self.evidence()["id"])
        self.service.reopen(self.run["id"], bug["id"], "Regression returned")
        self.store.update("runs", self.run["id"], {"status": "STOPPED"})
        self.run = self.new_run()
        self.service.link_history(self.run)
        self.service.set_applicability(self.run["id"], bug["id"], True, "Regression still applies")
        self.assert_error("untriaged_bug", self.service.check_fix, self.run, [bug["id"]])

    def test_run_scoped_approval_cannot_leak_into_new_run(self):
        bug = self.bug()
        result = self.decision(bug, decision_scope="run")
        self.resolve(result)
        self.store.update("runs", self.run["id"], {"status": "STOPPED"})
        self.run = self.new_run()
        self.service.link_history(self.run)
        self.service.set_applicability(self.run["id"], bug["id"], True, "Still applicable")
        self.assert_error("stale_decision", self.service.check_fix, self.run, [bug["id"]])


if __name__ == "__main__":
    unittest.main()
