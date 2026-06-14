"""Greptile adapter for the generic SaaS reviewer labeler."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

try:
    from ._base import SaaSReviewerAdapter
except ImportError:  # pragma: no cover - direct `python tools/foo.py` execution
    try:
        from tools.adapters._base import SaaSReviewerAdapter
    except ImportError:
        from adapters._base import SaaSReviewerAdapter


_ALT_P_TAG_RE = re.compile(r'alt="P([0-3])"', flags=re.IGNORECASE)
_BADGE_URL_RE = re.compile(
    r"greptile-static-assets\.s3\.amazonaws\.com/badges/p([0-3])\.svg",
    flags=re.IGNORECASE,
)


def severity_of(comment_body: str) -> Optional[int]:
    """Return 0/1/2/3 if a Greptile inline comment has a P-badge."""
    match = _ALT_P_TAG_RE.search(comment_body)
    if match:
        return int(match.group(1))
    match = _BADGE_URL_RE.search(comment_body)
    if match:
        return int(match.group(1))
    return None


@dataclass(frozen=True)
class GreptileVerdict:
    p0_count: int = 0
    p1_count: int = 0
    p2_count: int = 0
    p3_count: int = 0
    unparsed_count: int = 0

    @property
    def total_comments(self) -> int:
        return (
            self.p0_count
            + self.p1_count
            + self.p2_count
            + self.p3_count
            + self.unparsed_count
        )

    @property
    def blocker_count(self) -> int:
        return self.p0_count + self.p1_count

    def classify(self) -> str:
        if self.unparsed_count > 0:
            return "needs"
        if self.blocker_count > 0:
            return "blocked"
        return "done"


def parse_review_comments(comments: list[dict[str, Any]]) -> GreptileVerdict:
    p0_count = p1_count = p2_count = p3_count = unparsed_count = 0
    for comment in comments:
        severity = severity_of(comment.get("body") or "")
        if severity is None:
            unparsed_count += 1
        elif severity == 0:
            p0_count += 1
        elif severity == 1:
            p1_count += 1
        elif severity == 2:
            p2_count += 1
        elif severity == 3:
            p3_count += 1
    return GreptileVerdict(
        p0_count=p0_count,
        p1_count=p1_count,
        p2_count=p2_count,
        p3_count=p3_count,
        unparsed_count=unparsed_count,
    )


class GreptileAdapter(SaaSReviewerAdapter):
    @property
    def name(self) -> str:
        return "greptile"

    @property
    def label_prefix(self) -> str:
        return "greptile-audit"

    @property
    def bot_usernames(self) -> frozenset[str]:
        return frozenset({"greptile-apps", "greptile-apps[bot]"})

    @property
    def bot_authors_env_var(self) -> str:
        return "GREPTILE_BOT_AUTHORS"

    @property
    def event_type(self) -> str:
        return "pull_request_review"

    @property
    def opt_in_required(self) -> bool:
        return True

    @property
    def opt_in_label_names(self) -> frozenset[str]:
        return frozenset({self.needs_label, self.done_label, self.blocked_label})

    @property
    def opt_in_label_prefixes(self) -> frozenset[str]:
        return frozenset({"greptile-audit-"})

    @property
    def requires_review_comments(self) -> bool:
        return True

    @property
    def token_env_vars(self) -> tuple[str, ...]:
        return ("GREPTILE_AUDIT_LABEL_TOKEN", "GITHUB_TOKEN")

    @property
    def check_run_app_slugs(self) -> frozenset[str]:
        return frozenset({"greptile-apps"})

    @property
    def check_run_names(self) -> frozenset[str]:
        return frozenset({"greptile review"})

    @property
    def check_run_done_requires_absent_same_head_review(self) -> bool:
        return True

    def classify_verdict(self, event: dict[str, Any], **kwargs: Any) -> Optional[str]:
        check_run = kwargs.get("check_run")
        if isinstance(check_run, dict):
            conclusion = str(check_run.get("conclusion") or "").lower()
            if conclusion == "success":
                return "done"
            if conclusion in {"failure", "timed_out", "action_required", "startup_failure"}:
                return "blocked"
            # Any other or unknown conclusion fails closed to needs.
            return "needs"
        return parse_review_comments(kwargs.get("review_comments") or []).classify()

    def extract_stale_head_info(self, event: dict[str, Any]) -> tuple[Optional[str], bool]:
        review = event.get("review") or {}
        return review.get("commit_id") or None, True

    def label_reason(self, status: str, event: dict[str, Any], **kwargs: Any) -> str:
        check_run = kwargs.get("check_run")
        if isinstance(check_run, dict):
            conclusion = str(check_run.get("conclusion") or "unknown")
            head_sha = str(check_run.get("head_sha") or "")[:8]
            suffix = f"; checked {head_sha}" if head_sha else ""
            if status == "done":
                return f"Greptile status check succeeded{suffix}"
            if status == "blocked":
                return f"Greptile status check concluded {conclusion}{suffix}"
            return f"Greptile status check conclusion was not a final pass: {conclusion}{suffix}"

        verdict = parse_review_comments(kwargs.get("review_comments") or [])
        review_sha = ((event.get("review") or {}).get("commit_id") or "")[:8]
        if status == "needs":
            return (
                f"{verdict.total_comments} inline comments present but at least one "
                "has no recognizable P-badge (alt='P[N]' or badge URL); "
                "Greptile output format may have drifted"
            )
        if status == "blocked":
            return (
                f"Greptile found {verdict.blocker_count} blocker(s) "
                f"(P0={verdict.p0_count}, P1={verdict.p1_count}); "
                f"P2={verdict.p2_count}, P3={verdict.p3_count} concerns"
            )
        suffix = f"; reviewed {review_sha}" if review_sha else ""
        return (
            f"Greptile audit clean (0 P0/P1 blockers; "
            f"P2={verdict.p2_count}, P3={verdict.p3_count} concerns{suffix})"
        )


ADAPTER = GreptileAdapter()
