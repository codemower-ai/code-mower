#!/usr/bin/env python3
"""Shared audit labeler primitives.

Phase 1 of the audit-adapter refactor extracts only the boring pieces that
were duplicated across the lane-specific labelers. Verdict parsing and workflow
entrypoints intentionally stay in the existing scripts until the staged rollout
has proven the shared library on main.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Pattern, Sequence
from urllib.parse import quote
import re

MIN_ABBREVIATED_SHA_LENGTH = 7
AUTHOR_EXCLUSION_ENV = "CODE_MOWER_AUTHOR_EXCLUSION_JSON"
ACTIONS_RUN_MARKER_RE = re.compile(
    r"<!--\s*CODE_MOWER_AUDIT_RUN:\s*run_id=([0-9]+)"
    r"(?:\s+comment_id=([0-9]+))?"
    r"(?:\s+body_sha256=([0-9a-f]{64}))?\s*-->"
)
TRUSTED_ACTIONS_AUDIT_EVENTS = frozenset({"pull_request_target"})
AUDIT_RUN_NON_TERMINAL_STATUSES = frozenset(
    {"queued", "requested", "waiting", "pending", "in_progress"}
)
HEAD_SHA_LINE_RE = re.compile(r"Head SHA:\s*`?([0-9a-fA-F]{7,40})`?", re.IGNORECASE)


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
class ActionsRunMarker:
    run_id: str
    comment_id: str
    body_sha256: str


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

    def default_comment_authors(self) -> frozenset[str]:
        return _author_csv_set(",".join(self.default_authors))

    def configured_comment_authors(self) -> frozenset[str]:
        if not self.authors_env_var:
            return frozenset()
        return _author_csv_set(os.environ.get(self.authors_env_var) or "")

    def comment_authors(self) -> frozenset[str]:
        return self.default_comment_authors() | self.configured_comment_authors()

    def is_default_comment_author(self, login: str) -> bool:
        return login.strip().lower() in self.default_comment_authors()

    def is_configured_comment_author(self, login: str) -> bool:
        return login.strip().lower() in self.configured_comment_authors()

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


def _author_csv_set(raw_authors: str) -> frozenset[str]:
    return frozenset(
        author.strip().lower()
        for author in raw_authors.split(",")
        if author.strip()
    )


class GitHubRequestError(RuntimeError):
    def __init__(self, method: str, path: str, code: int, response_body: str) -> None:
        self.method = method
        self.path = path
        self.code = code
        self.response_body = response_body
        super().__init__(f"GitHub API {method} {path} failed: HTTP {code}\n{response_body}")


class IssueCommentPaginationLimitExceeded(RuntimeError):
    """Raised when issue comments exceed the configured safe pagination cap."""


def _string_mapping(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): str(item)
        for key, item in value.items()
        if str(key) and str(item)
    }


def load_author_exclusion_config(raw: str | None = None) -> Mapping[str, Any]:
    text = raw if raw is not None else os.environ.get(AUTHOR_EXCLUSION_ENV, "")
    if not text:
        return {"enabled": False}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"enabled": False}
    return parsed if isinstance(parsed, Mapping) else {"enabled": False}


def builder_identity_matches(
    *,
    labels: Sequence[str],
    author: str,
    text: str,
    config: Mapping[str, Any],
) -> tuple[str, ...]:
    if not bool(config.get("enabled")):
        return ()
    label_map = _string_mapping(config.get("labels"))
    author_map = {
        key.lower(): value
        for key, value in _string_mapping(config.get("authors")).items()
    }
    matches: list[str] = []

    for label in labels:
        lane = label_map.get(str(label))
        if lane:
            matches.append(lane)

    lane = author_map.get(author.lower())
    if lane:
        matches.append(lane)

    return tuple(dict.fromkeys(matches))


def author_exclusion_reason(
    *,
    lane_name: str,
    labels: Sequence[str],
    author: str,
    text: str,
    config: Mapping[str, Any] | None = None,
) -> str | None:
    exclusion_config = config or load_author_exclusion_config()
    matches = builder_identity_matches(
        labels=labels,
        author=author,
        text=text,
        config=exclusion_config,
    )
    if not matches:
        return None
    if len(set(matches)) > 1:
        return "conflicting builder identity; skipping author-excluded label update"
    builder_lane = matches[0]
    if builder_lane == lane_name:
        return f"{lane_name} lane excluded for builder-authored PR"
    return None


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


def parse_csv_set(raw: str) -> frozenset[str]:
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


def parse_audit_run_id(body: str) -> str:
    marker = parse_audit_run_marker(body)
    return marker.run_id if marker else ""


def parse_audit_run_marker(body: str) -> Optional[ActionsRunMarker]:
    match = ACTIONS_RUN_MARKER_RE.search(body)
    if not match:
        return None
    return ActionsRunMarker(
        run_id=match.group(1),
        comment_id=match.group(2) or "",
        body_sha256=match.group(3) or "",
    )


def audit_run_marker_body_digest_matches(body: str) -> bool:
    match = ACTIONS_RUN_MARKER_RE.search(body)
    if not match or not match.group(2) or not match.group(3):
        return False
    canonical_marker = (
        "<!-- CODE_MOWER_AUDIT_RUN: "
        f"run_id={match.group(1)} comment_id={match.group(2)} -->"
    )
    canonical_body = body[: match.start()] + canonical_marker + body[match.end() :]
    digest = hashlib.sha256(canonical_body.encode("utf-8")).hexdigest()
    return digest == match.group(3)


def workflow_path_matches(path: str, trusted_workflows: Sequence[str]) -> bool:
    if not path:
        return False
    return any(path == item or path.endswith("/" + item) for item in trusted_workflows)


def flatten_paginated_items(payload: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if isinstance(payload, list):
        for page in payload:
            if isinstance(page, list):
                items.extend(item for item in page if isinstance(item, dict))
            elif isinstance(page, dict):
                items.append(page)
    return items


def audit_comment_head_sha(body: str) -> str:
    match = HEAD_SHA_LINE_RE.search(body)
    return match.group(1) if match else ""


def terminal_audit_trailer_verdict(lane: Mapping[str, Any], body: str) -> str:
    candidates: list[tuple[int, str]] = []
    for label_key, verdict in (("done", "done"), ("blocked", "blocked")):
        label = str(lane.get(label_key) or "")
        if not label:
            continue
        index = body.rfind(": " + label + " -->")
        if index >= 0:
            candidates.append((index, verdict))
    if not candidates:
        return ""
    return max(candidates, key=lambda item: item[0])[1]


def current_audit_trailer_verdict(
    lane: Mapping[str, Any],
    body: str,
    *,
    head_sha: str,
) -> str:
    if "Head SHA: `" + head_sha + "`" not in body:
        return ""
    return terminal_audit_trailer_verdict(lane, body)


def latest_current_audit_verdict(
    lane: Mapping[str, Any],
    comments: Sequence[Mapping[str, Any]],
    *,
    head_sha: str,
    trusted_comment_author: Callable[
        [Mapping[str, Any], str, str, object, str | None],
        bool,
    ],
) -> str:
    verdicts: list[tuple[tuple[str, str, int, int], str]] = []
    for index, comment in enumerate(comments):
        author = str(((comment.get("user") or {}).get("login")) or "")
        body = str(comment.get("body") or "")
        if not trusted_comment_author(lane, author, body, comment.get("id"), None):
            continue
        verdict = current_audit_trailer_verdict(lane, body, head_sha=head_sha)
        if verdict:
            try:
                comment_id = int(comment.get("id") or 0)
            except (TypeError, ValueError):
                comment_id = 0
            updated = str(
                comment.get("updated_at")
                or comment.get("updatedAt")
                or comment.get("created_at")
                or comment.get("createdAt")
                or ""
            )
            created = str(comment.get("created_at") or comment.get("createdAt") or "")
            verdicts.append(((updated, created, comment_id, index), verdict))
    if verdicts:
        return max(verdicts, key=lambda item: item[0])[1]
    return ""


def attested_non_current_audit_heads(
    lanes: Sequence[Mapping[str, Any]],
    comments: Sequence[Mapping[str, Any]],
    *,
    head_sha: str,
    trusted_comment_author: Callable[
        [Mapping[str, Any], str, str, object, str | None],
        bool,
    ],
) -> list[str]:
    heads: list[str] = []
    for comment in comments:
        author = str(((comment.get("user") or {}).get("login")) or "")
        body = str(comment.get("body") or "")
        reviewed = audit_comment_head_sha(body)
        if not reviewed or sha_matches(reviewed, head_sha):
            continue
        for lane in lanes:
            if not terminal_audit_trailer_verdict(lane, body):
                continue
            if not trusted_comment_author(lane, author, body, comment.get("id"), reviewed):
                continue
            heads.append(reviewed)
            break
    return list(dict.fromkeys(heads))


def audit_run_workflow_path(run: Mapping[str, Any]) -> str:
    return str(run.get("path") or run.get("workflow_path") or run.get("workflowPath") or "")


def audit_run_matches_pr_head(
    run: Mapping[str, Any],
    *,
    head_sha: str,
    pr_number: int | str,
) -> bool:
    run_head_sha = str(run.get("head_sha") or "")
    pull_requests = [
        item for item in run.get("pull_requests") or [] if isinstance(item, Mapping)
    ]
    for item in pull_requests:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("number") or "") != str(pr_number):
            continue
        pr_head = str(((item.get("head") or {}).get("sha")) or "")
        if pr_head and sha_matches(pr_head, head_sha):
            return True
        if run_head_sha and sha_matches(run_head_sha, head_sha):
            return True
    if pull_requests:
        return False
    if run_head_sha and sha_matches(run_head_sha, head_sha):
        return True
    return False


def _audit_lane_match_tokens(lane: Mapping[str, Any]) -> set[str]:
    tokens = [
        str(lane.get("id") or ""),
        str(lane.get("author_lane") or ""),
        str(lane.get("display_name") or ""),
    ]
    for label_key in ("done", "blocked"):
        label = str(lane.get(label_key) or "")
        for suffix in ("-audit-done", "-audit-blocked", "-done", "-blocked"):
            if label.endswith(suffix):
                tokens.append(label[: -len(suffix)])
    return {token.strip().lower().replace("_", "-") for token in tokens if token.strip()}


def audit_job_matches_lane(job: Mapping[str, Any], lane: Mapping[str, Any]) -> bool:
    name = str(job.get("name") or "").lower().replace("_", "-")
    return any(token in name for token in _audit_lane_match_tokens(lane))


def audit_run_in_flight_for_lane(
    entry: Mapping[str, Any],
    lane: Mapping[str, Any],
    *,
    head_sha: str,
    pr_number: int | str,
) -> bool:
    run = entry.get("run") if isinstance(entry.get("run"), Mapping) else entry
    if not isinstance(run, Mapping):
        return False
    if str(run.get("status") or "") not in AUDIT_RUN_NON_TERMINAL_STATUSES:
        return False
    workflows = parse_csv_set(str(lane.get("github_actions_workflows") or ""))
    if not workflow_path_matches(audit_run_workflow_path(run), workflows):
        return False
    if not audit_run_matches_pr_head(run, head_sha=head_sha, pr_number=pr_number):
        return False
    jobs = entry.get("jobs") if isinstance(entry.get("jobs"), list) else []
    non_terminal_jobs = [
        job
        for job in jobs
        if isinstance(job, Mapping)
        and (
            str(job.get("status") or "") in AUDIT_RUN_NON_TERMINAL_STATUSES
            or (str(job.get("status") or "") != "completed" and job.get("conclusion") is None)
        )
    ]
    if non_terminal_jobs:
        return any(audit_job_matches_lane(job, lane) for job in non_terminal_jobs)
    return bool(entry.get("jobs_fetch_failed") or not jobs)


def _pr_item_matches_head(
    item: Mapping[str, Any],
    *,
    issue_number: int,
    head_sha: str,
    fallback_head_sha: str = "",
) -> bool:
    if str(item.get("number") or "") != str(issue_number):
        return False
    pr_head = str(((item.get("head") or {}).get("sha")) or "")
    if pr_head:
        return sha_matches(pr_head, head_sha)
    return bool(fallback_head_sha) and sha_matches(fallback_head_sha, head_sha)


def fetch_actions_run(
    repo: str,
    run_id: str,
    *,
    tokens: Sequence[GitHubToken],
) -> dict[str, Any]:
    response = github_request_with_fallback(
        "GET",
        f"/repos/{repo}/actions/runs/{run_id}",
        tokens=tokens,
    )
    return response if isinstance(response, dict) else {}


def fetch_pull_requests_for_commit(
    repo: str,
    head_sha: str,
    *,
    tokens: Sequence[GitHubToken],
) -> list[dict[str, Any]]:
    response = github_request_with_fallback(
        "GET",
        f"/repos/{repo}/commits/{head_sha}/pulls?per_page=100",
        tokens=tokens,
    )
    return response if isinstance(response, list) else []


def github_actions_comment_attested(
    *,
    repo: str,
    body: str,
    comment_id: int | str | None = None,
    issue_number: int,
    head_sha: str,
    workflow_paths: Sequence[str],
    tokens: Sequence[GitHubToken],
    actions_run_lookup: Optional[Callable[[str], Mapping[str, Any]]] = None,
    commit_pull_requests_lookup: Optional[Callable[[str], Sequence[Mapping[str, Any]]]] = None,
) -> bool:
    """Return whether a github-actions[bot] audit comment is tied to this PR head."""
    trusted_workflows = tuple(parse_csv_set(",".join(workflow_paths)))
    token_list = tuple(token for token in tokens if token.value)
    marker = parse_audit_run_marker(body)
    comment_id_text = str(comment_id or "").strip()
    if not trusted_workflows or not token_list or marker is None or not head_sha:
        return False
    if not comment_id_text or marker.comment_id != comment_id_text:
        return False
    if not audit_run_marker_body_digest_matches(body):
        return False
    try:
        if actions_run_lookup is None:
            run = fetch_actions_run(repo, marker.run_id, tokens=token_list)
        else:
            run = dict(actions_run_lookup(marker.run_id))
    except Exception:
        return False

    path = str(run.get("path") or "")
    if not workflow_path_matches(path, trusted_workflows):
        return False
    if str(run.get("event") or "") not in TRUSTED_ACTIONS_AUDIT_EVENTS:
        return False

    run_head_sha = str(run.get("head_sha") or "")
    pull_requests = run.get("pull_requests")
    if isinstance(pull_requests, list) and pull_requests:
        return any(
            isinstance(item, Mapping)
            and _pr_item_matches_head(
                item,
                issue_number=issue_number,
                head_sha=head_sha,
                fallback_head_sha=run_head_sha,
            )
            for item in pull_requests
        )
    if not run_head_sha or not sha_matches(run_head_sha, head_sha):
        return False

    try:
        if commit_pull_requests_lookup is None:
            associated_prs = fetch_pull_requests_for_commit(repo, run_head_sha, tokens=token_list)
        else:
            associated_prs = commit_pull_requests_lookup(run_head_sha)
    except Exception:
        return False
    return any(
        isinstance(item, Mapping)
        and _pr_item_matches_head(
            item,
            issue_number=issue_number,
            head_sha=head_sha,
            fallback_head_sha=run_head_sha,
        )
        for item in associated_prs
    )


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
    raise IssueCommentPaginationLimitExceeded(
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
