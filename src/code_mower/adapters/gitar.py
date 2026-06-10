"""Gitar adapter for the generic SaaS reviewer labeler."""

from __future__ import annotations

import re
from typing import Any, Optional

try:
    from ._base import SaaSReviewerAdapter
except ImportError:  # pragma: no cover - direct `python tools/foo.py` execution
    try:
        from tools.adapters._base import SaaSReviewerAdapter
    except ImportError:
        from adapters._base import SaaSReviewerAdapter


_CODE_REVIEW_LABEL_RE = re.compile(r"Code Review\s*</b>", re.IGNORECASE)
_CODE_REVIEW_BADGE_RE = re.compile(
    r"Code Review\s*</b>\s*<kbd[^>]*>\s*(?P<badge>.*?)\s*</kbd>",
    re.IGNORECASE | re.DOTALL,
)
_CODE_FENCE_RE = re.compile(r"(`+).*?\1|~~~.*?~~~", re.DOTALL)


def _strip_code(text: str) -> str:
    """Blank code spans/fences so quoted badges cannot spoof verdicts."""
    return _CODE_FENCE_RE.sub(lambda match: re.sub(r"[^\n]", " ", match.group(0)), text)


def has_code_review_block(comment_body: str) -> bool:
    return bool(_CODE_REVIEW_LABEL_RE.search(comment_body))


def parse_code_review_badge(comment_body: str) -> Optional[str]:
    match = _CODE_REVIEW_BADGE_RE.search(comment_body)
    if not match:
        return None
    badge = re.sub(r"<[^>]*>", "", match.group("badge"))
    return badge.strip()


def classify_badge(badge: Optional[str]) -> Optional[str]:
    if badge is None:
        return None
    if not badge:
        return "needs"
    normalized = re.sub(r"[^a-z]", "", badge.lower())
    if normalized in {"approved", "approvedwithsuggestions"}:
        return "done"
    return "blocked"


def classify_gitar_comment(comment_body: str) -> tuple[Optional[str], str]:
    body = _strip_code(comment_body)
    if not has_code_review_block(body):
        return None, "no Code Review block"
    badge = parse_code_review_badge(body)
    if not badge:
        return "needs", "Code Review block present but badge unparseable"
    return classify_badge(badge), f"Code Review badge {badge!r}"


class GitarAdapter(SaaSReviewerAdapter):
    @property
    def name(self) -> str:
        return "gitar"

    @property
    def label_prefix(self) -> str:
        return "gitar-audit"

    @property
    def bot_usernames(self) -> frozenset[str]:
        return frozenset({"gitar-ai[bot]", "gitar-bot", "gitar-bot[bot]"})

    @property
    def bot_authors_env_var(self) -> str:
        return "GITAR_BOT_AUTHORS"

    @property
    def event_type(self) -> str:
        return "issue_comment"

    @property
    def token_env_vars(self) -> tuple[str, ...]:
        return ("GITAR_AUDIT_LABEL_TOKEN", "GITHUB_TOKEN")

    def classify_verdict(self, event: dict[str, Any], **kwargs: Any) -> Optional[str]:
        status, _ = classify_gitar_comment(kwargs.get("comment_body") or "")
        return status

    def label_reason(self, status: str, event: dict[str, Any], **kwargs: Any) -> str:
        _, detail = classify_gitar_comment(kwargs.get("comment_body") or "")
        if status == "needs":
            return f"Gitar Code Review block present but verdict was empty/unparseable ({detail})"
        return f"Gitar verdict: {status} ({detail})"


ADAPTER = GitarAdapter()
