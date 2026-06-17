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
