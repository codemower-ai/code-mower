"""GitHub Actions failure and billing-block diagnostics."""

from __future__ import annotations

import re
from typing import Any, Mapping

from .common import (
    ACTIONS_BILLING_BLOCK_PATTERNS,
    ACTIONS_INSPECTABLE_FAILURE_CONCLUSIONS,
    MAX_ACTIONS_FAILED_JOBS_TO_INSPECT,
    MAX_ACTIONS_FAILED_RUNS_TO_INSPECT,
    DoctorCheck,
    STATUS_PASS,
    STATUS_WARN,
)
from .github_api import _github_api_json, _github_api_list


def _annotation_mentions_actions_billing_block(message: str) -> bool:
    lowered = message.lower()
    return any(pattern in lowered for pattern in ACTIONS_BILLING_BLOCK_PATTERNS)


def _check_run_id_from_actions_job(job: Mapping[str, Any]) -> Any | None:
    check_run_url = str(job.get("check_run_url") or "")
    match = re.search(r"/check-runs/([0-9]+)$", check_run_url)
    if match:
        return match.group(1)
    return None


def _check_recent_actions_billing_blocks(
    *,
    gh_path: str,
    slug: str,
    http_timeout: int,
) -> DoctorCheck:
    runs_payload, runs_detail = _github_api_json(
        gh_path,
        f"repos/{slug}/actions/runs?per_page=10",
        http_timeout=http_timeout,
    )
    if runs_payload is None:
        return DoctorCheck(
            name="github.actions.recent_failures",
            status=STATUS_WARN,
            message=f"could not inspect recent GitHub Actions runs for {slug}",
            detail={"repo": slug, **runs_detail},
            remediation=(
                "Verify gh auth can read Actions run metadata, then rerun "
                "`code-mower doctor --github`."
            ),
        )

    raw_runs = runs_payload.get("workflow_runs")
    if not isinstance(raw_runs, list):
        return DoctorCheck(
            name="github.actions.recent_failures",
            status=STATUS_WARN,
            message=f"recent Actions response for {slug} did not include workflow_runs",
            detail={"repo": slug},
        )

    inspected_runs = 0
    inspected_jobs = 0
    incomplete_inspections: list[dict[str, Any]] = []
    for run in raw_runs:
        if (
            not isinstance(run, Mapping)
            or run.get("conclusion") not in ACTIONS_INSPECTABLE_FAILURE_CONCLUSIONS
        ):
            continue
        if inspected_runs >= MAX_ACTIONS_FAILED_RUNS_TO_INSPECT:
            incomplete_inspections.append(
                {
                    "stage": "runs",
                    "reason": "inspection_limit_reached",
                    "limit": MAX_ACTIONS_FAILED_RUNS_TO_INSPECT,
                }
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
        if jobs_payload is None:
            incomplete_inspections.append(
                {
                    "run_id": run_id,
                    "workflow": str(run.get("name") or ""),
                    "stage": "jobs",
                }
            )
            continue
        raw_jobs = jobs_payload.get("jobs")
        if not isinstance(raw_jobs, list):
            incomplete_inspections.append(
                {
                    "run_id": run_id,
                    "workflow": str(run.get("name") or ""),
                    "stage": "jobs",
                    "reason": "missing_jobs",
                }
            )
            continue
        if not raw_jobs:
            incomplete_inspections.append(
                {
                    "run_id": run_id,
                    "workflow": str(run.get("name") or ""),
                    "stage": "jobs",
                    "reason": "no_jobs",
                }
            )
            continue
        for job in raw_jobs:
            if (
                not isinstance(job, Mapping)
                or job.get("conclusion") not in ACTIONS_INSPECTABLE_FAILURE_CONCLUSIONS
            ):
                continue
            if inspected_jobs >= MAX_ACTIONS_FAILED_JOBS_TO_INSPECT:
                incomplete_inspections.append(
                    {
                        "run_id": run_id,
                        "workflow": str(run.get("name") or ""),
                        "stage": "annotations",
                        "reason": "inspection_limit_reached",
                        "limit": MAX_ACTIONS_FAILED_JOBS_TO_INSPECT,
                    }
                )
                break
            job_id = job.get("id")
            if job_id is None:
                continue
            check_run_id = _check_run_id_from_actions_job(job)
            if check_run_id is None:
                incomplete_inspections.append(
                    {
                        "run_id": run_id,
                        "workflow": str(run.get("name") or ""),
                        "job": str(job.get("name") or ""),
                        "stage": "annotations",
                        "reason": "missing_check_run_id",
                    }
                )
                continue
            inspected_jobs += 1
            annotations, _annotations_detail = _github_api_list(
                gh_path,
                f"repos/{slug}/check-runs/{check_run_id}/annotations?per_page=20",
                http_timeout=http_timeout,
            )
            if annotations is None:
                incomplete_inspections.append(
                    {
                        "run_id": run_id,
                        "workflow": str(run.get("name") or ""),
                        "job_id": job_id,
                        "job": str(job.get("name") or ""),
                        "stage": "annotations",
                    }
                )
                continue
            for annotation in annotations:
                if not isinstance(annotation, Mapping):
                    continue
                message = str(annotation.get("message") or "")
                if _annotation_mentions_actions_billing_block(message):
                    return DoctorCheck(
                        name="github.actions.recent_failures",
                        status=STATUS_WARN,
                        message=(
                            f"{slug} has recent Actions jobs blocked by billing "
                            "or spending limits"
                        ),
                        detail={
                            "repo": slug,
                            "billing_block_count": 1,
                            "billing_blocks": [
                                {
                                    "run_id": run_id,
                                    "workflow": str(run.get("name") or ""),
                                    "head_sha": str(run.get("head_sha") or ""),
                                    "job_id": job_id,
                                    "check_run_id": str(check_run_id),
                                    "job": str(job.get("name") or ""),
                                }
                            ],
                            "inspected_failed_runs": inspected_runs,
                            "inspected_failed_jobs": inspected_jobs,
                        },
                        remediation=(
                            "Fix GitHub billing or Actions spending limits, then rerun "
                            "failed workflows before relying on branch protection or "
                            "deploy checks."
                        ),
                    )

    if incomplete_inspections:
        return DoctorCheck(
            name="github.actions.recent_failures",
            status=STATUS_WARN,
            message=f"{slug} has recent failed Actions runs that doctor could not fully inspect",
            detail={
                "repo": slug,
                "incomplete_inspection_count": len(incomplete_inspections),
                "incomplete_inspections": incomplete_inspections[:5],
                "inspected_failed_runs": inspected_runs,
                "inspected_failed_jobs": inspected_jobs,
            },
            remediation=(
                "Verify gh auth can read workflow jobs and annotations, or inspect "
                "recent failed Actions runs manually before treating doctor as a "
                "green setup signal."
            ),
        )

    return DoctorCheck(
        name="github.actions.recent_failures",
        status=STATUS_PASS,
        message=f"{slug} has no recent Actions billing-block annotations",
        detail={
            "repo": slug,
            "inspected_failed_runs": inspected_runs,
            "inspected_failed_jobs": inspected_jobs,
        },
    )
