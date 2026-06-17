"""Safe Git metadata helpers for Code Mower Cloud uploads."""

from __future__ import annotations

import subprocess
from pathlib import Path


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
