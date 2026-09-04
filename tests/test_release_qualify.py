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

    def test_adoption_result_schema_field_name(self) -> None:
        """Verify schema uses 'schema' not 'schema_version'."""
        result = release_qualify.AdoptionResult(
            schema="code_mower.adoptionResult.v1",
            timestamp_utc="2024-01-01T00:00:00Z",
            release_tag="v1.0.0",
            package_identity="code-mower",
            normalized_version="1.0.0",
            qualification_context="cold_install",
            starting_version="",
            ending_version="1.0.0",
            provider="local_cli",
            executor="test_exec",
            host_class="local",
            runtime_class="python_3.12",
            elapsed_seconds=10.0,
            outcome="pass",
            step_count=2,
            warning_count=0,
            owner_action_count=0,
        )
        result_dict = result.to_dict()
        self.assertEqual(result_dict["schema"], "code_mower.adoptionResult.v1")
        self.assertNotIn("schema_version", result_dict)

    def test_normalize_version_extracts_semantic_version(self) -> None:
        """Test version normalization from various string formats."""
        cases = [
            ("1.0.0", "1.0.0"),
            ("code-mower 1.0.0", "1.0.0"),
            ("1.0.0a1", "1.0.0a1"),
            ("1.0.0b2", "1.0.0b2"),
            ("1.0.0rc3", "1.0.0rc3"),
            ("v1.0.0", "1.0.0"),
            ("invalid", ""),
        ]
        for input_str, expected in cases:
            with self.subTest(input=input_str):
                self.assertEqual(
                    release_qualify._normalize_version(input_str),
                    expected,
                )

    def test_safe_identifier_validation_rejects_unsafe(self) -> None:
        """Provider and executor must be safe identifiers."""
        with self.assertRaises(ValueError) as ctx:
            release_qualify._validate_safe_identifier(
                "../secret",
                "provider",
                release_qualify.SAFE_PROVIDER_PATTERN,
            )
        self.assertIn("must match", str(ctx.exception))

        with self.assertRaises(ValueError):
            release_qualify._validate_safe_identifier(
                "UPPERCASE",
                "executor",
                release_qualify.SAFE_EXECUTOR_PATTERN,
            )

        with self.assertRaises(ValueError):
            release_qualify._validate_safe_identifier(
                "has-dash",
                "provider",
                release_qualify.SAFE_PROVIDER_PATTERN,
            )

    def test_safe_identifier_validation_accepts_safe(self) -> None:
        """Valid safe identifiers pass."""
        release_qualify._validate_safe_identifier(
            "local_cli",
            "provider",
            release_qualify.SAFE_PROVIDER_PATTERN,
        )
        release_qualify._validate_safe_identifier(
            "test_executor_01",
            "executor",
            release_qualify.SAFE_EXECUTOR_PATTERN,
        )

    def test_extract_package_identity_sanitizes(self) -> None:
        """Package identity is sanitized from spec."""
        self.assertEqual(
            release_qualify._extract_package_identity("code-mower==1.0.0"),
            "code-mower",
        )
        self.assertEqual(
            release_qualify._extract_package_identity("/local/path"),
            "code-mower",
        )
        self.assertEqual(
            release_qualify._extract_package_identity("git+https://example.com/repo.git@v1.0.0"),
            "code-mower",
        )

    def test_validate_tag_format_strict(self) -> None:
        """Tag validation is strict and anchored."""
        valid, normalized, error = release_qualify._validate_tag_format("v1.0.0")
        self.assertTrue(valid)
        self.assertEqual(normalized, "1.0.0")
        self.assertEqual(error, "")

        valid, normalized, error = release_qualify._validate_tag_format("v1.0.0-alpha.1")
        self.assertTrue(valid)
        self.assertEqual(normalized, "1.0.0a1")

        valid, normalized, error = release_qualify._validate_tag_format("v1.0.0-beta.2")
        self.assertTrue(valid)
        self.assertEqual(normalized, "1.0.0b2")

        valid, normalized, error = release_qualify._validate_tag_format("v1.0.0-rc.3")
        self.assertTrue(valid)
        self.assertEqual(normalized, "1.0.0rc3")

        valid, normalized, error = release_qualify._validate_tag_format("1.0.0")
        self.assertFalse(valid)
        self.assertIn("must match", error)

        valid, normalized, error = release_qualify._validate_tag_format("v1.0.0-suffix")
        self.assertFalse(valid)

    def test_validate_tag_package_match_success(self) -> None:
        """Matching tag and package spec validates."""
        valid, normalized, error = release_qualify._validate_tag_package_match(
            "v1.0.0",
            "code-mower==1.0.0",
        )
        self.assertTrue(valid)
        self.assertEqual(normalized, "1.0.0")
        self.assertEqual(error, "")

    def test_validate_tag_package_match_mismatch(self) -> None:
        """Mismatched versions fail validation."""
        valid, normalized, error = release_qualify._validate_tag_package_match(
            "v1.0.0",
            "code-mower==1.0.1",
        )
        self.assertFalse(valid)
        self.assertIn("mismatch", error)

    def test_validate_tag_package_match_pending_for_nonindex(self) -> None:
        """Non-index specs return pending_verification."""
        valid, normalized, error = release_qualify._validate_tag_package_match(
            "v1.0.0",
            "/local/path",
        )
        self.assertFalse(valid)
        self.assertEqual(normalized, "1.0.0")
        self.assertEqual(error, "pending_verification")

    def test_run_doctor_check_actually_calls(self) -> None:
        """Doctor check actually runs with correct arguments."""
        with mock.patch("code_mower.release_qualify.code_mower_doctor_checks.run_doctor") as mock_doctor:
            mock_report = mock.Mock()
            mock_report.status = "pass"
            mock_report.checks = []
            mock_doctor.return_value = mock_report

            with mock.patch("code_mower.release_qualify.code_mower_doctor_checks.resolve_doctor_provider_templates_path") as mock_resolve:
                mock_resolve.return_value = Path("/tmp/providers.yml")

                status, warnings, actions = release_qualify._run_doctor_check(timeout=60)

            self.assertEqual(status, "pass")
            self.assertTrue(mock_doctor.called)
            call_kwargs = mock_doctor.call_args.kwargs
            self.assertIn("config_path", call_kwargs)
            self.assertIn("provider_templates_path", call_kwargs)
            self.assertIn("profile", call_kwargs)

    def test_run_lanes_status_check_calls_subprocess(self) -> None:
        """Lanes status check calls subprocess correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = Path(tmpdir)
            with mock.patch("subprocess.run") as mock_run:
                mock_run.return_value = mock.Mock(returncode=0, stdout='{"status": "pass"}')

                status, warnings, actions = release_qualify._run_lanes_status_check(
                    repo_path,
                    timeout=60,
                )

                self.assertEqual(status, "pass")
                self.assertTrue(mock_run.called)
                args = mock_run.call_args.args[0]
                self.assertIn("lanes", args)
                self.assertIn("status", args)

    def test_run_board_check_calls_board(self) -> None:
        """Board check calls board diagnostics."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = Path(tmpdir)
            with mock.patch("code_mower.release_qualify.code_mower_board.render_board_report") as mock_board:
                mock_board.return_value = {}

                status, warnings, actions = release_qualify._run_board_check(
                    repo_path,
                    timeout=60,
                )

                self.assertEqual(status, "pass")
                self.assertTrue(mock_board.called)

    def test_run_release_qualification_validates_identifiers(self) -> None:
        """Qualification rejects unsafe provider/executor."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "result.json"

            with self.assertRaises(ValueError) as ctx:
                release_qualify.run_release_qualification(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    output_path=output_path,
                    dry_run=True,
                    provider="../unsafe",
                    executor="test",
                )
            self.assertIn("provider", str(ctx.exception).lower())

    def test_run_release_qualification_dry_run_no_paths_in_output(self) -> None:
        """Dry run output contains no local paths."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "result.json"

            with mock.patch("code_mower.release_qualify._run_doctor_check") as mock_doctor:
                mock_doctor.return_value = ("pass", 0, 0)

                result = release_qualify.run_release_qualification(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    output_path=output_path,
                    dry_run=True,
                    provider="local_cli",
                    executor="test_exec",
                )

            result_json = json.dumps(result)
            self.assertNotIn(str(tmpdir), result_json)
            self.assertNotIn("/workspace", result_json)
            self.assertNotIn("/home", result_json)
            self.assertEqual(result["package_identity"], "code-mower")
            self.assertNotIn("package_spec", result)

    def test_run_release_qualification_counts_not_prose(self) -> None:
        """Result uses counts not arbitrary prose."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "result.json"

            with mock.patch("code_mower.release_qualify._run_doctor_check") as mock_doctor:
                mock_doctor.return_value = ("pass", 2, 1)

                result = release_qualify.run_release_qualification(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    output_path=output_path,
                    dry_run=True,
                    provider="local_cli",
                    executor="test_exec",
                )

            self.assertIn("warning_count", result)
            self.assertIn("owner_action_count", result)
            self.assertIn("step_count", result)
            self.assertNotIn("warnings", result)
            self.assertNotIn("owner_actions", result)
            self.assertNotIn("steps", result)
            self.assertEqual(result["warning_count"], 2)
            self.assertEqual(result["owner_action_count"], 1)

    def test_qualification_context_not_install_mode(self) -> None:
        """Uses qualification_context not install_mode."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "result.json"

            with mock.patch("code_mower.release_qualify._run_doctor_check") as mock_doctor:
                mock_doctor.return_value = ("pass", 0, 0)

                result = release_qualify.run_release_qualification(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    output_path=output_path,
                    dry_run=True,
                )

            self.assertIn("qualification_context", result)
            self.assertNotIn("install_mode", result)
            self.assertIn(result["qualification_context"], ["cold_install", "upgrade", "unknown"])

    def test_pending_verification_for_local_path(self) -> None:
        """Local path package defers verification."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "result.json"

            with mock.patch("code_mower.release_qualify._run_doctor_check") as mock_doctor:
                mock_doctor.return_value = ("pass", 0, 0)

                result = release_qualify.run_release_qualification(
                    release_tag="v1.0.0",
                    package_spec="/local/code-mower",
                    output_path=output_path,
                    dry_run=True,
                )

            self.assertEqual(result["package_identity"], "code-mower")
            self.assertEqual(result["ending_version"], "")

    def test_main_command_no_output_path_in_text(self) -> None:
        """Text output does not print local paths."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "result.json"

            with mock.patch("code_mower.release_qualify._run_doctor_check") as mock_doctor:
                mock_doctor.return_value = ("pass", 0, 0)
                with mock.patch("sys.stdout", new=StringIO()) as mock_stdout:
                    exit_code = release_qualify.main([
                        "qualify",
                        "--release-tag", "v1.0.0",
                        "--package-spec", "code-mower==1.0.0",
                        "--output", str(output_path),
                    ])

                    output_text = mock_stdout.getvalue()
                    self.assertNotIn(str(output_path), output_text)
                    self.assertNotIn(str(tmpdir), output_text)

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
