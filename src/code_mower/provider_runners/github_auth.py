"""GitHub token resolution helpers for provider runner CLIs."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TextIO


def pop_github_token_env() -> str | None:
    """Return GITHUB_TOKEN and clear GitHub token aliases from this process.

    This preserves the legacy direct-invocation behavior for provider wrappers
    while keeping the cleanup logic in one place.
    """

    token = os.environ.pop("GITHUB_TOKEN", None)
    os.environ.pop("GH_TOKEN", None)
    return token


def resolve_github_token_from_stdin_or_env(
    read_from_stdin: bool,
    *,
    stdin: TextIO | None = None,
) -> str | None:
    """Resolve a GitHub token from stdin or the legacy process environment."""

    if read_from_stdin:
        source = stdin if stdin is not None else sys.stdin
        line = source.readline()
        os.environ.pop("GITHUB_TOKEN", None)
        os.environ.pop("GH_TOKEN", None)
        if not line:
            return None
        return line.rstrip("\r\n")
    return pop_github_token_env()


def resolve_github_token_from_env_or_gh() -> str:
    """Resolve a GitHub token from env or `gh auth token` for local lanes."""

    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    try:
        completed = subprocess.run(
            ["gh", "auth", "token"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ):
        return ""
    return completed.stdout.strip()
