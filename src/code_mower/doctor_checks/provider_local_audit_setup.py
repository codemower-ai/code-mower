"""Local audit wrapper setup checks."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from code_mower.provider_runners.repo_paths import (
    LOCAL_AUDIT_RUNNER_DOC,
    parse_repo_paths,
    validate_repo_path_for_wrapper,
)

from .common import DoctorCheck, STATUS_FAIL, STATUS_PASS, STATUS_WARN

_REPO_PATH_ENV_BY_PROVIDER = {
    "claude": "CLAUDE_AUDIT_REPO_PATHS",
    "codex": "CODEX_AUDIT_REPO_PATHS",
}


def _doc_hint() -> str:
    return f"See {LOCAL_AUDIT_RUNNER_DOC}."


def _wrapper_repo_paths_env(lane_id: str, lane: Mapping[str, Any]) -> str:
    provider = str(lane.get("provider") or lane_id).replace("-", "_")
    if lane_id == "claude_audit":
        provider = "claude"
    return _REPO_PATH_ENV_BY_PROVIDER.get(provider, "")


def _is_supported_local_audit_wrapper(lane_id: str, lane: Mapping[str, Any]) -> bool:
    if str(lane.get("driver") or "") != "local_cli":
        return False
    return bool(_wrapper_repo_paths_env(lane_id, lane))


def check_local_audit_wrapper_setup(
    lane_id: str,
    lane: Mapping[str, Any],
    *,
    repo_root: Path | None = None,
) -> list[DoctorCheck]:
    """Check direct local-audit wrapper auth and repo-path environment."""

    if not _is_supported_local_audit_wrapper(lane_id, lane):
        return []

    checks: list[DoctorCheck] = []
    token_env_present = bool(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
    checks.append(
        DoctorCheck(
            name="runtime.local_audit.auth",
            status=STATUS_PASS if token_env_present else STATUS_WARN,
            lane=lane_id,
            message=(
                "direct local-audit wrapper can read GITHUB_TOKEN"
                if token_env_present
                else (
                    "direct local-audit wrapper needs GITHUB_TOKEN or "
                    "--read-token-from-stdin"
                )
            ),
            detail={
                "accepted_auth": ["GITHUB_TOKEN", "GH_TOKEN", "--read-token-from-stdin"],
                "doc": LOCAL_AUDIT_RUNNER_DOC,
            },
            remediation=(
                None
                if token_env_present
                else (
                    "Set GITHUB_TOKEN for direct wrapper runs, or pipe the token "
                    f"with --read-token-from-stdin. {_doc_hint()}"
                )
            ),
        )
    )

    env_name = _wrapper_repo_paths_env(lane_id, lane)
    spec = os.environ.get(env_name, "").strip()
    detail = {
        "env": env_name,
        "expected": "OWNER/REPO:/absolute/path",
        "doc": LOCAL_AUDIT_RUNNER_DOC,
    }
    if not spec:
        checks.append(
            DoctorCheck(
                name="runtime.local_audit.repo_paths",
                status=STATUS_WARN,
                lane=lane_id,
                message=(
                    f"{env_name} is not set for direct local-audit wrapper runs"
                ),
                detail=detail,
                remediation=(
                    f"Pass --repo-paths OWNER/REPO:/absolute/path, or set {env_name}, "
                    f"pointing at a separate PR-head checkout. {_doc_hint()}"
                ),
            )
        )
        return checks

    try:
        repo_paths = parse_repo_paths(spec)
    except ValueError as exc:
        checks.append(
            DoctorCheck(
                name="runtime.local_audit.repo_paths",
                status=STATUS_FAIL,
                lane=lane_id,
                message=f"{env_name} is invalid: {exc}",
                detail={**detail, "value_set": True},
                remediation=(
                    f"Set {env_name}=OWNER/REPO:/absolute/path, where the path "
                    f"is a separate PR-head checkout. {_doc_hint()}"
                ),
            )
        )
        return checks

    invalid_repos: list[dict[str, str]] = []
    for repo in sorted(repo_paths):
        try:
            validate_repo_path_for_wrapper(repo_paths, repo, cwd=repo_root)
        except ValueError as exc:
            invalid_repos.append({"repo": repo, "error": str(exc)})
    if invalid_repos:
        checks.append(
            DoctorCheck(
                name="runtime.local_audit.repo_paths",
                status=STATUS_FAIL,
                lane=lane_id,
                message=f"{env_name} has invalid PR-head checkout paths",
                detail={
                    **detail,
                    "repos": sorted(repo_paths),
                    "invalid": invalid_repos,
                },
                remediation=(
                    f"Set {env_name}=OWNER/REPO:/absolute/path, where the path "
                    f"is an existing separate PR-head checkout. {_doc_hint()}"
                ),
            )
        )
        return checks

    checks.append(
        DoctorCheck(
            name="runtime.local_audit.repo_paths",
            status=STATUS_PASS,
            lane=lane_id,
            message=f"{env_name} contains valid absolute PR-head checkout paths",
            detail={**detail, "repos": sorted(repo_paths)},
        )
    )
    return checks


__all__ = ["check_local_audit_wrapper_setup"]
