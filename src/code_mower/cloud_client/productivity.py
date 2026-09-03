"""Productivity metric contract helpers for CodeMower.com uploads."""

from __future__ import annotations

import math
from typing import Any, Mapping

from .errors import CloudBundleError


PRODUCTIVITY_EVENT_TYPE = "productivity_summary"
PRODUCTIVITY_METRICS_SCHEMA = "code_mower.productivityMetrics.v1"

PRODUCTIVITY_TIME_METRICS = (
    "cycle_time_seconds",
    "active_time_seconds",
    "wait_time_seconds",
    "queue_wait_seconds",
    "time_to_first_review_seconds",
    "time_to_green_seconds",
    "time_to_merge_seconds",
    "owner_wait_seconds",
)

PRODUCTIVITY_COUNT_METRICS = (
    "builder_run_count",
    "reviewer_run_count",
    "audit_pass_count",
    "audit_blocked_count",
    "reviewer_catch_count",
    "blocking_bug_count",
    "blocked_finding_count",
    "false_blocker_count",
    "missed_blocker_count",
    "fix_round_count",
    "owner_intervention_count",
    "manual_override_count",
    "automerge_eligible_count",
    "automerge_requested_count",
    "automerge_completed_count",
    "merged_pr_count",
    "abandoned_pr_count",
    "reverted_pr_count",
    "checks_failed_count",
    "checks_passed_count",
    "post_merge_defect_count",
)

PRODUCTIVITY_TOKEN_METRICS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_input_tokens",
    "reasoning_tokens",
)

PRODUCTIVITY_METRIC_UNITS: dict[str, str] = {
    **dict.fromkeys(PRODUCTIVITY_TIME_METRICS, "seconds"),
    **dict.fromkeys(PRODUCTIVITY_COUNT_METRICS, "count"),
    **dict.fromkeys(PRODUCTIVITY_TOKEN_METRICS, "tokens"),
    "cost_usd": "usd",
}

PRODUCTIVITY_REQUIRED_DIMENSIONS = (
    "productivity_schema",
    "repo_slug",
    "window_start",
    "window_end",
    "window_granularity",
    "aggregation_subject",
)

PRODUCTIVITY_WINDOW_GRANULARITIES = (
    "cycle",
    "day",
    "week",
    "release",
    "custom",
)

PRODUCTIVITY_AGGREGATION_SUBJECTS = (
    "repo",
    "lane",
    "provider",
    "builder",
    "reviewer",
    "issue",
    "pr",
    "release",
)


def validate_productivity_summary_payload(event: Mapping[str, Any]) -> None:
    """Validate the stricter productivity summary contract."""

    dimensions = event.get("dimensions")
    if not isinstance(dimensions, Mapping):
        raise CloudBundleError("productivity_summary dimensions must be an object")
    metrics = event.get("metrics")
    if not isinstance(metrics, Mapping):
        raise CloudBundleError("productivity_summary metrics must be an object")

    if dimensions.get("productivity_schema") != PRODUCTIVITY_METRICS_SCHEMA:
        raise CloudBundleError(
            "productivity_summary dimensions.productivity_schema must be "
            f"{PRODUCTIVITY_METRICS_SCHEMA!r}"
        )
    for key in PRODUCTIVITY_REQUIRED_DIMENSIONS:
        value = dimensions.get(key)
        if not isinstance(value, str) or not value.strip():
            raise CloudBundleError(
                f"productivity_summary dimension {key!r} must be a non-empty string"
            )
    window = dimensions["window_granularity"]
    if window not in PRODUCTIVITY_WINDOW_GRANULARITIES:
        allowed = ", ".join(PRODUCTIVITY_WINDOW_GRANULARITIES)
        raise CloudBundleError(
            f"unsupported productivity_summary window_granularity {window!r}; "
            f"allowed: {allowed}"
        )
    subject = dimensions["aggregation_subject"]
    if subject not in PRODUCTIVITY_AGGREGATION_SUBJECTS:
        allowed = ", ".join(PRODUCTIVITY_AGGREGATION_SUBJECTS)
        raise CloudBundleError(
            f"unsupported productivity_summary aggregation_subject {subject!r}; "
            f"allowed: {allowed}"
        )

    if not metrics:
        raise CloudBundleError("productivity_summary metrics must not be empty")
    for key, value in metrics.items():
        unit = PRODUCTIVITY_METRIC_UNITS.get(str(key))
        if unit is None:
            allowed = ", ".join(sorted(PRODUCTIVITY_METRIC_UNITS))
            raise CloudBundleError(
                f"unsupported productivity_summary metric {key!r}; allowed: {allowed}"
            )
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise CloudBundleError(
                f"productivity_summary metric {key!r} must be numeric"
            )
        if not math.isfinite(value) or value < 0:
            raise CloudBundleError(
                f"productivity_summary metric {key!r} must be finite and non-negative"
            )
        if unit in {"count", "tokens"} and int(value) != value:
            raise CloudBundleError(
                f"productivity_summary metric {key!r} must be an integer {unit} value"
            )
