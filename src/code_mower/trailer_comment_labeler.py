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
from typing import Any, Dict, Optional, Sequence

if __package__ and __package__.startswith("code_mower"):
    from .audit_labeler_lib import (
        LaneConfig,
        LabelDecision,
        apply_label_decision,
        extract_reviewed_sha,
        fetch_pull_request,
        load_json,
        sha_matches_reviewed_head,
    )
    from .lane_configs import load_lane_config
else:
    try:
        from tools.audit_labeler_lib import (
            LaneConfig,
            LabelDecision,
            apply_label_decision,
            extract_reviewed_sha,
            fetch_pull_request,
            load_json,
            sha_matches_reviewed_head,
        )
        from tools.lane_configs import load_lane_config
    except ImportError:  # pragma: no cover - direct `python tools/foo.py` execution
        from audit_labeler_lib import (
            LaneConfig,
            LabelDecision,
            apply_label_decision,
            extract_reviewed_sha,
            fetch_pull_request,
            load_json,
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


def resolve_label_decision(
    event: Dict[str, Any],
    *,
    current_head_sha: Optional[str],
    config: LaneConfig,
) -> tuple[Optional[LabelDecision], str]:
    if event.get("action") != "created":
        return None, f"unsupported action: {event.get('action')}"

    issue = event.get("issue") or {}
    if "pull_request" not in issue:
        return None, "comment is not on a pull request"

    comment = event.get("comment") or {}
    author = (comment.get("user") or {}).get("login", "")
    if author.lower() not in config.comment_authors():
        return None, f"ignored comment author: {author}"

    body = comment.get("body") or ""
    status = classify_audit_comment(body, config)
    if status is None:
        return None, f"comment is not a final {config.display_name} audit result"

    issue_number = int(issue["number"])
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

    decision, reason = resolve_label_decision(event, current_head_sha=current_head_sha, config=config)
    if decision is None:
        print(f"skip: {reason}")
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
