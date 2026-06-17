"""GitHub Actions recent-failure inspection helpers."""

from __future__ import annotations

from typing import Any, Mapping

from .common import (
    ACTIONS_INSPECTABLE_FAILURE_CONCLUSIONS,
    MAX_ACTIONS_FAILED_JOBS_TO_INSPECT,
    MAX_ACTIONS_FAILED_RUNS_TO_INSPECT,
)
from .github_actions_failure_annotations import (
    _annotation_mentions_actions_billing_block,
    _check_run_id_from_actions_job,
)
from .github_actions_failure_models import (
    ActionsBillingBlock,
    ActionsFailureInspection,
)
from .github_api import _github_api_json, _github_api_list


def _append_incomplete(
    incomplete_inspections: list[dict[str, Any]],
    detail: Mapping[str, Any],
) -> None:
    incomplete_inspections.append(dict(detail))


def inspect_recent_actions_failures(
    *,
    gh_path: str,
    slug: str,
    http_timeout: int,
) -> ActionsFailureInspection:
    runs_payload, runs_detail = _github_api_json(
        gh_path,
        f"repos/{slug}/actions/runs?per_page=10",
        http_timeout=http_timeout,
    )
    if runs_payload is None:
        return ActionsFailureInspection(unavailable_detail=runs_detail)

    raw_runs = runs_payload.get("workflow_runs")
    if not isinstance(raw_runs, list):
        return ActionsFailureInspection(missing_workflow_runs=True)

    inspected_runs = 0
    inspected_jobs = 0
    billing_blocks: list[ActionsBillingBlock] = []
    incomplete_inspections: list[dict[str, Any]] = []
    for run in raw_runs:
        if (
            not isinstance(run, Mapping)
            or run.get("conclusion") not in ACTIONS_INSPECTABLE_FAILURE_CONCLUSIONS
        ):
            continue
        if inspected_runs >= MAX_ACTIONS_FAILED_RUNS_TO_INSPECT:
            _append_incomplete(
                incomplete_inspections,
                {
                    "stage": "runs",
                    "reason": "inspection_limit_reached",
                    "limit": MAX_ACTIONS_FAILED_RUNS_TO_INSPECT,
                },
            )
            break
        run_id = run.get("id")
        if run_id is None:
            continue
        inspected_runs += 1
        jobs_payload, _jobs_detail = _github_api_json(
            gh_path,
            f"repos/{slug}/actions/runs/{run_id}/jobs?per_page=20",
            http_timeout=http_timeout,
        )
        workflow = str(run.get("name") or "")
        if jobs_payload is None:
            _append_incomplete(
                incomplete_inspections,
                {
                    "run_id": run_id,
                    "workflow": workflow,
                    "stage": "jobs",
                },
            )
            continue
        raw_jobs = jobs_payload.get("jobs")
        if not isinstance(raw_jobs, list):
            _append_incomplete(
                incomplete_inspections,
                {
                    "run_id": run_id,
                    "workflow": workflow,
                    "stage": "jobs",
                    "reason": "missing_jobs",
                },
            )
            continue
        if not raw_jobs:
            _append_incomplete(
                incomplete_inspections,
                {
                    "run_id": run_id,
                    "workflow": workflow,
                    "stage": "jobs",
                    "reason": "no_jobs",
                },
            )
            continue
        for job in raw_jobs:
            if (
                not isinstance(job, Mapping)
                or job.get("conclusion") not in ACTIONS_INSPECTABLE_FAILURE_CONCLUSIONS
            ):
                continue
            if inspected_jobs >= MAX_ACTIONS_FAILED_JOBS_TO_INSPECT:
                _append_incomplete(
                    incomplete_inspections,
                    {
                        "run_id": run_id,
                        "workflow": workflow,
                        "stage": "annotations",
                        "reason": "inspection_limit_reached",
                        "limit": MAX_ACTIONS_FAILED_JOBS_TO_INSPECT,
                    },
                )
                break
            job_id = job.get("id")
            if job_id is None:
                continue
            check_run_id = _check_run_id_from_actions_job(job)
            job_name = str(job.get("name") or "")
            if check_run_id is None:
                _append_incomplete(
                    incomplete_inspections,
                    {
                        "run_id": run_id,
                        "workflow": workflow,
                        "job": job_name,
                        "stage": "annotations",
                        "reason": "missing_check_run_id",
                    },
                )
                continue
            inspected_jobs += 1
            annotations, _annotations_detail = _github_api_list(
                gh_path,
                f"repos/{slug}/check-runs/{check_run_id}/annotations?per_page=20",
                http_timeout=http_timeout,
            )
            if annotations is None:
                _append_incomplete(
                    incomplete_inspections,
                    {
                        "run_id": run_id,
                        "workflow": workflow,
                        "job_id": job_id,
                        "job": job_name,
                        "stage": "annotations",
                    },
                )
                continue
            for annotation in annotations:
                if not isinstance(annotation, Mapping):
                    continue
                message = str(annotation.get("message") or "")
                if _annotation_mentions_actions_billing_block(message):
                    billing_blocks.append(
                        ActionsBillingBlock(
                            run_id=run_id,
                            workflow=workflow,
                            head_sha=str(run.get("head_sha") or ""),
                            job_id=job_id,
                            check_run_id=str(check_run_id),
                            job=job_name,
                        )
                    )
                    return ActionsFailureInspection(
                        inspected_failed_runs=inspected_runs,
                        inspected_failed_jobs=inspected_jobs,
                        billing_blocks=tuple(billing_blocks),
                        incomplete_inspections=tuple(incomplete_inspections),
                    )

    return ActionsFailureInspection(
        inspected_failed_runs=inspected_runs,
        inspected_failed_jobs=inspected_jobs,
        incomplete_inspections=tuple(incomplete_inspections),
    )


__all__ = (
    "ActionsBillingBlock",
    "ActionsFailureInspection",
    "_annotation_mentions_actions_billing_block",
    "_check_run_id_from_actions_job",
    "inspect_recent_actions_failures",
)
