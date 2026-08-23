"""GitHub pull request API helpers for provider runners."""

from __future__ import annotations

import http.client
import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any


def _gh_request(
    method: str,
    path: str,
    *,
    token: str,
    body: dict[str, Any] | None = None,
    accept: str = "application/vnd.github+json",
    timeout: int = 30,
) -> Any:
    """Make a GitHub REST request and return parsed JSON or text diffs."""

    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        data=data,
        headers={
            "Accept": accept,
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method=method,
    )
    max_attempts = 3 if method.upper() in {"GET", "HEAD"} else 1
    transient_errors = (
        TimeoutError,
        socket.timeout,
        http.client.IncompleteRead,
        http.client.RemoteDisconnected,
        urllib.error.URLError,
    )
    for attempt in range(1, max_attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                text = response.read().decode("utf-8", errors="replace")
                if accept.endswith("diff"):
                    return text
                return json.loads(text) if text else None
        except urllib.error.HTTPError:
            raise
        except transient_errors:
            if attempt >= max_attempts:
                raise
            time.sleep(min(2 ** (attempt - 1), 4))
    raise AssertionError("unreachable GitHub request retry loop")


def fetch_pull_request(repo: str, pr_number: int, *, token: str) -> dict[str, Any]:
    return _gh_request("GET", f"/repos/{repo}/pulls/{pr_number}", token=token)


def fetch_pull_request_diff(repo: str, pr_number: int, *, token: str) -> str:
    return str(
        _gh_request(
            "GET",
            f"/repos/{repo}/pulls/{pr_number}",
            token=token,
            accept="application/vnd.github.v3.diff",
        )
    )


def fetch_pull_request_files(
    repo: str,
    pr_number: int,
    *,
    token: str,
    max_pages: int = 5,
    per_page: int = 100,
) -> list[dict[str, Any]]:
    """Return changed-file entries for a pull request.

    GitHub caps pull file pages at 100 entries. Provider runners usually do
    not benefit from reviewing enormous PRs in full, so the default mirrors the
    legacy local-LLM cap of 500 files while keeping the paging behavior shared.
    """

    all_files: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        chunk = _gh_request(
            "GET",
            f"/repos/{repo}/pulls/{pr_number}/files?per_page={per_page}&page={page}",
            token=token,
        )
        if not chunk:
            return all_comments
        if not isinstance(chunk, list):
            raise ValueError("GitHub pull request files response was not a list")
        all_files.extend(chunk)
        if len(chunk) < per_page:
            break
    return all_files


def fetch_issue_comments(
    repo: str,
    issue_number: int,
    *,
    token: str,
    page_cap: int = 10,
    per_page: int = 100,
) -> list[dict[str, Any]]:
    """Return issue/PR comments with a bounded pagination cap."""

    all_comments: list[dict[str, Any]] = []
    for page in range(1, page_cap + 1):
        chunk = _gh_request(
            "GET",
            f"/repos/{repo}/issues/{issue_number}/comments?per_page={per_page}&page={page}",
            token=token,
        )
        if not chunk:
            break
        if not isinstance(chunk, list):
            raise ValueError("GitHub issue comments response was not a list")
        all_comments.extend(comment for comment in chunk if isinstance(comment, dict))
        if len(chunk) < per_page:
            return all_comments
    raise RuntimeError(
        f"hit pagination cap of {page_cap} pages ({page_cap * per_page} comments) "
        f"for {repo}#{issue_number}; refusing to collect partial decision context"
    )


def post_pr_comment(
    repo: str,
    pr_number: int,
    body: str,
    *,
    token: str,
) -> dict[str, Any]:
    return _gh_request(
        "POST",
        f"/repos/{repo}/issues/{pr_number}/comments",
        token=token,
        body={"body": body},
    )


def edit_pr_comment(
    repo: str,
    comment_id: int | str,
    body: str,
    *,
    token: str,
) -> dict[str, Any]:
    return _gh_request(
        "PATCH",
        f"/repos/{repo}/issues/comments/{comment_id}",
        token=token,
        body={"body": body},
    )
