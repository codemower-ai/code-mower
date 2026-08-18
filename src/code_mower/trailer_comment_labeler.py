#!/usr/bin/env python3
"""Generic labeler for trailer-comment audit lanes.

Codex, Devin, and Local LLM audit comments all use the same state machine:
trusted comment author -> final verdict trailer/prose -> reviewed head check ->
terminal or needs label. Lane-specific values live in tools/lane_configs/.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

if __package__ and __package__.startswith("code_mower"):
    from .audit_labeler_lib import (
        LaneConfig,
        GitHubToken,
        LabelDecision,
        apply_label_decision,
        author_exclusion_reason,
        extract_reviewed_sha,
        fetch_pull_request,
        fetch_issue_comments,
        github_actions_comment_attested,
        IssueCommentPaginationLimitExceeded,
        load_json,
        parse_csv_set,
        sha_matches_reviewed_head,
    )
    from .lane_configs import load_lane_config
else:
    try:
        from tools.audit_labeler_lib import (
            LaneConfig,
            GitHubToken,
            LabelDecision,
            apply_label_decision,
            author_exclusion_reason,
            extract_reviewed_sha,
            fetch_pull_request,
            fetch_issue_comments,
            github_actions_comment_attested,
            IssueCommentPaginationLimitExceeded,
            load_json,
            parse_csv_set,
            sha_matches_reviewed_head,
        )
        from tools.lane_configs import load_lane_config
    except ImportError:  # pragma: no cover - direct `python tools/foo.py` execution
        from audit_labeler_lib import (
            LaneConfig,
            GitHubToken,
            LabelDecision,
            apply_label_decision,
            author_exclusion_reason,
            extract_reviewed_sha,
            fetch_pull_request,
            fetch_issue_comments,
            github_actions_comment_attested,
            IssueCommentPaginationLimitExceeded,
            load_json,
            parse_csv_set,
            sha_matches_reviewed_head,
        )
        from lane_configs import load_lane_config


HEAD_CHANGED_PATTERN = re.compile(
    r"HEAD_CHANGED_DURING_REVIEW\s*:\s*reviewed\b",
    flags=re.IGNORECASE,
)


def classify_audit_comment(body: str, config: LaneConfig) -> Optional[str]:
    """Return "done", "blocked", "needs", or None for a lane comment body."""
    trailers = list(config.trailer_pattern().finditer(body))
    if trailers:
        label = trailers[-1].group(1).lower()
        if label == config.done_label:
            return "done"
        if label == config.blocked_label:
            return "blocked"
        return "needs"

    if HEAD_CHANGED_PATTERN.search(body):
        return "needs"

    if any(pattern.search(body) for pattern in config.pass_patterns):
        return "done"
    if config.label_state_fallbacks and _has_label_fallback(body, config.done_label):
        return "done"

    if any(pattern.search(body) for pattern in config.blocked_patterns):
        return "blocked"
    if config.label_state_fallbacks and _has_label_fallback(body, config.blocked_label):
        return "blocked"

    return None


def has_authoritative_trailer(body: str, config: LaneConfig) -> bool:
    """Return whether the body carries this lane's explicit audit-state trailer."""
    return bool(config.trailer_pattern().search(body))


def _has_label_fallback(body: str, label: str) -> bool:
    escaped = re.escape(label)
    return bool(
        re.search(
            rf"\**(?:Intended\s+|Expected\s+)?Label state:\**\s*`?\b{escaped}\b",
            body,
            flags=re.IGNORECASE,
        )
        or re.search(
            rf"(?:Intended|Expected)\s+labels?:\s*(?:add\s+)?`?\b{escaped}\b",
            body,
            flags=re.IGNORECASE,
        )
    )


def _comment_sort_key(comment: Mapping[str, Any]) -> tuple[str, int]:
    raw_id = comment.get("id") or 0
    try:
        comment_id = int(raw_id)
    except (TypeError, ValueError):
        comment_id = 0
    return (
        str(comment.get("updated_at") or comment.get("created_at") or ""),
        comment_id,
    )


def _is_latest_current_terminal_comment(
    *,
    event_comment: Mapping[str, Any],
    issue_comments: Sequence[Mapping[str, Any]] | None,
    current_head_sha: str | None,
    config: LaneConfig,
    repo: str,
    tokens: Sequence[GitHubToken],
    github_actions_workflows: Sequence[str],
    actions_run_lookup: Optional[Callable[[str], Mapping[str, Any]]],
    commit_pull_requests_lookup: Optional[Callable[[str], Sequence[Mapping[str, Any]]]],
) -> bool:
    if not issue_comments or not current_head_sha:
        return True
    latest: Mapping[str, Any] | None = None
    for comment in issue_comments:
        author = str(((comment.get("user") or {}).get("login")) or "")
        if author.lower() not in config.comment_authors():
            continue
        body = str(comment.get("body") or "")
        if author.lower() == "github-actions[bot]" and not github_actions_comment_attested(
            repo=repo,
            body=body,
            comment_id=comment.get("id"),
            issue_number=int((comment.get("issue_number") or 0) or 0)
            if comment.get("issue_number")
            else int((event_comment.get("issue_number") or 0) or 0),
            head_sha=current_head_sha,
            workflow_paths=github_actions_workflows,
            tokens=tokens,
            actions_run_lookup=actions_run_lookup,
            commit_pull_requests_lookup=commit_pull_requests_lookup,
        ):
            continue
        if (
            config.is_configured_comment_author(author)
            and not config.is_default_comment_author(author)
            and not has_authoritative_trailer(body, config)
        ):
            continue
        status = classify_audit_comment(body, config)
        if status not in {"done", "blocked"}:
            continue
        reviewed_sha = extract_reviewed_sha(body)
        if not reviewed_sha or not sha_matches_reviewed_head(reviewed_sha, current_head_sha):
            continue
        if latest is None or _comment_sort_key(comment) > _comment_sort_key(latest):
            latest = comment
    if latest is None:
        return True
    return str(latest.get("id") or "") == str(event_comment.get("id") or "")


def resolve_label_decision(
    event: Dict[str, Any],
    *,
    current_head_sha: Optional[str],
    config: LaneConfig,
    repo: str = "",
    tokens: Sequence[GitHubToken] = (),
    github_actions_workflows: Sequence[str] = (),
    actions_run_lookup: Optional[Callable[[str], Mapping[str, Any]]] = None,
    commit_pull_requests_lookup: Optional[Callable[[str], Sequence[Mapping[str, Any]]]] = None,
    issue_comments: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[Optional[LabelDecision], str]:
    if event.get("action") not in {"created", "edited"}:
        return None, f"unsupported action: {event.get('action')}"

    issue = event.get("issue") or {}
    if "pull_request" not in issue:
        return None, "comment is not on a pull request"

    comment = event.get("comment") or {}
    author = (comment.get("user") or {}).get("login", "")
    if author.lower() not in config.comment_authors():
        return None, f"ignored comment author: {author}"

    body = comment.get("body") or ""
    issue_number = int(issue["number"])
    if author.lower() == "github-actions[bot]" and not github_actions_comment_attested(
        repo=repo,
        body=body,
        comment_id=comment.get("id"),
        issue_number=issue_number,
        head_sha=current_head_sha or "",
        workflow_paths=github_actions_workflows,
        tokens=tokens,
        actions_run_lookup=actions_run_lookup,
        commit_pull_requests_lookup=commit_pull_requests_lookup,
    ):
        return None, "github-actions[bot] audit comment is not run-attested"

    if (
        config.is_configured_comment_author(author)
        and not config.is_default_comment_author(author)
        and not has_authoritative_trailer(body, config)
    ):
        return (
            None,
            f"configured author {author} comment is missing matching "
            f"{config.trailer_prefix} trailer",
        )

    status = classify_audit_comment(body, config)
    if status is None:
        return None, f"comment is not a final {config.display_name} audit result"

    issue_labels = [
        str(label.get("name") or "")
        for label in issue.get("labels") or []
        if isinstance(label, dict) and str(label.get("name") or "")
    ]
    issue_author = str(((issue.get("user") or {}).get("login") or ""))
    exclusion = author_exclusion_reason(
        lane_name=config.name,
        labels=issue_labels,
        author=issue_author,
        text=str(issue.get("body") or ""),
    )
    if exclusion:
        return None, exclusion

    reviewed_sha = extract_reviewed_sha(body)
    if status != "needs" and not reviewed_sha:
        return None, "audit result is missing Head SHA"
    if reviewed_sha and current_head_sha and not sha_matches_reviewed_head(reviewed_sha, current_head_sha):
        return LabelDecision(
            issue_number=issue_number,
            add_label=config.needs_label,
            remove_labels=(config.done_label, config.blocked_label),
            reviewed_sha=reviewed_sha,
            reason=f"reviewed SHA {reviewed_sha} does not match current head {current_head_sha}",
        ), "label needs audit"

    if status == "needs":
        return LabelDecision(
            issue_number=issue_number,
            add_label=config.needs_label,
            remove_labels=(config.done_label, config.blocked_label),
            reviewed_sha=reviewed_sha,
            reason="audit reported head changed during review",
        ), "label needs audit"

    if status in {"done", "blocked"} and not _is_latest_current_terminal_comment(
        event_comment={**comment, "issue_number": issue_number},
        issue_comments=issue_comments,
        current_head_sha=current_head_sha,
        config=config,
        repo=repo,
        tokens=tokens,
        github_actions_workflows=github_actions_workflows,
        actions_run_lookup=actions_run_lookup,
        commit_pull_requests_lookup=commit_pull_requests_lookup,
    ):
        return None, "newer current-head audit verdict already exists"

    if status == "done":
        return LabelDecision(
            issue_number=issue_number,
            add_label=config.done_label,
            remove_labels=(config.needs_label, config.blocked_label),
            reviewed_sha=reviewed_sha,
            reason="audit passed",
        ), "label done"

    return LabelDecision(
        issue_number=issue_number,
        add_label=config.blocked_label,
        remove_labels=(config.needs_label, config.done_label),
        reviewed_sha=reviewed_sha,
        reason="audit blocked or incomplete",
    ), "label blocked"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lane", required=True, help="Trailer audit lane from tools/lane_configs/.")
    args = parser.parse_args(argv)
    config = load_lane_config(args.lane)

    event_path = Path(os.environ["GITHUB_EVENT_PATH"])
    repo = os.environ["GITHUB_REPOSITORY"]
    event = load_json(event_path)

    current_head_sha = os.environ.get("DRY_RUN_HEAD_SHA")
    issue_comments: Sequence[Mapping[str, Any]] | None = None
    comment_history_complete = True
    tokens = config.github_tokens_from_env()
    if not os.environ.get("DRY_RUN"):
        if not tokens:
            token_names = " or ".join(config.token_env_vars)
            print(f"error: {token_names} is required", file=sys.stderr)
            return 1
        issue = event.get("issue") or {}
        issue_number = int(issue.get("number", 0))
        if issue_number and "pull_request" in issue:
            current_head_sha = fetch_pull_request(repo, issue_number, tokens=tokens)["head"]["sha"]
            page_cap = int(os.environ.get("CODE_MOWER_LABELER_COMMENT_PAGE_CAP", "10"))
            try:
                issue_comments = fetch_issue_comments(
                    repo,
                    issue_number,
                    tokens=tokens,
                    page_cap=page_cap,
                )
            except IssueCommentPaginationLimitExceeded as exc:
                comment_history_complete = False
                print(
                    f"warning: {exc}; skipping label update because latest verdict cannot be established",
                    file=sys.stderr,
                )

    github_actions_workflows = tuple(
        parse_csv_set(os.environ.get("CODE_MOWER_GITHUB_ACTIONS_WORKFLOWS") or "")
    )
    decision, reason = resolve_label_decision(
        event,
        current_head_sha=current_head_sha,
        config=config,
        repo=repo,
        tokens=tokens,
        github_actions_workflows=github_actions_workflows,
        issue_comments=issue_comments,
    )
    if decision is None:
        print(f"skip: {reason}")
        return 0
    if not comment_history_complete:
        print("skip: comment history pagination cap exceeded; label state unchanged")
        return 0

    if os.environ.get("DRY_RUN"):
        print(json.dumps({"decision": decision.__dict__, "reason": reason}, sort_keys=True))
        return 0

    apply_label_decision(repo, decision, tokens=tokens)
    print(
        f"applied: add {decision.add_label}; remove {', '.join(decision.remove_labels)} "
        f"on {repo}#{decision.issue_number} ({decision.reason})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
