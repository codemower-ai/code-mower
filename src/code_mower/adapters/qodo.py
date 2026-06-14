"""Qodo adapter for the generic SaaS reviewer labeler.

Qodo is a paid hosted reviewer in the reference repos, so this adapter is
strictly opt-in. It only classifies comments from the trusted Qodo bot on PRs
that already carry a Qodo audit label.
"""

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


_QODO_REVIEW_RE = re.compile(r"\bCode Review by Qodo\b", re.IGNORECASE)
_QODO_TRANSIENT_FAILURE_RE = re.compile(
    r"sorry,\s+something\s+went\s+wrong|could\s+not\s+complete|try\s+again",
    re.IGNORECASE,
)
_COUNT_RE = re.compile(
    r"(?P<name>Bugs|Rule violations|Cross-repo conflicts)\s*\((?P<count>\d+)\)",
    re.IGNORECASE,
)
_EXPECTED_COUNT_KEYS = frozenset({"bugs", "rule violations", "cross-repo conflicts"})
_CODE_FENCE_RE = re.compile(r"(`+).*?\1|~~~.*?~~~", re.DOTALL)


def _strip_code(text: str) -> str:
    return _CODE_FENCE_RE.sub(lambda match: re.sub(r"[^\n]", " ", match.group(0)), text)


@dataclass(frozen=True)
class QodoVerdict:
    bugs: int = 0
    rule_violations: int = 0
    cross_repo_conflicts: int = 0
    has_review_marker: bool = False
    has_counts: bool = False
    transient_failure: bool = False

    @property
    def blocker_count(self) -> int:
        return self.bugs + self.rule_violations + self.cross_repo_conflicts

    def classify(self) -> Optional[str]:
        if self.transient_failure and not self.has_counts:
            return "needs"
        if not self.has_review_marker:
            return None
        if not self.has_counts:
            return "needs"
        if self.blocker_count > 0:
            return "blocked"
        return "done"


def parse_qodo_comment(comment_body: str) -> QodoVerdict:
    body_without_code = _strip_code(comment_body)
    counts = {
        match.group("name").lower(): int(match.group("count"))
        for match in _COUNT_RE.finditer(body_without_code)
    }
    return QodoVerdict(
        bugs=counts.get("bugs", 0),
        rule_violations=counts.get("rule violations", 0),
        cross_repo_conflicts=counts.get("cross-repo conflicts", 0),
        has_review_marker=bool(_QODO_REVIEW_RE.search(body_without_code)),
        has_counts=_EXPECTED_COUNT_KEYS.issubset(counts.keys()),
        transient_failure=bool(_QODO_TRANSIENT_FAILURE_RE.search(body_without_code)),
    )


class QodoAdapter(SaaSReviewerAdapter):
    @property
    def name(self) -> str:
        return "qodo"

    @property
    def label_prefix(self) -> str:
        return "qodo-audit"

    @property
    def bot_usernames(self) -> frozenset[str]:
        return frozenset({"qodo-code-review[bot]", "qodo-code-review"})

    @property
    def bot_authors_env_var(self) -> str:
        return "QODO_BOT_AUTHORS"

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
        return frozenset({"qodo-audit-"})

    @property
    def token_env_vars(self) -> tuple[str, ...]:
        return ("QODO_AUDIT_LABEL_TOKEN", "GITHUB_TOKEN")

    def classify_verdict(self, event: dict[str, Any], **kwargs: Any) -> Optional[str]:
        return parse_qodo_comment(kwargs.get("comment_body") or "").classify()

    def label_reason(self, status: str, event: dict[str, Any], **kwargs: Any) -> str:
        verdict = parse_qodo_comment(kwargs.get("comment_body") or "")
        if verdict.transient_failure and not verdict.has_counts:
            return "Qodo reported a transient review failure; re-queue for a fresh pass"
        if status == "needs":
            return "Qodo review marker found but verdict counts were not parseable"
        if status == "blocked":
            return (
                "Qodo found blocking findings "
                f"(bugs={verdict.bugs}, rule_violations={verdict.rule_violations}, "
                f"cross_repo_conflicts={verdict.cross_repo_conflicts})"
            )
        return (
            "Qodo audit clean "
            f"(bugs={verdict.bugs}, rule_violations={verdict.rule_violations}, "
            f"cross_repo_conflicts={verdict.cross_repo_conflicts})"
        )


ADAPTER = QodoAdapter()
