"""Per-Bug repair gates, explicit decisions, and run-specific verification."""
from .domain import TERMINAL_RUNS, authorize
from .errors import require

CLASSIFICATIONS = {"NEEDS_INVESTIGATION", "AUTO_ELIGIBLE", "NEEDS_DECISION"}
ACTIONS = {"fix", "defer", "reject", "accept-risk"}
PROPOSAL_FIELDS = ("summary", "evidence", "authorization_reference", "impact", "validation_plan")


def _text(value, field):
    require(isinstance(value, str) and bool(value.strip()), "invalid_input", f"{field} must be nonempty text")
    return value.strip()


class BugService:
    """Keep repair permission separate from a Bug's eventual verification."""

    def __init__(self, store):
        self.store = store

    def _run(self, run_id, *, active=False):
        run = self.store.get("runs", run_id)
        if active:
            require(run["status"] not in TERMINAL_RUNS, "terminal_run", "Run is already terminal")
        return run

    def _run_scope(self, run):
        return {"workspace_id": run["workspace_id"], "requirement_id": run["requirement_id"], "run_id": run["id"]}

    def _link(self, run, bug_id):
        bug = self.store.get("bugs", bug_id, run["workspace_id"])
        require(bug["requirement_id"] == run["requirement_id"], "scope_mismatch", "Bug belongs to another requirement")
        links = self.store.list("run_bugs", run_id=run["id"], bug_id=bug_id)
        require(len(links) == 1, "bug_not_linked", "Explicitly link and assess the historical Bug first")
        return bug, links[0]

    def _context(self, bug):
        runs = [run for run in self.store.list("runs", workspace_id=bug["workspace_id"], requirement_id=bug["requirement_id"])
                if run["status"] not in TERMINAL_RUNS]
        require(len(runs) == 1, "no_active_run", "Bug requires one active run in its requirement")
        run = runs[0]
        _, link = self._link(run, bug["id"])
        require(link["applicability"] == "APPLICABLE", "bug_not_applicable", "Assess the Bug's applicability before triage")
        return run, link

    def _evidence(self, value, run):
        value = _text(value, "evidence")
        if value.startswith("art_"):
            artifact = self.store.get("artifacts", value, run["workspace_id"])
            require(artifact["requirement_id"] == run["requirement_id"], "scope_mismatch", "Evidence belongs to another requirement")
            require(bool(self.store.read_artifact(value)), "invalid_evidence", "Evidence artifact is empty")
        return value

    def _current_scope(self, record, run):
        require((record.get("scope_revision"), record.get("scope_digest")) == (run["scope_revision"], run["scope_digest"]),
                "stale_scope", "Reassess the Bug and proposal against the current scope")

    def _applicable_inputs(self, proposal, link):
        # A reassessment applies only to the exact proposal whose meaning was reviewed.
        if link.get("assessed_proposal_id") == proposal["id"]:
            return link["assessed_input_fingerprint"]
        return proposal["input_fingerprint"]

    def _proposal(self, bug, link, run, *, inputs=False):
        proposal_id = link.get("current_proposal_id")
        require(proposal_id, "untriaged_bug", "Bug needs an explicit proposal and triage")
        require(proposal_id == bug.get("current_proposal_id"), "stale_proposal", "Bug has a newer proposal")
        proposal = self.store.get("proposals", proposal_id, run["workspace_id"])
        require(proposal["bug_id"] == bug["id"] and proposal["requirement_id"] == run["requirement_id"],
                "scope_mismatch", "Proposal does not belong to this Bug")
        self._current_scope(link, run)
        self._current_scope(proposal, run)
        if inputs:
            require(self._applicable_inputs(proposal, link) == run.get("fingerprint"),
                    "stale_inputs", "Proposal input changed; reassess its applicability")
        return proposal

    def _decision(self, proposal, run):
        decision_id = proposal.get("decision_id")
        if not decision_id:
            return None
        decision = self.store.get("decisions", decision_id, run["workspace_id"])
        require(decision["proposal_id"] == proposal["id"] and decision["bug_id"] == proposal["bug_id"],
                "scope_mismatch", "Decision does not match the proposal")
        self._current_scope(decision, run)
        require(decision.get("decision_scope", "requirement") != "run" or decision["run_id"] == run["id"],
                "stale_decision", "Decision is restricted to its original run")
        return decision

    def _event(self, run, kind, **data):
        self.store.event(kind, **self._run_scope(run), data=data)

    def create(self, run_id, title, evidence, expected, actual, stable_key=None):
        with self.store.transaction():
            run = self._run(run_id, active=True)
            fields = {"title": _text(title, "title"), "expected": _text(expected, "expected"),
                      "actual": _text(actual, "actual"), "evidence": self._evidence(evidence, run)}
            if stable_key is not None:
                stable_key = _text(stable_key, "stable_key")
                matches = self.store.list("bugs", workspace_id=run["workspace_id"], requirement_id=run["requirement_id"], stable_key=stable_key)
                current = [bug for bug in matches if bug["run_id"] == run_id]
                if current:
                    return current[0]
                require(not matches, "historical_bug_exists", "Assess the matching historical Bug explicitly", bug_ids=[bug["id"] for bug in matches])
            bug = self.store.create("bugs", {**self._run_scope(run), **fields, "stable_key": stable_key,
                                             "status": "OPEN", "classification": "NEEDS_INVESTIGATION", "required": True})
            self.store.create("run_bugs", {**self._run_scope(run), "bug_id": bug["id"], "applicability": "APPLICABLE",
                                            "status": "OPEN", "required": True, "scope_revision": run["scope_revision"], "scope_digest": run["scope_digest"]})
            self._event(run, "bug_created", bug_id=bug["id"])
            return bug

    def list(self, run_id):
        run = self._run(run_id)
        result = []
        for link in self.store.list("run_bugs", run_id=run_id):
            bug, _ = self._link(run, link["bug_id"])
            result.append({**bug, "applicability": link["applicability"], "run_bug": link})
        return result

    def link_history(self, run):
        with self.store.transaction():
            run = self._run(run["id"], active=True)
            known = {link["bug_id"] for link in self.store.list("run_bugs", run_id=run["id"])}
            for bug in self.store.list("bugs", workspace_id=run["workspace_id"], requirement_id=run["requirement_id"]):
                if bug["id"] not in known:
                    self.store.create("run_bugs", {**self._run_scope(run), "bug_id": bug["id"], "status": "NEEDS_CHECK",
                                                    "applicability": "NEEDS_CHECK", "required": bug.get("required", True),
                                                    "current_proposal_id": bug.get("current_proposal_id"),
                                                    "scope_revision": run["scope_revision"], "scope_digest": run["scope_digest"]})
                    self._event(run, "historical_bug_linked", bug_id=bug["id"], applicability="NEEDS_CHECK")

    def set_applicability(self, run_id, bug_id, applicable, reason, user_response=None):
        require(isinstance(applicable, bool), "invalid_input", "applicable must be a boolean")
        reason = _text(reason, "reason")
        with self.store.transaction():
            run = self._run(run_id, active=True)
            bug, link = self._link(run, bug_id)
            if not applicable and link.get("required", True):
                user_response = _text(user_response, "user_response for excluding a required Bug")
            changes = {"applicability": "APPLICABLE" if applicable else "EXCLUDED", "status": "OPEN" if applicable else "EXCLUDED",
                       "reason": reason, "user_response": user_response, "verification": None,
                       "scope_revision": run["scope_revision"], "scope_digest": run["scope_digest"]}
            updated = self.store.update("run_bugs", link["id"], changes)
            self._event(run, "bug_applicability", bug_id=bug["id"], **changes)
            return updated

    def reassess(self, run_id, bug_id, evidence):
        """Record that an unchanged proposal still applies after independent input changes."""
        with self.store.transaction():
            run = self._run(run_id, active=True)
            bug, link = self._link(run, bug_id)
            require(link["applicability"] == "APPLICABLE", "bug_not_applicable", "Assess the Bug's applicability first")
            proposal = self._proposal(bug, link, run)
            self._decision(proposal, run)
            evidence = self._evidence(evidence, run)
            changes = {"assessed_proposal_id": proposal["id"], "assessed_input_fingerprint": run.get("fingerprint"),
                       "assessment_evidence": evidence}
            updated = self.store.update("run_bugs", link["id"], changes)
            self._event(run, "bug_reassessed", bug_id=bug_id, **changes)
            return updated

    def triage(self, bug_id, classification, proposal, reason):
        require(isinstance(classification, str) and classification in CLASSIFICATIONS, "invalid_classification", "Unknown Bug classification")
        require(isinstance(proposal, dict), "invalid_proposal", "proposal must be an object")
        reason = _text(reason, "reason")
        fields = {key: _text(proposal.get(key), key) for key in PROPOSAL_FIELDS}
        with self.store.transaction():
            bug = self.store.get("bugs", bug_id)
            run, link = self._context(bug)
            fields["evidence"] = self._evidence(fields["evidence"], run)
            needs_decision = classification == "NEEDS_DECISION" or (classification == "AUTO_ELIGIBLE" and run["repair_policy"] == "confirm-every-fix")
            options = []
            recommended = proposal.get("recommended")
            if needs_decision:
                supplied = proposal.get("options")
                if supplied is None and classification == "AUTO_ELIGIBLE":
                    supplied = [{"id": "fix", "description": fields["summary"], "action": "fix"},
                                {"id": "reject", "description": "Reject this proposal", "action": "reject"}]
                    recommended = "fix"
                require(isinstance(supplied, list) and supplied, "invalid_proposal", "Decision needs nonempty options")
                for option in supplied:
                    require(isinstance(option, dict), "invalid_proposal", "Each option must be an object")
                    action = option.get("action", "fix")
                    require(isinstance(action, str) and action in ACTIONS, "invalid_proposal", "Unsupported decision action")
                    options.append({"id": _text(option.get("id"), "option.id"),
                                    "description": _text(option.get("description"), "option.description"), "action": action})
                ids = [option["id"] for option in options]
                require(len(ids) == len(set(ids)) and recommended in ids, "invalid_proposal", "Options must be unique and recommended must identify one")
            decision_scope = proposal.get("decision_scope", "requirement")
            require(decision_scope in ("requirement", "run"), "invalid_proposal", "Invalid decision scope")
            old = self.store.list("proposals", bug_id=bug_id)
            current = self.store.create("proposals", {**self._run_scope(run), **fields, "bug_id": bug_id, "version": len(old) + 1,
                                                       "status": "CURRENT", "classification": classification, "reason": reason,
                                                       "options": options, "recommended": recommended, "decision_scope": decision_scope,
                                                       "scope_revision": run["scope_revision"], "scope_digest": run["scope_digest"],
                                                       "input_fingerprint": run.get("fingerprint")})
            for previous in old:
                if previous["status"] == "CURRENT":
                    self.store.update("proposals", previous["id"], {"status": "SUPERSEDED", "superseded_by": current["id"]})
                    if previous.get("decision_id"):
                        decision = self.store.get("decisions", previous["decision_id"])
                        self.store.update("decisions", decision["id"], {"status": "SUPERSEDED", "previous_status": decision["status"], "superseded_by": current["id"]})
            result = {}
            if needs_decision:
                decision = self.store.create("decisions", {**self._run_scope(run), "bug_id": bug_id, "proposal_id": current["id"],
                                                           "status": "PENDING", "options": options, "recommended": recommended,
                                                           "scope_revision": run["scope_revision"], "scope_digest": run["scope_digest"],
                                                           "input_fingerprint": run.get("fingerprint"), "decision_scope": decision_scope})
                current = self.store.update("proposals", current["id"], {"decision_id": decision["id"]})
                result["decision"] = decision
            bug = self.store.update("bugs", bug_id, {"classification": classification, "current_proposal_id": current["id"],
                                                    "status": "NEEDS_DECISION" if needs_decision else "OPEN"})
            self.store.update("run_bugs", link["id"], {"current_proposal_id": current["id"], "status": "OPEN", "verification": None,
                                                      "assessed_proposal_id": None, "assessed_input_fingerprint": None, "assessment_evidence": None,
                                                      "scope_revision": run["scope_revision"], "scope_digest": run["scope_digest"]})
            self._event(run, "bug_triaged", bug_id=bug_id, proposal_id=current["id"], classification=classification)
            return {"bug": bug, "proposal": current, **result}

    def resolve(self, decision_id, option_id, response, source):
        response, source = _text(response, "response"), _text(source, "source")
        with self.store.transaction():
            decision = self.store.get("decisions", decision_id)
            bug = self.store.get("bugs", decision["bug_id"])
            run, link = self._context(bug)
            proposal = self._proposal(bug, link, run, inputs=True)
            require(proposal["id"] == decision["proposal_id"], "stale_proposal", "Decision refers to a superseded proposal")
            self._decision(proposal, run)
            choices = [option for option in decision["options"] if option["id"] == option_id]
            require(len(choices) == 1, "invalid_decision", "Unknown decision option")
            if decision["status"] != "PENDING":
                require(decision.get("option_id") == option_id and decision.get("response") == response and decision.get("source") == source,
                        "decision_closed", "A resolved decision cannot be overwritten")
                return decision
            action = choices[0]["action"]
            status = {"fix": "APPROVED", "reject": "REJECTED", "defer": "DEFERRED", "accept-risk": "ACCEPTED_RISK"}[action]
            decision = self.store.update("decisions", decision_id, {"status": status, "action": action, "option_id": option_id,
                                                                   "response": response, "source": source, "resolved_run_id": run["id"]})
            self.store.update("bugs", bug["id"], {"status": "FIX_APPROVED" if action == "fix" else status})
            self.store.update("run_bugs", link["id"], {"status": "ACCEPTED_RISK" if action == "accept-risk" else "OPEN",
                                                      "disposition_decision_id": decision_id})
            self._event(run, "decision_resolved", decision_id=decision_id, bug_id=bug["id"], action=action, source=source)
            return decision

    def check_fix(self, run, bug_ids):
        require(isinstance(bug_ids, list) and bug_ids and all(isinstance(value, str) and value for value in bug_ids)
                and len(bug_ids) == len(set(bug_ids)), "invalid_input", "Fix requires unique explicit Bug IDs")
        with self.store.transaction():
            run = self._run(run["id"], active=True)
            authorize(run, "code_fix")
            require(run["repair_policy"] in ("guarded", "confirm-every-fix"), "not_authorized", "Run is report-only")
            approved = []
            for bug_id in bug_ids:
                bug, link = self._link(run, bug_id)
                require(link["applicability"] == "APPLICABLE" and link["status"] == "OPEN", "bug_not_applicable", "Bug is not open for a repair in this run")
                proposal = self._proposal(bug, link, run, inputs=True)
                require(proposal["classification"] != "NEEDS_INVESTIGATION", "needs_investigation", "Investigate before proposing a repair")
                decision = self._decision(proposal, run)
                if proposal["classification"] == "NEEDS_DECISION" or run["repair_policy"] == "confirm-every-fix" or decision:
                    require(decision and decision["status"] == "APPROVED" and decision.get("action") == "fix",
                            "needs_decision", "Current proposal requires explicit approval to fix")
                approved.append(proposal)
            return approved

    def pending(self, run_id):
        run = self._run(run_id)
        result = []
        for row in self.list(run_id):
            link = row["run_bug"]
            if link["applicability"] != "APPLICABLE" or not link.get("current_proposal_id") or link["current_proposal_id"] != row.get("current_proposal_id"):
                continue
            proposal = self.store.get("proposals", link["current_proposal_id"])
            current_scope = (run["scope_revision"], run["scope_digest"])
            if ((link["scope_revision"], link["scope_digest"]) != current_scope
                    or (proposal["scope_revision"], proposal["scope_digest"]) != current_scope
                    or self._applicable_inputs(proposal, link) != run.get("fingerprint")):
                continue
            if proposal.get("decision_id"):
                decision = self.store.get("decisions", proposal["decision_id"])
                if decision["status"] == "PENDING" and (decision.get("decision_scope") != "run" or decision["run_id"] == run_id):
                    result.append(decision)
        return result

    def reopen(self, run_id, bug_id, reason):
        reason = _text(reason, "reason")
        with self.store.transaction():
            run = self._run(run_id, active=True)
            bug, link = self._link(run, bug_id)
            if link["status"] in ("VERIFIED", "ACCEPTED_RISK", "EXCLUDED"):
                self.store.update("run_bugs", link["id"], {"status": "OPEN", "applicability": "APPLICABLE", "verification": None,
                                                          "current_proposal_id": None, "disposition_decision_id": None,
                                                          "scope_revision": run["scope_revision"], "scope_digest": run["scope_digest"]})
                bug = self.store.update("bugs", bug_id, {"status": "OPEN", "classification": "NEEDS_INVESTIGATION", "current_proposal_id": None})
            self._event(run, "bug_failure_reported", bug_id=bug_id, reason=reason, previous_status=link["status"])
            return bug

    def mark_verified(self, run_id, bug_ids, evidence_id):
        with self.store.transaction():
            run = self._run(run_id, active=True)
            artifact = self.store.get("artifacts", evidence_id, run["workspace_id"])
            require(artifact["run_id"] == run_id and artifact["requirement_id"] == run["requirement_id"], "scope_mismatch", "Verification evidence belongs to another run")
            require(bool(self.store.read_artifact(evidence_id)), "invalid_evidence", "Verification evidence is empty")
            for bug_id in bug_ids:
                bug, link = self._link(run, bug_id)
                require(link["applicability"] == "APPLICABLE", "bug_not_applicable", "Only assessed Bugs can be verified")
                proposal = self._proposal(bug, link, run)
                decision = self._decision(proposal, run)
                require(proposal["classification"] != "NEEDS_INVESTIGATION", "needs_investigation", "Investigation is incomplete")
                require(not decision or (decision["status"] == "APPROVED" and decision.get("action") == "fix"),
                        "needs_decision", "Cannot mark an unresolved proposal as a verified repair")
                self.store.update("run_bugs", link["id"], {"status": "VERIFIED", "verification": {"evidence_id": evidence_id,
                                                                          "fingerprint": run.get("fingerprint"), "proposal_id": proposal["id"]}})
                self.store.update("bugs", bug_id, {"status": "RESOLVED"})
                self._event(run, "bug_verified", bug_id=bug_id, evidence_id=evidence_id, proposal_id=proposal["id"])

    def assert_complete(self, run_id):
        with self.store.transaction():
            run = self._run(run_id)
            links = self.store.list("run_bugs", run_id=run_id)
            all_bugs = [bug for bug in self.store.list("bugs", workspace_id=run["workspace_id"], requirement_id=run["requirement_id"])
                        if bug["run_id"] == run_id or bug["created_at"] < run["created_at"]]
            linked = {link["bug_id"] for link in links}
            require(all(bug["id"] in linked for bug in all_bugs), "unresolved_bugs", "Historical Bugs have not been linked and assessed")
            unresolved = []
            for link in links:
                self._current_scope(link, run)
                if link["applicability"] == "EXCLUDED" and (not link.get("required", True) or link.get("user_response")):
                    continue
                if link["applicability"] != "APPLICABLE":
                    unresolved.append(link["bug_id"])
                    continue
                bug, _ = self._link(run, link["bug_id"])
                if link["status"] == "VERIFIED":
                    verification = link["verification"]
                    require(verification["proposal_id"] == bug.get("current_proposal_id"), "stale_proposal", "Verified proposal changed")
                    require(verification["fingerprint"] == run.get("fingerprint"), "stale_evidence", "Bug verification does not cover current code")
                    self.store.read_artifact(verification["evidence_id"])
                elif link["status"] == "ACCEPTED_RISK":
                    proposal = self._proposal(bug, link, run)
                    decision = self._decision(proposal, run)
                    require(decision and decision["status"] == "ACCEPTED_RISK" and decision.get("response") and decision.get("source"),
                            "needs_decision", "Risk disposition requires an applicable explicit decision")
                else:
                    unresolved.append(link["bug_id"])
            require(not unresolved, "unresolved_bugs", "Required Bugs are still unchecked or unresolved", bug_ids=unresolved)
