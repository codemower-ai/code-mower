"""Shared local repository path parsing for provider wrappers."""

from __future__ import annotations

from pathlib import Path
from typing import Dict


def parse_repo_paths(spec: str) -> Dict[str, Path]:
    """Parse `owner/repo:/path,owner/repo:/path,...` into a dict."""

    out: Dict[str, Path] = {}
    for entry in spec.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            raise ValueError(
                f"bad repo paths entry: {entry!r} (expected owner/repo:/path)"
            )
        repo, path = entry.split(":", 1)
        out[repo.strip()] = Path(path.strip())
    return out
