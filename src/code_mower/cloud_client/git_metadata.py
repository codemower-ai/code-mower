"""Safe Git metadata helpers for Code Mower Cloud uploads."""

from __future__ import annotations

import os
import re
import shutil
import stat
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


def git_top_level(repo_path: Path, *, required: bool = False) -> Path:
    """Return the enclosing repository root for a path inside a checkout.

    Git discovers the enclosing repository from any directory inside it, so a
    nested path and the repository root describe the same source. Callers that
    derive repository-relative inputs resolve the canonical root first, so the
    same repository and commit cannot yield different inputs.
    """

    if required:
        top_level = _required_git_output(repo_path, ["rev-parse", "--show-toplevel"]).strip()
    else:
        top_level = run_git(repo_path, ["rev-parse", "--show-toplevel"]).strip()
    if not top_level:
        if required:
            raise CloudBundleError(f"unable to resolve the git root of {repo_path}")
        return repo_path
    return Path(top_level).expanduser().resolve()


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
    """Remove or restore write permission for a whole private directory tree.

    Only the write bits change: each path keeps its existing read and execute
    bits, so a tracked executable stays executable and the materialization does
    not become dirty through mode changes, and directories stay traversable.
    """

    paths = [root, *sorted(root.rglob("*"), reverse=True)]
    for path in paths:
        try:
            if path.is_symlink():
                continue
            mode = stat.S_IMODE(path.stat().st_mode)
            if writable:
                target = mode | (0o700 if path.is_dir() else 0o600)
            else:
                target = (mode & ~0o222) | (0o500 if path.is_dir() else 0o400)
            if target != mode:
                os.chmod(path, target)
        except OSError:
            if not writable:
                raise CloudBundleError(
                    "unable to make the private exact-commit source read-only"
                ) from None


def _private_tree_is_removed(root: Path) -> bool:
    """Restore write access, remove the private tree, and confirm it is gone.

    Removal is best effort, but the outcome is not assumed: the caller learns
    whether the whole temporary root is actually absent so a leftover private
    checkout cannot be reported as a clean run.
    """

    try:
        _set_tree_permissions(root, writable=True)
    except OSError:
        pass
    shutil.rmtree(root, ignore_errors=True)
    return not root.exists()


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
    collection_error: BaseException | None = None
    try:
        # A clone source must name the repository itself, so the enclosing root
        # is resolved before materializing.
        clone_source = git_top_level(repo_path, required=True)
        # A local clone reads the original repository's objects and writes
        # nothing into it, and the private clone cannot be moved to another
        # commit once it is read-only.
        _required_git_output(
            repo_path,
            [
                "clone",
                "--quiet",
                "--shared",
                "--no-checkout",
                str(clone_source),
                str(source),
            ],
        )
        _required_git_output(source, ["checkout", "--quiet", "--detach", expected])
        materialized = checkout_provenance(source, required=True)
        if materialized.get("head_sha") != expected or not materialized.get("clean"):
            raise CloudBundleError(
                "unable to materialize a clean private checkout of the required commit"
            )
        _set_tree_permissions(source, writable=False)
        # Hardening must not have changed the materialized tree itself, so the
        # exact commit and cleanliness are proven again before it is read.
        hardened = checkout_provenance(source, required=True)
        if hardened != materialized:
            raise CloudBundleError(
                "the private exact-commit source changed while it was made read-only"
            )
        yield source
    except BaseException as error:
        collection_error = error
        raise
    finally:
        if not _private_tree_is_removed(temp_root):
            # A private materialization left on disk is never reported as
            # success, and the bounded message names no path or content.
            cleanup_error = CloudBundleError(
                "unable to remove the private exact-commit source"
            )
            if collection_error is None:
                raise cleanup_error
            raise cleanup_error from collection_error


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
