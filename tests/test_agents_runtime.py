import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from herdr_team_loop.agents import discover_agents, validate_profile, validate_roles
from herdr_team_loop.errors import LoopError
from herdr_team_loop.runtime import HerdrRuntime


class AgentDiscoveryTests(unittest.TestCase):
    def test_discovery_only_checks_path_and_does_not_claim_authentication(self):
        with mock.patch("shutil.which", side_effect=lambda command: "/bin/" + command if command == "codex" else None), \
                mock.patch("subprocess.run", side_effect=AssertionError("process must not start")), \
                mock.patch.object(Path, "read_text", side_effect=AssertionError("credentials must not be read")):
            profiles = {item["id"]: item for item in discover_agents()}
        self.assertEqual(set(profiles), {"droid", "codex", "cursor", "kimi", "claude", "pi", "omp", "opencode"})
        self.assertEqual(profiles["codex"]["command"], ["codex", "--no-daemon"])
        self.assertTrue(profiles["codex"]["installed"])
        self.assertFalse(profiles["droid"]["installed"])
        self.assertEqual(profiles["codex"]["verification"]["authentication"], "unknown")
        self.assertEqual(profiles["codex"]["verification"]["first_message"], "unverified")

    def test_custom_array_overrides_builtin_without_shell_interpretation(self):
        with mock.patch("shutil.which", return_value="/bin/echo"):
            profiles = discover_agents([{"id": "droid", "command": ["echo", "$(never-run)"]}])
        selected = [profile for profile in profiles if profile["id"] == "droid"]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["command"], ["echo", "$(never-run)"])
        with self.assertRaises(LoopError):
            validate_profile({"id": "custom", "command": "echo unsafe"})
        with self.assertRaises(LoopError):
            validate_profile({"id": "custom", "command": ["echo", "bad\0argument"]})
        with self.assertRaises(LoopError):
            validate_profile({"id": "custom", "command": ["echo", "invalid\nfrontmatter"]})

    def test_roles_are_explicit_and_require_installed_profiles(self):
        profile = {"id": "custom", "command": [sys.executable]}
        self.assertEqual(validate_roles({"coordinator": {"primary": "custom"}, "tester": None}, [profile]),
                         {"coordinator": {"primary": "custom", "fallbacks": []}})
        for roles in ({}, {"coordinator": {"primary": "missing"}},
                      {"coordinator": {"primary": "custom", "fallbacks": ["custom"]}}):
            with self.subTest(roles=roles), self.assertRaises(LoopError):
                validate_roles(roles, [profile])
        with mock.patch("shutil.which", return_value=None), self.assertRaises(LoopError) as error:
            validate_roles({"coordinator": {"primary": "custom"}}, [profile])
        self.assertEqual(error.exception.code, "agent_unavailable")


FAKE_HC = r'''
import json, os, pathlib, sys, time
args = sys.argv[1:]
with open(os.environ['FAKE_HC_LOG'], 'a') as stream:
    stream.write(json.dumps(args) + '\n')
mode = os.environ.get('FAKE_HC_MODE', '')
if mode == 'timeout':
    time.sleep(2)
if mode == 'fail':
    print(json.dumps({'error': {'kind': 'unavailable', 'message': 'fixture'}}), file=sys.stderr)
    sys.exit(3)
if mode == 'garbled':
    print('not JSON'); sys.exit()
command = args[0]
if command == 'run':
    result = {'address': 't7', 'state': 'live', 'first_message': {'sent': mode != 'unsent'}}
    if mode == 'missing-delivery': result.pop('first_message')
elif command == 'ls':
    result = {'terminals': [] if mode == 'empty' else [{'address':'t7'}]}
    if mode == 'multiple': result['terminals'].append({'address':'t8'})
elif command == 'get':
    result = {'address':'t7', 'state':'live', 'labels':{'ht_run':'run1','ht_assignment':'a1'}}
    if mode == 'wrong-label': result['labels']['ht_assignment'] = 'different'
    if mode == 'unsent': result['first_message'] = {'sent':False,'reason':'not_ready'}
elif command == 'read':
    print('fixture screen'); sys.exit()
elif command == 'attach':
    sys.exit(6)
elif command == 'keys':
    result = {'turn':4,'status':None}
else:
    result = {'address':'t7','operation':command}
print(json.dumps(result))
'''


class HerdrRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.executable = self.root / "fake-hc"
        self.executable.write_text("#!" + sys.executable + "\n" + FAKE_HC)
        self.executable.chmod(0o755)
        self.log = self.root / "calls.jsonl"
        self.environment = {"FAKE_HC_LOG": str(self.log), "XDG_CONFIG_HOME": str(self.root / "config")}
        self.runtime = HerdrRuntime(str(self.executable), self.environment)
        self.profile = {"id": "fixture", "command": [sys.executable, "-u"]}
        self.assignment = {"id": "a1", "run_id": "run1", "workspace_id": "w1", "session_name": "htl-a1"}

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def launch(self, message="Inspect only; report through the team CLI."):
        return self.runtime.launch(self.assignment, self.profile, message, str(self.root))

    def test_native_template_launch_returns_real_fields_and_preserves_message(self):
        marker = self.root / "must-not-exist"
        message = 'Check "quoted" text; $(touch ' + str(marker) + ")\nsecond line"
        result = self.launch(message)
        self.assertEqual(result["terminal"], "t7")
        self.assertTrue(result["first_message_sent"])
        call = self.calls()[0]
        self.assertEqual(call[-1], message)
        self.assertIn("--no-parent", call)
        self.assertIn("ht_assignment=a1", call)
        self.assertIn("ht_run=run1", call)
        self.assertTrue(call[-2].startswith("htloop-"))
        self.assertFalse(marker.exists())
        template = Path(result["template_path"])
        self.assertTrue(template.is_relative_to(self.root / "config"))
        self.assertNotIn(message, template.read_text())
        command = next(line.removeprefix("command: ") for line in template.read_text().splitlines()
                       if line.startswith("command: "))
        self.assertEqual(shlex.split(command), [str(Path(sys.executable).resolve()), "-u"])

    def test_unsent_is_preserved_and_no_automatic_send_is_attempted(self):
        self.runtime.env["FAKE_HC_MODE"] = "unsent"
        self.assertFalse(self.launch()["first_message_sent"])
        self.assertEqual([call[0] for call in self.calls()], ["run"])

    def test_pending_submission_presses_enter_once_without_pasting(self):
        self.assertEqual(self.runtime.submit("t7"), {"turn": 4, "status": None})
        self.assertEqual(self.calls(), [["keys", "--json", "t7", "enter"]])

    def test_pending_submission_keeps_transport_uncertainty_and_never_retries(self):
        self.runtime.env["FAKE_HC_MODE"] = "garbled"
        with self.assertRaises(LoopError) as error:
            self.runtime.submit("t7")
        self.assertEqual(error.exception.code, "submission_unknown")
        self.assertEqual(self.calls(), [["keys", "--json", "t7", "enter"]])

    def test_uncertain_launch_never_retries(self):
        for mode in ("fail", "garbled", "missing-delivery", "timeout"):
            with self.subTest(mode=mode):
                runtime = HerdrRuntime(str(self.executable), {**self.environment, "FAKE_HC_MODE": mode},
                                       timeout=0.2 if mode == "timeout" else 3)
                before = len(self.calls())
                with mock.patch.object(runtime, "_json", wraps=runtime._json) as request:
                    with self.assertRaises(LoopError) as error:
                        runtime.launch(self.assignment, self.profile, "task", str(self.root))
                    request.assert_called_once()
                self.assertEqual(error.exception.code, "delivery_unknown")
                self.assertLessEqual(len(self.calls()), before + 1)

    def test_managed_template_is_reused_but_never_overwritten(self):
        result = self.launch()
        path = Path(result["template_path"])
        self.assertEqual(self.launch()["template_path"], str(path))
        path.write_text("user content\n")
        before = len(self.calls())
        with self.assertRaises(LoopError) as error:
            self.launch()
        self.assertEqual(error.exception.code, "template_conflict")
        self.assertEqual(path.read_text(), "user content\n")
        self.assertEqual(len(self.calls()), before)

    def test_reconcile_includes_finished_terminals_and_does_not_infer_delivery(self):
        result = self.runtime.reconcile(self.assignment)
        self.assertEqual(result["terminal"], "t7")
        self.assertFalse(result["delivery_known"])
        self.assertFalse(result["first_message_sent"])
        self.assertIn("--all", self.calls()[0])
        self.assertEqual([call[0] for call in self.calls()], ["ls", "get"])

    def test_reconcile_rejects_ambiguity_and_wrong_labels(self):
        for mode, code in (("multiple", "ambiguous_execution"), ("wrong-label", "runtime_protocol")):
            with self.subTest(mode=mode):
                self.runtime.env["FAKE_HC_MODE"] = mode
                with self.assertRaises(LoopError) as error:
                    self.runtime.reconcile(self.assignment)
                self.assertEqual(error.exception.code, code)
        self.runtime.env["FAKE_HC_MODE"] = "empty"
        self.assertIsNone(self.runtime.reconcile(self.assignment))
        self.assertNotIn("run", [call[0] for call in self.calls()])

    def test_transport_methods_and_interactive_attach(self):
        self.assertEqual(self.runtime.read("t7"), "fixture screen\n")
        self.runtime.observe("t7")
        self.runtime.wait("t7", timeout=2)
        self.runtime.send("t7", "literal $(unchanged)")
        self.runtime.interrupt("t7")
        self.runtime.terminate("t7")
        self.assertEqual(self.runtime.attach("t7"), 6)
        self.assertEqual([call[0] for call in self.calls()], ["read", "get", "wait", "send", "interrupt", "kill", "attach"])
        self.assertFalse(any("--force" in call for call in self.calls()))


if __name__ == "__main__":
    unittest.main()
