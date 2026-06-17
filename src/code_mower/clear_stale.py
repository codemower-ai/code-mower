#!/usr/bin/env python3
"""Clear stale terminal audit labels after a pull request head changes."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

if __package__ and __package__.startswith("code_mower"):
    from .audit_labeler_lib import (
        GitHubToken,
        IssueCommentPaginationLimitExceeded,
        LabelDecision,
        apply_label_decision,
        fetch_issue_comments,
        fetch_issue_labels,
        fetch_pull_request,
        extract_reviewed_sha,
        github_request_with_fallback,
        load_json,
        sha_matches_reviewed_head,
    )
    from .lane_configs import load_lane_config
    from .trailer_comment_labeler import classify_audit_comment
else:
    try:
        from tools.audit_labeler_lib import (
            GitHubToken,
            IssueCommentPaginationLimitExceeded,
            LabelDecision,
            apply_label_decision,
            fetch_issue_comments,
            fetch_issue_labels,
            fetch_pull_request,
            extract_reviewed_sha,
            github_request_with_fallback,
            load_json,
            sha_matches_reviewed_head,
        )
        from tools.lane_configs import load_lane_config
        from tools.trailer_comment_labeler import classify_audit_comment
    except ImportError:  # pragma: no cover - direct `python tools/foo.py` execution
        from audit_labeler_lib import (
            GitHubToken,
            IssueCommentPaginationLimitExceeded,
            LabelDecision,
            apply_label_decision,
            fetch_issue_comments,
            fetch_issue_labels,
            fetch_pull_request,
            extract_reviewed_sha,
            github_request_with_fallback,
            load_json,
            sha_matches_reviewed_head,
        )
        from lane_configs import load_lane_config
        from trailer_comment_labeler import classify_audit_comment


@dataclass(frozen=True)
class TrustedTerminalComment:
    status: str
    reviewed_sha: str
    author: str
    created_at: str


def _status_label(config: Any, status: str) -> str:
    return config.done_label if status == "done" else config.blocked_label


@dataclass(frozen=True)
class StaleClearResult:
    decision: Optional[LabelDecision]
    reason: str
    requeue_added: bool = False
    dispatch_requested: bool = False


def _comment_created_at(comment: dict[str, Any]) -> str:
    return str(comment.get("created_at") or "")


def _extract_reviewed_sha(body: str) -> Optional[str]:
    # Keep this local wrapper so no-SHA fallback comments never preserve terminal labels.
    return extract_reviewed_sha(body)


def latest_trusted_terminal_comment(
    comments: Sequence[dict[str, Any]],
    *,
    config: Any,
) -> Optional[TrustedTerminalComment]:
    trusted_authors = config.comment_authors()
    ordered = sorted(comments, key=_comment_created_at, reverse=True)
    for comment in ordered:
        author = str(((comment.get("user") or {}).get("login") or ""))
        if author.lower() not in trusted_authors:
            continue
        body = str(comment.get("body") or "")
        status = classify_audit_comment(body, config)
        if status not in {"done", "blocked"}:
            continue
        reviewed_sha = _extract_reviewed_sha(body)
        if not reviewed_sha:
            continue
        return TrustedTerminalComment(
            status=status,
            reviewed_sha=reviewed_sha,
            author=author,
            created_at=_comment_created_at(comment),
        )
    return None


def resolve_stale_clear_decision(
    *,
    issue_number: int,
    current_head_sha: str,
    labels: Sequence[str],
    comments: Sequence[dict[str, Any]],
    config: Any,
) -> StaleClearResult:
    label_set = set(labels)
    terminal_labels = {config.done_label, config.blocked_label}
    present_terminal = terminal_labels.intersection(label_set)
    if not present_terminal:
        return StaleClearResult(None, "no terminal audit labels present")

    current_comment = latest_trusted_terminal_comment(comments, config=config)
    if current_comment and sha_matches_reviewed_head(
        current_comment.reviewed_sha,
        current_head_sha,
    ):
        expected_label = _status_label(config, current_comment.status)
        wrong_terminal_labels = tuple(sorted(present_terminal - {expected_label}))
        if expected_label in label_set and not wrong_terminal_labels:
            return StaleClearResult(
                None,
                (
                    f"terminal label is current: {current_comment.status} by "
                    f"{current_comment.author} for {current_comment.reviewed_sha}"
                ),
            )
        return StaleClearResult(
            LabelDecision(
                issue_number=issue_number,
                add_label=expected_label,
                remove_labels=(config.needs_label, *wrong_terminal_labels),
                reviewed_sha=current_comment.reviewed_sha,
                reason=(
                    f"repaired terminal label mismatch for current head "
                    f"{current_head_sha}: expected {expected_label}"
                ),
            ),
            (
                "terminal audit label mismatch for current trusted "
                f"{current_comment.status} result"
            ),
        )

    decision = LabelDecision(
        issue_number=issue_number,
        add_label=config.needs_label,
        remove_labels=(config.done_label, config.blocked_label),
        reviewed_sha=current_comment.reviewed_sha if current_comment else None,
        reason=(
            f"cleared stale terminal labels after head moved to {current_head_sha}"
        ),
    )
    return StaleClearResult(
        decision,
        "terminal audit label is stale or lacks a current trusted Head SHA",
        requeue_added=config.needs_label not in label_set,
    )


def _event_pr_context(event: dict[str, Any]) -> tuple[int, Optional[str], list[str]]:
    pull_request = event.get("pull_request") or {}
    issue_number = int(pull_request.get("number") or event.get("number") or 0)
    head_sha = ((pull_request.get("head") or {}).get("sha") or None)
    labels = [
        str(label.get("name") or "")
        for label in pull_request.get("labels") or []
        if str(label.get("name") or "")
    ]
    return issue_number, head_sha, labels


def _render_dispatch_input(template: str, *, issue_number: int, head_sha: str, lane: str) -> str:
    return (
        template.replace("{pr}", str(issue_number))
        .replace("{pr_number}", str(issue_number))
        .replace("{head}", head_sha)
        .replace("{head_sha}", head_sha)
        .replace("{lane}", lane)
    )


def _parse_dispatch_inputs(
    entries: Sequence[str],
    *,
    issue_number: int,
    head_sha: str,
    lane: str,
) -> dict[str, str]:
    inputs: dict[str, str] = {}
    for entry in entries:
        if "=" not in entry:
            raise ValueError(f"dispatch input must be name=value: {entry}")
        name, value = entry.split("=", 1)
        name = name.strip()
        if not name:
            raise ValueError(f"dispatch input has an empty name: {entry}")
        inputs[name] = _render_dispatch_input(
            value,
            issue_number=issue_number,
            head_sha=head_sha,
            lane=lane,
        )
    return inputs


def dispatch_workflow(
    repo: str,
    workflow: str,
    *,
    ref: str,
    inputs: dict[str, str],
    tokens: Sequence[GitHubToken],
) -> None:
    github_request_with_fallback(
        "POST",
        f"/repos/{repo}/actions/workflows/{workflow}/dispatches",
        tokens=tokens,
        body={"ref": ref, "inputs": inputs},
    )


def _default_dispatch_ref(event: dict[str, Any]) -> str:
    pull_request = event.get("pull_request") or {}
    base = pull_request.get("base") or {}
    repo = event.get("repository") or {}
    return str(base.get("ref") or repo.get("default_branch") or os.environ.get("GITHUB_REF_NAME") or "main")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lane", required=True, help="Trailer audit lane from lane_configs/.")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--pr", type=int, default=0)
    parser.add_argument("--head-sha", default="")
    parser.add_argument("--event-path", default=os.environ.get("GITHUB_EVENT_PATH", ""))
    parser.add_argument("--page-cap", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--dispatch-workflow",
        default="",
        help="optional workflow filename/id to workflow_dispatch after a new stale requeue",
    )
    parser.add_argument("--dispatch-ref", default="")
    parser.add_argument(
        "--dispatch-input",
        action="append",
        default=[],
        help="workflow_dispatch input as name=value; supports {pr}, {head}, and {lane}",
    )
    args = parser.parse_args(argv)

    config = load_lane_config(args.lane)
    tokens = config.github_tokens_from_env()
    if not args.dry_run and not tokens:
        token_names = " or ".join(config.token_env_vars)
        print(f"error: {token_names} is required", file=sys.stderr)
        return 1

    event = load_json(Path(args.event_path)) if args.event_path else {}
    repo = args.repo or str((event.get("repository") or {}).get("full_name") or "")
    if not repo:
        print("error: --repo or GITHUB_REPOSITORY is required", file=sys.stderr)
        return 1

    event_issue_number, event_head_sha, event_labels = _event_pr_context(event)
    issue_number = args.pr or event_issue_number
    current_head_sha = args.head_sha or event_head_sha or ""
    labels = event_labels
    if not args.dry_run:
        if not issue_number:
            print("error: --pr or pull_request event is required", file=sys.stderr)
            return 1
        pull_request = fetch_pull_request(repo, issue_number, tokens=tokens)
        current_head_sha = str((pull_request.get("head") or {}).get("sha") or current_head_sha)
        labels = fetch_issue_labels(repo, issue_number, tokens=tokens)
        try:
            comments = fetch_issue_comments(
                repo,
                issue_number,
                tokens=tokens,
                page_cap=args.page_cap,
            )
        except IssueCommentPaginationLimitExceeded as exc:
            # Conservative for merge-authority lanes: if we cannot inspect enough
            # comments to prove a terminal label is current, clear and requeue
            # rather than leaving stale approval authoritative.
            print(f"warning: {exc}; using conservative stale requeue", file=sys.stderr)
            comments = []
    else:
        comments = list((event.get("comments") or []))

    if not issue_number or not current_head_sha:
        print("error: PR number and head SHA are required", file=sys.stderr)
        return 1

    result = resolve_stale_clear_decision(
        issue_number=issue_number,
        current_head_sha=current_head_sha,
        labels=labels,
        comments=comments,
        config=config,
    )

    dispatch_requested = False
    if result.decision is not None and not args.dry_run:
        apply_label_decision(repo, result.decision, tokens=tokens)
        if args.dispatch_workflow and result.requeue_added:
            dispatch_ref = args.dispatch_ref or _default_dispatch_ref(event)
            dispatch_inputs = _parse_dispatch_inputs(
                args.dispatch_input,
                issue_number=issue_number,
                head_sha=current_head_sha,
                lane=config.name,
            )
            dispatch_workflow(
                repo,
                args.dispatch_workflow,
                ref=dispatch_ref,
                inputs=dispatch_inputs,
                tokens=tokens,
            )
            dispatch_requested = True

    output = {
        "lane": config.name,
        "repo": repo,
        "pr": issue_number,
        "head_sha": current_head_sha,
        "reason": result.reason,
        "decision": result.decision.__dict__ if result.decision else None,
        "requeue_added": result.requeue_added,
        "dispatch_requested": dispatch_requested,
    }
    if args.json or args.dry_run:
        print(json.dumps(output, indent=2, sort_keys=True))
    elif result.decision:
        print(
            f"applied: add {result.decision.add_label}; remove "
            f"{', '.join(result.decision.remove_labels)} on {repo}#{issue_number} "
            f"({result.reason})"
        )
    else:
        print(f"skip: {result.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
