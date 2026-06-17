"""GitHub auth, repository, and Actions cost diagnostics facade."""

from __future__ import annotations

import shutil
from typing import Any, Mapping, Sequence

from .common import (
    ACTIONS_COST_SAMPLE_DEFAULT,
    DoctorCheck,
    STATUS_PASS,
    STATUS_WARN,
)
from .github_actions import (
    _check_actions_cost_sample,
    _check_recent_actions_billing_blocks,
)
from .github_actions_permissions import check_actions_permissions
from .github_branch import check_branch_protection
from .github_config import configured_repositories, selected_saas_or_hosted_lanes
from .github_provider import check_private_repo_provider_surface
from .github_repo import check_repo_metadata, check_repo_permissions


def check_github_setup(
    *,
    config: Mapping[str, Any],
    lanes: Sequence[tuple[str, Mapping[str, Any]]],
    http_timeout: int,
    actions_cost_sample: int = ACTIONS_COST_SAMPLE_DEFAULT,
) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    gh_path = shutil.which("gh")
    if not gh_path:
        return [
            DoctorCheck(
                name="github.cli",
                status=STATUS_WARN,
                message="GitHub setup checks require the gh CLI",
                detail={"gh_path": ""},
                remediation=(
                    "Install GitHub CLI, run `gh auth login`, then rerun "
                    "`code-mower doctor --github`."
                ),
            )
        ]

    checks.append(
        DoctorCheck(
            name="github.cli",
            status=STATUS_PASS,
            message="gh found for GitHub setup checks",
            detail={"gh_path": gh_path},
        )
    )

    repos = configured_repositories(config)
    if not repos:
        checks.append(
            DoctorCheck(
                name="github.repositories",
                status=STATUS_WARN,
                message="config declares no repositories for GitHub setup checks",
                remediation=(
                    "Add repositories[].slug entries to code-mower.yml before "
                    "running GitHub-backed audits."
                ),
            )
        )
        return checks

    selected_saas_or_hosted = selected_saas_or_hosted_lanes(lanes)
    private_repos: list[str] = []
    unknown_visibility_repos: list[str] = []
    for repo in repos:
        slug = str(repo.get("slug") or "")
        metadata_check, metadata = check_repo_metadata(
            gh_path=gh_path,
            slug=slug,
            configured_default_branch=str(repo.get("default_branch") or "main"),
            http_timeout=http_timeout,
        )
        checks.append(metadata_check)
        if metadata is None:
            unknown_visibility_repos.append(slug)
            continue

        if metadata.is_private:
            private_repos.append(slug)

        checks.extend(
            [
                check_repo_permissions(slug=slug, repo_payload=metadata.payload),
                check_actions_permissions(
                    gh_path=gh_path,
                    slug=slug,
                    http_timeout=http_timeout,
                ),
                _check_recent_actions_billing_blocks(
                    gh_path=gh_path,
                    slug=slug,
                    http_timeout=http_timeout,
                ),
                _check_actions_cost_sample(
                    gh_path=gh_path,
                    slug=slug,
                    private=metadata.is_private,
                    http_timeout=http_timeout,
                    sample_limit=actions_cost_sample,
                ),
                check_branch_protection(
                    gh_path=gh_path,
                    slug=slug,
                    default_branch=metadata.default_branch,
                    http_timeout=http_timeout,
                ),
            ]
        )

    provider_check = check_private_repo_provider_surface(
        private_repos=private_repos,
        unknown_visibility_repos=unknown_visibility_repos,
        selected_saas_or_hosted=selected_saas_or_hosted,
    )
    if provider_check is not None:
        checks.append(provider_check)

    return checks
