from __future__ import annotations

import json
import unittest
from unittest import mock

from code_mower.cloud_client import (
    PR_OUTCOME_EVENT_TYPE,
    PR_OUTCOME_SCHEMA,
    CloudBundleError,
    build_pr_outcome_event,
    run_gh_pr_list,
    validate_cloud_event,
)


def _builder_run_event(
    event_id: str,
    pr_number: str,
    cost_usd: float | None,
    provider: str = "devin",
) -> dict[str, object]:
    return {
        "schema": "code_mower.benchmarkEvent.v1",
        "event_id": event_id,
        "event_type": "builder_run",
        "created_at": "2026-09-03T10:00:00Z",
        "repo_slug": "owner/repo",
        "team_id": "team",
        "install_id": "install",
        "source": "unit-test",
        "provider": provider,
        "lens": "implementation",
        "status": "pr-opened",
        "tool": {
            "role": "builder",
            "tool_name": provider,
            "provider": provider,
        },
        "metrics": ({"cost_usd": cost_usd} if cost_usd is not None else {}),
        "dimensions": {
            "builder_provider": provider,
            "pr_number": pr_number,
        },
    }


def _reviewer_run_event(
    event_id: str,
    pr_number: str,
    cost_usd: float | None,
    lane: str = "claude-audit",
) -> dict[str, object]:
    return {
        "schema": "code_mower.benchmarkEvent.v1",
        "event_id": event_id,
        "event_type": "reviewer_run",
        "created_at": "2026-09-03T11:00:00Z",
        "repo_slug": "owner/repo",
        "team_id": "team",
        "install_id": "install",
        "source": "unit-test",
        "provider": "claude",
        "lens": lane,
        "status": "pass",
        "tool": {
            "role": "reviewer",
            "tool_name": "claude",
            "provider": "claude",
        },
        "metrics": ({"cost_usd": cost_usd} if cost_usd is not None else {}),
        "dimensions": {
            "lane": lane,
            "pr_number": pr_number,
        },
    }


class PrOutcomeCoverageTests(unittest.TestCase):
    def test_complete_coverage_when_all_attempts_report_cost(self) -> None:
        event = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="42",
            outcome="merged",
            opened_at="2026-09-03T10:00:00Z",
            merged_at="2026-09-03T12:00:00Z",
            run_events=[
                _builder_run_event("b1", "42", 0.15),
                _reviewer_run_event("r1", "42", 0.10),
            ],
            created_at="2026-09-03T13:00:00Z",
        )

        self.assertEqual(event["event_type"], PR_OUTCOME_EVENT_TYPE)
        self.assertEqual(event["dimensions"]["pr_outcome_schema"], PR_OUTCOME_SCHEMA)
        self.assertEqual(event["dimensions"]["cost_coverage"], "complete")
        self.assertEqual(event["metrics"]["cost_reported_run_count"], 2)
        self.assertEqual(event["metrics"]["cost_expected_run_count"], 2)
        self.assertEqual(event["metrics"]["cost_covered_pr_count"], 1)
        self.assertAlmostEqual(event["metrics"]["reported_cost_usd"], 0.25)
        self.assertNotIn("missing_cost_sources", event["dimensions"])
        validate_cloud_event(event)

    def test_partial_coverage_when_some_attempts_miss_cost(self) -> None:
        event = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="43",
            outcome="open",
            opened_at="2026-09-03T10:00:00Z",
            run_events=[
                _builder_run_event("b1", "43", 0.15),
                _reviewer_run_event("r1", "43", None),
            ],
            created_at="2026-09-03T13:00:00Z",
        )

        self.assertEqual(event["dimensions"]["cost_coverage"], "partial")
        self.assertEqual(event["metrics"]["cost_reported_run_count"], 1)
        self.assertEqual(event["metrics"]["cost_expected_run_count"], 2)
        self.assertEqual(event["metrics"]["cost_covered_pr_count"], 0)
        self.assertAlmostEqual(event["metrics"]["reported_cost_usd"], 0.15)
        self.assertEqual(
            event["dimensions"]["missing_cost_sources"],
            ["claude-audit"],
        )
        validate_cloud_event(event)

    def test_unknown_coverage_when_no_attempts_report_cost(self) -> None:
        event = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="44",
            outcome="merged",
            opened_at="2026-09-03T10:00:00Z",
            merged_at="2026-09-03T12:00:00Z",
            run_events=[
                _builder_run_event("b1", "44", None),
                _reviewer_run_event("r1", "44", None),
            ],
            created_at="2026-09-03T13:00:00Z",
        )

        self.assertEqual(event["dimensions"]["cost_coverage"], "unknown")
        self.assertEqual(event["metrics"]["cost_reported_run_count"], 0)
        self.assertEqual(event["metrics"]["cost_expected_run_count"], 2)
        self.assertEqual(event["metrics"]["cost_covered_pr_count"], 0)
        self.assertNotIn("reported_cost_usd", event["metrics"])
        self.assertEqual(
            set(event["dimensions"]["missing_cost_sources"]),
            {"devin", "claude-audit"},
        )
        validate_cloud_event(event)

    def test_no_run_events_yields_unknown_with_zero_counts(self) -> None:
        event = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="45",
            outcome="open",
            opened_at="2026-09-03T10:00:00Z",
            run_events=[],
            created_at="2026-09-03T13:00:00Z",
        )

        self.assertEqual(event["dimensions"]["cost_coverage"], "unknown")
        self.assertEqual(event["metrics"]["cost_reported_run_count"], 0)
        self.assertEqual(event["metrics"]["cost_expected_run_count"], 0)
        self.assertEqual(event["metrics"]["cost_covered_pr_count"], 0)
        self.assertNotIn("reported_cost_usd", event["metrics"])
        validate_cloud_event(event)

    def test_duplicate_event_ids_are_deduped(self) -> None:
        event = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="46",
            outcome="merged",
            opened_at="2026-09-03T10:00:00Z",
            merged_at="2026-09-03T12:00:00Z",
            run_events=[
                _builder_run_event("dup", "46", 0.15),
                _builder_run_event("dup", "46", 0.15),
            ],
            created_at="2026-09-03T13:00:00Z",
        )

        self.assertEqual(event["dimensions"]["cost_coverage"], "complete")
        self.assertEqual(event["metrics"]["cost_reported_run_count"], 1)
        self.assertEqual(event["metrics"]["cost_expected_run_count"], 1)
        self.assertAlmostEqual(event["metrics"]["reported_cost_usd"], 0.15)
        validate_cloud_event(event)

    def test_non_build_or_review_events_ignored(self) -> None:
        event = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="47",
            outcome="merged",
            opened_at="2026-09-03T10:00:00Z",
            merged_at="2026-09-03T12:00:00Z",
            run_events=[
                _builder_run_event("b1", "47", 0.15),
                {
                    "schema": "code_mower.benchmarkEvent.v1",
                    "event_id": "other",
                    "event_type": "dogfood_upload",
                    "repo_slug": "owner/repo",
                },
            ],
            created_at="2026-09-03T13:00:00Z",
        )

        self.assertEqual(event["dimensions"]["cost_coverage"], "complete")
        self.assertEqual(event["metrics"]["cost_expected_run_count"], 1)
        validate_cloud_event(event)

    def test_rejects_negative_cost(self) -> None:
        with self.assertRaises(CloudBundleError):
            build_pr_outcome_event(
                repo_slug="owner/repo",
                pr_number="48",
                outcome="merged",
                opened_at="2026-09-03T10:00:00Z",
                merged_at="2026-09-03T12:00:00Z",
                run_events=[
                    _builder_run_event("b1", "48", -0.01),
                ],
                created_at="2026-09-03T13:00:00Z",
            )


class GhPrListTests(unittest.TestCase):
    def test_run_gh_pr_list_parses_json_array(self) -> None:
        payload = [
            {
                "number": 101,
                "state": "MERGED",
                "createdAt": "2026-09-01T10:00:00Z",
                "closedAt": "2026-09-02T10:00:00Z",
                "mergedAt": "2026-09-02T09:00:00Z",
                "updatedAt": "2026-09-02T10:00:00Z",
                "url": "https://github.com/owner/repo/pull/101",
                "headRefName": "feature",
            }
        ]
        completed = mock.MagicMock()
        completed.returncode = 0
        completed.stdout = json.dumps(payload)
        completed.stderr = ""

        with mock.patch("subprocess.run", return_value=completed) as run:
            result = run_gh_pr_list(
                repo_slug="owner/repo",
                limit=10,
                repo_path=__import__("pathlib").Path("."),
            )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["number"], 101)
        run.assert_called_once()
        args = run.call_args[0][0]
        self.assertIn("pr", args)
        self.assertIn("--state", args)
        self.assertIn("all", args)

    def test_run_gh_pr_list_raises_on_failure(self) -> None:
        completed = mock.MagicMock()
        completed.returncode = 1
        completed.stdout = ""
        completed.stderr = "auth failed"

        with mock.patch("subprocess.run", return_value=completed):
            with self.assertRaises(CloudBundleError):
                run_gh_pr_list(
                    repo_slug="owner/repo",
                    limit=10,
                    repo_path=__import__("pathlib").Path("."),
                )


if __name__ == "__main__":
    unittest.main()
