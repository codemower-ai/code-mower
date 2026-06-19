"""Shared PR fetch and worktree helpers for provider audit runners."""

from __future__ import annotations

import os
import secrets
import shutil
import tempfile
from pathlib import Path
from typing import Sequence

from .git import run_git


class FetchedHeadMismatch(RuntimeError):
    """Raised when a freshly fetched PR head differs from the expected SHA."""

    def __init__(self, expected_sha: str, actual_sha: str) -> None:
        self.expected_sha = expected_sha
        self.actual_sha = actual_sha
        super().__init__(
            f"fetched PR head {actual_sha} does not match expected {expected_sha}"
        )


def run_git_text(
    repo_path: Path,
    args: Sequence[str],
    *,
    timeout: int = 60,
) -> str:
    """Run ``git`` in ``repo_path`` and return stdout text."""

    return run_git(repo_path, args, timeout=timeout).stdout


def fetch_base_ref(repo_path: Path, base_ref: str, *, remote: str = "origin") -> None:
    """Refresh ``base_ref`` from ``remote`` for provider review diffs."""

    if "/" in base_ref and base_ref.startswith(f"{remote}/"):
        remote_branch = base_ref[len(f"{remote}/") :]
        run_git(repo_path, ["fetch", remote, remote_branch])
        return
    run_git(repo_path, ["fetch", remote, base_ref])


def fetch_base_ref_sha(repo_path: Path, base_ref: str, *, remote: str = "origin") -> str:
    """Fetch ``base_ref`` and return the resolved commit SHA.

    Common branch forms are fetched into their remote-tracking refs. Less common
    refs are fetched into a temporary namespace and deleted after resolution so
    callers can build stable diff ranges without leaving repo-local residue.
    """

    temporary_ref = False
    if base_ref.startswith(f"{remote}/"):
        remote_branch = base_ref[len(f"{remote}/") :]
        local_ref = f"refs/remotes/{remote}/{remote_branch}"
        fetch_refspec = f"+{remote_branch}:{local_ref}"
    elif base_ref.startswith("refs/heads/"):
        remote_branch = base_ref[len("refs/heads/") :]
        local_ref = f"refs/remotes/{remote}/{remote_branch}"
        fetch_refspec = f"+{base_ref}:{local_ref}"
    elif "/" not in base_ref:
        local_ref = f"refs/remotes/{remote}/{base_ref}"
        fetch_refspec = f"+{base_ref}:{local_ref}"
    else:
        local_ref = f"refs/code-mower/base/{os.getpid()}-{secrets.token_hex(8)}"
        fetch_refspec = f"+{base_ref}:{local_ref}"
        temporary_ref = True

    try:
        run_git(repo_path, ["fetch", remote, fetch_refspec])
        return run_git_text(
            repo_path,
            ["rev-parse", "--verify", f"{local_ref}^{{commit}}"],
        ).strip()
    finally:
        if temporary_ref:
            run_git(repo_path, ["update-ref", "-d", local_ref], check=False)


def fetch_pr_head(repo_path: Path, pr_number: int, *, remote: str = "origin") -> None:
    """Ensure ``pull/<pr_number>/head`` is locally available."""

    run_git(repo_path, ["fetch", remote, f"pull/{pr_number}/head"])


def fetch_pr_head_sha(repo_path: Path, pr_number: int, *, remote: str = "origin") -> str:
    """Fetch a PR head into a temporary ref and return its commit SHA."""

    local_ref = (
        f"refs/code-mower/pr/{pr_number}/{os.getpid()}-{secrets.token_hex(8)}"
    )
    try:
        run_git(repo_path, ["fetch", remote, f"+pull/{pr_number}/head:{local_ref}"])
        return run_git_text(
            repo_path,
            ["rev-parse", "--verify", f"{local_ref}^{{commit}}"],
        ).strip()
    finally:
        run_git(repo_path, ["update-ref", "-d", local_ref], check=False)


def fetch_pr_head_sha_or_raise(
    repo_path: Path,
    pr_number: int,
    *,
    expected_head_sha: str,
    remote: str = "origin",
) -> str:
    """Fetch a PR head and raise if it moved from ``expected_head_sha``."""

    fetched_head_sha = fetch_pr_head_sha(repo_path, pr_number, remote=remote)
    if fetched_head_sha.lower() != expected_head_sha.lower():
        raise FetchedHeadMismatch(expected_head_sha, fetched_head_sha)
    return fetched_head_sha


def create_temp_worktree(
    repo_path: Path,
    head_sha: str,
    *,
    prefix: str = "code-mower-audit-",
) -> Path:
    """Create a detached worktree for ``head_sha`` and return its path."""

    tmp_root = Path(tempfile.mkdtemp(prefix=prefix))
    worktree_path = tmp_root / "wt"
    try:
        run_git(repo_path, ["worktree", "add", "--detach", str(worktree_path), head_sha])
    except Exception:
        shutil.rmtree(tmp_root, ignore_errors=True)
        raise
    return worktree_path


def remove_worktree(repo_path: Path, worktree_path: Path) -> None:
    """Best-effort remove of a temporary provider worktree."""

    try:
        run_git(
            repo_path,
            ["worktree", "remove", "--force", str(worktree_path)],
            check=False,
        )
    except Exception:  # noqa: BLE001 - cleanup must not mask audit failures.
        pass
    try:
        worktree_path.parent.rmdir()
    except Exception:  # noqa: BLE001 - temp parent may already be gone.
        pass
