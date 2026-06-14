"""Routine dogfood upload planning helpers.

The cloud CLI remains the public API. These helpers keep the repeatable
dogfood bundle shape separate from network posting and argument parsing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_DOGFOOD_REPORTS: tuple[tuple[str, str], ...] = (
    ("docs/reviewer-value-report.md", "value-report"),
    ("docs/lane-promotion-policy.md", "lane-policy"),
    (".code-mower/reviewer-value-report.md", "value-report"),
    (".code-mower/reviewer-metrics.json", "reviewer-metrics"),
)


@dataclass(frozen=True)
class DogfoodPlan:
    repo_slug: str
    team_id: str
    install_id: str
    source: str
    report_count: int
    extra_event_count: int


def default_dogfood_reports(repo_path: Path) -> list[tuple[Path, str]]:
    seen: set[Path] = set()
    reports: list[tuple[Path, str]] = []
    for relative_path, kind in DEFAULT_DOGFOOD_REPORTS:
        path = repo_path / relative_path
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        reports.append((path, kind))
    return reports


def build_dogfood_plan(
    *,
    repo_slug: str,
    team_id: str,
    install_id: str,
    source: str,
    reports: list[tuple[Path, str]],
    events: list[dict[str, Any]],
) -> DogfoodPlan:
    return DogfoodPlan(
        repo_slug=repo_slug,
        team_id=team_id,
        install_id=install_id,
        source=source,
        report_count=len(reports),
        extra_event_count=len(events),
    )


def build_dogfood_dry_run_preview(
    *,
    endpoint: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "mode": "cloud-upload-dry-run",
        "endpoint": endpoint,
        "would_upload": False,
        "requires_yes": True,
        "upload_mode": payload["upload_mode"],
        "report_count": len(payload["reports"]),
        "event_count": len(payload["events"]),
        "privacy_mode": payload["privacy_mode"],
        "excluded_content": payload["excluded_content"],
    }
