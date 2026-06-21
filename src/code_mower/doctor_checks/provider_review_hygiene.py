"""Review-hygiene checks for merge-authority provider lanes."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .common import DoctorCheck, STATUS_FAIL, STATUS_PASS, STATUS_SKIP, as_sequence


def check_review_hygiene(
    lane_id: str,
    lane: Mapping[str, Any],
    *,
    effective_lane: Mapping[str, Any] | None = None,
    repo_root: Path | None = None,
) -> DoctorCheck:
    """Verify that merge-authority lanes have stale terminal-label protection."""

    merge_authority_lane = effective_lane if effective_lane is not None else lane
    if not bool(merge_authority_lane.get("merge_authority", False)):
        return DoctorCheck(
            name="provider.review_hygiene",
            status=STATUS_SKIP,
            lane=lane_id,
            message="lane is not merge authority; stale terminal-label guard is optional",
        )

    review_hygiene = lane.get("review_hygiene")
    if not isinstance(review_hygiene, Mapping):
        review_hygiene = {}

    workflow = str(review_hygiene.get("workflow") or "").strip()
    token_env = str(review_hygiene.get("token_env") or "").strip()
    dispatch_workflow = str(review_hygiene.get("dispatch_workflow") or "").strip()
    trusted_authors = tuple(str(author) for author in as_sequence(review_hygiene.get("trusted_authors", [])))
    missing: list[str] = []
    if not workflow:
        missing.append("workflow")
    if not token_env:
        missing.append("token_env")

    detail = {
        "workflow": workflow,
        "token_env": token_env,
        "dispatch_workflow": dispatch_workflow,
        "trusted_authors": list(trusted_authors),
        "merge_authority": True,
    }

    if missing:
        return DoctorCheck(
            name="provider.review_hygiene",
            status=STATUS_FAIL,
            lane=lane_id,
            message=(
                "merge-authority lane is missing stale terminal-label guard config: "
                + ", ".join(missing)
            ),
            detail={**detail, "missing": missing},
            remediation=(
                "Add review_hygiene.workflow and review_hygiene.token_env to this "
                "lane, or regenerate workflows with `code-mower init --easy --apply`."
            ),
        )

    if token_env != "GITHUB_TOKEN":
        return DoctorCheck(
            name="provider.review_hygiene",
            status=STATUS_FAIL,
            lane=lane_id,
            message=(
                "merge-authority lane uses unsupported stale terminal-label token_env: "
                f"{token_env}"
            ),
            detail={**detail, "supported_token_env": "GITHUB_TOKEN"},
            remediation=(
                "Use review_hygiene.token_env: GITHUB_TOKEN, or update the generated "
                "clear-stale workflow template before relying on a custom token."
            ),
        )

    if repo_root is not None:
        workflow_path = Path(workflow)
        if not workflow_path.is_absolute():
            workflow_path = repo_root / workflow_path
        if not workflow_path.is_file():
            return DoctorCheck(
                name="provider.review_hygiene",
                status=STATUS_FAIL,
                lane=lane_id,
                message=(
                    "merge-authority lane stale terminal-label workflow is "
                    f"configured but missing from the repo: {workflow}"
                ),
                detail={
                    **detail,
                    "workflow_exists": False,
                    "workflow_path": str(workflow_path),
                },
                remediation=(
                    "Run `code-mower init --easy --apply` from the repository "
                    "root, commit the generated clear-stale workflow, then rerun "
                    "doctor before relying on this lane as merge authority."
                ),
            )
        detail = {
            **detail,
            "workflow_exists": True,
            "workflow_path": str(workflow_path),
        }

    return DoctorCheck(
        name="provider.review_hygiene",
        status=STATUS_PASS,
        lane=lane_id,
        message=f"stale terminal-label guard configured via {workflow}",
        detail=detail,
    )
