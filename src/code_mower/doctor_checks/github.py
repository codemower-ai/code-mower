"""GitHub auth, repository, and Actions cost diagnostics."""

from __future__ import annotations

import shutil
import urllib.parse
from typing import Any, Mapping, Sequence

from .common import (
    ACTIONS_COST_SAMPLE_DEFAULT,
    DoctorCheck,
    STATUS_PASS,
    STATUS_WARN,
    as_sequence,
)
from .github_actions import (
    _check_actions_cost_sample,
    _check_recent_actions_billing_blocks,
)
from .github_api import _github_api_json


def _configured_repositories(config: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    repos: list[Mapping[str, Any]] = []
    for repo in as_sequence(config.get("repositories", [])):
        if isinstance(repo, Mapping) and repo.get("slug"):
            repos.append(repo)
    return tuple(repos)


def _selected_saas_or_hosted_lanes(
    lanes: Sequence[tuple[str, Mapping[str, Any]]],
) -> list[str]:
    selected: list[str] = []
    for lane_id, lane in lanes:
        if str(lane.get("driver", "")) in {"saas_event", "hosted_bridge"}:
            selected.append(lane_id)
    return selected


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

    repos = _configured_repositories(config)
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

    selected_saas_or_hosted = _selected_saas_or_hosted_lanes(lanes)
    private_repos: list[str] = []
    unknown_visibility_repos: list[str] = []
    for repo in repos:
        slug = str(repo.get("slug") or "")
        configured_default_branch = str(repo.get("default_branch") or "main")
        repo_payload, repo_detail = _github_api_json(
            gh_path,
            f"repos/{slug}",
            http_timeout=http_timeout,
        )
        if repo_payload is None:
            unknown_visibility_repos.append(slug)
            checks.append(
                DoctorCheck(
                    name="github.repo.metadata",
                    status=STATUS_WARN,
                    message=f"could not read GitHub repository metadata for {slug}",
                    detail={"repo": slug, **repo_detail},
                    remediation=(
                        "Verify gh auth can read this repo. Private repos need "
                        "a token or GitHub App installation with repository access."
                    ),
                )
            )
            continue

        is_private = bool(repo_payload.get("private"))
        default_branch = str(
            repo_payload.get("default_branch") or configured_default_branch or "main"
        )
        if is_private:
            private_repos.append(slug)
        checks.append(
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
            )
        )

        permissions = repo_payload.get("permissions")
        if isinstance(permissions, Mapping):
            write_like = any(
                bool(permissions.get(name))
                for name in ("admin", "maintain", "push", "triage")
            )
            checks.append(
                DoctorCheck(
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
            )
        else:
            checks.append(
                DoctorCheck(
                    name="github.repo.permissions",
                    status=STATUS_WARN,
                    message=f"{slug} metadata did not include token permissions",
                    detail={"repo": slug},
                    remediation=(
                        "If label writes fail, configure the lane token secrets "
                        "documented by the provider matrix."
                    ),
                )
            )

        actions_payload, actions_detail = _github_api_json(
            gh_path,
            f"repos/{slug}/actions/permissions",
            http_timeout=http_timeout,
        )
        if actions_payload is None:
            checks.append(
                DoctorCheck(
                    name="github.actions.permissions",
                    status=STATUS_WARN,
                    message=f"could not inspect GitHub Actions permissions for {slug}",
                    detail={"repo": slug, **actions_detail},
                    remediation=(
                        "A repo admin should verify Actions are enabled and workflow "
                        "token permissions can write issues/labels or that PAT "
                        "fallback secrets are configured."
                    ),
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    name="github.actions.permissions",
                    status=(
                        STATUS_PASS
                        if bool(actions_payload.get("enabled"))
                        else STATUS_WARN
                    ),
                    message=(
                        f"{slug} GitHub Actions are enabled and inspectable"
                        if bool(actions_payload.get("enabled"))
                        else f"{slug} GitHub Actions appear disabled"
                    ),
                    detail={
                        "repo": slug,
                        "enabled": bool(actions_payload.get("enabled")),
                        "allowed_actions": str(actions_payload.get("allowed_actions") or ""),
                    },
                    remediation=(
                        None
                        if bool(actions_payload.get("enabled"))
                        else (
                            "Enable GitHub Actions for this repository before "
                            "expecting Code Mower labelers or audit workflows to run."
                        )
                    ),
                )
            )

        checks.append(
            _check_recent_actions_billing_blocks(
                gh_path=gh_path,
                slug=slug,
                http_timeout=http_timeout,
            )
        )
        checks.append(
            _check_actions_cost_sample(
                gh_path=gh_path,
                slug=slug,
                private=is_private,
                http_timeout=http_timeout,
                sample_limit=actions_cost_sample,
            )
        )

        encoded_branch = urllib.parse.quote(default_branch, safe="")
        protection_payload, protection_detail = _github_api_json(
            gh_path,
            f"repos/{slug}/branches/{encoded_branch}/protection",
            http_timeout=http_timeout,
        )
        if protection_payload is None:
            checks.append(
                DoctorCheck(
                    name="github.branch_protection",
                    status=STATUS_WARN,
                    message=f"could not confirm branch protection for {slug}@{default_branch}",
                    detail={
                        "repo": slug,
                        "default_branch": default_branch,
                        **protection_detail,
                    },
                    remediation=(
                        "Before enabling autonomous merge, protect the default branch "
                        "and make required checks explicit."
                    ),
                )
            )
        else:
            required_checks = protection_payload.get("required_status_checks")
            contexts: list[str] = []
            if isinstance(required_checks, Mapping):
                raw_contexts = required_checks.get("contexts")
                if isinstance(raw_contexts, list):
                    contexts = [str(item) for item in raw_contexts]
            checks.append(
                DoctorCheck(
                    name="github.branch_protection",
                    status=STATUS_PASS,
                    message=f"{slug}@{default_branch} branch protection is inspectable",
                    detail={
                        "repo": slug,
                        "default_branch": default_branch,
                        "required_status_check_count": len(contexts),
                    },
                )
            )

    if private_repos and selected_saas_or_hosted:
        checks.append(
            DoctorCheck(
                name="github.provider.private_repo",
                status=STATUS_WARN,
                message=(
                    "private repos selected with SaaS/hosted lanes: "
                    + ", ".join(selected_saas_or_hosted)
                ),
                detail={
                    "private_repo_count": len(private_repos),
                    "lanes": selected_saas_or_hosted,
                },
                remediation=(
                    "Install each provider's GitHub App for the selected private "
                    "repositories, confirm plan support, and decide whether sending "
                    "diffs/source to that provider is acceptable."
                ),
            )
        )
    elif unknown_visibility_repos and selected_saas_or_hosted:
        checks.append(
            DoctorCheck(
                name="github.provider.private_repo",
                status=STATUS_WARN,
                message=(
                    "could not determine repository visibility for SaaS/hosted lanes: "
                    + ", ".join(selected_saas_or_hosted)
                ),
                detail={
                    "unknown_repo_count": len(unknown_visibility_repos),
                    "lanes": selected_saas_or_hosted,
                },
                remediation=(
                    "Verify gh auth can read repository metadata before deciding "
                    "whether hosted provider apps need private-repo access."
                ),
            )
        )
    elif selected_saas_or_hosted:
        checks.append(
            DoctorCheck(
                name="github.provider.private_repo",
                status=STATUS_PASS,
                message="selected SaaS/hosted lanes have no private repos in config",
                detail={"lanes": selected_saas_or_hosted},
            )
        )

    return checks
