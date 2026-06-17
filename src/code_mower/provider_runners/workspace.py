"""Shared local checkout validation helpers for provider runners."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .git import local_head_sha, run_git


class ProviderWorkspaceError(RuntimeError):
    """Raised when a provider runner cannot safely use a local checkout."""


def working_tree_status(repo_path: Path) -> str:
    """Return porcelain status for ``repo_path``."""

    return run_git(repo_path, ["status", "--porcelain"]).stdout


def verify_checkout_at_head(
    repo_path: Path,
    *,
    expected_head_sha: str,
    allow_dirty: bool = False,
    purpose: str = "provider runner",
    dirty_remediation: str = (
        "commit/stash them or pass --allow-dirty for an explicitly exploratory run"
    ),
) -> dict[str, Any]:
    """Verify that ``repo_path`` is a git checkout at ``expected_head_sha``."""

    if not repo_path.is_dir():
        raise ProviderWorkspaceError(f"repo path does not exist: {repo_path}")
    try:
        local_head = local_head_sha(repo_path)
    except subprocess.CalledProcessError as exc:
        raise ProviderWorkspaceError(
            f"repo path is not a git checkout: {repo_path}"
        ) from exc
    if local_head.lower() != expected_head_sha.lower():
        raise ProviderWorkspaceError(
            "local checkout must be at the PR head for "
            f"{purpose}; local={local_head} expected={expected_head_sha}."
        )
    status = working_tree_status(repo_path)
    if status.strip() and not allow_dirty:
        raise ProviderWorkspaceError(
            "local checkout has uncommitted changes; "
            f"{dirty_remediation}."
        )
    return {
        "repo_path": str(repo_path),
        "local_head_sha": local_head,
        "dirty": bool(status.strip()),
    }
