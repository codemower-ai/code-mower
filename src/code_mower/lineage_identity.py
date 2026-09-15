"""Canonical reviewer identity and admission: the pure contract.

Every direct reviewer wrapper used to answer "may I review this?" on its own
terms, leaning on label/author exclusion or a product-specific deny list. None
of those survive a takeover, where the opener, the branch and the active label
can each name a different lane than the one that wrote the current diff.

This module is the pure half of the one admission seam: given an identity
contract, trusted pull request metadata and the episodes a caller already
established as trusted, it decides whether a reviewer lane is independent of
the diff. Fetching the metadata, reading the environment, loading a private
record and running a provider are all somewhere else, and deliberately so --
an admission decision that cannot be computed from explicit inputs cannot be
audited from them either.

Role eligibility is a separate decision and is not consulted here: a qualified
lane can still be a contributor, and an independent lane can still be
unqualified.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping, Sequence

from .builder_lineage import (
    SCHEMA,
    ContributionEpisode,
    IdentityConflictError,
    Lineage,
    LineageError,
    canonical_identity,
    merge_episodes,
    published_episodes,
    require_comment_list,
    resolve_builder_lineage,
)


#: The environment variable the deployment's identity contract is rendered
#: into. Named here so consumers and tests agree on it; nothing in this module
#: reads it, because a pure decision may not depend on ambient state.
AUTHOR_EXCLUSION_ENV = "CODE_MOWER_AUTHOR_EXCLUSION_JSON"

#: Accounts a reviewer lane writes under. Used only as a floor, so an
#: unconfigured or malformed identity contract cannot make a contributing
#: reviewer admissible; it never widens who may review.
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


#: The deployment's identity contract misnames the reviewer's own lane, or
#: gives one normalized key two answers. It is the core's conflict class rather
#: than a second one, so a contradiction found while canonicalizing and one
#: found while flooring are the same failure to every caller.
ReviewerIdentityInvalid = IdentityConflictError


def identity_from_json(raw: str | None) -> Mapping[str, Any]:
    """Parse the author-exclusion identity contract from explicit text.

    A missing or unparsable value disables lane naming rather than inventing
    one, which keeps an unconfigured checkout behaving as it does today. The
    text is always supplied by the caller: reading it from the environment is a
    consumer's job, so that this decision stays reproducible from its inputs.
    """

    text = raw or ""
    if not text:
        return {"enabled": False}
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError):
        return {"enabled": False}
    return parsed if isinstance(parsed, Mapping) else {"enabled": False}


#: Compatibility spelling for callers that already say ``load_identity``. It
#: requires the raw text explicitly; there is deliberately no environment
#: fallback in the pure contract.
load_identity = identity_from_json


def account_key(login: Any) -> str:
    """The form account lookups actually use: trimmed and case-folded."""

    return str(login or "").strip().lower()


def normalized_account_map(authors: Any) -> dict:
    """The account map keyed the way resolution reads it.

    A thin view onto :func:`~code_mower.builder_lineage.canonical_identity`,
    not a second normalization: the reviewer floor and the resolver have to
    compose over the very same keys, and the way they stopped doing so was by
    each normalizing for itself.
    """

    return canonical_identity({"authors": authors})["authors"]


def _claim_own_identity(mapping: dict, key: str, reviewer: str, kind: str) -> None:
    """Make ``key`` name ``reviewer``, or refuse if it already names another."""

    present = mapping.get(key)
    named = str(present).strip().lower() if isinstance(present, str) else ""
    if named and named != reviewer:
        raise ReviewerIdentityInvalid(
            f"reviewer_identity_invalid: the configured {kind} `{key}` names "
            f"lane `{named}`, but it is the {reviewer} lane's own {kind}. "
            f"Correct {AUTHOR_EXCLUSION_ENV} before running a {reviewer} "
            f"review; a reviewer that cannot name its own lane cannot be "
            f"excluded from its own contribution."
        )
    mapping[key] = reviewer


def identity_with_lane_floor(
    identity: Mapping[str, Any] | None, lane: str
) -> Mapping[str, Any]:
    """Guarantee the reviewer lane can be named, whatever the configuration says.

    Reviewer independence is decided by naming lanes. A missing, disabled or
    malformed identity contract would name none of them, and an unnameable lane
    cannot be recognised as a contributor -- which would silently admit exactly
    the reviewer this seam exists to exclude. So the lane's own label and
    accounts are always present. Only the reviewer's own lane is synthesized:
    this adds exclusion and never admission.

    It is a floor, not a default. ``setdefault`` leaves a present-but-useless
    mapping alone -- ``{"builder:codex": ""}`` keeps naming no lane -- so a
    blank or malformed own entry is overwritten, and one that names a
    *different* lane is a configuration error the reviewer refuses on rather
    than silently correcting, because the deployment believes something about
    its own identity that is not true. Disabling the contract does not make a
    misnamed own lane safe, so the check runs either way.

    The floor raises the fields it is responsible for and leaves the rest of
    the deployment's contract -- ``branch_prefixes``,
    ``require_verified_lineage`` and anything else -- intact. Rebuilding the
    mapping from scratch dropped the configured branch identity every wrapper
    was rendered to use, so a ``codex/`` branch labelled ``builder:claude``
    came back a sole Claude writer, admitting Codex to its own diff.

    It composes over the canonical representation, so it floors the very keys
    resolution will look up and an alias cannot outrank the canonical account.
    """

    reviewer = str(lane or "").strip().lower()
    floored = canonical_identity(identity)
    if reviewer:
        _claim_own_identity(floored["labels"], f"builder:{reviewer}", reviewer, "label")
        for login in LANE_ACCOUNT_FLOOR.get(reviewer, ()):
            _claim_own_identity(
                floored["authors"], account_key(login), reviewer, "account"
            )
    floored["enabled"] = True
    return floored


def marker_author_trust(
    authorities: Sequence[str] = (),
) -> Callable[[str], bool]:
    """Who a consumer may read published lineage markers from.

    This is deliberately the *same* rule the gate, the labelers and the
    reviewer wrappers apply: the repository's configured decision authorities,
    and nobody else. A lineage marker is a transport for bounded metadata, so
    being able to post an audit comment on a pull request is not being able to
    assert a takeover of it. An unconfigured checkout trusts nobody and reads
    no published evidence, which leaves it behaving exactly as it does without
    this seam.

    The authority list is supplied by the caller. Resolving it from the
    environment here would make the same marker trusted or untrusted depending
    on where the predicate happened to be built.
    """

    allowed = {
        str(item).strip().lower().lstrip("@")
        for item in authorities
        if str(item).strip()
    }

    def trusted(login: str) -> bool:
        return bool(allowed) and str(login).strip().lower().lstrip("@") in allowed

    return trusted


def trusted_published_episodes(
    comments: Any,
    *,
    authorities: Sequence[str] = (),
    what: str = "the published builder lineage",
) -> tuple[ContributionEpisode, ...]:
    """Validate a raw comment history, then read the markers it is trusted for.

    Validation comes first and unconditionally. A successful read that is not a
    list of comment objects has not answered the question, and filtering it
    down to what happens to be a mapping reports an unreadable history as an
    absent one -- which is the answer that admits a reviewer.
    """

    records = require_comment_list(comments, what=what)
    trusted = [str(item).strip() for item in authorities if str(item).strip()]
    if not trusted:
        return ()
    return published_episodes(records, trusted_author=marker_author_trust(trusted))


def combine_evidence(
    recorded: Sequence[ContributionEpisode] = (),
    published: Sequence[ContributionEpisode] = (),
) -> tuple[ContributionEpisode, ...]:
    """All contribution evidence a reviewer is allowed to read, in order.

    The durable record is the producing host's own; published markers are the
    transport for a reviewer running somewhere that record does not exist --
    which is the ordinary case for an independent reviewer host, whose private
    store is empty. Reading the private store alone therefore answers "no
    takeover happened" on exactly the hosts where the question matters, so both
    are carried through as validated, bounded raw arrivals. Nothing is
    collapsed here: the owning resolver deduplicates exactly once, where it can
    also see a contradiction at a position and refuse it.
    """

    return merge_episodes(recorded, published)


def pr_lineage(
    *,
    repo: str,
    pr_number: Any,
    pr_meta: Mapping[str, Any],
    head_sha: str,
    identity: Mapping[str, Any] | None,
    episodes: Sequence[ContributionEpisode] = (),
) -> Lineage:
    """Resolve lineage from trusted pull request metadata at an exact head.

    ``pr_meta`` must be the metadata the caller fetched from GitHub itself and
    ``head_sha`` the head it pinned; passing a head the caller did not verify
    would make every downstream decision unverified too. ``identity`` is
    explicit for the same reason -- a wrapper and the gate that read the same
    pull request must not disagree because one of them found a different
    contract in its environment.

    Admission is an exact-target claim, so the complete repository, pull
    request number, branch and head are required. Metadata that names no branch
    is a ``target_invalid`` conflict, not an identity-only answer about an
    unbound diff.
    """

    labels = [
        str(label.get("name") or "")
        for label in (pr_meta.get("labels") or [])
        if isinstance(label, Mapping)
    ]
    user = pr_meta.get("user")
    author = str((user.get("login") if isinstance(user, Mapping) else "") or "")
    head = pr_meta.get("head")
    branch = str((head.get("ref") if isinstance(head, Mapping) else "") or "")
    # The wrapper decides admission from the same composition the gate does, so
    # the configured branch identity reaches it here too -- including when there
    # are no episodes at all.
    return resolve_builder_lineage(
        identity=identity,
        labels=labels,
        author=author,
        repo=repo,
        pr_number=pr_number,
        branch=branch,
        head_sha=head_sha,
        episodes=episodes,
    )


def reviewer_admission(
    lane: str,
    *,
    repo: str,
    pr_number: Any,
    pr_meta: Mapping[str, Any],
    head_sha: str,
    identity: Mapping[str, Any] | None,
    episodes: Sequence[ContributionEpisode] = (),
) -> dict[str, Any]:
    """Decide whether ``lane`` may review this exact head. Fails closed.

    The reviewer's own lane is floored before resolution, so a contract that
    cannot name it still excludes it. An identity contract that *misnames* it
    raises :class:`ReviewerIdentityInvalid` rather than resolving, because a
    deployment that is wrong about its own reviewer has not asked a question
    this seam can answer.
    """

    floored = identity_with_lane_floor(identity, lane)
    try:
        lineage = pr_lineage(
            repo=repo,
            pr_number=pr_number,
            pr_meta=pr_meta,
            head_sha=head_sha,
            identity=floored,
            episodes=episodes,
        )
    except LineageError:
        return {
            "schema": SCHEMA,
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
    pr_number: Any,
    pr_meta: Mapping[str, Any],
    head_sha: str,
    identity: Mapping[str, Any] | None,
    episodes: Sequence[ContributionEpisode] = (),
) -> dict[str, Any]:
    """Admit ``lane`` or raise :class:`ReviewerNotIndependent`."""

    decision = reviewer_admission(
        lane,
        repo=repo,
        pr_number=pr_number,
        pr_meta=pr_meta,
        head_sha=head_sha,
        identity=identity,
        episodes=episodes,
    )
    if not decision["admitted"]:
        raise ReviewerNotIndependent(decision)
    return decision


def require_reviewer_lane(
    lane: str,
    repo: str,
    pr_number: Any,
    pr_meta: Mapping[str, Any],
    head_sha: str,
    *,
    identity: Mapping[str, Any] | None,
    episodes: Sequence[ContributionEpisode] = (),
) -> dict[str, Any]:
    """Admission that reports refusal as a plain ``RuntimeError``.

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
            identity=identity,
            episodes=episodes,
        )
    except ReviewerNotIndependent as exc:
        raise RuntimeError(
            f"{lane} reviewer lane is not admitted for {repo}#{pr_number} at "
            f"{str(head_sha)[:12]}: {exc.reason}; {exc.owner_action}"
        ) from None
