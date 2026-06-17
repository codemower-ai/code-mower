"""GitHub Actions failure selection and detail helpers."""

from __future__ import annotations

from typing import Any, Mapping

from .common import ACTIONS_INSPECTABLE_FAILURE_CONCLUSIONS


def _is_inspectable_actions_failure(record: Any) -> bool:
    return (
        isinstance(record, Mapping)
        and record.get("conclusion") in ACTIONS_INSPECTABLE_FAILURE_CONCLUSIONS
    )


def _actions_run_context(run: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run.get("id"),
        "workflow": str(run.get("name") or ""),
        "head_sha": str(run.get("head_sha") or ""),
    }


def _actions_job_context(job: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "job_id": job.get("id"),
        "job": str(job.get("name") or ""),
    }


def _incomplete_inspection_detail(
    *,
    stage: str,
    run_id: Any | None = None,
    workflow: str | None = None,
    job_id: Any | None = None,
    job: str | None = None,
    reason: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    detail: dict[str, Any] = {"stage": stage}
    if run_id is not None:
        detail["run_id"] = run_id
    if workflow:
        detail["workflow"] = workflow
    if job_id is not None:
        detail["job_id"] = job_id
    if job:
        detail["job"] = job
    if reason:
        detail["reason"] = reason
    if limit is not None:
        detail["limit"] = limit
    return detail


__all__ = (
    "_actions_job_context",
    "_actions_run_context",
    "_incomplete_inspection_detail",
    "_is_inspectable_actions_failure",
)
