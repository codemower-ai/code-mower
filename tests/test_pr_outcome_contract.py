from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from code_mower.cloud_client import (
    PR_OUTCOME_EVENT_TYPE,
    PR_OUTCOME_SCHEMA,
    SAFE_EVENT_TYPES,
    CloudBundleError,
    normalize_event,
    validate_cloud_event,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "pr_outcome_events.json"


def _fixture() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


class PrOutcomeContractTests(unittest.TestCase):
    def test_fixtures_cover_cost_states_and_legacy_uploads(self) -> None:
        payload = _fixture()
        for event in payload["legacy_events"]:
            validate_cloud_event(event)

        coverage = set()
        for event in payload["pr_outcome_events"]:
            validate_cloud_event(event)
            coverage.add(event["dimensions"]["cost_coverage"])
            self.assertEqual(event["event_type"], PR_OUTCOME_EVENT_TYPE)
            self.assertEqual(event["dimensions"]["pr_outcome_schema"], PR_OUTCOME_SCHEMA)

        self.assertEqual(coverage, {"complete", "partial", "unknown"})
        unknown = payload["pr_outcome_events"][2]
        self.assertNotIn("reported_cost_usd", unknown["metrics"])

    def test_normalizes_with_reporter_provenance(self) -> None:
        event = normalize_event(
            {
                "repo_slug": "owner/repo",
                "source": "unit-test",
                "status": "observed",
                "metrics": {"pr_count": 1, "cost_covered_pr_count": 0},
                "dimensions": {
                    "pr_outcome_schema": PR_OUTCOME_SCHEMA,
                    "pr_number": "104",
                    "opened_at": "2026-09-03T12:00:00Z",
                    "outcome": "open",
                    "cost_coverage": "unknown",
                },
            },
            PR_OUTCOME_EVENT_TYPE,
        )

        self.assertIn(PR_OUTCOME_EVENT_TYPE, SAFE_EVENT_TYPES)
        self.assertEqual(event["tool"]["role"], "reporter")
        self.assertEqual(event["tool"]["tool_name"], "code-mower")

    def test_rejects_inconsistent_cost_coverage(self) -> None:
        cases = (
            (
                "complete",
                {
                    "reported_cost_usd": 0.2,
                    "cost_reported_run_count": 1,
                    "cost_expected_run_count": 2,
                },
            ),
            (
                "partial",
                {
                    "reported_cost_usd": 0.2,
                    "cost_reported_run_count": 2,
                    "cost_expected_run_count": 2,
                },
            ),
            (
                "unknown",
                {
                    "reported_cost_usd": 0.0,
                    "cost_reported_run_count": 0,
                    "cost_expected_run_count": 2,
                },
            ),
        )
        for coverage, metrics in cases:
            with self.subTest(coverage=coverage):
                event = copy.deepcopy(_fixture()["pr_outcome_events"][2])
                event["dimensions"]["cost_coverage"] = coverage
                event["metrics"].update(metrics)

                with self.assertRaisesRegex(CloudBundleError, coverage):
                    validate_cloud_event(event)

    def test_fixture_stays_metadata_only(self) -> None:
        serialized = json.dumps(_fixture()).lower()
        for phrase in (
            "raw_diff",
            "transcript",
            "issue body",
            "source code",
            "auth output",
            "local path",
            "secret",
        ):
            self.assertNotIn(phrase, serialized)

    def test_rejects_undeclared_prose_channels(self) -> None:
        event = copy.deepcopy(_fixture()["pr_outcome_events"][0])
        event["dimensions"]["issue_body"] = "untrusted prose"

        with self.assertRaisesRegex(CloudBundleError, "unsupported pr_outcome dimension"):
            validate_cloud_event(event)

    def test_rejects_revert_before_merge(self) -> None:
        event = copy.deepcopy(_fixture()["pr_outcome_events"][0])
        event["dimensions"].update(
            {
                "outcome": "reverted",
                "merged_at": "2026-09-03T12:55:00Z",
                "reverted_at": "2026-09-03T12:54:00Z",
            }
        )

        with self.assertRaisesRegex(CloudBundleError, "cannot precede merged_at"):
            validate_cloud_event(event)
