"""GitHub Actions private-repo cost heuristics."""

from __future__ import annotations

from .common import (
    ACTIONS_COST_SAMPLE_MAX,
    DoctorCheck,
    STATUS_PASS,
    STATUS_WARN,
)
from .github_api import _github_api_json
from .github_actions_cost_summary import summarize_actions_cost_runs


def _check_actions_cost_sample(
    *,
    gh_path: str,
    slug: str,
    private: bool,
    http_timeout: int,
    sample_limit: int,
) -> DoctorCheck:
    bounded_limit = max(1, min(sample_limit, ACTIONS_COST_SAMPLE_MAX))
    runs_payload, runs_detail = _github_api_json(
        gh_path,
        f"repos/{slug}/actions/runs?per_page={bounded_limit}",
        http_timeout=http_timeout,
    )
    if runs_payload is None:
        return DoctorCheck(
            name="github.actions.cost_sample",
            status=STATUS_WARN,
            message=f"could not sample recent GitHub Actions usage for {slug}",
            detail={"repo": slug, **runs_detail},
            remediation=(
                "Verify gh auth can read Actions run metadata. Private repos "
                "should periodically inspect Actions usage metrics after enabling "
                "hosted or informational lanes."
            ),
        )

    raw_runs = runs_payload.get("workflow_runs")
    if not isinstance(raw_runs, list):
        return DoctorCheck(
            name="github.actions.cost_sample",
            status=STATUS_WARN,
            message=f"Actions usage response for {slug} did not include workflow_runs",
            detail={"repo": slug},
        )

    detail = summarize_actions_cost_runs(
        raw_runs,
        slug=slug,
        private=private,
        bounded_limit=bounded_limit,
    )
    sample_size = int(detail["sampled_runs"])
    metadata_run_count = int(detail["metadata_workflow_runs"])
    schedule_runs = int(detail["schedule_runs"])
    if not sample_size:
        return DoctorCheck(
            name="github.actions.cost_sample",
            status=STATUS_PASS,
            message=f"{slug} has no recent Actions runs in the sampled window",
            detail=detail,
        )

    noisy_metadata = private and metadata_run_count >= max(5, int(sample_size * 0.2))
    scheduled_private = private and schedule_runs > 0
    if noisy_metadata or scheduled_private:
        reasons: list[str] = []
        if noisy_metadata:
            reasons.append(
                f"{metadata_run_count}/{sample_size} sampled runs look like "
                "metadata or reviewer labeler workflows"
            )
        if scheduled_private:
            reasons.append(f"{schedule_runs} sampled runs were scheduled")
        return DoctorCheck(
            name="github.actions.cost_sample",
            status=STATUS_WARN,
            message=(
                f"{slug} private-repo Actions sample suggests avoidable spend: "
                f"{'; '.join(reasons)}"
            ),
            detail=detail,
            remediation=(
                "Prefer label, trusted-comment, or workflow_dispatch triggers for "
                "optional hosted reviewers; add job-level if guards before checkout; "
                "keep informational lanes out of branch protection."
            ),
        )

    return DoctorCheck(
        name="github.actions.cost_sample",
        status=STATUS_PASS,
        message=f"{slug} recent Actions sample has no obvious private-repo cost traps",
        detail=detail,
    )
