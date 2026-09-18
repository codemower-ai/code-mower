"""Synthetic operator/probe tests; no Slack, credential or provider calls."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
from io import StringIO
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from code_mower import cli, doctor, slack_readiness as readiness, slack_setup


def observation():
    now = time.time()
    return {"schema": readiness.PROBE_SCHEMA, "nonce": "0" * 64,
        "observed_at": now, "expires_at": now + 100,
        "components": {key: rule[0] for key, rule in readiness.COMPONENTS.items()},
        "supervisor_product": "codex", "supervisor_contract": readiness.SUPERVISOR_SCHEMA,
        "caps": {"task_acu": 1, "campaign_acu": 2, "reserved_acu": 0,
            "task_limit": 2, "reserved_tasks": 0, "runtime_calls": 4,
            "runtime_seconds": 120, "review_rounds": 1, "review_budget_usd": 2,
            "clarification_answers": 1, "fix_requests": 0, "recovery_creates": 0}}


class ReadinessTests(unittest.TestCase):
    def check(self, value, name):
        return next(c for c in value["checks"] if c["name"] == name)

    def test_live_readiness_is_advisory_and_offline_cannot_pass(self):
        value = observation()
        self.assertTrue(readiness.report(value, live=True)["ready"])
        self.assertFalse(readiness.report(value, live=True)["dispatch_authorized"])
        self.assertFalse(readiness.report(value)["ready"])
        self.assertFalse(readiness.report()["ready"])
        self.assertEqual(self.check(readiness.report(), "supervisor")["state"], "not_observed")

    def test_every_component_failure_blocks_with_separate_remediation(self):
        for name, (_, states, _) in readiness.COMPONENTS.items():
            for state in states:
                with self.subTest(name=name, state=state):
                    value = observation()
                    value["components"][name] = state
                    result = readiness.report(value, live=True)
                    self.assertFalse(result["ready"])
                    self.assertEqual(self.check(result, name)["state"], state)
                    self.assertTrue(self.check(result, name)["remediation"])

    def test_registration_does_not_imply_qualified_reachability(self):
        for state in ("unqualified", "unreachable", "stale", "revoked", "mismatched"):
            value = observation()
            value["components"]["supervisor"] = state
            result = readiness.report(value, live=True)
            self.assertEqual(self.check(result, "registration")["status"], "pass")
            self.assertEqual(self.check(result, "supervisor")["status"], "fail")
        for product in ("claude", "devin", "none"):
            value = observation()
            value["supervisor_product"] = product
            self.assertFalse(readiness.report(value, live=True)["ready"])
        value = observation()
        value["supervisor_contract"] = "unsupported"
        self.assertFalse(readiness.report(value, live=True)["ready"])

    def test_stale_future_expired_and_overlong_observations_fail(self):
        for start, end in ((-121, 1), (1, 100), (-1, -1), (-1, 121), (0, 0)):
            value = observation()
            value.update(observed_at=1000 + start, expires_at=1000 + end)
            result = readiness.report(value, live=True, now=1000)
            self.assertFalse(result["ready"])
            self.assertEqual(self.check(result, "observation")["state"], "stale")

    def test_numeric_caps_and_nonrefundable_reservations(self):
        cases = [({"task_acu": 0}, "missing"), ({"campaign_acu": 0}, "missing"),
            ({"review_budget_usd": 0}, "missing"), ({"runtime_calls": 0}, "missing"),
            ({"runtime_seconds": 0}, "missing"), ({"review_rounds": 0}, "missing"),
            ({"clarification_answers": 0}, "missing"), ({"runtime_calls": 1}, "mismatched"),
            ({"runtime_seconds": 301}, "mismatched"), ({"review_rounds": 10}, "mismatched"),
            ({"clarification_answers": 33}, "mismatched"), ({"fix_requests": 9}, "mismatched"),
            ({"task_acu": 3}, "mismatched"), ({"task_limit": 51}, "mismatched"),
            ({"recovery_creates": 1}, "mismatched"), ({"reserved_acu": 2}, "exhausted"),
            ({"reserved_tasks": 2}, "exhausted")]
        for updates, expected in cases:
            with self.subTest(updates=updates):
                value = observation()
                value["caps"].update(updates)
                result = readiness.report(value, live=True)
                self.assertFalse(result["ready"])
                self.assertEqual(self.check(result, "budgets")["state"], expected)

    def test_malformed_and_private_input_never_appears_in_diagnostics(self):
        private = "synthetic-private-identity-channel-repository-token-url-task"
        original = observation()
        values = [private, [], None]
        for key in original:
            value = deepcopy(original)
            value[key] = private
            values.append(value)
        for key in original["components"]:
            value = deepcopy(original)
            value["components"][key] = private
            values.append(value)
        for bad in (True, -1, float("inf"), float("nan"), "1", None, 1.5):
            value = deepcopy(original)
            value["caps"]["task_acu"] = bad
            values.append(value)
        value = deepcopy(original)
        value["private"] = private
        values.append(value)
        for value in values:
            result = readiness.report(value, live=True)
            self.assertFalse(result["ready"])
            self.assertNotIn(private, json.dumps(result) + readiness.render(result))
        result = readiness.report(original, live=True)
        for omitted in ("nonce", "observed_at", "expires_at", "task_acu", "reserved_tasks"):
            self.assertNotIn(omitted, json.dumps(result))

    def test_parser_rejects_duplicate_keys_and_oversize(self):
        raw = json.dumps(observation()).encode()
        for value in (b'{"schema":"bad",' + raw[1:], b" " * (readiness.MAX_BYTES + 1),
                      b"[" * 100, b"\xff", b'{"x":NaN}'):
            with self.assertRaisesRegex(readiness.ReadinessError, "invalid_observation"):
                readiness.decode(value)

    def test_snapshot_is_bounded_offline_and_rejects_symlink_fifo(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "observation.json"
            path.write_text(json.dumps(observation()))
            self.assertFalse(readiness.read_snapshot(path)["ready"])
            alias = Path(root) / "alias"
            alias.symlink_to(path)
            self.assertEqual(self.check(readiness.read_snapshot(alias), "observation")["state"], "snapshot_unavailable")
            fifo = Path(root) / "fifo"
            os.mkfifo(fifo)
            self.assertEqual(self.check(readiness.read_snapshot(fifo), "observation")["state"], "snapshot_unavailable")
            path.write_bytes(b" " * (readiness.MAX_BYTES + 1))
            self.assertEqual(self.check(readiness.read_snapshot(path), "observation")["state"], "invalid_observation")


class ProbeTests(unittest.TestCase):
    def run_probe(self, body):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "probe"
            path.write_text(f"#!{sys.executable}\nimport json, sys, time, os\n" + body)
            path.chmod(0o700)
            return readiness.probe(path)

    def test_live_nonce_bound_read_only_protocol(self):
        result = self.run_probe("request = json.load(sys.stdin)\n"
            + "value = " + repr(observation()) + "\n"
            + "value.update(nonce=request['nonce'], observed_at=time.time(), expires_at=time.time()+60)\n"
            + "print(json.dumps(value))\n")
        self.assertTrue(result["ready"])
        self.assertFalse(result["dispatch_authorized"])

    def test_cached_nonce_and_pre_request_observation_are_rejected(self):
        result = self.run_probe("print(" + repr(json.dumps(observation())) + ")\n")
        self.assertFalse(result["ready"])
        self.assertEqual(result["checks"][0]["state"], "probe_mismatch")
        result = self.run_probe("request = json.load(sys.stdin)\n"
            + "value = " + repr(observation()) + "\n"
            + "value['nonce'] = request['nonce']\nprint(json.dumps(value))\n")
        self.assertEqual(result["checks"][0]["state"], "probe_mismatch")

    def test_probe_failure_overflow_timeout_and_private_errors_are_redacted(self):
        private = "synthetic-private-provider-response"
        for body, expected in (
            (f"print({private!r}, file=sys.stderr)\nsys.exit(1)\n", "probe_failed"),
            ("sys.stdout.write('x' * 20000)\n", "invalid_observation"),
            ("sys.stderr.write('x' * 20000)\n", "invalid_observation"),
            ("time.sleep(10)\n", "probe_timeout"),
            ("os.close(1)\nos.close(2)\ntime.sleep(10)\n", "probe_timeout"),
        ):
            timeout = 1 if expected == "probe_timeout" else 5
            with self.subTest(expected=expected), patch.object(readiness, "PROBE_TIMEOUT", timeout):
                started = time.monotonic()
                result = self.run_probe(body)
                self.assertLess(time.monotonic() - started, 6)
                self.assertFalse(result["ready"])
                self.assertEqual(result["checks"][0]["state"], expected)
                self.assertNotIn(private, json.dumps(result))

    def test_inherited_pipes_cannot_extend_deadline(self):
        with patch.object(readiness, "PROBE_TIMEOUT", 1):
            result = self.run_probe("if os.fork() == 0: time.sleep(10)\nelse: sys.exit(0)\n")
        self.assertEqual(result["checks"][0]["state"], "probe_timeout")

    def test_relative_and_missing_probe_fail_without_path_disclosure(self):
        for path in (Path("private-probe"), Path("/nonexistent/synthetic-private-probe")):
            result = readiness.probe(path)
            self.assertFalse(result["ready"])
            self.assertNotIn("private-probe", json.dumps(result))


class SetupTests(unittest.TestCase):
    def call(self, args):
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            try:
                code = slack_setup.main(args)
            except SystemExit as exc:
                code = exc.code
        return code, output.getvalue()

    def test_scripted_opt_in_exports_only_hosted_manifest_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "manifest.json"
            args = ["setup", "--manifest", str(path)]
            self.assertEqual(self.call(args)[0], 2)
            self.assertFalse(path.exists())
            self.assertEqual(self.call([*args, "--yes"])[0], 0)
            manifest = json.loads(path.read_bytes())
            self.assertEqual(manifest["oauth_config"]["scopes"], {"bot": ["commands"]})
            self.assertEqual(manifest["oauth_config"]["redirect_urls"], [
                "https://codemower-slack-oauth-ingress.jhuber.workers.dev/callback"
            ])
            self.assertEqual(manifest["features"]["slash_commands"][0]["url"],
                             "https://codemower.com/api/slack/commands")
            self.assertEqual(manifest["settings"]["interactivity"]["request_url"],
                             "https://codemower.com/api/slack/interactions")
            self.assertTrue(manifest["settings"]["token_rotation_enabled"])
            self.assertNotIn("event_subscriptions", manifest["settings"])
            self.assertEqual(manifest["features"]["slash_commands"][0]["command"], "/codemower")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.call([*args, "--yes"])[0], 1)
            self.assertEqual(json.loads(path.read_bytes()), manifest)

    def test_interactive_yes_no_and_no_terminal(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "manifest.json"
            args = ["setup", "--manifest", str(path), "--interactive"]
            with patch.object(sys.stdin, "isatty", return_value=False):
                self.assertEqual(self.call(args)[0], 1)
            with patch.object(sys.stdin, "isatty", return_value=True), patch("builtins.input", return_value="n"):
                self.assertEqual(self.call(args)[0], 0)
                self.assertFalse(path.exists())
            with patch.object(sys.stdin, "isatty", return_value=True), patch("builtins.input", return_value="y"):
                self.assertEqual(self.call(args)[0], 0)
                self.assertTrue(path.exists())

    def test_no_probe_does_not_start_process_or_read_environment(self):
        with patch.object(readiness.subprocess, "Popen") as process:
            code, output = self.call(["doctor", "--json"])
            self.assertEqual(code, 1)
            self.assertFalse(json.loads(output)["ready"])
            process.assert_not_called()

    def test_doctor_alias_bypasses_generic_private_output(self):
        output = StringIO()
        with redirect_stdout(output), patch.object(doctor, "run_doctor") as generic:
            self.assertEqual(cli.main(["doctor", "--slack", "--json"]), 1)
            generic.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["schema"], readiness.SCHEMA)
        with redirect_stdout(StringIO()):
            self.assertEqual(cli.main(["slack", "doctor", "--json"]), 1)

    def test_bad_arguments_and_paths_do_not_echo_private_values(self):
        private = "synthetic-private-identity"
        for args in (["doctor", "--" + private], ["doctor", "--snapshot", private],
                     ["setup", "--yes", "--manifest", private + "/missing"]):
            code, output = self.call(args)
            self.assertNotEqual(code, 0)
            self.assertNotIn(private, output)

    def test_default_participants_and_first_user_help_unchanged(self):
        self.assertNotIn("slack", cli.FIRST_USER_COMMANDS)
        # The packaged defaults contain no Slack hook, dependency or prompt.
        root = Path(slack_setup.__file__).parent
        for relative in ("templates/code-mower.example.yml", "templates/providers.yml", "init.py"):
            self.assertNotIn("slack", (root / relative).read_text().lower())


if __name__ == "__main__":
    unittest.main()
