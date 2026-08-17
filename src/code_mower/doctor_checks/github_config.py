"""GitHub doctor configuration helpers."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .common import as_sequence
from .models import DoctorCheck, STATUS_PASS, STATUS_WARN


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


def check_repository_posture(config: Mapping[str, Any]) -> DoctorCheck:
    repos = configured_repositories(config)
    repo_slugs = [str(repo.get("slug") or "") for repo in repos]
    local_path_env_count = sum(
        1 for repo in repos if str(repo.get("local_path_env") or "")
    )
    if not repo_slugs:
        return DoctorCheck(
            name="config.repositories",
            status=STATUS_WARN,
            message="no repositories configured",
            detail={"repo_count": 0, "repositories": []},
            remediation=(
                "Add repositories[].slug to code-mower.yml, or run "
                "`code-mower init --add-repo OWNER/REPO --dry-run` to preview "
                "a sibling repo target."
            ),
        )
    posture = "multi-repo" if len(repo_slugs) > 1 else "single-repo"
    return DoctorCheck(
        name="config.repositories",
        status=STATUS_PASS,
        message=f"{posture} posture: {len(repo_slugs)} configured repository target(s)",
        detail={
            "repo_count": len(repo_slugs),
            "repositories": repo_slugs,
            "local_path_env_count": local_path_env_count,
        },
    )
