"""Unit and integration tests for doctor release campaign readiness checks."""

from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

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
                env={"DEVIN_AUDIT_LABEL_TOKEN": "secret-token"},
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


if __name__ == "__main__":
    unittest.main()
