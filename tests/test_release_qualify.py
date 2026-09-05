#!/usr/bin/env python3
"""Tests for release qualification."""

from __future__ import annotations

import contextlib
import io
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

    def test_campaign_help_names_builder_default(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit) as caught:
            release_qualify.main(["campaign", "--help"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("cursor_cloud_agent", stdout.getvalue())
        self.assertNotIn("cursor_bugbot", stdout.getvalue())

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

    def test_upgrade_context_requires_starting_version(self) -> None:
        """Upgrade context requires a bounded starting version."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "result.json"

            with self.assertRaises(ValueError) as ctx:
                release_qualify.run_release_qualification(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    output_path=output_path,
                    qualification_context="upgrade",
                    dry_run=True,
                )
            self.assertIn("starting_version is required", str(ctx.exception))

    def test_upgrade_requires_an_older_starting_version(self) -> None:
        """Upgrade context rejects equal or newer starting versions."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "result.json"
            with self.assertRaises(ValueError) as ctx:
                release_qualify.run_release_qualification(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    output_path=output_path,
                    qualification_context="upgrade",
                    starting_version="1.0.0",
                    dry_run=True,
                )
            self.assertIn("lower than the target", str(ctx.exception))

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

    def test_rc_release_tags_supported(self) -> None:
        """RC release tags normalize correctly and match package specs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "result.json"

            release_qualify.run_release_qualification(
                release_tag="v1.0.0-rc.1",
                package_spec="code-mower==1.0.0rc1",
                output_path=output_path,
                dry_run=True,
            )

            self.assertTrue(output_path.exists())
            with open(output_path, encoding="utf-8") as f:
                result = json.load(f)
            self.assertEqual(result["outcome"], "incomplete")
            self.assertEqual(result["package_identity"], "code-mower")

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

    def test_doctor_unrecognized_status_mapped_to_fail(self) -> None:
        """Doctor unrecognized status values are mapped to fail."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = Path(tmpdir)
            config_path = repo_path / "code-mower.yml"
            config_path.write_text("repositories: []\n")
            output_path = Path(tmpdir) / "result.json"

            with mock.patch("code_mower.release_qualify.doctor_checks.run_doctor") as mock_doctor:
                mock_report = mock.Mock()
                mock_report.status = "critical"
                mock_report.warnings = 5
                mock_report.owner_actions = 2
                mock_doctor.return_value = mock_report

                result = release_qualify.run_release_qualification(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    output_path=output_path,
                    repo_path=repo_path,
                    dry_run=True,
                )

                doctor_step = [s for s in result["steps"] if s["id"] == "doctor"][0]
                self.assertEqual(doctor_step["status"], "fail")
                self.assertEqual(result["outcome"], "fail")

    def test_config_path_scoped_to_repo_when_missing(self) -> None:
        """Config path stays in selected repo even when file doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = Path(tmpdir) / "empty-repo"
            repo_path.mkdir()
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
                expected_config = repo_path / "code-mower.yml"
                self.assertEqual(kwargs["config_path"], expected_config)
                self.assertFalse(expected_config.exists())

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
        planned_step = release_qualify.StepResult("test", "planned", 1.0, 0, 0)

        self.assertEqual(release_qualify._aggregate_outcome([fail_step]), "fail")
        self.assertEqual(release_qualify._aggregate_outcome([fail_step, pass_step]), "fail")
        self.assertEqual(release_qualify._aggregate_outcome([warn_step]), "pass_with_warnings")
        self.assertEqual(release_qualify._aggregate_outcome([unavail_step]), "pass_with_warnings")
        self.assertEqual(release_qualify._aggregate_outcome([pass_step]), "pass")
        self.assertEqual(
            release_qualify._aggregate_outcome([planned_step], execution_state="planned"),
            "incomplete",
        )
        self.assertEqual(release_qualify._aggregate_outcome([planned_step]), "fail")

    def test_dry_run_emits_planned_step(self) -> None:
        """Dry-run emits package_install step with planned status."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "result.json"

            with mock.patch("code_mower.release_qualify._run_doctor_check") as mock_doctor, mock.patch(
                "code_mower.release_qualify.time.time", side_effect=[100.0, 102.0]
            ):
                mock_doctor.return_value = release_qualify.StepResult(
                    id="doctor", status="pass", elapsed_seconds=1.0,
                    warning_count=0, owner_action_count=0
                )

                result = release_qualify.run_release_qualification(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    output_path=output_path,
                    dry_run=True,
                    repo_path=Path(tmpdir),
                )

            install_step = [s for s in result["steps"] if s["id"] == "package_install"][0]
            overhead_step = [s for s in result["steps"] if s["id"] == "overhead"][0]
            self.assertEqual(install_step["status"], "planned")
            self.assertEqual(overhead_step["status"], "planned")
            self.assertEqual(overhead_step["elapsed_seconds"], 1.0)
            self.assertEqual(
                sum(step["elapsed_seconds"] for step in result["steps"]),
                result["elapsed_seconds"],
            )
            self.assertEqual(result["ending_version"], "")
            self.assertEqual(result["execution_state"], "planned")
            self.assertEqual(result["outcome"], "incomplete")

    def test_execute_normalizes_rehearsal_version(self) -> None:
        """Execute normalizes rehearsal version from CLI format."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "result.json"

            with mock.patch("code_mower.release_qualify._run_doctor_check") as mock_doctor:
                with mock.patch("code_mower.release_qualify.run_package_install_rehearsal") as mock_rehearsal:
                    mock_doctor.return_value = release_qualify.StepResult(
                        id="doctor", status="pass", elapsed_seconds=1.0,
                        warning_count=0, owner_action_count=0
                    )
                    mock_rehearsal.return_value = {"version": "code-mower 1.0.0"}

                    result = release_qualify.run_release_qualification(
                        release_tag="v1.0.0",
                        package_spec="code-mower==1.0.0",
                        output_path=output_path,
                        dry_run=False,
                        repo_path=Path(tmpdir),
                    )

            install_step = [s for s in result["steps"] if s["id"] == "package_install"][0]
            self.assertEqual(install_step["status"], "pass")
            self.assertEqual(result["ending_version"], "1.0.0")
            self.assertEqual(result["execution_state"], "executed")

    def test_upgrade_rehearses_start_then_target_in_same_run(self) -> None:
        """Upgrade mode passes an exact preinstall spec and verifies both versions."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "result.json"

            with mock.patch("code_mower.release_qualify._run_doctor_check") as mock_doctor:
                with mock.patch("code_mower.release_qualify.run_package_install_rehearsal") as rehearsal:
                    mock_doctor.return_value = release_qualify.StepResult(
                        id="doctor", status="pass", elapsed_seconds=1.0,
                        warning_count=0, owner_action_count=0
                    )
                    rehearsal.return_value = {
                        "preinstall_version": "code-mower 1.0.0",
                        "version": "code-mower 1.0.1",
                    }

                    result = release_qualify.run_release_qualification(
                        release_tag="v1.0.1",
                        package_spec="code-mower==1.0.1",
                        output_path=output_path,
                        qualification_context="upgrade",
                        starting_version="1.0.0",
                        dry_run=False,
                        repo_path=Path(tmpdir),
                    )

            self.assertEqual(
                rehearsal.call_args.kwargs["preinstall_package_spec"],
                "code-mower==1.0.0",
            )
            self.assertEqual(result["outcome"], "pass")
            self.assertEqual(result["starting_version"], "1.0.0")
            self.assertEqual(result["ending_version"], "1.0.1")

    def test_upgrade_fails_when_preinstall_version_mismatches(self) -> None:
        """Upgrade evidence fails if the isolated starting version is wrong."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "result.json"

            with mock.patch("code_mower.release_qualify._run_doctor_check") as mock_doctor:
                with mock.patch("code_mower.release_qualify.run_package_install_rehearsal") as rehearsal:
                    mock_doctor.return_value = release_qualify.StepResult(
                        id="doctor", status="pass", elapsed_seconds=1.0,
                        warning_count=0, owner_action_count=0
                    )
                    rehearsal.return_value = {
                        "preinstall_version": "code-mower 0.9.9",
                        "version": "code-mower 1.0.1",
                    }

                    result = release_qualify.run_release_qualification(
                        release_tag="v1.0.1",
                        package_spec="code-mower==1.0.1",
                        output_path=output_path,
                        qualification_context="upgrade",
                        starting_version="1.0.0",
                        dry_run=False,
                        repo_path=Path(tmpdir),
                    )

            self.assertEqual(result["outcome"], "fail")

    def test_unknown_step_status_fails_closed(self) -> None:
        """Every helper status is normalized through the closed vocabulary."""
        step = release_qualify.StepResult("doctor", "critical", 0.0, 0, 0)
        self.assertEqual(step.status, "fail")
        self.assertEqual(release_qualify._aggregate_outcome([step]), "fail")

    def test_execute_fails_on_version_mismatch(self) -> None:
        """Execute fails when rehearsal version doesn't match tag."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "result.json"

            with mock.patch("code_mower.release_qualify._run_doctor_check") as mock_doctor:
                with mock.patch("code_mower.release_qualify.run_package_install_rehearsal") as mock_rehearsal:
                    mock_doctor.return_value = release_qualify.StepResult(
                        id="doctor", status="pass", elapsed_seconds=1.0,
                        warning_count=0, owner_action_count=0
                    )
                    mock_rehearsal.return_value = {"version": "code-mower 1.0.1"}

                    result = release_qualify.run_release_qualification(
                        release_tag="v1.0.0",
                        package_spec="code-mower==1.0.0",
                        output_path=output_path,
                        dry_run=False,
                        repo_path=Path(tmpdir),
                    )

            install_step = [s for s in result["steps"] if s["id"] == "package_install"][0]
            self.assertEqual(install_step["status"], "fail")
            self.assertEqual(result["outcome"], "fail")

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


def _valid_adoption_result(**overrides: object) -> dict:
    result = {
        "schema": release_qualify.ADOPTION_RESULT_SCHEMA,
        "timestamp_utc": "2026-09-04T08:00:00Z",
        "release_tag": "v1.0.0",
        "package_identity": "code-mower",
        "normalized_version": "1.0.0",
        "qualification_context": "cold_install",
        "starting_version": "",
        "ending_version": "1.0.0",
        "provider": "codex",
        "executor": "codex_cli",
        "host_class": "local",
        "runtime_class": "python_3.12",
        "execution_state": "executed",
        "elapsed_seconds": 1.0,
        "outcome": "pass",
        "steps": [
            {
                "id": "doctor",
                "status": "pass",
                "elapsed_seconds": 0.5,
                "warning_count": 0,
                "owner_action_count": 0,
            }
        ],
    }
    result.update(overrides)
    return result


class EndingVersionValidationTests(unittest.TestCase):
    """An executed pass/pass_with_warnings result must report the target version
    it actually ended on; planned/incomplete results may retain an empty one.
    """

    def test_executed_pass_with_matching_ending_version_accepted(self) -> None:
        release_qualify.validate_adoption_result_payload(
            _valid_adoption_result(execution_state="executed", outcome="pass", ending_version="1.0.0")
        )

    def test_executed_pass_with_warnings_with_matching_ending_version_accepted(self) -> None:
        release_qualify.validate_adoption_result_payload(
            _valid_adoption_result(
                execution_state="executed",
                outcome="pass_with_warnings",
                ending_version="1.0.0",
                steps=[
                    {
                        "id": "doctor",
                        "status": "warn",
                        "elapsed_seconds": 0.5,
                        "warning_count": 1,
                        "owner_action_count": 0,
                    }
                ],
            )
        )

    def test_executed_pass_with_empty_ending_version_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            release_qualify.validate_adoption_result_payload(
                _valid_adoption_result(execution_state="executed", outcome="pass", ending_version="")
            )
        self.assertIn("ending_version", str(ctx.exception))

    def test_executed_pass_with_stale_ending_version_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            release_qualify.validate_adoption_result_payload(
                _valid_adoption_result(execution_state="executed", outcome="pass", ending_version="0.9.0")
            )
        self.assertIn("ending_version", str(ctx.exception))

    def test_executed_pass_with_warnings_with_empty_ending_version_rejected(self) -> None:
        with self.assertRaises(ValueError):
            release_qualify.validate_adoption_result_payload(
                _valid_adoption_result(
                    execution_state="executed",
                    outcome="pass_with_warnings",
                    ending_version="",
                    steps=[
                        {
                            "id": "doctor",
                            "status": "warn",
                            "elapsed_seconds": 0.5,
                            "warning_count": 1,
                            "owner_action_count": 0,
                        }
                    ],
                )
            )

    def test_executed_fail_does_not_require_ending_version_match(self) -> None:
        """A failed rehearsal may legitimately report the version it actually
        landed on, which is exactly why it failed -- no equality is required."""
        release_qualify.validate_adoption_result_payload(
            _valid_adoption_result(
                execution_state="executed",
                outcome="fail",
                ending_version="0.9.0",
                steps=[
                    {
                        "id": "package_install",
                        "status": "fail",
                        "elapsed_seconds": 0.5,
                        "warning_count": 0,
                        "owner_action_count": 0,
                    }
                ],
            )
        )

    def test_planned_incomplete_allows_empty_ending_version(self) -> None:
        release_qualify.validate_adoption_result_payload(
            _valid_adoption_result(
                execution_state="planned",
                outcome="incomplete",
                ending_version="",
                steps=[
                    {
                        "id": "package_install",
                        "status": "planned",
                        "elapsed_seconds": 0.0,
                        "warning_count": 0,
                        "owner_action_count": 0,
                    }
                ],
            )
        )


class TimestampUtcValidationTests(unittest.TestCase):
    """timestamp_utc must be a real ISO 8601 timestamp with a UTC/offset designator."""

    def test_accepts_z_suffix(self) -> None:
        release_qualify.validate_adoption_result_payload(
            _valid_adoption_result(timestamp_utc="2026-09-04T08:00:00Z")
        )

    def test_accepts_numeric_offset(self) -> None:
        release_qualify.validate_adoption_result_payload(
            _valid_adoption_result(timestamp_utc="2026-09-04T08:00:00+05:30")
        )

    def test_accepts_fractional_seconds(self) -> None:
        release_qualify.validate_adoption_result_payload(
            _valid_adoption_result(timestamp_utc="2026-09-04T08:00:00.123456Z")
        )

    def test_rejects_missing_timezone(self) -> None:
        with self.assertRaises(ValueError):
            release_qualify.validate_adoption_result_payload(
                _valid_adoption_result(timestamp_utc="2026-09-04T08:00:00")
            )

    def test_rejects_path_like_value(self) -> None:
        with self.assertRaises(ValueError):
            release_qualify.validate_adoption_result_payload(
                _valid_adoption_result(timestamp_utc="../../etc/passwd")
            )

    def test_rejects_multiline_value(self) -> None:
        with self.assertRaises(ValueError):
            release_qualify.validate_adoption_result_payload(
                _valid_adoption_result(timestamp_utc="2026-09-04T08:00:00Z\nmalicious payload")
            )

    def test_rejects_arbitrary_free_text(self) -> None:
        with self.assertRaises(ValueError):
            release_qualify.validate_adoption_result_payload(
                _valid_adoption_result(timestamp_utc="not a timestamp at all")
            )

    def test_rejects_semantically_invalid_calendar_date(self) -> None:
        """Plausible-looking but out-of-range values (month 13) are still rejected."""
        with self.assertRaises(ValueError):
            release_qualify.validate_adoption_result_payload(
                _valid_adoption_result(timestamp_utc="2026-13-45T99:99:99Z")
            )

    def test_rejects_non_string(self) -> None:
        with self.assertRaises(ValueError):
            release_qualify.validate_adoption_result_payload(
                _valid_adoption_result(timestamp_utc=1757000000)
            )

    def test_rejects_empty_string(self) -> None:
        with self.assertRaises(ValueError):
            release_qualify.validate_adoption_result_payload(_valid_adoption_result(timestamp_utc=""))


class PackageIdentityDerivationTests(unittest.TestCase):
    """Package identity comes from the exact spec, not from a hard-coded package."""

    def test_exact_index_spec_yields_its_own_package_name(self) -> None:
        self.assertEqual(
            release_qualify._extract_package_identity("code-mower==1.0.0"), "code-mower"
        )
        self.assertEqual(
            release_qualify._extract_package_identity("other-widget==1.0.0"), "other-widget"
        )

    def test_identity_is_pep503_normalized(self) -> None:
        """Spellings a package index treats as one package are one identity here."""
        for spelling in ("Code_Mower==1.0.0", "code.mower==1.0.0", "CODE--MOWER==1.0.0"):
            with self.subTest(spelling=spelling):
                self.assertEqual(
                    release_qualify._extract_package_identity(spelling), "code-mower"
                )

    def test_inexact_and_non_index_specs_are_rejected(self) -> None:
        for spec in ("", ".", "code-mower", "code-mower>=1.0.0", "/tmp/code-mower",
                     "git+https://example.invalid/x.git", "https://example.invalid/x.whl"):
            with self.subTest(spec=spec):
                with self.assertRaises(ValueError):
                    release_qualify._extract_package_identity(spec)

    def test_builtin_runner_still_only_qualifies_code_mower(self) -> None:
        """The built-in runner installs and drives `code-mower`, so it says so.

        Campaigns are not narrowed this way -- they bind whatever exact spec
        they were created with.
        """
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as ctx:
                release_qualify.run_release_qualification(
                    release_tag="v1.0.0",
                    package_spec="other-widget==1.0.0",
                    output_path=Path(tmp) / "result.json",
                    repo_path=Path(tmp),
                )
            self.assertIn("only supports the code-mower package", str(ctx.exception))
            self.assertFalse((Path(tmp) / "result.json").exists())


class AdoptionResultPackageIdentityTests(unittest.TestCase):
    """A result's package_identity is validated structurally and bound on request."""

    def test_unbound_validation_accepts_any_normalized_package_name(self) -> None:
        release_qualify.validate_adoption_result_payload(
            _valid_adoption_result(package_identity="other-widget")
        )

    def test_free_form_package_identity_is_always_rejected(self) -> None:
        for value in ("Code-Mower", "code mower", "/tmp/code-mower", "code_mower", "", 7, None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    release_qualify.validate_adoption_result_payload(
                        _valid_adoption_result(package_identity=value)
                    )

    def test_expected_identity_binds_the_result_to_one_package(self) -> None:
        release_qualify.validate_adoption_result_payload(
            _valid_adoption_result(package_identity="other-widget"),
            expected_package_identity="other-widget",
        )
        with self.assertRaises(ValueError) as ctx:
            release_qualify.validate_adoption_result_payload(
                _valid_adoption_result(package_identity="code-mower"),
                expected_package_identity="other-widget",
            )
        self.assertIn("does not match the campaign package", str(ctx.exception))


class ExactPackageSpecParseTests(unittest.TestCase):
    """One parse yields both halves of an exact spec, for every caller."""

    def test_identity_and_version_come_out_together(self) -> None:
        for spec, identity, version in (
            ("code-mower==1.0.0", "code-mower", "1.0.0"),
            ("code.mower==1.0.0", "code-mower", "1.0.0"),
            ("Code_Mower==1.0.0rc1", "code-mower", "1.0.0rc1"),
            ("zope.interface==5.0.0", "zope-interface", "5.0.0"),
            ("  other-widget==1.0.0  ", "other-widget", "1.0.0"),
        ):
            with self.subTest(spec=spec):
                self.assertEqual(
                    release_qualify._parse_exact_package_spec(spec), (identity, version)
                )
                self.assertEqual(release_qualify._extract_package_identity(spec), identity)

    def test_inexact_and_malformed_specs_are_rejected_with_one_message(self) -> None:
        for spec in (
            "",
            ".",
            "code-mower",
            "code-mower>=1.0.0",
            "code-mower==",
            "==1.0.0",
            "code-mower[extra]==1.0.0",
            'code-mower==1.0.0; python_version<"3"',
            "/tmp/code-mower",
            "git+https://example.invalid/x.git",
        ):
            with self.subTest(spec=spec):
                with self.assertRaises(ValueError) as ctx:
                    release_qualify._parse_exact_package_spec(spec)
                self.assertIn(
                    "Only exact package-index specs supported", str(ctx.exception)
                )

    def test_builtin_runner_accepts_a_dotted_spelling_of_its_own_package(self) -> None:
        """`code.mower==1.0.0` is `code-mower==1.0.0` to a package index.

        The runner used to read the version back with a name grammar that
        stopped at the dot and refuse the spec as inexact.
        """
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "result.json"
            result = release_qualify.run_release_qualification(
                release_tag="v1.0.0",
                package_spec="code.mower==1.0.0",
                output_path=output_path,
                repo_path=Path(tmp),
                dry_run=True,
            )
            self.assertEqual(result["package_identity"], "code-mower")
            self.assertEqual(result["normalized_version"], "1.0.0")

    def test_builtin_runner_still_refuses_another_package_however_spelled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as ctx:
                release_qualify.run_release_qualification(
                    release_tag="v5.0.0",
                    package_spec="zope.interface==5.0.0",
                    output_path=Path(tmp) / "result.json",
                    repo_path=Path(tmp),
                )
            self.assertIn("only supports the code-mower package", str(ctx.exception))
            self.assertFalse((Path(tmp) / "result.json").exists())

    def test_builtin_runner_reports_a_dotted_spec_version_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as ctx:
                release_qualify.run_release_qualification(
                    release_tag="v1.0.0",
                    package_spec="code.mower==1.0.1",
                    output_path=Path(tmp) / "result.json",
                    repo_path=Path(tmp),
                )
            self.assertIn("Version mismatch", str(ctx.exception))

    def test_validate_adoption_result_payload_requires_python_312_or_higher(self) -> None:
        release_qualify.validate_adoption_result_payload(_valid_adoption_result(runtime_class="python_3.12"))
        release_qualify.validate_adoption_result_payload(_valid_adoption_result(runtime_class="python_3.13"))
        release_qualify.validate_adoption_result_payload(_valid_adoption_result(runtime_class="python_3.14"))

        for unsupported in ("python_3.11", "python_3.10", "python_3.9", "python_3.8"):
            with self.subTest(runtime_class=unsupported):
                with self.assertRaisesRegex(ValueError, "must be >= python_3.12"):
                    release_qualify.validate_adoption_result_payload(_valid_adoption_result(runtime_class=unsupported))

        for non_python in ("unknown", "", "node_22", "ruby_3.2", "python_3", "python_3.12.1", "pypy_3.12"):
            with self.subTest(runtime_class=non_python):
                with self.assertRaisesRegex(ValueError, "must be 'python_<major>.<minor>'"):
                    release_qualify.validate_adoption_result_payload(_valid_adoption_result(runtime_class=non_python))

    def test_validate_adoption_result_rejects_unknown_runtime_across_result_types(self) -> None:
        """Hosted, manual, and custom results reporting runtime_class unknown must be rejected."""
        # 1. Hosted runner result (e.g. host_class='github_actions')
        hosted_unknown = _valid_adoption_result(
            host_class="github_actions",
            provider="devin",
            executor="devin",
            runtime_class="unknown",
        )
        with self.assertRaisesRegex(ValueError, "must be 'python_<major>.<minor>'"):
            release_qualify.validate_adoption_result_payload(hosted_unknown)

        hosted_valid = _valid_adoption_result(
            host_class="github_actions",
            provider="devin",
            executor="devin",
            runtime_class="python_3.12",
        )
        release_qualify.validate_adoption_result_payload(hosted_valid)

        # 2. Manual qualification result
        manual_unknown = _valid_adoption_result(
            host_class="local",
            provider="manual",
            executor="manual",
            runtime_class="unknown",
        )
        with self.assertRaisesRegex(ValueError, "must be 'python_<major>.<minor>'"):
            release_qualify.validate_adoption_result_payload(manual_unknown)

        manual_valid = _valid_adoption_result(
            host_class="local",
            provider="manual",
            executor="manual",
            runtime_class="python_3.12",
        )
        release_qualify.validate_adoption_result_payload(manual_valid)

        # 3. Custom-adapter result
        custom_unknown = _valid_adoption_result(
            host_class="local",
            provider="custom_audit",
            executor="custom_runner",
            runtime_class="unknown",
        )
        with self.assertRaisesRegex(ValueError, "must be 'python_<major>.<minor>'"):
            release_qualify.validate_adoption_result_payload(custom_unknown)

        custom_valid = _valid_adoption_result(
            host_class="local",
            provider="custom_audit",
            executor="custom_runner",
            runtime_class="python_3.12",
        )
        release_qualify.validate_adoption_result_payload(custom_valid)


class PackageSourceTests(unittest.TestCase):
    """Closed `package_source` vocabulary: parsing, index construction, defaults."""

    def test_default_source_is_pypi(self) -> None:
        self.assertEqual(release_qualify.DEFAULT_PACKAGE_SOURCE, "pypi")

    def test_valid_sources_are_closed(self) -> None:
        self.assertEqual(release_qualify.VALID_PACKAGE_SOURCES, {"pypi", "testpypi"})

    def test_validate_rejects_anything_outside_the_closed_set(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            release_qualify._validate_package_source("https://example.invalid/simple")
        self.assertIn("package_source must be one of", str(ctx.exception))
        # An attempted arbitrary URL never survives into the error message.
        self.assertNotIn("example.invalid", str(ctx.exception))

    def test_validate_accepts_pypi_and_testpypi(self) -> None:
        release_qualify._validate_package_source("pypi")
        release_qualify._validate_package_source("testpypi")

    def test_pypi_uses_no_index_override(self) -> None:
        index_url, extra_urls = release_qualify._package_source_pip_index_args("pypi")
        self.assertEqual(index_url, "")
        self.assertEqual(extra_urls, ())

    def test_testpypi_upgrade_baseline_uses_only_production_pypi(self) -> None:
        index_url, extra_urls = release_qualify._package_source_pip_index_args("testpypi")
        self.assertEqual(index_url, "https://pypi.org/simple/")
        self.assertEqual(extra_urls, ())

    def test_package_source_pip_index_args_rejects_unknown_source(self) -> None:
        with self.assertRaises(ValueError):
            release_qualify._package_source_pip_index_args("bogus")

    def test_pypi_candidate_index_args_have_no_override(self) -> None:
        candidate_index_url, dependency_index_url = (
            release_qualify._package_source_candidate_index_args("pypi")
        )
        self.assertEqual(candidate_index_url, "")
        self.assertEqual(dependency_index_url, "")

    def test_testpypi_candidate_index_args_are_the_canonical_pair(self) -> None:
        candidate_index_url, dependency_index_url = (
            release_qualify._package_source_candidate_index_args("testpypi")
        )
        self.assertEqual(candidate_index_url, "https://test.pypi.org/simple/")
        self.assertEqual(dependency_index_url, "https://pypi.org/simple/")

    def test_package_source_candidate_index_args_rejects_unknown_source(self) -> None:
        with self.assertRaises(ValueError):
            release_qualify._package_source_candidate_index_args("bogus")

    def test_run_release_qualification_rejects_unknown_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as ctx:
                release_qualify.run_release_qualification(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    output_path=Path(tmp) / "result.json",
                    repo_path=Path(tmp),
                    dry_run=True,
                    package_source="bogus",
                )
            self.assertIn("package_source must be one of", str(ctx.exception))

    def test_execute_with_pypi_source_passes_no_index_override(self) -> None:
        """The default source builds a pip command with no index override at all."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "result.json"
            with mock.patch("code_mower.release_qualify._run_doctor_check") as mock_doctor:
                with mock.patch(
                    "code_mower.release_qualify.run_package_install_rehearsal"
                ) as mock_rehearsal:
                    mock_doctor.return_value = release_qualify.StepResult(
                        id="doctor", status="pass", elapsed_seconds=1.0,
                        warning_count=0, owner_action_count=0
                    )
                    mock_rehearsal.return_value = {"version": "code-mower 1.0.0"}

                    release_qualify.run_release_qualification(
                        release_tag="v1.0.0",
                        package_spec="code-mower==1.0.0",
                        output_path=output_path,
                        dry_run=False,
                        repo_path=Path(tmpdir),
                    )

            self.assertEqual(mock_rehearsal.call_args.kwargs["pip_index_url"], "")
            self.assertEqual(mock_rehearsal.call_args.kwargs["pip_extra_index_urls"], ())
            # The default source never triggers the closed two-stage candidate
            # flow: no index override at all applies to the candidate either.
            self.assertEqual(mock_rehearsal.call_args.kwargs["candidate_index_url"], "")
            self.assertEqual(
                mock_rehearsal.call_args.kwargs["candidate_dependency_index_url"], ""
            )

    def test_testpypi_upgrade_separates_baseline_candidate_and_dependencies(self) -> None:
        """A TestPyPI upgrade uses one exclusive source for each package role."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "result.json"
            with mock.patch("code_mower.release_qualify._run_doctor_check") as mock_doctor:
                with mock.patch(
                    "code_mower.release_qualify.run_package_install_rehearsal"
                ) as mock_rehearsal:
                    mock_doctor.return_value = release_qualify.StepResult(
                        id="doctor", status="pass", elapsed_seconds=1.0,
                        warning_count=0, owner_action_count=0
                    )
                    mock_rehearsal.return_value = {
                        "preinstall_version": "code-mower 1.0.0",
                        "version": "code-mower 1.0.1",
                    }

                    result = release_qualify.run_release_qualification(
                        release_tag="v1.0.1",
                        package_spec="code-mower==1.0.1",
                        output_path=output_path,
                        dry_run=False,
                        repo_path=Path(tmpdir),
                        qualification_context="upgrade",
                        starting_version="1.0.0",
                        package_source="testpypi",
                    )

            self.assertEqual(
                mock_rehearsal.call_args.kwargs["preinstall_package_spec"],
                "code-mower==1.0.0",
            )
            self.assertEqual(
                mock_rehearsal.call_args.kwargs["pip_index_url"],
                "https://pypi.org/simple/",
            )
            self.assertEqual(
                mock_rehearsal.call_args.kwargs["pip_extra_index_urls"],
                (),
            )
            # allow_package_index stays on, so the existing bounded pip-install
            # retry behavior (see migration_rehearsal) applies unchanged.
            self.assertTrue(mock_rehearsal.call_args.kwargs["allow_package_index"])
            # The candidate itself goes through the closed two-stage flow:
            # TestPyPI as the only candidate index, production PyPI only for
            # the local artifact's own dependencies.
            self.assertEqual(
                mock_rehearsal.call_args.kwargs["candidate_index_url"],
                "https://test.pypi.org/simple/",
            )
            self.assertEqual(
                mock_rehearsal.call_args.kwargs["candidate_dependency_index_url"],
                "https://pypi.org/simple/",
            )
            self.assertEqual(result["outcome"], "pass")

    def test_cli_qualify_accepts_package_source_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "result.json"
            with mock.patch("code_mower.release_qualify._run_doctor_check") as mock_doctor:
                with mock.patch(
                    "code_mower.release_qualify.run_package_install_rehearsal"
                ) as mock_rehearsal:
                    mock_doctor.return_value = release_qualify.StepResult(
                        id="doctor", status="pass", elapsed_seconds=1.0,
                        warning_count=0, owner_action_count=0
                    )
                    mock_rehearsal.return_value = {"version": "code-mower 1.0.0"}
                    ret = release_qualify.main(
                        [
                            "qualify",
                            "--release-tag",
                            "v1.0.0",
                            "--package-spec",
                            "code-mower==1.0.0",
                            "--output",
                            str(output_path),
                            "--repo-path",
                            tmpdir,
                            "--execute",
                            "--package-source",
                            "testpypi",
                        ]
                    )
            self.assertEqual(ret, 0)
            self.assertEqual(
                mock_rehearsal.call_args.kwargs["pip_index_url"],
                "https://pypi.org/simple/",
            )
            self.assertEqual(
                mock_rehearsal.call_args.kwargs["candidate_index_url"],
                "https://test.pypi.org/simple/",
            )

    def test_cli_qualify_defaults_package_source_to_pypi(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "result.json"
            with mock.patch("code_mower.release_qualify._run_doctor_check") as mock_doctor:
                with mock.patch(
                    "code_mower.release_qualify.run_package_install_rehearsal"
                ) as mock_rehearsal:
                    mock_doctor.return_value = release_qualify.StepResult(
                        id="doctor", status="pass", elapsed_seconds=1.0,
                        warning_count=0, owner_action_count=0
                    )
                    mock_rehearsal.return_value = {"version": "code-mower 1.0.0"}
                    ret = release_qualify.main(
                        [
                            "qualify",
                            "--release-tag",
                            "v1.0.0",
                            "--package-spec",
                            "code-mower==1.0.0",
                            "--output",
                            str(output_path),
                            "--repo-path",
                            tmpdir,
                            "--execute",
                        ]
                    )
            self.assertEqual(ret, 0)
            self.assertEqual(mock_rehearsal.call_args.kwargs["pip_index_url"], "")


if __name__ == "__main__":
    unittest.main()
