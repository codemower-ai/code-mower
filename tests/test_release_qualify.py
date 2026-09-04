#!/usr/bin/env python3
"""Tests for release qualification."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from code_mower import release_qualify


class ReleaseQualifyTests(unittest.TestCase):
    """Tests for release qualification command."""

    def test_safe_identifier_rejects_unsafe(self) -> None:
        """Provider/executor must be safe identifiers."""
        with self.assertRaises(ValueError) as ctx:
            release_qualify._validate_safe_identifier("../unsafe", "provider")
        self.assertIn("must match", str(ctx.exception))

        with self.assertRaises(ValueError):
            release_qualify._validate_safe_identifier("HAS-DASH", "executor")

    def test_exact_package_index_required(self) -> None:
        """Only exact package-index specs are accepted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "result.json"

            with self.assertRaises(ValueError) as ctx:
                release_qualify.run_release_qualification(
                    release_tag="v1.0.0",
                    package_spec="/local/path",
                    output_path=output_path,
                    dry_run=True,
                )
            self.assertIn("Only exact package-index", str(ctx.exception))

    def test_tag_spec_version_match_required(self) -> None:
        """Tag and spec versions must match exactly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "result.json"

            with self.assertRaises(ValueError) as ctx:
                release_qualify.run_release_qualification(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.1",
                    output_path=output_path,
                    dry_run=True,
                )
            self.assertIn("mismatch", str(ctx.exception))

    def test_doctor_check_runs_with_correct_api(self) -> None:
        """Doctor check calls run_doctor with correct arguments."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "code-mower.yml"
            config_path.write_text("repositories: []\n")

            with mock.patch("code_mower.release_qualify.doctor_checks.run_doctor") as mock_doctor:
                with mock.patch("code_mower.release_qualify.doctor_checks.resolve_doctor_provider_templates_path") as mock_resolve:
                    mock_report = mock.Mock()
                    mock_report.status = "pass"
                    mock_report.checks = []
                    mock_doctor.return_value = mock_report
                    mock_resolve.return_value = Path("/tmp/providers.yml")

                    step = release_qualify._run_doctor_check(config_path, 60)

                    self.assertTrue(mock_doctor.called)
                    kwargs = mock_doctor.call_args.kwargs
                    self.assertIn("config_path", kwargs)
                    self.assertIn("adoption", kwargs)
                    self.assertIn("github", kwargs)
                    self.assertEqual(step.id, "doctor")
                    self.assertEqual(step.status, "pass")

    def test_lanes_check_uses_collect_status(self) -> None:
        """Lanes check calls collect_status with repo slug."""
        with mock.patch("code_mower.release_qualify.lane_status.collect_status") as mock_lanes:
            mock_lanes.return_value = {"status": "ok"}

            step = release_qualify._run_lanes_check("owner/repo", 60)

            self.assertTrue(mock_lanes.called)
            kwargs = mock_lanes.call_args.kwargs
            self.assertEqual(kwargs["repo"], "owner/repo")
            self.assertEqual(step.id, "lanes_status")
            self.assertEqual(step.status, "pass")

    def test_board_check_uses_doctor_payload(self) -> None:
        """Board check calls doctor_payload with BoardConfig."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = Path(tmpdir)

            with mock.patch("code_mower.release_qualify.code_mower_board.doctor_payload") as mock_board:
                mock_board.return_value = {}

                step = release_qualify._run_board_check("owner/repo", repo_path, 60)

                self.assertTrue(mock_board.called)
                config = mock_board.call_args.args[0]
                self.assertEqual(config.repo, "owner/repo")
                self.assertEqual(step.id, "board")
                self.assertEqual(step.status, "pass")

    def test_required_step_fail_makes_outcome_fail(self) -> None:
        """Failed required step makes final outcome fail."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "result.json"

            with mock.patch("code_mower.release_qualify._run_doctor_check") as mock_doctor:
                mock_doctor.return_value = release_qualify.StepResult(
                    id="doctor",
                    status="fail",
                    elapsed_seconds=1.0,
                    warning_count=0,
                    owner_action_count=0,
                )

                result = release_qualify.run_release_qualification(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    output_path=output_path,
                    dry_run=True,
                )

            self.assertEqual(result["outcome"], "fail")

    def test_unavailable_makes_pass_with_warnings(self) -> None:
        """Unavailable optional step makes outcome pass_with_warnings."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "result.json"

            with mock.patch("code_mower.release_qualify._run_doctor_check") as mock_doctor:
                mock_doctor.return_value = release_qualify.StepResult(
                    id="doctor",
                    status="unavailable",
                    elapsed_seconds=1.0,
                    warning_count=0,
                    owner_action_count=0,
                )

                result = release_qualify.run_release_qualification(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    output_path=output_path,
                    dry_run=True,
                )

            self.assertEqual(result["outcome"], "pass_with_warnings")

    def test_steps_list_has_stable_ids_and_status(self) -> None:
        """Steps list contains stable IDs and statuses."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "result.json"

            with mock.patch("code_mower.release_qualify._run_doctor_check") as mock_doctor:
                mock_doctor.return_value = release_qualify.StepResult(
                    id="doctor",
                    status="pass",
                    elapsed_seconds=1.0,
                    warning_count=0,
                    owner_action_count=0,
                )

                result = release_qualify.run_release_qualification(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    output_path=output_path,
                    dry_run=True,
                )

            self.assertIn("steps", result)
            self.assertIsInstance(result["steps"], list)
            self.assertEqual(len(result["steps"]), 1)
            step = result["steps"][0]
            self.assertEqual(step["id"], "doctor")
            self.assertIn("status", step)
            self.assertIn("elapsed_seconds", step)
            self.assertIn("warning_count", step)
            self.assertIn("owner_action_count", step)
            self.assertNotIn("command", step)
            self.assertNotIn("stdout", step)
            self.assertNotIn("stderr", step)
            self.assertNotIn("message", step)

    def test_no_local_paths_in_result(self) -> None:
        """Result contains no local paths."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "result.json"

            with mock.patch("code_mower.release_qualify._run_doctor_check") as mock_doctor:
                mock_doctor.return_value = release_qualify.StepResult(
                    id="doctor",
                    status="pass",
                    elapsed_seconds=1.0,
                    warning_count=0,
                    owner_action_count=0,
                )

                result = release_qualify.run_release_qualification(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    output_path=output_path,
                    dry_run=True,
                )

            result_json = json.dumps(result)
            self.assertNotIn(str(tmpdir), result_json)
            self.assertNotIn("/workspace", result_json)
            self.assertNotIn("/home", result_json)
            self.assertEqual(result["package_identity"], "code-mower")

    def test_explicit_qualification_context(self) -> None:
        """Qualification context is explicit parameter."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "result.json"

            with mock.patch("code_mower.release_qualify._run_doctor_check") as mock_doctor:
                mock_doctor.return_value = release_qualify.StepResult(
                    id="doctor",
                    status="pass",
                    elapsed_seconds=1.0,
                    warning_count=0,
                    owner_action_count=0,
                )

                result = release_qualify.run_release_qualification(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    output_path=output_path,
                    qualification_context="cold_install",
                    starting_version="",
                    dry_run=True,
                )

            self.assertEqual(result["qualification_context"], "cold_install")
            self.assertEqual(result["starting_version"], "")


if __name__ == "__main__":
    unittest.main()
