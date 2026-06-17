"""Shared git helpers for provider-runner wrappers."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Sequence


def run_git(
    repo_path: Path,
    args: Sequence[str],
    *,
    check: bool = True,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    """Run ``git -C repo_path`` and return captured text output."""

    return subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def local_head_sha(repo_path: Path) -> str:
    """Return the current HEAD SHA for a local git checkout."""

    return run_git(repo_path, ["rev-parse", "HEAD"]).stdout.strip()


def fetch_local_checkout_diff(repo_path: Path, *, base_ref: str) -> tuple[str, str]:
    """Return ``(head_sha, diff)`` for a local checkout against ``base_ref``."""

    resolved_repo_path = repo_path.expanduser().resolve()
    if not resolved_repo_path.is_dir():
        raise ValueError(f"repo path does not exist: {resolved_repo_path}")
    head_sha = local_head_sha(resolved_repo_path)
    diff = run_git(
        resolved_repo_path,
        ["diff", "--no-ext-diff", "--find-renames", f"{base_ref}...HEAD"],
        timeout=120,
    ).stdout
    return head_sha, diff
