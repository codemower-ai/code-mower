#!/usr/bin/env python3
"""Unit tests for safe provider credential resolution and profile discovery."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from code_mower import devin_api, provider_credentials


class ProviderCredentialsTests(unittest.TestCase):
    def test_ambient_environment_precedence(self) -> None:
        """Ambient environment variables take highest priority over files."""
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            cred_file = config_dir / "devin.env"
            cred_file.write_text(
                "DEVIN_API_KEY=file-token\n"
                "DEVIN_ORG_ID=org-from-file\n"
                "CODE_MOWER_DEVIN_REPOSITORIES=file/repo\n"
            )
            cred_file.chmod(0o600)

            env = {
                "DEVIN_API_KEY": "env-token",
                "DEVIN_ORG_ID": "org-from-env",
                "CODE_MOWER_DEVIN_REPOSITORIES": "env/repo",
            }

            res = provider_credentials.resolve_provider_credentials(
                "devin",
                config_dir=config_dir,
                env=env,
            )
            self.assertTrue(res.has_credentials)
            self.assertEqual(res.status, "ok")
            self.assertEqual(res.source, "env")
            self.assertEqual(res.credentials.get("DEVIN_API_KEY"), "env-token")
            self.assertEqual(res.credentials.get("DEVIN_ORG_ID"), "org-from-env")
            self.assertEqual(
                res.credentials.get("CODE_MOWER_DEVIN_REPOSITORIES"), "env/repo"
            )
            self.assertIsNone(res.profile_file)

    def test_explicit_credential_file_success(self) -> None:
        """Explicitly specified credential file is loaded when permissions are secure."""
        with tempfile.TemporaryDirectory() as tmp:
            cred_file = Path(tmp) / "custom.env"
            cred_file.write_text(
                "export DEVIN_API_KEY='custom-token'\n"
                "DEVIN_ORG_ID=\"org-custom\"\n"
                "CODE_MOWER_DEVIN_REPOSITORIES=custom/repo\n"
            )
            cred_file.chmod(0o600)

            res = provider_credentials.resolve_provider_credentials(
                "devin",
                credential_file=str(cred_file),
                env={},
            )
            self.assertTrue(res.has_credentials)
            self.assertEqual(res.status, "ok")
            self.assertEqual(res.source, "credential_file")
            self.assertEqual(res.credentials.get("DEVIN_API_KEY"), "custom-token")
            self.assertEqual(res.credentials.get("DEVIN_ORG_ID"), "org-custom")
            self.assertEqual(
                res.credentials.get("CODE_MOWER_DEVIN_REPOSITORIES"), "custom/repo"
            )
            self.assertEqual(res.candidate_files, ("custom.env",))
            safe_detail = res.safe_detail()
            self.assertEqual(safe_detail["status"], "ok")
            self.assertEqual(safe_detail["source"], "credential_file")
            self.assertNotIn("custom-token", str(safe_detail))

    def test_explicit_credential_file_missing(self) -> None:
        """Explicitly specified credential file fails closed when nonexistent."""
        with tempfile.TemporaryDirectory() as tmp:
            missing_path = Path(tmp) / "nonexistent.env"
            res = provider_credentials.resolve_provider_credentials(
                "devin",
                credential_file=str(missing_path),
                env={},
            )
            self.assertFalse(res.has_credentials)
            self.assertEqual(res.status, "missing")
            self.assertIn("nonexistent.env", res.message)

    def test_explicit_credential_file_insecure_permissions(self) -> None:
        """Explicit credential file fails closed when group/world readable."""
        with tempfile.TemporaryDirectory() as tmp:
            cred_file = Path(tmp) / "insecure.env"
            cred_file.write_text("DEVIN_API_KEY=token\nDEVIN_ORG_ID=org-test\n")
            cred_file.chmod(0o644)

            res = provider_credentials.resolve_provider_credentials(
                "devin",
                credential_file=str(cred_file),
                env={},
            )
            self.assertFalse(res.has_credentials)
            self.assertEqual(res.status, "insecure_permissions")
            self.assertIn("chmod 600", res.remediation)
            self.assertIn("insecure.env", res.message)

    def test_explicit_profile_name_success(self) -> None:
        """Explicit profile name resolves to matching profile file in config dir."""
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            staging_file = config_dir / "devin.staging.env"
            staging_file.write_text("DEVIN_API_KEY=stg-token\nDEVIN_ORG_ID=org-stg\n")
            staging_file.chmod(0o600)

            res = provider_credentials.resolve_provider_credentials(
                "devin",
                profile="staging",
                config_dir=config_dir,
                env={},
            )
            self.assertTrue(res.has_credentials)
            self.assertEqual(res.status, "ok")
            self.assertEqual(res.source, "profile")
            self.assertEqual(res.credentials.get("DEVIN_API_KEY"), "stg-token")
            self.assertEqual(res.credentials.get("DEVIN_ORG_ID"), "org-stg")

    def test_explicit_profile_name_missing(self) -> None:
        """Explicit profile name that does not exist fails closed."""
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            res = provider_credentials.resolve_provider_credentials(
                "devin",
                profile="nonexistent",
                config_dir=config_dir,
                env={},
            )
            self.assertFalse(res.has_credentials)
            self.assertEqual(res.status, "missing")
            self.assertIn("nonexistent", res.message)

    def test_auto_discovery_single_profile_success(self) -> None:
        """Single matching default profile file in config dir is safely discovered."""
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            devin_file = config_dir / "devin.env"
            devin_file.write_text("DEVIN_API_KEY=disc-token\nDEVIN_ORG_ID=org-disc\n")
            devin_file.chmod(0o600)

            res = provider_credentials.resolve_provider_credentials(
                "devin",
                config_dir=config_dir,
                env={},
            )
            self.assertTrue(res.has_credentials)
            self.assertEqual(res.status, "ok")
            self.assertEqual(res.source, "single_profile")
            self.assertEqual(res.credentials.get("DEVIN_API_KEY"), "disc-token")

    def test_auto_discovery_ambiguous_profiles_rejected(self) -> None:
        """Multiple candidate profiles without explicit selector fail closed."""
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            (config_dir / "devin.alpha.env").write_text("DEVIN_API_KEY=key_a\n")
            (config_dir / "devin.alpha.env").chmod(0o600)
            (config_dir / "devin.beta.env").write_text("DEVIN_API_KEY=key_b\n")
            (config_dir / "devin.beta.env").chmod(0o600)

            res = provider_credentials.resolve_provider_credentials(
                "devin",
                config_dir=config_dir,
                env={},
            )
            self.assertFalse(res.has_credentials)
            self.assertEqual(res.status, "ambiguous")
            self.assertEqual(len(res.candidate_files), 2)
            self.assertEqual(
                res.candidate_files, ("devin.alpha.env", "devin.beta.env")
            )
            self.assertIn("devin.alpha.env", res.message)
            self.assertIn("devin.beta.env", res.message)
            self.assertIn("--provider-profile", res.remediation)
            self.assertNotIn("key_a", res.message)
            self.assertNotIn("key_b", res.message)

    def test_auto_discovery_insecure_permissions(self) -> None:
        """Discovered profile with insecure permissions fails closed."""
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            devin_file = config_dir / "devin.env"
            devin_file.write_text("DEVIN_API_KEY=disc-token\n")
            devin_file.chmod(0o644)

            res = provider_credentials.resolve_provider_credentials(
                "devin",
                config_dir=config_dir,
                env={},
            )
            self.assertFalse(res.has_credentials)
            self.assertEqual(res.status, "insecure_permissions")
            self.assertIn("chmod 600", res.remediation)

    def test_auto_discovery_empty_dir_missing(self) -> None:
        """Empty or absent config dir returns missing status gracefully."""
        with tempfile.TemporaryDirectory() as tmp:
            empty_dir = Path(tmp) / "empty"
            res = provider_credentials.resolve_provider_credentials(
                "devin",
                config_dir=empty_dir,
                env={},
            )
            self.assertFalse(res.has_credentials)
            self.assertEqual(res.status, "missing")

    def test_parse_env_file_syntax(self) -> None:
        """Env file parsing correctly handles comments, quotes, exports, and whitespace."""
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "test.env"
            content = (
                "# Leading comment\n"
                "\n"
                "DEVIN_API_KEY=plain_value\n"
                'DEVIN_ORG_ID="double-quoted-org"\n'
                "export CODE_MOWER_DEVIN_REPOSITORIES='repo1,repo2'\n"
                "  SPACED_KEY = spaced_value  \n"
                "# Trailing comment with DEVIN_SECRET_SHOULD_NOT_LEAK=yes\n"
                "INVALID_LINE_NO_EQUALS\n"
                "=EMPTY_KEY\n"
            )
            env_file.write_text(content)
            parsed = provider_credentials.parse_env_file(env_file)
            self.assertEqual(parsed["DEVIN_API_KEY"], "plain_value")
            self.assertEqual(parsed["DEVIN_ORG_ID"], "double-quoted-org")
            self.assertEqual(
                parsed["CODE_MOWER_DEVIN_REPOSITORIES"], "repo1,repo2"
            )
            self.assertEqual(parsed["SPACED_KEY"], "spaced_value")
            self.assertNotIn("INVALID_LINE_NO_EQUALS", parsed)
            self.assertNotIn("", parsed)

    def test_display_profile_path_redaction(self) -> None:
        """display_profile_path replaces absolute directories with safe display prefix."""
        fake_home = Path("/home/fakeuser")
        with mock.patch("pathlib.Path.home", return_value=fake_home):
            home_file = fake_home / ".config" / "code-mower" / "devin.env"
            display = provider_credentials.display_profile_path(home_file)
            self.assertEqual(display, "~/.config/code-mower/devin.env")

            other_file = Path("/var/folders/xyz/temp_dir/devin.env")
            display_other = provider_credentials.display_profile_path(
                other_file, config_dir=other_file.parent
            )
            self.assertEqual(display_other, "~/.config/code-mower/devin.env")

    def test_devin_api_credentials_from_env_integration(self) -> None:
        """devin_api.credentials_from_env picks up credentials from config profile."""
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            profile_file = config_dir / "devin.env"
            profile_file.write_text(
                "DEVIN_API_KEY=integ-key\n"
                "DEVIN_ORG_ID=org-integ\n"
                "CODE_MOWER_DEVIN_REPOSITORIES=integ/repo\n"
            )
            profile_file.chmod(0o600)

            with mock.patch.dict(os.environ, {}, clear=True):
                api_key, org_id, missing = devin_api.credentials_from_env(
                    config_dir=config_dir
                )
                self.assertEqual(api_key, "integ-key")
                self.assertEqual(org_id, "org-integ")
                self.assertEqual(missing, "")

                ack = devin_api.repository_scope_acknowledged(
                    "integ/repo", config_dir=config_dir
                )
                self.assertTrue(ack)

                ack_unmatched = devin_api.repository_scope_acknowledged(
                    "other/repo", config_dir=config_dir
                )
                self.assertFalse(ack_unmatched)

    def test_permission_check_non_posix_bypasses(self) -> None:
        """On non-posix systems, permission checking does not reject files."""
        with tempfile.TemporaryDirectory() as tmp:
            cred_file = Path(tmp) / "test.env"
            cred_file.write_text("DEVIN_API_KEY=key\n")
            with mock.patch("os.name", "nt"):
                ok = provider_credentials.check_file_permissions(cred_file)
                self.assertTrue(ok)

    def test_provider_config_dir_environment_variable(self) -> None:
        """The documented CODE_MOWER_PROVIDER_CONFIG_DIR variable is honored consistently."""
        with tempfile.TemporaryDirectory() as tmp:
            custom_dir = Path(tmp) / "custom_config"
            custom_dir.mkdir()
            cred_file = custom_dir / "devin.env"
            cred_file.write_text("DEVIN_API_KEY=cfg-dir-token\nDEVIN_ORG_ID=org-cfg-dir\n")
            cred_file.chmod(0o600)

            # 1. CODE_MOWER_PROVIDER_CONFIG_DIR works
            res = provider_credentials.resolve_provider_credentials(
                "devin",
                env={"CODE_MOWER_PROVIDER_CONFIG_DIR": str(custom_dir)},
            )
            self.assertTrue(res.has_credentials)
            self.assertEqual(res.status, "ok")
            self.assertEqual(res.credentials.get("DEVIN_API_KEY"), "cfg-dir-token")
            self.assertEqual(res.credentials.get("DEVIN_ORG_ID"), "org-cfg-dir")

            # 2. Legacy alias CODE_MOWER_CONFIG_DIR works as fallback
            res_legacy = provider_credentials.resolve_provider_credentials(
                "devin",
                env={"CODE_MOWER_CONFIG_DIR": str(custom_dir)},
            )
            self.assertTrue(res_legacy.has_credentials)
            self.assertEqual(res_legacy.status, "ok")
            self.assertEqual(res_legacy.credentials.get("DEVIN_API_KEY"), "cfg-dir-token")

            # 3. CODE_MOWER_PROVIDER_CONFIG_DIR takes precedence over CODE_MOWER_CONFIG_DIR
            other_dir = Path(tmp) / "other_config"
            other_dir.mkdir()
            other_file = other_dir / "devin.env"
            other_file.write_text("DEVIN_API_KEY=other-token\nDEVIN_ORG_ID=org-other\n")
            other_file.chmod(0o600)

            res_precedence = provider_credentials.resolve_provider_credentials(
                "devin",
                env={
                    "CODE_MOWER_PROVIDER_CONFIG_DIR": str(custom_dir),
                    "CODE_MOWER_CONFIG_DIR": str(other_dir),
                },
            )
            self.assertEqual(res_precedence.credentials.get("DEVIN_API_KEY"), "cfg-dir-token")

    def test_extra_profile_keys_cannot_enter_resolved_environment(self) -> None:
        """Loaded profile values are restricted strictly to provider spec required/optional keys."""
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            profile_file = config_dir / "devin.env"
            profile_file.write_text(
                "DEVIN_API_KEY=token-from-file\n"
                "DEVIN_ORG_ID=org-spec-test\n"
                "CODE_MOWER_DEVIN_REPOSITORIES=spec/repo\n"
                "GITHUB_TOKEN=ghp_dummy\n"
                "ANTHROPIC_API_KEY=sk-dummy\n"
                "CODE_MOWER_STATE_DIR=/unwanted/path\n"
            )
            profile_file.chmod(0o600)

            res = provider_credentials.resolve_provider_credentials(
                "devin",
                config_dir=config_dir,
                env={},
            )
            self.assertTrue(res.has_credentials)
            self.assertEqual(res.status, "ok")
            self.assertEqual(res.credentials.get("DEVIN_API_KEY"), "token-from-file")
            self.assertEqual(res.credentials.get("DEVIN_ORG_ID"), "org-spec-test")
            self.assertEqual(
                res.credentials.get("CODE_MOWER_DEVIN_REPOSITORIES"), "spec/repo"
            )
            # Unrelated keys must NOT be present in credentials
            self.assertNotIn("GITHUB_TOKEN", res.credentials)
            self.assertNotIn("ANTHROPIC_API_KEY", res.credentials)
            self.assertNotIn("CODE_MOWER_STATE_DIR", res.credentials)

            # Unrelated keys must NOT enter applied environment
            applied = res.apply_to_env({})
            self.assertEqual(applied.get("DEVIN_API_KEY"), "token-from-file")
            self.assertEqual(applied.get("DEVIN_ORG_ID"), "org-spec-test")
            self.assertNotIn("GITHUB_TOKEN", applied)
            self.assertNotIn("ANTHROPIC_API_KEY", applied)
            self.assertNotIn("CODE_MOWER_STATE_DIR", applied)

    def test_partial_ambient_credentials_do_not_mix_with_disk_credentials(self) -> None:
        """If any required credential is in ambient env but set is incomplete/invalid, fail closed without mixing."""
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            profile_file = config_dir / "devin.env"
            profile_file.write_text(
                "DEVIN_API_KEY=disk-token\n"
                "DEVIN_ORG_ID=org-from-disk\n"
                "CODE_MOWER_DEVIN_REPOSITORIES=disk/repo\n"
            )
            profile_file.chmod(0o600)

            # Case 1: DEVIN_API_KEY set ambiently, DEVIN_ORG_ID missing -> fails closed, no disk mix
            res_missing_org = provider_credentials.resolve_provider_credentials(
                "devin",
                config_dir=config_dir,
                env={"DEVIN_API_KEY": "ambient-token"},
            )
            self.assertFalse(res_missing_org.has_credentials)
            self.assertEqual(res_missing_org.status, "missing")
            self.assertEqual(res_missing_org.source, "env")
            self.assertEqual(res_missing_org.missing_variables, ("DEVIN_ORG_ID",))
            self.assertIn("DEVIN_ORG_ID is not set", res_missing_org.message)
            self.assertIn("unset ambient Devin variables", res_missing_org.remediation)
            self.assertEqual(dict(res_missing_org.credentials), {})
            self.assertNotIn("org-from-disk", str(res_missing_org.credentials))
            self.assertNotIn("disk-token", str(res_missing_org.credentials))

            # Case 2: DEVIN_ORG_ID set ambiently, DEVIN_API_KEY missing -> fails closed, no disk mix
            res_missing_key = provider_credentials.resolve_provider_credentials(
                "devin",
                config_dir=config_dir,
                env={"DEVIN_ORG_ID": "org-ambient"},
            )
            self.assertFalse(res_missing_key.has_credentials)
            self.assertEqual(res_missing_key.status, "missing")
            self.assertEqual(res_missing_key.source, "env")
            self.assertEqual(res_missing_key.missing_variables, ("DEVIN_API_KEY",))
            self.assertIn("DEVIN_API_KEY is not set", res_missing_key.message)
            self.assertEqual(dict(res_missing_key.credentials), {})

            # Case 3: DEVIN_API_KEY set ambiently, DEVIN_ORG_ID invalid -> fails closed with malformed
            res_invalid_org = provider_credentials.resolve_provider_credentials(
                "devin",
                config_dir=config_dir,
                env={"DEVIN_API_KEY": "ambient-token", "DEVIN_ORG_ID": "not-an-org"},
            )
            self.assertFalse(res_invalid_org.has_credentials)
            self.assertEqual(res_invalid_org.status, "malformed")
            self.assertEqual(res_invalid_org.source, "env")
            self.assertEqual(res_invalid_org.missing_variables, ("DEVIN_ORG_ID",))
            self.assertIn("DEVIN_ORG_ID is invalid", res_invalid_org.message)
            self.assertEqual(dict(res_invalid_org.credentials), {})

            # Case 4: devin_api.credentials_from_env integration with partial ambient env
            key, org, missing = devin_api.credentials_from_env(
                env={"DEVIN_API_KEY": "ambient-token"},
                config_dir=config_dir,
            )
            self.assertEqual(key, "")
            self.assertEqual(org, "")
            self.assertEqual(missing, "DEVIN_ORG_ID")

    def test_devin_org_id_strict_validation_agreement(self) -> None:
        """DEVIN_ORG_ID validation is identical between provider_credentials and devin_api dispatch."""
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            profile_file = config_dir / "devin.env"
            profile_file.write_text(
                "DEVIN_API_KEY=file-token\nDEVIN_ORG_ID=org-bad/path\n"
            )
            profile_file.chmod(0o600)

            # 1. Invalid org_id in stored profile fails closed and identifies DEVIN_ORG_ID
            res_stored = provider_credentials.resolve_provider_credentials(
                "devin",
                config_dir=config_dir,
                env={},
            )
            self.assertFalse(res_stored.has_credentials)
            self.assertEqual(res_stored.status, "malformed")
            self.assertEqual(res_stored.missing_variables, ("DEVIN_ORG_ID",))
            self.assertIn("DEVIN_ORG_ID", res_stored.message)

            key, org, missing = devin_api.credentials_from_env(
                config_dir=config_dir,
                env={},
            )
            self.assertEqual(key, "")
            self.assertEqual(org, "")
            self.assertEqual(missing, "DEVIN_ORG_ID")

            # 2. Invalid org_id in ambient env fails closed and identifies DEVIN_ORG_ID
            res_ambient = provider_credentials.resolve_provider_credentials(
                "devin",
                config_dir=config_dir,
                env={"DEVIN_API_KEY": "sk-token", "DEVIN_ORG_ID": "org-bad/path"},
            )
            self.assertFalse(res_ambient.has_credentials)
            self.assertEqual(res_ambient.status, "malformed")
            self.assertEqual(res_ambient.missing_variables, ("DEVIN_ORG_ID",))
            self.assertIn("DEVIN_ORG_ID is invalid", res_ambient.message)

            key_amb, org_amb, missing_amb = devin_api.credentials_from_env(
                config_dir=config_dir,
                env={"DEVIN_API_KEY": "sk-token", "DEVIN_ORG_ID": "org-bad/path"},
            )
            self.assertEqual(key_amb, "")
            self.assertEqual(org_amb, "")
            self.assertEqual(missing_amb, "DEVIN_ORG_ID")

            # 3. Valid org_id with allowed chars passes in both
            res_valid = provider_credentials.resolve_provider_credentials(
                "devin",
                config_dir=config_dir,
                env={"DEVIN_API_KEY": "sk-token", "DEVIN_ORG_ID": "org-Valid_123-abc"},
            )
            self.assertTrue(res_valid.has_credentials)
            self.assertEqual(res_valid.status, "ok")
            key_ok, org_ok, missing_ok = devin_api.credentials_from_env(
                config_dir=config_dir,
                env={"DEVIN_API_KEY": "sk-token", "DEVIN_ORG_ID": "org-Valid_123-abc"},
            )
            self.assertEqual(key_ok, "sk-token")
            self.assertEqual(org_ok, "org-Valid_123-abc")
            self.assertEqual(missing_ok, "")

    def test_file_read_oserror_redaction(self) -> None:
        """OSError during profile read produces a bounded diagnostic without leaking local path."""
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            cred_file = config_dir / "devin.env"
            cred_file.write_text("DEVIN_API_KEY=key\nDEVIN_ORG_ID=org-123\n")
            cred_file.chmod(0o600)

            # Simulate an OSError from read_text whose str(exc) includes the absolute path
            fake_oserror = PermissionError(13, "Permission denied", str(cred_file))
            with mock.patch.object(Path, "read_text", side_effect=fake_oserror):
                res = provider_credentials.resolve_provider_credentials(
                    "devin",
                    credential_file=cred_file,
                    env={},
                )
                self.assertFalse(res.has_credentials)
                self.assertEqual(res.status, "malformed")
                # Bounded diagnostic must not contain raw absolute filesystem path
                self.assertNotIn(str(cred_file), res.message)
                self.assertNotIn(str(config_dir), res.message)
                self.assertIn("unable to read file", res.message)
                self.assertIn("Permission denied", res.message)

                # Remediation and safe_detail must use display_profile_path, not raw path
                self.assertNotIn(str(config_dir), res.remediation)
                safe = res.safe_detail()
                self.assertNotIn(str(config_dir), str(safe))

    def test_repository_aliases_conflicting_ambient_precedence(self) -> None:
        """Aliases are normalized so ambient repository scope overrides stored profiles consistently."""
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)

            # Case 1: Conflicting alias - ambient uses DEVIN_REPOSITORIES, stored uses CODE_MOWER_DEVIN_REPOSITORIES
            cred_file1 = config_dir / "devin.env"
            cred_file1.write_text(
                "DEVIN_API_KEY=key-1\n"
                "DEVIN_ORG_ID=org-1\n"
                "CODE_MOWER_DEVIN_REPOSITORIES=disk/repo\n"
            )
            cred_file1.chmod(0o600)

            ambient_env = {"DEVIN_REPOSITORIES": "ambient/repo"}
            res1 = provider_credentials.resolve_provider_credentials(
                "devin",
                config_dir=config_dir,
                env=ambient_env,
            )
            self.assertTrue(res1.has_credentials)
            # Ambient repository scope wins across both aliases
            self.assertEqual(
                res1.credentials.get("CODE_MOWER_DEVIN_REPOSITORIES"), "ambient/repo"
            )
            self.assertEqual(
                res1.credentials.get("DEVIN_REPOSITORIES"), "ambient/repo"
            )

            # Applied environment preserves ambient repository scope on both aliases
            applied1 = res1.apply_to_env(ambient_env)
            self.assertEqual(
                applied1.get("CODE_MOWER_DEVIN_REPOSITORIES"), "ambient/repo"
            )
            self.assertEqual(
                applied1.get("DEVIN_REPOSITORIES"), "ambient/repo"
            )

            # Devin repository scope acknowledgement accepts ambient and rejects disk
            self.assertTrue(
                devin_api.repository_scope_acknowledged(
                    "ambient/repo",
                    env=applied1,
                )
            )
            self.assertFalse(
                devin_api.repository_scope_acknowledged(
                    "disk/repo",
                    env=applied1,
                )
            )

            # Case 2: Reverse conflict - ambient uses CODE_MOWER_DEVIN_REPOSITORIES, stored uses DEVIN_REPOSITORIES
            cred_file2 = config_dir / "devin.staging.env"
            cred_file2.write_text(
                "DEVIN_API_KEY=key-2\n"
                "DEVIN_ORG_ID=org-2\n"
                "DEVIN_REPOSITORIES=disk/other\n"
            )
            cred_file2.chmod(0o600)

            ambient_env2 = {"CODE_MOWER_DEVIN_REPOSITORIES": "ambient/primary"}
            res2 = provider_credentials.resolve_provider_credentials(
                "devin",
                profile="staging",
                config_dir=config_dir,
                env=ambient_env2,
            )
            self.assertTrue(res2.has_credentials)
            self.assertEqual(
                res2.credentials.get("CODE_MOWER_DEVIN_REPOSITORIES"), "ambient/primary"
            )
            self.assertEqual(
                res2.credentials.get("DEVIN_REPOSITORIES"), "ambient/primary"
            )

            applied2 = res2.apply_to_env(ambient_env2)
            self.assertTrue(
                devin_api.repository_scope_acknowledged(
                    "ambient/primary",
                    env=applied2,
                )
            )
            self.assertFalse(
                devin_api.repository_scope_acknowledged(
                    "disk/other",
                    env=applied2,
                )
            )

            # Case 3: Stored profile alias normalized when ambient has no repository scope
            cred_file3 = config_dir / "devin.stored_alias.env"
            cred_file3.write_text(
                "DEVIN_API_KEY=key-3\n"
                "DEVIN_ORG_ID=org-3\n"
                "DEVIN_REPOSITORIES=stored/alias-repo\n"
            )
            cred_file3.chmod(0o600)

            res3 = provider_credentials.resolve_provider_credentials(
                "devin",
                profile="stored_alias",
                config_dir=config_dir,
                env={},
            )
            self.assertTrue(res3.has_credentials)
            self.assertEqual(
                res3.credentials.get("CODE_MOWER_DEVIN_REPOSITORIES"), "stored/alias-repo"
            )
            self.assertEqual(
                res3.credentials.get("DEVIN_REPOSITORIES"), "stored/alias-repo"
            )
            applied3 = res3.apply_to_env({})
            self.assertTrue(
                devin_api.repository_scope_acknowledged(
                    "stored/alias-repo",
                    env=applied3,
                )
            )

    def test_empty_required_ambient_credentials_fail_closed_with_stored_profile_present(self) -> None:
        """Explicitly empty ambient credential variables fail closed and do not fall back to stored profiles."""
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            cred_file = config_dir / "devin.env"
            cred_file.write_text(
                "DEVIN_API_KEY=stored-secret-key\n"
                "DEVIN_ORG_ID=org-stored\n"
                "CODE_MOWER_DEVIN_REPOSITORIES=stored/repo\n"
            )
            cred_file.chmod(0o600)

            # Case 1: DEVIN_API_KEY explicitly empty string, DEVIN_ORG_ID unset
            res1 = provider_credentials.resolve_provider_credentials(
                "devin",
                config_dir=config_dir,
                env={"DEVIN_API_KEY": ""},
            )
            self.assertFalse(res1.has_credentials)
            self.assertEqual(res1.status, "missing")
            self.assertEqual(res1.source, "env")
            self.assertEqual(res1.missing_variables, ("DEVIN_API_KEY", "DEVIN_ORG_ID"))
            self.assertIn("DEVIN_API_KEY is not set", res1.message)
            self.assertIn("unset ambient Devin variables", res1.remediation)
            self.assertEqual(dict(res1.credentials), {})
            self.assertNotIn("stored-secret-key", str(res1.credentials))

            key1, org1, missing1 = devin_api.credentials_from_env(
                config_dir=config_dir,
                env={"DEVIN_API_KEY": ""},
            )
            self.assertEqual(key1, "")
            self.assertEqual(org1, "")
            self.assertEqual(missing1, "DEVIN_API_KEY")

            # Case 2: DEVIN_ORG_ID explicitly empty string, DEVIN_API_KEY unset
            res2 = provider_credentials.resolve_provider_credentials(
                "devin",
                config_dir=config_dir,
                env={"DEVIN_ORG_ID": ""},
            )
            self.assertFalse(res2.has_credentials)
            self.assertEqual(res2.status, "missing")
            self.assertEqual(res2.source, "env")
            self.assertEqual(dict(res2.credentials), {})
            self.assertNotIn("stored-secret-key", str(res2.credentials))

            # Case 3: Both DEVIN_API_KEY and DEVIN_ORG_ID explicitly empty strings
            res3 = provider_credentials.resolve_provider_credentials(
                "devin",
                config_dir=config_dir,
                env={"DEVIN_API_KEY": "", "DEVIN_ORG_ID": ""},
            )
            self.assertFalse(res3.has_credentials)
            self.assertEqual(res3.status, "missing")
            self.assertEqual(res3.source, "env")
            self.assertEqual(res3.missing_variables, ("DEVIN_API_KEY", "DEVIN_ORG_ID"))
            self.assertIn("DEVIN_API_KEY is not set", res3.message)
            self.assertEqual(dict(res3.credentials), {})

            # Case 4: DEVIN_API_KEY empty string with valid ambient DEVIN_ORG_ID
            res4 = provider_credentials.resolve_provider_credentials(
                "devin",
                config_dir=config_dir,
                env={"DEVIN_API_KEY": "", "DEVIN_ORG_ID": "org-ambient"},
            )
            self.assertFalse(res4.has_credentials)
            self.assertEqual(res4.status, "missing")
            self.assertEqual(res4.source, "env")
            self.assertEqual(res4.missing_variables, ("DEVIN_API_KEY",))
            self.assertIn("DEVIN_API_KEY is not set", res4.message)
            self.assertEqual(dict(res4.credentials), {})

            # Case 5: Whitespace-only DEVIN_API_KEY
            res5 = provider_credentials.resolve_provider_credentials(
                "devin",
                config_dir=config_dir,
                env={"DEVIN_API_KEY": "   ", "DEVIN_ORG_ID": "org-ambient"},
            )
            self.assertFalse(res5.has_credentials)
            self.assertEqual(res5.status, "missing")
            self.assertEqual(res5.source, "env")
            self.assertEqual(res5.missing_variables, ("DEVIN_API_KEY",))
            self.assertIn("DEVIN_API_KEY is not set", res5.message)

    def test_parse_env_file_unquoted_inline_comments(self) -> None:
        """Unquoted inline comments are stripped while hash characters inside quotes are preserved."""
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "comments.env"
            content = (
                "DEVIN_ORG_ID=org-example # production\n"
                'QUOTED_DOUBLE="org-example # production"\n'
                "QUOTED_SINGLE='org-example # production'\n"
                'AFTER_DOUBLE="org-example" # production\n'
                "AFTER_SINGLE='org-example' # production\n"
                "EMBEDDED_HASH=token#123 # inline comment\n"
                'MULTI_HASH="sk#1#2" # comment with # inside\n'
                "export EXPORT_KEY=export-val # export comment\n"
                "SPACED_KEY=spaced-val   # multi spaces\n"
                "TABBED_KEY=tabbed-val\t# tab separated\n"
                "EMPTY_WITH_COMMENT= # comment only\n"
                "LITERAL_HASH_NO_SPACE=token#nohashcomment\n"
            )
            env_file.write_text(content)
            parsed = provider_credentials.parse_env_file(env_file)
            self.assertEqual(parsed["DEVIN_ORG_ID"], "org-example")
            self.assertEqual(parsed["QUOTED_DOUBLE"], "org-example # production")
            self.assertEqual(parsed["QUOTED_SINGLE"], "org-example # production")
            self.assertEqual(parsed["AFTER_DOUBLE"], "org-example")
            self.assertEqual(parsed["AFTER_SINGLE"], "org-example")
            self.assertEqual(parsed["EMBEDDED_HASH"], "token#123")
            self.assertEqual(parsed["MULTI_HASH"], "sk#1#2")
            self.assertEqual(parsed["EXPORT_KEY"], "export-val")
            self.assertEqual(parsed["SPACED_KEY"], "spaced-val")
            self.assertEqual(parsed["TABBED_KEY"], "tabbed-val")
            self.assertEqual(parsed["EMPTY_WITH_COMMENT"], "")
            self.assertEqual(parsed["LITERAL_HASH_NO_SPACE"], "token#nohashcomment")

    def test_resolver_with_inline_comments_in_profile(self) -> None:
        """Provider credential resolver succeeds when profile contains inline comments."""
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            cred_file = config_dir / "devin.env"
            cred_file.write_text(
                'DEVIN_API_KEY="secret#api#key" # active production key\n'
                "DEVIN_ORG_ID=org-example # production\n"
                "CODE_MOWER_DEVIN_REPOSITORIES=myorg/myrepo # primary repo\n"
            )
            cred_file.chmod(0o600)

            res = provider_credentials.resolve_provider_credentials(
                "devin",
                config_dir=config_dir,
                env={},
            )
            self.assertTrue(res.has_credentials)
            self.assertEqual(res.status, "ok")
            self.assertEqual(res.source, "single_profile")
            self.assertEqual(res.credentials.get("DEVIN_API_KEY"), "secret#api#key")
            self.assertEqual(res.credentials.get("DEVIN_ORG_ID"), "org-example")
            self.assertEqual(
                res.credentials.get("CODE_MOWER_DEVIN_REPOSITORIES"), "myorg/myrepo"
            )

            key, org, missing = devin_api.credentials_from_env(
                config_dir=config_dir,
                env={},
            )
            self.assertEqual(key, "secret#api#key")
            self.assertEqual(org, "org-example")
            self.assertEqual(missing, "")
            self.assertTrue(
                devin_api.repository_scope_acknowledged(
                    "myorg/myrepo",
                    config_dir=config_dir,
                    env={},
                )
            )


if __name__ == "__main__":
    unittest.main()
