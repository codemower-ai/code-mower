"""GitHub Actions failure and billing-block diagnostics."""

from __future__ import annotations

from .common import DoctorCheck, STATUS_PASS, STATUS_WARN
from .github_actions_failure_annotations import (
    _annotation_mentions_actions_billing_block,
    _check_run_id_from_actions_job,
)
from .github_actions_failure_scan import (
    inspect_recent_actions_failures,
)


def _check_recent_actions_billing_blocks(
    *,
    gh_path: str,
    slug: str,
    http_timeout: int,
) -> DoctorCheck:
    inspection = inspect_recent_actions_failures(
        gh_path=gh_path,
        slug=slug,
        http_timeout=http_timeout,
    )
    if inspection.unavailable_detail is not None:
        return DoctorCheck(
            name="github.actions.recent_failures",
            status=STATUS_WARN,
            message=f"could not inspect recent GitHub Actions runs for {slug}",
            detail={"repo": slug, **inspection.unavailable_detail},
            remediation=(
                "Verify gh auth can read Actions run metadata, then rerun "
                "`code-mower doctor --github`."
            ),
        )

    if inspection.missing_workflow_runs:
        return DoctorCheck(
            name="github.actions.recent_failures",
            status=STATUS_WARN,
            message=f"recent Actions response for {slug} did not include workflow_runs",
            detail={"repo": slug},
        )

    if inspection.has_billing_blocks:
        return DoctorCheck(
            name="github.actions.recent_failures",
            status=STATUS_WARN,
            message=(
                f"{slug} has recent Actions jobs blocked by billing "
                "or spending limits"
            ),
            detail={
                "repo": slug,
                "billing_block_count": len(inspection.billing_blocks),
                "billing_blocks": [block.as_detail() for block in inspection.billing_blocks],
                "inspected_failed_runs": inspection.inspected_failed_runs,
                "inspected_failed_jobs": inspection.inspected_failed_jobs,
            },
            remediation=(
                "Fix GitHub billing or Actions spending limits, then rerun "
                "failed workflows before relying on branch protection or "
                "deploy checks."
            ),
        )

    if inspection.incomplete_inspections:
        return DoctorCheck(
            name="github.actions.recent_failures",
            status=STATUS_WARN,
            message=f"{slug} has recent failed Actions runs that doctor could not fully inspect",
            detail={
                "repo": slug,
                "incomplete_inspection_count": inspection.incomplete_inspection_count,
                "incomplete_inspections": list(inspection.incomplete_inspections[:5]),
                "inspected_failed_runs": inspection.inspected_failed_runs,
                "inspected_failed_jobs": inspection.inspected_failed_jobs,
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
            "inspected_failed_runs": inspection.inspected_failed_runs,
            "inspected_failed_jobs": inspection.inspected_failed_jobs,
        },
    )


__all__ = (
    "_annotation_mentions_actions_billing_block",
    "_check_recent_actions_billing_blocks",
    "_check_run_id_from_actions_job",
)
