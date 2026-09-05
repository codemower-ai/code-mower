"""Tests for bounded campaign authentication readiness probes."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from code_mower.campaign_adapters import CODEX_CAMPAIGN_HOME_ENV
from code_mower.doctor_checks import (
    STATUS_PASS,
    STATUS_SKIP,
    STATUS_WARN,
    check_adoption_campaign_readiness,
)
from code_mower.doctor_checks.campaign_auth import (
    AUTH_ERROR_PROBE_TIMEOUT,
    AUTH_ERROR_PROBE_UNAVAILABLE,
    AUTH_ERROR_UNAUTHENTICATED,
    CAMPAIGN_AUTH_CHECK_NAME,
    CAMPAIGN_AUTH_PROBE_ENV,
    campaign_auth_logged_out_exit_codes,
    campaign_auth_logged_out_markers,
    campaign_auth_probe_args,
)
from code_mower.provider_registry import REFERENCE_PROVIDERS


CODEX_CONFIG = {
    "lanes": {
        "codex": {
            "provider_config": {
                "campaign_adapter_argv": ["{command}", "qualify", "--output", "{output}"],
                "campaign_adapter_timeout_seconds": 60,
            }
        }
    }
}


def _which(cmd: str) -> str | None:
    return "/opt/bin/codex" if cmd == "codex" else None


def _completed(returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["codex", "login", "status"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _run_checks(probe_runner, *, providers=("codex",), env=None, posture="reviewer-gate"):
    """Run campaign readiness against a disposable isolated Codex home."""
    with tempfile.TemporaryDirectory() as tmp:
        codex_home = Path(tmp) / "codex-home"
        with mock.patch.dict(
            os.environ,
            {CODEX_CAMPAIGN_HOME_ENV: str(codex_home)},
            clear=False,
        ):
            return check_adoption_campaign_readiness(
                config=CODEX_CONFIG,
                repo_root=Path(tmp),
                adoption_posture=posture,
                env={} if env is None else env,
                which_fn=_which,
                auth_probe_runner=probe_runner,
                providers=list(providers),
            )


def _auth_checks(checks):
    return [check for check in checks if check.name == CAMPAIGN_AUTH_CHECK_NAME]


def _rendered(checks):
    """Render every check the way doctor JSON output would."""
    return json.dumps([check.as_dict() for check in checks], default=str)


class CampaignAuthProbeTests(unittest.TestCase):
    def test_authenticated_isolated_home_passes_without_exposing_output(self) -> None:
        recorded: list[tuple[list[str], int, dict]] = []

        def runner(argv, timeout_seconds, child_env):
            recorded.append((list(argv), timeout_seconds, dict(child_env)))
            return _completed(0, stdout="Logged in as account-name\n")

        checks = _run_checks(runner)
        auth_checks = _auth_checks(checks)
        self.assertEqual(len(auth_checks), 1)
        check = auth_checks[0]
        self.assertEqual(check.status, STATUS_PASS)
        self.assertEqual(check.lane, "codex")
        self.assertEqual(check.detail.get("auth_probe"), "authenticated")
        self.assertTrue(check.detail.get("output_redacted"))
        self.assertNotIn("account-name", repr(check.as_dict()))
        self.assertNotIn("error", check.detail)

        argv, timeout_seconds, child_env = recorded[0]
        # The probe runs the same resolved command the adapter would invoke.
        self.assertEqual(argv, ["codex", "login", "status"])
        self.assertEqual(timeout_seconds, 20)
        # The probe observes the adapter's isolated home and the OS home used
        # to reach the platform keyring, with no ambient provider tokens.
        self.assertIn("CODEX_HOME", child_env)
        self.assertIn("HOME", child_env)
        self.assertNotIn("OPENAI_API_KEY", child_env)
        self.assertNotIn("GITHUB_TOKEN", child_env)

    def test_unauthenticated_isolated_home_is_an_owner_action(self) -> None:
        checks = _run_checks(
            lambda argv, timeout, env: _completed(1, stderr="Not logged in: run codex login")
        )
        auth_checks = _auth_checks(checks)
        self.assertEqual(len(auth_checks), 1)
        check = auth_checks[0]
        self.assertEqual(check.status, STATUS_WARN)
        self.assertTrue(check.detail.get("owner_action"))
        self.assertTrue(check.detail.get("actionable"))
        self.assertEqual(check.detail.get("error"), AUTH_ERROR_UNAUTHENTICATED)
        self.assertEqual(check.detail.get("auth_probe"), "unauthenticated")
        self.assertIn("not authenticated", check.message)
        self.assertIn("docs/release-qualification.md", check.remediation)
        self.assertNotIn("run codex login", repr(check.as_dict()))

    def test_unauthenticated_provider_is_not_campaign_ready(self) -> None:
        checks = _run_checks(
            lambda argv, timeout, env: _completed(1, stderr="Not logged in\n")
        )
        readiness = [c for c in checks if c.name == "doctor.campaign.readiness"]
        self.assertEqual(len(readiness), 1)
        self.assertEqual(readiness[0].status, STATUS_WARN)
        self.assertNotIn("codex", readiness[0].detail.get("ready_providers", []))
        self.assertIn("codex", readiness[0].detail.get("actionable_providers", []))

    def test_authenticated_provider_stays_campaign_ready(self) -> None:
        checks = _run_checks(lambda argv, timeout, env: _completed(0))
        readiness = [c for c in checks if c.name == "doctor.campaign.readiness"]
        self.assertIn("codex", readiness[0].detail.get("ready_providers", []))

    def test_probe_timeout_degrades_safely(self) -> None:
        def runner(argv, timeout_seconds, child_env):
            raise subprocess.TimeoutExpired(cmd=list(argv), timeout=timeout_seconds)

        checks = _run_checks(runner)
        check = _auth_checks(checks)[0]
        self.assertEqual(check.status, STATUS_SKIP)
        self.assertEqual(check.detail.get("error"), AUTH_ERROR_PROBE_TIMEOUT)
        self.assertEqual(check.detail.get("auth_probe"), "unknown")
        self.assertFalse(check.detail.get("actionable"))
        self.assertNotIn("owner_action", check.detail)

    def test_probe_error_degrades_safely(self) -> None:
        def runner(argv, timeout_seconds, child_env):
            raise OSError("keyring unavailable at /home/example/.config")

        checks = _run_checks(runner)
        check = _auth_checks(checks)[0]
        self.assertEqual(check.status, STATUS_SKIP)
        self.assertEqual(check.detail.get("error"), AUTH_ERROR_PROBE_UNAVAILABLE)
        self.assertNotIn("keyring unavailable", repr(check.as_dict()))
        readiness = [c for c in checks if c.name == "doctor.campaign.readiness"]
        self.assertIn("codex", readiness[0].detail.get("ready_providers", []))

    def test_probe_can_be_disabled_by_environment(self) -> None:
        def runner(argv, timeout_seconds, child_env):
            raise AssertionError("probe must not run when disabled")

        checks = _run_checks(runner, env={CAMPAIGN_AUTH_PROBE_ENV: "0"})
        check = _auth_checks(checks)[0]
        self.assertEqual(check.status, STATUS_SKIP)
        self.assertEqual(check.detail.get("auth_probe"), "skipped")

    def test_orchestrator_only_posture_skips_local_auth_probe(self) -> None:
        def runner(argv, timeout_seconds, child_env):
            raise AssertionError("probe must not run in orchestrator-only posture")

        for posture in ("orchestrator-only", "hosted-builders"):
            with self.subTest(posture=posture):
                checks = _run_checks(runner, posture=posture)
                self.assertEqual(_auth_checks(checks), [])
                adapter = [c for c in checks if c.name == "doctor.campaign.adapter"]
                self.assertEqual(adapter[0].status, STATUS_SKIP)

    def test_provider_without_safe_probe_stays_capability_only(self) -> None:
        def runner(argv, timeout_seconds, child_env):
            raise AssertionError("provider declares no safe auth probe")

        config = {
            "lanes": {
                "muse_cli": {
                    "provider_config": {
                        "campaign_adapter_argv": ["{command}", "qualify", "--output", "{output}"],
                    }
                }
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            checks = check_adoption_campaign_readiness(
                config=config,
                repo_root=Path(tmp),
                env={},
                which_fn=lambda cmd: "/opt/bin/muse" if cmd == "muse" else None,
                auth_probe_runner=runner,
                providers=["muse"],
            )
        self.assertEqual(_auth_checks(checks), [])
        adapter = [c for c in checks if c.name == "doctor.campaign.adapter"]
        self.assertEqual(adapter[0].status, STATUS_PASS)

    def test_only_codex_declares_a_campaign_auth_probe(self) -> None:
        probing = sorted(
            lane_id
            for lane_id, lane in REFERENCE_PROVIDERS.items()
            if campaign_auth_probe_args(lane)
        )
        self.assertEqual(probing, ["codex"])
        self.assertEqual(
            campaign_auth_probe_args(REFERENCE_PROVIDERS["codex"]),
            ("login", "status"),
        )

    def test_confirmed_logged_out_signature_is_the_only_owner_action(self) -> None:
        """Expected exit code plus an allowlisted marker: a real owner action."""
        checks = _run_checks(
            lambda argv, timeout, env: _completed(
                1, stdout="Not logged in. Run `codex login` as user@example.com\n"
            )
        )
        check = _auth_checks(checks)[0]
        self.assertEqual(check.status, STATUS_WARN)
        self.assertEqual(check.detail.get("error"), AUTH_ERROR_UNAUTHENTICATED)
        self.assertEqual(check.detail.get("auth_probe"), "unauthenticated")
        self.assertTrue(check.detail.get("owner_action"))
        readiness = [c for c in checks if c.name == "doctor.campaign.readiness"]
        self.assertNotIn("codex", readiness[0].detail.get("ready_providers", []))
        # Only output shape reaches doctor JSON -- never the matched text.
        rendered = _rendered(checks)
        self.assertNotIn("Not logged in", rendered)
        self.assertNotIn("user@example.com", rendered)
        self.assertNotIn("codex login", rendered)
        self.assertTrue(check.detail.get("output_redacted"))

    def test_nonzero_exit_without_logged_out_marker_is_unknown(self) -> None:
        """A keyring or config failure is not evidence of a missing login."""
        checks = _run_checks(
            lambda argv, timeout, env: _completed(
                1, stderr="error: failed to read keyring entry for user@example.com\n"
            )
        )
        check = _auth_checks(checks)[0]
        self.assertEqual(check.status, STATUS_SKIP)
        self.assertEqual(check.detail.get("error"), AUTH_ERROR_PROBE_UNAVAILABLE)
        self.assertEqual(check.detail.get("auth_probe"), "unknown")
        self.assertFalse(check.detail.get("actionable"))
        self.assertNotIn("owner_action", check.detail)
        # A probe failure never removes a usable provider from readiness.
        readiness = [c for c in checks if c.name == "doctor.campaign.readiness"]
        self.assertIn("codex", readiness[0].detail.get("ready_providers", []))
        rendered = _rendered(checks)
        self.assertNotIn("keyring", rendered)
        self.assertNotIn("user@example.com", rendered)

    def test_unsupported_subcommand_is_unknown_not_unauthenticated(self) -> None:
        """An older or newer CLI without `login status` stays campaign-ready."""
        checks = _run_checks(
            lambda argv, timeout, env: _completed(
                2, stderr="error: unrecognized subcommand 'login'\n"
            )
        )
        check = _auth_checks(checks)[0]
        self.assertEqual(check.status, STATUS_SKIP)
        self.assertEqual(check.detail.get("error"), AUTH_ERROR_PROBE_UNAVAILABLE)
        self.assertEqual(check.detail.get("auth_probe"), "unknown")
        self.assertNotIn("owner_action", check.detail)
        readiness = [c for c in checks if c.name == "doctor.campaign.readiness"]
        self.assertIn("codex", readiness[0].detail.get("ready_providers", []))
        rendered = _rendered(checks)
        self.assertNotIn("unrecognized subcommand", rendered)

    def test_logged_out_marker_alone_without_expected_exit_code_is_unknown(self) -> None:
        checks = _run_checks(
            lambda argv, timeout, env: _completed(3, stderr="Not logged in\n")
        )
        check = _auth_checks(checks)[0]
        self.assertEqual(check.status, STATUS_SKIP)
        self.assertEqual(check.detail.get("error"), AUTH_ERROR_PROBE_UNAVAILABLE)

    def test_codex_declares_a_bounded_logged_out_signature(self) -> None:
        lane = REFERENCE_PROVIDERS["codex"]
        self.assertEqual(campaign_auth_logged_out_exit_codes(lane), (1,))
        self.assertEqual(
            campaign_auth_logged_out_markers(lane),
            ("not logged in", "not authenticated"),
        )
        # Providers without a probe declare no logged-out signature either.
        capability_only = REFERENCE_PROVIDERS["claude_review"]
        self.assertEqual(campaign_auth_logged_out_exit_codes(capability_only), ())
        self.assertEqual(campaign_auth_logged_out_markers(capability_only), ())


if __name__ == "__main__":
    unittest.main()
