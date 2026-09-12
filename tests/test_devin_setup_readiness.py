"""Optional Devin setup, doctor readiness, guidance, and privacy contracts."""

import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from code_mower import doctor as code_mower_doctor
from code_mower.config import ConfigError, load_config
from code_mower.devin_readiness import (
    CLI_COMMAND_ENV,
    DEVIN_API_KEY_ENV,
    DEVIN_ORG_ID_ENV,
    DEVIN_REPOSITORIES_ENV,
    HOSTED_TRANSPORT,
    LOCAL_TRANSPORT,
    OBSERVER_POSTURES,
    POSTURE_HOSTED_API,
    POSTURE_LOCAL_CLI,
    POSTURE_UNAVAILABLE,
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
from code_mower.doctor_checks.devin import devin_effective_lane
from code_mower.doctor_checks.providers import check_lane_runtime
from code_mower.local_cli_commands import candidate_local_cli_commands
from code_mower.next_steps import build_next_steps
from code_mower.package import load_provider_templates
from code_mower.participants import DEFAULT_PARTICIPANTS, config_with_participants
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
                effective_lane=lane,
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
        selected = devin_effective_lane(lanes)
        self.assertEqual(
            candidate_local_cli_commands(selected, env={}), ["/opt/private/devin-lane"]
        )
        self.assertIsNone(devin_effective_lane(lanes[:1]))


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
