"""GitHub Actions private-repo cost heuristics."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Mapping

from .common import (
    ACTIONS_COST_SAMPLE_MAX,
    ACTIONS_METADATA_WORKFLOW_MARKERS,
    DoctorCheck,
    STATUS_PASS,
    STATUS_WARN,
)
from .github_api import _github_api_json


def _parse_github_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).astimezone(timezone.utc)
    except ValueError:
        return None


def _approx_run_seconds(run: Mapping[str, Any]) -> float:
    started = _parse_github_timestamp(
        run.get("run_started_at") or run.get("created_at")
    )
    updated = _parse_github_timestamp(run.get("updated_at"))
    if started is None or updated is None or updated < started:
        return 0.0
    return (updated - started).total_seconds()


def _is_metadata_workflow(name: str, path: str) -> bool:
    haystack = f"{name}\n{path}".lower()
    return any(marker in haystack for marker in ACTIONS_METADATA_WORKFLOW_MARKERS)


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

    workflow_counts: Counter[str] = Counter()
    event_counts: Counter[str] = Counter()
    workflow_seconds: defaultdict[str, float] = defaultdict(float)
    metadata_counts: Counter[str] = Counter()
    metadata_seconds: defaultdict[str, float] = defaultdict(float)
    schedule_runs = 0
    total_seconds = 0.0

    for run in raw_runs:
        if not isinstance(run, Mapping):
            continue
        workflow_name = str(run.get("name") or run.get("display_title") or "unknown")
        workflow_path = str(run.get("path") or "")
        event = str(run.get("event") or "unknown")
        seconds = _approx_run_seconds(run)
        workflow_counts[workflow_name] += 1
        event_counts[event] += 1
        workflow_seconds[workflow_name] += seconds
        total_seconds += seconds
        if event == "schedule":
            schedule_runs += 1
        if _is_metadata_workflow(workflow_name, workflow_path):
            metadata_counts[workflow_name] += 1
            metadata_seconds[workflow_name] += seconds

    sample_size = sum(workflow_counts.values())
    metadata_run_count = sum(metadata_counts.values())
    metadata_share = (metadata_run_count / sample_size) if sample_size else 0.0
    top_workflows = [
        {
            "workflow": workflow,
            "runs": count,
            "approx_minutes": round(workflow_seconds[workflow] / 60, 2),
        }
        for workflow, count in workflow_counts.most_common(10)
    ]
    top_metadata_workflows = [
        {
            "workflow": workflow,
            "runs": count,
            "approx_minutes": round(metadata_seconds[workflow] / 60, 2),
        }
        for workflow, count in metadata_counts.most_common(10)
    ]
    detail = {
        "repo": slug,
        "private": private,
        "sample_limit": bounded_limit,
        "sampled_runs": sample_size,
        "approx_total_minutes": round(total_seconds / 60, 2),
        "metadata_workflow_runs": metadata_run_count,
        "metadata_workflow_share": round(metadata_share, 3),
        "schedule_runs": schedule_runs,
        "top_workflows": top_workflows,
        "top_metadata_workflows": top_metadata_workflows,
        "events": [
            {"event": event, "runs": count}
            for event, count in event_counts.most_common(10)
        ],
    }
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
