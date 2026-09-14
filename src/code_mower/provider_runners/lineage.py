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


#: Accounts a reviewer lane writes under. Used only as a floor, so an
#: unconfigured or malformed identity file cannot make a contributing reviewer
#: admissible; it never widens who may review.
LANE_ACCOUNT_FLOOR: Mapping[str, tuple[str, ...]] = {
    "devin": (
        "devin-ai-integration",
        "devin-ai-integration[bot]",
        "devin-cli-audit-bot",
        "devin-cli-audit-bot[bot]",
    ),
    "codex": ("chatgpt-codex-connector[bot]", "codex[bot]"),
    "claude": ("claude[bot]", "claude-bot"),
}


def identity_with_lane_floor(identity: Mapping[str, Any] | None, lane: str) -> Mapping[str, Any]:
    """Guarantee the reviewer lane can be named, whatever the configuration says.

    Reviewer independence is decided by naming lanes. A missing, disabled or
    malformed identity contract would name none of them, and an unnameable lane
    cannot be recognised as a contributor -- which would silently admit exactly
    the reviewer this seam exists to exclude. So the lane's own label and
    accounts are always present. Only the reviewer's own lane is synthesized:
    this adds exclusion and never admission.
    """

    reviewer = str(lane or "").strip().lower()
    base = dict(identity) if isinstance(identity, Mapping) else {}
    labels = base.get("labels")
    authors = base.get("authors")
    merged_labels = dict(labels) if isinstance(labels, Mapping) else {}
    merged_authors = dict(authors) if isinstance(authors, Mapping) else {}
    if reviewer:
        merged_labels.setdefault(f"builder:{reviewer}", reviewer)
        for login in LANE_ACCOUNT_FLOOR.get(reviewer, ()):
            merged_authors.setdefault(login, reviewer)
    return {"enabled": True, "labels": merged_labels, "authors": merged_authors}


def recorded_episodes(repo: str, pr_number: Any, state_dir: Any = None) -> tuple:
    """Load contribution episodes the verified delivery boundary persisted.

    This is the wrapper-side counterpart of the runner's record, so it must
    resolve the *same* directory the runner writes to. When the deployment
    configures ``LANE_HANDOFF_STATE_DIR``, reading the packaged default instead
    would consult an empty store and miss every verified contribution.

    An unreadable record -- including a configured directory that cannot be
    resolved -- raises :class:`~code_mower.builder_lineage.LineageError` so the
    caller fails closed; a checkout with no record at all simply has no
    episodes, which is the ordinary single-builder case.
    """

    from pathlib import Path

    from ..builder_lineage import LineageError, load_episodes
    from ..lane_delivery import LaneDeliveryError
    from ..lane_handoff import configured_root, lineage_root

    if state_dir is not None:
        root = Path(state_dir)
    else:
        try:
            root = configured_root()
        except LaneDeliveryError as exc:
            raise LineageError(str(exc)) from None
    return load_episodes(lineage_root(root), repo, pr_number)


def trusted_episodes(
    repo: str,
    pr_number: Any,
    *,
    comments: Sequence[Mapping[str, Any]] = (),
    trusted_author: Callable[[str], bool] | None = None,
    published: Sequence[Any] = (),
    state_dir: Any = None,
) -> tuple:
    """All contribution evidence this reviewer is allowed to read, in order.

    The durable record is the runner's own; published markers are the transport
    for a reviewer running somewhere the record does not exist -- which is the
    ordinary case for an independent reviewer host, whose private store is
    empty. Both are parsed strictly and merged by sequence.

    An episode that merely repeats one already collected is dropped, so the
    producer republishing the whole chain on every round does not inflate the
    input. An episode that *contradicts* one already collected is kept, so the
    resolver still sees the duplicate position and fails closed on it.

    ``published`` accepts episodes an embedding caller already established as
    trusted, for adapters that carry the transport rather than the comments.
    """

    collected = list(recorded_episodes(repo, pr_number, state_dir))
    incoming: list[Any] = list(published)
    if comments and trusted_author is not None:
        incoming.extend(published_episodes(comments, trusted_author=trusted_author))
    seen = {episode.sequence: episode.as_dict() for episode in collected}
    for episode in incoming:
        payload = episode.as_dict()
        known = seen.get(episode.sequence)
        if known == payload:
            continue
        collected.append(episode)
        if known is None:
            seen[episode.sequence] = payload
    return tuple(collected)


def marker_author_trust(authorities: Sequence[str] = ()) -> Callable[[str], bool]:
    """Who a reviewer may read published lineage markers from.

    This is deliberately the *same* rule the gate and the labelers apply: the
    repository's configured decision authorities, and nobody else. A lineage
    marker is a transport for bounded metadata, so being able to post an audit
    comment on a pull request is not being able to assert a takeover of it. An
    unconfigured checkout trusts nobody and reads no published evidence, which
    leaves it behaving exactly as it does without this seam.
    """

    from ..audit_labeler_lib import lineage_marker_author_trust

    return lineage_marker_author_trust(authorities=authorities)


def reviewer_evidence(
    repo: str,
    pr_number: Any,
    *,
    authorities: Sequence[str] = (),
    fetch_comments: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
    state_dir: Any = None,
) -> tuple:
    """Every contribution record a reviewer host may read, in order.

    A reviewer usually runs somewhere that never recorded anything: its private
    store is empty, and the only evidence of a takeover is what the producer
    published on the pull request. Reading the private store alone therefore
    answers "no takeover happened" on exactly the hosts where the question
    matters, so this reads both.

    Comments are fetched only when the repository names decision authorities;
    with none configured there is nobody to trust and the fetch would be spent
    on evidence that could not be used. A fetch that fails once authorities
    *are* configured raises :class:`~code_mower.builder_lineage.LineageError`,
    because silently continuing on the private store would be the empty-store
    answer again, now indistinguishable from a real absence of lineage.
    """

    trusted = [str(item).strip() for item in authorities if str(item).strip()]
    comments: Sequence[Mapping[str, Any]] = ()
    if trusted and fetch_comments is not None:
        try:
            fetched = fetch_comments()
        except Exception as exc:  # bounded: transport, auth and parse failures alike
            raise LineageError(
                f"published builder lineage for {repo}#{pr_number} could not be "
                f"read: {type(exc).__name__}"
            ) from None
        comments = [item for item in (fetched or ()) if isinstance(item, Mapping)]
    return trusted_episodes(
        repo,
        pr_number,
        comments=comments,
        trusted_author=marker_author_trust(trusted) if trusted else None,
        state_dir=state_dir,
    )


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
