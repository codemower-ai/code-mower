"""Reviewer run status normalization for calibration reports."""

from __future__ import annotations

from typing import Any, Mapping


RUN_STATUS_PASS = "pass"
RUN_STATUS_BLOCKED = "blocked"
RUN_STATUS_AUDIT_INPUT_INSUFFICIENT = "audit_input_insufficient"
RUN_STATUS_INFRA_ERROR = "infra_error"
RUN_STATUS_UNKNOWN = "unknown"

RUN_STATUS_CATEGORY_ALIASES = {
    "pass": RUN_STATUS_PASS,
    "passed": RUN_STATUS_PASS,
    "complete": RUN_STATUS_PASS,
    "completed": RUN_STATUS_PASS,
    "done": RUN_STATUS_PASS,
    "success": RUN_STATUS_PASS,
    "succeeded": RUN_STATUS_PASS,
    "block": RUN_STATUS_BLOCKED,
    "blocked": RUN_STATUS_BLOCKED,
    "audit_input_insufficient": RUN_STATUS_AUDIT_INPUT_INSUFFICIENT,
    "context_insufficient": RUN_STATUS_AUDIT_INPUT_INSUFFICIENT,
    "fail": RUN_STATUS_BLOCKED,
    "error": RUN_STATUS_INFRA_ERROR,
    "failed": RUN_STATUS_INFRA_ERROR,
    "failure": RUN_STATUS_INFRA_ERROR,
    "infra_error": RUN_STATUS_INFRA_ERROR,
    "input_insufficient": RUN_STATUS_AUDIT_INPUT_INSUFFICIENT,
    "insufficient_context": RUN_STATUS_AUDIT_INPUT_INSUFFICIENT,
    "invalid_summary": RUN_STATUS_INFRA_ERROR,
    "launch_failed": RUN_STATUS_INFRA_ERROR,
    "missing_summary": RUN_STATUS_INFRA_ERROR,
    "rate_limit": RUN_STATUS_INFRA_ERROR,
    "rate_limited": RUN_STATUS_INFRA_ERROR,
    "setup_error": RUN_STATUS_INFRA_ERROR,
    "stale": RUN_STATUS_INFRA_ERROR,
    "timeout": RUN_STATUS_INFRA_ERROR,
    "timed_out": RUN_STATUS_INFRA_ERROR,
}


def normalize_run_status_category(value: Any) -> str:
    """Return the semantic category used for reviewer-run policy decisions."""

    status = str(value or "").strip().lower().replace("-", "_")
    return RUN_STATUS_CATEGORY_ALIASES.get(status, RUN_STATUS_UNKNOWN)


def status_from_verdict(value: Any, *, returncode: int | None = None) -> str:
    category = normalize_run_status_category(value)
    if category != RUN_STATUS_UNKNOWN:
        return category
    if returncode is not None and returncode != 0:
        return RUN_STATUS_INFRA_ERROR
    return RUN_STATUS_UNKNOWN


def count_normalized_findings(findings: Any) -> int:
    if not isinstance(findings, list):
        return 0
    return sum(1 for finding in findings if isinstance(finding, Mapping))
