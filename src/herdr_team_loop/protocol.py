"""Machine-readable collaboration contract kept beside the implementation."""


def protocol() -> dict:
    return {
        "schema_version": 1,
        "command": "herdr-team-loop-cli",
        "context": "Use the data directory and --actor assignment ID supplied at launch. Never invent IDs or change the active requirement implicitly.",
        "global_options": ["--data-dir PATH", "--workspace PATH", "--actor ASSIGNMENT_ID", "--json"],
        "worker_steps": ["assignment claim --assignment ID", "assignment context --assignment ID",
                         "Perform only the assigned role and authorized work", "report submit --assignment ID --file report.json"],
        "coordinator_steps": ["assignment claim --assignment ID", "run next --run RUN_ID", "run dispatch --run RUN_ID",
                              "run watch --run RUN_ID --timeout 30", "report show --report REPORT_ID",
                              "Verify actual evidence, then report accept --report REPORT_ID --rationale TEXT",
                              "run finish only after all required stages and Bugs are resolved"],
        "report": {
            "result": "pass | fail | blocked", "summary": "Nonempty factual outcome", "coverage": ["Verified criterion/case"],
            "not_covered": ["Explicit gaps"], "evidence": ["Command, observed output or artifact reference"],
            "input_fingerprint": "Copy from the assignment, never replace with a different value",
            "bug_ids": ["Failed Bug IDs for fail; explicitly retested and verified Bug IDs for pass. Assignment membership alone is not verification."]},
        "bug_triage": {
            "classification": "NEEDS_INVESTIGATION | AUTO_ELIGIBLE | NEEDS_DECISION",
            "proposal": {"summary": "Plan", "evidence": "Facts or artifact ID", "authorization_reference": "Existing authorization",
                         "impact": "Behavior, scope and dependencies", "validation_plan": "How to verify",
                         "options": [{"id": "option_a", "description": "Tradeoff", "action": "fix|reject|defer|accept-risk"}],
                         "recommended": "option_a"},
            "rule": "Material business/scope/compatibility choices need an explicit user response. Number of possible implementations is not the criterion."},
        "environment_evidence": {
            "environment": "Must match run", "input_fingerprint": "Current run fingerprint digest", "verified_at": "Actual verification time",
            "evidence": ["Platform observation"], "project": "Required for nonlocal environments", "config_version": "Actual configuration identifier",
            "test_objects": ["Actual test objects"], "applications": [{"name": "Application", "version": "Actual build/deployment",
                "source_mapping": "Verified source or build mapping", "deployment_evidence": "Platform result"}],
            "deployment_performed": "Boolean; true requires explicit deployment authorization"},
        "invariants": ["Only the current coordinator advances stages", "Worker reports require independent acceptance",
                       "Do not bypass CLI by editing state or using hc for team coordination",
                       "Retain existing project rules and unrelated dirty files",
                       "Unknown delivery means reconcile, never duplicate launch", "SC evidence must identify the actual deployed version",
                       "No daemon or automatic model wake-up; preserve checkpoint and resume coordination after interruption"],
        "recovery": ["assignment reconcile observes original execution; assignment observe reads terminal output",
                     "assignment confirm-delivery --evidence TEXT records verified receipt, never resends",
                     "assignment deliver sends the original task once only when its managed terminal proves initial delivery false and is idle/done; an uncertain attempt must not be retried",
                     "For an explicit null terminal status only, the initiating operator may supply --ready-evidence-file PATH and the observed --ready-turn N; assignment actors cannot assert this readiness",
                     "If the original delivered text is visibly still pending, the initiating operator may use assignment submit-pending --evidence-file PATH --observed-turn N once; it sends only Enter, never pastes or launches again, and an uncertain submission must not be retried",
                     "run extend-budget --active-minutes TOTAL --reason TEXT --authorization-source TEXT requires actual authorization and the initiating operator; it never resets elapsed time, rounds, phases or the absolute deadline",
                     "assignment terminate preserves a blocked worker until operation checks and handoff/retirement",
                     "bug reassess --run ID --bug ID --evidence TEXT reuses an unchanged proposal after unrelated input changes",
                     "run resume --revalidate with checkpoint, --operations-checked and --old-owner-stopped schedules missing validation"],
    }
