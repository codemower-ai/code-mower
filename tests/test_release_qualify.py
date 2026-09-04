#!/usr/bin/env python3
"""Tests for release qualification and adoption result schema."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from code_mower import release_qualify


class ReleaseQualifyTests(unittest.TestCase):
    """Tests for release qualification command."""

    def test_adoption_result_schema_is_versioned(self) -> None:
        """Verify adoption result schema has stable version."""
        self.assertEqual(
            release_qualify.ADOPTION_RESULT_SCHEMA_VERSION,
            "code_mower.adoptionResult.v1",
        )

    def test_normalize_version_extracts_semantic_version(self) -> None:
        """Test version normalization from various string formats."""
        cases = [
            ("1.0.0", "1.0.0"),
            ("code-mower 1.0.0", "1.0.0"),
            ("1.0.0a1", "1.0.0a1"),
            ("1.0.0b2", "1.0.0b2"),
            ("1.0.0rc3", "1.0.0rc3"),
            ("v1.0.0", "1.0.0"),
        ]
        for input_str, expected in cases:
            with self.subTest(input=input_str):
                self.assertEqual(
                    release_qualify._normalize_version(input_str),
                    expected,
                )

    def test_detect_install_mode_cold_install(self) -> None:
        """Cold install when no starting version."""
        self.assertEqual(
            release_qualify._detect_install_mode(""),
            "cold_install",
        )

    def test_detect_install_mode_upgrade(self) -> None:
        """Upgrade when starting version exists."""
        self.assertEqual(
            release_qualify._detect_install_mode("1.0.0"),
            "upgrade",
        )

    def test_validate_tag_package_match_success(self) -> None:
        """Matching tag and package spec validates."""
        valid, error = release_qualify._validate_tag_package_match(
            "v1.0.0",
            "code-mower==1.0.0",
        )
        self.assertTrue(valid)
        self.assertEqual(error, "")

    def test_validate_tag_package_match_alpha(self) -> None:
        """Alpha versions validate correctly."""
        valid, error = release_qualify._validate_tag_package_match(
            "v1.0.0-alpha.1",
            "code-mower==1.0.0a1",
        )
        self.assertTrue(valid)
        self.assertEqual(error, "")

    def test_validate_tag_package_match_beta(self) -> None:
        """Beta versions validate correctly."""
        valid, error = release_qualify._validate_tag_package_match(
            "v1.0.0-beta.2",
            "code-mower==1.0.0b2",
        )
        self.assertTrue(valid)
        self.assertEqual(error, "")

    def test_validate_tag_package_match_rc(self) -> None:
        """RC versions validate correctly."""
        valid, error = release_qualify._validate_tag_package_match(
            "v1.0.0-rc.1",
            "code-mower==1.0.0rc1",
        )
        self.assertTrue(valid)
        self.assertEqual(error, "")

    def test_validate_tag_package_match_mismatch(self) -> None:
        """Mismatched versions fail validation."""
        valid, error = release_qualify._validate_tag_package_match(
            "v1.0.0",
            "code-mower==1.0.1",
        )
        self.assertFalse(valid)
        self.assertIn("mismatch", error.lower())

    def test_validate_tag_package_match_invalid_tag(self) -> None:
        """Invalid tag format fails validation."""
        valid, error = release_qualify._validate_tag_package_match(
            "invalid-tag",
            "code-mower==1.0.0",
        )
        self.assertFalse(valid)
        self.assertIn("not recognized", error.lower())

    def test_validate_tag_package_match_missing_exact_version(self) -> None:
        """Package spec without exact version fails validation."""
        valid, error = release_qualify._validate_tag_package_match(
            "v1.0.0",
            "code-mower",
        )
        self.assertFalse(valid)
        self.assertIn("missing exact version", error.lower())

    def test_validate_tag_package_match_git_url_allowed(self) -> None:
        """Git URL specs pass validation without version check."""
        valid, error = release_qualify._validate_tag_package_match(
            "v1.0.0",
            "git+https://github.com/example/repo.git@v1.0.0",
        )
        self.assertTrue(valid)
        self.assertEqual(error, "")

    def test_adoption_result_to_dict_redacts_paths(self) -> None:
        """AdoptionResult.to_dict() removes local paths."""
        result = release_qualify.AdoptionResult(
            schema_version="code_mower.adoptionResult.v1",
            timestamp_utc="2024-01-01T00:00:00Z",
            release_tag="v1.0.0",
            package_spec="code-mower==1.0.0",
            normalized_version="1.0.0",
            install_mode="cold_install",
            starting_version="",
            ending_version="1.0.0",
            provider="local_cli",
            executor="test",
            host_class="local",
            runtime_class="python_3.12",
            elapsed_seconds=10.5,
            outcome="pass",
            steps=[
                {
                    "step": "package_install",
                    "status": "completed",
                    "elapsed_seconds": 5.0,
                    "output": "sensitive output with /local/path",
                }
            ],
            warnings=[],
            owner_actions=[],
        )

        result_dict = result.to_dict()

        # Verify steps are sanitized
        self.assertEqual(len(result_dict["steps"]), 1)
        step = result_dict["steps"][0]
        self.assertEqual(step["step"], "package_install")
        self.assertEqual(step["status"], "completed")
        self.assertNotIn("output", step)
        self.assertNotIn("/local/path", json.dumps(result_dict))

    def test_adoption_result_schema_deterministic(self) -> None:
        """AdoptionResult dict representation is deterministic."""
        result1 = release_qualify.AdoptionResult(
            schema_version="code_mower.adoptionResult.v1",
            timestamp_utc="2024-01-01T00:00:00Z",
            release_tag="v1.0.0",
            package_spec="code-mower==1.0.0",
            normalized_version="1.0.0",
            install_mode="cold_install",
            starting_version="",
            ending_version="1.0.0",
            provider="local_cli",
            executor="test",
            host_class="local",
            runtime_class="python_3.12",
            elapsed_seconds=10.0,
            outcome="pass",
            steps=[],
            warnings=["warning1"],
            owner_actions=[],
        )
        result2 = release_qualify.AdoptionResult(
            schema_version="code_mower.adoptionResult.v1",
            timestamp_utc="2024-01-01T00:00:00Z",
            release_tag="v1.0.0",
            package_spec="code-mower==1.0.0",
            normalized_version="1.0.0",
            install_mode="cold_install",
            starting_version="",
            ending_version="1.0.0",
            provider="local_cli",
            executor="test",
            host_class="local",
            runtime_class="python_3.12",
            elapsed_seconds=10.0,
            outcome="pass",
            steps=[],
            warnings=["warning1"],
            owner_actions=[],
        )

        json1 = json.dumps(result1.to_dict(), sort_keys=True)
        json2 = json.dumps(result2.to_dict(), sort_keys=True)
        self.assertEqual(json1, json2)

    def test_run_release_qualification_dry_run(self) -> None:
        """Dry run performs no installation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "result.json"

            with mock.patch("code_mower.release_qualify._detect_starting_version") as mock_ver:
                mock_ver.return_value = ""

                result = release_qualify.run_release_qualification(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    output_path=output_path,
                    dry_run=True,
                    provider="local_cli",
                    executor="test",
                )

            self.assertEqual(result["schema_version"], "code_mower.adoptionResult.v1")
            self.assertEqual(result["release_tag"], "v1.0.0")
            self.assertEqual(result["install_mode"], "cold_install")
            self.assertIn("pass", result["outcome"])

            # Verify JSON was written
            self.assertTrue(output_path.exists())
            with output_path.open() as f:
                saved = json.load(f)
            self.assertEqual(saved["release_tag"], "v1.0.0")

            # Verify dry run skipped install
            install_step = next(
                (s for s in result["steps"] if s["step"] == "package_install"),
                None,
            )
            self.assertIsNotNone(install_step)
            self.assertEqual(install_step["status"], "skipped_dry_run")

    def test_run_release_qualification_tag_mismatch_fails(self) -> None:
        """Mismatched tag/package fails before execution."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "result.json"

            with self.assertRaises(ValueError) as ctx:
                release_qualify.run_release_qualification(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.1",
                    output_path=output_path,
                    dry_run=True,
                )

            self.assertIn("mismatch", str(ctx.exception).lower())
            # No output file should be created
            self.assertFalse(output_path.exists())

    def test_adoption_result_no_secrets_in_output(self) -> None:
        """Adoption result never contains secrets or raw command output."""
        result = release_qualify.AdoptionResult(
            schema_version="code_mower.adoptionResult.v1",
            timestamp_utc="2024-01-01T00:00:00Z",
            release_tag="v1.0.0",
            package_spec="code-mower==1.0.0",
            normalized_version="1.0.0",
            install_mode="cold_install",
            starting_version="",
            ending_version="1.0.0",
            provider="local_cli",
            executor="test",
            host_class="github_actions",
            runtime_class="python_3.12",
            elapsed_seconds=10.0,
            outcome="pass",
            steps=[
                {
                    "step": "doctor",
                    "status": "completed",
                    "elapsed_seconds": 2.0,
                    "secret_token": "ghp_secret123",
                }
            ],
            warnings=[],
            owner_actions=[],
        )

        result_dict = result.to_dict()
        result_json = json.dumps(result_dict)

        # Verify no secrets leak
        self.assertNotIn("secret_token", result_json)
        self.assertNotIn("ghp_", result_json)

    def test_main_qualify_command_json_output(self) -> None:
        """Main function supports JSON output mode."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "result.json"

            with mock.patch("code_mower.release_qualify._detect_starting_version") as mock_ver:
                mock_ver.return_value = ""

                exit_code = release_qualify.main([
                    "qualify",
                    "--release-tag", "v1.0.0",
                    "--package-spec", "code-mower==1.0.0",
                    "--output", str(output_path),
                    "--json",
                ])

            self.assertEqual(exit_code, 0)

    def test_main_qualify_command_text_output(self) -> None:
        """Main function supports text output mode."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "result.json"

            with mock.patch("code_mower.release_qualify._detect_starting_version") as mock_ver:
                mock_ver.return_value = ""

                exit_code = release_qualify.main([
                    "qualify",
                    "--release-tag", "v1.0.0",
                    "--package-spec", "code-mower==1.0.0",
                    "--output", str(output_path),
                ])

            self.assertEqual(exit_code, 0)

    def test_detect_host_class_github_actions(self) -> None:
        """GitHub Actions environment detected correctly."""
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            self.assertEqual(release_qualify._detect_host_class(), "github_actions")

    def test_detect_host_class_ci(self) -> None:
        """Generic CI environment detected correctly."""
        with mock.patch.dict(os.environ, {"CI": "true"}, clear=False):
            env = os.environ.copy()
            env.pop("GITHUB_ACTIONS", None)
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(release_qualify._detect_host_class(), "ci")

    def test_detect_host_class_local(self) -> None:
        """Local environment detected as fallback."""
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(release_qualify._detect_host_class(), "local")


if __name__ == "__main__":
    unittest.main()
