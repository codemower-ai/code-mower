import unittest

from code_mower.doctor_checks.github_actions_cost_summary import (
    _approx_run_seconds,
    _is_metadata_workflow,
    summarize_actions_cost_runs,
)


class GitHubActionsCostSummaryTests(unittest.TestCase):
    def test_summarizes_metadata_and_scheduled_runs(self) -> None:
        summary = summarize_actions_cost_runs(
            [
                {
                    "name": "codex-audit-labeler.yml",
                    "path": ".github/workflows/codex-audit-labeler.yml",
                    "event": "schedule",
                    "run_started_at": "2026-06-17T10:00:00Z",
                    "updated_at": "2026-06-17T10:02:30Z",
                },
                {
                    "name": "CI",
                    "path": ".github/workflows/ci.yml",
                    "event": "pull_request",
                    "run_started_at": "2026-06-17T11:00:00Z",
                    "updated_at": "2026-06-17T11:01:00Z",
                },
                "ignored",
            ],
            slug="owner/repo",
            private=True,
            bounded_limit=10,
        )

        self.assertEqual(summary["repo"], "owner/repo")
        self.assertEqual(summary["private"], True)
        self.assertEqual(summary["sampled_runs"], 2)
        self.assertEqual(summary["metadata_workflow_runs"], 1)
        self.assertEqual(summary["metadata_workflow_share"], 0.5)
        self.assertEqual(summary["schedule_runs"], 1)
        self.assertEqual(summary["approx_total_minutes"], 3.5)
        self.assertEqual(
            summary["top_metadata_workflows"],
            [
                {
                    "workflow": "codex-audit-labeler.yml",
                    "runs": 1,
                    "approx_minutes": 2.5,
                }
            ],
        )

    def test_invalid_or_reversed_timestamps_count_as_zero_seconds(self) -> None:
        self.assertEqual(
            _approx_run_seconds(
                {
                    "run_started_at": "2026-06-17T10:01:00Z",
                    "updated_at": "2026-06-17T10:00:00Z",
                }
            ),
            0.0,
        )
        self.assertEqual(
            _approx_run_seconds(
                {
                    "run_started_at": "not-a-date",
                    "updated_at": "2026-06-17T10:00:00Z",
                }
            ),
            0.0,
        )

    def test_metadata_workflow_marker_matches_name_or_path(self) -> None:
        self.assertTrue(
            _is_metadata_workflow(
                "Regular title",
                ".github/workflows/claude-audit-labeler.yml",
            )
        )
        self.assertFalse(_is_metadata_workflow("CI", ".github/workflows/ci.yml"))


if __name__ == "__main__":
    unittest.main()
