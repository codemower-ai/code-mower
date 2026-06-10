"""Cursor BugBot adapter for the generic SaaS reviewer labeler.

BugBot is currently treated as an informational, manually triggered lane. This
adapter is intentionally conservative: the disabled-repository response is
classified as needing setup, an explicit no-bugs response can mark the lane
done, and all other trusted BugBot comments fail closed to ``needs`` until
real enabled-output examples are captured and calibrated.
"""

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


_DISABLED_RE = re.compile(r"\bBugbot is disabled for this repository\b", re.IGNORECASE)
_NO_BUGS_RE = re.compile(
    r"\A\s*(?:BugBot\s+)?(?:no\s+(?:bugs?|issues?)\s+(?:were\s+)?found|found\s+no\s+(?:bugs?|issues?))"
    r"(?:\s+(?:in|for)\s+this\s+(?:pull\s+request|pr))?[.!]?\s*\Z",
    re.IGNORECASE,
)
_FINDING_HINT_RE = re.compile(
    r"\b(?:found\s+[1-9]\d*\s+(?:bugs?|issues?|findings?)|"
    r"(?:bug|issue|finding|severity)\s*[:#]|changes\s+requested|p[0-3]\b)",
    re.IGNORECASE,
)


def classify_cursor_bugbot_comment(comment_body: str) -> tuple[Optional[str], str]:
    if _DISABLED_RE.search(comment_body):
        return "needs", "BugBot is disabled for this repository"
    if _NO_BUGS_RE.search(comment_body) and not _FINDING_HINT_RE.search(comment_body):
        return "done", "BugBot reported no bugs"
    if "bugbot" not in comment_body.lower() and "cursor" not in comment_body.lower():
        return None, "no Cursor BugBot marker"
    return "needs", "Cursor BugBot output shape is not calibrated yet"


class CursorBugBotAdapter(SaaSReviewerAdapter):
    @property
    def name(self) -> str:
        return "cursor_bugbot"

    @property
    def label_prefix(self) -> str:
        return "cursor-bugbot-audit"

    @property
    def bot_usernames(self) -> frozenset[str]:
        return frozenset({"cursor[bot]", "cursor"})

    @property
    def bot_authors_env_var(self) -> str:
        return "CURSOR_BUGBOT_BOT_AUTHORS"

    @property
    def event_type(self) -> str:
        return "issue_comment"

    @property
    def opt_in_required(self) -> bool:
        return True

    @property
    def opt_in_label_names(self) -> frozenset[str]:
        return frozenset({self.needs_label, self.done_label, self.blocked_label})

    @property
    def opt_in_label_prefixes(self) -> frozenset[str]:
        return frozenset({"cursor-bugbot-audit-"})

    @property
    def token_env_vars(self) -> tuple[str, ...]:
        return ("CURSOR_BUGBOT_AUDIT_LABEL_TOKEN", "GITHUB_TOKEN")

    def classify_verdict(self, event: dict[str, Any], **kwargs: Any) -> Optional[str]:
        status, _ = classify_cursor_bugbot_comment(kwargs.get("comment_body") or "")
        return status

    def label_reason(self, status: str, event: dict[str, Any], **kwargs: Any) -> str:
        _, detail = classify_cursor_bugbot_comment(kwargs.get("comment_body") or "")
        if status == "needs":
            return f"Cursor BugBot needs attention ({detail})"
        if status == "done":
            return f"Cursor BugBot clean ({detail})"
        return f"Cursor BugBot verdict: {status} ({detail})"


ADAPTER = CursorBugBotAdapter()
