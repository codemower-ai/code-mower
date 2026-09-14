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

LANE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}\Z")
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
REPO_RE = re.compile(r"[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}\Z")
BRANCH_RE = re.compile(r"[A-Za-z0-9._/-]{1,200}\Z")

#: Verified writer states the handoff boundary is allowed to report. Anything
#: else (including a missing or "unknown" state) is uncertainty.
WRITER_STATES = frozenset({"suspended", "terminated"})

#: A lineage longer than this is treated as malformed rather than walked.
MAX_EPISODES = 32

EPISODE_FIELDS = (
    "schema",
    "sequence",
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

    def __post_init__(self) -> None:
        if (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or not 1 <= self.sequence <= MAX_EPISODES
            or not REPO_RE.match(_text(self.repo))
            or _pr_number(self.pr_number) != self.pr_number
            or not BRANCH_RE.match(_text(self.branch))
            or not LANE_RE.match(_text(self.source_lane))
            or not LANE_RE.match(_text(self.destination_lane))
            or self.source_lane == self.destination_lane
            or not SHA_RE.match(_text(self.expected_head))
            or not SHA_RE.match(_text(self.resulting_head))
            or self.writer_state not in WRITER_STATES
        ):
            raise LineageError("contribution episode is malformed")

    @property
    def moved_head(self) -> bool:
        return self.expected_head != self.resulting_head

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": EPISODE_SCHEMA,
            "sequence": self.sequence,
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
        repo=pr_repo,
        pr_number=_pr_number(pr_number),
        branch=_text(record.get("target_branch")),
        source_lane=_lane(record.get("source_lane")),
        destination_lane=_lane(record.get("destination_lane")),
        expected_head=_sha(record.get("expected_head")),
        resulting_head=_sha(resulting_head),
        writer_state=_text(writer_state).lower(),
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

    parsed: list[ContributionEpisode] = []
    for item in episodes:
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
        if episode.writer_state not in WRITER_STATES:
            return _lineage("conflict", "writer_state_unverified", head_sha=head)
        parsed.append(episode)
    if len(parsed) > MAX_EPISODES:
        return _lineage("conflict", "episode_malformed", head_sha=head)

    if not parsed:
        return resolve_identity_only(opener_lane=opener, label_lanes=labels, head_sha=head)

    ordered = sorted(parsed, key=lambda episode: episode.sequence)
    seen: dict[int, ContributionEpisode] = {}
    for episode in ordered:
        previous = seen.get(episode.sequence)
        if previous is not None:
            if previous.as_dict() != episode.as_dict():
                return _lineage("conflict", "episode_duplicated", head_sha=head)
            continue
        seen[episode.sequence] = episode
    ordered = [seen[sequence] for sequence in sorted(seen)]
    if [episode.sequence for episode in ordered] != list(range(1, len(ordered) + 1)):
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
    *, opener_lane: str = "", label_lanes: Sequence[str] = (), head_sha: str = ""
) -> Lineage:
    """The ordinary single-builder case, and the #959 shape without evidence.

    With no verified handoff there is exactly one consistent story available:
    one lane opened the PR and still holds the only builder label. Any other
    combination is the inconsistency this issue exists to stop guessing about,
    so it fails closed instead of preferring the opener or the newest label.
    """

    head = _sha(head_sha)
    opener = _lane(opener_lane)
    labels = tuple(dict.fromkeys(lane for lane in (_lane(item) for item in label_lanes) if lane))
    candidates = tuple(dict.fromkeys(([opener] if opener else []) + list(labels)))
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


def episodes_from_comment_body(body: str) -> tuple[ContributionEpisode, ...]:
    """Parse lineage markers out of one already trusted comment body.

    The caller decides trust. This function never treats the presence of a
    marker as evidence that its author was allowed to publish one.
    """

    episodes: list[ContributionEpisode] = []
    for match in LINEAGE_MARKER_RE.finditer(_text(body)[:MAX_EPISODES * 2048]):
        try:
            payload = json.loads(match.group("payload"))
        except (ValueError, RecursionError):
            raise LineageError("published builder lineage is unreadable") from None
        if not isinstance(payload, Mapping) or payload.get("schema") != SCHEMA:
            raise LineageError("published builder lineage schema is unsupported")
        items = payload.get("episodes")
        if not isinstance(items, list) or len(items) > MAX_EPISODES:
            raise LineageError("published builder lineage is unreadable")
        episodes.extend(episode_from_mapping(item) for item in items)
    return tuple(episodes)
