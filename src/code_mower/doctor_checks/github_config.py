"""GitHub doctor configuration helpers."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .common import as_sequence


def configured_repositories(config: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    repos: list[Mapping[str, Any]] = []
    for repo in as_sequence(config.get("repositories", [])):
        if isinstance(repo, Mapping) and repo.get("slug"):
            repos.append(repo)
    return tuple(repos)


def selected_saas_or_hosted_lanes(
    lanes: Sequence[tuple[str, Mapping[str, Any]]],
) -> list[str]:
    selected: list[str] = []
    for lane_id, lane in lanes:
        if str(lane.get("driver", "")) in {"saas_event", "hosted_bridge"}:
            selected.append(lane_id)
    return selected
