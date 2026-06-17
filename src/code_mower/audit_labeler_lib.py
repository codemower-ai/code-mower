#!/usr/bin/env python3
"""Shared audit labeler primitives.

Phase 1 of the audit-adapter refactor extracts only the boring pieces that
were duplicated across the lane-specific labelers. Verdict parsing and workflow
entrypoints intentionally stay in the existing scripts until the staged rollout
has proven the shared library on main.
"""

from __future__ import annotations

import functools
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Pattern, Sequence
from urllib.parse import quote
import re

MIN_ABBREVIATED_SHA_LENGTH = 7


@dataclass(frozen=True)
class LabelDecision:
    issue_number: int
    add_label: str
    remove_labels: tuple[str, ...]
    reviewed_sha: Optional[str] = None
    reason: str = ""


@dataclass(frozen=True)
class GitHubToken:
    name: str
    value: str


@dataclass(frozen=True)
class LaneConfig:
    name: str
    display_name: str
    needs_label: str
    done_label: str
    blocked_label: str
    trailer_prefix: str
    default_authors: tuple[str, ...]
    authors_env_var: Optional[str]
    pass_patterns: tuple[Pattern[str], ...]
    blocked_patterns: tuple[Pattern[str], ...]
    label_state_fallbacks: bool = False
    token_env_vars: tuple[str, ...] = ("GITHUB_TOKEN",)

    @functools.lru_cache(maxsize=1)
    def trailer_pattern(self) -> Pattern[str]:
        labels = "|".join(
            re.escape(label)
            for label in (self.done_label, self.blocked_label, self.needs_label)
        )
        return re.compile(
            rf"<!--\s*{re.escape(self.trailer_prefix)}:\s*({labels})\s*-->",
            flags=re.IGNORECASE,
        )

    def comment_authors(self) -> frozenset[str]:
        if self.authors_env_var:
            raw_authors = os.environ.get(self.authors_env_var) or ",".join(self.default_authors)
        else:
            raw_authors = ",".join(self.default_authors)
        return frozenset(author.strip().lower() for author in raw_authors.split(",") if author.strip())

    def github_tokens_from_env(self) -> tuple[GitHubToken, ...]:
        tokens = []
        seen = set()
        for name in self.token_env_vars:
            value = (os.environ.get(name) or "").strip()
            if not value or value in seen:
                continue
            tokens.append(GitHubToken(name, value))
            seen.add(value)
        return tuple(tokens)


class GitHubRequestError(RuntimeError):
    def __init__(self, method: str, path: str, code: int, response_body: str) -> None:
        self.method = method
        self.path = path
        self.code = code
        self.response_body = response_body
        super().__init__(f"GitHub API {method} {path} failed: HTTP {code}\n{response_body}")


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def extract_reviewed_sha(body: str) -> Optional[str]:
    patterns = [
        r"Head SHA:\*\*\s*`?([0-9a-fA-F]{7,40})`?",
        r"Head SHA:\s*`?([0-9a-fA-F]{7,40})`?",
    ]
    for pattern in patterns:
        match = re.search(pattern, body, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def sha_matches_reviewed_head(reviewed_sha: str, current_head_sha: str) -> bool:
    reviewed = reviewed_sha.lower()
    current = current_head_sha.lower()
    if len(reviewed) < MIN_ABBREVIATED_SHA_LENGTH or len(current) < MIN_ABBREVIATED_SHA_LENGTH:
        return False
    return reviewed == current or current.startswith(reviewed)


def sha_matches(a: str, b: str) -> bool:
    a, b = a.lower(), b.lower()
    if len(a) < MIN_ABBREVIATED_SHA_LENGTH or len(b) < MIN_ABBREVIATED_SHA_LENGTH:
        return False
    return a == b or b.startswith(a) or a.startswith(b)


def github_request(
    method: str,
    path: str,
    *,
    token: str,
    body: Optional[Dict[str, Any]] = None,
    allow_missing: bool = False,
) -> Any:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        f"https://api.github.com{path}",
        data=data,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response_body = response.read().decode("utf-8")
            return json.loads(response_body) if response_body else None
    except urllib.error.HTTPError as exc:
        if allow_missing and exc.code == 404:
            return None
        response_body = exc.read().decode("utf-8", errors="replace")
        raise GitHubRequestError(method, path, exc.code, response_body) from exc


def github_request_with_fallback(
    method: str,
    path: str,
    *,
    tokens: Sequence[GitHubToken],
    body: Optional[Dict[str, Any]] = None,
    allow_missing: bool = False,
) -> Any:
    """Use the optional PAT first, then fall back to GITHUB_TOKEN on auth errors."""
    token_list = tuple(tokens)
    last_error: Optional[GitHubRequestError] = None
    for index, token in enumerate(token_list):
        try:
            return github_request(
                method,
                path,
                token=token.value,
                body=body,
                allow_missing=allow_missing,
            )
        except GitHubRequestError as exc:
            last_error = exc
            if exc.code not in {401, 403}:
                raise
            suffix = (
                "; trying next token"
                if index < len(token_list) - 1
                else "; no more tokens"
            )
            print(
                f"warning: {method} {path} failed with HTTP {exc.code} using "
                f"{token.name}{suffix}",
                file=sys.stderr,
            )
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"no GitHub tokens available for {method} {path}")


def fetch_pull_request(
    repo: str,
    issue_number: int,
    *,
    token: Optional[str] = None,
    tokens: Optional[Sequence[GitHubToken]] = None,
) -> Dict[str, Any]:
    path = f"/repos/{repo}/pulls/{issue_number}"
    if tokens is not None:
        response = github_request_with_fallback("GET", path, tokens=tokens)
    else:
        if token is None:
            raise ValueError("token or tokens is required")
        response = github_request("GET", path, token=token)
    if not isinstance(response, dict):
        raise RuntimeError(f"GitHub API GET {path} returned an empty or non-object response")
    return response


def fetch_issue_labels(
    repo: str,
    issue_number: int,
    *,
    tokens: Sequence[GitHubToken],
) -> list[str]:
    response = github_request_with_fallback(
        "GET",
        f"/repos/{repo}/issues/{issue_number}",
        tokens=tokens,
    )
    if not isinstance(response, dict):
        raise RuntimeError("GitHub API issue lookup returned a non-object response")
    return [
        str(label.get("name") or "")
        for label in response.get("labels") or []
        if str(label.get("name") or "")
    ]


def fetch_issue_comments(
    repo: str,
    issue_number: int,
    *,
    tokens: Sequence[GitHubToken],
    page_cap: int,
) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    page = 1
    while page <= page_cap:
        chunk = github_request_with_fallback(
            "GET",
            f"/repos/{repo}/issues/{issue_number}/comments?per_page=100&page={page}",
            tokens=tokens,
        ) or []
        if not isinstance(chunk, list):
            raise RuntimeError("GitHub API issue comments returned a non-list response")
        if not chunk:
            return comments
        comments.extend(comment for comment in chunk if isinstance(comment, dict))
        if len(chunk) < 100:
            return comments
        page += 1
    raise RuntimeError(
        f"hit pagination cap of {page_cap} pages ({page_cap * 100} comments) "
        f"for {repo}#{issue_number}; refusing to classify stale labels on partial data"
    )


def apply_label_decision(
    repo: str,
    decision: LabelDecision,
    *,
    token: Optional[str] = None,
    tokens: Optional[Sequence[GitHubToken]] = None,
) -> None:
    if tokens is None:
        if token is None:
            raise ValueError("token or tokens is required")
        tokens = (GitHubToken("GITHUB_TOKEN", token),)
    github_request_with_fallback(
        "POST",
        f"/repos/{repo}/issues/{decision.issue_number}/labels",
        tokens=tokens,
        body={"labels": [decision.add_label]},
    )
    for label in decision.remove_labels:
        github_request_with_fallback(
            "DELETE",
            f"/repos/{repo}/issues/{decision.issue_number}/labels/{quote(label, safe='')}",
            tokens=tokens,
            allow_missing=True,
        )


def apply_or_log(repo: str, decision: LabelDecision, *, token: str, lane_name: str) -> None:
    """Apply a label decision; treat failures as non-fatal for informational lanes."""
    try:
        apply_label_decision(repo, decision, token=token)
        print(
            f"applied: add {decision.add_label}; remove "
            f"{', '.join(decision.remove_labels)} "
            f"on {repo}#{decision.issue_number} ({decision.reason})"
        )
    except Exception as exc:
        print(
            f"verdict (label apply skipped — {lane_name} lane is "
            f"non-blocking): add {decision.add_label}; remove "
            f"{', '.join(decision.remove_labels)} "
            f"on {repo}#{decision.issue_number} ({decision.reason})"
        )
        print(f"warn: could not apply label: {exc}", file=sys.stderr)
