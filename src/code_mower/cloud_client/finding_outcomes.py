"""Reviewer finding outcome contract for CodeMower.com uploads."""

from __future__ import annotations

import datetime as dt
from typing import Any, Mapping

from .errors import CloudBundleError


REVIEWER_FINDING_OUTCOME_EVENT_TYPE = "reviewer_finding_outcome"
REVIEWER_FINDING_OUTCOME_SCHEMA = "code_mower.reviewerFindingOutcome.v1"
FINDING_DISPOSITION_VALUES = (
    "accepted_fixed",
    "false_positive",
    "accepted_risk",
    "owner_decision",
    "duplicate",
    "infrastructure",
    "insufficient_context",
)
FINDING_SEVERITY_VALUES = ("blocker", "major", "minor", "info")
FINDING_SOURCE_VALUES = ("automated", "manual")
REVIEWER_FINDING_OUTCOME_DIMENSIONS = (
    "reviewer_finding_outcome_schema",
    "finding_id",
    "repo_slug",
    "pr_number",
    "head_sha",
    "lane_id",
    "severity",
    "disposition",
    "observed_at",
    "resolved_at",
    "fix_commit_sha",
    "decision_id",
    "source",
)


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CloudBundleError(
            f"reviewer_finding_outcome {field} must be a non-empty string"
        )
    return value.strip()


def _optional_text(value: object) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise CloudBundleError("text field must be string when present")
    stripped = value.strip()
    return stripped if stripped else None


def _timestamp(value: object, field: str) -> dt.datetime:
    text = _required_text(value, field)
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CloudBundleError(
            f"reviewer_finding_outcome {field} must be an ISO 8601 timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise CloudBundleError(
            f"reviewer_finding_outcome {field} must include a UTC offset"
        )
    return parsed


def validate_reviewer_finding_outcome_payload(event: Mapping[str, Any]) -> None:
    """Validate one metadata-only reviewer finding outcome.

    This validates blocker-scoped disposition evidence without finding prose,
    source, diffs, transcripts, issue bodies, finding titles/details, file paths,
    raw stdout/stderr, auth output, local paths, or secrets.
    """

    _required_text(event.get("repo_slug"), "repo_slug")
    dimensions = event.get("dimensions")
    metrics = event.get("metrics")
    if not isinstance(dimensions, Mapping):
        raise CloudBundleError("reviewer_finding_outcome dimensions must be an object")
    if not isinstance(metrics, Mapping):
        raise CloudBundleError("reviewer_finding_outcome metrics must be an object")
    if dimensions.get("reviewer_finding_outcome_schema") != REVIEWER_FINDING_OUTCOME_SCHEMA:
        raise CloudBundleError(
            "reviewer_finding_outcome dimensions.reviewer_finding_outcome_schema "
            f"must be {REVIEWER_FINDING_OUTCOME_SCHEMA!r}"
        )
    unknown_dimensions = [
        str(key) for key in dimensions if key not in REVIEWER_FINDING_OUTCOME_DIMENSIONS
    ]
    if unknown_dimensions:
        raise CloudBundleError(
            f"unsupported reviewer_finding_outcome dimension {unknown_dimensions[0]!r}"
        )

    finding_id = _required_text(dimensions.get("finding_id"), "dimension 'finding_id'")
    if len(finding_id) > 160:
        raise CloudBundleError(
            "reviewer_finding_outcome dimension 'finding_id' must be at most 160 characters"
        )

    repo_slug = _required_text(dimensions.get("repo_slug"), "dimension 'repo_slug'")
    if "/" not in repo_slug:
        raise CloudBundleError(
            "reviewer_finding_outcome dimension 'repo_slug' must be in OWNER/REPO format"
        )

    pr_number = _required_text(dimensions.get("pr_number"), "dimension 'pr_number'")
    if not pr_number.isdigit() or int(pr_number) < 1:
        raise CloudBundleError(
            "reviewer_finding_outcome dimension 'pr_number' must be a positive integer string"
        )

    head_sha = _required_text(dimensions.get("head_sha"), "dimension 'head_sha'")
    if len(head_sha) < 7 or len(head_sha) > 64:
        raise CloudBundleError(
            "reviewer_finding_outcome dimension 'head_sha' must be 7-64 characters"
        )

    lane_id = _required_text(dimensions.get("lane_id"), "dimension 'lane_id'")
    if len(lane_id) > 80:
        raise CloudBundleError(
            "reviewer_finding_outcome dimension 'lane_id' must be at most 80 characters"
        )

    severity = _required_text(dimensions.get("severity"), "dimension 'severity'")
    if severity not in FINDING_SEVERITY_VALUES:
        raise CloudBundleError(
            f"unsupported reviewer_finding_outcome severity {severity!r}"
        )

    disposition = _required_text(dimensions.get("disposition"), "dimension 'disposition'")
    if disposition not in FINDING_DISPOSITION_VALUES:
        raise CloudBundleError(
            f"unsupported reviewer_finding_outcome disposition {disposition!r}"
        )

    source = _required_text(dimensions.get("source"), "dimension 'source'")
    if source not in FINDING_SOURCE_VALUES:
        raise CloudBundleError(
            f"unsupported reviewer_finding_outcome source {source!r}"
        )

    observed_at = _timestamp(dimensions.get("observed_at"), "dimension 'observed_at'")
    resolved_at_raw = dimensions.get("resolved_at")
    if resolved_at_raw not in (None, ""):
        resolved_at = _timestamp(resolved_at_raw, "dimension 'resolved_at'")
        if resolved_at < observed_at:
            raise CloudBundleError(
                "reviewer_finding_outcome dimension 'resolved_at' cannot precede observed_at"
            )

    fix_commit_sha = _optional_text(dimensions.get("fix_commit_sha"))
    if fix_commit_sha and (len(fix_commit_sha) < 7 or len(fix_commit_sha) > 64):
        raise CloudBundleError(
            "reviewer_finding_outcome dimension 'fix_commit_sha' must be 7-64 characters when present"
        )

    decision_id = _optional_text(dimensions.get("decision_id"))
    if decision_id and len(decision_id) > 160:
        raise CloudBundleError(
            "reviewer_finding_outcome dimension 'decision_id' must be at most 160 characters when present"
        )

    if disposition == "accepted_fixed" and not fix_commit_sha:
        raise CloudBundleError(
            "reviewer_finding_outcome disposition 'accepted_fixed' requires fix_commit_sha"
        )

    if disposition == "owner_decision" and not decision_id:
        raise CloudBundleError(
            "reviewer_finding_outcome disposition 'owner_decision' requires decision_id"
        )

    allowed_metrics = {"finding_outcome_count"}
    unknown_metrics = [str(key) for key in metrics if key not in allowed_metrics]
    if unknown_metrics:
        raise CloudBundleError(
            f"unsupported reviewer_finding_outcome metric {unknown_metrics[0]!r}"
        )

    outcome_count = metrics.get("finding_outcome_count")
    if outcome_count != 1:
        raise CloudBundleError(
            "reviewer_finding_outcome metric 'finding_outcome_count' must equal 1"
        )
