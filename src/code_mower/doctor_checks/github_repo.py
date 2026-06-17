"""GitHub repository metadata and permission doctor checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .common import DoctorCheck, STATUS_PASS, STATUS_WARN
from .github_api import _github_api_json


@dataclass(frozen=True)
class GitHubRepoMetadata:
    slug: str
    is_private: bool
    default_branch: str
    payload: Mapping[str, Any]


def check_repo_metadata(
    *,
    gh_path: str,
    slug: str,
    configured_default_branch: str,
    http_timeout: int,
) -> tuple[DoctorCheck, GitHubRepoMetadata | None]:
    repo_payload, repo_detail = _github_api_json(
        gh_path,
        f"repos/{slug}",
        http_timeout=http_timeout,
    )
    if repo_payload is None:
        return (
            DoctorCheck(
                name="github.repo.metadata",
                status=STATUS_WARN,
                message=f"could not read GitHub repository metadata for {slug}",
                detail={"repo": slug, **repo_detail},
                remediation=(
                    "Verify gh auth can read this repo. Private repos need "
                    "a token or GitHub App installation with repository access."
                ),
            ),
            None,
        )

    is_private = bool(repo_payload.get("private"))
    default_branch = str(
        repo_payload.get("default_branch") or configured_default_branch or "main"
    )
    return (
        DoctorCheck(
            name="github.repo.metadata",
            status=STATUS_PASS,
            message=(
                f"{slug} is reachable "
                f"({'private' if is_private else 'public'} repository)"
            ),
            detail={
                "repo": slug,
                "private": is_private,
                "visibility": str(repo_payload.get("visibility") or ""),
                "default_branch": str(repo_payload.get("default_branch") or ""),
                "archived": bool(repo_payload.get("archived")),
                "fork": bool(repo_payload.get("fork")),
            },
        ),
        GitHubRepoMetadata(
            slug=slug,
            is_private=is_private,
            default_branch=default_branch,
            payload=repo_payload,
        ),
    )


def check_repo_permissions(*, slug: str, repo_payload: Mapping[str, Any]) -> DoctorCheck:
    permissions = repo_payload.get("permissions")
    if isinstance(permissions, Mapping):
        write_like = any(
            bool(permissions.get(name))
            for name in ("admin", "maintain", "push", "triage")
        )
        return DoctorCheck(
            name="github.repo.permissions",
            status=STATUS_PASS if write_like else STATUS_WARN,
            message=(
                f"{slug} token has repository write-adjacent permission"
                if write_like
                else f"{slug} token appears read-only for repository metadata"
            ),
            detail={
                "repo": slug,
                "admin": bool(permissions.get("admin")),
                "maintain": bool(permissions.get("maintain")),
                "push": bool(permissions.get("push")),
                "triage": bool(permissions.get("triage")),
                "pull": bool(permissions.get("pull")),
            },
            remediation=(
                None
                if write_like
                else (
                    "Configure a fine-grained PAT or GitHub App token with "
                    "Issues read/write and Pull requests read before expecting "
                    "Code Mower to apply labels or comments."
                )
            ),
        )

    return DoctorCheck(
        name="github.repo.permissions",
        status=STATUS_WARN,
        message=f"{slug} metadata did not include token permissions",
        detail={"repo": slug},
        remediation=(
            "If label writes fail, configure the lane token secrets "
            "documented by the provider matrix."
        ),
    )
