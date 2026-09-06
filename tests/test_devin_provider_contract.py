"""Focused regression and privacy tests for Devin CLI/Cloud contract (issue #743)."""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

from code_mower import config as code_mower_config
from code_mower import init as code_mower_init
from code_mower import package as code_mower_package
from code_mower import code_mower_telemetry
from code_mower.config import ConfigError
from code_mower.doctor_checks import check_lane_runtime, check_adoption_campaign_readiness
from code_mower.doctor_checks.campaign_auth import (
    campaign_auth_location_label,
    check_campaign_auth_readiness,
)
from code_mower.doctor_checks.provider_local_cli import (
    check_local_cli,
    check_local_cli_probe,
)
from code_mower.doctor_checks.provider_local_cli_commands import local_cli_command
from code_mower.doctor_checks.provider_local_cli_probe_config import local_cli_probe_args
from code_mower.provider_registry import REFERENCE_PROVIDERS
from code_mower.providers import build_provider_lane_tool_provenance
from code_mower.release_campaigns import (
    PROVIDER_ALIAS_MAP,
    initialize_campaign,
    resolve_provider_lane,
)


def _write_executable(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)
    return path


def _lane_dict(lane_id: str) -> dict[str, object]:
    lane = REFERENCE_PROVIDERS[lane_id]
    return {
        field.name: (
            dict(lane.provider_config)
            if field.name == "provider_config"
            else getattr(lane, field.name)
        )
        for field in dataclasses.fields(lane)
    }


def _lane_config(lane_id: str) -> dict[str, Any]:
    """Return a config-shaped lane dict from the reference registry."""
    lane = REFERENCE_PROVIDERS[lane_id]
    labels = lane.labels
    return {
        "type": lane.lane_type,
        "driver": lane.driver,
        "provider": lane.provider,
        "merge_authority": lane.merge_authority,
        "informational": lane.informational,
        "enabled_by_default": lane.enabled_by_default,
        "trigger_policy": lane.trigger_policy,
        "spend_policy": lane.spend_policy,
        "labels": {
            "needs": labels.needs,
            "done": labels.done,
            "blocked": labels.blocked,
        },
        "token_env": list(lane.token_env),
        "token_env_any": [list(group) for group in lane.token_env_any],
        "provider_config": dict(lane.provider_config),
    }


@contextmanager
def _override_env(overrides: dict[str, str | None]) -> None:
    """Temporarily override/clear environment variables and restore them."""
    old = dict(os.environ)
    try:
        for key, value in overrides.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)
        yield
    finally:
        os.environ.clear()
        os.environ.update(old)


class _DevinTempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_path = Path(tempfile.mkdtemp())
        super().setUp()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_path, ignore_errors=True)
        super().tearDown()


class TestDevinRegistryContract(unittest.TestCase):
    def test_devin_lanes_are_registered(self) -> None:
        self.assertIn("devin", REFERENCE_PROVIDERS)
        self.assertIn("devin_cli", REFERENCE_PROVIDERS)
        self.assertNotIn("devin_cloud", REFERENCE_PROVIDERS)

    def test_devin_remains_hosted_compatibility_alias(self) -> None:
        lane = REFERENCE_PROVIDERS["devin"]
        self.assertEqual(lane.driver, "hosted_bridge")
        self.assertEqual(lane.provider, "devin")
        self.assertTrue(lane.merge_authority)
        self.assertEqual(lane.token_env, ("DEVIN_AUDIT_LABEL_TOKEN", "GITHUB_TOKEN"))
        self.assertEqual(lane.labels.needs, "needs-devin-audit")

    def test_devin_cli_is_local_and_informational(self) -> None:
        lane = REFERENCE_PROVIDERS["devin_cli"]
        self.assertEqual(lane.driver, "local_cli")
        self.assertEqual(lane.provider, "devin_cli")
        self.assertTrue(lane.informational)
        self.assertFalse(lane.merge_authority)
        self.assertFalse(lane.enabled_by_default)

    def test_devin_cli_declarative_metadata(self) -> None:
        config = REFERENCE_PROVIDERS["devin_cli"].provider_config
        self.assertEqual(config["command"], "devin")
        self.assertEqual(config["command_env"], "CODE_MOWER_DEVIN_CLI_COMMAND")
        self.assertEqual(config["model_env"], "CODE_MOWER_DEVIN_CLI_MODEL")
        self.assertEqual(config["model_env_any"], ("DEVIN_CLI_MODEL", "DEVIN_MODEL"))
        self.assertEqual(config["doctor_probe_args"], ("--version",))
        self.assertEqual(config["doctor_probe_timeout_seconds"], 30)
        self.assertEqual(config["campaign_auth_probe_args"], ("auth", "status"))
        self.assertEqual(config["campaign_auth_probe_timeout_seconds"], 20)
        self.assertIn(1, config["campaign_auth_logged_out_exit_codes"])
        self.assertEqual(config["campaign_auth_location_label"], "ambient Devin CLI session")
        self.assertIs(config["local_cli_path_basename_only"], True)
        self.assertIs(config["local_audit_eligible"], False)
        self.assertIs(config["campaign_eligible"], False)

    def test_devin_cli_package_manifest_source_exists(self) -> None:
        entry = (
            "src/code_mower/lane_configs/devin_cli.py",
            "src/code_mower/lane_configs/devin_cli.py",
            "lane-config",
        )
        self.assertIn(entry, code_mower_package.PACKAGE_FILES)
        self.assertTrue(
            (Path(__file__).resolve().parents[1] / entry[0]).is_file()
        )

    def test_provider_alias_map_resolves_devin_identities(self) -> None:
        self.assertEqual(PROVIDER_ALIAS_MAP["devin"], "devin")
        self.assertEqual(PROVIDER_ALIAS_MAP["devin_cloud"], "devin")
        self.assertEqual(PROVIDER_ALIAS_MAP["devin_cli"], "devin_cli")

    def test_telemetry_aliases_match_new_identities(self) -> None:
        self.assertEqual(code_mower_telemetry._lane_provider("devin-audit"), "devin")
        self.assertEqual(code_mower_telemetry._lane_provider("devin-cloud-audit"), "devin")
        self.assertEqual(code_mower_telemetry._lane_provider("devin-cli-audit"), "devin_cli")

    def test_builder_aliases_map_devin_variants_to_devin(self) -> None:
        self.assertEqual(code_mower_init._canonical_builder_lane("devin"), "devin")
        self.assertEqual(code_mower_init._canonical_builder_lane("devin-cloud"), "devin")
        self.assertEqual(code_mower_init._canonical_builder_lane("devin_cli"), "devin")
        self.assertEqual(code_mower_init._canonical_builder_lane("devin-cli"), "devin")

    def test_devin_cloud_resolves_to_canonical_devin_lane(self) -> None:
        provider, lane = resolve_provider_lane("devin_cloud")
        self.assertEqual(provider, "devin")
        self.assertEqual(lane.lane_id, "devin")
        self.assertEqual(lane.labels.needs, "needs-devin-audit")

    def test_devin_cli_is_rejected_from_campaign_initialization(self) -> None:
        # The lane still resolves as an identity (doctor and provenance use it);
        # only release-campaign participation is fail-closed until #744 lands a
        # maintained adapter.
        provider, lane = resolve_provider_lane("devin_cli")
        self.assertEqual(provider, "devin_cli")
        with self.assertRaisesRegex(ValueError, "not campaign eligible"):
            initialize_campaign(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["devin_cli"],
            )
        # Providers that omit the flag keep current behavior.
        provider, lane = resolve_provider_lane("devin_cloud")
        self.assertEqual(provider, "devin")
        campaign = initialize_campaign(
            release_tag="v1.0.0",
            package_spec="code-mower==1.0.0",
            providers=["devin_cloud"],
        )
        self.assertEqual(campaign.providers[0]["provider"], "devin")
        provider, lane = resolve_provider_lane("codex")
        self.assertEqual(provider, "codex")


class TestDevinLocalCliDoctor(_DevinTempDirTestCase):
    def test_devin_cli_command_resolution(self) -> None:
        lane = _lane_dict("devin_cli")
        self.assertEqual(local_cli_command(lane), "devin")
        self.assertEqual(local_cli_probe_args(lane, "devin"), ("--version",))

    def test_check_local_cli_finds_devin_and_reports_version(self) -> None:
        _write_executable(
            self.tmp_path / "devin",
            "#!/bin/sh\nprintf 'devin 1.2.3\\n'\n",
        )
        with _override_env(
            {
                "PATH": os.fspath(self.tmp_path),
                "CODE_MOWER_DEVIN_CLI_COMMAND": None,
            }
        ):
            check = check_local_cli(
                "devin_cli",
                _lane_dict("devin_cli"),
            )

        self.assertEqual(check.status, "pass")
        self.assertEqual(check.message, "devin found (devin 1.2.3)")
        self.assertEqual(check.detail["command"], "devin")
        self.assertEqual(check.detail["tool_version"], "devin 1.2.3")
        self.assertEqual(check.detail["path"], "devin")
        rendered = json.dumps(check.as_dict())
        self.assertNotIn(os.fspath(self.tmp_path), rendered)
        self.assertNotIn(os.fspath(self.tmp_path / "devin"), rendered)

    def test_check_local_cli_probe_runs_version_and_redacts_output(self) -> None:
        _write_executable(
            self.tmp_path / "devin",
            "#!/bin/sh\nprintf 'devin 1.2.3\\n'\n",
        )
        with _override_env(
            {
                "PATH": os.fspath(self.tmp_path),
                "CODE_MOWER_DEVIN_CLI_COMMAND": None,
            }
        ):
            check = check_local_cli_probe(
                "devin_cli",
                _lane_dict("devin_cli"),
                probe_runtime=True,
                http_timeout=5,
            )

        self.assertEqual(check.status, "pass")
        self.assertEqual(check.message, "devin probe succeeded")
        self.assertEqual(check.detail["command"], "devin")
        self.assertEqual(check.detail["returncode"], 0)
        self.assertEqual(check.detail["path"], "devin")
        self.assertTrue(check.detail["output_redacted"])
        self.assertEqual(check.detail["output_line_count"], 1)
        rendered = json.dumps(check.as_dict())
        self.assertNotIn("devin 1.2.3", rendered)
        self.assertNotIn(os.fspath(self.tmp_path), rendered)
        self.assertNotIn(os.fspath(self.tmp_path / "devin"), rendered)

    def test_check_local_cli_probe_preserves_path_for_failed_probe(self) -> None:
        _write_executable(
            self.tmp_path / "devin",
            "#!/bin/sh\nexit 1\n",
        )
        with _override_env(
            {
                "PATH": os.fspath(self.tmp_path),
                "CODE_MOWER_DEVIN_CLI_COMMAND": None,
            }
        ):
            check = check_local_cli_probe(
                "devin_cli",
                _lane_dict("devin_cli"),
                probe_runtime=True,
                http_timeout=5,
            )

        self.assertEqual(check.status, "warn")
        self.assertEqual(check.detail["command"], "devin")
        self.assertEqual(check.detail["path"], "devin")
        self.assertFalse(check.detail["output_redacted"])
        self.assertEqual(check.detail["output_line_count"], 0)
        rendered = json.dumps(check.as_dict())
        self.assertNotIn(os.fspath(self.tmp_path), rendered)
        self.assertNotIn(os.fspath(self.tmp_path / "devin"), rendered)

    def test_check_local_cli_redacts_absolute_command_override(self) -> None:
        executable = _write_executable(
            self.tmp_path / "devin",
            "#!/bin/sh\nprintf 'devin 1.2.3\\n'\n",
        )
        override = os.fspath(executable)
        with _override_env(
            {
                "CODE_MOWER_DEVIN_CLI_COMMAND": override,
                "PATH": "/nonexistent",
            }
        ):
            check = check_local_cli(
                "devin_cli",
                _lane_dict("devin_cli"),
            )

        self.assertEqual(check.status, "pass")
        self.assertEqual(check.message, "devin found (devin 1.2.3)")
        self.assertEqual(check.detail["command"], "devin")
        self.assertEqual(check.detail["commands"], ["devin"])
        self.assertEqual(check.detail["path"], "devin")
        rendered = json.dumps(check.as_dict())
        self.assertNotIn(override, rendered)
        self.assertNotIn(os.fspath(self.tmp_path), rendered)

    def test_check_local_cli_probe_redacts_absolute_command_override(self) -> None:
        executable = _write_executable(
            self.tmp_path / "devin",
            "#!/bin/sh\nprintf 'devin 1.2.3\\n'\n",
        )
        override = os.fspath(executable)
        with _override_env(
            {
                "CODE_MOWER_DEVIN_CLI_COMMAND": override,
                "PATH": "/nonexistent",
            }
        ):
            check = check_local_cli_probe(
                "devin_cli",
                _lane_dict("devin_cli"),
                probe_runtime=True,
                http_timeout=5,
            )

        self.assertEqual(check.status, "pass")
        self.assertEqual(check.message, "devin probe succeeded")
        self.assertEqual(check.detail["command"], "devin")
        self.assertEqual(check.detail["path"], "devin")
        rendered = json.dumps(check.as_dict())
        self.assertNotIn(override, rendered)
        self.assertNotIn(os.fspath(self.tmp_path), rendered)

    def test_check_local_cli_probe_redacts_override_on_failed_probe(self) -> None:
        executable = _write_executable(
            self.tmp_path / "devin",
            "#!/bin/sh\nexit 1\n",
        )
        override = os.fspath(executable)
        with _override_env(
            {
                "CODE_MOWER_DEVIN_CLI_COMMAND": override,
                "PATH": "/nonexistent",
            }
        ):
            check = check_local_cli_probe(
                "devin_cli",
                _lane_dict("devin_cli"),
                probe_runtime=True,
                http_timeout=5,
            )

        self.assertEqual(check.status, "warn")
        self.assertEqual(check.detail["command"], "devin")
        self.assertEqual(check.detail["path"], "devin")
        rendered = json.dumps(check.as_dict())
        self.assertNotIn(override, rendered)
        self.assertNotIn(os.fspath(self.tmp_path), rendered)

    def test_check_local_cli_redacts_override_when_not_found(self) -> None:
        override = os.fspath(self.tmp_path / "missing-devin")
        with _override_env(
            {
                "CODE_MOWER_DEVIN_CLI_COMMAND": override,
                "PATH": "/nonexistent",
            }
        ):
            check = check_local_cli(
                "devin_cli",
                _lane_dict("devin_cli"),
            )

            self.assertEqual(check.status, "warn")
            self.assertEqual(check.detail["commands"], ["missing-devin", "devin"])
            rendered = json.dumps(check.as_dict())
            self.assertNotIn(override, rendered)
            self.assertNotIn(os.fspath(self.tmp_path), rendered)

            probe = check_local_cli_probe(
                "devin_cli",
                _lane_dict("devin_cli"),
                probe_runtime=True,
                http_timeout=5,
            )

        self.assertEqual(probe.status, "warn")
        self.assertEqual(probe.detail["command"], "missing-devin")
        rendered = json.dumps(probe.as_dict())
        self.assertNotIn(override, rendered)
        self.assertNotIn(os.fspath(self.tmp_path), rendered)

    def test_check_local_cli_probe_redacts_override_in_exception_message(self) -> None:
        executable = _write_executable(
            self.tmp_path / "devin",
            "#!/bin/sh\nexit 0\n",
        )
        override = os.fspath(executable)

        def fake_run(*args, **kwargs):
            raise FileNotFoundError(2, f"No such file or directory: '{override}'")

        with _override_env(
            {
                "CODE_MOWER_DEVIN_CLI_COMMAND": override,
                "PATH": "/nonexistent",
            }
        ), mock.patch(
            "code_mower.doctor_checks.provider_local_cli.subprocess.run",
            fake_run,
        ):
            check = check_local_cli_probe(
                "devin_cli",
                _lane_dict("devin_cli"),
                probe_runtime=True,
                http_timeout=5,
            )

        self.assertEqual(check.status, "warn")
        rendered = json.dumps(check.as_dict())
        self.assertNotIn(override, rendered)
        self.assertNotIn(os.fspath(self.tmp_path), rendered)

    def test_check_local_cli_preserves_path_for_unrelated_provider(self) -> None:
        executable = _write_executable(
            self.tmp_path / "claude",
            "#!/bin/sh\nprintf 'claude 0.1.0\\n'\n",
        )
        with _override_env({"PATH": os.fspath(self.tmp_path)}):
            check = check_local_cli(
                "claude_audit",
                _lane_dict("claude_audit"),
            )

        self.assertEqual(check.status, "pass")
        self.assertEqual(check.detail["command"], "claude")
        self.assertEqual(check.detail["tool_version"], "claude 0.1.0")
        self.assertEqual(check.detail["path"], os.fspath(executable))

    def test_hosted_builder_posture_skips_devin_cli_local_checks(self) -> None:
        checks = check_lane_runtime(
            "devin_cli",
            {
                "driver": "local_cli",
                "provider": "devin_cli",
                "token_env": ["GITHUB_TOKEN"],
                "provider_config": {"command": "devin"},
            },
            probe_runtime=True,
            http_timeout=1,
            adoption_posture="hosted-builders",
        )

        local_checks = {
            check.name: check
            for check in checks
            if check.name
            in {"runtime.local_audit", "runtime.local_cli", "runtime.local_cli.probe"}
        }
        self.assertTrue(all(c.status == "skip" for c in local_checks.values()))

    def test_orchestrator_only_posture_skips_devin_cli_local_checks(self) -> None:
        checks = check_lane_runtime(
            "devin_cli",
            {
                "driver": "local_cli",
                "provider": "devin_cli",
                "token_env": ["GITHUB_TOKEN"],
                "provider_config": {"command": "devin"},
            },
            probe_runtime=True,
            http_timeout=1,
            adoption_posture="orchestrator-only",
        )

        local_checks = {
            check.name: check
            for check in checks
            if check.name
            in {"runtime.local_audit", "runtime.local_cli", "runtime.local_cli.probe"}
        }
        self.assertTrue(all(c.status == "skip" for c in local_checks.values()))


class TestDevinAuthProbe(unittest.TestCase):
    def test_devin_cli_auth_probe_authenticated(self) -> None:
        check = check_campaign_auth_readiness(
            lane=REFERENCE_PROVIDERS["devin_cli"],
            canonical="devin_cli",
            enabled=False,
            command="/opt/bin/devin",
            env={},
            probe_runner=lambda argv, timeout, env: subprocess.CompletedProcess(
                argv,
                0,
                stdout="Logged in as user@example.com\n",
                stderr="",
            ),
        )

        self.assertIsNotNone(check)
        self.assertEqual(check.status, "pass")
        self.assertEqual(check.message, "devin_cli ambient Devin CLI session is authenticated")
        self.assertNotIn("isolated campaign home", check.message)
        self.assertNotIn("user@example.com", check.message)
        self.assertNotIn("user@example.com", str(check.detail))
        self.assertNotIn("user@example.com", json.dumps(check.as_dict()))

    def test_devin_cli_auth_probe_unauthenticated(self) -> None:
        check = check_campaign_auth_readiness(
            lane=REFERENCE_PROVIDERS["devin_cli"],
            canonical="devin_cli",
            enabled=False,
            command="/opt/bin/devin",
            env={},
            probe_runner=lambda argv, timeout, env: subprocess.CompletedProcess(
                argv,
                1,
                stdout="",
                stderr="Error: not logged in\n",
            ),
        )

        self.assertIsNotNone(check)
        self.assertEqual(check.status, "warn")
        self.assertEqual(check.message, "devin_cli ambient Devin CLI session is not authenticated")
        self.assertNotIn("isolated campaign home", check.message)
        self.assertIn("ambient Devin CLI session", check.remediation)
        self.assertNotIn("isolated", check.remediation)
        self.assertNotIn("not logged in", check.message)
        self.assertNotIn("not logged in", str(check.detail))
        self.assertNotIn("not logged in", json.dumps(check.as_dict()))

    def test_devin_cli_auth_probe_unknown_nonzero_degrades(self) -> None:
        check = check_campaign_auth_readiness(
            lane=REFERENCE_PROVIDERS["devin_cli"],
            canonical="devin_cli",
            enabled=False,
            command="/opt/bin/devin",
            env={},
            probe_runner=lambda argv, timeout, env: subprocess.CompletedProcess(
                argv,
                7,
                stdout="",
                stderr="some internal error\n",
            ),
        )

        self.assertIsNotNone(check)
        self.assertEqual(check.status, "skip")
        self.assertIn("could not be verified", check.message)
        self.assertIn("ambient Devin CLI session", check.remediation)
        self.assertNotIn("isolated", check.remediation)
        self.assertNotIn("some internal error", str(check.detail))
        self.assertNotIn("some internal error", json.dumps(check.as_dict()))

    def test_other_providers_keep_isolated_campaign_home_wording(self) -> None:
        self.assertEqual(
            campaign_auth_location_label(REFERENCE_PROVIDERS["codex"]),
            "isolated campaign home",
        )
        self.assertEqual(
            campaign_auth_location_label(REFERENCE_PROVIDERS["devin_cli"]),
            "ambient Devin CLI session",
        )


class TestDevinProvenance(_DevinTempDirTestCase):
    def test_devin_cli_provenance_prefers_code_mower_model_env(self) -> None:
        _write_executable(
            self.tmp_path / "devin",
            "#!/bin/sh\nprintf 'devin 1.2.3\\n'\n",
        )
        with _override_env(
            {
                "PATH": os.fspath(self.tmp_path),
                "CODE_MOWER_DEVIN_CLI_MODEL": "devin-model-v1",
                "DEVIN_CLI_MODEL": None,
                "DEVIN_MODEL": None,
            }
        ):
            tool, detail = build_provider_lane_tool_provenance(
                "devin_cli",
                REFERENCE_PROVIDERS["devin_cli"],
                source="unit-test",
            )

        self.assertEqual(tool["tool_name"], "devin")
        self.assertEqual(tool["tool_version"], "devin 1.2.3")
        self.assertEqual(tool["provider"], "devin_cli")
        self.assertEqual(tool["model"], "devin-model-v1")
        self.assertEqual(tool["model_source"], "env")
        self.assertTrue(detail["command_found"])
        self.assertTrue(detail["model_known"])
        self.assertNotIn("path", detail)
        self.assertEqual(detail["path_basename"], "devin")

class TestDevinInitContract(unittest.TestCase):
    def _devin_cli_profile(self) -> dict[str, Any]:
        config = copy.deepcopy(
            code_mower_config.load_config(
                Path(__file__).resolve().parents[1]
                / "src"
                / "code_mower"
                / "templates"
                / "code-mower.example.yml"
            )
        )
        config["lanes"]["devin_cli"] = _lane_config("devin_cli")
        config["profiles"]["devin_cli_only"] = {
            "description": "test devin_cli local audit selection",
            "lanes": ["devin_cli"],
        }
        return config

    def test_devin_cli_init_rejects_local_audit_selection(self) -> None:
        config = self._devin_cli_profile()
        with self.assertRaisesRegex(ConfigError, "not available yet"):
            code_mower_init.render_init_plan(
                config,
                profile_id="devin_cli_only",
                package_mode=True,
                package_command="code-mower",
            )

    def test_codex_claude_init_still_generates_local_audit_workflow(self) -> None:
        config = copy.deepcopy(
            code_mower_config.load_config(
                Path(__file__).resolve().parents[1]
                / "src"
                / "code_mower"
                / "templates"
                / "code-mower.example.yml"
            )
        )
        plan = code_mower_init.render_init_plan(
            config,
            profile_id="recommended",
            package_mode=True,
            package_command="code-mower",
        )

        audit_files = [
            entry
            for entry in plan.data["generated_files"]
            if entry["path"] == ".github/workflows/local-cli-audit.yml"
        ]
        self.assertEqual(len(audit_files), 1)
        local_audit = audit_files[0]
        lanes = json.loads(local_audit["local_audit_lanes_json"])
        lane_names = {lane["lane"] for lane in lanes}
        self.assertIn("claude", lane_names)
        self.assertIn("codex", lane_names)
        self.assertNotIn("devin_cli", lane_names)


class TestDevinHostedDoctor(unittest.TestCase):
    def test_devin_cloud_alias_runs_hosted_devin_readiness(self) -> None:
        checks = check_adoption_campaign_readiness(
            config={},
            repo_root=Path.cwd(),
            repo_slug="owner/repo",
            providers=["devin_cloud"],
            env={
                "DEVIN_AUDIT_LABEL_TOKEN": "present",
                "GITHUB_TOKEN": "present",
            },
        )
        readiness = next(c for c in checks if c.name == "doctor.campaign.readiness")
        self.assertEqual(readiness.status, "warn")
        self.assertEqual(readiness.detail["provider_readiness"]["devin"]["auth"], "unverified_transport")

    def test_adoption_campaign_readiness_hosted_devin_distinguishes_transport(self) -> None:
        checks = check_adoption_campaign_readiness(
            config={},
            repo_root=Path.cwd(),
            repo_slug="owner/repo",
            providers=["devin"],
            env={
                "DEVIN_AUDIT_LABEL_TOKEN": "present",
                "GITHUB_TOKEN": "present",
            },
        )
        readiness = next(c for c in checks if c.name == "doctor.campaign.readiness")
        self.assertEqual(readiness.status, "warn")
        self.assertEqual(readiness.detail["provider_readiness"]["devin"]["auth"], "unverified_transport")

        transport = next(c for c in checks if c.name == "doctor.campaign.transport")
        self.assertEqual(transport.status, "warn")
        self.assertIn("transport", transport.message)
