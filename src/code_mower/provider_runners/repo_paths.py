"""Shared local repository path parsing for provider wrappers."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict

LOCAL_AUDIT_RUNNER_DOC = "docs/local-audit-runner.md"
_REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _doc_hint() -> str:
    return f"See {LOCAL_AUDIT_RUNNER_DOC}."


def parse_repo_paths(spec: str) -> Dict[str, Path]:
    """Parse `OWNER/REPO:/absolute/path,...` into a dict."""

    out: Dict[str, Path] = {}
    for entry in spec.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            raise ValueError(
                f"bad repo paths entry: {entry!r} "
                f"(expected OWNER/REPO:/absolute/path). {_doc_hint()}"
            )
        repo, path = entry.split(":", 1)
        repo = repo.strip()
        path_text = path.strip()
        if not _REPO_SLUG_RE.fullmatch(repo):
            raise ValueError(
                f"bad repo paths entry: {entry!r} "
                f"(expected OWNER/REPO:/absolute/path). {_doc_hint()}"
            )
        repo_path = Path(path_text)
        if not path_text or not repo_path.is_absolute():
            raise ValueError(
                f"bad repo paths entry: {entry!r} "
                f"(expected OWNER/REPO:/absolute/path). {_doc_hint()}"
            )
        out[repo] = repo_path
    return out


def validate_repo_path_for_wrapper(
    repo_paths: Dict[str, Path],
    repo: str,
    *,
    cwd: Path | None = None,
) -> Path:
    """Return the PR-head checkout path for a wrapper invocation or raise."""

    local_repo = repo_paths.get(repo)
    if local_repo is None:
        raise ValueError(
            f"--repo-paths must include {repo} as OWNER/REPO:/absolute/path "
            f"pointing at the separate PR-head checkout. {_doc_hint()}"
        )
    if not local_repo.is_dir():
        raise ValueError(
            f"--repo-paths entry for {repo} points at {local_repo}, "
            f"which is not an existing directory. {_doc_hint()}"
        )

    current = cwd if cwd is not None else Path.cwd()
    if local_repo.resolve() == current.resolve():
        raise ValueError(
            f"--repo-paths entry for {repo} points at the current working "
            "directory; it must point at a separate PR-head checkout, not the "
            f"Code Mower support checkout. {_doc_hint()}"
        )
    return local_repo


__all__ = [
    "LOCAL_AUDIT_RUNNER_DOC",
    "parse_repo_paths",
    "validate_repo_path_for_wrapper",
]
