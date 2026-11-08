#!/usr/bin/env python3
"""Exercise real Herdr delivery and the review workflow without running a model.

Run from any directory: python3 tools/smoke_herdr.py --evidence-out /tmp/smoke.json
Only a unique smoke HC_SESSION and scratch XDG directories are started/cleaned.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid

FIXTURE = "An inert review fixture. No model or external service is involved.\n"


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def osc(state):
    # Herdr's OSC 7501 grammar is colon-separated key/value fields, not JSON.
    print("\x1b]7501;set:app=htl-smoke:state=" + state + "\x07", end="", flush=True)


def cli(prefix, arguments, env):
    completed = subprocess.run([*prefix, *arguments, "--json"], env=env, capture_output=True, text=True, timeout=25)
    if completed.returncode:
        raise RuntimeError("CLI failed: " + completed.stdout.strip() + " " + completed.stderr.strip())
    envelope = json.loads(completed.stdout)
    if envelope.get("status") != "ok":
        raise RuntimeError("Unexpected CLI envelope: " + json.dumps(envelope))
    return envelope["data"]


def worker(args):
    """A tiny OSC-aware program that executes only known fixture CLI actions."""
    evidence_dir = Path(args.evidence_dir).resolve()
    env = {**os.environ, "PYTHONPATH": str(Path(args.source_root).resolve()), "PYTHONDONTWRITEBYTECODE": "1"}
    prefix = context = assignment = None
    osc("idle")
    for raw in sys.stdin:
        line = raw.replace("\x1b[200~", "").replace("\x1b[201~", "").strip()
        try:
            if "领取任务：" in line and assignment is None:
                osc("working")
                command = shlex.split(line.split("领取任务：", 1)[1])
                if Path(command[0]).resolve() != Path(sys.executable).resolve() or command[1:3] != ["-m", "herdr_team_loop"]:
                    raise RuntimeError("Smoke accepts only its own Python CLI entrypoint")
                index = next(i for i in range(len(command) - 1) if command[i:i + 2] == ["assignment", "claim"])
                prefix = command[:index]
                assignment_id = command[command.index("--assignment") + 1]
                if prefix[prefix.index("--actor") + 1] != assignment_id:
                    raise RuntimeError("Actor and assignment differ")
                assignment = cli(prefix, ["assignment", "claim", "--assignment", assignment_id], env)
                context = cli(prefix, ["assignment", "context", "--assignment", assignment_id], env)
                receipt = {"assignment_id": assignment_id, "terminal": os.environ.get("HC_TERMINAL_ID"),
                           "slot": assignment["slot"], "status": assignment["status"], "claim_command": command,
                           "received_initial_claim": line}
                write_json(evidence_dir / (assignment_id + ".claim.json"), receipt)
                if assignment["slot"] == "worker":
                    if assignment["phase"] != "review":
                        raise RuntimeError("Smoke worker only performs an inert review")
                    source = Path(context["run"]["repos"][0]) / "fixture.txt"
                    content = source.read_text(encoding="utf-8")
                    if content != FIXTURE:
                        raise RuntimeError("Unexpected fixture content")
                    report = {"result": "pass", "summary": "Read-only fixture review passed",
                              "coverage": ["Read fixture.txt and checked its entire exact content"], "not_covered": [],
                              "evidence": [str(source), "Actual content matched the smoke fixture"], "bug_ids": [],
                              "input_fingerprint": assignment["fingerprint"]["digest"],
                              "fixture_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
                    report_path = evidence_dir / (assignment_id + ".report.json")
                    write_json(report_path, report)
                    submitted = cli(prefix, ["report", "submit", "--assignment", assignment_id, "--report-file", str(report_path)], env)
                    write_json(evidence_dir / (assignment_id + ".submitted.json"), submitted)
                    print("SMOKE_REPORTED " + submitted["id"], flush=True)
                else:
                    print("SMOKE_COORDINATOR_CLAIMED " + assignment_id, flush=True)
                osc("done")
            elif line.startswith("SMOKE_ACCEPT ") and context is not None:
                osc("working")
                if assignment["slot"] != "coordinator":
                    raise RuntimeError("Only the fixture coordinator accepts reports")
                report_id = line.split()[1]
                report = cli(prefix, ["report", "show", "--report", report_id], env)
                source = Path(context["run"]["repos"][0]) / "fixture.txt"
                digest = hashlib.sha256(source.read_bytes()).hexdigest()
                if (report["report"].get("fixture_sha256") != digest or source.read_text(encoding="utf-8") != FIXTURE
                        or report["report"]["result"] != "pass"):
                    raise RuntimeError("Independent coordinator readback did not match the report")
                accepted = cli(prefix, ["report", "accept", "--report", report_id,
                                       "--rationale", "Read immutable report and independently matched exact fixture content and SHA256"], env)
                finished = cli(prefix, ["run", "finish", "--run", context["run"]["id"],
                                       "--summary", "Real Herdr delivered the task, claimed worker reported, and coordinator independently accepted review"], env)
                if accepted["phase"] != "acceptance" or finished["status"] != "DONE":
                    raise RuntimeError("Review workflow did not complete")
                write_json(evidence_dir / "completion.json", {"accepted": accepted, "finished": finished})
                print("SMOKE_DONE " + finished["id"], flush=True)
                osc("done")
        except Exception as error:
            write_json(evidence_dir / ("worker-error-" + str(os.getpid()) + ".json"), {"error": str(error), "line": line})
            print("SMOKE_ERROR " + str(error), flush=True)
            osc("error")
    return 0


def smoke(args):
    source_root = Path(__file__).resolve().parents[1] / "src"
    sys.path.insert(0, str(source_root))
    from herdr_team_loop.runs import RunService
    from herdr_team_loop.runtime import HerdrRuntime
    from herdr_team_loop.storage import Store
    from herdr_team_loop.workspace import WorkspaceService

    hc = shutil.which("hc")
    if not hc:
        raise RuntimeError("hc is not installed")
    scratch = Path(tempfile.mkdtemp(prefix="htls-", dir="/private/tmp" if Path("/private/tmp").is_dir() else None)).resolve()
    session = "smk-" + uuid.uuid4().hex[:8]
    env = {**os.environ, "HC_SESSION": session, "PYTHONPATH": str(source_root), "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("HC_TERMINAL_ID", None)
    for variable, suffix in (("XDG_CONFIG_HOME", "c"), ("XDG_DATA_HOME", "d"), ("XDG_STATE_HOME", "s"),
                             ("XDG_CACHE_HOME", "a"), ("XDG_RUNTIME_DIR", "r")):
        directory = scratch / suffix
        directory.mkdir(mode=0o700)
        env[variable] = str(directory)
    evidence_dir = scratch / "evidence"
    evidence_dir.mkdir()
    store = runtime = None
    run_id = workspace_id = None
    result = {"status": "failed", "hc_session": session, "scratch": str(scratch), "model_processes": 0}

    def hc_call(*arguments):
        # This closure always uses the unique session and its scratch XDG roots.
        return subprocess.run([hc, *arguments], env=env, capture_output=True, text=True, timeout=45)

    def wait_for(terminal, description):
        observation = runtime.wait(terminal, timeout=20)
        result.setdefault("waits", []).append({"step": description, "result": observation})
        errors = list(evidence_dir.glob("worker-error-*.json"))
        if errors:
            raise RuntimeError(errors[0].read_text(encoding="utf-8"))

    try:
        repo = scratch / "project"
        repo.mkdir()
        for command in (["git", "init", "-b", "master", str(repo)],
                        ["git", "-C", str(repo), "config", "user.name", "No-model smoke"],
                        ["git", "-C", str(repo), "config", "user.email", "smoke@example.invalid"]):
            subprocess.run(command, env=env, capture_output=True, check=True, timeout=20)
        (repo / "fixture.txt").write_text(FIXTURE, encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "fixture.txt"], env=env, check=True, capture_output=True, timeout=20)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "Inert smoke fixture"], env=env, check=True, capture_output=True, timeout=20)
        store = Store(scratch / "loop-state")
        workspace_service = WorkspaceService(store)
        workspace_service.register_profile({"id": "python-osc-smoke", "command": [sys.executable, "-u", str(Path(__file__).resolve()),
                                                  "--worker", "--source-root", str(source_root), "--evidence-dir", str(evidence_dir)]})
        roles = {role: {"primary": "python-osc-smoke"} for role in ("coordinator", "reviewer")}
        workspace = workspace_service.initialize(str(repo), roles)
        workspace_id = workspace["id"]
        requirement = workspace_service.create_requirement(workspace_id, "No-model Herdr review smoke",
                         "Read only fixture.txt. Verify its exact inert text, submit actual evidence, and independently accept the review.")
        runtime = HerdrRuntime(executable=hc, env=env, timeout=45)
        service = RunService(store, runtime)
        state = service.start(workspace_id, requirement["id"], mode="review", repos=[str(repo)], repair_policy="report-only",
                              authorization={"source": "Explicit no-model smoke", "code_fix": False, "deploy": False}, active_minutes=2)
        run_id = state["id"]
        coordinator_id = state["coordinator_id"]
        coordinator = store.get("assignments", coordinator_id)
        wait_for(coordinator["terminal"], "coordinator initial message and CLI claim")
        coordinator = store.get("assignments", coordinator_id)
        if coordinator["status"] != "RUNNING" or not coordinator["first_message_sent"]:
            raise RuntimeError("Coordinator did not receive and claim the real task")
        reviewer = service.dispatch(run_id, coordinator_id)
        wait_for(reviewer["terminal"], "reviewer initial message, claim and CLI report submission")
        reports = store.list("reports", run_id=run_id, assignment_id=reviewer["id"])
        if len(reports) != 1 or reports[0]["status"] != "SUBMITTED":
            raise RuntimeError("Reviewer did not submit exactly one real report")
        runtime.send(coordinator["terminal"], "SMOKE_ACCEPT " + reports[0]["id"])
        wait_for(coordinator["terminal"], "coordinator CLI readback, accept and finish")
        final = store.get("runs", run_id)
        if final["status"] != "DONE" or not (evidence_dir / "completion.json").is_file():
            raise RuntimeError("Coordinator did not finish the actual review workflow")
        assignments = store.list("assignments", run_id=run_id)
        claims = [json.loads((evidence_dir / (item["id"] + ".claim.json")).read_text()) for item in assignments]
        result.update(status="passed", run_id=run_id, final_status=final["status"], report_id=reports[0]["id"],
                      assignments=[{key: item[key] for key in ("id", "role", "terminal", "first_message_sent", "status")} for item in assignments],
                      claims=claims, events=[event["kind"] for event in store.events(run_id)],
                      fixture_sha256=hashlib.sha256((repo / "fixture.txt").read_bytes()).hexdigest())
    except Exception as error:
        result["error"] = str(error)
        result["error_chain"] = []
        cause = error
        while cause is not None:
            result["error_chain"].append({"type": type(cause).__name__, "message": str(cause), "details": getattr(cause, "details", {})})
            cause = cause.__cause__
        if store and runtime:
            result["diagnostics"] = []
            for assignment in store.list("assignments"):
                if assignment.get("terminal"):
                    try:
                        result["diagnostics"].append({"assignment": assignment["id"], "output": runtime.read(assignment["terminal"], lines=30)})
                    except Exception as read_error:
                        result["diagnostics"].append({"assignment": assignment["id"], "error": str(read_error)})
    finally:
        cleanup = []
        # Never fall back to the caller's session, even if a launch failed partway.
        assert env["HC_SESSION"] == session and session.startswith("smk-")
        assert all(Path(env[key]).is_relative_to(scratch) for key in
                   ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR"))
        try:
            listed = hc_call("ls", "--all", "--json", "--label", "ht_workspace=" + str(workspace_id))
            terminals = json.loads(listed.stdout).get("terminals", []) if listed.returncode == 0 else []
            known_runs = {run["id"] for run in store.list("runs")} if store else set()
            addresses = {assignment["terminal"] for assignment in store.list("assignments")
                         if assignment.get("terminal") and assignment["run_id"] in known_runs
                         and assignment["workspace_id"] == workspace_id} if store else set()
            for terminal in terminals:
                # The server-side label selector also recovers an unbound launch.
                addresses.add(terminal["address"])
            for address in sorted(addresses):
                killed = hc_call("kill", "--json", address)
                removed = hc_call("rm", "--json", address)
                cleanup.append({"terminal": address, "kill_exit": killed.returncode, "remove_exit": removed.returncode})
                if removed.returncode:
                    result.update(status="failed", cleanup_error="Failed to remove owned smoke terminal " + address)
        except Exception as cleanup_error:
            result.update(status="failed", cleanup_error=str(cleanup_error))
        # Even a terminal lookup/removal failure must not leave the isolated core running.
        try:
            stopped = hc_call("server", "stop", "--json")
            cleanup.append({"isolated_core": session, "stop_exit": stopped.returncode})
            if stopped.returncode:
                result.update(status="failed", cleanup_error=stopped.stderr.strip())
        except Exception as cleanup_error:
            result.update(status="failed", cleanup_error=str(cleanup_error))
        if store:
            store.close()
        result["cleanup"] = cleanup
        result["artifacts_retained"] = args.keep_artifacts or bool(result.get("cleanup_error"))
        if not result["artifacts_retained"]:
            shutil.rmtree(scratch)
        if args.evidence_out:
            destination = Path(args.evidence_out).expanduser().resolve()
            destination.parent.mkdir(parents=True, exist_ok=True)
            write_json(destination, result)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0 if result["status"] == "passed" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-out")
    parser.add_argument("--keep-artifacts", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--source-root", help=argparse.SUPPRESS)
    parser.add_argument("--evidence-dir", help=argparse.SUPPRESS)
    args = parser.parse_args()
    return worker(args) if args.worker else smoke(args)


if __name__ == "__main__":
    sys.exit(main())
