"""Exact-head builder contribution lineage: the pure contract.

A pull request can be built by more than one Code Mower builder lane. The
opener, the branch prefix and the single active ``builder:*`` label each
describe at most one of those lanes, so any of them alone will misdescribe a
pull request that changed hands. This module keeps the ordered contribution
history instead, and derives the one current writer from it.

Trust rules this module exists to enforce:

* A contribution episode is evidence produced by a verified handoff and
  delivery boundary that observed the source writer going quiescent and
  observed both heads. A caller-supplied boolean, a pull request body marker, a
  commit trailer, the opener or the most recent label are none of them able to
  attest that a takeover happened.
* Episodes are bound to repository, pull request, branch, source lane,
  destination lane, expected head and resulting head. An episode that does not
  bind to the pull request under decision is not evidence about it.
* Resolution is exact-head. Lineage that stops short of the current head is
  *waiting*, never a guess about who wrote the current diff.
* Conflicting, duplicated, unchained or unbound evidence fails closed with one
  concise owner action rather than picking a winner.

Purity is part of the contract, not an implementation detail. Nothing here
reads the environment, touches a store, opens a socket or imports an adapter:
every decision is a function of its explicit arguments. That is what lets the
same answer be computed by the package, by the vendored ``tools/`` copy inside
a generated product repository, and by a reviewer host that has no private
state at all. Recording, publication, label application and consumer
activation are deliberately somewhere else.

Everything here is metadata-only: lane names, a repository slug, a pull request
number, a branch name and commit shas.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence


SCHEMA = "code_mower.builderLineage.v1"
EPISODE_SCHEMA = "code_mower.contributionEpisode.v1"
RECORD_SCHEMA = "code_mower.builderLineageRecord.v1"

#: Hidden marker used to publish bounded lineage metadata on a pull request.
#: Only comments from an already trusted author are parsed; the marker is a
#: transport, never an authorization.
LINEAGE_MARKER = "CODE_MOWER_BUILDER_LINEAGE"
LINEAGE_MARKER_RE = re.compile(
    r"<!--\s*" + LINEAGE_MARKER + r"\s+(?P<payload>\{.*?\})\s*-->",
    re.DOTALL,
)

#: Marker *presence*, decided without looking at the payload at all.
#:
#: :data:`LINEAGE_MARKER_RE` only matches a complete, object-shaped, terminated
#: marker, so looking for evidence with it alone means a broken marker is not
#: seen rather than read as broken. Absence and unreadability are opposite
#: answers: one admits an independent reviewer on the ordinary single-builder
#: story, the other must stop. Presence is found first, and the payload is then
#: required to parse.
LINEAGE_MARKER_PRESENT_RE = re.compile(r"<!--\s*" + LINEAGE_MARKER + r"(?![0-9A-Z_])")

#: How much of one comment body a published marker is parsed out of.
MAX_MARKER_BODY_CHARS = 2048 * 32

LANE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}\Z")
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
REPO_RE = re.compile(r"[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}\Z")
BRANCH_RE = re.compile(r"[A-Za-z0-9._/-]{1,200}\Z")

#: Verified writer states a handoff boundary may report for a takeover.
#: Anything else (including a missing or "unknown" state) is uncertainty.
HANDOFF_WRITER_STATES = frozenset({"suspended", "terminated"})

#: The only writer state a continuation may carry. A continuation is recorded
#: after the destination lane's *own* supervised round ended and its process
#: group was reaped, so the writer that went quiescent is the recording lane
#: itself. Spelling that differently from the handoff states keeps a takeover
#: episode from ever being mistaken for a continuation, or the reverse.
CONTINUATION_WRITER_STATE = "self_quiescent"

WRITER_STATES = HANDOFF_WRITER_STATES | {CONTINUATION_WRITER_STATE}

#: A takeover moves the pen between two lanes; a continuation is the lane that
#: already holds it advancing the same pull request in an ordinary fix round.
HANDOFF_KIND = "handoff"
CONTINUATION_KIND = "continuation"
EPISODE_KINDS = frozenset({HANDOFF_KIND, CONTINUATION_KIND})

#: A lineage longer than this is treated as malformed rather than walked.
MAX_EPISODES = 32

#: How many raw arrivals a caller may hand the contract. A lineage is at most
#: :data:`MAX_EPISODES` distinct episodes, but the same chain legitimately
#: arrives many times over: evidence is published as a *cumulative* snapshot,
#: so a full-length lineage is delivered as ``1 + 2 + ... + MAX_EPISODES``
#: entries, and a reader also holding the private record sees the completed
#: chain once more on top. Anything under that total would refuse a lineage the
#: system is documented to support. See :func:`bounded_arrivals`.
MAX_EPISODE_ARRIVALS = MAX_EPISODES * (MAX_EPISODES + 1) // 2 + MAX_EPISODES

#: Retained name for the arrival bound, used by the vendored gate helper.
MAX_EPISODE_ENTRIES = MAX_EPISODE_ARRIVALS

EPISODE_FIELDS = (
    "schema",
    "sequence",
    "kind",
    "repo",
    "pr_number",
    "branch",
    "source_lane",
    "destination_lane",
    "expected_head",
    "resulting_head",
    "writer_state",
)


class LineageError(ValueError):
    """Raised when contribution evidence cannot be accepted as recorded."""


class IdentityConflictError(LineageError):
    """The configured identity contract gives one key two different answers."""


class _Refusal(LineageError):
    """An internal refusal carrying the reason a resolver reports."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _lane(value: Any) -> str:
    lane = _text(value).lower()
    return lane if LANE_RE.match(lane) else ""


def _sha(value: Any) -> str:
    sha = _text(value).lower()
    return sha if SHA_RE.match(sha) else ""


def _is_pr_number(value: Any) -> bool:
    """Whether ``value`` already *is* a usable pull request number.

    Asked directly, because :func:`_pr_number` answers a different question.
    That function normalizes, and it spells "unusable" as ``0`` -- so comparing
    its result back against its input accepts the two values that equal that
    sentinel. ``0`` is not a pull request, and ``False`` is not either; both
    round-tripped as valid and bound an episode to a pull request that cannot
    exist. A bool is never a number here even when it compares equal to one.
    """

    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 < value <= 2**31 - 1
    )


def _pr_number(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if _is_pr_number(number) else 0


@dataclass(frozen=True)
class ContributionEpisode:
    """One verified builder contribution bound to an exact head transition.

    ``expected_head`` is the head the source lane left behind and the
    destination lane was launched against; ``resulting_head`` is the head the
    destination lane actually produced. A destination that never moved the head
    is still the writer, but it contributed nothing to the current diff.

    ``kind`` is ``handoff`` when the pen moved between two lanes and
    ``continuation`` when the lane that already held it advanced the same pull
    request again. A continuation is the only episode whose source and
    destination lane are the same, and it must carry the self-quiescent writer
    state, so neither shape can be forged into the other.
    """

    sequence: int
    repo: str
    pr_number: int
    branch: str
    source_lane: str
    destination_lane: str
    expected_head: str
    resulting_head: str
    writer_state: str
    kind: str = HANDOFF_KIND

    def __post_init__(self) -> None:
        same_lane = self.source_lane == self.destination_lane
        if self.kind == CONTINUATION_KIND:
            shape_ok = same_lane and self.writer_state == CONTINUATION_WRITER_STATE
        elif self.kind == HANDOFF_KIND:
            shape_ok = not same_lane and self.writer_state in HANDOFF_WRITER_STATES
        else:
            shape_ok = False
        if (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or not 1 <= self.sequence <= MAX_EPISODES
            or not REPO_RE.match(_text(self.repo))
            or not _is_pr_number(self.pr_number)
            or not BRANCH_RE.match(_text(self.branch))
            or not LANE_RE.match(_text(self.source_lane))
            or not LANE_RE.match(_text(self.destination_lane))
            or not SHA_RE.match(_text(self.expected_head))
            or not SHA_RE.match(_text(self.resulting_head))
            or not shape_ok
        ):
            raise LineageError("contribution episode is malformed")

    @property
    def moved_head(self) -> bool:
        return self.expected_head != self.resulting_head

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": EPISODE_SCHEMA,
            "sequence": self.sequence,
            "kind": self.kind,
            "repo": self.repo,
            "pr_number": self.pr_number,
            "branch": self.branch,
            "source_lane": self.source_lane,
            "destination_lane": self.destination_lane,
            "expected_head": self.expected_head,
            "resulting_head": self.resulting_head,
            "writer_state": self.writer_state,
        }


def episode_from_mapping(payload: Mapping[str, Any]) -> ContributionEpisode:
    """Parse one episode strictly. Unknown or missing fields are malformed."""

    if not isinstance(payload, Mapping) or set(payload) != set(EPISODE_FIELDS):
        raise LineageError("contribution episode is malformed")
    if payload.get("schema") != EPISODE_SCHEMA:
        raise LineageError("contribution episode schema is unsupported")
    return ContributionEpisode(
        sequence=payload.get("sequence"),  # type: ignore[arg-type]
        kind=_text(payload.get("kind")).lower(),
        repo=_text(payload.get("repo")),
        pr_number=_pr_number(payload.get("pr_number")),
        branch=_text(payload.get("branch")),
        source_lane=_lane(payload.get("source_lane")),
        destination_lane=_lane(payload.get("destination_lane")),
        expected_head=_sha(payload.get("expected_head")),
        resulting_head=_sha(payload.get("resulting_head")),
        writer_state=_text(payload.get("writer_state")).lower(),
    )


def episode_from_handoff(
    handoff: Mapping[str, Any] | Any,
    *,
    resulting_head: str,
    writer_state: str,
    sequence: int,
    repo: str = "",
) -> ContributionEpisode:
    """Build an episode from an accepted handoff plus its delivered head.

    ``handoff`` is the handoff record (or its ``as_dict``) that a verified
    handoff boundary accepted, so repository, pull request, branch, lanes and
    expected head all come from evidence that boundary observed.
    ``resulting_head`` is the delivery-side attestation of what the destination
    lane actually produced; it is deliberately not part of the handoff record,
    which is written before the destination lane has written anything.

    The handoff record is supplied as plain data. This function never reaches
    for a store, which is what lets the conversion be tested and audited
    without one.
    """

    record = handoff if isinstance(handoff, Mapping) else handoff.as_dict()
    if not isinstance(record, Mapping):
        raise LineageError("contribution episode is malformed")
    target_pr = _text(record.get("target_pr"))
    if "#" not in target_pr:
        raise LineageError("contribution episode is malformed")
    pr_repo, _, pr_number = target_pr.partition("#")
    if repo and _text(repo).lower() != pr_repo.lower():
        raise LineageError(
            "contribution episode does not bind to the repository under work"
        )
    return ContributionEpisode(
        sequence=sequence,
        kind=HANDOFF_KIND,
        repo=pr_repo,
        pr_number=_pr_number(pr_number),
        branch=_text(record.get("target_branch")),
        source_lane=_lane(record.get("source_lane")),
        destination_lane=_lane(record.get("destination_lane")),
        expected_head=_sha(record.get("expected_head")),
        resulting_head=_sha(resulting_head),
        writer_state=_text(writer_state).lower(),
    )


def continuation_episode(
    previous: ContributionEpisode,
    *,
    lane: str,
    resulting_head: str,
    branch: str = "",
) -> ContributionEpisode:
    """Build the next episode for an ordinary round by the current writer.

    A continuation repairs the gap the takeover model would otherwise leave:
    once a lane has taken a pull request over, its next fix round advances the
    head without any new handoff to record, and exact-head resolution would
    report ``lineage_behind_head`` forever.

    It is not a handoff and must not be manufactured into one. ``previous`` is
    the recorded tip, and only the lane that record already names as the
    current writer may continue from it; every other field is inherited from
    that verified episode rather than supplied by the caller.
    """

    if not isinstance(previous, ContributionEpisode):
        raise LineageError("contribution episode is malformed")
    writer = _lane(lane)
    if not writer or writer != previous.destination_lane:
        raise LineageError(
            "only the lane the recorded lineage names as current writer may continue it"
        )
    observed = _sha(resulting_head)
    if not observed or observed == previous.resulting_head:
        raise LineageError("a continuation must record a head the writer actually moved")
    target_branch = _text(branch) or previous.branch
    if target_branch != previous.branch:
        raise LineageError("a continuation must stay on the recorded branch")
    return ContributionEpisode(
        sequence=previous.sequence + 1,
        kind=CONTINUATION_KIND,
        repo=previous.repo,
        pr_number=previous.pr_number,
        branch=previous.branch,
        source_lane=writer,
        destination_lane=writer,
        expected_head=previous.resulting_head,
        resulting_head=observed,
        writer_state=CONTINUATION_WRITER_STATE,
    )


@dataclass(frozen=True)
class Lineage:
    """The resolved answer for one pull request at one exact head.

    ``status`` is ``resolved`` only when the evidence covers the current head
    without contradiction. ``waiting`` and ``conflict`` both carry a concise
    ``owner_action`` and never imply a writer.
    """

    status: str
    reason: str
    head_sha: str
    contributors: tuple[str, ...]
    current_writer: str
    builder_label: str
    stale_builder_labels: tuple[str, ...]
    evidence: str
    episodes: int
    owner_action: str = ""

    @property
    def resolved(self) -> bool:
        return self.status == "resolved"

    def contributed(self, lane: str) -> bool:
        """Whether ``lane`` is a verified contributor to the current diff."""

        return _lane(lane) in self.contributors

    def independent(self, lane: str) -> bool:
        """Whether ``lane`` may satisfy an independent review requirement.

        Independence is a separate decision from role eligibility: a qualified
        reviewer lane that contributed to this diff is still not independent of
        it, and an unqualified lane is not made eligible by being independent.
        Unresolved lineage is never independence.
        """

        return self.resolved and bool(_lane(lane)) and not self.contributed(lane)

    def admission(self, lane: str) -> dict[str, Any]:
        """A closed, metadata-only admission decision for one reviewer lane."""

        candidate = _lane(lane)
        if not candidate:
            admitted, reason = False, "reviewer_lane_invalid"
        elif not self.resolved:
            admitted, reason = False, "lineage_" + self.status
        elif self.contributed(candidate):
            admitted, reason = False, "contributor_not_independent"
        else:
            admitted, reason = True, "independent"
        return {
            "schema": SCHEMA,
            "lane": candidate,
            "admitted": admitted,
            "reason": reason,
            "head_sha": self.head_sha,
            "contributors": list(self.contributors),
            "current_writer": self.current_writer,
            "owner_action": (
                "" if admitted else (self.owner_action or _ADMISSION_ACTIONS[reason])
            ),
        }

    def independent_lanes(self, lanes: Iterable[str]) -> tuple[str, ...]:
        return tuple(lane for lane in lanes if self.independent(lane))

    def as_dict(self) -> dict[str, Any]:
        """Bounded metadata for status/Board projection and public rendering."""

        return {
            "schema": SCHEMA,
            "status": self.status,
            "reason": self.reason,
            "head_sha": self.head_sha,
            "contributors": list(self.contributors),
            "current_writer": self.current_writer,
            "builder_label": self.builder_label,
            "stale_builder_labels": list(self.stale_builder_labels),
            "evidence": self.evidence,
            "episodes": self.episodes,
            "owner_action": self.owner_action,
        }


_ADMISSION_ACTIONS = {
    "reviewer_lane_invalid": "name the reviewer lane requesting admission",
    "contributor_not_independent": (
        "select a reviewer lane that did not contribute to this head"
    ),
    "lineage_waiting": "re-record builder contribution lineage for the current head",
    "lineage_conflict": "resolve the conflicting builder contribution evidence",
}

_OWNER_ACTIONS = {
    "lineage_behind_head": (
        "recorded builder lineage stops before the current head; "
        "re-record the contribution episode for this head"
    ),
    "episode_malformed": (
        "a recorded contribution episode is malformed; re-record it from the "
        "verified handoff"
    ),
    "episode_unbound": (
        "a recorded contribution episode does not bind to this repository, "
        "pull request or branch; re-record it against this pull request"
    ),
    "episode_duplicated": (
        "two different contribution episodes claim the same position; "
        "re-record the lineage for this pull request"
    ),
    "episode_unchained": (
        "recorded contribution episodes do not form one head-to-head chain; "
        "re-record the lineage from the verified handoff"
    ),
    "writer_state_unverified": (
        "a recorded contribution episode has no verified source writer state; "
        "re-run the handoff boundary before recording it"
    ),
    "opener_outside_lineage": (
        "the pull request opener is not part of the recorded lineage; "
        "record the opening contribution or correct the lineage"
    ),
    "label_outside_lineage": (
        "an active builder label names a lane with no recorded contribution; "
        "remove it or record the missing contribution"
    ),
    "conflicting_builder_identity": (
        "builder author and label evidence disagree and no verified handoff "
        "explains the change; record the handoff or correct the identity"
    ),
    "target_invalid": (
        "the pull request target could not be identified; supply repository, "
        "number, branch and the exact head"
    ),
}


def _lineage(
    status: str,
    reason: str,
    *,
    head_sha: str = "",
    contributors: Sequence[str] = (),
    current_writer: str = "",
    stale: Sequence[str] = (),
    evidence: str = "none",
    episodes: int = 0,
) -> Lineage:
    return Lineage(
        status=status,
        reason=reason,
        head_sha=head_sha,
        contributors=tuple(contributors),
        current_writer=current_writer,
        builder_label=f"builder:{current_writer}" if current_writer else "",
        stale_builder_labels=tuple(dict.fromkeys(stale)),
        evidence=evidence,
        episodes=episodes,
        owner_action="" if status == "resolved" else _OWNER_ACTIONS.get(reason, ""),
    )


@dataclass(frozen=True)
class ExactTarget:
    """The complete binding every exact-head decision needs, or nothing."""

    repo: str
    pr_number: int
    branch: str
    head_sha: str


def require_exact_target(
    *, repo: Any, pr_number: Any, branch: Any, head_sha: Any
) -> ExactTarget:
    """Validate a complete repository/pull request/branch/head binding.

    One contract, so no caller reaches resolution holding three of the four
    fields. A partially populated target is not an absent one: falling back to
    the identity-only answer on missing data is how a decision that was never
    bound to a branch came back ``resolved``, and how an episode from another
    branch was accepted as evidence about this one. Identity-only is a route a
    caller selects on purpose (:func:`resolve_identity_only`), never one it
    lands in by leaving a field out.

    The pull request number must already *be* a number. Coercing it here would
    make ``"959"`` a valid target, and this contract is what a caller reaches
    holding data it claims to have verified -- a string that happens to parse
    is data nobody checked. The coercive parser stays where raw published or
    recorded payloads are read (:func:`episode_from_mapping`), which is the
    boundary that legitimately has text to interpret.
    """

    target_repo = _text(repo)
    name = _text(branch)
    head = _sha(head_sha)
    if (
        not REPO_RE.match(target_repo)
        or not _is_pr_number(pr_number)
        or not name
        or not BRANCH_RE.match(name)
        or not head
    ):
        raise _Refusal("target_invalid")
    return ExactTarget(
        repo=target_repo, pr_number=pr_number, branch=name, head_sha=head
    )


def bounded_arrivals(
    items: Any, *, limit: int = MAX_EPISODE_ARRIVALS, what: str = "contribution evidence"
) -> Any:
    """Yield at most ``limit`` raw arrivals, refusing the one past it.

    The bound is enforced *while* the input is walked, so an oversized input is
    never materialised and a lazy source is not consumed past the first
    disallowed arrival. Collectors and composers keep their validated raw
    arrivals uncollapsed: deduplicating on the way in would let two
    independently collapsed inputs reset this cap, and the owning resolver is
    the one place that can both collapse a repeat and refuse a contradiction.
    """

    count = 0
    for item in items or ():
        count += 1
        if count > limit:
            raise _Refusal("episode_malformed")
        yield item


def require_episode(item: Any) -> ContributionEpisode:
    """One episode, however it arrived. Anything else is malformed evidence."""

    if isinstance(item, ContributionEpisode):
        return item
    if isinstance(item, Mapping):
        return episode_from_mapping(item)
    raise LineageError("contribution episode is malformed")


def _collect_episodes(
    episodes: Any, *, target: ExactTarget | None = None
) -> dict[int, ContributionEpisode]:
    """Walk bounded raw arrivals into one collapsed position map.

    A conforming cumulative publication history -- the same chain republished
    after every round, optionally overlapping a private record of it -- is
    collapsed here rather than refused for its length. Working state never
    exceeds the lineage bound, because an episode whose sequence falls outside
    ``1..MAX_EPISODES`` never constructs. Repeats collapse; disagreements at
    one position refuse.
    """

    seen: dict[int, ContributionEpisode] = {}
    for item in bounded_arrivals(episodes):
        try:
            episode = require_episode(item)
        except _Refusal:
            raise
        except LineageError:
            raise _Refusal("episode_malformed") from None
        if target is not None and (
            episode.repo.lower() != target.repo.lower()
            or episode.pr_number != target.pr_number
            or episode.branch != target.branch
        ):
            raise _Refusal("episode_unbound")
        expected_state = (
            {CONTINUATION_WRITER_STATE}
            if episode.kind == CONTINUATION_KIND
            else HANDOFF_WRITER_STATES
        )
        if episode.writer_state not in expected_state:
            raise _Refusal("writer_state_unverified")
        previous = seen.get(episode.sequence)
        if previous is not None:
            if previous.as_dict() != episode.as_dict():
                # Two records claim the same position and disagree. Which one
                # describes the diff is exactly what cannot be guessed.
                raise _Refusal("episode_duplicated")
            continue
        seen[episode.sequence] = episode
    return seen


def _order_episodes(
    seen: Mapping[int, ContributionEpisode]
) -> tuple[ContributionEpisode, ...]:
    """One head-to-head chain beginning where the pen moved, or a refusal."""

    ordered = [seen[sequence] for sequence in sorted(seen)]
    if [episode.sequence for episode in ordered] != list(range(1, len(ordered) + 1)):
        raise _Refusal("episode_unchained")
    # Lineage begins when the pen moves. A continuation with nothing to continue
    # describes an ordinary single-builder round, which needs no episode at all.
    if ordered[0].kind != HANDOFF_KIND:
        raise _Refusal("episode_unchained")
    for index, episode in enumerate(ordered):
        if index and (
            episode.expected_head != ordered[index - 1].resulting_head
            or episode.source_lane != ordered[index - 1].destination_lane
        ):
            raise _Refusal("episode_unchained")
    return tuple(ordered)


def require_episode_chain(episodes: Any) -> tuple[ContributionEpisode, ...]:
    """Validate raw arrivals into one complete, nonempty, ordered chain.

    The same walk the resolver uses, so a chain that would not resolve is never
    published either. Raises :class:`LineageError` rather than answering with a
    shorter chain: losing an episode is exactly the outcome being prevented.
    """

    seen = _collect_episodes(episodes)
    if not seen:
        raise LineageError("a published builder lineage must carry at least one episode")
    return _order_episodes(seen)


def resolve_lineage(
    *,
    repo: str,
    pr_number: Any,
    branch: str,
    head_sha: str,
    episodes: Sequence[Mapping[str, Any] | ContributionEpisode] = (),
    opener_lane: str = "",
    label_lanes: Sequence[str] = (),
    branch_lane: str = "",
) -> Lineage:
    """Resolve who built the diff at ``head_sha``.

    The full exact target is required. ``episodes`` is verified
    handoff/delivery evidence. ``opener_lane``, ``label_lanes`` and
    ``branch_lane`` are the weak signals the rest of the system used to carry
    on with on their own: they are accepted here only as corroboration, and
    they can fail the resolution closed, but none of them can establish a
    takeover.
    """

    head = _sha(head_sha)
    try:
        target = require_exact_target(
            repo=repo, pr_number=pr_number, branch=branch, head_sha=head_sha
        )
        seen = _collect_episodes(episodes, target=target)
        ordered = _order_episodes(seen) if seen else ()
    except _Refusal as refusal:
        return _lineage("conflict", refusal.reason, head_sha=head)

    opener = _lane(opener_lane)
    labels = tuple(
        dict.fromkeys(lane for lane in (_lane(item) for item in label_lanes) if lane)
    )
    if not ordered:
        return resolve_identity_only(
            opener_lane=opener,
            label_lanes=labels,
            head_sha=target.head_sha,
            branch_lane=branch_lane,
        )

    contributors = list(
        dict.fromkeys(
            [ordered[0].source_lane]
            + [episode.destination_lane for episode in ordered if episode.moved_head]
        )
    )
    writer = ordered[-1].destination_lane

    if ordered[-1].resulting_head != head:
        # The lineage may be stale, or history may have been rewritten under
        # it. Either way it does not describe the diff being decided on.
        return _lineage(
            "waiting",
            "lineage_behind_head",
            head_sha=head,
            evidence="handoff_episodes",
            episodes=len(ordered),
        )
    if opener and opener not in contributors and opener != writer:
        return _lineage(
            "conflict",
            "opener_outside_lineage",
            head_sha=head,
            evidence="handoff_episodes",
            episodes=len(ordered),
        )
    known = set(contributors) | {writer}
    if any(lane not in known for lane in labels):
        return _lineage(
            "conflict",
            "label_outside_lineage",
            head_sha=head,
            evidence="handoff_episodes",
            episodes=len(ordered),
        )
    return _lineage(
        "resolved",
        "verified_handoff" if len(ordered) > 1 else "verified_takeover",
        head_sha=head,
        contributors=contributors,
        current_writer=writer,
        stale=[lane for lane in labels if lane != writer],
        evidence="handoff_episodes",
        episodes=len(ordered),
    )


def resolve_identity_only(
    *,
    opener_lane: str = "",
    label_lanes: Sequence[str] = (),
    head_sha: str = "",
    branch_lane: str = "",
) -> Lineage:
    """The ordinary single-builder case, with no verified handoff evidence.

    With no verified handoff there is exactly one consistent story available:
    one lane opened the pull request and still holds the only builder label.
    Any other combination is the inconsistency this contract exists to stop
    guessing about, so it fails closed instead of preferring the opener or the
    newest label.

    ``branch_lane`` is the deployment's *configured* branch identity, a signal
    of the same weight as the label and the opener, so it joins them as a
    candidate: a ``codex/`` branch carrying a ``builder:claude`` label is two
    lanes disagreeing about who wrote the diff, and answering "Claude" would
    admit Codex to review its own work. It can never establish a takeover --
    only a recorded episode does that -- only refuse to pick a winner.
    """

    head = _sha(head_sha)
    opener = _lane(opener_lane)
    branch = _lane(branch_lane)
    labels = tuple(
        dict.fromkeys(lane for lane in (_lane(item) for item in label_lanes) if lane)
    )
    candidates = tuple(
        dict.fromkeys(
            ([opener] if opener else []) + list(labels) + ([branch] if branch else [])
        )
    )
    if len(candidates) > 1:
        return _lineage("conflict", "conflicting_builder_identity", head_sha=head)
    if not candidates:
        return _lineage("resolved", "no_builder_identity", head_sha=head, evidence="none")
    lane = candidates[0]
    return _lineage(
        "resolved",
        "single_builder",
        head_sha=head,
        contributors=(lane,),
        current_writer=lane,
        evidence="single_builder",
    )


def _collapse_aliases(raw: Any, *, what: str) -> dict[str, Any]:
    """Collapse trim/case-equivalent keys once, refusing a contradiction.

    Account logins and branch prefixes are both matched trimmed and case-folded
    downstream, so two spellings of one key are one key. Which spelling survived
    used to depend on insertion order, letting an alias outrank the canonical
    account. Values are retained exactly as configured, so trimming a key never
    loses its lookup.
    """

    collapsed: dict[str, Any] = {}
    if not isinstance(raw, Mapping):
        return collapsed
    for key, value in raw.items():
        name = _text(key).lower()
        if not name:
            continue
        if name in collapsed:
            first, second = _lane(collapsed[name]), _lane(value)
            if first != second:
                raise IdentityConflictError(
                    f"the configured {what} name `{name}` as both "
                    f"`{first or 'nothing'}` and `{second or 'nothing'}`; they are "
                    f"matched case-insensitively, so this is one key with two answers"
                )
            continue
        collapsed[name] = value
    return collapsed


def canonical_identity(identity: Mapping[str, Any] | None) -> dict[str, Any]:
    """The one normalized identity contract every consumer reads.

    Produced once and reused by lane naming, branch lookup, the complete
    resolver, the carried context and the reviewer floor, so a second
    normalization cannot drift from this one. Every other configured field is
    carried through untouched. Idempotent: canonicalizing a canonical contract
    is a no-op.
    """

    base = dict(identity) if isinstance(identity, Mapping) else {}
    labels = base.get("labels")
    base["labels"] = dict(labels) if isinstance(labels, Mapping) else {}
    base["authors"] = _collapse_aliases(base.get("authors"), what="accounts")
    base["branch_prefixes"] = _collapse_aliases(
        base.get("branch_prefixes"), what="branch prefixes"
    )
    return base


def lanes_from_identity(
    *,
    identity: Mapping[str, Any] | None,
    labels: Sequence[str] = (),
    author: str = "",
) -> tuple[str, tuple[str, ...]]:
    """Map GitHub labels and a pull request author onto builder lane names.

    Returns ``(opener_lane, label_lanes)``; both are weak signals that
    :func:`resolve_lineage` may reject.
    """

    contract = canonical_identity(identity)
    if not contract.get("enabled"):
        return "", ()
    label_map, author_map = contract["labels"], contract["authors"]
    label_lanes = tuple(
        dict.fromkeys(
            lane
            for lane in (_lane(label_map.get(_text(label))) for label in labels)
            if lane
        )
    )
    return _lane(author_map.get(_text(author).lower())), label_lanes


def branch_lane_from_identity(
    *, identity: Mapping[str, Any] | None, branch: str = ""
) -> str:
    """The lane the deployment's configured branch prefixes name, if any.

    ``branch_prefixes`` and ``require_verified_lineage`` are read only
    together: a deployment that did not ask for verified lineage keeps the
    ordinary opener/label answer it has always had.
    """

    contract = canonical_identity(identity)
    if not contract.get("enabled") or not contract.get("require_verified_lineage"):
        return ""
    prefixes = contract["branch_prefixes"]
    name = _text(branch).lower()
    if not name:
        return ""
    # Longest prefix first, so `codex-review/` cannot be shadowed by `codex-`.
    for prefix in sorted(prefixes, key=len, reverse=True):
        if name.startswith(prefix):
            return _lane(prefixes[prefix])
    return ""


def resolve_configured_identity(
    *,
    identity: Mapping[str, Any] | None,
    labels: Sequence[str] = (),
    author: str = "",
    branch: str = "",
) -> Lineage:
    """The identity-only route, selected on purpose and taking no evidence.

    There is deliberately no ``episodes`` argument. A caller that has evidence
    has a pull request to bind it to, and must go through
    :func:`resolve_builder_lineage`; this is the answer for the ordinary case
    where there is nothing to bind.
    """

    contract = canonical_identity(identity)
    opener_lane, label_lanes = lanes_from_identity(
        identity=contract, labels=labels, author=author
    )
    return resolve_identity_only(
        opener_lane=opener_lane,
        label_lanes=label_lanes,
        branch_lane=branch_lane_from_identity(identity=contract, branch=branch),
    )


def resolve_builder_lineage(
    *,
    identity: Mapping[str, Any] | None,
    repo: str,
    pr_number: Any,
    branch: str,
    head_sha: str,
    labels: Sequence[str] = (),
    author: str = "",
    episodes: Sequence[Mapping[str, Any] | ContributionEpisode] = (),
) -> Lineage:
    """The one identity-plus-branch-plus-evidence decision, in one place.

    Every consumer -- the gate, the labelers, the reviewer wrappers, the status
    projection -- has to reach the same answer from the same inputs, and the
    way they stopped agreeing was by each composing the pieces slightly
    differently. So the composition itself is the contract: labels and author
    become lane names, the configured branch prefix becomes a lane, and the
    verified episodes decide.

    The branch signal is counted **whether or not there are episodes**. A
    configured ``codex/`` branch carrying a ``builder:claude`` label with no
    recorded handoff is a conflict, not an ordinary single-builder pull
    request; treating the empty-episode case as a separate, easier question is
    how a lane came to review its own diff. It is still only a conflict signal:
    a branch prefix never grants takeover authority, and only a verified
    episode moves the writer.

    The complete exact target is required. Missing or invalid target data is a
    ``target_invalid`` conflict, never a quiet downgrade to the identity-only
    answer: that downgrade skipped branch binding entirely, so evidence from
    another branch resolved as evidence about this one. Callers that genuinely
    have no pull request use :func:`resolve_configured_identity`.
    """

    contract = canonical_identity(identity)
    opener_lane, label_lanes = lanes_from_identity(
        identity=contract, labels=labels, author=author
    )
    branch_lane = branch_lane_from_identity(identity=contract, branch=branch)
    return resolve_lineage(
        repo=repo,
        pr_number=pr_number,
        branch=branch,
        head_sha=head_sha,
        episodes=episodes,
        opener_lane=opener_lane,
        label_lanes=label_lanes,
        branch_lane=branch_lane,
    )


# --- raw transport validation ------------------------------------------------

#: The author field, by transport. GitHub REST names the commenter ``user``;
#: ``gh ... --json comments`` names it ``author``. Both are nullable for a
#: deleted account, and whichever one a payload carries is validated.
COMMENT_AUTHOR_FIELDS = ("user", "author")

#: "This argument was not supplied", which no JSON value can say for itself.
#: ``None``, ``False`` and ``{}`` are all *present* values that a comment
#: history is not allowed to be, so none of them can stand in for absence.
OMITTED: Any = object()


def require_comment_list(value: Any, *, what: str) -> tuple[Mapping[str, Any], ...]:
    """A successful comment read must be a complete list of comment objects.

    ``None``, ``False``, a bare object and a list holding a non-object are all
    *successful* responses that carry no readable history. Normalising them to
    an empty list -- with ``or ()``, or by filtering non-mappings out -- turns
    "this could not be read" into "there is nothing here", which is the answer
    that admits a reviewer onto a diff whose takeover marker was in the part
    that got dropped. A genuinely empty list is ordinary and stays ordinary.
    """

    if not isinstance(value, list):
        raise LineageError(f"{what} did not come back as a list of comments")
    for item in value:
        if not isinstance(item, Mapping):
            raise LineageError(f"{what} contains an entry that is not a comment")
        require_comment_record(item, what=what)
    return tuple(value)


def require_comment_record(comment: Mapping[str, Any], *, what: str) -> None:
    """The two fields lineage actually reads must be readable, or nothing is.

    Being a dict is not being a comment. The marker lives in ``body`` and trust
    is decided from the author's ``login``, so a present ``body`` that is a
    number, or a ``login`` that is an object, is a record whose meaning cannot
    be recovered. Coercing those with ``str()`` invents an author or a body
    GitHub never sent; skipping them drops the record. Either way an unreadable
    history becomes an absent one, and absence is what admits a reviewer.

    GitHub's own schema is the boundary, not a stricter invention of one:
    ``"user": null`` (deleted account) and an omitted ``body`` are accepted and
    simply name no author and no marker.
    """

    if not isinstance(comment, Mapping):
        raise LineageError(f"{what} contains an entry that is not a comment")
    # Presence and value are different questions. An omitted optional field
    # says nothing and is ordinary; a field that is *there* and holds null or
    # the wrong type is a record whose meaning cannot be recovered. Only one
    # null is meaningful in GitHub's schema -- a whole author object, for a
    # comment whose account was deleted -- and that one stays valid.
    if "body" in comment and not isinstance(comment["body"], str):
        raise LineageError(f"{what} contains a comment whose body is not text")
    for field_name in COMMENT_AUTHOR_FIELDS:
        if field_name not in comment:
            continue
        author = comment[field_name]
        if author is None:
            continue
        if not isinstance(author, Mapping):
            raise LineageError(f"{what} contains a comment whose author is not an object")
        if "login" in author and not isinstance(author["login"], str):
            raise LineageError(
                f"{what} contains a comment whose author login is not text"
            )


def comment_author_login(comment: Mapping[str, Any]) -> str:
    """The commenter's login under whichever transport the record came from.

    A ``gh --json comments`` record names the author ``author`` rather than
    ``user``. Reading only ``user`` means every marker published through that
    transport is attributed to nobody, silently trusted by no rule, and
    therefore never read -- announced evidence disappearing into the ordinary
    single-builder answer again.
    """

    for field_name in COMMENT_AUTHOR_FIELDS:
        if field_name not in comment:
            continue
        author = comment[field_name]
        if isinstance(author, Mapping):
            login = author.get("login")
            if isinstance(login, str) and login.strip():
                return login.strip()
    return ""


def comment_body(comment: Mapping[str, Any]) -> str:
    """The comment's body text. Absent is empty; present-and-invalid raised."""

    require_comment_record(comment, what="comment")
    body = comment.get("body")
    return body if isinstance(body, str) else ""


def flatten_comment_pages(pages: Any, *, what: str = "the comment history") -> list[dict]:
    """Flatten a slurped paginated comment response, validating as it goes.

    A generic flattener is right for a mixed event timeline and wrong for a
    comment history: it drops members it cannot use, and a dropped comment is a
    dropped marker. So pages are validated before anything flattens, filters or
    stringifies them, and an unreadable page raises rather than shrinking.

    Every page is an array. ``gh api --paginate --slurp`` produces a list of
    pages, so anything else in that position is not a page. Accepting a bare
    object as a one-comment page reinterprets ``{}`` or ``{"comments": []}`` as
    a comment with no body and no author, and a response nobody could read then
    looks like an absent history -- which record validation cannot recover,
    because the wrapper has already made the payload look well formed.

    Genuinely empty pages stay ordinary.
    """

    if not isinstance(pages, list):
        raise LineageError(f"{what} did not come back as a paginated array")
    comments: list[dict] = []
    for index, page in enumerate(pages, start=1):
        if not isinstance(page, list):
            raise LineageError(f"{what}: page {index} is not an array of comments")
        comments.extend(
            dict(comment)
            for comment in require_comment_list(page, what=f"{what}: page {index}")
        )
    return comments


def select_comment_history(
    *,
    selected: Any = OMITTED,
    embedded: Any = OMITTED,
    what: str = "the pull request comment history",
) -> tuple[Mapping[str, Any], ...]:
    """Choose the comment history to read, and validate exactly that choice.

    Two shapes can sit under a pull request payload's ``comments`` key and they
    mean opposite things. ``gh pr view --json comments`` embeds the actual list;
    the REST pull request representation puts an integer *count* there. Reading
    the count as a history makes ``42`` an unreadable object and fails a run
    that is perfectly ordinary; reading an embedded list as a count throws the
    takeover marker away.

    An explicitly ``selected`` history always wins, including when it is
    genuinely empty -- a caller that says "read these comments and no others"
    has answered the question, and letting whatever sits beside it override
    that would make the selection meaningless. Everything that is neither
    omitted nor a REST count is validated as a history and fails closed.
    """

    if selected is not OMITTED:
        return require_comment_list(selected, what=what)
    if embedded is OMITTED:
        return ()
    # A REST numeric count is metadata about the history, not the history.
    if isinstance(embedded, int) and not isinstance(embedded, bool):
        return ()
    return require_comment_list(embedded, what=what)


# --- bounded public transport ------------------------------------------------


def lineage_comment_marker(episodes: Sequence[ContributionEpisode]) -> str:
    """Render a validated chain as one hidden, metadata-only marker.

    Rendering is not a truncation operation. Slicing to the bound turned an
    over-long chain into a *successful* publication that had quietly lost its
    newest episodes -- the exact evidence loss this contract exists to prevent,
    and undetectable by the reader, which sees a well-formed marker. So the
    whole chain is validated through :func:`require_episode_chain` first, and a
    chain that could not resolve is refused rather than shortened.
    """

    payload = {
        "schema": SCHEMA,
        "episodes": [episode.as_dict() for episode in require_episode_chain(episodes)],
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"<!-- {LINEAGE_MARKER} {body} -->"


def _reject_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    """``object_pairs_hook`` that refuses an object naming a key twice.

    ``json.loads`` keeps the last value for a repeated key, so one marker can
    carry two answers to the same question -- two ``episodes`` lists, two
    ``schema`` values, two ``resulting_head`` shas inside one episode -- and
    every reader silently agrees on whichever came last. Which one describes
    the diff is exactly what must not be decided by parser order. This applies
    at every depth, so a conflicting nested binding is refused too.
    """

    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key in published builder lineage: {key}")
        seen[key] = value
    return seen


def _loads_without_duplicate_keys(payload: str) -> Any:
    return json.loads(payload, object_pairs_hook=_reject_duplicate_keys)


def episodes_from_comment_body(body: str) -> tuple[ContributionEpisode, ...]:
    """Parse lineage markers out of one already trusted comment body.

    The caller decides trust. This function never treats the presence of a
    marker as evidence that its author was allowed to publish one.
    """

    if body is None:
        body = ""
    if not isinstance(body, str):
        raise LineageError("published builder lineage is unreadable")
    text = _text(body)
    present = LINEAGE_MARKER_PRESENT_RE.findall(text)
    if not present:
        # An ordinary comment. Not evidence, and not a failure either.
        return ()
    if len(present) > 1:
        # Two markers on one comment cannot both be "the" published lineage,
        # and which one describes the head is exactly what may not be guessed.
        raise LineageError("published builder lineage is ambiguous")
    matches = LINEAGE_MARKER_RE.findall(text[:MAX_MARKER_BODY_CHARS])
    if len(matches) != 1:
        # The marker is there, but no single complete object payload parses out
        # of it: unterminated, not an object, or cut off past the bound.
        raise LineageError("published builder lineage is unreadable")
    try:
        payload = _loads_without_duplicate_keys(matches[0])
    except (ValueError, RecursionError):
        raise LineageError("published builder lineage is unreadable") from None
    if not isinstance(payload, Mapping) or payload.get("schema") != SCHEMA:
        raise LineageError("published builder lineage schema is unsupported")
    items = payload.get("episodes")
    if not isinstance(items, list) or len(items) > MAX_EPISODES:
        raise LineageError("published builder lineage is unreadable")
    if not items:
        # A marker announces lineage. The publisher refuses to publish zero
        # episodes, so a trusted marker carrying an empty chain is not a
        # history that happens to be empty -- it is a claim that contradicts
        # itself, and reading it as ordinary absence is how announced evidence
        # disappears into the single-builder answer.
        raise LineageError("published builder lineage declares no episodes")
    return tuple(episode_from_mapping(item) for item in items)


def published_episodes(
    comments: Sequence[Mapping[str, Any]],
    *,
    trusted_author: Callable[[str], bool],
) -> tuple[ContributionEpisode, ...]:
    """Collect lineage episodes published by already trusted comment authors.

    The hidden marker is a transport for bounded metadata. Trust comes from the
    caller's author check, never from the marker being present, so an untrusted
    commenter cannot assert a takeover into existence -- and equally cannot
    force a stop by posting a deliberately broken one.

    Records are validated, not skipped: an entry that is not a readable comment
    raises rather than quietly shrinking the history it was part of.
    """

    def arrivals():
        for comment in comments or ():
            if not isinstance(comment, Mapping):
                raise LineageError("the comment history holds an entry that is not a comment")
            require_comment_record(comment, what="the comment history")
            login = comment_author_login(comment)
            if not login or not trusted_author(login):
                continue
            yield from episodes_from_comment_body(comment.get("body") or "")

    return tuple(bounded_arrivals(arrivals()))


def merge_episodes(
    recorded: Sequence[Any] = (),
    incoming: Sequence[Any] = (),
) -> tuple[ContributionEpisode, ...]:
    """Concatenate two sources of raw arrivals, validated and bounded.

    Deliberately no deduplication. Collapsing here would let two independently
    collapsed inputs each arrive under the cap and together exceed it, and the
    owning resolver already collapses exactly once -- in the one place that can
    also see a contradiction at a position and refuse it. Every arrival is
    coerced to an episode here, so a malformed member is the documented
    contract error rather than an incidental attribute failure later.
    """

    def arrivals():
        for source in (recorded, incoming):
            for item in source or ():
                yield require_episode(item)

    return tuple(bounded_arrivals(arrivals()))


@dataclass(frozen=True)
class LineageContext:
    """The trusted exact-head evidence a consumer carries into resolution.

    Every field has to come from something the caller verified for itself: the
    repository it is running in, the head it fetched from the pull request, and
    episodes published by an author it already trusts. An empty context is not
    a failure -- it is the honest statement that this call has no exact-head
    evidence, and resolution falls back to the ordinary identity-only answer,
    which still refuses a configured branch/label disagreement.
    """

    repo: str = ""
    pr_number: Any = 0
    branch: str = ""
    head_sha: str = ""
    episodes: tuple[ContributionEpisode, ...] = field(default_factory=tuple)


#: A consumer that has no exact-head evidence at all.
NO_LINEAGE = LineageContext()


def lineage_context(
    *,
    repo: str,
    pr_number: Any,
    branch: str = "",
    head_sha: str | None = "",
    comments: Sequence[Mapping[str, Any]] | None = (),
    trusted_author: Callable[[str], bool] | None = None,
) -> LineageContext:
    """Assemble exact-head lineage evidence for one consumer entry path.

    A *deliberately* absent target -- nothing supplied at all -- is the
    ordinary no-evidence case and yields :data:`NO_LINEAGE`, which callers
    resolve through the explicit identity-only route. A partially populated or
    malformed target is not absent: it raises, because quietly returning
    :data:`NO_LINEAGE` for it skipped branch binding and decided a diff nobody
    bound. Evidence that arrives with no target to bind it to raises for the
    same reason. Unreadable published evidence propagates as
    :class:`LineageError` for the caller's fail-closed handling.
    """

    episodes: tuple[ContributionEpisode, ...] = ()
    if trusted_author is not None:
        episodes = published_episodes(comments or (), trusted_author=trusted_author)
    supplied = any(_text(value) for value in (repo, branch, head_sha)) or (
        pr_number not in (None, 0, False, "")
    )
    if not supplied:
        if episodes:
            raise LineageError(
                "published builder lineage arrived without a pull request to bind it to"
            )
        return NO_LINEAGE
    target = require_exact_target(
        repo=repo, pr_number=pr_number, branch=branch, head_sha=head_sha
    )
    return LineageContext(
        repo=target.repo,
        pr_number=target.pr_number,
        branch=target.branch,
        head_sha=target.head_sha,
        episodes=episodes,
    )


def resolve_lineage_context(
    context: LineageContext | None,
    *,
    identity: Mapping[str, Any] | None,
    labels: Sequence[str] = (),
    author: str = "",
) -> Lineage:
    """Resolve a carried :class:`LineageContext` through the one decision.

    Only a context that is *exactly* absent takes the explicit identity-only
    route. A partially populated context, or one carrying episodes, is a claim
    about a specific pull request and goes through exact-target resolution,
    which refuses it rather than answering about a diff it never bound.
    """

    if context is None or context == NO_LINEAGE:
        return resolve_configured_identity(
            identity=identity, labels=labels, author=author
        )
    return resolve_builder_lineage(
        identity=identity,
        labels=labels,
        author=author,
        repo=context.repo,
        pr_number=context.pr_number,
        branch=context.branch,
        head_sha=context.head_sha,
        episodes=context.episodes,
    )


# --- pure keys and label planning --------------------------------------------


def pr_key(repo: str, pr_number: Any) -> str:
    """A stable, opaque key for one pull request's private lineage record.

    Pure by design: the recording side lives in a later stage, but both sides
    have to derive the same key from the same pair, and deriving it twice in
    two places is how they stop matching.
    """

    seed = json.dumps([_text(repo).lower(), _pr_number(pr_number)], sort_keys=True)
    return "l" + hashlib.sha256(seed.encode()).hexdigest()[:62]


def builder_label_for(lane: str, identity: Mapping[str, Any] | None = None) -> str:
    """The one active label that names ``lane`` as the current writer."""

    writer = _lane(lane)
    if not writer:
        return ""
    label_map = (
        identity.get("labels") if isinstance(identity, Mapping) else None
    )
    if isinstance(label_map, Mapping):
        for label in sorted(_text(item) for item in label_map):
            if _lane(label_map.get(label)) == writer and label.startswith("builder:"):
                return label
    return f"builder:{writer}"


def builder_label_plan(
    lineage: Lineage,
    *,
    current_labels: Sequence[str] = (),
    identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Plan reconciliation to exactly one active builder label.

    A plan, never an application: this stage computes what would have to change
    and nothing mutates. The label set says who may write next, and after a
    verified takeover that is exactly one lane. Historical contributions are
    not deleted by this -- they live in the recorded lineage, which is what
    reviewer exclusion and the status projection read. Unresolved lineage plans
    no mutation at all; a label moved on a guess is the failure this contract
    exists to stop.
    """

    label_map = identity.get("labels") if isinstance(identity, Mapping) else None
    known = (
        {_text(label): _lane(lane) for label, lane in label_map.items()}
        if isinstance(label_map, Mapping)
        else {}
    )
    present = tuple(
        dict.fromkeys(
            label
            for label in (_text(item) for item in current_labels)
            if label and (label.startswith("builder:") or known.get(label))
        )
    )
    if not lineage.resolved or not lineage.current_writer:
        return {
            "schema": SCHEMA,
            "status": "blocked",
            "reason": lineage.reason if not lineage.resolved else "no_builder_identity",
            "head_sha": lineage.head_sha,
            "current_writer": lineage.current_writer,
            "add": [],
            "remove": [],
            "owner_action": lineage.owner_action or _OWNER_ACTIONS["label_outside_lineage"],
        }

    writer = lineage.current_writer
    target = next(
        (
            label
            for label in present
            if known.get(label) == writer or label == f"builder:{writer}"
        ),
        "",
    ) or builder_label_for(writer, identity)
    remove = [label for label in present if label != target]
    add = [] if target in present else [target]
    return {
        "schema": SCHEMA,
        "status": "reconcile" if (add or remove) else "current",
        "reason": lineage.reason,
        "head_sha": lineage.head_sha,
        "current_writer": writer,
        "add": add,
        "remove": remove,
        "owner_action": "",
    }
