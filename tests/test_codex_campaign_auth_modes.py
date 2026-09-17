"""Tests for the explicitly selected isolated Codex campaign credential source.

The released isolated campaign home is keyring-only, which a headless Linux
host cannot hold. These tests cover the opt-in file-backed source: that it is
never entered by accident, that it keeps the campaign boundary the keyring mode
established, that the adapter and doctor apply the same readiness rule, and
that nothing about the credential reaches argv, doctor output, or public JSON.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from code_mower import campaign_adapters
from code_mower.campaign_adapters import (
    CODEX_AUTH_MODE_FILE,
    CODEX_AUTH_MODE_KEYRING,
    CODEX_AUTH_SOURCE_EXTERNAL,
    CODEX_AUTH_SOURCE_MISSING,
    CODEX_AUTH_SOURCE_PRESENT,
    CODEX_AUTH_SOURCE_UNUSABLE,
    CODEX_CAMPAIGN_AUTH_FILENAME,
    CODEX_CAMPAIGN_AUTH_MODE_ENV,
    CODEX_CAMPAIGN_CONFIG,
    CODEX_CAMPAIGN_HOME_ENV,
    DEFAULT_CODEX_AUTH_MODE,
    KEYRING_SESSION_ENV_NAMES,
    REFUSED_CODEX_AUTH_MODES,
    SUPPORTED_CODEX_AUTH_MODES,
    build_adapter_child_env,
    build_codex_campaign_config,
    codex_campaign_auth_source_state,
    prepare_codex_campaign_home,
    resolve_codex_campaign_auth_mode,
)
from code_mower.doctor_checks import STATUS_PASS, STATUS_WARN, check_adoption_campaign_readiness
from code_mower.doctor_checks.campaign_auth import (
    AUTH_ERROR_SOURCE_MISSING,
    AUTH_ERROR_SOURCE_UNUSABLE,
    AUTH_ERROR_UNSUPPORTED_MODE,
    AUTH_STATE_UNSUPPORTED_MODE,
    CAMPAIGN_AUTH_CHECK_NAME,
    CAMPAIGN_AUTH_HOME_PREPARED_KEY,
    CAMPAIGN_AUTH_MODE_ENV_KEY,
    resolve_campaign_auth_source,
)
from code_mower.provider_registry import REFERENCE_PROVIDERS


#: A credential shaped like the real one, used only to prove it never escapes.
CREDENTIAL_CANARY = "sk-campaign-canary-must-not-leak"

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


def _write_credential(home: Path) -> Path:
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    auth_path = home / CODEX_CAMPAIGN_AUTH_FILENAME
    auth_path.write_text(json.dumps({"OPENAI_API_KEY": CREDENTIAL_CANARY}), encoding="utf-8")
    return auth_path


def _run_checks(
    probe_runner,
    *,
    mode=None,
    codex_home=None,
    doctor_env=None,
    platform="linux",
):
    """Run campaign readiness against a disposable isolated Codex home."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "codex-home" if codex_home is None else codex_home
        env = dict(doctor_env or {})
        if mode is not None:
            env[CODEX_CAMPAIGN_AUTH_MODE_ENV] = mode
        ambient = {CODEX_CAMPAIGN_HOME_ENV: str(home)}
        with mock.patch.dict(os.environ, ambient, clear=False):
            with mock.patch(
                "code_mower.doctor_checks.campaign_auth.sys.platform", platform
            ):
                return check_adoption_campaign_readiness(
                    config=CODEX_CONFIG,
                    repo_root=Path(tmp),
                    adoption_posture="reviewer-gate",
                    env=env,
                    which_fn=_which,
                    auth_probe_runner=probe_runner,
                    providers=["codex"],
                    campaign_requested=True,
                )


def _auth_check(checks):
    matching = [c for c in checks if c.name == CAMPAIGN_AUTH_CHECK_NAME]
    assert len(matching) == 1, matching
    return matching[0]


def _readiness(checks):
    return next(c for c in checks if c.name == "doctor.campaign.readiness")


class ModeSelectionTests(unittest.TestCase):
    """Only an explicit, supported selection changes the credential source."""

    def test_unset_selection_keeps_released_keyring_mode(self) -> None:
        self.assertEqual(DEFAULT_CODEX_AUTH_MODE, CODEX_AUTH_MODE_KEYRING)
        for value in ("", "   "):
            with self.subTest(value=value):
                self.assertEqual(
                    resolve_codex_campaign_auth_mode({CODEX_CAMPAIGN_AUTH_MODE_ENV: value}),
                    CODEX_AUTH_MODE_KEYRING,
                )
        self.assertEqual(resolve_codex_campaign_auth_mode({}), CODEX_AUTH_MODE_KEYRING)

    def test_supported_selections_are_exactly_keyring_and_file(self) -> None:
        self.assertEqual(
            set(SUPPORTED_CODEX_AUTH_MODES), {CODEX_AUTH_MODE_KEYRING, CODEX_AUTH_MODE_FILE}
        )
        for mode in SUPPORTED_CODEX_AUTH_MODES:
            with self.subTest(mode=mode):
                self.assertEqual(
                    resolve_codex_campaign_auth_mode({CODEX_CAMPAIGN_AUTH_MODE_ENV: mode.upper()}),
                    mode,
                )

    def test_maintained_cli_modes_without_one_durable_source_are_refused(self) -> None:
        """``auto`` and ``ephemeral`` exist upstream but are not campaign sources."""
        self.assertEqual(set(REFUSED_CODEX_AUTH_MODES), {"auto", "ephemeral"})
        for mode in REFUSED_CODEX_AUTH_MODES:
            with self.subTest(mode=mode):
                with self.assertRaises(ValueError):
                    resolve_codex_campaign_auth_mode({CODEX_CAMPAIGN_AUTH_MODE_ENV: mode})

    def test_unknown_selection_raises_rather_than_falling_back(self) -> None:
        with self.assertRaises(ValueError):
            resolve_codex_campaign_auth_mode({CODEX_CAMPAIGN_AUTH_MODE_ENV: "ambient"})


class CampaignConfigTests(unittest.TestCase):
    """Both modes keep the isolation contract; only the store differs."""

    def test_keyring_rendering_is_unchanged(self) -> None:
        config = build_codex_campaign_config(CODEX_AUTH_MODE_KEYRING)
        self.assertEqual(config, CODEX_CAMPAIGN_CONFIG)
        self.assertIn('cli_auth_credentials_store = "keyring"', config)
        self.assertIn("secret_auth_storage = true", config)

    def test_file_mode_declares_the_file_store_without_keyring_features(self) -> None:
        config = build_codex_campaign_config(CODEX_AUTH_MODE_FILE)
        self.assertIn('cli_auth_credentials_store = "file"', config)
        self.assertNotIn("secret_auth_storage", config)
        self.assertNotIn("keyring", config)

    def test_every_supported_mode_keeps_the_restricted_provider_config(self) -> None:
        for mode in SUPPORTED_CODEX_AUTH_MODES:
            with self.subTest(mode=mode):
                config = build_codex_campaign_config(mode)
                self.assertIn('default_permissions = "campaign"', config)
                self.assertIn('":root" = "deny"', config)
                self.assertIn('":minimal" = "read"', config)
                self.assertIn('":workspace_roots" = "write"', config)

    def test_unsupported_mode_never_renders_a_config(self) -> None:
        for mode in ("auto", "ephemeral", "ambient", ""):
            with self.subTest(mode=mode):
                with self.assertRaises(ValueError):
                    build_codex_campaign_config(mode)


class IsolatedHomeTests(unittest.TestCase):
    """The isolated home holds one private credential under our permissions."""

    def test_keyring_mode_still_refuses_a_credential_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex-home"
            _write_credential(home)
            with self.assertRaises(ValueError):
                prepare_codex_campaign_home(home, auth_mode=CODEX_AUTH_MODE_KEYRING)

    def test_file_mode_accepts_and_tightens_the_credential(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex-home"
            auth_path = _write_credential(home)
            auth_path.chmod(0o644)
            prepared = prepare_codex_campaign_home(home, auth_mode=CODEX_AUTH_MODE_FILE)
            self.assertEqual(stat.S_IMODE(prepared.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(auth_path.stat().st_mode), 0o600)
            config = (prepared / "config.toml").read_text(encoding="utf-8")
            self.assertIn('cli_auth_credentials_store = "file"', config)

    def test_file_mode_refuses_a_symlinked_credential(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex-home"
            home.mkdir(mode=0o700, parents=True)
            elsewhere = Path(tmp) / "ambient-auth.json"
            elsewhere.write_text(json.dumps({"k": CREDENTIAL_CANARY}), encoding="utf-8")
            (home / CODEX_CAMPAIGN_AUTH_FILENAME).symlink_to(elsewhere)
            with self.assertRaises(ValueError):
                prepare_codex_campaign_home(home, auth_mode=CODEX_AUTH_MODE_FILE)

    def test_file_mode_refuses_a_credential_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex-home"
            (home / CODEX_CAMPAIGN_AUTH_FILENAME).mkdir(mode=0o700, parents=True)
            with self.assertRaises(ValueError):
                prepare_codex_campaign_home(home, auth_mode=CODEX_AUTH_MODE_FILE)

    def test_preparing_a_home_never_creates_a_credential(self) -> None:
        """Cold start: bootstrap makes the boundary, the operator logs in."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex-home"
            prepared = prepare_codex_campaign_home(home, auth_mode=CODEX_AUTH_MODE_FILE)
            self.assertFalse((prepared / CODEX_CAMPAIGN_AUTH_FILENAME).exists())
            self.assertEqual(
                codex_campaign_auth_source_state(prepared, CODEX_AUTH_MODE_FILE),
                CODEX_AUTH_SOURCE_MISSING,
            )

    def test_source_state_is_bounded_and_carries_no_credential(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex-home"
            self.assertEqual(
                codex_campaign_auth_source_state(home, CODEX_AUTH_MODE_KEYRING),
                CODEX_AUTH_SOURCE_EXTERNAL,
            )
            auth_path = _write_credential(home)
            state = codex_campaign_auth_source_state(home, CODEX_AUTH_MODE_FILE)
            self.assertEqual(state, CODEX_AUTH_SOURCE_PRESENT)
            self.assertNotIn(CREDENTIAL_CANARY, state)
            auth_path.unlink()
            (home / CODEX_CAMPAIGN_AUTH_FILENAME).mkdir()
            self.assertEqual(
                codex_campaign_auth_source_state(home, CODEX_AUTH_MODE_FILE),
                CODEX_AUTH_SOURCE_UNUSABLE,
            )

    def test_restart_reuses_the_existing_credential(self) -> None:
        """A second bootstrap is idempotent and does not re-prompt for a login."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex-home"
            auth_path = _write_credential(home)
            prepare_codex_campaign_home(home, auth_mode=CODEX_AUTH_MODE_FILE)
            prepared = prepare_codex_campaign_home(home, auth_mode=CODEX_AUTH_MODE_FILE)
            self.assertTrue(auth_path.is_file())
            self.assertEqual(
                codex_campaign_auth_source_state(prepared, CODEX_AUTH_MODE_FILE),
                CODEX_AUTH_SOURCE_PRESENT,
            )


class ChildEnvironmentTests(unittest.TestCase):
    """No credential inheritance, and no keyring coordinates without a keyring."""

    AMBIENT = {
        "OPENAI_API_KEY": CREDENTIAL_CANARY,
        "CODEX_API_KEY": CREDENTIAL_CANARY,
        "GITHUB_TOKEN": CREDENTIAL_CANARY,
        "CODE_MOWER_CLOUD_TOKEN": CREDENTIAL_CANARY,
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "HOME": "/home/operator",
        "PATH": "/usr/bin",
    }

    def test_no_supported_mode_inherits_an_ambient_provider_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex-home"
            for mode in SUPPORTED_CODEX_AUTH_MODES:
                with self.subTest(mode=mode):
                    with mock.patch.dict(os.environ, self.AMBIENT, clear=True):
                        child_env = build_adapter_child_env(
                            "codex", codex_home=home, codex_auth_mode=mode
                        )
                    self.assertNotIn(CREDENTIAL_CANARY, json.dumps(child_env))
                    for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "GITHUB_TOKEN"):
                        self.assertNotIn(name, child_env)
                    self.assertEqual(child_env["CODEX_HOME"], str(home.resolve()))
                    self.assertNotEqual(child_env.get("HOME"), child_env["CODEX_HOME"])

    def test_file_mode_drops_the_keyring_session_coordinates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex-home"
            with mock.patch.dict(os.environ, self.AMBIENT, clear=True):
                keyring_env = build_adapter_child_env(
                    "codex", codex_home=home, codex_auth_mode=CODEX_AUTH_MODE_KEYRING
                )
                file_env = build_adapter_child_env(
                    "codex", codex_home=home, codex_auth_mode=CODEX_AUTH_MODE_FILE
                )
        for name in KEYRING_SESSION_ENV_NAMES:
            self.assertIn(name, keyring_env)
            self.assertNotIn(name, file_env)


class AdapterReadinessTests(unittest.TestCase):
    """The adapter enforces the rule doctor reports, and fails closed."""

    def _run(self, *, mode, home, tmp):
        def refuse(*args, **kwargs):  # pragma: no cover - must never be reached
            raise AssertionError("provider CLI must not run without a usable source")

        provider_bin = Path(tmp) / "codex"
        provider_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        provider_bin.chmod(0o700)
        output = Path(tmp) / "result.json"
        env = {CODEX_CAMPAIGN_AUTH_MODE_ENV: mode, CODEX_CAMPAIGN_HOME_ENV: str(home)}
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False):
            with contextlib.redirect_stderr(stderr):
                code = campaign_adapters.run_campaign_adapter(
                    provider="codex",
                    provider_bin=str(provider_bin),
                    release_tag="v1.4.3",
                    package_spec="code-mower==1.4.3",
                    qualification_context="cold_install",
                    starting_version="",
                    output_path=output,
                    provider_runner=refuse,
                )
        return code, stderr.getvalue(), output

    def test_unsupported_mode_fails_closed_before_any_provider_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, _stderr, output = self._run(
                mode="auto", home=Path(tmp) / "codex-home", tmp=tmp
            )
            self.assertNotEqual(code, 0)
            self.assertFalse(output.exists())

    def test_missing_file_mode_credential_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, _stderr, output = self._run(
                mode=CODEX_AUTH_MODE_FILE, home=Path(tmp) / "codex-home", tmp=tmp
            )
            self.assertNotEqual(code, 0)
            self.assertFalse(output.exists())

    def test_adapter_failure_text_never_carries_a_credential(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex-home"
            (home / CODEX_CAMPAIGN_AUTH_FILENAME).mkdir(mode=0o700, parents=True)
            code, stderr, _output = self._run(
                mode=CODEX_AUTH_MODE_FILE, home=home, tmp=tmp
            )
            self.assertNotEqual(code, 0)
            self.assertNotIn(CREDENTIAL_CANARY, stderr)
            self.assertNotIn(str(home), stderr)


class DoctorParityTests(unittest.TestCase):
    """Doctor resolves the same source the adapter will use."""

    def test_registry_declares_the_selection_variable(self) -> None:
        provider_config = REFERENCE_PROVIDERS["codex"].provider_config
        self.assertEqual(
            provider_config[CAMPAIGN_AUTH_MODE_ENV_KEY], CODEX_CAMPAIGN_AUTH_MODE_ENV
        )

    def test_unsupported_mode_is_an_owner_action_and_never_probes(self) -> None:
        def refuse(*args, **kwargs):  # pragma: no cover - must never be reached
            raise AssertionError("no probe may run for an unsupported mode")

        checks = _run_checks(refuse, mode="ephemeral")
        check = _auth_check(checks)
        self.assertEqual(check.status, STATUS_WARN)
        self.assertEqual(check.detail.get("auth_probe"), AUTH_STATE_UNSUPPORTED_MODE)
        self.assertEqual(check.detail.get("error"), AUTH_ERROR_UNSUPPORTED_MODE)
        self.assertFalse(check.detail.get("campaign_auth_mode_supported"))
        self.assertIn(CODEX_CAMPAIGN_AUTH_MODE_ENV, check.remediation)
        # The unsupported value came from the operator's environment and is
        # never echoed back; only the supported vocabulary is.
        self.assertNotIn("ephemeral", json.dumps(check.as_dict()))
        self.assertNotIn("codex", _readiness(checks).detail.get("ready_providers", []))

    def test_missing_file_mode_credential_is_an_owner_action(self) -> None:
        def refuse(*args, **kwargs):  # pragma: no cover - must never be reached
            raise AssertionError("no probe may run without a credential source")

        checks = _run_checks(refuse, mode=CODEX_AUTH_MODE_FILE)
        check = _auth_check(checks)
        self.assertEqual(check.status, STATUS_WARN)
        self.assertEqual(check.detail.get("error"), AUTH_ERROR_SOURCE_MISSING)
        self.assertEqual(check.detail.get("campaign_auth_mode"), CODEX_AUTH_MODE_FILE)
        self.assertEqual(check.detail.get("campaign_auth_source"), CODEX_AUTH_SOURCE_MISSING)
        self.assertTrue(check.detail.get("owner_action"))
        self.assertIn("docs/release-qualification.md", check.remediation)
        self.assertNotIn("codex", _readiness(checks).detail.get("ready_providers", []))

    def test_missing_file_mode_credential_still_prepares_the_selected_home(self) -> None:
        """The documented login runs against the configuration doctor writes.

        An operator migrating an existing keyring-configured home to file mode
        runs doctor before logging in. If doctor returned the missing-source
        warning without preparing that home, the home would still name the
        keyring store, and the login the remediation asks for would try to
        store the new credential in a keyring this headless host does not have.
        """

        def refuse(*args, **kwargs):  # pragma: no cover - must never be reached
            raise AssertionError("no probe may run without a credential source")

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex-home"
            # The home as a previous keyring-mode run left it.
            prepare_codex_campaign_home(home, auth_mode=CODEX_AUTH_MODE_KEYRING)
            config_path = home / "config.toml"
            self.assertIn(
                'cli_auth_credentials_store = "keyring"',
                config_path.read_text(encoding="utf-8"),
            )

            checks = _run_checks(refuse, mode=CODEX_AUTH_MODE_FILE, codex_home=home)

            config = config_path.read_text(encoding="utf-8")
            self.assertIn('cli_auth_credentials_store = "file"', config)
            self.assertNotIn("keyring", config)
            # Preparing the home is not authenticating it: no credential is
            # created, so the source stays missing and the owner still owes a
            # login.
            self.assertFalse((home / CODEX_CAMPAIGN_AUTH_FILENAME).exists())
            self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)
            # The restricted boundary is written, not relaxed.
            self.assertIn('default_permissions = "campaign"', config)
            self.assertIn('":root" = "deny"', config)

        check = _auth_check(checks)
        self.assertEqual(check.status, STATUS_WARN)
        self.assertEqual(check.detail.get("error"), AUTH_ERROR_SOURCE_MISSING)
        self.assertTrue(check.detail.get(CAMPAIGN_AUTH_HOME_PREPARED_KEY))
        self.assertTrue(check.detail.get("owner_action"))
        self.assertNotIn("codex", _readiness(checks).detail.get("ready_providers", []))
        self.assertNotIn(str(home), json.dumps(check.as_dict()))

    def test_unpreparable_home_says_so_instead_of_promising_the_login(self) -> None:
        """A home doctor could not configure must not be reported as ready to log in."""

        def refuse(*args, **kwargs):  # pragma: no cover - must never be reached
            raise AssertionError("no probe may run without a credential source")

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex-home"
            with mock.patch(
                "code_mower.campaign_adapters.prepare_codex_campaign_home",
                side_effect=OSError("read-only file system"),
            ):
                checks = _run_checks(refuse, mode=CODEX_AUTH_MODE_FILE, codex_home=home)
        check = _auth_check(checks)
        self.assertEqual(check.status, STATUS_WARN)
        self.assertEqual(check.detail.get("error"), AUTH_ERROR_SOURCE_MISSING)
        self.assertFalse(check.detail.get(CAMPAIGN_AUTH_HOME_PREPARED_KEY))
        self.assertTrue(check.detail.get("owner_action"))
        self.assertIn("could not be prepared", check.remediation)
        # The filesystem error text is never echoed back into doctor output.
        rendered = json.dumps(check.as_dict())
        self.assertNotIn("read-only file system", rendered)
        self.assertNotIn(str(home), rendered)

    def test_unusable_file_mode_credential_is_an_owner_action(self) -> None:
        def refuse(*args, **kwargs):  # pragma: no cover - must never be reached
            raise AssertionError("no probe may run for an unusable source")

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex-home"
            (home / CODEX_CAMPAIGN_AUTH_FILENAME).mkdir(mode=0o700, parents=True)
            checks = _run_checks(refuse, mode=CODEX_AUTH_MODE_FILE, codex_home=home)
        check = _auth_check(checks)
        self.assertEqual(check.status, STATUS_WARN)
        self.assertEqual(check.detail.get("error"), AUTH_ERROR_SOURCE_UNUSABLE)
        # Preparation refuses a home whose credential is not a private regular
        # file, in file mode exactly as in keyring mode, so the refusal is
        # reported rather than worked around.
        self.assertFalse(check.detail.get(CAMPAIGN_AUTH_HOME_PREPARED_KEY))
        self.assertIn("Remove it", check.remediation)
        self.assertNotIn(str(home), json.dumps(check.as_dict()))

    def test_authenticated_file_mode_home_passes_on_a_headless_host(self) -> None:
        """The whole point: headless Linux, no keyring, still campaign-ready."""
        recorded: list[dict] = []

        def runner(argv, timeout_seconds, child_env):
            recorded.append(dict(child_env))
            return _completed(0, stderr=f"Logged in using an API key - {CREDENTIAL_CANARY}\n")

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex-home"
            _write_credential(home)
            checks = _run_checks(runner, mode=CODEX_AUTH_MODE_FILE, codex_home=home)
        check = _auth_check(checks)
        self.assertEqual(check.status, STATUS_PASS)
        self.assertEqual(check.detail.get("campaign_auth_mode"), CODEX_AUTH_MODE_FILE)
        self.assertEqual(check.detail.get("campaign_auth_source"), CODEX_AUTH_SOURCE_PRESENT)
        self.assertIn("codex", _readiness(checks).detail.get("ready_providers", []))
        # The probe's own output held a credential fragment; doctor keeps only
        # its shape.
        rendered = json.dumps(check.as_dict())
        self.assertNotIn(CREDENTIAL_CANARY, rendered)
        self.assertNotIn(str(home), rendered)
        self.assertTrue(check.detail.get("output_redacted"))
        self.assertNotIn(CREDENTIAL_CANARY, json.dumps(recorded))

    def test_revoked_file_mode_login_is_a_bounded_owner_action(self) -> None:
        """A credential that still exists but no longer authenticates."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex-home"
            _write_credential(home)
            checks = _run_checks(
                lambda argv, timeout, env: _completed(1, stderr="Not logged in\n"),
                mode=CODEX_AUTH_MODE_FILE,
                codex_home=home,
            )
        check = _auth_check(checks)
        self.assertEqual(check.status, STATUS_WARN)
        self.assertEqual(check.detail.get("auth_probe"), "unauthenticated")
        self.assertEqual(check.detail.get("campaign_auth_mode"), CODEX_AUTH_MODE_FILE)
        # File mode does not need a desktop keyring, so it must not be blamed.
        self.assertNotIn("keyring", check.message)
        self.assertNotIn("codex", _readiness(checks).detail.get("ready_providers", []))

    def test_headless_keyring_mode_now_names_the_supported_alternative(self) -> None:
        checks = _run_checks(
            lambda argv, timeout, env: _completed(1, stderr="Not logged in\n"),
            mode=CODEX_AUTH_MODE_KEYRING,
            doctor_env={},
        )
        check = _auth_check(checks)
        self.assertEqual(check.status, STATUS_WARN)
        self.assertTrue(check.detail.get("keyring_required"))
        self.assertFalse(check.detail.get("host_keyring_available"))
        self.assertIn(f"{CODEX_CAMPAIGN_AUTH_MODE_ENV}=file", check.remediation)

    def test_file_mode_clears_the_keyring_requirement(self) -> None:
        lane = REFERENCE_PROVIDERS["codex"]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex-home"
            _write_credential(home)
            with mock.patch.dict(
                os.environ, {CODEX_CAMPAIGN_HOME_ENV: str(home)}, clear=False
            ):
                keyring_source = resolve_campaign_auth_source(lane, "codex", {})
                file_source = resolve_campaign_auth_source(
                    lane, "codex", {CODEX_CAMPAIGN_AUTH_MODE_ENV: CODEX_AUTH_MODE_FILE}
                )
        self.assertTrue(keyring_source.keyring_required)
        self.assertEqual(keyring_source.state, CODEX_AUTH_SOURCE_EXTERNAL)
        self.assertFalse(file_source.keyring_required)
        self.assertEqual(file_source.state, CODEX_AUTH_SOURCE_PRESENT)


class BackwardsCompatibilityTests(unittest.TestCase):
    """An adopter who never opts in sees no change at all."""

    def test_no_campaign_intent_still_means_no_campaign_auth_owner_action(self) -> None:
        """#953/#966: ordinary adoption never becomes a campaign-auth action."""

        def refuse(*args, **kwargs):  # pragma: no cover - must never be reached
            raise AssertionError("no probe may run without campaign intent")

        with tempfile.TemporaryDirectory() as tmp:
            checks = check_adoption_campaign_readiness(
                config={"lanes": {"codex": {"enabled": True}}},
                repo_root=Path(tmp),
                adoption_posture="reviewer-gate",
                env={CODEX_CAMPAIGN_AUTH_MODE_ENV: CODEX_AUTH_MODE_FILE},
                which_fn=_which,
                auth_probe_runner=refuse,
                providers=["codex"],
                campaign_requested=False,
            )
        check = _auth_check(checks)
        self.assertEqual(check.detail.get("auth_probe"), "not_requested")
        self.assertFalse(check.detail.get("actionable"))

    def test_default_keyring_run_is_unaffected_by_the_new_selection(self) -> None:
        checks = _run_checks(lambda argv, timeout, env: _completed(0), platform="darwin")
        check = _auth_check(checks)
        self.assertEqual(check.status, STATUS_PASS)
        self.assertEqual(check.detail.get("campaign_auth_mode"), CODEX_AUTH_MODE_KEYRING)
        self.assertIn("codex", _readiness(checks).detail.get("ready_providers", []))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
