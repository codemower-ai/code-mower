"""Optional Devin setup, doctor readiness, guidance, and privacy contracts."""

import copy
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import asdict
from io import StringIO
from pathlib import Path
from unittest import mock

from code_mower import doctor as code_mower_doctor
from code_mower import init as code_mower_init
from code_mower.config import ConfigError, load_config, validate_config
from code_mower.devin_readiness import (
    CLI_COMMAND_ENV,
    DEFAULT_CLI_COMMAND,
    DEVIN_API_KEY_ENV,
    DEVIN_ORG_ID_ENV,
    DEVIN_REPOSITORIES_ENV,
    GENERATED_OUTPUT_DIR,
    HOSTED_TRANSPORT,
    LOCAL_TRANSPORT,
    OBSERVER_POSTURES,
    POSTURE_HOSTED_API,
    POSTURE_LOCAL_CLI,
    POSTURE_UNAVAILABLE,
    STATUS_WARN,
    devin_readiness,
    readiness_command,
    selected_devin_transport,
    setup_instructions,
)
from code_mower.doctor_checks import (
    build_doctor_run_plan,
    check_devin_readiness,
    doctor_check_group_id,
)
from code_mower.doctor_checks.common import OBSERVER_ADOPTION_POSTURES
from code_mower.doctor_checks.devin import (
    devin_effective_lane,
    devin_effective_lanes,
)
from code_mower.doctor_checks.provider_local_cli_commands import (
    resolved_local_cli_command,
)
from code_mower.doctor_checks.providers import check_lane_runtime
from code_mower.local_cli_commands import candidate_local_cli_commands
from code_mower.next_steps import build_next_steps
from code_mower.package import load_provider_templates
from code_mower.participants import (
    DEFAULT_PARTICIPANTS,
    TRANSPORT_PARTICIPANT_ALIASES,
    config_with_participants,
    config_with_transport,
    parse_transport_selection,
)
from code_mower.provider_capabilities import TRANSPORTS
from code_mower.session import build_session

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = ROOT / "src/code_mower/templates/code-mower.example.yml"
PROVIDER_TEMPLATES = ROOT / "src/code_mower/templates/providers.yml"

FAKE_KEY = "devin-fake-key"
FAKE_ORG_ID = "org-fake-identifier"


def _config(*participants: str) -> dict:
    return {"session_defaults": {"participants": list(participants)}}


def _hosted_env(*, repositories: str = "") -> dict[str, str]:
    env = {DEVIN_API_KEY_ENV: FAKE_KEY, DEVIN_ORG_ID_ENV: FAKE_ORG_ID}
    if repositories:
        env[DEVIN_REPOSITORIES_ENV] = repositories
    return env


_ISOLATED_STORE = tempfile.TemporaryDirectory(prefix="code-mower-devin-readiness-")


def tearDownModule() -> None:
    _ISOLATED_STORE.cleanup()


def _readiness(config, **kwargs):
    """Resolve readiness against an empty credential store.

    Stored provider profiles on the host machine are legitimate credential
    sources, so fixtures must pin an isolated configuration directory or their
    outcome would depend on whoever runs the suite.
    """
    kwargs.setdefault("config_dir", Path(_ISOLATED_STORE.name))
    return devin_readiness(config, **kwargs)


def _finding(findings, name: str):
    return next(finding for finding in findings if finding.name == name)


class DevinSelectionTests(unittest.TestCase):
    def test_default_participants_select_no_devin_transport(self) -> None:
        self.assertEqual(DEFAULT_PARTICIPANTS, ("claude", "codex"))
        self.assertIsNone(selected_devin_transport({"session_defaults": {}}))
        self.assertIsNone(selected_devin_transport(_config("claude", "codex")))

    def test_scripted_and_interactive_selection_resolve_each_transport(self) -> None:
        base = load_config(EXAMPLE_CONFIG)
        scripted = config_with_participants(base, ("claude", "codex", "devin-cli"))
        self.assertEqual(selected_devin_transport(scripted), LOCAL_TRANSPORT)
        hosted = config_with_participants(base, ("claude", "codex", "devin-api-v3"))
        self.assertEqual(selected_devin_transport(hosted), HOSTED_TRANSPORT)
        default = config_with_participants(base, DEFAULT_PARTICIPANTS)
        self.assertIsNone(selected_devin_transport(default))

    def test_active_devin_review_lane_selects_devin(self) -> None:
        self.assertEqual(
            selected_devin_transport(_config("claude", "codex"), lanes=("devin_cli",)),
            LOCAL_TRANSPORT,
        )

    def test_active_hosted_lane_selects_hosted_without_profile_inference(self) -> None:
        config = _config("claude", "codex")
        self.assertEqual(
            selected_devin_transport(config, lanes=("devin",), profile=None),
            HOSTED_TRANSPORT,
        )
        explicit = _config("claude", "codex", "devin-cli")
        self.assertEqual(
            selected_devin_transport(explicit, lanes=("devin",), profile=None),
            LOCAL_TRANSPORT,
        )

    def test_malformed_participants_raise_instead_of_reporting_no_devin(self) -> None:
        with self.assertRaises(ConfigError):
            selected_devin_transport({"session_defaults": {"participants": "devin"}})

    def test_unselected_devin_produces_no_findings_by_default(self) -> None:
        self.assertEqual(_readiness(_config("claude", "codex"), env={}), ())

    def test_requested_unselected_guidance_names_every_posture(self) -> None:
        findings = _readiness(_config("claude", "codex"), env={}, include_unselected=True)
        selection = _finding(findings, "provider.devin.selection")
        self.assertEqual(selection.status, "skip")
        self.assertEqual(selection.detail["posture"], POSTURE_UNAVAILABLE)
        self.assertEqual(selection.detail["default_participants"], ["claude", "codex"])
        postures = _finding(findings, "provider.devin.postures")
        self.assertEqual(
            postures.detail["postures"],
            [POSTURE_LOCAL_CLI, POSTURE_HOSTED_API, POSTURE_UNAVAILABLE],
        )
        self.assertEqual(len(postures.detail["next_actions"]), 3)


class DevinReadinessFindingTests(unittest.TestCase):
    def test_local_posture_reports_cli_authentication_and_next_action(self) -> None:
        with mock.patch("code_mower.devin_readiness.shutil.which", return_value=None):
            findings = _readiness(_config("claude", "codex", "devin-cli"), env={})
        names = [finding.name for finding in findings]
        self.assertEqual(
            names,
            [
                "provider.devin.selection",
                "provider.devin.capabilities",
                "provider.devin.local_cli",
                "provider.devin.permissions",
                "provider.devin.lifecycle",
            ],
        )
        selection = _finding(findings, "provider.devin.selection")
        self.assertEqual(selection.detail["posture"], POSTURE_LOCAL_CLI)
        cli = _finding(findings, "provider.devin.local_cli")
        self.assertEqual(cli.status, "warn")
        self.assertIn("devin auth login", cli.remediation)
        self.assertEqual(cli.detail["authentication"], "ambient_cli_login")

    def test_available_local_cli_passes_without_hosted_credentials(self) -> None:
        with mock.patch(
            "code_mower.devin_readiness.shutil.which", return_value="/opt/private/bin/devin"
        ):
            findings = _readiness(_config("devin-cli"), env={})
        cli = _finding(findings, "provider.devin.local_cli")
        self.assertEqual(cli.status, "pass")
        self.assertEqual(cli.detail["command"], "devin")
        self.assertNotIn("/opt/private", json.dumps(dict(cli.detail)) + cli.remediation)

    def test_configured_command_override_is_discovered_but_reported_as_a_basename(self) -> None:
        override = "/opt/private/tools/devin-cli-bin"
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "code_mower.devin_readiness.shutil.which", side_effect=lambda name: name == override
        ) as which:
            findings = _readiness(_config("devin-cli"), env={CLI_COMMAND_ENV: override})
        which.assert_called_once_with(override)
        cli = _finding(findings, "provider.devin.local_cli")
        self.assertEqual(cli.status, "pass")
        self.assertEqual(cli.detail["command"], "devin-cli-bin")
        self.assertNotIn("/opt/private", json.dumps(dict(cli.detail)) + cli.message + cli.remediation)

    def test_lane_configured_command_is_discovered_as_runtime_would(self) -> None:
        lane = {
            "provider": "devin_cli",
            "driver": "local_cli",
            "provider_config": {"command": "/opt/private/tools/devin-review"},
        }
        with mock.patch(
            "code_mower.devin_readiness.shutil.which",
            side_effect=lambda name: name == "/opt/private/tools/devin-review",
        ):
            findings = _readiness(_config("devin-cli"), env={}, lane_config=lane)
        cli = _finding(findings, "provider.devin.local_cli")
        self.assertEqual(cli.status, "pass")
        self.assertEqual(cli.detail["command"], "devin-review")
        rendered = json.dumps(dict(cli.detail)) + cli.message + cli.remediation
        self.assertNotIn("/opt/private", rendered)

    def test_lane_custom_command_env_is_discovered(self) -> None:
        lane = {
            "provider": "devin_cli",
            "driver": "local_cli",
            "provider_config": {"command_env": "DEVIN_LANE_COMMAND", "command": "devin"},
        }
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "code_mower.devin_readiness.shutil.which",
            side_effect=lambda name: name == "/opt/lane/bin/devin-lane",
        ):
            findings = _readiness(
                _config("devin-cli"),
                env={"DEVIN_LANE_COMMAND": "/opt/lane/bin/devin-lane"},
                lane_config=lane,
            )
        cli = _finding(findings, "provider.devin.local_cli")
        self.assertEqual(cli.status, "pass")
        self.assertEqual(cli.detail["command"], "devin-lane")
        self.assertNotIn("/opt/lane", json.dumps(dict(cli.detail)) + cli.message)

    def test_lane_command_discovery_ignores_the_ambient_environment(self) -> None:
        lane = {
            "provider": "devin_cli",
            "driver": "local_cli",
            "provider_config": {"command_env": "DEVIN_LANE_COMMAND", "command": "devin"},
        }
        with mock.patch.dict(
            os.environ, {"DEVIN_LANE_COMMAND": "/host/only/devin-host"}, clear=False
        ), mock.patch(
            "code_mower.devin_readiness.shutil.which", return_value=None
        ) as which:
            findings = _readiness(_config("devin-cli"), env={}, lane_config=lane)
        inspected = [call.args[0] for call in which.call_args_list]
        self.assertNotIn("/host/only/devin-host", inspected)
        cli = _finding(findings, "provider.devin.local_cli")
        self.assertEqual(cli.detail["command"], "devin")

    def test_lane_alternate_command_is_discovered(self) -> None:
        lane = {
            "provider": "devin_cli",
            "driver": "local_cli",
            "provider_config": {
                "command": "devin-primary",
                "alternate_commands": ["devin-alternate"],
            },
        }
        with mock.patch(
            "code_mower.devin_readiness.shutil.which",
            side_effect=lambda name: name == "devin-alternate",
        ):
            findings = _readiness(_config("devin-cli"), env={}, lane_config=lane)
        cli = _finding(findings, "provider.devin.local_cli")
        self.assertEqual(cli.status, "pass")
        self.assertEqual(cli.detail["command"], "devin-alternate")

    def test_observer_postures_skip_the_local_executable_requirement(self) -> None:
        self.assertEqual(OBSERVER_POSTURES, OBSERVER_ADOPTION_POSTURES)
        for posture in sorted(OBSERVER_ADOPTION_POSTURES):
            with self.subTest(posture=posture):
                with mock.patch(
                    "code_mower.devin_readiness.shutil.which", return_value=None
                ):
                    findings = _readiness(
                        _config("devin-cli"), env={}, adoption_posture=posture
                    )
                cli = _finding(findings, "provider.devin.local_cli")
                self.assertEqual(cli.status, "skip")
                self.assertEqual(cli.detail["adoption_posture"], posture)
                self.assertIn(posture, cli.message)
        with mock.patch("code_mower.devin_readiness.shutil.which", return_value=None):
            executing = _readiness(
                _config("devin-cli"), env={}, adoption_posture="reviewer-gate"
            )
        self.assertEqual(_finding(executing, "provider.devin.local_cli").status, "warn")

    def test_caller_profile_selects_the_transport_without_recommended_fallback(
        self,
    ) -> None:
        config = {
            "session_defaults": {"participants": ["claude", "codex", "devin"]},
            "profiles": {
                "recommended": {"lanes": ["claude_code", "codex_cli"]},
                "hosted-devin": {"lanes": ["claude_code", "codex_cli", "devin"]},
            },
        }
        hosted = _readiness(
            config,
            env={},
            repo_slug="codemower-ai/code-mower",
            config_profile="hosted-devin",
        )
        self.assertEqual(
            _finding(hosted, "provider.devin.selection").detail["transport"],
            HOSTED_TRANSPORT,
        )
        with mock.patch("code_mower.devin_readiness.shutil.which", return_value=None):
            default = _readiness(config, env={})
        self.assertEqual(
            _finding(default, "provider.devin.selection").detail["transport"],
            LOCAL_TRANSPORT,
        )

    def test_hosted_posture_requires_credentials_and_exact_repository_scope(self) -> None:
        findings = _readiness(
            _config("devin-api-v3"), repo_slug="codemower-ai/code-mower", env={}
        )
        credentials = _finding(findings, "provider.devin.hosted_credentials")
        self.assertEqual(credentials.status, "warn")
        self.assertEqual(
            credentials.detail["required_variables"], [DEVIN_API_KEY_ENV, DEVIN_ORG_ID_ENV]
        )
        scope = _finding(findings, "provider.devin.repository_scope")
        self.assertEqual(scope.status, "warn")
        self.assertTrue(scope.detail["exact_slug_required"])
        self.assertIn(DEVIN_REPOSITORIES_ENV, scope.remediation)

    def test_stored_host_profile_cannot_satisfy_the_missing_credential_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            profile = store / "devin.env"
            profile.write_text(f"{DEVIN_API_KEY_ENV}={FAKE_KEY}\n{DEVIN_ORG_ID_ENV}={FAKE_ORG_ID}\n")
            profile.chmod(0o600)
            discovered = devin_readiness(
                _config("devin-api-v3"), env={}, config_dir=store
            )
            self.assertEqual(
                _finding(discovered, "provider.devin.hosted_credentials").status, "pass"
            )
            isolated = _readiness(_config("devin-api-v3"), env={})
            self.assertEqual(
                _finding(isolated, "provider.devin.hosted_credentials").status, "warn"
            )

    def test_hosted_posture_passes_with_credentials_and_acknowledged_repository(self) -> None:
        findings = _readiness(
            _config("devin-api-v3"),
            repo_slug="codemower-ai/code-mower",
            env=_hosted_env(repositories="other/repo,CodeMower-AI/Code-Mower"),
        )
        self.assertEqual(_finding(findings, "provider.devin.hosted_credentials").status, "pass")
        scope = _finding(findings, "provider.devin.repository_scope")
        self.assertEqual(scope.status, "pass")
        self.assertTrue(scope.detail["acknowledged"])

    def test_same_name_fork_is_not_acknowledged(self) -> None:
        findings = _readiness(
            _config("devin-api-v3"),
            repo_slug="fork-owner/code-mower",
            env=_hosted_env(repositories="codemower-ai/code-mower"),
        )
        self.assertEqual(_finding(findings, "provider.devin.repository_scope").status, "warn")

    def test_hosted_scope_is_skipped_without_a_repository_target(self) -> None:
        findings = _readiness(_config("devin-api-v3"), env=_hosted_env())
        scope = _finding(findings, "provider.devin.repository_scope")
        self.assertEqual(scope.status, "skip")
        self.assertNotIn("repository", scope.detail)
        self.assertIn("--repo OWNER/REPO", scope.remediation)

    def test_permissions_report_create_view_and_manage_for_each_posture(self) -> None:
        for participant, transport in (
            ("devin-cli", LOCAL_TRANSPORT),
            ("devin-api-v3", HOSTED_TRANSPORT),
        ):
            findings = _readiness(_config(participant), env={})
            permissions = _finding(findings, "provider.devin.permissions")
            self.assertEqual(permissions.status, "skip")
            self.assertEqual(permissions.detail["transport"], transport)
            self.assertTrue(permissions.detail["owner_action"])
            requirements = " ".join(permissions.detail["requirements"])
            for verb in ("create:", "view:", "manage:"):
                self.assertIn(verb, requirements)

    def test_capability_gaps_and_lifecycle_recovery_are_reported(self) -> None:
        findings = _readiness(_config("devin-api-v3"), env={})
        capabilities = _finding(findings, "provider.devin.capabilities")
        self.assertEqual(capabilities.detail["capabilities"]["coordinate"], "unavailable")
        self.assertIn("coordinate", capabilities.detail["capability_gaps"])
        self.assertIn("report them", capabilities.remediation)
        lifecycle = _finding(findings, "provider.devin.lifecycle")
        self.assertIn("--apply", lifecycle.message)
        self.assertIn("never redispatch", lifecycle.remediation)

    def test_local_lifecycle_states_missing_remote_controls(self) -> None:
        findings = _readiness(_config("devin-cli"), env={})
        lifecycle = _finding(findings, "provider.devin.lifecycle")
        self.assertIn("message and cancel are unavailable", lifecycle.message)

    def test_unknown_transport_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            _readiness(_config("devin"), transport="devin_desktop", env={})

    def test_observer_postures_inspect_no_executable_at_all(self) -> None:
        for posture in sorted(OBSERVER_POSTURES):
            with self.subTest(posture=posture):
                with mock.patch(
                    "code_mower.devin_readiness.shutil.which"
                ) as which:
                    findings = _readiness(
                        _config("devin-cli"), env={}, adoption_posture=posture
                    )
                which.assert_not_called()
                finding = _finding(findings, "provider.devin.local_cli")
                self.assertEqual(finding.status, "skip")
                self.assertIn("was not inspected", finding.message)

    def test_configured_lane_remediation_names_only_its_own_candidates(self) -> None:
        lane = {
            "provider": "devin_cli",
            "driver": "local_cli",
            "provider_config": {
                "command": "/opt/private/devin-lane",
                "command_env": "TEAM_DEVIN_CLI",
                "alternate_commands": ["devin-lane-fallback"],
            },
        }
        with mock.patch("code_mower.devin_readiness.shutil.which", return_value=None):
            findings = _readiness(
                _config("devin-cli"),
                env={CLI_COMMAND_ENV: "devin-override"},
                lane_config=lane,
            )
        finding = _finding(findings, "provider.devin.local_cli")
        self.assertEqual(finding.status, STATUS_WARN)
        self.assertEqual(finding.detail["command_env"], "TEAM_DEVIN_CLI")
        self.assertEqual(
            finding.detail["commands"], ["devin-lane", "devin-lane-fallback"]
        )
        self.assertIn("devin-lane", finding.remediation)
        self.assertIn("devin-lane-fallback", finding.remediation)
        self.assertIn("TEAM_DEVIN_CLI", finding.remediation)
        # The lane's runtime never reads the historical override or default, so
        # remediation must not send the operator to either of them.
        self.assertNotIn(CLI_COMMAND_ENV, finding.remediation)
        self.assertNotIn(f"`{DEFAULT_CLI_COMMAND}`", finding.remediation)
        rendered = finding.message + finding.remediation + str(finding.detail)
        self.assertNotIn("/opt/private", rendered)

    def test_lane_without_configuration_keeps_the_historical_guidance(self) -> None:
        with mock.patch("code_mower.devin_readiness.shutil.which", return_value=None):
            findings = _readiness(_config("devin-cli"), env={})
        finding = _finding(findings, "provider.devin.local_cli")
        self.assertEqual(finding.detail["command_env"], CLI_COMMAND_ENV)
        self.assertIn(CLI_COMMAND_ENV, finding.remediation)
        self.assertIn(f"`{DEFAULT_CLI_COMMAND}`", finding.remediation)

    def test_ambiguous_credential_profiles_name_no_variable_or_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            for name in ("devin.env", "devin-team.env"):
                profile = store / name
                profile.write_text(
                    f"{DEVIN_API_KEY_ENV}={FAKE_KEY}\n{DEVIN_ORG_ID_ENV}={FAKE_ORG_ID}\n"
                )
                profile.chmod(0o600)
            findings = devin_readiness(
                _config("devin-api-v3"),
                env={},
                config_dir=store,
                config_path="ops/code-mower.yml",
                config_profile="hosted-devin",
            )
        finding = _finding(findings, "provider.devin.hosted_credentials")
        self.assertEqual(finding.status, "fail")
        self.assertIn("ambiguous", finding.message)
        self.assertNotIn("first unresolved variable", finding.message)
        self.assertIn("--provider-profile NAME", finding.remediation)
        self.assertIn("--profile hosted-devin", finding.remediation)
        self.assertIn("ops/code-mower.yml", finding.remediation)
        rendered = finding.message + finding.remediation + str(finding.detail)
        for secret in ("devin-team", "devin.env", tmp, FAKE_KEY, FAKE_ORG_ID):
            self.assertNotIn(secret, rendered)


class DevinReadinessPrivacyTests(unittest.TestCase):
    def _rendered(self, findings) -> str:
        return json.dumps(
            [
                {
                    "name": finding.name,
                    "status": finding.status,
                    "message": finding.message,
                    "detail": dict(finding.detail),
                    "remediation": finding.remediation,
                }
                for finding in findings
            ]
        )

    def test_credentials_identities_and_inventory_never_appear(self) -> None:
        rendered = self._rendered(
            _readiness(
                _config("devin-api-v3"),
                repo_slug="codemower-ai/code-mower",
                env=_hosted_env(repositories="codemower-ai/code-mower,private-org/secret-repo"),
            )
        )
        for secret in (FAKE_KEY, FAKE_ORG_ID, "private-org/secret-repo"):
            self.assertNotIn(secret, rendered)

    def test_credential_profile_paths_and_filenames_never_appear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "code-mower"
            store.mkdir()
            profile = store / "devin.env"
            profile.write_text(f"{DEVIN_API_KEY_ENV}={FAKE_KEY}\n{DEVIN_ORG_ID_ENV}={FAKE_ORG_ID}\n")
            profile.chmod(0o600)
            resolved = devin_readiness(_config("devin-api-v3"), env={}, config_dir=store)
            profile.write_text(f"{DEVIN_API_KEY_ENV}={FAKE_KEY}\n{DEVIN_ORG_ID_ENV}=not-an-org\n")
            malformed = devin_readiness(_config("devin-api-v3"), env={}, config_dir=store)
            unresolved = devin_readiness(
                _config("devin-api-v3"), env={}, config_dir=Path(tmp) / "empty"
            )
        self.assertEqual(
            _finding(resolved, "provider.devin.hosted_credentials").status, "pass"
        )
        self.assertNotEqual(
            _finding(malformed, "provider.devin.hosted_credentials").status, "pass"
        )
        for findings in (resolved, malformed, unresolved):
            credentials = _finding(findings, "provider.devin.hosted_credentials")
            self.assertNotIn("profile_file", credentials.detail)
            self.assertNotIn("candidate_files", credentials.detail)
            rendered = self._rendered(findings)
            for leak in ("devin.env", "code-mower/devin", str(store), tmp, "~/"):
                self.assertNotIn(leak, rendered)

    def test_doctor_checks_carry_no_credentials_and_group_under_providers(self) -> None:
        checks = check_devin_readiness(
            config=_config("devin-api-v3"),
            repo_slug="codemower-ai/code-mower",
            provider_config_dir=Path(_ISOLATED_STORE.name),
        )
        self.assertTrue(checks)
        for check in checks:
            self.assertIn(check.status, {"pass", "warn", "fail", "skip"})
            self.assertTrue(check.name.startswith("provider.devin."))
            self.assertEqual(doctor_check_group_id(check.name, check.lane), "providers")
            self.assertNotIn(FAKE_KEY, json.dumps(check.as_dict()))


class DevinDoctorStageTests(unittest.TestCase):
    def _report(self, argv: list[str]) -> dict:
        argv = [*argv, "--provider-config-dir", _ISOLATED_STORE.name]
        buffer = StringIO()
        with redirect_stdout(buffer):
            code = code_mower_doctor.main(argv)
        return {"code": code, "report": json.loads(buffer.getvalue())}

    def test_devin_stage_is_optional_and_explicitly_enabled(self) -> None:
        self.assertNotIn(
            "devin-readiness", {stage.id for stage in build_doctor_run_plan()}
        )
        plan = build_doctor_run_plan(devin=True)
        stage = next(item for item in plan if item.id == "devin-readiness")
        self.assertTrue(stage.optional)
        self.assertEqual(stage.group_id, "providers")

    def test_fresh_no_devin_doctor_run_is_unchanged(self) -> None:
        result = self._report(
            [
                str(EXAMPLE_CONFIG),
                "--provider-templates",
                str(PROVIDER_TEMPLATES),
                "--json",
            ]
        )
        names = {check["name"] for check in result["report"]["checks"]}
        self.assertFalse({name for name in names if name.startswith("provider.devin.")})
        plan = next(
            check for check in result["report"]["checks"] if check["name"] == "doctor.plan"
        )
        self.assertNotIn(
            "devin-readiness", {stage["id"] for stage in plan["detail"]["stages"]}
        )

    def test_devin_flag_reports_postures_without_selecting_devin(self) -> None:
        result = self._report(
            [
                str(EXAMPLE_CONFIG),
                "--provider-templates",
                str(PROVIDER_TEMPLATES),
                "--devin",
                "--json",
            ]
        )
        checks = {check["name"]: check for check in result["report"]["checks"]}
        self.assertEqual(checks["provider.devin.selection"]["status"], "skip")
        self.assertIn("provider.devin.postures", checks)

    def _both_devin_config(self, directory: str) -> Path:
        labels = (
            "    labels:\n"
            "      needs: needs-devin-audit\n"
            "      done: devin-audit-done\n"
            "      blocked: devin-audit-blocked\n"
        )
        lanes = (
            "lanes:\n"
            "  devin:\n"
            "    type: audit\n"
            "    driver: hosted_bridge\n"
            "    provider: devin\n"
            "    informational: true\n"
            f"{labels}"
            "  devin_cli:\n"
            "    type: audit\n"
            "    driver: local_cli\n"
            "    provider: devin_cli\n"
            "    informational: true\n"
            f"{labels}"
            "    provider_config:\n"
            "      command: devin\n"
        )
        text = EXAMPLE_CONFIG.read_text(encoding="utf-8")
        text = text.replace("\nlanes:\n", "\n" + lanes, 1)
        text = text.replace(
            "\nprofiles:\n",
            "\nprofiles:\n"
            "  both-devin:\n"
            "    description: Both Devin review transports are active.\n"
            "    lanes:\n"
            "      - codex\n"
            "      - devin\n"
            "      - devin_cli\n",
            1,
        )
        path = Path(directory) / "code-mower.yml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_ordinary_doctor_reports_an_ambiguous_devin_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = self._both_devin_config(directory)
            for argv in (
                [str(config_path), "--profile", "both-devin", "--json"],
                [str(config_path), "--profile", "both-devin", "--devin", "--json"],
            ):
                with self.subTest(argv=argv):
                    result = self._report(
                        [*argv, "--provider-templates", str(PROVIDER_TEMPLATES)]
                    )
                    plan = next(
                        check
                        for check in result["report"]["checks"]
                        if check["name"] == "doctor.plan"
                    )
                    self.assertIn(
                        "devin-readiness",
                        {stage["id"] for stage in plan["detail"]["stages"]},
                    )
                    selection = next(
                        check
                        for check in result["report"]["checks"]
                        if check["name"] == "provider.devin.selection"
                    )
                    self.assertEqual(selection["status"], "fail")
                    self.assertIn("--profile", selection["remediation"])
                    self.assertIn(
                        "session_defaults.transports.devin", selection["remediation"]
                    )

    def _devin_lane(self) -> dict:
        return {
            "provider": "devin_cli",
            "driver": "local_cli",
            "product": "devin",
            "transport": "devin_cli",
            "informational": True,
            "provider_config": {"command": "devin"},
        }

    def _readiness_and_runtime(self, posture: str) -> tuple[str, str]:
        lane = self._devin_lane()
        with mock.patch("code_mower.devin_readiness.shutil.which", return_value=None), \
                mock.patch(
                    "code_mower.doctor_checks.provider_local_cli.shutil.which",
                    return_value=None,
                ):
            readiness = check_devin_readiness(
                config=_config("devin-cli"),
                effective_lanes=(("devin_cli", lane),),
                adoption_posture=posture,
                provider_config_dir=Path(_ISOLATED_STORE.name),
            )
            runtime = check_lane_runtime(
                "devin_cli",
                lane,
                probe_runtime=False,
                http_timeout=1,
                adoption_posture=posture,
            )
        readiness_status = next(
            check.status
            for check in readiness
            if check.name == "provider.devin.local_cli"
        )
        runtime_status = next(
            check.status for check in runtime if check.name == "runtime.local_cli"
        )
        return readiness_status, runtime_status

    def test_observer_postures_skip_local_cli_readiness_like_lane_runtime(self) -> None:
        for posture in sorted(OBSERVER_ADOPTION_POSTURES):
            with self.subTest(posture=posture):
                readiness, runtime = self._readiness_and_runtime(posture)
                self.assertEqual(runtime, "skip")
                self.assertEqual(readiness, "skip")
        readiness, runtime = self._readiness_and_runtime("reviewer-gate")
        self.assertNotEqual(runtime, "skip")
        self.assertEqual(readiness, "warn")

    def test_devin_lane_configuration_reaches_readiness(self) -> None:
        lanes = [
            ("claude_code", {"provider": "claude_code", "driver": "local_cli"}),
            (
                "devin_cli",
                {
                    "provider": "devin_cli",
                    "driver": "local_cli",
                    "provider_config": {"command": "/opt/private/devin-lane"},
                },
            ),
        ]
        lane_id, selected = devin_effective_lane(lanes)
        self.assertEqual(lane_id, "devin_cli")
        self.assertEqual(
            candidate_local_cli_commands(selected, env={}), ["/opt/private/devin-lane"]
        )
        self.assertIsNone(devin_effective_lane(lanes[:1]))

    def test_effective_lane_follows_the_selected_transport_not_lane_order(self) -> None:
        hosted = {"provider": "devin", "driver": "hosted_bridge"}
        local = {
            "provider": "devin_cli",
            "driver": "local_cli",
            "provider_config": {"command": "/opt/private/devin-lane"},
        }
        lanes = [("devin", hosted), ("devin_cli", local)]
        self.assertEqual(
            devin_effective_lane(lanes, LOCAL_TRANSPORT), ("devin_cli", local)
        )
        self.assertEqual(
            devin_effective_lane(lanes, HOSTED_TRANSPORT), ("devin", hosted)
        )

    def test_an_unresolved_mixed_transport_selection_fails_closed(self) -> None:
        hosted = {"provider": "devin", "driver": "hosted_bridge"}
        local = {
            "provider": "devin_cli",
            "driver": "local_cli",
            "product": "devin",
            "transport": "devin_cli",
        }
        # Lanes spanning both transports leave the posture itself unknown, so the
        # selection fails closed; lanes sharing one transport are a valid
        # configuration and only the single-lane helper rejects them.
        with self.assertRaises(ConfigError):
            devin_effective_lane([("devin", hosted), ("devin_cli", local)], "claude_cli")
        with self.assertRaises(ConfigError):
            devin_effective_lanes(
                [("devin", hosted), ("devin_cli", local)], "claude_cli"
            )
        with self.assertRaises(ConfigError):
            devin_effective_lane(
                [("team_devin", local), ("night_devin", local)], LOCAL_TRANSPORT
            )
        self.assertEqual(
            devin_effective_lanes(
                [("team_devin", local), ("night_devin", local)], LOCAL_TRANSPORT
            ),
            (("team_devin", local), ("night_devin", local)),
        )
        checks = check_devin_readiness(
            config={"lanes": {"devin": hosted, "devin_cli": local}},
            lanes=("devin", "devin_cli"),
            effective_lanes=(("devin", hosted), ("devin_cli", local)),
            provider_config_dir=Path(_ISOLATED_STORE.name),
        )
        self.assertEqual([check.status for check in checks], ["fail"])


    def _custom_lane_config(self, directory: str, *, hosted: bool) -> Path:
        labels = (
            "    labels:\n"
            "      needs: needs-team-devin-audit\n"
            "      done: team-devin-audit-done\n"
            "      blocked: team-devin-audit-blocked\n"
        )
        if hosted:
            lane = (
                "lanes:\n"
                "  team_devin:\n"
                "    type: audit\n"
                "    driver: hosted_bridge\n"
                "    provider: devin\n"
                "    product: devin\n"
                "    transport: devin_api_v3\n"
                "    informational: true\n"
                f"{labels}"
            )
        else:
            lane = (
                "lanes:\n"
                "  team_devin:\n"
                "    type: audit\n"
                "    driver: local_cli\n"
                "    provider: devin_cli\n"
                "    product: devin\n"
                "    transport: devin_cli\n"
                "    informational: true\n"
                f"{labels}"
                "    provider_config:\n"
                "      command: team-devin\n"
                "      command_env: TEAM_DEVIN_CLI\n"
            )
        text = EXAMPLE_CONFIG.read_text(encoding="utf-8")
        text = text.replace("\nlanes:\n", "\n" + lane, 1)
        text = text.replace(
            "\nprofiles:\n",
            "\nprofiles:\n"
            "  team-devin:\n"
            "    description: A custom-named Devin lane.\n"
            "    lanes:\n"
            "      - codex\n"
            "      - team_devin\n",
            1,
        )
        path = Path(directory) / "code-mower.yml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_custom_named_devin_lanes_reach_readiness_and_next_steps(self) -> None:
        for hosted, transport, posture in (
            (False, LOCAL_TRANSPORT, POSTURE_LOCAL_CLI),
            (True, HOSTED_TRANSPORT, POSTURE_HOSTED_API),
        ):
            with self.subTest(hosted=hosted), tempfile.TemporaryDirectory() as directory:
                config_path = self._custom_lane_config(directory, hosted=hosted)
                result = self._report(
                    [
                        str(config_path),
                        "--profile",
                        "team-devin",
                        "--provider-templates",
                        str(PROVIDER_TEMPLATES),
                        "--json",
                    ]
                )
                checks = {
                    check["name"]: check for check in result["report"]["checks"]
                }
                selection = checks["provider.devin.selection"]
                self.assertEqual(selection["detail"]["transport"], transport)
                self.assertEqual(selection["detail"]["posture"], posture)
                # Every finding names the lane the repository actually has, so
                # doctor and Board metadata are not attributed to a lane that
                # does not exist.
                devin_checks = [
                    check
                    for check in result["report"]["checks"]
                    if check["name"].startswith("provider.devin.")
                ]
                self.assertEqual(
                    {check["lane"] for check in devin_checks}, {"team_devin"}
                )
                if hosted:
                    self.assertIn("provider.devin.hosted_credentials", checks)
                else:
                    local = checks["provider.devin.local_cli"]
                    # The custom lane's own command configuration decides
                    # readiness, exactly as its runtime does.
                    self.assertEqual(local["detail"]["commands"], ["team-devin"])
                    self.assertEqual(
                        local["detail"]["command_env"], "TEAM_DEVIN_CLI"
                    )
                config = load_config(config_path)
                payload = build_next_steps(
                    {
                        "profiles": config.get("profiles"),
                        "provider_templates": config.get("lanes"),
                    },
                    profile="team-devin",
                    repo="codemower-ai/code-mower",
                    pr="123",
                )
                step = next(
                    item
                    for item in payload["steps"]
                    if item["id"] == "devin-readiness"
                )
                self.assertEqual(step["lanes"], ["team_devin"])
                self.assertIn("--profile team-devin", step["command"])

    def test_a_configured_lane_command_is_the_only_readiness_candidate(self) -> None:
        lane = {
            "provider": "devin_cli",
            "driver": "local_cli",
            "provider_config": {"command": "/opt/private/devin-lane"},
        }
        installed = {"devin", DEFAULT_CLI_COMMAND}

        def which(command: str) -> str | None:
            return f"/usr/local/bin/{command}" if command in installed else None

        with mock.patch("code_mower.devin_readiness.shutil.which", side_effect=which):
            findings = devin_readiness(
                _config("devin-cli"),
                env={CLI_COMMAND_ENV: "devin-override"},
                lane_config=lane,
                config_dir=Path(_ISOLATED_STORE.name),
            )
        with mock.patch(
            "code_mower.doctor_checks.provider_local_cli_commands.shutil.which",
            side_effect=which,
        ):
            self.assertIsNone(resolved_local_cli_command(lane))
        finding = next(
            item for item in findings if item.name == "provider.devin.local_cli"
        )
        # Runtime resolves only the lane's own candidates, so an installed
        # default or override must not make readiness disagree with it.
        self.assertEqual(finding.status, STATUS_WARN)
        self.assertEqual(finding.detail["command"], "devin-lane")
        self.assertNotIn("devin-override", str(finding.detail) + finding.message)


class DevinGuidanceTests(unittest.TestCase):
    def test_session_brief_states_the_selected_posture_and_permissions(self) -> None:
        config = load_config(EXAMPLE_CONFIG)
        hosted = build_session(
            repo="codemower-ai/code-mower",
            host="claude",
            selected=("claude", "codex", "devin-api-v3"),
            config=config,
        )
        instructions = " ".join(hosted["instructions"])
        self.assertIn(DEVIN_API_KEY_ENV, instructions)
        self.assertIn(DEVIN_REPOSITORIES_ENV, instructions)
        self.assertIn("create, view, and manage", instructions)
        local = build_session(
            repo="codemower-ai/code-mower",
            host="claude",
            selected=("claude", "codex", "devin-cli"),
            config=config,
        )
        local_instructions = " ".join(local["instructions"])
        self.assertIn("Devin Desktop/CLI", local_instructions)
        self.assertNotIn(DEVIN_API_KEY_ENV, local_instructions)

    def test_default_session_brief_carries_no_devin_guidance(self) -> None:
        brief = build_session(
            repo="codemower-ai/code-mower",
            host="claude",
            selected=("claude", "codex"),
            config=load_config(EXAMPLE_CONFIG),
        )
        self.assertNotIn("Devin", " ".join(brief["instructions"]))

    def test_setup_instructions_reject_an_unknown_transport(self) -> None:
        with self.assertRaises(ConfigError):
            setup_instructions("devin_desktop")

    def test_setup_instructions_pin_the_selected_config_and_profile(self) -> None:
        hosted = setup_instructions(
            HOSTED_TRANSPORT,
            config_path="dir with spaces/code mower.yml",
            profile="custom devin",
            repo_slug="codemower-ai/code-mower",
        )
        self.assertIn(
            "code-mower doctor 'dir with spaces/code mower.yml' "
            "--profile 'custom devin' --devin --repo codemower-ai/code-mower",
            " ".join(hosted),
        )
        local = setup_instructions(LOCAL_TRANSPORT, profile="recommended")
        self.assertIn("--profile recommended --devin", " ".join(local))

    def test_guidance_without_profile_inputs_avoids_an_ambiguous_command(self) -> None:
        guidance = readiness_command()
        self.assertNotIn("code-mower doctor --profile", guidance)
        self.assertIn("same configuration and --profile selected here", guidance)
        instructions = " ".join(setup_instructions(LOCAL_TRANSPORT))
        self.assertIn("same configuration and --profile selected here", instructions)

    def test_session_brief_guidance_never_emits_an_unpinned_check(self) -> None:
        brief = build_session(
            repo="codemower-ai/code-mower",
            host="claude",
            selected=("claude", "codex", "devin-api-v3"),
            config=load_config(EXAMPLE_CONFIG),
        )
        instructions = " ".join(brief["instructions"])
        self.assertNotIn("Confirm readiness with `code-mower doctor", instructions)
        self.assertIn("same configuration and --profile selected here", instructions)

    def test_multiple_devin_transports_fail_closed_instead_of_first_match(self) -> None:
        config = {
            "session_defaults": {"participants": ["claude", "codex", "devin"]},
            "profiles": {
                "recommended": {"lanes": ["codex_cli", "devin_cli"]},
                "hosted-devin": {"lanes": ["codex_cli", "devin"]},
            },
        }
        with self.assertRaises(ConfigError) as raised:
            selected_devin_transport(config, lanes=("devin", "devin_cli"))
        self.assertIn("--profile", str(raised.exception))
        self.assertNotIn("participants", str(raised.exception))
        self.assertEqual(
            selected_devin_transport(config, lanes=("devin_cli",), profile="recommended"),
            LOCAL_TRANSPORT,
        )
        self.assertEqual(
            selected_devin_transport(config, lanes=("devin",), profile="hosted-devin"),
            HOSTED_TRANSPORT,
        )
        explicit = {
            **config,
            "session_defaults": {
                "participants": ["claude", "codex", "devin"],
                "transports": {"devin": "devin_api_v3"},
            },
        }
        self.assertEqual(
            selected_devin_transport(explicit, lanes=("devin", "devin_cli")),
            HOSTED_TRANSPORT,
        )

    def test_next_steps_add_the_devin_check_only_for_devin_profiles(self) -> None:
        templates = load_provider_templates(PROVIDER_TEMPLATES)
        default_steps = build_next_steps(templates, repo="codemower-ai/code-mower")
        self.assertNotIn(
            "devin-readiness", {step["id"] for step in default_steps["steps"]}
        )
        devin_templates = json.loads(json.dumps(templates))
        devin_templates["profiles"]["recommended"]["lanes"].append("devin_cli")
        devin_steps = build_next_steps(devin_templates, repo="codemower-ai/code-mower")
        step = next(
            item for item in devin_steps["steps"] if item["id"] == "devin-readiness"
        )
        self.assertEqual(
            step["command"],
            "code-mower doctor --profile recommended --devin "
            "--repo codemower-ai/code-mower --json",
        )
        self.assertEqual(step["lanes"], ["devin_cli"])
        self.assertEqual(devin_steps["steps"][-1]["id"], "devin-readiness")
        self.assertEqual(
            [step["id"] for step in devin_steps["steps"][:-1]],
            [step["id"] for step in default_steps["steps"]],
        )

    def test_next_steps_devin_check_preserves_the_selected_config_and_profile(self) -> None:
        templates = json.loads(json.dumps(load_provider_templates(PROVIDER_TEMPLATES)))
        templates["profiles"]["custom-devin"] = {
            "lanes": [*templates["profiles"]["recommended"]["lanes"], "devin"]
        }
        steps = build_next_steps(
            templates,
            profile="custom-devin",
            repo="codemower-ai/code-mower",
            config_path="custom.yml",
        )
        step = next(item for item in steps["steps"] if item["id"] == "devin-readiness")
        self.assertEqual(
            step["command"],
            "code-mower doctor custom.yml --profile custom-devin --devin "
            "--repo codemower-ai/code-mower --json",
        )
        doctor = next(item for item in steps["steps"] if item["id"] == "doctor-easy")
        self.assertIn("custom.yml", doctor["command"])
        self.assertIn("--profile custom-devin", doctor["command"])

    def test_next_steps_devin_check_quotes_unsafe_config_paths(self) -> None:
        templates = json.loads(json.dumps(load_provider_templates(PROVIDER_TEMPLATES)))
        templates["profiles"]["recommended"]["lanes"].append("devin_cli")
        steps = build_next_steps(
            templates,
            repo="codemower-ai/code-mower",
            config_path="dir with spaces/code mower.yml",
        )
        step = next(item for item in steps["steps"] if item["id"] == "devin-readiness")
        self.assertEqual(
            step["command"],
            "code-mower doctor 'dir with spaces/code mower.yml' "
            "--profile recommended --devin --repo codemower-ai/code-mower --json",
        )


class DevinPinnedRemediationTests(unittest.TestCase):
    """Every actionable command names the configuration that produced it."""

    CONFIG_PATH = "ops/custom mower.yml"
    PROFILE = "custom profile"
    DOCTOR = "code-mower doctor 'ops/custom mower.yml' --profile 'custom profile'"
    INIT = "code-mower init 'ops/custom mower.yml' --profile 'custom profile'"
    SWITCH_LOCAL = "--set-transport devin=devin_cli"
    SWITCH_HOSTED = "--set-transport devin=devin_api_v3"
    STAGED = "--apply --output-dir .code-mower.generated"

    def _assert_switch_steps(self, remediation: str, selection: str) -> None:
        """A switch previews, stages a review tree, and installs before rerunning."""
        self.assertIn(f"`{self.INIT} {selection} --dry-run`", remediation)
        self.assertIn(f"`{self.INIT} {selection} {self.STAGED}`", remediation)
        self.assertIn("install", remediation)
        self.assertIn(f"`{self.DOCTOR} --devin`", remediation)

    def _findings(self, *participants: str, **kwargs):
        kwargs.setdefault("env", {})
        kwargs.setdefault("config_path", self.CONFIG_PATH)
        kwargs.setdefault("config_profile", self.PROFILE)
        return _readiness(_config(*participants), **kwargs)

    def test_local_selection_and_unavailable_cli_pin_the_configuration(self) -> None:
        with mock.patch("code_mower.devin_readiness.shutil.which", return_value=None):
            findings = self._findings("claude", "codex", "devin-cli")
        selection = _finding(findings, "provider.devin.selection")
        self._assert_switch_steps(selection.remediation, self.SWITCH_HOSTED)
        cli = _finding(findings, "provider.devin.local_cli")
        self.assertIn(f"`{self.DOCTOR} --devin`", cli.remediation)
        self._assert_switch_steps(cli.remediation, self.SWITCH_HOSTED)
        # A switch never rewrites the participant list, which would delete every
        # unrelated participant and profile lane the configuration selected.
        for finding in findings:
            self.assertNotIn("--with", finding.remediation)
        # Public detail and cloud metadata stay path-free even though the
        # locally rendered remediation may repeat the caller's path.
        for finding in findings:
            self.assertNotIn(self.CONFIG_PATH, json.dumps(dict(finding.detail)))

    def test_hosted_selection_and_credentials_pin_the_configuration(self) -> None:
        findings = self._findings("devin-api-v3")
        selection = _finding(findings, "provider.devin.selection")
        self._assert_switch_steps(selection.remediation, self.SWITCH_LOCAL)
        credentials = _finding(findings, "provider.devin.hosted_credentials")
        self.assertIn(f"`{self.DOCTOR} --devin`", credentials.remediation)
        scope = _finding(findings, "provider.devin.repository_scope")
        self.assertIn(f"`{self.DOCTOR} --devin --repo OWNER/REPO`", scope.remediation)

    def test_observer_rerun_pins_the_configuration(self) -> None:
        findings = self._findings(
            "devin-cli",
            lane_config={"provider": "devin_cli", "driver": "local_cli"},
            adoption_posture="hosted-builders",
        )
        cli = _finding(findings, "provider.devin.local_cli")
        self.assertEqual(cli.status, "skip")
        self.assertIn(f"`{self.DOCTOR} --devin`", cli.remediation)

    def test_unselected_guidance_pins_the_configuration(self) -> None:
        findings = self._findings("claude", "codex", include_unselected=True)
        selection = _finding(findings, "provider.devin.selection")
        self._assert_switch_steps(selection.remediation, self.SWITCH_LOCAL)
        postures = _finding(findings, "provider.devin.postures")
        self._assert_switch_steps(postures.remediation, self.SWITCH_LOCAL)
        # The participant picker can rebuild a profile around the lanes it knows,
        # so no readiness answer offers it as a transport editor.
        for finding in findings:
            self.assertNotIn("--interactive", finding.remediation)
            self.assertNotIn("--with", finding.remediation)

    def test_the_recommended_profile_stays_explicit(self) -> None:
        with mock.patch("code_mower.devin_readiness.shutil.which", return_value=None):
            findings = _readiness(
                _config("devin-cli"), env={}, config_path="code-mower.yml"
            )
        cli = _finding(findings, "provider.devin.local_cli")
        self.assertIn(
            "`code-mower doctor code-mower.yml --profile recommended --devin`",
            cli.remediation,
        )
        self.assertIn(
            "`code-mower init code-mower.yml --profile recommended "
            "--set-transport devin=devin_api_v3 --dry-run`",
            cli.remediation,
        )
        self.assertIn(
            "`code-mower init code-mower.yml --profile recommended "
            "--set-transport devin=devin_api_v3 --apply --output-dir "
            ".code-mower.generated`",
            cli.remediation,
        )
        # The credential profile selector is never conflated with it.
        self.assertNotIn("--provider-profile", cli.remediation)


class DevinMultiLaneReadinessTests(unittest.TestCase):
    """A profile may validly select several Devin lanes on one transport."""

    def _local_lane(self, command: str) -> dict:
        return {
            "provider": "devin_cli",
            "driver": "local_cli",
            "product": "devin",
            "transport": LOCAL_TRANSPORT,
            "provider_config": {"command": command},
        }

    def _hosted_lane(self) -> dict:
        return {
            "provider": "devin",
            "driver": "hosted_bridge",
            "product": "devin",
            "transport": HOSTED_TRANSPORT,
        }

    def _checks(self, lanes, **kwargs):
        return check_devin_readiness(
            config=_config("devin-cli" if kwargs.pop("local", True) else "devin-api-v3"),
            effective_lanes=tuple(lanes),
            provider_config_dir=Path(_ISOLATED_STORE.name),
            **kwargs,
        )

    def test_one_custom_lane_owns_every_finding(self) -> None:
        for local, lane in ((True, self._local_lane("team-devin")), (False, self._hosted_lane())):
            with self.subTest(local=local):
                with mock.patch(
                    "code_mower.devin_readiness.shutil.which", return_value=None
                ):
                    checks = self._checks([("team_devin", lane)], local=local)
                self.assertEqual({check.lane for check in checks}, {"team_devin"})

    def test_same_transport_local_lanes_report_their_own_executables(self) -> None:
        lanes = [
            ("team_devin", self._local_lane("team-devin")),
            ("night_devin", self._local_lane("night-devin")),
        ]
        installed = {"team-devin"}

        def which(command: str) -> str | None:
            return f"/usr/local/bin/{command}" if command in installed else None

        with mock.patch(
            "code_mower.devin_readiness.shutil.which", side_effect=which
        ):
            checks = self._checks(lanes)
        names = [check.name for check in checks]
        # The posture, capabilities, permissions, and lifecycle belong to the
        # product, so they are stated once and never attributed to one of the
        # lanes that share them.
        for name in (
            "provider.devin.selection",
            "provider.devin.capabilities",
            "provider.devin.permissions",
            "provider.devin.lifecycle",
        ):
            self.assertEqual(names.count(name), 1, name)
            product = next(check for check in checks if check.name == name)
            self.assertIsNone(product.lane)
        selection = next(
            check for check in checks if check.name == "provider.devin.selection"
        )
        self.assertEqual(selection.detail["lanes"], ["team_devin", "night_devin"])
        runtime = [
            check for check in checks if check.name == "provider.devin.local_cli"
        ]
        self.assertEqual([check.lane for check in runtime], ["team_devin", "night_devin"])
        self.assertEqual(
            [check.detail["commands"] for check in runtime],
            [["team-devin"], ["night-devin"]],
        )
        # Each lane reports its own executable, so a configuration is neither
        # rejected nor reported as ready on another lane's behalf.
        self.assertEqual([check.status for check in runtime], ["pass", "warn"])

    def test_same_transport_hosted_lanes_report_credentials_once(self) -> None:
        lanes = [("team_devin", self._hosted_lane()), ("night_devin", self._hosted_lane())]
        checks = self._checks(lanes, local=False, transport=HOSTED_TRANSPORT)
        names = [check.name for check in checks]
        for name in (
            "provider.devin.hosted_credentials",
            "provider.devin.repository_scope",
        ):
            self.assertEqual(names.count(name), 1, name)
            self.assertIsNone(
                next(check for check in checks if check.name == name).lane
            )
        self.assertNotIn("provider.devin.local_cli", names)

    def test_custom_lanes_are_retargeted_by_bounded_manual_guidance(self) -> None:
        # No generated command can retarget a lane the repository named: the
        # participant picker selects products and canonical reviewer lanes, so it
        # would rebuild the profile and drop the lanes it cannot name.
        cases = (
            (True, [("team_devin", self._local_lane("team-devin"))], HOSTED_TRANSPORT),
            (
                True,
                [
                    ("team_devin", self._local_lane("team-devin")),
                    ("night_devin", self._local_lane("night-devin")),
                ],
                HOSTED_TRANSPORT,
            ),
            (False, [("team_devin", self._hosted_lane())], LOCAL_TRANSPORT),
            (
                False,
                [
                    ("team_devin", self._hosted_lane()),
                    ("night_devin", self._hosted_lane()),
                ],
                LOCAL_TRANSPORT,
            ),
        )
        for local, lanes, wanted in cases:
            with self.subTest(local=local, lanes=len(lanes)):
                with mock.patch(
                    "code_mower.devin_readiness.shutil.which", return_value=None
                ):
                    checks = self._checks(
                        lanes,
                        local=local,
                        config_path="ops/custom mower.yml",
                        config_profile="custom profile",
                    )
                selection = next(
                    check for check in checks if check.name == "provider.devin.selection"
                )
                # Only the configured lane IDs and their public declaration
                # fields are named, so nothing is rebuilt and nothing is dropped.
                for lane_id, _ in lanes:
                    self.assertIn(f"`{lane_id}`", selection.remediation)
                self.assertIn(f"`transport: {wanted}`", selection.remediation)
                # Editing the four declaration fields alone can leave the
                # configuration invalid or still selecting the old transport, so
                # every transport-dependent setting is named too.
                self.assertIn("`capabilities`", selection.remediation)
                self.assertIn(
                    "`provider_config.campaign_transport`", selection.remediation
                )
                self.assertIn(
                    f"`session_defaults.transports.devin: {wanted}`",
                    selection.remediation,
                )
                self.assertIn(
                    f"`{TRANSPORT_PARTICIPANT_ALIASES[wanted]}`", selection.remediation
                )
                self.assertIn(
                    f"`driver: {TRANSPORTS[wanted].driver}`", selection.remediation
                )
                self.assertIn("'ops/custom mower.yml'", selection.remediation)
                self.assertIn("'custom profile'", selection.remediation)
                for forbidden in ("--interactive", "--with", "--set-transport"):
                    self.assertNotIn(forbidden, selection.remediation)

    @staticmethod
    def _named_devin_config(transport: str, names: tuple[str, ...]) -> dict:
        """Return a full maintained declaration renamed to custom lanes.

        Lanes copied from the maintained configuration carry `capabilities` and a
        `provider_config.campaign_transport`, and the configuration saves its own
        transport selection and participant alias, so guidance is only complete if
        performing every edit it names leaves a valid configuration.
        """
        config = config_with_transport(load_config(EXAMPLE_CONFIG), "devin", transport)
        maintained = TRANSPORTS[transport].review_lane
        source = dict(config["lanes"][maintained])
        source["capabilities"] = asdict(TRANSPORTS[transport].capabilities)
        source["provider_config"] = {
            **dict(source.get("provider_config") or {}),
            "campaign_transport": transport,
        }
        del config["lanes"][maintained]
        for index, lane_id in enumerate(names):
            config["lanes"][lane_id] = {
                **copy.deepcopy(source),
                "labels": {
                    "needs": f"needs-{lane_id}",
                    "done": f"{lane_id}-done",
                    "blocked": f"{lane_id}-blocked",
                },
            }
            if index:
                # Same-transport lanes stay distinguishable by their own command.
                config["lanes"][lane_id]["provider_config"]["command"] = (
                    f"{lane_id}-devin"
                )
        active = [
            lane for lane in config["profiles"]["recommended"]["lanes"] if lane != maintained
        ]
        config["profiles"]["recommended"]["lanes"] = [*active, *names]
        config["session_defaults"]["transports"] = {"devin": transport}
        config["session_defaults"]["participants"] = [
            *DEFAULT_PARTICIPANTS,
            TRANSPORT_PARTICIPANT_ALIASES[transport],
        ]
        return config

    def test_following_the_manual_guidance_retargets_every_named_lane(self) -> None:
        for names in (("team_devin",), ("team_devin", "night_devin")):
            for start, wanted in (
                (LOCAL_TRANSPORT, HOSTED_TRANSPORT),
                (HOSTED_TRANSPORT, LOCAL_TRANSPORT),
            ):
                with self.subTest(lanes=len(names), start=start, wanted=wanted):
                    config = self._named_devin_config(start, names)
                    self.assertEqual(validate_config(config), [])
                    self.assertEqual(selected_devin_transport(config), start)
                    entry = TRANSPORTS[wanted]
                    for lane_id in names:
                        lane = config["lanes"][lane_id]
                        lane.update(
                            product="devin",
                            provider="devin" if wanted == HOSTED_TRANSPORT else "devin_cli",
                            transport=wanted,
                            driver=entry.driver,
                        )
                        del lane["capabilities"]
                        lane["provider_config"]["campaign_transport"] = wanted
                    config["session_defaults"]["transports"]["devin"] = wanted
                    config["session_defaults"]["participants"] = [
                        TRANSPORT_PARTICIPANT_ALIASES[wanted]
                        if name == TRANSPORT_PARTICIPANT_ALIASES[start]
                        else name
                        for name in config["session_defaults"]["participants"]
                    ]
                    self.assertEqual(validate_config(config), [])
                    self.assertEqual(selected_devin_transport(config), wanted)
                    findings = _readiness(
                        config,
                        env={},
                        config_path="ops/custom mower.yml",
                        config_profile="recommended",
                    )
                    selection = _finding(findings, "provider.devin.selection")
                    self.assertEqual(selection.detail["transport"], wanted)
                    # Every named lane keeps its own ID, and unrelated participants stay.
                    for lane_id in names:
                        self.assertIn(
                            lane_id, config["profiles"]["recommended"]["lanes"]
                        )
                    self.assertEqual(
                        [
                            name
                            for name in config["session_defaults"]["participants"]
                            if name not in TRANSPORT_PARTICIPANT_ALIASES.values()
                        ],
                        list(DEFAULT_PARTICIPANTS),
                    )


class DevinTransportSwitchTests(unittest.TestCase):
    """A transport switch replaces Devin's transport and nothing else."""

    PARTICIPANTS = ("claude", "codex", "cursor", "gitar", "devin-cli")
    CUSTOM_LANE = (
        "lanes:\n"
        "  house_review:\n"
        "    type: audit\n"
        "    driver: local_cli\n"
        "    provider: codex\n"
        "    informational: true\n"
        "    labels:\n"
        "      needs: needs-house-review\n"
        "      done: house-review-done\n"
        "      blocked: house-review-blocked\n"
    )

    def _config_text(self) -> str:
        """Return an example config that also selects an unrelated custom lane."""
        text = EXAMPLE_CONFIG.read_text(encoding="utf-8")
        text = text.replace("\nlanes:\n", "\n" + self.CUSTOM_LANE, 1)
        return text.replace(
            "  recommended:\n"
            "    description: Start with Codex and Claude as local peer reviewers.\n"
            "    lanes:\n"
            "      - codex\n"
            "      - claude_audit\n",
            "  recommended:\n"
            "    description: Start with Codex and Claude as local peer reviewers.\n"
            "    lanes:\n"
            "      - codex\n"
            "      - claude_audit\n"
            "      - house_review\n",
            1,
        )

    def _config(self) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "code-mower.yml"
            path.write_text(self._config_text(), encoding="utf-8")
            config = config_with_participants(load_config(path), self.PARTICIPANTS)
        # A scripted participant selection covers the catalog products only, so the
        # repository's own lane is selected the way an operator would keep it.
        active = config["profiles"]["recommended"]["lanes"]
        config["profiles"]["recommended"]["lanes"] = [*active, "house_review"]
        return config

    def test_the_selection_parses_a_product_and_transport(self) -> None:
        self.assertEqual(
            parse_transport_selection("devin=devin_api_v3"), ("devin", HOSTED_TRANSPORT)
        )
        for invalid in ("devin", "=devin_cli", "devin=", "claude=devin_cli"):
            with self.subTest(invalid=invalid), self.assertRaises(ConfigError):
                parse_transport_selection(invalid)

    def test_switching_the_transport_keeps_every_other_selection(self) -> None:
        before = self._config()
        after = config_with_transport(before, "devin", HOSTED_TRANSPORT)
        self.assertEqual(
            after["profiles"]["recommended"]["lanes"],
            ["claude_audit", "codex", "gitar", "devin", "house_review"],
        )
        self.assertEqual(
            after["session_defaults"]["participants"],
            list(before["session_defaults"]["participants"]),
        )
        self.assertEqual(
            after["session_defaults"]["transports"]["devin"], HOSTED_TRANSPORT
        )
        for lane_id, lane in before["lanes"].items():
            self.assertEqual(after["lanes"][lane_id], lane, lane_id)
        self.assertEqual(validate_config(after), [])
        # The fresh default pair still switches, and a configuration that saved no
        # participant list is not narrowed to a generated selection.
        default = load_config(EXAMPLE_CONFIG)
        switched = config_with_transport(default, "devin", LOCAL_TRANSPORT)
        self.assertEqual(validate_config(switched), [])
        self.assertEqual(
            switched["session_defaults"]["participants"],
            [*DEFAULT_PARTICIPANTS, "devin-cli"],
        )
        self.assertEqual(
            switched["profiles"]["recommended"]["lanes"],
            [*default["profiles"]["recommended"]["lanes"], "devin_cli"],
        )

    def test_every_profile_selecting_devin_follows_the_saved_selection(self) -> None:
        # The saved selection is repository-wide, so a switch that retargeted one
        # profile alone would leave another profile declaring the transport its own
        # readiness contradicts.
        before = config_with_transport(
            load_config(EXAMPLE_CONFIG), "devin", LOCAL_TRANSPORT
        )
        before["profiles"]["nightly"] = {
            "description": "Nightly builders with Devin selected.",
            "lanes": ["codex", "devin_cli"],
        }
        before["profiles"]["review_only"] = {
            "description": "Reviewers without Devin.",
            "lanes": ["codex", "claude_audit"],
        }
        self.assertEqual(validate_config(before), [])
        after = config_with_transport(before, "devin", HOSTED_TRANSPORT)
        self.assertEqual(validate_config(after), [])
        self.assertEqual(
            after["session_defaults"]["transports"]["devin"], HOSTED_TRANSPORT
        )
        for name in ("recommended", "nightly"):
            with self.subTest(profile=name):
                lanes = after["profiles"][name]["lanes"]
                self.assertIn("devin", lanes)
                self.assertNotIn("devin_cli", lanes)
                self.assertEqual(
                    selected_devin_transport(after, profile=name), HOSTED_TRANSPORT
                )
        self.assertEqual(
            after["profiles"]["review_only"], before["profiles"]["review_only"]
        )
        self.assertNotIn("devin", after["profiles"]["review_only"]["lanes"])

    def test_a_custom_named_devin_lane_in_any_profile_is_never_rewritten(self) -> None:
        config = config_with_transport(
            load_config(EXAMPLE_CONFIG), "devin", LOCAL_TRANSPORT
        )
        config["lanes"]["team_devin"] = {
            **dict(config["lanes"]["devin_cli"]),
            "labels": {
                "needs": "needs-team-devin",
                "done": "team-devin-done",
                "blocked": "team-devin-blocked",
            },
        }
        config["profiles"]["nightly"] = {
            "description": "Nightly builders with a lane this repository named.",
            "lanes": ["codex", "team_devin"],
        }
        self.assertEqual(validate_config(config), [])
        with self.assertRaises(ConfigError) as caught:
            config_with_transport(config, "devin", HOSTED_TRANSPORT)
        self.assertIn("team_devin", str(caught.exception))
        self.assertIn("nightly", str(caught.exception))

    def test_a_custom_named_devin_lane_is_never_rewritten(self) -> None:
        config = self._config()
        config["lanes"]["team_devin"] = dict(config["lanes"]["devin_cli"])
        config["profiles"]["recommended"]["lanes"] = [
            "team_devin" if lane == "devin_cli" else lane
            for lane in config["profiles"]["recommended"]["lanes"]
        ]
        with self.assertRaises(ConfigError):
            config_with_transport(config, "devin", HOSTED_TRANSPORT)

    def _init(self, path: Path, *args: str) -> dict:
        stream = StringIO()
        with redirect_stdout(stream):
            code_mower_init.main([str(path), *args])
        return json.loads(stream.getvalue())

    def test_init_switches_only_the_devin_transport_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "custom mower.yml"
            source.write_text(self._config_text(), encoding="utf-8")
            selected = root / "selected"
            self._init(
                source,
                "--profile",
                "recommended",
                "--with",
                ",".join(self.PARTICIPANTS),
                "--apply",
                "--output-dir",
                str(selected),
                "--json",
            )
            written = selected / "code-mower.yml"
            text = written.read_text(encoding="utf-8")
            marker = '\n      - "devin_cli"\n'
            self.assertIn(marker, text)
            written.write_text(
                text.replace(marker, marker + '      - "house_review"\n', 1),
                encoding="utf-8",
            )
            before = load_config(written)
            # The generated configuration is compared against one written by the
            # same path without a transport switch, so only the switch's own
            # difference can appear.
            kept = root / "kept"
            self._init(
                written,
                "--profile",
                "recommended",
                "--tracker",
                "github",
                "--apply",
                "--output-dir",
                str(kept),
                "--json",
            )
            baseline = load_config(kept / "code-mower.yml")
            switched = root / "switched"
            applied = self._init(
                written,
                "--profile",
                "recommended",
                "--set-transport",
                "devin=devin_api_v3",
                "--apply",
                "--output-dir",
                str(switched),
                "--json",
            )
            self.assertIn(str(switched / "code-mower.yml"), applied["written_files"])
            after = load_config(switched / "code-mower.yml")
            # Only Devin's transport, its own lane, and its own alias move; every
            # unrelated participant and profile lane survives the switch.
            self.assertEqual(
                after["session_defaults"]["participants"],
                before["session_defaults"]["participants"],
            )
            self.assertEqual(
                after["session_defaults"]["transports"]["devin"], HOSTED_TRANSPORT
            )
            self.assertEqual(
                [
                    lane
                    for lane in after["profiles"]["recommended"]["lanes"]
                    if lane != "devin"
                ],
                [
                    lane
                    for lane in before["profiles"]["recommended"]["lanes"]
                    if lane != "devin_cli"
                ],
            )
            self.assertIn("devin", after["profiles"]["recommended"]["lanes"])
            self.assertIn("house_review", after["profiles"]["recommended"]["lanes"])
            self.assertEqual(
                set(after["lanes"]) - set(baseline["lanes"]), {"devin"}
            )
            for lane_id, lane in baseline["lanes"].items():
                self.assertEqual(after["lanes"][lane_id], lane, lane_id)
            self.assertEqual(
                {name: profile for name, profile in after["profiles"].items()
                 if name != "recommended"},
                {name: profile for name, profile in baseline["profiles"].items()
                 if name != "recommended"},
            )

    def test_a_preview_switches_nothing_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "custom mower.yml"
            text = self._config_text()
            source.write_text(text, encoding="utf-8")
            output = root / "preview"
            plan = self._init(
                source,
                "--profile",
                "recommended",
                "--set-transport",
                "devin=devin_cli",
                "--dry-run",
                "--json",
                "--output-dir",
                str(output),
            )
            self.assertEqual(
                plan["transport_selection"],
                {"product": "devin", "transport": LOCAL_TRANSPORT},
            )
            self.assertEqual(source.read_text(encoding="utf-8"), text)
            self.assertFalse(output.exists())

    def test_the_rendered_switch_stages_a_review_tree_without_switching(self) -> None:
        # The generated remediation is followed literally: a preview mutates
        # nothing, an apply writes only the review tree, and readiness keeps
        # describing the installed configuration until the operator installs the
        # generated one.
        self.assertEqual(GENERATED_OUTPUT_DIR, code_mower_init.DEFAULT_APPLY_OUTPUT_DIR)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            starter = root / "custom mower.yml"
            starter.write_text(self._config_text(), encoding="utf-8")
            installed = root / "selected"
            self._init(
                starter, "--profile", "recommended",
                "--with", ",".join(self.PARTICIPANTS), "--apply", "--json",
                "--output-dir", str(installed),
            )
            source = installed / "code-mower.yml"
            text = source.read_text(encoding="utf-8")
            staged = root / GENERATED_OUTPUT_DIR
            self._init(
                source, "--profile", "recommended",
                "--set-transport", "devin=devin_api_v3", "--dry-run", "--json",
                "--output-dir", str(staged),
            )
            self.assertEqual(source.read_text(encoding="utf-8"), text)
            self.assertFalse(staged.exists())
            self._init(
                source, "--profile", "recommended",
                "--set-transport", "devin=devin_api_v3", "--apply", "--json",
                "--output-dir", str(staged),
            )
            self.assertEqual(source.read_text(encoding="utf-8"), text)
            self.assertEqual(
                selected_devin_transport(load_config(staged / "code-mower.yml")),
                HOSTED_TRANSPORT,
            )
            # The active configuration is still the local posture, so readiness
            # must not claim the switch happened because files were staged.
            self.assertEqual(
                selected_devin_transport(load_config(source)), LOCAL_TRANSPORT
            )
            with mock.patch(
                "code_mower.devin_readiness.shutil.which", return_value=None
            ):
                findings = devin_readiness(
                    load_config(source), env={}, config_path=str(source)
                )
            selection = _finding(findings, "provider.devin.selection")
            self.assertEqual(selection.detail["transport"], LOCAL_TRANSPORT)


class DevinDocumentationTests(unittest.TestCase):
    def test_docs_distinguish_local_and_hosted_setup_paths(self) -> None:
        for name in ("docs/troubleshooting.md", "docs/upgrade-existing-repo.md", "docs/sessions.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("code-mower doctor --profile recommended --devin", text, name)
            self.assertNotIn("code-mower doctor --devin", text, name)
            self.assertIn("recommended", text, name)
        for name in ("docs/troubleshooting.md", "docs/upgrade-existing-repo.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn(DEVIN_REPOSITORIES_ENV, text, name)
            self.assertIn("devin auth login", text, name)


if __name__ == "__main__":
    unittest.main()
