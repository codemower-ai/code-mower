"""Safe Git metadata helpers for Code Mower Cloud uploads."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
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


def _set_tree_permissions(root: Path, *, writable: bool) -> None:
    """Remove or restore write permission for a whole private directory tree."""

    directory_mode = 0o700 if writable else 0o500
    file_mode = 0o600 if writable else 0o400
    paths = [root, *sorted(root.rglob("*"), reverse=True)]
    for path in paths:
        try:
            if path.is_symlink():
                continue
            os.chmod(path, directory_mode if path.is_dir() else file_mode)
        except OSError:
            if not writable:
                raise CloudBundleError(
                    "unable to make the private exact-commit source read-only"
                ) from None


@contextmanager
def materialized_commit_source(repo_path: Path, commit_sha: str) -> Iterator[Path]:
    """Yield a private read-only checkout materialized from an exact commit.

    Tracked and source-derived data is read from Git objects in a private
    location rather than from the caller's mutable worktree, so a tracked file
    that is changed and restored while the data is read cannot be observed at
    all: the only state reachable during collection is the required commit.
    The materialization is made non-writable for the collection interval, so an
    attempt to mutate it fails, and it is removed on every exit.
    """

    expected = commit_sha.strip().lower()
    if not COMMIT_SHA_PATTERN.match(expected):
        raise CloudBundleError("expected head sha must be an exact 40-character commit")
    temp_root = Path(tempfile.mkdtemp(prefix="code-mower-exact-commit-"))
    source = temp_root / "source"
    try:
        # A local clone reads the original repository's objects and writes
        # nothing into it, and the private clone cannot be moved to another
        # commit once it is read-only.
        _required_git_output(
            repo_path,
            ["clone", "--quiet", "--shared", "--no-checkout", str(repo_path), str(source)],
        )
        _required_git_output(source, ["checkout", "--quiet", "--detach", expected])
        materialized = checkout_provenance(source, required=True)
        if materialized.get("head_sha") != expected or not materialized.get("clean"):
            raise CloudBundleError(
                "unable to materialize a clean private checkout of the required commit"
            )
        _set_tree_permissions(source, writable=False)
        yield source
    finally:
        _set_tree_permissions(temp_root, writable=True)
        shutil.rmtree(temp_root, ignore_errors=True)


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
