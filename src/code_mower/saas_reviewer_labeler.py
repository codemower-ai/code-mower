#!/usr/bin/env python3
"""Generic labeler for advisory SaaS reviewer lanes.

Greptile and Gitar expose different GitHub event shapes, but their label
state machine is the same: trust only the service bot, classify its native
verdict, then apply an advisory done/blocked/needs label without failing the
workflow if label writes are unavailable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

if __package__ and __package__.startswith("code_mower"):
    from .adapters import load_adapter
    from .adapters._base import SaaSReviewerAdapter
    from .audit_labeler_lib import (
        GitHubToken,
        LabelDecision,
        GitHubRequestError,
        apply_label_decision,
        fetch_pull_request,
        github_request_with_fallback,
        load_json,
        sha_matches,
    )
else:
    try:
        from tools.adapters import load_adapter
        from tools.adapters._base import SaaSReviewerAdapter
        from tools.audit_labeler_lib import (
            GitHubToken,
            LabelDecision,
            GitHubRequestError,
            apply_label_decision,
            fetch_pull_request,
            github_request_with_fallback,
            load_json,
            sha_matches,
        )
    except ImportError:  # pragma: no cover - direct `python tools/foo.py` execution
        from adapters import load_adapter
        from adapters._base import SaaSReviewerAdapter
        from audit_labeler_lib import (
            GitHubToken,
            LabelDecision,
            GitHubRequestError,
            apply_label_decision,
            fetch_pull_request,
            github_request_with_fallback,
            load_json,
            sha_matches,
        )


class ReviewCommentsTruncated(RuntimeError):
    """Raised when a review-comments fetch hits the safety page cap."""


def github_tokens_from_env(*env_names: str) -> tuple[GitHubToken, ...]:
    tokens: list[GitHubToken] = []
    seen_values: set[str] = set()
    for name in env_names:
        value = (os.environ.get(name) or "").strip()
        if not value or value in seen_values:
            continue
        tokens.append(GitHubToken(name, value))
        seen_values.add(value)
    return tuple(tokens)


def env_flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() not in {
        "",
        "0",
        "false",
        "no",
    }


def fetch_review_comments(
    repo: str,
    pr_number: int,
    review_id: int,
    *,
    tokens: Sequence[GitHubToken],
    page_cap: int,
) -> list[dict[str, Any]]:
    """Fetch inline comments for one pull request review with a safety cap."""
    all_comments: list[dict[str, Any]] = []
    page = 1
    while page <= page_cap:
        chunk = github_request_with_fallback(
            "GET",
            f"/repos/{repo}/pulls/{pr_number}/reviews/{review_id}/comments"
            f"?per_page=100&page={page}",
            tokens=tokens,
        ) or []
        if not chunk:
            return all_comments
        all_comments.extend(chunk)
        if len(chunk) < 100:
            return all_comments
        page += 1
    raise ReviewCommentsTruncated(
        f"hit pagination cap of {page_cap} pages ({page_cap * 100} comments) "
        f"for {repo}#{pr_number} review {review_id}; refusing to classify on partial data"
    )


def fetch_pull_requests_for_commit(
    repo: str,
    head_sha: str,
    *,
    tokens: Sequence[GitHubToken],
) -> list[dict[str, Any]]:
    """Fetch pull requests associated with a commit SHA."""
    pulls = github_request_with_fallback(
        "GET",
        f"/repos/{repo}/commits/{head_sha}/pulls?per_page=100",
        tokens=tokens,
    ) or []
    return pulls if isinstance(pulls, list) else []


def fetch_pull_request_reviews(
    repo: str,
    pr_number: int,
    *,
    tokens: Sequence[GitHubToken],
    page_cap: int,
) -> list[dict[str, Any]]:
    """Fetch pull request reviews with a safety cap."""
    all_reviews: list[dict[str, Any]] = []
    page = 1
    while page <= page_cap:
        chunk = github_request_with_fallback(
            "GET",
            f"/repos/{repo}/pulls/{pr_number}/reviews?per_page=100&page={page}",
            tokens=tokens,
        ) or []
        if not chunk:
            return all_reviews
        all_reviews.extend(chunk)
        if len(chunk) < 100:
            return all_reviews
        page += 1
    raise ReviewCommentsTruncated(
        f"hit pagination cap of {page_cap} pages ({page_cap * 100} reviews) "
        f"for {repo}#{pr_number}; refusing to classify on partial data"
    )


def fetch_issue_comments(
    repo: str,
    issue_number: int,
    *,
    tokens: Sequence[GitHubToken],
    page_cap: int,
) -> list[dict[str, Any]]:
    """Fetch issue/PR comments with a safety cap."""
    all_comments: list[dict[str, Any]] = []
    page = 1
    while page <= page_cap:
        chunk = github_request_with_fallback(
            "GET",
            f"/repos/{repo}/issues/{issue_number}/comments?per_page=100&page={page}",
            tokens=tokens,
        ) or []
        if not chunk:
            return all_comments
        all_comments.extend(chunk)
        if len(chunk) < 100:
            return all_comments
        page += 1
    raise ReviewCommentsTruncated(
        f"hit pagination cap of {page_cap} pages ({page_cap * 100} comments) "
        f"for {repo}#{issue_number}; refusing to classify on partial data"
    )


def has_same_head_review(
    reviews: list[dict[str, Any]],
    *,
    adapter: SaaSReviewerAdapter,
    head_sha: Optional[str],
) -> bool:
    """Return whether the adapter already has a PR review for this head."""
    if not head_sha:
        return False
    for review in reviews:
        author = ((review.get("user") or {}).get("login") or "")
        commit_id = review.get("commit_id") or ""
        if adapter.is_review_author(author) and sha_matches(str(commit_id), head_sha):
            return True
    return False


def resolve_label_decision(
    event: dict[str, Any],
    *,
    adapter: SaaSReviewerAdapter,
    event_type: Optional[str] = None,
    pr_number: Optional[int] = None,
    pr_labels: Optional[list[str]] = None,
    current_head_sha: Optional[str] = None,
    review_comments: Optional[list[dict[str, Any]]] = None,
    same_head_review_exists: bool = False,
) -> tuple[Optional[LabelDecision], str]:
    event_type = event_type or adapter.event_type
    if event_type == "pull_request_review":
        return _resolve_pull_request_review(
            event,
            adapter=adapter,
            pr_labels=pr_labels or [],
            current_head_sha=current_head_sha,
            review_comments=review_comments or [],
        )
    if event_type == "issue_comment":
        return _resolve_issue_comment(event, adapter=adapter, pr_labels=pr_labels or [])
    if event_type == "check_run":
        return _resolve_check_run(
            event,
            adapter=adapter,
            pr_number=pr_number,
            pr_labels=pr_labels or [],
            current_head_sha=current_head_sha,
            same_head_review_exists=same_head_review_exists,
        )
    return None, f"unsupported adapter event type: {event_type}"


def _resolve_pull_request_review(
    event: dict[str, Any],
    *,
    adapter: SaaSReviewerAdapter,
    pr_labels: list[str],
    current_head_sha: Optional[str],
    review_comments: list[dict[str, Any]],
) -> tuple[Optional[LabelDecision], str]:
    if event.get("action") not in ("submitted", "edited"):
        return None, f"unsupported pull_request_review action: {event.get('action')}"

    review = event.get("review") or {}
    author = (review.get("user") or {}).get("login", "")
    if not adapter.is_review_author(author):
        return None, f"ignored review author: {author}"

    pull_request = event.get("pull_request") or {}
    issue_number = int(pull_request.get("number") or 0)
    if not issue_number:
        return None, "event has no pull request number"

    if adapter.opt_in_required and not adapter.is_opted_in(pr_labels):
        return None, (
            f"PR does not carry an opt-in {adapter.label_prefix} label; "
            f"add `{adapter.needs_label}` to opt in"
        )

    reviewed_sha, needs_stale_check = adapter.extract_stale_head_info(event)
    if (
        needs_stale_check
        and current_head_sha
        and reviewed_sha
        and not sha_matches(reviewed_sha, current_head_sha)
    ):
        return LabelDecision(
            issue_number=issue_number,
            add_label=adapter.needs_label,
            remove_labels=(adapter.done_label, adapter.blocked_label),
            reviewed_sha=reviewed_sha,
            reason=f"review SHA {reviewed_sha[:8]} != current head {current_head_sha[:8]}",
        ), "stale review — re-queue"

    status = adapter.classify_verdict(event, review_comments=review_comments)
    return _decision_from_status(
        adapter=adapter,
        event=event,
        issue_number=issue_number,
        status=status,
        reviewed_sha=reviewed_sha,
        review_comments=review_comments,
    )


def _resolve_issue_comment(
    event: dict[str, Any],
    *,
    adapter: SaaSReviewerAdapter,
    pr_labels: list[str],
) -> tuple[Optional[LabelDecision], str]:
    if event.get("action") not in ("created", "edited"):
        return None, f"unsupported issue_comment action: {event.get('action')}"

    issue = event.get("issue") or {}
    if "pull_request" not in issue:
        return None, "comment is on an issue, not a pull request"

    issue_number = int(issue.get("number") or 0)
    if not issue_number:
        return None, "event has no issue/PR number"

    comment = event.get("comment") or {}
    author = (comment.get("user") or {}).get("login", "")
    if not adapter.is_review_author(author):
        return None, f"ignored comment author: {author}"

    if adapter.opt_in_required and not adapter.is_opted_in(pr_labels):
        return None, (
            f"PR does not carry an opt-in {adapter.label_prefix} label; "
            f"add `{adapter.needs_label}` to opt in"
        )

    comment_body = comment.get("body") or ""
    status = adapter.classify_verdict(event, comment_body=comment_body)
    return _decision_from_status(
        adapter=adapter,
        event=event,
        issue_number=issue_number,
        status=status,
        comment_body=comment_body,
    )


def _resolve_check_run(
    event: dict[str, Any],
    *,
    adapter: SaaSReviewerAdapter,
    pr_number: Optional[int],
    pr_labels: list[str],
    current_head_sha: Optional[str],
    same_head_review_exists: bool,
) -> tuple[Optional[LabelDecision], str]:
    if event.get("action") != "completed":
        return None, f"unsupported check_run action: {event.get('action')}"

    check_run = event.get("check_run") or {}
    if check_run.get("status") != "completed":
        return None, f"check_run is not completed: {check_run.get('status')}"
    if not adapter.is_check_run_author(check_run):
        app = check_run.get("app") or {}
        return None, f"ignored check_run app: {app.get('slug') or app.get('name')}"
    if not adapter.is_check_run_name(check_run):
        return None, f"ignored check_run name: {check_run.get('name')}"
    if not pr_number:
        return None, "check_run event has no associated pull request number"

    if adapter.opt_in_required and not adapter.is_opted_in(pr_labels):
        return None, (
            f"PR does not carry an opt-in {adapter.label_prefix} label; "
            f"add `{adapter.needs_label}` to opt in"
        )

    reviewed_sha = check_run.get("head_sha") or None
    if reviewed_sha and not current_head_sha:
        if adapter.blocked_label in pr_labels and adapter.needs_label not in pr_labels:
            return None, (
                f"check_run verdict ignored: could not verify current PR head "
                f"and PR already carries `{adapter.blocked_label}`"
            )
        return LabelDecision(
            issue_number=pr_number,
            add_label=adapter.needs_label,
            remove_labels=(adapter.done_label,),
            reviewed_sha=str(reviewed_sha),
            reason=(
                "could not verify current PR head SHA for check_run "
                f"{str(reviewed_sha)[:8]}"
            ),
        ), "missing current head — re-queue"
    if (
        current_head_sha
        and reviewed_sha
        and not sha_matches(str(reviewed_sha), current_head_sha)
    ):
        return LabelDecision(
            issue_number=pr_number,
            add_label=adapter.needs_label,
            remove_labels=(adapter.done_label, adapter.blocked_label),
            reviewed_sha=str(reviewed_sha),
            reason=f"check_run SHA {str(reviewed_sha)[:8]} != current head {current_head_sha[:8]}",
        ), "stale check_run — re-queue"

    status = adapter.classify_verdict(event, check_run=check_run)
    if status == "done" and same_head_review_exists:
        return None, adapter.same_head_review_exists_reason()
    if (
        status != "blocked"
        and adapter.blocked_label in pr_labels
        and adapter.needs_label not in pr_labels
    ):
        return None, (
            f"check_run verdict ({status}) ignored: PR already carries "
            f"`{adapter.blocked_label}` from a prior review verdict"
        )
    if (
        status == "needs"
        and adapter.done_label in pr_labels
        and adapter.needs_label not in pr_labels
    ):
        return None, (
            f"non-final check_run ignored: PR already carries "
            f"`{adapter.done_label}` from a prior review verdict"
        )
    return _decision_from_status(
        adapter=adapter,
        event=event,
        issue_number=pr_number,
        status=status,
        reviewed_sha=str(reviewed_sha) if reviewed_sha else None,
        check_run=check_run,
    )


def _decision_from_status(
    *,
    adapter: SaaSReviewerAdapter,
    event: dict[str, Any],
    issue_number: int,
    status: Optional[str],
    reviewed_sha: Optional[str] = None,
    **kwargs: Any,
) -> tuple[Optional[LabelDecision], str]:
    if status is None:
        return None, f"no {adapter.name} verdict — skipping"
    if status == "needs":
        return LabelDecision(
            issue_number=issue_number,
            add_label=adapter.needs_label,
            remove_labels=(adapter.done_label, adapter.blocked_label),
            reviewed_sha=reviewed_sha,
            reason=adapter.label_reason(status, event, **kwargs),
        ), "format unknown — re-queue"
    if status == "blocked":
        return LabelDecision(
            issue_number=issue_number,
            add_label=adapter.blocked_label,
            remove_labels=(adapter.needs_label, adapter.done_label),
            reviewed_sha=reviewed_sha,
            reason=adapter.label_reason(status, event, **kwargs),
        ), "label blocked"
    if status == "done":
        return LabelDecision(
            issue_number=issue_number,
            add_label=adapter.done_label,
            remove_labels=(adapter.needs_label, adapter.blocked_label),
            reviewed_sha=reviewed_sha,
            reason=adapter.label_reason(status, event, **kwargs),
        ), "label done"
    return None, f"unknown {adapter.name} verdict status: {status}"


def structural_requeue_decision(
    event: dict[str, Any],
    *,
    adapter: SaaSReviewerAdapter,
    pr_labels: list[str],
    reason: str,
) -> tuple[Optional[LabelDecision], str]:
    """Fail closed to needs after the same author and opt-in gates pass."""
    if adapter.event_type != "pull_request_review":
        return None, f"{adapter.name} has no structural requeue path"

    review = event.get("review") or {}
    author = (review.get("user") or {}).get("login", "")
    if not adapter.is_review_author(author):
        return None, f"ignored review author: {author}"
    if adapter.opt_in_required and not adapter.is_opted_in(pr_labels):
        return None, f"PR does not carry an opt-in {adapter.label_prefix} label"

    pull_request = event.get("pull_request") or {}
    issue_number = int(pull_request.get("number") or 0)
    if not issue_number:
        return None, "event has no pull request number"
    reviewed_sha, _ = adapter.extract_stale_head_info(event)
    return LabelDecision(
        issue_number=issue_number,
        add_label=adapter.needs_label,
        remove_labels=(adapter.done_label, adapter.blocked_label),
        reviewed_sha=reviewed_sha,
        reason=reason,
    ), "structural failure — re-queue"


def _apply_or_log(
    repo: str,
    decision: LabelDecision,
    *,
    tokens: Sequence[GitHubToken],
    lane_name: str,
) -> None:
    try:
        apply_label_decision(repo, decision, tokens=tokens)
        print(
            f"applied: add {decision.add_label}; remove "
            f"{', '.join(decision.remove_labels)} "
            f"on {repo}#{decision.issue_number} ({decision.reason})"
        )
    except Exception as exc:
        print(
            f"verdict (label apply skipped — {lane_name} lane is non-blocking): "
            f"add {decision.add_label}; remove {', '.join(decision.remove_labels)} "
            f"on {repo}#{decision.issue_number} ({decision.reason})"
        )
        print(f"warn: could not apply label: {exc}", file=sys.stderr)


def _event_pr_number(
    event: dict[str, Any],
    adapter: SaaSReviewerAdapter,
    event_type: str,
) -> int:
    if event_type == "pull_request_review":
        return int((event.get("pull_request") or {}).get("number") or 0)
    if event_type == "check_run":
        pull_requests = ((event.get("check_run") or {}).get("pull_requests") or [])
        if len(pull_requests) == 1:
            return int(pull_requests[0].get("number") or 0)
        return 0
    return int((event.get("issue") or {}).get("number") or 0)


def _github_event_type(adapter: SaaSReviewerAdapter) -> str:
    raw_event_type = os.environ.get("GITHUB_EVENT_NAME") or ""
    if raw_event_type == "issues" and adapter.event_type == "issue_comment":
        return "issues"
    if raw_event_type in adapter.supported_event_types:
        return raw_event_type
    return adapter.event_type


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--adapter",
        required=True,
        help="SaaS reviewer adapter: greptile, gitar, or qodo.",
    )
    args = parser.parse_args(argv)
    adapter = load_adapter(args.adapter)

    event_path = Path(os.environ["GITHUB_EVENT_PATH"])
    repo = os.environ["GITHUB_REPOSITORY"]
    event = load_json(event_path)
    event_type = _github_event_type(adapter)

    tokens = github_tokens_from_env(*adapter.token_env_vars)
    dry_run = env_flag("DRY_RUN")
    if not dry_run and not tokens:
        token_names = " or ".join(adapter.token_env_vars)
        print(f"error: {token_names} is required", file=sys.stderr)
        return 1

    pr_labels: list[str] = []
    current_head_sha = os.environ.get("DRY_RUN_HEAD_SHA")
    review_comments: list[dict[str, Any]] = []
    same_head_review_exists = False
    pr_number = _event_pr_number(event, adapter, event_type)

    if dry_run:
        if event_type == "pull_request_review":
            pull_request = event.get("pull_request") or {}
            pr_labels = [label.get("name", "") for label in pull_request.get("labels") or []]
            review_comments = event.get("_dry_run_review_comments") or []
        elif event_type == "issue_comment":
            issue = event.get("issue") or {}
            pr_labels = list(event.get("_dry_run_pr_labels") or [])
            if not pr_labels:
                pr_labels = [label.get("name", "") for label in issue.get("labels") or []]
        elif event_type == "check_run":
            pr_number = int(event.get("_dry_run_pr_number") or pr_number or 0)
            pr_labels = list(event.get("_dry_run_pr_labels") or [])
            current_head_sha = event.get("_dry_run_current_head_sha")
            same_head_review_exists = bool(event.get("_dry_run_same_head_review_exists"))
    elif event_type == "check_run":
        check_run = event.get("check_run") or {}
        if event.get("action") != "completed":
            print(f"skip: unsupported check_run action: {event.get('action')}")
            return 0
        if check_run.get("status") != "completed":
            print(f"skip: check_run is not completed: {check_run.get('status')}")
            return 0
        if not adapter.is_check_run_author(check_run):
            app = check_run.get("app") or {}
            print(f"skip: ignored check_run app: {app.get('slug') or app.get('name')}")
            return 0
        if not adapter.is_check_run_name(check_run):
            print(f"skip: ignored check_run name: {check_run.get('name')}")
            return 0
        check_run_prs = list(check_run.get("pull_requests") or [])
        head_sha = check_run.get("head_sha")
        if not check_run_prs and head_sha:
            try:
                check_run_prs = fetch_pull_requests_for_commit(
                    repo, str(head_sha), tokens=tokens
                )
            except GitHubRequestError as exc:
                print(
                    f"skip: could not resolve PRs for commit "
                    f"{str(head_sha)[:8]}: {exc}"
                )
                return 0
        if not check_run_prs:
            print("skip: check_run has no associated pull requests")
            return 0

        seen_prs: set[int] = set()
        applied = 0
        considered = 0
        for pr_summary in check_run_prs:
            candidate_number = int(pr_summary.get("number") or 0)
            if not candidate_number or candidate_number in seen_prs:
                continue
            seen_prs.add(candidate_number)
            considered += 1
            try:
                pr_current = fetch_pull_request(repo, candidate_number, tokens=tokens)
            except GitHubRequestError as exc:
                print(f"skip: could not fetch PR #{candidate_number}: {exc}")
                continue
            if pr_current.get("state") != "open":
                print(f"skip: PR #{candidate_number} is not open")
                continue
            candidate_labels = [
                label.get("name", "") for label in pr_current.get("labels") or []
            ]
            candidate_head = pr_current.get("head", {}).get("sha")
            if adapter.opt_in_required and not adapter.is_opted_in(candidate_labels):
                _, reason = resolve_label_decision(
                    event,
                    adapter=adapter,
                    event_type=event_type,
                    pr_number=candidate_number,
                    pr_labels=candidate_labels,
                    current_head_sha=candidate_head,
                )
                print(f"skip: {reason}")
                continue

            decision, reason = resolve_label_decision(
                event,
                adapter=adapter,
                event_type=event_type,
                pr_number=candidate_number,
                pr_labels=candidate_labels,
                current_head_sha=candidate_head,
            )
            if decision is None:
                print(f"skip: {reason}")
                continue
            if (
                adapter.check_run_done_requires_absent_same_head_review
                and decision.add_label == adapter.done_label
            ):
                try:
                    if has_same_head_review(
                        fetch_pull_request_reviews(
                            repo,
                            candidate_number,
                            tokens=tokens,
                            page_cap=adapter.review_comments_page_cap,
                        ),
                        adapter=adapter,
                        head_sha=str(check_run.get("head_sha") or ""),
                    ):
                        print(f"skip: {adapter.same_head_review_exists_reason()}")
                        continue
                except (GitHubRequestError, ReviewCommentsTruncated) as exc:
                    decision = LabelDecision(
                        issue_number=candidate_number,
                        add_label=adapter.needs_label,
                        remove_labels=(adapter.done_label, adapter.blocked_label),
                        reviewed_sha=str(check_run.get("head_sha") or "") or None,
                        reason=adapter.review_lookup_failed_reason(exc),
                    )
            _apply_or_log(repo, decision, tokens=tokens, lane_name=adapter.name)
            applied += 1
        if applied == 0:
            if considered:
                print("skip: check_run-associated PRs produced no label updates")
            else:
                print("skip: no check_run-associated PR needed a label update")
        return 0
    elif event_type == "pull_request_review" and pr_number:
        pr_current = fetch_pull_request(repo, pr_number, tokens=tokens)
        pr_labels = [label.get("name", "") for label in pr_current.get("labels") or []]
        current_head_sha = pr_current.get("head", {}).get("sha")
        if adapter.requires_review_comments:
            review = event.get("review") or {}
            review_id = review.get("id")
            if not review_id:
                decision, reason = structural_requeue_decision(
                    event,
                    adapter=adapter,
                    pr_labels=pr_labels,
                    reason="review.id missing from event payload",
                )
                if decision is None:
                    print(f"skip: {reason}")
                    return 0
                _apply_or_log(repo, decision, tokens=tokens, lane_name=adapter.name)
                return 0
            try:
                review_comments = fetch_review_comments(
                    repo,
                    pr_number,
                    int(review_id),
                    tokens=tokens,
                    page_cap=adapter.review_comments_page_cap,
                )
            except (GitHubRequestError, ReviewCommentsTruncated) as exc:
                decision, reason = structural_requeue_decision(
                    event,
                    adapter=adapter,
                    pr_labels=pr_labels,
                    reason=f"could not fetch review comments: {exc}",
                )
                if decision is None:
                    print(f"skip: {reason}")
                    return 0
                _apply_or_log(repo, decision, tokens=tokens, lane_name=adapter.name)
                return 0
    elif event_type == "issue_comment" and pr_number and adapter.opt_in_required:
        issue = event.get("issue") or {}
        comment = event.get("comment") or {}
        author = (comment.get("user") or {}).get("login", "")
        if "pull_request" in issue and adapter.is_review_author(author):
            pr_current = fetch_pull_request(repo, pr_number, tokens=tokens)
            pr_labels = [label.get("name", "") for label in pr_current.get("labels") or []]
    elif event_type == "issues" and pr_number and adapter.event_type == "issue_comment":
        if event.get("action") != "labeled":
            print(f"skip: unsupported issues action: {event.get('action')}")
            return 0
        issue = event.get("issue") or {}
        if "pull_request" not in issue:
            print("skip: label is on an issue, not a pull request")
            return 0
        label_name = str((event.get("label") or {}).get("name") or "")
        if not adapter.is_opted_in([label_name]):
            print(
                f"skip: label `{label_name}` is not an opt-in "
                f"{adapter.label_prefix} label"
            )
            return 0
        pr_current = fetch_pull_request(repo, pr_number, tokens=tokens)
        pr_labels = [label.get("name", "") for label in pr_current.get("labels") or []]
        if adapter.opt_in_required and not adapter.is_opted_in(pr_labels):
            print(f"skip: PR does not carry an opt-in {adapter.label_prefix} label")
            return 0
        try:
            comments = fetch_issue_comments(
                repo,
                pr_number,
                tokens=tokens,
                page_cap=adapter.review_comments_page_cap,
            )
        except (GitHubRequestError, ReviewCommentsTruncated) as exc:
            print(f"skip: could not fetch issue comments: {exc}")
            return 0
        for comment in reversed(comments):
            synthetic_event = {
                "action": "created",
                "issue": issue,
                "comment": comment,
            }
            decision, reason = resolve_label_decision(
                synthetic_event,
                adapter=adapter,
                event_type=adapter.event_type,
                pr_number=pr_number,
                pr_labels=pr_labels,
                current_head_sha=current_head_sha,
            )
            if decision is None:
                continue
            _apply_or_log(repo, decision, tokens=tokens, lane_name=adapter.name)
            return 0
        print("skip: no existing issue comment produced a label update")
        return 0

    decision, reason = resolve_label_decision(
        event,
        adapter=adapter,
        event_type=event_type,
        pr_number=pr_number,
        pr_labels=pr_labels,
        current_head_sha=current_head_sha,
        review_comments=review_comments,
        same_head_review_exists=same_head_review_exists,
    )
    if decision is None:
        print(f"skip: {reason}")
        return 0

    if dry_run:
        print(json.dumps({"decision": decision.__dict__, "reason": reason}, sort_keys=True))
        return 0

    _apply_or_log(repo, decision, tokens=tokens, lane_name=adapter.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
