"""Base contract for SaaS reviewer labeler adapters."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Any, Optional


class SaaSReviewerAdapter(ABC):
    """Interface for advisory SaaS code-review labelers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short CLI adapter name, e.g. "greptile"."""

    @property
    @abstractmethod
    def label_prefix(self) -> str:
        """Audit label prefix, e.g. "greptile-audit"."""

    @property
    @abstractmethod
    def bot_usernames(self) -> frozenset[str]:
        """Default trusted bot author logins."""

    @property
    @abstractmethod
    def bot_authors_env_var(self) -> str:
        """Optional comma-separated override env var for bot authors."""

    @property
    @abstractmethod
    def event_type(self) -> str:
        """GitHub event type this adapter handles."""

    @property
    def supported_event_types(self) -> frozenset[str]:
        event_types = {self.event_type}
        if self.check_run_app_slugs or self.check_run_names:
            event_types.add("check_run")
        return frozenset(event_types)

    @property
    def needs_label(self) -> str:
        return f"needs-{self.label_prefix}"

    @property
    def done_label(self) -> str:
        return f"{self.label_prefix}-done"

    @property
    def blocked_label(self) -> str:
        return f"{self.label_prefix}-blocked"

    @property
    def opt_in_required(self) -> bool:
        return False

    @property
    def opt_in_label_names(self) -> frozenset[str]:
        return frozenset()

    @property
    def opt_in_label_prefixes(self) -> frozenset[str]:
        return frozenset()

    @property
    def informational_only(self) -> bool:
        return True

    @property
    def requires_review_comments(self) -> bool:
        return False

    @property
    def review_comments_page_cap(self) -> int:
        return 50

    @property
    def token_env_vars(self) -> tuple[str, ...]:
        return ("GITHUB_TOKEN",)

    @property
    def check_run_app_slugs(self) -> frozenset[str]:
        return frozenset()

    @property
    def check_run_names(self) -> frozenset[str]:
        return frozenset()

    @property
    def check_run_done_requires_absent_same_head_review(self) -> bool:
        return False

    def same_head_review_exists_reason(self) -> str:
        return (
            f"check_run success ignored: {self.name} PR review already exists "
            "for this head; review event owns the verdict"
        )

    def review_lookup_failed_reason(self, exc: Exception) -> str:
        return f"could not verify absence of same-head {self.name} PR review: {exc}"

    def review_authors(self) -> frozenset[str]:
        raw_authors = os.environ.get(self.bot_authors_env_var) or ",".join(
            sorted(self.bot_usernames)
        )
        return frozenset(
            author.strip().lower() for author in raw_authors.split(",") if author.strip()
        )

    def is_review_author(self, login: str) -> bool:
        return login.lower() in self.review_authors()

    def is_check_run_author(self, check_run: dict[str, Any]) -> bool:
        app = check_run.get("app") or {}
        slug = str(app.get("slug") or "").lower()
        return bool(slug and slug in self.check_run_app_slugs)

    def is_check_run_name(self, check_run: dict[str, Any]) -> bool:
        allowed = self.check_run_names
        if not allowed:
            return False
        return str(check_run.get("name") or "").strip().lower() in allowed

    def is_opted_in(self, labels: list[str]) -> bool:
        if not self.opt_in_required:
            return True
        label_set = set(labels)
        if label_set & self.opt_in_label_names:
            return True
        return any(
            label.startswith(prefix)
            for label in label_set
            for prefix in self.opt_in_label_prefixes
        )

    @abstractmethod
    def classify_verdict(self, event: dict[str, Any], **kwargs: Any) -> Optional[str]:
        """Return "done", "blocked", "needs", or None for no adapter verdict."""

    def extract_stale_head_info(self, event: dict[str, Any]) -> tuple[Optional[str], bool]:
        return None, False

    def label_reason(self, status: str, event: dict[str, Any], **kwargs: Any) -> str:
        return f"{self.name} verdict: {status}"
