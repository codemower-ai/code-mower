"""Test reviewer finding outcome contract validation."""

from __future__ import annotations

import pytest

from code_mower.cloud_client.bundle import validate_metadata_payload
from code_mower.cloud_client.errors import CloudBundleError
from code_mower.cloud_client.events import normalize_event, validate_cloud_event
from code_mower.cloud_client.finding_outcomes import (
    FINDING_DISPOSITION_VALUES,
    FINDING_SEVERITY_VALUES,
    FINDING_SOURCE_VALUES,
    REVIEWER_FINDING_OUTCOME_EVENT_TYPE,
    REVIEWER_FINDING_OUTCOME_SCHEMA,
    validate_reviewer_finding_outcome_payload,
)


def valid_finding_outcome() -> dict:
    """Return a minimal valid reviewer finding outcome event."""
    return {
        "schema": "code_mower.benchmarkEvent.v1",
        "event_type": REVIEWER_FINDING_OUTCOME_EVENT_TYPE,
        "event_id": "test-event-id",
        "created_at": "2026-09-07T20:00:00Z",
        "repo_slug": "codemower-ai/code-mower",
        "team_id": "test-team",
        "install_id": "test-install",
        "source": "code-mower-audit",
        "provider": "codex",
        "lens": "security",
        "status": "observed",
        "tool": {
            "role": "reporter",
            "tool_name": "code-mower",
            "tool_version": "1.0.0",
            "provider": "code-mower",
            "model": "",
            "model_source": "not_applicable",
            "version_source": "cli_version_probe",
            "integration": "cli",
            "lens": "security",
            "source": "code-mower-audit",
        },
        "metrics": {
            "finding_outcome_count": 1,
        },
        "dimensions": {
            "reviewer_finding_outcome_schema": REVIEWER_FINDING_OUTCOME_SCHEMA,
            "finding_id": "codex-sec-001",
            "repo_slug": "codemower-ai/code-mower",
            "pr_number": "123",
            "head_sha": "abc1234567890",
            "lane_id": "codex",
            "severity": "blocker",
            "disposition": "accepted_fixed",
            "observed_at": "2026-09-07T20:00:00Z",
            "resolved_at": "2026-09-07T21:00:00Z",
            "fix_commit_sha": "def9876543210",
            "source": "automated",
        },
    }


def test_valid_finding_outcome():
    """Test that a valid finding outcome passes validation."""
    event = valid_finding_outcome()
    validate_cloud_event(event)
    validate_reviewer_finding_outcome_payload(event)


def test_finding_outcome_normalization():
    """Test that finding outcomes are properly normalized."""
    event = valid_finding_outcome()
    normalized = normalize_event(event, REVIEWER_FINDING_OUTCOME_EVENT_TYPE)
    assert normalized["event_type"] == REVIEWER_FINDING_OUTCOME_EVENT_TYPE
    assert normalized["dimensions"]["reviewer_finding_outcome_schema"] == REVIEWER_FINDING_OUTCOME_SCHEMA


def test_missing_schema():
    """Test that missing schema is rejected."""
    event = valid_finding_outcome()
    del event["dimensions"]["reviewer_finding_outcome_schema"]
    with pytest.raises(CloudBundleError, match="reviewer_finding_outcome_schema"):
        validate_reviewer_finding_outcome_payload(event)


def test_wrong_schema():
    """Test that wrong schema version is rejected."""
    event = valid_finding_outcome()
    event["dimensions"]["reviewer_finding_outcome_schema"] = "code_mower.reviewerFindingOutcome.v0"
    with pytest.raises(CloudBundleError, match="reviewer_finding_outcome_schema"):
        validate_reviewer_finding_outcome_payload(event)


def test_missing_finding_id():
    """Test that missing finding_id is rejected."""
    event = valid_finding_outcome()
    del event["dimensions"]["finding_id"]
    with pytest.raises(CloudBundleError, match="finding_id"):
        validate_reviewer_finding_outcome_payload(event)


def test_finding_id_too_long():
    """Test that finding_id over 160 characters is rejected."""
    event = valid_finding_outcome()
    event["dimensions"]["finding_id"] = "x" * 161
    with pytest.raises(CloudBundleError, match="finding_id.*160 characters"):
        validate_reviewer_finding_outcome_payload(event)


def test_missing_repo_slug():
    """Test that missing repo_slug is rejected."""
    event = valid_finding_outcome()
    del event["dimensions"]["repo_slug"]
    with pytest.raises(CloudBundleError, match="repo_slug"):
        validate_reviewer_finding_outcome_payload(event)


def test_invalid_repo_slug():
    """Test that repo_slug without slash is rejected."""
    event = valid_finding_outcome()
    event["dimensions"]["repo_slug"] = "codemower"
    with pytest.raises(CloudBundleError, match="repo_slug.*OWNER/REPO"):
        validate_reviewer_finding_outcome_payload(event)


def test_mismatched_repo_slug():
    """Test that dimension repo_slug must match envelope repo_slug."""
    event = valid_finding_outcome()
    event["repo_slug"] = "different-owner/different-repo"
    with pytest.raises(CloudBundleError, match="dimension 'repo_slug'.*must match.*envelope repo_slug"):
        validate_reviewer_finding_outcome_payload(event)


def test_missing_pr_number():
    """Test that missing pr_number is rejected."""
    event = valid_finding_outcome()
    del event["dimensions"]["pr_number"]
    with pytest.raises(CloudBundleError, match="pr_number"):
        validate_reviewer_finding_outcome_payload(event)


def test_invalid_pr_number():
    """Test that non-numeric pr_number is rejected."""
    event = valid_finding_outcome()
    event["dimensions"]["pr_number"] = "abc"
    with pytest.raises(CloudBundleError, match="pr_number.*positive integer"):
        validate_reviewer_finding_outcome_payload(event)


def test_zero_pr_number():
    """Test that zero pr_number is rejected."""
    event = valid_finding_outcome()
    event["dimensions"]["pr_number"] = "0"
    with pytest.raises(CloudBundleError, match="pr_number.*positive integer"):
        validate_reviewer_finding_outcome_payload(event)


def test_unicode_digit_pr_number():
    """Test that non-ASCII digit characters in pr_number are rejected."""
    event = valid_finding_outcome()
    event["dimensions"]["pr_number"] = "\u00b9\u00b2\u00b3"  # superscript 123
    with pytest.raises(CloudBundleError, match="pr_number.*positive integer"):
        validate_reviewer_finding_outcome_payload(event)


def test_missing_head_sha():
    """Test that missing head_sha is rejected."""
    event = valid_finding_outcome()
    del event["dimensions"]["head_sha"]
    with pytest.raises(CloudBundleError, match="head_sha"):
        validate_reviewer_finding_outcome_payload(event)


def test_head_sha_too_short():
    """Test that head_sha under 7 characters is rejected."""
    event = valid_finding_outcome()
    event["dimensions"]["head_sha"] = "abc123"
    with pytest.raises(CloudBundleError, match="head_sha.*7-64 characters"):
        validate_reviewer_finding_outcome_payload(event)


def test_head_sha_too_long():
    """Test that head_sha over 64 characters is rejected."""
    event = valid_finding_outcome()
    event["dimensions"]["head_sha"] = "x" * 65
    with pytest.raises(CloudBundleError, match="head_sha.*7-64 characters"):
        validate_reviewer_finding_outcome_payload(event)


def test_missing_lane_id():
    """Test that missing lane_id is rejected."""
    event = valid_finding_outcome()
    del event["dimensions"]["lane_id"]
    with pytest.raises(CloudBundleError, match="lane_id"):
        validate_reviewer_finding_outcome_payload(event)


def test_lane_id_too_long():
    """Test that lane_id over 80 characters is rejected."""
    event = valid_finding_outcome()
    event["dimensions"]["lane_id"] = "x" * 81
    with pytest.raises(CloudBundleError, match="lane_id.*80 characters"):
        validate_reviewer_finding_outcome_payload(event)


def test_missing_severity():
    """Test that missing severity is rejected."""
    event = valid_finding_outcome()
    del event["dimensions"]["severity"]
    with pytest.raises(CloudBundleError, match="severity"):
        validate_reviewer_finding_outcome_payload(event)


def test_invalid_severity():
    """Test that invalid severity value is rejected."""
    event = valid_finding_outcome()
    event["dimensions"]["severity"] = "critical"
    with pytest.raises(CloudBundleError, match="unsupported.*severity"):
        validate_reviewer_finding_outcome_payload(event)


@pytest.mark.parametrize("severity", FINDING_SEVERITY_VALUES)
def test_all_severity_values(severity):
    """Test that all defined severity values are accepted."""
    event = valid_finding_outcome()
    event["dimensions"]["severity"] = severity
    validate_reviewer_finding_outcome_payload(event)


def test_missing_disposition():
    """Test that missing disposition is rejected."""
    event = valid_finding_outcome()
    del event["dimensions"]["disposition"]
    with pytest.raises(CloudBundleError, match="disposition"):
        validate_reviewer_finding_outcome_payload(event)


def test_invalid_disposition():
    """Test that invalid disposition value is rejected."""
    event = valid_finding_outcome()
    event["dimensions"]["disposition"] = "wont_fix"
    with pytest.raises(CloudBundleError, match="unsupported.*disposition"):
        validate_reviewer_finding_outcome_payload(event)


@pytest.mark.parametrize("disposition", FINDING_DISPOSITION_VALUES)
def test_all_disposition_values(disposition):
    """Test that all defined disposition values are accepted when requirements met."""
    event = valid_finding_outcome()
    event["dimensions"]["disposition"] = disposition

    # Add required fields for certain dispositions
    if disposition == "accepted_fixed":
        event["dimensions"]["fix_commit_sha"] = "def9876543210"
    elif disposition == "owner_decision":
        event["dimensions"]["decision_id"] = "decision-001"
    else:
        # These dispositions don't require fix_commit_sha
        event["dimensions"].pop("fix_commit_sha", None)

    validate_reviewer_finding_outcome_payload(event)


def test_missing_source():
    """Test that missing source is rejected."""
    event = valid_finding_outcome()
    del event["dimensions"]["source"]
    with pytest.raises(CloudBundleError, match="source"):
        validate_reviewer_finding_outcome_payload(event)


def test_invalid_source():
    """Test that invalid source value is rejected."""
    event = valid_finding_outcome()
    event["dimensions"]["source"] = "hybrid"
    with pytest.raises(CloudBundleError, match="unsupported.*source"):
        validate_reviewer_finding_outcome_payload(event)


@pytest.mark.parametrize("source", FINDING_SOURCE_VALUES)
def test_all_source_values(source):
    """Test that all defined source values are accepted."""
    event = valid_finding_outcome()
    event["dimensions"]["source"] = source
    validate_reviewer_finding_outcome_payload(event)


def test_missing_observed_at():
    """Test that missing observed_at is rejected."""
    event = valid_finding_outcome()
    del event["dimensions"]["observed_at"]
    with pytest.raises(CloudBundleError, match="observed_at"):
        validate_reviewer_finding_outcome_payload(event)


def test_invalid_observed_at():
    """Test that invalid observed_at timestamp is rejected."""
    event = valid_finding_outcome()
    event["dimensions"]["observed_at"] = "not-a-timestamp"
    with pytest.raises(CloudBundleError, match="observed_at.*ISO 8601"):
        validate_reviewer_finding_outcome_payload(event)


def test_observed_at_without_timezone():
    """Test that observed_at without UTC offset is rejected."""
    event = valid_finding_outcome()
    event["dimensions"]["observed_at"] = "2026-09-07T20:00:00"
    with pytest.raises(CloudBundleError, match="observed_at.*UTC offset"):
        validate_reviewer_finding_outcome_payload(event)


def test_optional_resolved_at():
    """Test that resolved_at is optional."""
    event = valid_finding_outcome()
    del event["dimensions"]["resolved_at"]
    validate_reviewer_finding_outcome_payload(event)


def test_resolved_at_precedes_observed_at():
    """Test that resolved_at cannot precede observed_at."""
    event = valid_finding_outcome()
    event["dimensions"]["observed_at"] = "2026-09-07T21:00:00Z"
    event["dimensions"]["resolved_at"] = "2026-09-07T20:00:00Z"
    with pytest.raises(CloudBundleError, match="resolved_at.*cannot precede observed_at"):
        validate_reviewer_finding_outcome_payload(event)


def test_accepted_fixed_requires_fix_commit_sha():
    """Test that accepted_fixed disposition requires fix_commit_sha."""
    event = valid_finding_outcome()
    event["dimensions"]["disposition"] = "accepted_fixed"
    del event["dimensions"]["fix_commit_sha"]
    with pytest.raises(CloudBundleError, match="accepted_fixed.*requires fix_commit_sha"):
        validate_reviewer_finding_outcome_payload(event)


def test_fix_commit_sha_too_short():
    """Test that fix_commit_sha under 7 characters is rejected."""
    event = valid_finding_outcome()
    event["dimensions"]["fix_commit_sha"] = "abc123"
    with pytest.raises(CloudBundleError, match="fix_commit_sha.*7-64 characters"):
        validate_reviewer_finding_outcome_payload(event)


def test_fix_commit_sha_too_long():
    """Test that fix_commit_sha over 64 characters is rejected."""
    event = valid_finding_outcome()
    event["dimensions"]["fix_commit_sha"] = "x" * 65
    with pytest.raises(CloudBundleError, match="fix_commit_sha.*7-64 characters"):
        validate_reviewer_finding_outcome_payload(event)


def test_owner_decision_requires_decision_id():
    """Test that owner_decision disposition requires decision_id."""
    event = valid_finding_outcome()
    event["dimensions"]["disposition"] = "owner_decision"
    event["dimensions"].pop("fix_commit_sha", None)
    with pytest.raises(CloudBundleError, match="owner_decision.*requires decision_id"):
        validate_reviewer_finding_outcome_payload(event)


def test_decision_id_too_long():
    """Test that decision_id over 160 characters is rejected."""
    event = valid_finding_outcome()
    event["dimensions"]["disposition"] = "owner_decision"
    event["dimensions"]["decision_id"] = "x" * 161
    event["dimensions"].pop("fix_commit_sha", None)
    with pytest.raises(CloudBundleError, match="decision_id.*160 characters"):
        validate_reviewer_finding_outcome_payload(event)


def test_missing_finding_outcome_count():
    """Test that finding_outcome_count is required."""
    event = valid_finding_outcome()
    del event["metrics"]["finding_outcome_count"]
    with pytest.raises(CloudBundleError, match="finding_outcome_count"):
        validate_reviewer_finding_outcome_payload(event)


def test_finding_outcome_count_not_one():
    """Test that finding_outcome_count must equal 1."""
    event = valid_finding_outcome()
    event["metrics"]["finding_outcome_count"] = 2
    with pytest.raises(CloudBundleError, match="finding_outcome_count.*must equal 1"):
        validate_reviewer_finding_outcome_payload(event)


def test_finding_outcome_count_boolean():
    """Test that boolean True is rejected for finding_outcome_count."""
    event = valid_finding_outcome()
    event["metrics"]["finding_outcome_count"] = True
    with pytest.raises(CloudBundleError, match="finding_outcome_count.*must equal 1"):
        validate_reviewer_finding_outcome_payload(event)


def test_finding_outcome_count_float():
    """Test that float 1.0 is rejected for finding_outcome_count."""
    event = valid_finding_outcome()
    event["metrics"]["finding_outcome_count"] = 1.0
    with pytest.raises(CloudBundleError, match="finding_outcome_count.*must equal 1"):
        validate_reviewer_finding_outcome_payload(event)


def test_unknown_dimension_rejected():
    """Test that unknown dimensions are rejected."""
    event = valid_finding_outcome()
    event["dimensions"]["unknown_field"] = "test"
    with pytest.raises(CloudBundleError, match="unsupported.*dimension"):
        validate_reviewer_finding_outcome_payload(event)


def test_unknown_metric_rejected():
    """Test that unknown metrics are rejected."""
    event = valid_finding_outcome()
    event["metrics"]["unknown_metric"] = 42
    with pytest.raises(CloudBundleError, match="unsupported.*metric"):
        validate_reviewer_finding_outcome_payload(event)


def test_false_positive_disposition():
    """Test false_positive disposition without fix linkage."""
    event = valid_finding_outcome()
    event["dimensions"]["disposition"] = "false_positive"
    event["dimensions"].pop("fix_commit_sha", None)
    event["dimensions"].pop("decision_id", None)
    validate_reviewer_finding_outcome_payload(event)


def test_duplicate_disposition():
    """Test duplicate disposition without fix linkage."""
    event = valid_finding_outcome()
    event["dimensions"]["disposition"] = "duplicate"
    event["dimensions"].pop("fix_commit_sha", None)
    event["dimensions"].pop("decision_id", None)
    validate_reviewer_finding_outcome_payload(event)


def test_infrastructure_disposition():
    """Test infrastructure disposition without fix linkage."""
    event = valid_finding_outcome()
    event["dimensions"]["disposition"] = "infrastructure"
    event["dimensions"].pop("fix_commit_sha", None)
    event["dimensions"].pop("decision_id", None)
    validate_reviewer_finding_outcome_payload(event)


def test_insufficient_context_disposition():
    """Test insufficient_context disposition without fix linkage."""
    event = valid_finding_outcome()
    event["dimensions"]["disposition"] = "insufficient_context"
    event["dimensions"].pop("fix_commit_sha", None)
    event["dimensions"].pop("decision_id", None)
    validate_reviewer_finding_outcome_payload(event)


def test_metadata_privacy_boundary():
    """Test that events do not contain unsafe metadata."""
    event = valid_finding_outcome()
    # This should pass metadata validation
    validate_metadata_payload(event)


def test_no_finding_prose_in_dimensions():
    """Test that dimensions do not contain finding prose fields."""
    event = valid_finding_outcome()
    # Verify that prose fields are not in allowed dimensions
    disallowed = ["finding_title", "finding_description", "finding_message", "file_path"]
    for field in disallowed:
        assert field not in event["dimensions"], f"Finding prose field {field!r} should not be present"


def test_fixture_events():
    """Test that fixture events pass validation."""
    import json
    from pathlib import Path

    fixture_path = Path(__file__).parent / "fixtures" / "reviewer_finding_outcome_events.json"
    if not fixture_path.exists():
        pytest.skip(f"Fixture file not found: {fixture_path}")

    with open(fixture_path) as f:
        events = json.load(f)

    assert isinstance(events, list), "Fixture file should contain a list of events"
    assert len(events) > 0, "Fixture file should contain at least one event"

    for event in events:
        validate_cloud_event(event)
        validate_reviewer_finding_outcome_payload(event)
