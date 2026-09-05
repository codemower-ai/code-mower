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
                "claude_review": {
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
                which_fn=lambda cmd: "/opt/bin/claude" if cmd == "claude" else None,
                auth_probe_runner=runner,
                providers=["claude"],
            )
        self.assertEqual(_auth_checks(checks), [])
        adapter = [c for c in checks if c.name == "doctor.campaign.adapter"]
        self.assertEqual(adapter[0].status, STATUS_PASS)

    def test_antigravity_requires_ambient_home_opt_in(self) -> None:
        config = {
            "lanes": {
                "antigravity_cli": {
                    "provider_config": {
                        "campaign_adapter_argv": ["{command}", "qualify", "--output", "{output}"],
                    }
                }
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            # Missing opt-in warns and is excluded from ready_providers
            checks = check_adoption_campaign_readiness(
                config=config,
                repo_root=Path(tmp),
                env={},
                which_fn=lambda cmd: "/opt/bin/agy" if cmd == "agy" else None,
                providers=["antigravity"],
            )
            auth_check = next(c for c in checks if c.name == CAMPAIGN_AUTH_CHECK_NAME)
            self.assertEqual(auth_check.status, STATUS_WARN)
            self.assertEqual(auth_check.lane, "antigravity")
            self.assertEqual(auth_check.detail.get("auth_probe"), "missing_opt_in")
            readiness = next(c for c in checks if c.name == "doctor.campaign.readiness")
            self.assertNotIn("antigravity", readiness.detail.get("ready_providers", []))
            self.assertEqual(
                readiness.detail.get("provider_readiness", {}).get("antigravity"),
                {"command": True, "auth": "missing_opt_in", "structured_result": True},
            )

            # Present opt-in passes and is included in ready_providers
            checks_opted_in = check_adoption_campaign_readiness(
                config=config,
                repo_root=Path(tmp),
                env={"ANTIGRAVITY_CLI_USE_AMBIENT_HOME": "1"},
                which_fn=lambda cmd: "/opt/bin/agy" if cmd == "agy" else None,
                providers=["antigravity"],
            )
            auth_pass = next(c for c in checks_opted_in if c.name == CAMPAIGN_AUTH_CHECK_NAME)
            self.assertEqual(auth_pass.status, STATUS_PASS)
            self.assertEqual(auth_pass.lane, "antigravity")
            readiness_pass = next(c for c in checks_opted_in if c.name == "doctor.campaign.readiness")
            self.assertIn("antigravity", readiness_pass.detail.get("ready_providers", []))
            self.assertEqual(
                readiness_pass.detail.get("provider_readiness", {}).get("antigravity"),
                {"command": True, "auth": "ambient_opt_in", "structured_result": True},
            )

    def test_muse_requires_api_key_or_ambient_home(self) -> None:
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
            # Missing auth warns and drops from ready_providers
            checks = check_adoption_campaign_readiness(
                config=config,
                repo_root=Path(tmp),
                env={},
                which_fn=lambda cmd: "/opt/bin/muse" if cmd == "muse" else None,
                providers=["muse"],
            )
            auth_check = next(c for c in checks if c.name == CAMPAIGN_AUTH_CHECK_NAME)
            self.assertEqual(auth_check.status, STATUS_WARN)
            self.assertEqual(auth_check.detail.get("auth_probe"), "missing_auth")
            readiness = next(c for c in checks if c.name == "doctor.campaign.readiness")
            self.assertNotIn("muse", readiness.detail.get("ready_providers", []))

            # Opted in via ambient home passes
            checks_ambient = check_adoption_campaign_readiness(
                config=config,
                repo_root=Path(tmp),
                env={"MUSE_CLI_USE_AMBIENT_HOME": "1"},
                which_fn=lambda cmd: "/opt/bin/muse" if cmd == "muse" else None,
                providers=["muse"],
            )
            auth_pass = next(c for c in checks_ambient if c.name == CAMPAIGN_AUTH_CHECK_NAME)
            self.assertEqual(auth_pass.status, STATUS_PASS)
            readiness_ambient = next(c for c in checks_ambient if c.name == "doctor.campaign.readiness")
            self.assertIn("muse", readiness_ambient.detail.get("ready_providers", []))

            # Opted in via META_API_KEY passes
            checks_key = check_adoption_campaign_readiness(
                config=config,
                repo_root=Path(tmp),
                env={"META_API_KEY": "dummy-key"},
                which_fn=lambda cmd: "/opt/bin/muse" if cmd == "muse" else None,
                providers=["muse"],
            )
            auth_key = next(c for c in checks_key if c.name == CAMPAIGN_AUTH_CHECK_NAME)
            self.assertEqual(auth_key.status, STATUS_PASS)
            readiness_key = next(c for c in checks_key if c.name == "doctor.campaign.readiness")
            self.assertIn("muse", readiness_key.detail.get("ready_providers", []))

    def test_muse_meta_api_key_file_validation(self) -> None:
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
            tmp_path = Path(tmp)

            def which_fn(cmd: str) -> str | None:
                return "/opt/bin/muse" if cmd == "muse" else None

            # 1. Missing file: path does not exist
            missing_file = tmp_path / "nonexistent.key"
            checks_missing = check_adoption_campaign_readiness(
                config=config,
                repo_root=tmp_path,
                env={"META_API_KEY_FILE": str(missing_file)},
                which_fn=which_fn,
                providers=["muse"],
            )
            auth_missing = next(c for c in checks_missing if c.name == CAMPAIGN_AUTH_CHECK_NAME)
            self.assertEqual(auth_missing.status, STATUS_WARN)
            self.assertEqual(auth_missing.detail.get("auth_probe"), "missing_auth")
            readiness_missing = next(c for c in checks_missing if c.name == "doctor.campaign.readiness")
            self.assertNotIn("muse", readiness_missing.detail.get("ready_providers", []))
            self.assertNotIn(str(missing_file), auth_missing.message)
            self.assertNotIn(str(missing_file), str(auth_missing.detail))

            # 2. Non-file (directory)
            non_file = tmp_path / "key_dir"
            non_file.mkdir()
            checks_dir = check_adoption_campaign_readiness(
                config=config,
                repo_root=tmp_path,
                env={"META_API_KEY_FILE": str(non_file)},
                which_fn=which_fn,
                providers=["muse"],
            )
            auth_dir = next(c for c in checks_dir if c.name == CAMPAIGN_AUTH_CHECK_NAME)
            self.assertEqual(auth_dir.status, STATUS_WARN)
            readiness_dir = next(c for c in checks_dir if c.name == "doctor.campaign.readiness")
            self.assertNotIn("muse", readiness_dir.detail.get("ready_providers", []))

            # 3. Unreadable file where portable
            unreadable_file = tmp_path / "unreadable.key"
            unreadable_file.write_text("secret-unreadable-token", encoding="utf-8")
            try:
                unreadable_file.chmod(0o000)
                is_unreadable = not os.access(unreadable_file, os.R_OK)
            except OSError:
                is_unreadable = False
            if is_unreadable:
                checks_unreadable = check_adoption_campaign_readiness(
                    config=config,
                    repo_root=tmp_path,
                    env={"META_API_KEY_FILE": str(unreadable_file)},
                    which_fn=which_fn,
                    providers=["muse"],
                )
                auth_unreadable = next(c for c in checks_unreadable if c.name == CAMPAIGN_AUTH_CHECK_NAME)
                self.assertEqual(auth_unreadable.status, STATUS_WARN)
                readiness_unreadable = next(c for c in checks_unreadable if c.name == "doctor.campaign.readiness")
                self.assertNotIn("muse", readiness_unreadable.detail.get("ready_providers", []))
                self.assertNotIn("secret-unreadable-token", str(auth_unreadable.detail))
                self.assertNotIn(str(unreadable_file), str(auth_unreadable.detail))
                unreadable_file.chmod(0o600)

            # 4. Empty and whitespace-only file
            empty_file = tmp_path / "empty.key"
            empty_file.write_text("", encoding="utf-8")
            checks_empty = check_adoption_campaign_readiness(
                config=config,
                repo_root=tmp_path,
                env={"META_API_KEY_FILE": str(empty_file)},
                which_fn=which_fn,
                providers=["muse"],
            )
            auth_empty = next(c for c in checks_empty if c.name == CAMPAIGN_AUTH_CHECK_NAME)
            self.assertEqual(auth_empty.status, STATUS_WARN)
            readiness_empty = next(c for c in checks_empty if c.name == "doctor.campaign.readiness")
            self.assertNotIn("muse", readiness_empty.detail.get("ready_providers", []))

            ws_file = tmp_path / "whitespace.key"
            ws_file.write_text("   \n\t \n", encoding="utf-8")
            checks_ws = check_adoption_campaign_readiness(
                config=config,
                repo_root=tmp_path,
                env={"META_API_KEY_FILE": str(ws_file)},
                which_fn=which_fn,
                providers=["muse"],
            )
            auth_ws = next(c for c in checks_ws if c.name == CAMPAIGN_AUTH_CHECK_NAME)
            self.assertEqual(auth_ws.status, STATUS_WARN)
            readiness_ws = next(c for c in checks_ws if c.name == "doctor.campaign.readiness")
            self.assertNotIn("muse", readiness_ws.detail.get("ready_providers", []))

            # 5. Malformed file (rejected assignment or comments only)
            malformed_file = tmp_path / "malformed.key"
            malformed_file.write_text("OTHER_API_KEY=token123\n", encoding="utf-8")
            checks_malformed = check_adoption_campaign_readiness(
                config=config,
                repo_root=tmp_path,
                env={"META_API_KEY_FILE": str(malformed_file)},
                which_fn=which_fn,
                providers=["muse"],
            )
            auth_malformed = next(c for c in checks_malformed if c.name == CAMPAIGN_AUTH_CHECK_NAME)
            self.assertEqual(auth_malformed.status, STATUS_WARN)
            readiness_malformed = next(c for c in checks_malformed if c.name == "doctor.campaign.readiness")
            self.assertNotIn("muse", readiness_malformed.detail.get("ready_providers", []))

            comment_file = tmp_path / "comments.key"
            comment_file.write_text("# Just a comment\n# Another comment\n", encoding="utf-8")
            checks_comment = check_adoption_campaign_readiness(
                config=config,
                repo_root=tmp_path,
                env={"META_API_KEY_FILE": str(comment_file)},
                which_fn=which_fn,
                providers=["muse"],
            )
            auth_comment = next(c for c in checks_comment if c.name == CAMPAIGN_AUTH_CHECK_NAME)
            self.assertEqual(auth_comment.status, STATUS_WARN)
            readiness_comment = next(c for c in checks_comment if c.name == "doctor.campaign.readiness")
            self.assertNotIn("muse", readiness_comment.detail.get("ready_providers", []))

            # 6. Valid file coverage (raw key, shell assignment, export assignment)
            valid_secret = "muse-super-secret-key-12345"
            for filename, content in (
                ("valid_raw.key", valid_secret),
                ("valid_shell.key", f"META_API_KEY={valid_secret}"),
                ("valid_export.key", f'export META_API_KEY="{valid_secret}"\n'),
            ):
                with self.subTest(file_type=filename):
                    valid_file = tmp_path / filename
                    valid_file.write_text(content, encoding="utf-8")
                    checks_valid = check_adoption_campaign_readiness(
                        config=config,
                        repo_root=tmp_path,
                        env={"META_API_KEY_FILE": str(valid_file)},
                        which_fn=which_fn,
                        providers=["muse"],
                    )
                    auth_valid = next(c for c in checks_valid if c.name == CAMPAIGN_AUTH_CHECK_NAME)
                    self.assertEqual(auth_valid.status, STATUS_PASS)
                    self.assertEqual(auth_valid.detail.get("auth_probe"), "api_key")
                    readiness_valid = next(c for c in checks_valid if c.name == "doctor.campaign.readiness")
                    self.assertIn("muse", readiness_valid.detail.get("ready_providers", []))
                    # Do not expose path or contents
                    self.assertNotIn(valid_secret, auth_valid.message)
                    self.assertNotIn(valid_secret, str(auth_valid.detail))
                    self.assertNotIn(valid_secret, str(readiness_valid.detail))
                    self.assertNotIn(str(valid_file), auth_valid.message)
                    self.assertNotIn(str(valid_file), str(auth_valid.detail))
                    self.assertNotIn(str(valid_file), str(readiness_valid.detail))

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
