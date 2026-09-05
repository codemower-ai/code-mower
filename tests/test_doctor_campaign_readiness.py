"""Unit and integration tests for doctor release campaign readiness checks."""

from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from code_mower.doctor_checks import (
    STATUS_PASS,
    STATUS_SKIP,
    STATUS_WARN,
    check_adoption_campaign_readiness,
    doctor_check_group_id,
    run_doctor,
)


class DoctorCampaignReadinessTests(unittest.TestCase):
    def test_campaign_adapter_passes_when_command_and_adapter_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            config = {
                "lanes": {
                    "codex": {
                        "provider_config": {
                            "campaign_adapter_argv": ["{command}", "qualify", "--output", "{output}"],
                            "campaign_adapter_timeout_seconds": 60,
                        }
                    }
                }
            }
            checks = check_adoption_campaign_readiness(
                config=config,
                repo_root=repo_root,
                which_fn=lambda cmd: f"/bin/{cmd}" if cmd == "codex" else None,
                providers=["codex"],
            )
            adapter_checks = [c for c in checks if c.name == "doctor.campaign.adapter"]
            self.assertEqual(len(adapter_checks), 1)
            check = adapter_checks[0]
            self.assertEqual(check.status, STATUS_PASS)
            self.assertEqual(check.lane, "codex")
            self.assertTrue(check.detail.get("adapter_configured"))
            self.assertTrue(check.detail.get("command_found"))

    def test_campaign_runtime_unavailable_excludes_enabled_local_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "lanes": {
                    "codex": {
                        "provider_config": {
                            "campaign_adapter_argv": ["{command}", "qualify", "--output", "{output}"],
                        }
                    }
                }
            }
            with mock.patch(
                "code_mower.release_campaigns.resolve_supported_runtime",
                return_value=None,
            ):
                checks = check_adoption_campaign_readiness(
                    config=config,
                    repo_root=Path(tmp),
                    env={"CODE_MOWER_CAMPAIGN_AUTH_PROBE": "0"},
                    which_fn=lambda cmd: f"/bin/{cmd}" if cmd == "codex" else None,
                    providers=["codex"],
                )

            runtime = next(c for c in checks if c.name == "doctor.campaign.runtime")
            self.assertEqual(runtime.status, STATUS_WARN)
            self.assertEqual(runtime.lane, "codex")
            self.assertTrue(runtime.detail.get("actionable"))
            self.assertTrue(runtime.detail.get("owner_action"))
            self.assertEqual(runtime.detail.get("error"), "python_runtime_unavailable")
            self.assertNotIn("executable", runtime.detail)
            self.assertIn("CODE_MOWER_PYTHON", runtime.remediation)

            readiness = next(c for c in checks if c.name == "doctor.campaign.readiness")
            self.assertNotIn("codex", readiness.detail.get("ready_providers", []))
            self.assertIn("codex", readiness.detail.get("actionable_providers", []))

    def test_campaign_runtime_available_keeps_local_provider_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                "code_mower.release_campaigns.resolve_supported_runtime",
                return_value=("/private/python", "python_3.13"),
            ):
                checks = check_adoption_campaign_readiness(
                    config={
                        "lanes": {
                            "codex": {
                                "provider_config": {
                                    "campaign_adapter_argv": ["{command}", "qualify", "--output", "{output}"],
                                }
                            }
                        }
                    },
                    repo_root=Path(tmp),
                    env={"CODE_MOWER_CAMPAIGN_AUTH_PROBE": "0"},
                    which_fn=lambda cmd: f"/bin/{cmd}" if cmd == "codex" else None,
                    providers=["codex"],
                )

            runtime = next(c for c in checks if c.name == "doctor.campaign.runtime")
            self.assertEqual(runtime.status, STATUS_PASS)
            self.assertEqual(runtime.detail.get("runtime_class"), "python_3.13")
            self.assertNotIn("/private/python", repr(runtime.as_dict()))
            readiness = next(c for c in checks if c.name == "doctor.campaign.readiness")
            self.assertIn(
                "codex",
                readiness.detail.get("ready_providers", []),
                [(c.name, c.status, c.lane, c.detail) for c in checks],
            )

    def test_campaign_runtime_unavailable_is_optional_for_disabled_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                "code_mower.release_campaigns.resolve_supported_runtime",
                return_value=None,
            ):
                checks = check_adoption_campaign_readiness(
                    config={"lanes": {"codex": {"enabled": False}}},
                    repo_root=Path(tmp),
                    which_fn=lambda cmd: f"/bin/{cmd}" if cmd == "codex" else None,
                    providers=["codex"],
                )

            runtime = next(c for c in checks if c.name == "doctor.campaign.runtime")
            self.assertEqual(runtime.status, STATUS_WARN)
            self.assertTrue(runtime.detail.get("optional"))
            self.assertFalse(runtime.detail.get("actionable"))
            self.assertNotIn("owner_action", runtime.detail)
            readiness = next(c for c in checks if c.name == "doctor.campaign.readiness")
            self.assertNotIn("codex", readiness.detail.get("ready_providers", []))
            self.assertIn("codex", readiness.detail.get("optional_providers", []))

    def test_campaign_adapter_warns_actionable_when_enabled_missing_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            # An explicit null disables the maintained adapter while retaining the lane.
            checks = check_adoption_campaign_readiness(
                config={
                    "lanes": {
                        "codex": {
                            "provider_config": {"campaign_adapter_argv": None},
                        }
                    }
                },
                repo_root=repo_root,
                which_fn=lambda cmd: f"/bin/{cmd}" if cmd == "codex" else None,
                providers=["codex"],
            )
            adapter_checks = [c for c in checks if c.name == "doctor.campaign.adapter"]
            self.assertEqual(len(adapter_checks), 1)
            check = adapter_checks[0]
            self.assertEqual(check.status, STATUS_WARN)
            self.assertTrue(check.detail.get("actionable"))
            self.assertTrue(check.detail.get("owner_action"))
            self.assertIn("campaign adapter not configured", check.message)
            self.assertIn("Configure campaign_adapter_argv", check.remediation)

    def test_campaign_adapter_warns_actionable_when_enabled_missing_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            config = {
                "lanes": {
                    "codex": {
                        "provider_config": {
                            "campaign_adapter_argv": ["{command}", "qualify"],
                        }
                    }
                }
            }
            checks = check_adoption_campaign_readiness(
                config=config,
                repo_root=repo_root,
                which_fn=lambda cmd: None,
                providers=["codex"],
            )
            adapter_checks = [c for c in checks if c.name == "doctor.campaign.adapter"]
            self.assertEqual(len(adapter_checks), 1)
            check = adapter_checks[0]
            self.assertEqual(check.status, STATUS_WARN)
            self.assertTrue(check.detail.get("actionable"))
            self.assertTrue(check.detail.get("owner_action"))
            self.assertIn("not found on PATH", check.message)
            self.assertIn("Install codex CLI", check.remediation)

    def test_campaign_adapter_warns_optional_when_unconfigured_optional_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            # Antigravity is not enabled by default
            checks = check_adoption_campaign_readiness(
                config={},
                repo_root=repo_root,
                which_fn=lambda cmd: None,
                providers=["antigravity"],
            )
            adapter_checks = [c for c in checks if c.name == "doctor.campaign.adapter"]
            self.assertEqual(len(adapter_checks), 1)
            check = adapter_checks[0]
            self.assertEqual(check.status, STATUS_WARN)
            self.assertFalse(check.detail.get("actionable"))
            self.assertTrue(check.detail.get("optional"))
            self.assertNotIn("owner_action", check.detail)

    def test_campaign_adapter_explicitly_enabled_becomes_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            config = {
                "lanes": {
                    "antigravity_cli": {
                        "enabled_by_default": True,
                    }
                }
            }
            checks = check_adoption_campaign_readiness(
                config=config,
                repo_root=repo_root,
                which_fn=lambda cmd: None,
                providers=["antigravity"],
            )
            adapter_checks = [c for c in checks if c.name == "doctor.campaign.adapter"]
            self.assertEqual(len(adapter_checks), 1)
            check = adapter_checks[0]
            self.assertEqual(check.status, STATUS_WARN)
            self.assertTrue(check.detail.get("actionable"))
            self.assertTrue(check.detail.get("owner_action"))

    def test_campaign_adapter_explicitly_disabled_becomes_optional(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            config = {
                "lanes": {
                    "codex": {
                        "enabled": False,
                    }
                }
            }
            checks = check_adoption_campaign_readiness(
                config=config,
                repo_root=repo_root,
                which_fn=lambda cmd: None,
                providers=["codex"],
            )
            adapter_checks = [c for c in checks if c.name == "doctor.campaign.adapter"]
            self.assertEqual(len(adapter_checks), 1)
            check = adapter_checks[0]
            self.assertEqual(check.status, STATUS_WARN)
            self.assertFalse(check.detail.get("actionable"))
            self.assertTrue(check.detail.get("optional"))

    def test_campaign_adapter_honors_explicit_adapter_disable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checks = check_adoption_campaign_readiness(
                config={
                    "lanes": {
                        "codex": {
                            "provider_config": {"campaign_adapter_enabled": False},
                        }
                    }
                },
                repo_root=Path(tmp),
                which_fn=lambda command: f"/bin/{command}",
                providers=["codex"],
            )

            check = next(c for c in checks if c.name == "doctor.campaign.adapter")
            self.assertEqual(check.status, STATUS_WARN)
            self.assertFalse(check.detail.get("adapter_configured"))
            self.assertTrue(check.detail.get("actionable"))

    def test_campaign_adapter_invalid_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            # Conflicting aliases for the same lane
            config = {
                "lanes": {
                    "claude": {
                        "provider_config": {"campaign_adapter_argv": ["{command}"]}
                    },
                    "claude_code": {
                        "provider_config": {"campaign_adapter_argv": ["{command}"]}
                    },
                }
            }
            checks = check_adoption_campaign_readiness(
                config=config,
                repo_root=repo_root,
                which_fn=lambda cmd: "/bin/claude",
                providers=["claude"],
            )
            adapter_checks = [c for c in checks if c.name == "doctor.campaign.adapter"]
            self.assertEqual(len(adapter_checks), 1)
            check = adapter_checks[0]
            self.assertEqual(check.status, STATUS_WARN)
            self.assertEqual(check.detail.get("error"), "adapter_configuration_invalid")
            self.assertIn("same provider lane under 2 names", check.remediation)

    def test_campaign_adapter_skipped_in_observer_postures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            for posture in ("hosted-builders", "orchestrator-only"):
                checks = check_adoption_campaign_readiness(
                    config={},
                    repo_root=repo_root,
                    adoption_posture=posture,
                    providers=["claude", "codex"],
                )
                adapter_checks = [c for c in checks if c.name == "doctor.campaign.adapter"]
                self.assertEqual(len(adapter_checks), 2)
                for check in adapter_checks:
                    self.assertEqual(check.status, STATUS_SKIP)
                    self.assertTrue(check.detail.get("skipped"))
                    self.assertEqual(check.detail.get("adoption_posture"), posture)

    def test_campaign_credentials_passes_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            checks = check_adoption_campaign_readiness(
                config={},
                repo_root=repo_root,
                repo_slug="owner/repo",
                env={
                    "DEVIN_AUDIT_LABEL_TOKEN": "secret-token",
                    "CODE_MOWER_DEVIN_CAMPAIGN_TRANSPORT_READY": "1",
                },
                providers=["devin"],
            )
            cred_checks = [c for c in checks if c.name == "doctor.campaign.credentials"]
            self.assertEqual(len(cred_checks), 1)
            check = cred_checks[0]
            self.assertEqual(check.status, STATUS_PASS)
            self.assertEqual(check.lane, "devin")
            self.assertTrue(check.detail.get("has_credentials"))
            self.assertEqual(check.detail.get("repo_slug"), "owner/repo")
            # Must not leak secret value
            self.assertNotIn("secret-token", str(check.detail))

    def test_campaign_transport_warns_until_explicitly_verified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checks = check_adoption_campaign_readiness(
                config={},
                repo_root=Path(tmp),
                repo_slug="owner/repo",
                env={"DEVIN_AUDIT_LABEL_TOKEN": "secret-token"},
                providers=["devin"],
            )

            transport = [c for c in checks if c.name == "doctor.campaign.transport"]
            self.assertEqual(len(transport), 1)
            check = transport[0]
            self.assertEqual(check.status, STATUS_WARN)
            self.assertFalse(check.detail.get("transport_verified"))
            self.assertEqual(
                check.detail.get("verification_variable"),
                "CODE_MOWER_DEVIN_CAMPAIGN_TRANSPORT_READY",
            )
            self.assertNotIn("secret-token", str(check.detail))

    def test_campaign_credentials_warns_when_missing_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            checks = check_adoption_campaign_readiness(
                config={},
                repo_root=repo_root,
                repo_slug="owner/repo",
                env={},
                providers=["devin"],
            )
            cred_checks = [c for c in checks if c.name == "doctor.campaign.credentials"]
            self.assertEqual(len(cred_checks), 1)
            check = cred_checks[0]
            self.assertEqual(check.status, STATUS_WARN)
            self.assertEqual(check.detail.get("missing_variable"), "DEVIN_AUDIT_LABEL_TOKEN")
            self.assertFalse(check.detail.get("actionable"))
            self.assertTrue(check.detail.get("optional"))

    def test_campaign_credentials_warns_when_missing_repo_slug(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            checks = check_adoption_campaign_readiness(
                config={},
                repo_root=repo_root,
                repo_slug="",
                env={"DEVIN_AUDIT_LABEL_TOKEN": "token"},
                providers=["devin"],
            )
            cred_checks = [c for c in checks if c.name == "doctor.campaign.credentials"]
            self.assertEqual(len(cred_checks), 1)
            check = cred_checks[0]
            self.assertEqual(check.status, STATUS_WARN)
            self.assertIn("requires a repository slug", check.message)

    def test_campaign_storage_passes_and_preserves_privacy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            checks = check_adoption_campaign_readiness(
                config={},
                repo_root=repo_root,
                providers=[],
            )
            storage_checks = [c for c in checks if c.name == "doctor.campaign.storage"]
            self.assertEqual(len(storage_checks), 1)
            check = storage_checks[0]
            self.assertEqual(check.status, STATUS_PASS)
            self.assertEqual(check.detail.get("storage_dir"), ".code-mower/campaigns")
            self.assertTrue(check.detail.get("writable"))
            self.assertFalse((repo_root / ".code-mower").exists())
            # Confirm no absolute path leaked
            self.assertNotIn(tmp, check.message)
            self.assertNotIn(tmp, str(check.detail))

    def test_campaign_storage_warns_when_unwritable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            # Make a file where directory should be to force OSError
            (repo_root / ".code-mower").write_text("not a dir", encoding="utf-8")
            checks = check_adoption_campaign_readiness(
                config={},
                repo_root=repo_root,
                providers=[],
            )
            storage_checks = [c for c in checks if c.name == "doctor.campaign.storage"]
            self.assertEqual(len(storage_checks), 1)
            check = storage_checks[0]
            self.assertEqual(check.status, STATUS_WARN)
            self.assertFalse(check.detail.get("writable"))
            self.assertTrue(check.detail.get("actionable"))
            self.assertTrue(check.detail.get("owner_action"))
            self.assertEqual(check.detail.get("storage_dir"), ".code-mower/campaigns")
            self.assertNotIn(tmp, str(check.detail))

    def test_campaign_storage_warns_for_broken_path_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / ".code-mower").symlink_to(repo_root / "missing-target")

            checks = check_adoption_campaign_readiness(
                config={},
                repo_root=repo_root,
                providers=[],
            )

            check = next(c for c in checks if c.name == "doctor.campaign.storage")
            self.assertEqual(check.status, STATUS_WARN)
            self.assertFalse(check.detail.get("writable"))

    def test_campaign_cloud_upload_passes_with_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            checks = check_adoption_campaign_readiness(
                config={},
                repo_root=repo_root,
                env={"CODE_MOWER_CLOUD_TOKEN": "fake-cloud-token"},
                providers=[],
            )
            cloud_checks = [c for c in checks if c.name == "doctor.campaign.cloud_upload"]
            self.assertEqual(len(cloud_checks), 1)
            check = cloud_checks[0]
            self.assertEqual(check.status, STATUS_PASS)
            self.assertTrue(check.detail.get("configured"))
            self.assertEqual(check.detail.get("source"), "env")
            self.assertNotIn("fake-cloud-token", str(check.detail))

    def test_campaign_cloud_upload_passes_with_token_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            token_dir = Path(tmp) / "tokens"
            token_dir.mkdir()
            (token_dir / "cloud.env").write_text(
                "export CODE_MOWER_CLOUD_TOKEN=file-token\n",
                encoding="utf-8",
            )
            checks = check_adoption_campaign_readiness(
                config={},
                repo_root=repo_root,
                env={},
                token_dir=token_dir,
                providers=[],
            )
            cloud_checks = [c for c in checks if c.name == "doctor.campaign.cloud_upload"]
            self.assertEqual(len(cloud_checks), 1)
            check = cloud_checks[0]
            self.assertEqual(check.status, STATUS_PASS)
            self.assertTrue(check.detail.get("configured"))
            self.assertEqual(check.detail.get("source"), "single_profile")
            self.assertNotIn("file-token", str(check.detail))

    def test_campaign_cloud_upload_warns_optional_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            token_dir = Path(tmp) / "nonexistent"
            checks = check_adoption_campaign_readiness(
                config={},
                repo_root=repo_root,
                env={},
                token_dir=token_dir,
                providers=[],
            )
            cloud_checks = [c for c in checks if c.name == "doctor.campaign.cloud_upload"]
            self.assertEqual(len(cloud_checks), 1)
            check = cloud_checks[0]
            self.assertEqual(check.status, STATUS_WARN)
            self.assertFalse(check.detail.get("configured"))
            self.assertTrue(check.detail.get("optional"))
            self.assertFalse(check.detail.get("actionable"))

    def test_campaign_cloud_upload_warns_safely_when_profiles_are_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            token_dir = repo_root / "tokens"
            token_dir.mkdir()
            for name in ("alpha.env", "beta.env"):
                (token_dir / name).write_text(
                    "export CODE_MOWER_CLOUD_TOKEN=not-serialized\n",
                    encoding="utf-8",
                )

            checks = check_adoption_campaign_readiness(
                config={},
                repo_root=repo_root,
                env={},
                token_dir=token_dir,
                providers=[],
            )

            check = next(c for c in checks if c.name == "doctor.campaign.cloud_upload")
            self.assertEqual(check.status, STATUS_WARN)
            self.assertEqual(check.detail.get("status"), "ambiguous")
            self.assertEqual(check.detail.get("candidate_files"), ["alpha.env", "beta.env"])
            self.assertNotIn("not-serialized", str(check.as_dict()))

    def test_campaign_cloud_upload_rejects_empty_token_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            token_dir = repo_root / "tokens"
            token_dir.mkdir()
            (token_dir / "empty.env").write_text(
                "export CODE_MOWER_CLOUD_TOKEN=\n",
                encoding="utf-8",
            )

            checks = check_adoption_campaign_readiness(
                config={},
                repo_root=repo_root,
                env={},
                token_dir=token_dir,
                providers=[],
            )

            check = next(c for c in checks if c.name == "doctor.campaign.cloud_upload")
            self.assertEqual(check.status, STATUS_WARN)
            self.assertFalse(check.detail.get("configured"))
            self.assertEqual(check.detail.get("status"), "malformed")

    def test_campaign_board_visibility_passes_and_redacts_cwd(self) -> None:
        def fake_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            # Simulate lsof output finding a listener on port 8000
            if cmd[0] == "lsof" and "-a" in cmd:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="p1234\nn/example/secret-path/repo\n", stderr="")
            if cmd[0] == "lsof":
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="p1234\nn*:8000\n", stderr="")
            if cmd[0] == "ps":
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="python -m code_mower.board", stderr="")
            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            checks = check_adoption_campaign_readiness(
                config={},
                repo_root=repo_root,
                command_runner=fake_runner,
                providers=[],
            )
            board_checks = [c for c in checks if c.name == "doctor.campaign.board_visibility"]
            self.assertEqual(len(board_checks), 1)
            check = board_checks[0]
            self.assertEqual(check.status, STATUS_PASS)
            self.assertIn("Code Mower Board visible", check.message)
            boards = check.detail.get("boards", [])
            self.assertEqual(len(boards), 1)
            # Verify privacy: cwd and local paths must NOT be present
            self.assertNotIn("cwd", boards[0])
            self.assertNotIn("secret-path", str(check.detail))
            self.assertEqual(boards[0].get("port"), 8000)

    def test_campaign_board_visibility_warns_when_no_boards(self) -> None:
        def fake_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            # Listener on port 22 (ssh), not a board
            if cmd[0] == "lsof":
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="p999\nn*:22\n", stderr="")
            if cmd[0] == "ps":
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="/usr/sbin/sshd", stderr="")
            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            checks = check_adoption_campaign_readiness(
                config={},
                repo_root=repo_root,
                command_runner=fake_runner,
                providers=[],
            )
            board_checks = [c for c in checks if c.name == "doctor.campaign.board_visibility"]
            self.assertEqual(len(board_checks), 1)
            check = board_checks[0]
            self.assertEqual(check.status, STATUS_WARN)
            self.assertIn("not running locally", check.message)
            self.assertTrue(check.detail.get("available"))
            self.assertTrue(check.detail.get("optional"))
            self.assertFalse(check.detail.get("actionable"))

    def test_campaign_board_visibility_warns_when_inventory_unavailable(self) -> None:
        def fake_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            checks = check_adoption_campaign_readiness(
                config={},
                repo_root=repo_root,
                command_runner=fake_runner,
                providers=[],
            )
            board_checks = [c for c in checks if c.name == "doctor.campaign.board_visibility"]
            self.assertEqual(len(board_checks), 1)
            check = board_checks[0]
            self.assertEqual(check.status, STATUS_WARN)
            self.assertIn("inventory unavailable", check.message)
            self.assertFalse(check.detail.get("available"))
            self.assertTrue(check.detail.get("optional"))
            self.assertFalse(check.detail.get("actionable"))

    def test_group_mapping_for_campaign_checks(self) -> None:
        self.assertEqual(doctor_check_group_id("doctor.campaign.adapter", lane="codex"), "providers")
        self.assertEqual(doctor_check_group_id("doctor.campaign.credentials", lane="devin"), "providers")
        self.assertEqual(doctor_check_group_id("doctor.campaign.storage", lane=None), "setup")
        self.assertEqual(doctor_check_group_id("doctor.campaign.cloud_upload", lane=None), "setup")
        self.assertEqual(doctor_check_group_id("doctor.campaign.board_visibility", lane=None), "setup")

    def test_campaign_readiness_points_to_preview_and_ignores_other_lanes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checks = check_adoption_campaign_readiness(
                config={"lanes": {"gitar": {"enabled": True}}},
                repo_root=Path(tmp),
                env={"CODE_MOWER_CAMPAIGN_AUTH_PROBE": "0"},
                which_fn=lambda command: f"/bin/{command}",
                providers=["codex"],
            )

            self.assertFalse(any(check.lane == "gitar" for check in checks))
            readiness = next(c for c in checks if c.name == "doctor.campaign.readiness")
            self.assertEqual(readiness.status, STATUS_PASS)
            self.assertIn("release campaign", readiness.message)
            self.assertIn("code-mower release campaign", readiness.remediation)

    def test_run_doctor_integration_with_adoption_flag(self) -> None:
        root = Path(__file__).resolve().parents[1]
        report = run_doctor(
            config_path=root / "src/code_mower/templates/code-mower.example.yml",
            provider_templates_path=root / "src/code_mower/templates/providers.yml",
            profile="recommended",
            repo_slug="codemower-ai/code-mower",
            adoption=True,
        )
        check_names = {c.name for c in report.checks}
        self.assertIn("doctor.campaign.adapter", check_names)
        self.assertIn("doctor.campaign.credentials", check_names)
        self.assertIn("doctor.campaign.storage", check_names)
        self.assertIn("doctor.campaign.cloud_upload", check_names)
        self.assertIn("doctor.campaign.board_visibility", check_names)

    def test_run_doctor_passes_configured_repo_to_campaign_readiness(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with mock.patch(
            "code_mower.doctor_checks.runner.check_adoption_campaign_readiness",
            return_value=(),
        ) as readiness:
            run_doctor(
                config_path=root / "code-mower.yml",
                provider_templates_path=root / "src/code_mower/templates/providers.yml",
                profile="recommended",
                adoption=True,
            )

        self.assertEqual(
            readiness.call_args.kwargs["repo_slug"],
            "codemower-ai/code-mower",
        )

    def test_structured_result_capability_failure_emits_actionable_warn(self) -> None:
        """When command and auth pass but structured-result capability fails, emit actionable WARN."""
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "lanes": {
                    "codex": {
                        "provider_config": {
                            "campaign_adapter_argv": ["{command}", "qualify", "--output", "{output}"],
                            "campaign_adapter_timeout_seconds": 60,
                        }
                    }
                }
            }
            with mock.patch(
                "code_mower.campaign_adapters.check_structured_result_capability",
                return_value=False,
            ):
                checks = check_adoption_campaign_readiness(
                    config=config,
                    repo_root=Path(tmp),
                    env={"CODE_MOWER_CAMPAIGN_AUTH_PROBE": "0"},
                    which_fn=lambda cmd: f"/bin/{cmd}" if cmd == "codex" else None,
                    providers=["codex"],
                )

            # 1. doctor.campaign.adapter passed
            adapter_check = next(c for c in checks if c.name == "doctor.campaign.adapter")
            self.assertEqual(adapter_check.status, STATUS_PASS)

            # 2. doctor.campaign.structured_result emitted WARN
            structured_check = next(c for c in checks if c.name == "doctor.campaign.structured_result")
            self.assertEqual(structured_check.status, STATUS_WARN)
            self.assertEqual(structured_check.lane, "codex")
            self.assertTrue(structured_check.detail.get("actionable"))
            self.assertTrue(structured_check.detail.get("owner_action"))
            self.assertIn("capability probe failed", structured_check.message)
            self.assertIn("Verify codex campaign adapter output parsing", structured_check.remediation)

            # 3. Provider is in actionable_providers and NOT in ready_providers
            readiness = next(c for c in checks if c.name == "doctor.campaign.readiness")
            self.assertIn("codex", readiness.detail.get("actionable_providers", []))
            self.assertNotIn("codex", readiness.detail.get("ready_providers", []))
            self.assertEqual(readiness.status, STATUS_WARN)

    def test_structured_result_capability_failure_for_hosted_provider(self) -> None:
        """When hosted provider credentials pass but structured-result capability fails, emit actionable WARN."""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                "code_mower.campaign_adapters.check_structured_result_capability",
                return_value=False,
            ):
                checks = check_adoption_campaign_readiness(
                    config={"lanes": {"devin": {"enabled": True}}},
                    repo_root=Path(tmp),
                    env={
                        "DEVIN_AUDIT_LABEL_TOKEN": "dummy-token",
                        "CODE_MOWER_DEVIN_CAMPAIGN_TRANSPORT_READY": "1",
                    },
                    repo_slug="codemower-ai/code-mower",
                    providers=["devin"],
                )

            cred_check = next(c for c in checks if c.name == "doctor.campaign.credentials")
            self.assertEqual(cred_check.status, STATUS_PASS)

            structured_check = next(c for c in checks if c.name == "doctor.campaign.structured_result")
            self.assertEqual(structured_check.status, STATUS_WARN)
            self.assertEqual(structured_check.lane, "devin")
            self.assertTrue(structured_check.detail.get("actionable"))
            self.assertFalse(structured_check.detail.get("optional"))
            self.assertTrue(structured_check.detail.get("owner_action"))

            readiness = next(c for c in checks if c.name == "doctor.campaign.readiness")
            self.assertIn("devin", readiness.detail.get("actionable_providers", []))
            self.assertNotIn("devin", readiness.detail.get("ready_providers", []))
            self.assertEqual(readiness.status, STATUS_WARN)

    def test_structured_result_capability_failure_disabled_optional_local_cli_provider(self) -> None:
        """When disabled/optional local_cli provider fails structured-result probe, do not become actionable or flip aggregate readiness."""
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "lanes": {
                    "claude": {
                        "provider_config": {
                            "campaign_adapter_argv": ["{command}", "qualify", "--output", "{output}"],
                            "campaign_adapter_timeout_seconds": 60,
                        }
                    },
                    "codex": {
                        "enabled": False,
                        "provider_config": {
                            "campaign_adapter_argv": ["{command}", "qualify", "--output", "{output}"],
                            "campaign_adapter_timeout_seconds": 60,
                        },
                    },
                }
            }

            def fake_capability(provider: str) -> bool:
                return provider == "claude"

            with mock.patch(
                "code_mower.campaign_adapters.check_structured_result_capability",
                side_effect=fake_capability,
            ):
                checks = check_adoption_campaign_readiness(
                    config=config,
                    repo_root=Path(tmp),
                    env={"CODE_MOWER_CAMPAIGN_AUTH_PROBE": "0"},
                    which_fn=lambda cmd: f"/bin/{cmd}" if cmd in {"claude", "codex"} else None,
                    providers=["claude", "codex"],
                )

            claude_adapter = next(c for c in checks if c.name == "doctor.campaign.adapter" and c.lane == "claude")
            self.assertEqual(claude_adapter.status, STATUS_PASS)

            codex_adapter = next(c for c in checks if c.name == "doctor.campaign.adapter" and c.lane == "codex")
            self.assertEqual(codex_adapter.status, STATUS_PASS)

            structured_check = next(
                c for c in checks if c.name == "doctor.campaign.structured_result" and c.lane == "codex"
            )
            self.assertEqual(structured_check.status, STATUS_WARN)
            self.assertFalse(structured_check.detail.get("actionable"))
            self.assertTrue(structured_check.detail.get("optional"))
            self.assertNotIn("owner_action", structured_check.detail)

            readiness = next(c for c in checks if c.name == "doctor.campaign.readiness")
            self.assertEqual(readiness.status, STATUS_PASS)
            self.assertIn("claude", readiness.detail.get("ready_providers", []))
            self.assertNotIn("codex", readiness.detail.get("ready_providers", []))
            self.assertIn("codex", readiness.detail.get("optional_providers", []))
            self.assertNotIn("codex", readiness.detail.get("actionable_providers", []))

    def test_structured_result_capability_failure_disabled_by_default_local_cli_provider(self) -> None:
        """When disabled-by-default local_cli provider fails structured-result probe, do not become actionable or flip aggregate readiness."""
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "lanes": {
                    "claude": {
                        "provider_config": {
                            "campaign_adapter_argv": ["{command}", "qualify", "--output", "{output}"],
                            "campaign_adapter_timeout_seconds": 60,
                        }
                    },
                }
            }

            def fake_capability(provider: str) -> bool:
                return provider == "claude"

            with mock.patch(
                "code_mower.campaign_adapters.check_structured_result_capability",
                side_effect=fake_capability,
            ):
                checks = check_adoption_campaign_readiness(
                    config=config,
                    repo_root=Path(tmp),
                    which_fn=lambda cmd: f"/bin/{cmd}" if cmd in {"claude", "agy"} else None,
                    env={"ANTIGRAVITY_CLI_USE_AMBIENT_HOME": "1"},
                    providers=["claude", "antigravity"],
                )

            structured_check = next(
                c for c in checks if c.name == "doctor.campaign.structured_result" and c.lane == "antigravity"
            )
            self.assertEqual(structured_check.status, STATUS_WARN)
            self.assertFalse(structured_check.detail.get("actionable"))
            self.assertTrue(structured_check.detail.get("optional"))
            self.assertNotIn("owner_action", structured_check.detail)

            readiness = next(c for c in checks if c.name == "doctor.campaign.readiness")
            self.assertEqual(readiness.status, STATUS_PASS)
            self.assertIn("claude", readiness.detail.get("ready_providers", []))
            self.assertNotIn("antigravity", readiness.detail.get("ready_providers", []))
            self.assertIn("antigravity", readiness.detail.get("optional_providers", []))
            self.assertNotIn("antigravity", readiness.detail.get("actionable_providers", []))

    def test_structured_result_capability_failure_disabled_optional_hosted_provider(self) -> None:
        """When disabled/optional hosted provider fails structured-result probe, do not become actionable or flip aggregate readiness."""
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "lanes": {
                    "codex": {
                        "provider_config": {
                            "campaign_adapter_argv": ["{command}", "qualify", "--output", "{output}"],
                            "campaign_adapter_timeout_seconds": 60,
                        }
                    },
                }
            }

            def fake_capability(provider: str) -> bool:
                return provider == "codex"

            with mock.patch(
                "code_mower.campaign_adapters.check_structured_result_capability",
                side_effect=fake_capability,
            ):
                checks = check_adoption_campaign_readiness(
                    config=config,
                    repo_root=Path(tmp),
                    which_fn=lambda cmd: f"/bin/{cmd}" if cmd == "codex" else None,
                    env={
                        "CODE_MOWER_CAMPAIGN_AUTH_PROBE": "0",
                        "DEVIN_AUDIT_LABEL_TOKEN": "dummy-token",
                        "CODE_MOWER_DEVIN_CAMPAIGN_TRANSPORT_READY": "1",
                    },
                    repo_slug="codemower-ai/code-mower",
                    providers=["codex", "devin"],
                )

            cred_check = next(c for c in checks if c.name == "doctor.campaign.credentials" and c.lane == "devin")
            self.assertEqual(cred_check.status, STATUS_PASS)

            structured_check = next(
                c for c in checks if c.name == "doctor.campaign.structured_result" and c.lane == "devin"
            )
            self.assertEqual(structured_check.status, STATUS_WARN)
            self.assertFalse(structured_check.detail.get("actionable"))
            self.assertTrue(structured_check.detail.get("optional"))
            self.assertNotIn("owner_action", structured_check.detail)

            readiness = next(c for c in checks if c.name == "doctor.campaign.readiness")
            self.assertEqual(readiness.status, STATUS_PASS)
            self.assertIn("codex", readiness.detail.get("ready_providers", []))
            self.assertNotIn("devin", readiness.detail.get("ready_providers", []))
            self.assertIn("devin", readiness.detail.get("optional_providers", []))
            self.assertNotIn("devin", readiness.detail.get("actionable_providers", []))

    def test_structured_result_capability_failure_disabled_optional_saas_event_provider(self) -> None:
        """When disabled/optional saas_event hosted provider fails structured-result probe, do not become actionable or flip aggregate readiness."""
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "lanes": {
                    "codex": {
                        "provider_config": {
                            "campaign_adapter_argv": ["{command}", "qualify", "--output", "{output}"],
                            "campaign_adapter_timeout_seconds": 60,
                        }
                    },
                }
            }

            def fake_capability(provider: str) -> bool:
                return provider == "codex"

            with mock.patch(
                "code_mower.campaign_adapters.check_structured_result_capability",
                side_effect=fake_capability,
            ):
                checks = check_adoption_campaign_readiness(
                    config=config,
                    repo_root=Path(tmp),
                    which_fn=lambda cmd: f"/bin/{cmd}" if cmd == "codex" else None,
                    env={
                        "CODE_MOWER_CAMPAIGN_AUTH_PROBE": "0",
                        "CURSOR_BUGBOT_AUDIT_LABEL_TOKEN": "dummy-token",
                        "CODE_MOWER_CURSOR_BUGBOT_CAMPAIGN_TRANSPORT_READY": "1",
                    },
                    repo_slug="codemower-ai/code-mower",
                    providers=["codex", "cursor_bugbot"],
                )

            cred_check = next(c for c in checks if c.name == "doctor.campaign.credentials" and c.lane == "cursor_bugbot")
            self.assertEqual(cred_check.status, STATUS_PASS)

            structured_check = next(
                c for c in checks if c.name == "doctor.campaign.structured_result" and c.lane == "cursor_bugbot"
            )
            self.assertEqual(structured_check.status, STATUS_WARN)
            self.assertFalse(structured_check.detail.get("actionable"))
            self.assertTrue(structured_check.detail.get("optional"))
            self.assertNotIn("owner_action", structured_check.detail)

            readiness = next(c for c in checks if c.name == "doctor.campaign.readiness")
            self.assertEqual(readiness.status, STATUS_PASS)
            self.assertIn("codex", readiness.detail.get("ready_providers", []))
            self.assertNotIn("cursor_bugbot", readiness.detail.get("ready_providers", []))
            self.assertIn("cursor_bugbot", readiness.detail.get("optional_providers", []))
            self.assertNotIn("cursor_bugbot", readiness.detail.get("actionable_providers", []))

    def test_disabled_auth_probe_preserves_aggregate_readiness_for_ambient_providers(self) -> None:
        """Setting CODE_MOWER_CAMPAIGN_AUTH_PROBE=0 keeps Antigravity and Muse ready without opt-ins or warnings."""
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "lanes": {
                    "antigravity_cli": {
                        "provider_config": {
                            "campaign_adapter_argv": ["{command}", "qualify", "--output", "{output}"],
                            "campaign_adapter_timeout_seconds": 60,
                        }
                    },
                    "muse_cli": {
                        "provider_config": {
                            "campaign_adapter_argv": ["{command}", "qualify", "--output", "{output}"],
                            "campaign_adapter_timeout_seconds": 60,
                        }
                    },
                }
            }

            # 1. Probing enabled (default) without ambient opt-ins emits warnings and excludes both
            checks_probed = check_adoption_campaign_readiness(
                config=config,
                repo_root=Path(tmp),
                which_fn=lambda cmd: f"/bin/{cmd}" if cmd in {"agy", "muse"} else None,
                env={},
                providers=["antigravity", "muse"],
            )
            readiness_probed = next(c for c in checks_probed if c.name == "doctor.campaign.readiness")
            self.assertEqual(readiness_probed.status, STATUS_WARN)
            self.assertEqual(readiness_probed.detail.get("ready_providers"), [])
            self.assertIn("antigravity", readiness_probed.detail.get("actionable_providers", []))
            self.assertIn("muse", readiness_probed.detail.get("actionable_providers", []))

            # 2. Probing disabled via CODE_MOWER_CAMPAIGN_AUTH_PROBE=0 skips auth, no warnings, both ready
            checks_disabled = check_adoption_campaign_readiness(
                config=config,
                repo_root=Path(tmp),
                which_fn=lambda cmd: f"/bin/{cmd}" if cmd in {"agy", "muse"} else None,
                env={"CODE_MOWER_CAMPAIGN_AUTH_PROBE": "0"},
                providers=["antigravity", "muse"],
            )
            readiness_disabled = next(c for c in checks_disabled if c.name == "doctor.campaign.readiness")
            self.assertEqual(readiness_disabled.status, STATUS_PASS)
            self.assertEqual(readiness_disabled.detail.get("ready_providers"), ["antigravity", "muse"])
            self.assertEqual(readiness_disabled.detail.get("actionable_providers"), [])
            self.assertEqual(readiness_disabled.detail.get("optional_providers"), [])
            warn_checks = [
                c for c in checks_disabled if c.status == STATUS_WARN and c.lane in {"antigravity", "muse"}
            ]
            self.assertEqual(warn_checks, [])


if __name__ == "__main__":
    unittest.main()
