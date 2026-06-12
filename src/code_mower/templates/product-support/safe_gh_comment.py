#!/usr/bin/env python3
"""Post or edit GitHub issue/PR comments without shell-interpreting Markdown.

Never pass Markdown through `gh ... --body "..."`: backticks and `$()` inside
the body are shell syntax before GitHub ever sees them. This helper reads the
body from a file or stdin, serializes it as JSON, and invokes `gh api` with
argument-list subprocess calls.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional, Sequence

if TYPE_CHECKING:
    RunFn = Callable[..., subprocess.CompletedProcess[str]]
else:
    # Keep the generated helper importable under older ambient Python 3.x
    # interpreters that cannot evaluate subprocess.CompletedProcess[str].
    RunFn = Callable[..., Any]


def _read_body(*, body_file: Optional[Path], stdin_text: Optional[str] = None) -> str:
    if body_file is not None:
        return body_file.read_text(encoding="utf-8")
    if stdin_text is not None:
        return stdin_text
    return sys.stdin.read()


def _repo_from_gh(run: RunFn = subprocess.run) -> str:
    result = run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def _run_gh_api(args: Sequence[str], payload: "dict[str, str]", run: RunFn = subprocess.run) -> None:
    run(
        ["gh", "api", *args, "--input", "-"],
        input=json.dumps(payload),
        check=True,
        text=True,
        capture_output=True,
    )


def post_comment(repo: str, issue_or_pr: int, body: str, run: RunFn = subprocess.run) -> None:
    _run_gh_api(
        ["-X", "POST", f"repos/{repo}/issues/{issue_or_pr}/comments"],
        {"body": body},
        run,
    )


def edit_comment(repo: str, comment_id: int, body: str, run: RunFn = subprocess.run) -> None:
    _run_gh_api(
        ["-X", "PATCH", f"repos/{repo}/issues/comments/{comment_id}"],
        {"body": body},
        run,
    )


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--issue", "--pr", dest="issue_or_pr", type=int)
    target.add_argument("--edit-comment-id", type=int)
    parser.add_argument("--repo", help="owner/repo. Defaults to `gh repo view`.")
    parser.add_argument("--body-file", type=Path, help="Markdown file to post/edit. Defaults to stdin.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    try:
        repo = args.repo or _repo_from_gh()
        body = _read_body(body_file=args.body_file)
        if args.edit_comment_id is not None:
            edit_comment(repo, args.edit_comment_id, body)
        else:
            post_comment(repo, args.issue_or_pr, body)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
