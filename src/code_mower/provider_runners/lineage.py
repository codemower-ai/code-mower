"""Shared reviewer-independence admission for provider runner wrappers.

Every direct reviewer wrapper used to answer "may I review this?" on its own
terms: Codex and Claude leaned on the gate's label/author exclusion, and the
Devin wrappers carried a product-specific PR-author deny list. None of those
survive a takeover, where the opener, the branch and the active label can each
name a different lane than the one that wrote the current diff.

This module is the one admission seam. It runs after the wrapper has fetched
trusted pull request metadata and pinned the exact head, and before any
provider execution, so a lane that contributed to the diff is never spent
reviewing its own work.

Role eligibility is a separate decision (see :mod:`code_mower.role_eligibility`)
and is deliberately not consulted here: a qualified lane can still be a
contributor, and an independent lane can still be unqualified.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Mapping, Sequence

from ..builder_lineage import (
    Lineage,
    LineageError,
    episodes_from_comment_body,
    lanes_from_identity,
    resolve_lineage,
)


AUTHOR_EXCLUSION_ENV = "CODE_MOWER_AUTHOR_EXCLUSION_JSON"


class ReviewerNotIndependent(RuntimeError):
    """Raised when a reviewer lane may not gate the pull request under review.

    ``reason`` and ``owner_action`` are bounded metadata safe to surface in a
    public comment; no diagnostic output, path or provider reference is carried.
    """

    def __init__(self, decision: Mapping[str, Any]) -> None:
        self.decision = dict(decision)
        self.reason = str(decision.get("reason") or "")
        self.owner_action = str(decision.get("owner_action") or "")
        super().__init__(
            f"{decision.get('lane') or 'reviewer'} lane is not admitted: "
            f"{self.reason}; {self.owner_action}"
        )


def load_identity(raw: str | None = None) -> Mapping[str, Any]:
    """Read the existing author-exclusion identity contract.

    A missing or unparsable value disables lane naming rather than inventing
    one, which keeps an unconfigured checkout behaving as it does today.
    """

    text = raw if raw is not None else os.environ.get(AUTHOR_EXCLUSION_ENV, "")
    if not text:
        return {"enabled": False}
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError):
        return {"enabled": False}
    return parsed if isinstance(parsed, Mapping) else {"enabled": False}


def published_episodes(
    comments: Sequence[Mapping[str, Any]],
    *,
    trusted_author: Callable[[str], bool],
) -> tuple:
    """Collect lineage episodes published by already trusted comment authors.

    The hidden marker is a transport for bounded metadata. Trust comes from the
    caller's author check, never from the marker being present, so an untrusted
    commenter cannot assert a takeover into existence.
    """

    collected: list = []
    for comment in comments:
        if not isinstance(comment, Mapping):
            continue
        login = str(((comment.get("user") or {}).get("login")) or "")
        if not login or not trusted_author(login):
            continue
        collected.extend(episodes_from_comment_body(str(comment.get("body") or "")))
    return tuple(collected)


def pr_lineage(
    *,
    repo: str,
    pr_number: int,
    pr_meta: Mapping[str, Any],
    head_sha: str,
    episodes: Sequence[Any] = (),
    identity: Mapping[str, Any] | None = None,
) -> Lineage:
    """Resolve lineage from trusted pull request metadata at an exact head.

    ``pr_meta`` must be the metadata the wrapper fetched from GitHub itself;
    ``head_sha`` must be the head the wrapper pinned. Passing a head the caller
    did not verify would make every downstream decision unverified too.
    """

    labels = [
        str(label.get("name") or "")
        for label in (pr_meta.get("labels") or [])
        if isinstance(label, Mapping)
    ]
    author = str(((pr_meta.get("user") or {}).get("login")) or "")
    branch = str(((pr_meta.get("head") or {}).get("ref")) or "")
    opener_lane, label_lanes = lanes_from_identity(
        identity=identity if identity is not None else load_identity(),
        labels=labels,
        author=author,
    )
    return resolve_lineage(
        repo=repo,
        pr_number=pr_number,
        branch=branch,
        head_sha=head_sha,
        episodes=episodes,
        opener_lane=opener_lane,
        label_lanes=label_lanes,
    )


def reviewer_admission(
    lane: str,
    *,
    repo: str,
    pr_number: int,
    pr_meta: Mapping[str, Any],
    head_sha: str,
    episodes: Sequence[Any] = (),
    identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Decide whether ``lane`` may review this exact head. Fails closed."""

    try:
        lineage = pr_lineage(
            repo=repo,
            pr_number=pr_number,
            pr_meta=pr_meta,
            head_sha=head_sha,
            episodes=episodes,
            identity=identity,
        )
    except LineageError:
        return {
            "schema": "code_mower.builderLineage.v1",
            "lane": str(lane or "").strip().lower(),
            "admitted": False,
            "reason": "lineage_unreadable",
            "head_sha": str(head_sha or ""),
            "contributors": [],
            "current_writer": "",
            "owner_action": (
                "builder contribution evidence for this pull request could not "
                "be read; re-record it from the verified handoff"
            ),
        }
    return lineage.admission(lane)


def require_independent_reviewer(
    lane: str,
    *,
    repo: str,
    pr_number: int,
    pr_meta: Mapping[str, Any],
    head_sha: str,
    episodes: Sequence[Any] = (),
    identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Admit ``lane`` or raise :class:`ReviewerNotIndependent`."""

    decision = reviewer_admission(
        lane,
        repo=repo,
        pr_number=pr_number,
        pr_meta=pr_meta,
        head_sha=head_sha,
        episodes=episodes,
        identity=identity,
    )
    if not decision["admitted"]:
        raise ReviewerNotIndependent(decision)
    return decision


def require_reviewer_lane(
    lane: str,
    repo: str,
    pr_number: int,
    pr_meta: Mapping[str, Any],
    head_sha: str,
    *,
    episodes: Sequence[Any] = (),
    identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Wrapper-facing admission that reports refusal as a plain ``RuntimeError``.

    Direct reviewer wrappers already surface ``RuntimeError`` as an operator
    message with their normal exit handling, so this keeps the shared decision
    from needing per-wrapper exception plumbing. The message is bounded
    metadata plus one owner action; no diagnostic output or path is included.
    """

    try:
        return require_independent_reviewer(
            lane,
            repo=repo,
            pr_number=pr_number,
            pr_meta=pr_meta,
            head_sha=head_sha,
            episodes=episodes,
            identity=identity,
        )
    except ReviewerNotIndependent as exc:
        raise RuntimeError(
            f"{lane} reviewer lane is not admitted for {repo}#{pr_number} at "
            f"{str(head_sha)[:12]}: {exc.reason}; {exc.owner_action}"
        ) from None
