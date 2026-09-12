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
    POSTURE_HOSTED_API,
    POSTURE_LOCAL_CLI,
    POSTURE_UNAVAILABLE,
    devin_readiness,
    selected_devin_transport,
    setup_instructions,
)
from code_mower.doctor_checks import (
    build_doctor_run_plan,
    check_devin_readiness,
    doctor_check_group_id,
)
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
        with mock.patch.dict(os.environ, {CLI_COMMAND_ENV: override}, clear=False), mock.patch(
            "code_mower.devin_readiness.shutil.which", side_effect=lambda name: name == override
        ) as which:
            findings = _readiness(_config("devin-cli"), env={})
        which.assert_called_once_with(override)
        cli = _finding(findings, "provider.devin.local_cli")
        self.assertEqual(cli.status, "pass")
        self.assertEqual(cli.detail["command"], "devin-cli-bin")
        self.assertNotIn("/opt/private", json.dumps(dict(cli.detail)) + cli.message + cli.remediation)

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
            step["command"], "code-mower doctor --devin --repo codemower-ai/code-mower --json"
        )
        self.assertEqual(step["lanes"], ["devin_cli"])


class DevinDocumentationTests(unittest.TestCase):
    def test_docs_distinguish_local_and_hosted_setup_paths(self) -> None:
        for name in ("docs/troubleshooting.md", "docs/upgrade-existing-repo.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("code-mower doctor --devin", text, name)
            self.assertIn(DEVIN_REPOSITORIES_ENV, text, name)
            self.assertIn("devin auth login", text, name)


if __name__ == "__main__":
    unittest.main()
