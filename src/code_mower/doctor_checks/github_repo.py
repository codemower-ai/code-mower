"""GitHub repository metadata and permission doctor checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .common import DoctorCheck, OBSERVER_ADOPTION_POSTURES, STATUS_FAIL, STATUS_PASS, STATUS_WARN
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


def check_repo_auto_merge(
    *,
    slug: str,
    repo_payload: Mapping[str, Any],
    adoption: bool = False,
    adoption_posture: str = "reviewer-gate",
) -> DoctorCheck:
    if "allow_auto_merge" not in repo_payload:
        return DoctorCheck(
            name="github.repo.auto_merge",
            status=STATUS_WARN,
            message=f"{slug} metadata did not include auto-merge posture",
            detail={
                "repo": slug,
                "owner_action": not adoption,
                "promotion_todo": adoption,
                "promotion_todo_kind": "repo_auto_merge_visibility",
                "owner_action_kind": "repo_auto_merge_visibility",
                "adoption_posture": adoption_posture,
            },
            remediation=(
                "Verify repository auto-merge manually before relying on the "
                "generated Code Mower gate to call enablePullRequestAutoMerge."
            ),
        )

    allow_auto_merge = bool(repo_payload.get("allow_auto_merge"))
    blocked = (
        not allow_auto_merge
        and not adoption
        and adoption_posture not in OBSERVER_ADOPTION_POSTURES
    )
    return DoctorCheck(
        name="github.repo.auto_merge",
        status=STATUS_PASS if allow_auto_merge else (STATUS_FAIL if blocked else STATUS_WARN),
        message=(
            f"{slug} has auto-merge enabled"
            if allow_auto_merge
            else f"{slug} does not allow auto-merge"
        ),
        detail={
            "repo": slug,
            "allow_auto_merge": allow_auto_merge,
            "owner_action": not allow_auto_merge and not adoption,
            "promotion_todo": adoption and not allow_auto_merge,
            "promotion_todo_kind": "repo_auto_merge",
            "owner_action_kind": "repo_auto_merge",
            "adoption_posture": adoption_posture,
        },
        remediation=(
            None
            if allow_auto_merge
            else (
                "During an informational pilot this is expected; enable "
                "repository auto-merge only when a reviewer lane meets "
                "docs/lane-promotion-policy.md and code-mower/gate is required. "
                f"Promotion command: `gh api -X PATCH repos/{slug} -f allow_auto_merge=true`."
            )
        ),
    )
