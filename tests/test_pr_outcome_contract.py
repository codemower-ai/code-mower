from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

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


def test_pr_outcome_fixtures_cover_cost_states_and_legacy_uploads() -> None:
    payload = _fixture()
    for event in payload["legacy_events"]:
        validate_cloud_event(event)

    coverage = set()
    for event in payload["pr_outcome_events"]:
        validate_cloud_event(event)
        coverage.add(event["dimensions"]["cost_coverage"])
        assert event["event_type"] == PR_OUTCOME_EVENT_TYPE
        assert event["dimensions"]["pr_outcome_schema"] == PR_OUTCOME_SCHEMA

    assert coverage == {"complete", "partial", "unknown"}
    unknown = payload["pr_outcome_events"][2]
    assert "reported_cost_usd" not in unknown["metrics"]


def test_pr_outcome_normalizes_with_reporter_provenance() -> None:
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

    assert PR_OUTCOME_EVENT_TYPE in SAFE_EVENT_TYPES
    assert event["tool"]["role"] == "reporter"
    assert event["tool"]["tool_name"] == "code-mower"


@pytest.mark.parametrize(
    ("coverage", "metrics", "message"),
    [
        ("complete", {"reported_cost_usd": 0.2, "cost_reported_run_count": 1, "cost_expected_run_count": 2}, "complete"),
        ("partial", {"reported_cost_usd": 0.2, "cost_reported_run_count": 2, "cost_expected_run_count": 2}, "partial"),
        ("unknown", {"reported_cost_usd": 0.0, "cost_reported_run_count": 0, "cost_expected_run_count": 2}, "unknown"),
    ],
)
def test_pr_outcome_rejects_inconsistent_cost_coverage(
    coverage: str,
    metrics: dict[str, object],
    message: str,
) -> None:
    event = copy.deepcopy(_fixture()["pr_outcome_events"][2])
    event["dimensions"]["cost_coverage"] = coverage
    event["metrics"].update(metrics)

    with pytest.raises(CloudBundleError, match=message):
        validate_cloud_event(event)


def test_pr_outcome_fixture_stays_metadata_only() -> None:
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
        assert phrase not in serialized


def test_pr_outcome_rejects_undeclared_prose_channels() -> None:
    event = copy.deepcopy(_fixture()["pr_outcome_events"][0])
    event["dimensions"]["issue_body"] = "untrusted prose"

    with pytest.raises(CloudBundleError, match="unsupported pr_outcome dimension"):
        validate_cloud_event(event)
