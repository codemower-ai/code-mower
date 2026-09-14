"""Exact-head builder contribution lineage after a verified handoff.

A pull request can be built by more than one Code Mower builder lane. The
opener, the branch prefix and the single active ``builder:*`` label each
describe at most one of those lanes, so any of them alone will misdescribe a
PR that changed hands. This module keeps the ordered contribution history
instead, and derives the one current writer from it.

Trust rules this module exists to enforce:

* A contribution episode is evidence produced by the verified handoff and
  delivery path (see :mod:`code_mower.lane_handoff`), which observed the source
  writer going quiescent and observed both heads. A caller-supplied boolean, a
  PR body marker, a commit trailer, the PR opener or the most recent label are
  none of them able to attest that a takeover happened.
* Episodes are bound to repository, pull request, branch, source lane,
  destination lane, expected head and resulting head. An episode that does not
  bind to the pull request under decision is not evidence about it.
* Resolution is exact-head. Lineage that stops short of the current head is
  *waiting*, never a guess about who wrote the current diff.
* Conflicting, duplicated, unchained or unbound evidence fails closed with one
  concise owner action rather than picking a winner.

Everything here is metadata-only: lane names, a repository slug, a PR number, a
branch name and commit shas. No source, diffs, prompts, transcripts, paths,
provider references or credentials pass through this module, so a resolved
lineage is safe for the existing public/cloud metadata contract.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
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
#: :data:`LINEAGE_MARKER_RE` only matches a complete, object-shaped, properly
#: terminated marker. Looking for published evidence with it alone means an
#: unterminated or non-object marker is not read as broken -- it is not seen,
#: and a trusted comment that announces lineage reports none. Absence and
#: unreadability are opposite answers: one admits an independent reviewer on
#: the ordinary single-builder story, the other must stop. Presence is found
#: first, and the payload is then required to parse.
LINEAGE_MARKER_PRESENT_RE = re.compile(
    r"<!--\s*" + LINEAGE_MARKER + r"(?![0-9A-Z_])"
)

#: How much of one comment body a published marker is parsed out of.
MAX_MARKER_BODY_CHARS = 2048 * 32

LANE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}\Z")
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
REPO_RE = re.compile(r"[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}\Z")
BRANCH_RE = re.compile(r"[A-Za-z0-9._/-]{1,200}\Z")

#: Verified writer states the handoff boundary is allowed to report for a
#: takeover. Anything else (including a missing or "unknown" state) is
#: uncertainty.
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

#: How many raw entries a caller may hand the resolver before it refuses to
#: parse them. A lineage is at most :data:`MAX_EPISODES` distinct episodes, but
#: the same chain legitimately arrives many times over, and the bound has to be
#: the one the supported publication contract actually produces rather than a
#: round multiple.
#:
#: Evidence is published as a *cumulative* snapshot: after episode ``n`` the
#: producer republishes all ``n``. A lineage that runs to its full length is
#: therefore delivered as ``1 + 2 + ... + MAX_EPISODES`` entries, and a reader
#: that also holds the private record sees the completed chain once more on top
#: of that. Anything under that total would refuse a lineage the system is
#: documented to support -- and would refuse it *before* deduplication, which
#: is the only step that could have shown the arrivals to be one chain.
#:
#: So arrivals are bounded here, deduplication happens as they are walked, and
#: the lineage bound applies to the distinct episodes that survive: an episode
#: sequence outside ``1..MAX_EPISODES`` is rejected per entry, so working state
#: stays bounded by the lineage length however many times it repeats. Entries
#: that merely repeat are collapsed; entries that disagree still fail closed.
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


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _lane(value: Any) -> str:
    lane = _text(value).lower()
    return lane if LANE_RE.match(lane) else ""


def _sha(value: Any) -> str:
    sha = _text(value).lower()
    return sha if SHA_RE.match(sha) else ""


def _pr_number(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if 0 < number <= 2**31 - 1 else 0


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
            or _pr_number(self.pr_number) != self.pr_number
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
    repo = _text(payload.get("repo"))
    return ContributionEpisode(
        sequence=payload.get("sequence"),  # type: ignore[arg-type]
        kind=_text(payload.get("kind")).lower(),
        repo=repo,
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

    ``handoff`` is the ``Handoff`` record (or its ``as_dict``) that
    :mod:`code_mower.lane_handoff` accepted, so repository, PR, branch, lanes
    and expected head all come from evidence the handoff boundary verified.
    ``resulting_head`` is the delivery-side attestation of what the destination
    lane actually produced; it is deliberately not part of the handoff record,
    which is written before the destination lane has written anything.
    """

    record = handoff if isinstance(handoff, Mapping) else handoff.as_dict()
    target_pr = _text(record.get("target_pr"))
    if "#" not in target_pr:
        raise LineageError("contribution episode is malformed")
    pr_repo, _, pr_number = target_pr.partition("#")
    if repo and _text(repo).lower() != pr_repo.lower():
        raise LineageError("contribution episode does not bind to the repository under work")
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
            "owner_action": "" if admitted else (self.owner_action or _ADMISSION_ACTIONS[reason]),
        }

    def independent_lanes(self, lanes: Iterable[str]) -> tuple[str, ...]:
        return tuple(lane for lane in lanes if self.independent(lane))

    def as_dict(self) -> dict[str, Any]:
        """Bounded metadata for Board/status projection and public rendering."""

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

    ``episodes`` is verified handoff/delivery evidence. ``opener_lane`` and
    ``label_lanes`` are the weak signals the rest of the system used to carry
    on their own: they are accepted here only as corroboration, and they can
    fail the resolution closed, but neither can establish a takeover.
    """

    target_repo = _text(repo)
    number = _pr_number(pr_number)
    head = _sha(head_sha)
    target_branch = _text(branch)
    if not REPO_RE.match(target_repo) or not number or not head:
        return _lineage("conflict", "target_invalid", head_sha=head)

    opener = _lane(opener_lane)
    labels = tuple(dict.fromkeys(lane for lane in (_lane(item) for item in label_lanes) if lane))

    # Bounded input, not a bounded lineage. Arrivals are counted as they are
    # walked and collapsed on the way, so a conforming cumulative publication
    # history -- the same chain republished after every round, optionally
    # overlapping a private record of it -- is deduplicated into one lineage
    # instead of being refused for its length. Working state never exceeds the
    # lineage bound, because an episode whose sequence falls outside
    # ``1..MAX_EPISODES`` is rejected as it arrives.
    seen: dict[int, ContributionEpisode] = {}
    arrivals = 0
    for item in episodes:
        arrivals += 1
        if arrivals > MAX_EPISODE_ARRIVALS:
            return _lineage("conflict", "episode_malformed", head_sha=head)
        try:
            episode = item if isinstance(item, ContributionEpisode) else episode_from_mapping(item)
        except LineageError:
            return _lineage("conflict", "episode_malformed", head_sha=head)
        if (
            episode.repo.lower() != target_repo.lower()
            or episode.pr_number != number
            or (target_branch and episode.branch != target_branch)
        ):
            return _lineage("conflict", "episode_unbound", head_sha=head)
        expected_state = (
            {CONTINUATION_WRITER_STATE}
            if episode.kind == CONTINUATION_KIND
            else HANDOFF_WRITER_STATES
        )
        if episode.writer_state not in expected_state:
            return _lineage("conflict", "writer_state_unverified", head_sha=head)
        previous = seen.get(episode.sequence)
        if previous is not None:
            if previous.as_dict() != episode.as_dict():
                # Two records claim the same position and disagree. Which one
                # describes the diff is exactly what cannot be guessed.
                return _lineage("conflict", "episode_duplicated", head_sha=head)
            continue
        seen[episode.sequence] = episode

    if not seen:
        return resolve_identity_only(
            opener_lane=opener,
            label_lanes=labels,
            head_sha=head,
            branch_lane=branch_lane,
        )

    ordered = [seen[sequence] for sequence in sorted(seen)]
    if [episode.sequence for episode in ordered] != list(range(1, len(ordered) + 1)):
        return _lineage("conflict", "episode_unchained", head_sha=head)

    if ordered[0].kind != HANDOFF_KIND:
        # Lineage begins when the pen moves. A continuation with nothing to
        # continue describes an ordinary single-builder round, which needs no
        # episode at all, so recorded evidence in that shape is not trustworthy.
        return _lineage("conflict", "episode_unchained", head_sha=head)

    contributors: list[str] = [ordered[0].source_lane]
    for index, episode in enumerate(ordered):
        if index and (
            episode.expected_head != ordered[index - 1].resulting_head
            or episode.source_lane != ordered[index - 1].destination_lane
        ):
            return _lineage("conflict", "episode_unchained", head_sha=head)
        if episode.moved_head:
            contributors.append(episode.destination_lane)
    contributors = list(dict.fromkeys(contributors))
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
            "conflict", "opener_outside_lineage", head_sha=head,
            evidence="handoff_episodes", episodes=len(ordered),
        )
    known = set(contributors) | {writer}
    if any(lane not in known for lane in labels):
        return _lineage(
            "conflict", "label_outside_lineage", head_sha=head,
            evidence="handoff_episodes", episodes=len(ordered),
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
    """The ordinary single-builder case, and the #959 shape without evidence.

    With no verified handoff there is exactly one consistent story available:
    one lane opened the PR and still holds the only builder label. Any other
    combination is the inconsistency this issue exists to stop guessing about,
    so it fails closed instead of preferring the opener or the newest label.

    ``branch_lane`` is the deployment's *configured* branch identity, when it
    configured one. It is a signal of the same weight as the label and the
    opener, so it joins them as a candidate: a `codex/` branch carrying a
    `builder:claude` label is two lanes disagreeing about who wrote the diff,
    and answering "Claude" would admit Codex to review its own work. It can
    never establish a takeover on its own -- only a recorded episode does that
    -- it can only refuse to pick a winner.
    """

    head = _sha(head_sha)
    opener = _lane(opener_lane)
    branch = _lane(branch_lane)
    labels = tuple(dict.fromkeys(lane for lane in (_lane(item) for item in label_lanes) if lane))
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


def lanes_from_identity(
    *,
    identity: Mapping[str, Any] | None,
    labels: Sequence[str] = (),
    author: str = "",
) -> tuple[str, tuple[str, ...]]:
    """Map GitHub labels and a PR author onto builder lane names.

    ``identity`` is the existing author-exclusion contract
    (``{"enabled": bool, "labels": {...}, "authors": {...}}``) so lane naming
    stays in one place. Returns ``(opener_lane, label_lanes)``; both are weak
    signals that :func:`resolve_lineage` may reject.
    """

    if not isinstance(identity, Mapping) or not identity.get("enabled"):
        return "", ()
    label_map = identity.get("labels")
    author_map = identity.get("authors")
    label_map = label_map if isinstance(label_map, Mapping) else {}
    author_map = author_map if isinstance(author_map, Mapping) else {}
    label_lanes = tuple(
        dict.fromkeys(
            lane
            for lane in (_lane(label_map.get(_text(label))) for label in labels)
            if lane
        )
    )
    lowered = {_text(key).lower(): value for key, value in author_map.items()}
    return _lane(lowered.get(_text(author).lower())), label_lanes


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
        _require_comment_record(item, what=what)
    return tuple(value)


def _require_comment_record(comment: Mapping[str, Any], *, what: str) -> None:
    """The two fields lineage actually reads must be readable, or nothing is.

    Being a dict is not being a comment. The marker lives in ``body`` and trust
    is decided from ``user.login``, so a present ``body`` that is a number, or
    a ``user`` that is a string, or a ``login`` that is an object, is a record
    whose meaning cannot be recovered. Coercing those with ``str()`` invents an
    author or a body that GitHub never sent; skipping them drops the record.
    Either way an unreadable history becomes an absent one -- and absence is
    what admits a reviewer.

    GitHub's own schema is the boundary, not a stricter invention of one: a
    comment from a deleted account really does carry ``"user": null``, and
    ``body`` really is optional on some representations. Both are accepted and
    simply name no author and no marker.
    """

    body = comment.get("body")
    if body is not None and not isinstance(body, str):
        raise LineageError(f"{what} contains a comment whose body is not text")
    user = comment.get("user")
    if user is None:
        return
    if not isinstance(user, Mapping):
        raise LineageError(f"{what} contains a comment whose author is not an object")
    login = user.get("login")
    if login is not None and not isinstance(login, str):
        raise LineageError(f"{what} contains a comment whose author login is not text")


def branch_lane_from_identity(
    *, identity: Mapping[str, Any] | None, branch: str = ""
) -> str:
    """The lane the deployment's configured branch prefixes name, if any.

    ``branch_prefixes`` and ``require_verified_lineage`` are rendered into the
    identity contract for exactly this question, and were being rendered and
    then ignored. They are read only together: a deployment that did not ask
    for verified lineage keeps the ordinary opener/label answer it has always
    had, and one that did gets its branch identity counted as the signal it
    configured it to be.
    """

    if not isinstance(identity, Mapping) or not identity.get("enabled"):
        return ""
    if not identity.get("require_verified_lineage"):
        return ""
    prefixes = identity.get("branch_prefixes")
    if not isinstance(prefixes, Mapping):
        return ""
    name = _text(branch).lower()
    if not name:
        return ""
    # Longest prefix first, so `codex-review/` cannot be shadowed by `codex-`.
    for prefix in sorted((_text(key) for key in prefixes), key=len, reverse=True):
        if prefix and name.startswith(prefix.lower()):
            return _lane(prefixes.get(prefix))
    return ""


# --- durable metadata-only record -------------------------------------------


def _store(root: Path):
    """Import the private context store lazily.

    The resolver above is pure and is copied into environments (the generated
    gate, mirrored tooling) that have no private state directory at all. Only
    the recording side needs the store, so only it pays for the dependency.
    """

    from .context_store import ContextStore

    return ContextStore(Path(root))


def pr_key(repo: str, pr_number: Any) -> str:
    seed = json.dumps([_text(repo).lower(), _pr_number(pr_number)], sort_keys=True)
    return "l" + hashlib.sha256(seed.encode()).hexdigest()[:62]


def record_episode(root: Path, episode: ContributionEpisode) -> dict[str, Any]:
    """Append one verified episode. Replay is a no-op, never a duplicate.

    Recording is refused when the new episode does not chain onto the recorded
    lineage, so a mismatched or reordered write is an owner action at the point
    it is attempted rather than an ambiguity discovered at review time.
    """

    if not isinstance(episode, ContributionEpisode):
        raise LineageError("contribution episode is malformed")
    with _store(root).locked(pr_key(episode.repo, episode.pr_number)) as locked:
        record = locked.read() or {
            "schema": RECORD_SCHEMA,
            "repo": episode.repo,
            "pr_number": episode.pr_number,
            "episodes": [],
        }
        if record.get("schema") != RECORD_SCHEMA:
            raise LineageError("recorded builder lineage is unreadable")
        recorded = list(record.get("episodes") or [])
        payload = episode.as_dict()
        for existing in recorded:
            if existing.get("sequence") == episode.sequence:
                if existing != payload:
                    raise LineageError(
                        "a different contribution episode is already recorded at this position"
                    )
                return {"recorded": False, "duplicate": True, "episodes": len(recorded)}
        if len(recorded) >= MAX_EPISODES:
            raise LineageError("recorded builder lineage is already at its bound")
        if episode.sequence != len(recorded) + 1:
            raise LineageError("contribution episode is out of order for this pull request")
        if recorded:
            previous = recorded[-1]
            if (
                previous.get("resulting_head") != episode.expected_head
                or previous.get("destination_lane") != episode.source_lane
                or previous.get("branch") != episode.branch
            ):
                raise LineageError(
                    "contribution episode does not chain onto the recorded lineage"
                )
        recorded.append(payload)
        record["episodes"] = recorded
        locked.write(record)
        return {"recorded": True, "duplicate": False, "episodes": len(recorded)}


def load_episodes(root: Path, repo: str, pr_number: Any) -> tuple[ContributionEpisode, ...]:
    """Read back recorded episodes. Unreadable evidence raises, never guesses."""

    path = Path(root)
    if not path.exists():
        return ()
    with _store(path).locked(pr_key(repo, pr_number)) as locked:
        record = locked.read()
    if record is None:
        return ()
    if record.get("schema") != RECORD_SCHEMA:
        raise LineageError("recorded builder lineage is unreadable")
    return tuple(
        episode_from_mapping(item) for item in (record.get("episodes") or [])
    )


# --- active builder label reconciliation -------------------------------------


def builder_label_for(lane: str, identity: Mapping[str, Any] | None = None) -> str:
    """The one active label that names ``lane`` as the current writer."""

    writer = _lane(lane)
    if not writer:
        return ""
    label_map = (identity or {}).get("labels") if isinstance(identity, Mapping) else None
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

    The label set says who may write next, and after a verified takeover that
    is exactly one lane. Historical contributions are not deleted by this: they
    live in the recorded lineage, which is what reviewer exclusion and the
    Board projection read. Unresolved lineage plans no mutation at all -- a
    label moved on a guess is the failure this issue exists to stop.
    """

    label_map = (identity or {}).get("labels") if isinstance(identity, Mapping) else None
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


def reconcile_active_builder_label(
    *,
    repo: str,
    pr_number: Any,
    branch: str,
    head_sha: str,
    current_labels: Sequence[str] = (),
    episodes: Sequence[Mapping[str, Any] | ContributionEpisode] = (),
    identity: Mapping[str, Any] | None = None,
    opener_lane: str = "",
    observe_head: Callable[[], str] | None = None,
    apply_labels: Callable[[Sequence[str], Sequence[str]], None] | None = None,
) -> dict[str, Any]:
    """Move the active builder label to the verified current writer.

    The head is rechecked on both sides of the mutation. A head that moved
    before the resolution makes the lineage describe a different diff, and a
    head that moved after it makes the label this call just applied a claim
    about a diff nobody verified; both report ``blocked`` with one owner action
    rather than leaving a confident but unfounded label behind.
    """

    pinned = _sha(head_sha)
    if not pinned:
        return {
            "schema": SCHEMA,
            "status": "blocked",
            "reason": "target_invalid",
            "head_sha": "",
            "current_writer": "",
            "add": [],
            "remove": [],
            "applied": False,
            "owner_action": _OWNER_ACTIONS["target_invalid"],
        }
    if observe_head is not None and _sha(observe_head()) != pinned:
        return {
            "schema": SCHEMA,
            "status": "blocked",
            "reason": "head_moved_before_reconcile",
            "head_sha": pinned,
            "current_writer": "",
            "add": [],
            "remove": [],
            "applied": False,
            "owner_action": (
                "the pull request head moved while reconciling the active builder "
                "label; re-run reconciliation against the current head"
            ),
        }

    label_lanes = tuple(
        dict.fromkeys(
            lane
            for lane in (
                _lane(((identity or {}).get("labels") or {}).get(_text(label)))
                if isinstance(identity, Mapping)
                else ""
                for label in current_labels
            )
            if lane
        )
    )
    lineage = resolve_lineage(
        repo=repo,
        pr_number=pr_number,
        branch=branch,
        head_sha=pinned,
        episodes=episodes,
        opener_lane=opener_lane,
        label_lanes=label_lanes,
    )
    plan = builder_label_plan(lineage, current_labels=current_labels, identity=identity)
    plan["applied"] = False
    if plan["status"] != "reconcile":
        return plan
    if apply_labels is not None:
        apply_labels(tuple(plan["add"]), tuple(plan["remove"]))
        plan["applied"] = True
    if observe_head is not None and _sha(observe_head()) != pinned:
        plan["status"] = "blocked"
        plan["reason"] = "head_moved_during_reconcile"
        plan["owner_action"] = (
            "the pull request head moved while the active builder label was being "
            "reconciled; re-record the contribution episode and reconcile again"
        )
    return plan


# --- bounded public transport ------------------------------------------------


def lineage_comment_marker(episodes: Sequence[ContributionEpisode]) -> str:
    """Render episodes as one hidden, metadata-only pull request marker."""

    payload = {
        "schema": SCHEMA,
        "episodes": [episode.as_dict() for episode in episodes][:MAX_EPISODES],
    }
    return f"<!-- {LINEAGE_MARKER} {json.dumps(payload, sort_keys=True, separators=(',', ':'))} -->"


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
    return tuple(episode_from_mapping(item) for item in items)
