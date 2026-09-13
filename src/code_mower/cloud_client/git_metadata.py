"""Safe Git metadata helpers for Code Mower Cloud uploads."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from .errors import CloudBundleError


COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def run_git(repo_path: Path, args: list[str]) -> str:
    """Return stdout for a best-effort git command, or an empty string."""
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo_path,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def repo_slug_from_remote(remote_url: str) -> str:
    remote = remote_url.strip()
    if not remote:
        return ""
    if remote.startswith("git@github.com:"):
        remote = remote.removeprefix("git@github.com:")
    elif remote.startswith("https://github.com/"):
        remote = remote.removeprefix("https://github.com/")
    elif remote.startswith("http://github.com/"):
        remote = remote.removeprefix("http://github.com/")
    else:
        return ""
    remote = remote.removesuffix(".git").strip("/")
    parts = remote.split("/")
    if len(parts) >= 2 and parts[0] and parts[1]:
        return f"{parts[0]}/{parts[1]}"
    return ""


def detect_repo_slug(repo_path: Path) -> str:
    return repo_slug_from_remote(run_git(repo_path, ["config", "--get", "remote.origin.url"]))


def _required_git_output(repo_path: Path, args: list[str]) -> str:
    """Return stdout for a git command that must succeed."""
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo_path,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise CloudBundleError(f"unable to run git {args[0]} in {repo_path}: {exc}") from exc
    if completed.returncode != 0:
        raise CloudBundleError(f"git {args[0]} failed in {repo_path}")
    return completed.stdout


UNAVAILABLE_PROVENANCE: dict[str, Any] = {
    "available": False,
    "head_sha": "",
    "clean": False,
    "dirty_entry_count": 0,
}


def checkout_provenance(repo_path: Path, *, required: bool = False) -> dict[str, Any]:
    """Report the exact commit and worktree cleanliness of a source checkout.

    Unlike ``run_git``, a failed command never reads as a clean checkout: a
    directory that is not a usable Git checkout is reported unavailable, and is
    an error when the caller requires provenance.
    """

    try:
        head_sha = _required_git_output(repo_path, ["rev-parse", "HEAD"]).strip()
        if not COMMIT_SHA_PATTERN.match(head_sha):
            raise CloudBundleError(f"unable to resolve an exact HEAD commit in {repo_path}")
        status = _required_git_output(
            repo_path,
            ["status", "--porcelain", "--untracked-files=all"],
        )
    except CloudBundleError:
        if required:
            raise
        return dict(UNAVAILABLE_PROVENANCE)
    dirty_entries = [line for line in status.splitlines() if line.strip()]
    return {
        "available": True,
        "head_sha": head_sha,
        "clean": not dirty_entries,
        "dirty_entry_count": len(dirty_entries),
    }


def require_checkout_provenance(
    provenance: dict[str, Any],
    *,
    expected_head_sha: str = "",
    require_clean: bool = False,
) -> None:
    """Fail closed unless a checkout is the expected commit and clean enough."""

    expected = expected_head_sha.strip().lower()
    if (expected or require_clean) and not provenance.get("available"):
        raise CloudBundleError("source checkout provenance is unavailable")
    if expected:
        if not COMMIT_SHA_PATTERN.match(expected):
            raise CloudBundleError(
                "expected head sha must be an exact 40-character commit"
            )
        if provenance.get("head_sha") != expected:
            raise CloudBundleError("source checkout is not the expected commit")
    if require_clean and not provenance.get("clean"):
        raise CloudBundleError("source checkout has uncommitted or untracked changes")
