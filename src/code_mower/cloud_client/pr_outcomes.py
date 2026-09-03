"""Atomic pull-request outcome contract for CodeMower.com uploads."""

from __future__ import annotations

import datetime as dt
import math
from typing import Any, Mapping

from .errors import CloudBundleError


PR_OUTCOME_EVENT_TYPE = "pr_outcome"
PR_OUTCOME_SCHEMA = "code_mower.prOutcome.v1"
PR_OUTCOME_VALUES = ("open", "merged", "closed_unmerged", "reverted")
PR_COST_COVERAGE_VALUES = ("complete", "partial", "unknown")
PR_OUTCOME_COUNT_METRICS = (
    "pr_count",
    "fix_round_count",
    "reviewer_catch_count",
    "blocking_bug_count",
    "cost_reported_run_count",
    "cost_expected_run_count",
    "cost_covered_pr_count",
)
PR_OUTCOME_COST_METRICS = ("reported_cost_usd",)
PR_OUTCOME_DIMENSIONS = (
    "pr_outcome_schema",
    "pr_number",
    "opened_at",
    "merged_at",
    "closed_at",
    "reverted_at",
    "outcome",
    "cost_coverage",
)


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CloudBundleError(f"pr_outcome {field} must be a non-empty string")
    return value.strip()


def _timestamp(value: object, field: str) -> dt.datetime:
    text = _required_text(value, field)
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CloudBundleError(f"pr_outcome {field} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise CloudBundleError(f"pr_outcome {field} must include a UTC offset")
    return parsed


def _count(metrics: Mapping[str, Any], field: str, *, required: bool = False) -> int | None:
    value = metrics.get(field)
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CloudBundleError(f"pr_outcome metric {field!r} must be a non-negative integer")
    return value


def validate_pr_outcome_payload(event: Mapping[str, Any]) -> None:
    """Validate one metadata-only PR outcome and its cost coverage."""

    _required_text(event.get("repo_slug"), "repo_slug")
    dimensions = event.get("dimensions")
    metrics = event.get("metrics")
    if not isinstance(dimensions, Mapping):
        raise CloudBundleError("pr_outcome dimensions must be an object")
    if not isinstance(metrics, Mapping):
        raise CloudBundleError("pr_outcome metrics must be an object")
    if dimensions.get("pr_outcome_schema") != PR_OUTCOME_SCHEMA:
        raise CloudBundleError(
            "pr_outcome dimensions.pr_outcome_schema must be "
            f"{PR_OUTCOME_SCHEMA!r}"
        )
    unknown_dimensions = [str(key) for key in dimensions if key not in PR_OUTCOME_DIMENSIONS]
    if unknown_dimensions:
        raise CloudBundleError(f"unsupported pr_outcome dimension {unknown_dimensions[0]!r}")

    pr_number = _required_text(dimensions.get("pr_number"), "dimension 'pr_number'")
    if not pr_number.isdigit() or int(pr_number) < 1:
        raise CloudBundleError("pr_outcome dimension 'pr_number' must be a positive integer string")

    opened_at = _timestamp(dimensions.get("opened_at"), "dimension 'opened_at'")
    timestamps = {
        key: _timestamp(dimensions[key], f"dimension {key!r}")
        for key in ("merged_at", "closed_at", "reverted_at")
        if dimensions.get(key) not in (None, "")
    }
    for key, value in timestamps.items():
        if value < opened_at:
            raise CloudBundleError(f"pr_outcome dimension {key!r} cannot precede opened_at")

    outcome = _required_text(dimensions.get("outcome"), "dimension 'outcome'")
    if outcome not in PR_OUTCOME_VALUES:
        raise CloudBundleError(f"unsupported pr_outcome outcome {outcome!r}")
    if outcome in {"merged", "reverted"} and "merged_at" not in timestamps:
        raise CloudBundleError(f"pr_outcome outcome {outcome!r} requires merged_at")
    if outcome == "closed_unmerged" and "closed_at" not in timestamps:
        raise CloudBundleError("pr_outcome outcome 'closed_unmerged' requires closed_at")
    if outcome == "reverted" and "reverted_at" not in timestamps:
        raise CloudBundleError("pr_outcome outcome 'reverted' requires reverted_at")

    allowed_metrics = set(PR_OUTCOME_COUNT_METRICS + PR_OUTCOME_COST_METRICS)
    unknown_metrics = [str(key) for key in metrics if key not in allowed_metrics]
    if unknown_metrics:
        raise CloudBundleError(f"unsupported pr_outcome metric {unknown_metrics[0]!r}")
    if _count(metrics, "pr_count", required=True) != 1:
        raise CloudBundleError("pr_outcome metric 'pr_count' must equal 1")
    covered_prs = _count(metrics, "cost_covered_pr_count", required=True)
    for field in PR_OUTCOME_COUNT_METRICS:
        _count(metrics, field, required=field in {"pr_count", "cost_covered_pr_count"})
    catches = _count(metrics, "reviewer_catch_count")
    blockers = _count(metrics, "blocking_bug_count")
    if catches is not None and blockers is not None and blockers > catches:
        raise CloudBundleError("pr_outcome blocking_bug_count cannot exceed reviewer_catch_count")

    coverage = _required_text(dimensions.get("cost_coverage"), "dimension 'cost_coverage'")
    if coverage not in PR_COST_COVERAGE_VALUES:
        raise CloudBundleError(f"unsupported pr_outcome cost_coverage {coverage!r}")
    reported_runs = _count(metrics, "cost_reported_run_count")
    expected_runs = _count(metrics, "cost_expected_run_count")
    if (reported_runs is None) != (expected_runs is None):
        raise CloudBundleError("pr_outcome cost run counts must be provided together")

    cost = metrics.get("reported_cost_usd")
    if cost is not None and (
        isinstance(cost, bool)
        or not isinstance(cost, int | float)
        or not math.isfinite(cost)
        or cost < 0
    ):
        raise CloudBundleError("pr_outcome metric 'reported_cost_usd' must be finite and non-negative")
    if coverage == "complete":
        if cost is None or reported_runs is None or reported_runs != expected_runs or covered_prs != 1:
            raise CloudBundleError(
                "complete pr_outcome cost coverage requires reported cost, equal run counts, "
                "and cost_covered_pr_count=1"
            )
    elif coverage == "partial":
        if (
            cost is None
            or reported_runs is None
            or reported_runs < 1
            or reported_runs >= expected_runs
            or covered_prs != 0
        ):
            raise CloudBundleError(
                "partial pr_outcome cost coverage requires reported cost, 0 < reported < expected, "
                "and cost_covered_pr_count=0"
            )
    elif cost is not None or covered_prs != 0 or (reported_runs is not None and reported_runs != 0):
        raise CloudBundleError(
            "unknown pr_outcome cost coverage must omit reported cost and have zero covered/reported counts"
        )
