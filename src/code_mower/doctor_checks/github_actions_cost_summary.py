"""Summarize recent GitHub Actions runs for private-repo cost diagnostics."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Mapping

from .common import ACTIONS_METADATA_WORKFLOW_MARKERS


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


def summarize_actions_cost_runs(
    raw_runs: list[Any],
    *,
    slug: str,
    private: bool,
    bounded_limit: int,
) -> dict[str, Any]:
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
    return {
        "repo": slug,
        "private": private,
        "sample_limit": bounded_limit,
        "sampled_runs": sample_size,
        "approx_total_minutes": round(total_seconds / 60, 2),
        "metadata_workflow_runs": metadata_run_count,
        "metadata_workflow_share": round(metadata_share, 3),
        "schedule_runs": schedule_runs,
        "top_workflows": [
            {
                "workflow": workflow,
                "runs": count,
                "approx_minutes": round(workflow_seconds[workflow] / 60, 2),
            }
            for workflow, count in workflow_counts.most_common(10)
        ],
        "top_metadata_workflows": [
            {
                "workflow": workflow,
                "runs": count,
                "approx_minutes": round(metadata_seconds[workflow] / 60, 2),
            }
            for workflow, count in metadata_counts.most_common(10)
        ],
        "events": [
            {"event": event, "runs": count}
            for event, count in event_counts.most_common(10)
        ],
    }


__all__ = (
    "_approx_run_seconds",
    "_is_metadata_workflow",
    "_parse_github_timestamp",
    "summarize_actions_cost_runs",
)
