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
        self.assertIn("must be safe", str(ctx.exception))
        self.assertNotIn("../unsafe", str(ctx.exception))

    def test_starting_version_validation(self) -> None:
        """Starting version must be empty or normalized."""
        release_qualify._validate_starting_version("")
        release_qualify._validate_starting_version("1.0.0")
        release_qualify._validate_starting_version("1.0.0a1")

        with self.assertRaises(ValueError) as ctx:
            release_qualify._validate_starting_version("/path/to/version")
        self.assertIn("must be empty or normalized", str(ctx.exception))
        self.assertNotIn("/path", str(ctx.exception))

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

    def test_doctor_uses_real_config(self) -> None:
        """Doctor check uses real repo config."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = Path(tmpdir)
            config_path = repo_path / "code-mower.yml"
            config_path.write_text("repositories: []\n")
            output_path = Path(tmpdir) / "result.json"

            with mock.patch("code_mower.release_qualify.doctor_checks.run_doctor") as mock_doctor:
                mock_report = mock.Mock()
                mock_report.status = "pass"
                mock_report.warnings = 0
                mock_report.owner_actions = 0
                mock_doctor.return_value = mock_report

                release_qualify.run_release_qualification(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    output_path=output_path,
                    repo_path=repo_path,
                    dry_run=True,
                )

                kwargs = mock_doctor.call_args.kwargs
                self.assertEqual(kwargs["config_path"], config_path)
                self.assertIn("adoption", kwargs)
                self.assertTrue(kwargs["adoption"])
                self.assertTrue(kwargs["probe_runtime"])
                self.assertTrue(kwargs["github"])
                self.assertTrue(kwargs["cloud"])

    def test_lanes_check_uses_realistic_payload(self) -> None:
        """Lanes check interprets realistic collect_status payload."""
        with mock.patch("code_mower.release_qualify.lane_status.collect_status") as mock_lanes:
            mock_lanes.return_value = {
                "schema": "code_mower.laneStatus.v1",
                "remote": {"available": True},
                "local_boards": []
            }

            step = release_qualify._run_lanes_check("owner/repo")

            self.assertEqual(step.id, "lanes_status")
            self.assertEqual(step.status, "pass")

    def test_lanes_remote_unavailable_is_warn(self) -> None:
        """Lanes with remote unavailable is warn, not fail."""
        with mock.patch("code_mower.release_qualify.lane_status.collect_status") as mock_lanes:
            mock_lanes.return_value = {
                "schema": "code_mower.laneStatus.v1",
                "remote": {"available": False},
            }

            step = release_qualify._run_lanes_check("owner/repo")

            self.assertEqual(step.status, "warn")
            self.assertEqual(step.warning_count, 1)

    def test_board_check_uses_realistic_payload(self) -> None:
        """Board check interprets realistic doctor_payload."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = Path(tmpdir)

            with mock.patch("code_mower.release_qualify.code_mower_board.doctor_payload") as mock_board:
                mock_board.return_value = {
                    "schema": "code_mower.boardDoctor.v1",
                    "status": "pass",
                    "checks": []
                }

                step = release_qualify._run_board_check("owner/repo", repo_path)

                self.assertEqual(step.id, "board")
                self.assertEqual(step.status, "pass")

    def test_board_warn_status_preserved(self) -> None:
        """Board warn status is preserved."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = Path(tmpdir)

            with mock.patch("code_mower.release_qualify.code_mower_board.doctor_payload") as mock_board:
                mock_board.return_value = {
                    "status": "warn",
                    "checks": [{"status": "warn"}]
                }

                step = release_qualify._run_board_check("owner/repo", repo_path)

                self.assertEqual(step.status, "warn")
                self.assertEqual(step.warning_count, 1)

    def test_aggregate_outcome_handles_all_statuses(self) -> None:
        """Outcome aggregation handles all bounded statuses."""
        fail_step = release_qualify.StepResult("test", "fail", 1.0, 0, 0)
        warn_step = release_qualify.StepResult("test", "warn", 1.0, 0, 0)
        unavail_step = release_qualify.StepResult("test", "unavailable", 1.0, 0, 0)
        pass_step = release_qualify.StepResult("test", "pass", 1.0, 0, 0)

        self.assertEqual(release_qualify._aggregate_outcome([fail_step]), "fail")
        self.assertEqual(release_qualify._aggregate_outcome([fail_step, pass_step]), "fail")
        self.assertEqual(release_qualify._aggregate_outcome([warn_step]), "pass_with_warnings")
        self.assertEqual(release_qualify._aggregate_outcome([unavail_step]), "pass_with_warnings")
        self.assertEqual(release_qualify._aggregate_outcome([pass_step]), "pass")

    def test_dry_run_emits_planned_step(self) -> None:
        """Dry-run emits package_install step with planned status."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "result.json"

            with mock.patch("code_mower.release_qualify._run_doctor_check") as mock_doctor:
                mock_doctor.return_value = release_qualify.StepResult(
                    id="doctor", status="pass", elapsed_seconds=1.0,
                    warning_count=0, owner_action_count=0
                )

                result = release_qualify.run_release_qualification(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    output_path=output_path,
                    dry_run=True,
                )

            install_step = [s for s in result["steps"] if s["id"] == "package_install"][0]
            self.assertEqual(install_step["status"], "planned")
            self.assertEqual(result["ending_version"], "")

    def test_no_local_paths_in_result(self) -> None:
        """Result contains no local paths."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "result.json"

            with mock.patch("code_mower.release_qualify._run_doctor_check") as mock_doctor:
                mock_doctor.return_value = release_qualify.StepResult(
                    id="doctor", status="pass", elapsed_seconds=1.0,
                    warning_count=0, owner_action_count=0
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

    def test_steps_list_stable_schema(self) -> None:
        """Steps list has stable IDs and no sensitive fields."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "result.json"

            with mock.patch("code_mower.release_qualify._run_doctor_check") as mock_doctor:
                mock_doctor.return_value = release_qualify.StepResult(
                    id="doctor", status="pass", elapsed_seconds=1.0,
                    warning_count=0, owner_action_count=0
                )

                result = release_qualify.run_release_qualification(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    output_path=output_path,
                    dry_run=True,
                )

            self.assertIn("steps", result)
            for step in result["steps"]:
                self.assertIn("id", step)
                self.assertIn("status", step)
                self.assertIn("elapsed_seconds", step)
                self.assertIn("warning_count", step)
                self.assertIn("owner_action_count", step)
                self.assertNotIn("command", step)
                self.assertNotIn("stdout", step)
                self.assertNotIn("message", step)


if __name__ == "__main__":
    unittest.main()
