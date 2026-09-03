from __future__ import annotations

import json
from pathlib import Path

from code_mower.cloud_client import (
    EVENT_SCHEMA,
    PRODUCTIVITY_EVENT_TYPE,
    PRODUCTIVITY_METRIC_UNITS,
    PRODUCTIVITY_METRICS_SCHEMA,
    PRODUCTIVITY_REQUIRED_DIMENSIONS,
    SAFE_EVENT_TYPES,
    CloudBundleError,
    normalize_event,
    validate_cloud_event,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "productivity_metrics_events.json"


def _fixture() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _event(metrics: dict[str, object]) -> dict[str, object]:
    return {
        "event_type": PRODUCTIVITY_EVENT_TYPE,
        "repo_slug": "owner/repo",
        "source": "unit-test",
        "status": "observed",
        "metrics": metrics,
        "dimensions": {
            "productivity_schema": PRODUCTIVITY_METRICS_SCHEMA,
            "repo_slug": "owner/repo",
            "window_start": "2026-09-03T00:00:00Z",
            "window_end": "2026-09-03T01:00:00Z",
            "window_granularity": "cycle",
            "aggregation_subject": "repo",
        },
    }


def test_productivity_summary_event_type_is_supported() -> None:
    assert PRODUCTIVITY_EVENT_TYPE == "productivity_summary"
    assert PRODUCTIVITY_EVENT_TYPE in SAFE_EVENT_TYPES


def test_productivity_fixtures_validate_and_stay_backward_compatible() -> None:
    payload = _fixture()
    for event in payload["legacy_events"]:
        validate_cloud_event(event)
        assert event["event_type"] in {"controller_decision", "reviewer_run"}
    for event in payload["productivity_summary_events"]:
        validate_cloud_event(event)
        assert event["schema"] == EVENT_SCHEMA
        assert event["dimensions"]["productivity_schema"] == PRODUCTIVITY_METRICS_SCHEMA
        for key in PRODUCTIVITY_REQUIRED_DIMENSIONS:
            assert event["dimensions"][key]
        for key in event["metrics"]:
            assert key in PRODUCTIVITY_METRIC_UNITS


def test_productivity_summary_defaults_to_code_mower_tool_provenance() -> None:
    event = normalize_event(_event({"merged_pr_count": 1}), PRODUCTIVITY_EVENT_TYPE)

    assert event["provider"] == "code-mower"
    assert event["tool"]["tool_name"] == "code-mower"
    assert event["tool"]["role"] == "reporter"
    assert event["tool"]["model_source"] == "not_applicable"


def test_productivity_summary_rejects_metric_drift() -> None:
    try:
        normalize_event(_event({"made_up_rate": 1}), PRODUCTIVITY_EVENT_TYPE)
    except CloudBundleError as exc:
        assert "unsupported productivity_summary metric" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected productivity metric drift rejection")


def test_productivity_summary_rejects_fractional_count() -> None:
    try:
        normalize_event(_event({"merged_pr_count": 1.5}), PRODUCTIVITY_EVENT_TYPE)
    except CloudBundleError as exc:
        assert "integer count" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected fractional count rejection")


def test_productivity_summary_fixture_stays_metadata_only() -> None:
    serialized = json.dumps(_fixture()).lower()

    forbidden = (
        "raw_diff",
        "transcript",
        "issue body",
        "source code",
        "auth output",
        "local path",
        "secret",
    )
    for phrase in forbidden:
        assert phrase not in serialized
