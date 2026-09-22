"""Pure, explicit builder lineage contracts.

All invalid contract inputs raise ContractError. Target and Episode constructors
own canonicalization; their mapping factories use those same constructors.
Repo, SHA, lane, account, label and prefix signals are trimmed/lowercased.
Branch bindings are validated verbatim (including case), never normalized.

Only Chain.from_arrivals validates/deduplicates episode streams. Feed public
parse_markers arrivals and private arrivals together, for example with
itertools.chain, exactly once. History([]) means a successful empty fetch;
unavailable history is not an input to this module. No operation performs I/O.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import json
import re

MAX_EPISODES = 32
MAX_RAW_ARRIVALS = 560
LINEAGE_SCHEMA = "code_mower.builderLineage.v1"
LINEAGE_MARKER = "CODE_MOWER_BUILDER_LINEAGE"
LINEAGE_CONTROL_PREFIX = f"<!-- {LINEAGE_MARKER}"
CONTINUATION_WRITER_STATE = "same_writer"
HANDOFF_WRITER_STATES = frozenset({"terminated", "completed", "cancelled"})
# A creation records the same independently observed writer exit as a handoff,
# because the only writer it can name is the lane that opened the pull request.
CREATION_WRITER_STATES = HANDOFF_WRITER_STATES
FIRST_EPISODE_KINDS = frozenset({"handoff", "creation"})


class ContractError(ValueError):
    """An explicit input violates the lineage contract; no fallback is made."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{name} must be a nonempty string")
    return value.strip().lower()


def _lane(value: object) -> str:
    value = _text(value, "lane")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", value):
        raise ContractError("invalid lane")
    return value


def _account(value: object) -> str:
    value = _text(value, "account")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*(?:\[bot\])?", value):
        raise ContractError("invalid account")
    return value


def _repo(value: object) -> str:
    value = _text(value, "repo")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*/[a-z0-9][a-z0-9_.-]*", value):
        raise ContractError("invalid repo")
    return value


def _sha(value: object) -> str:
    value = _text(value, "SHA")
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ContractError("SHA must contain 40 hex characters")
    return value


def _positive(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ContractError(f"{name} must be a positive non-boolean integer")
    return value


def _branch(value: object) -> str:
    if (not isinstance(value, str) or not 0 < len(value) <= 200
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/_.-]*", value)
            or any(part in value for part in ("..", "//", "@{"))
            or value.endswith("/")
            or any(p.startswith(".") or p.endswith((".", ".lock")) for p in value.split("/"))):
        raise ContractError("invalid exact branch")
    return value


def _mapping(value: object, allowed: set[str]) -> Mapping:
    if not isinstance(value, Mapping) or set(value) - allowed:
        raise ContractError("expected mapping with supported fields only")
    return value


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("duplicate JSON key")
        result[key] = value
    return result


def _json(text: object) -> object:
    if not isinstance(text, str):
        raise ContractError("expected JSON text")
    try:
        return json.loads(text, object_pairs_hook=_unique_pairs,
                          parse_constant=lambda _: _bad_json_constant())
    except (ValueError, RecursionError) as exc:
        raise ContractError("invalid or non-unique JSON") from exc


def _bad_json_constant() -> None:
    raise ContractError("non-finite JSON constant")


@dataclass(frozen=True, slots=True, init=False)
class Target:
    repo: str
    pr_number: int
    branch: str
    head_sha: str

    def __init__(self, repo: object = None, pr_number: object = None,
                 branch: object = None, head_sha: object = None):
        object.__setattr__(self, "repo", _repo(repo))
        object.__setattr__(self, "pr_number", _positive(pr_number, "PR number"))
        object.__setattr__(self, "branch", _branch(branch))
        object.__setattr__(self, "head_sha", _sha(head_sha))

    @classmethod
    def from_mapping(cls, value: object) -> Target:
        return cls(**_mapping(value, {"repo", "pr_number", "branch", "head_sha"}))


_EPISODE_FIELDS = frozenset({"sequence", "repo", "pr_number", "branch", "source_lane",
                             "destination_lane", "expected_head", "resulting_head",
                             "writer_state", "kind"})


@dataclass(frozen=True, slots=True, init=False)
class Episode:
    sequence: int
    repo: str
    pr_number: int
    branch: str
    source_lane: str
    destination_lane: str
    expected_head: str
    resulting_head: str
    writer_state: str
    kind: str

    def __init__(self, sequence: object = None, repo: object = None,
                 pr_number: object = None, branch: object = None,
                 source_lane: object = None, destination_lane: object = None,
                 expected_head: object = None, resulting_head: object = None,
                 writer_state: object = None, kind: object = "handoff"):
        target = Target(repo, pr_number, branch, resulting_head)
        values = dict(sequence=_positive(sequence, "sequence"), repo=target.repo,
                      pr_number=target.pr_number, branch=target.branch,
                      source_lane=_lane(source_lane), destination_lane=_lane(destination_lane),
                      expected_head=_sha(expected_head), resulting_head=target.head_sha,
                      writer_state=_text(writer_state, "writer state"), kind=_text(kind, "kind"))
        if values["kind"] == "handoff":
            if (values["source_lane"] == values["destination_lane"]
                    or values["writer_state"] not in HANDOFF_WRITER_STATES):
                raise ContractError("handoff requires distinct lanes and a verified stopped writer")
        elif values["kind"] == "creation":
            # An issue-targeted lane opens the pull request itself: it is the only
            # contributor, it starts from an immutable base that is not the created
            # head, and only its own observed exit can name it. A creation is the
            # origin of a chain here, never a later episode.
            if (values["sequence"] != 1
                    or values["source_lane"] != values["destination_lane"]
                    or values["expected_head"] == values["resulting_head"]
                    or values["writer_state"] not in CREATION_WRITER_STATES):
                raise ContractError("creation requires the first episode, one lane, "
                                    "an immutable distinct base and a verified stopped writer")
        elif values["kind"] == "continuation":
            if (values["source_lane"] != values["destination_lane"]
                    or values["writer_state"] != CONTINUATION_WRITER_STATE):
                raise ContractError("continuation requires the same writer and lane")
        else:
            raise ContractError("unknown episode kind")
        for key, value in values.items():
            object.__setattr__(self, key, value)

    @classmethod
    def from_mapping(cls, value: object) -> Episode:
        return cls(**_mapping(value, _EPISODE_FIELDS))

    def to_mapping(self) -> dict:
        return {key: getattr(self, key) for key in sorted(_EPISODE_FIELDS)}


def _aliases(value: object, section: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{section} must be a mapping")
    result = {}
    for key, lane in value.items():
        key = _account(key) if section == "authors" else _text(key, section)
        lane = _lane(lane)
        if key in result and result[key] != lane:
            raise ContractError(f"conflicting normalized {section} aliases")
        result[key] = lane
    return tuple(sorted(result.items()))


@dataclass(frozen=True, slots=True, init=False)
class Identity:
    """Canonical identity policy; unknown fields reject rather than disappear.

    Prefixes supply provenance only when require_verified_lineage is true.
    The longest matching prefix wins. An absent branch contract preserves
    identity-only behavior, while all explicit policy fields remain stored.
    """
    enabled: bool
    labels: tuple[tuple[str, str], ...]
    authors: tuple[tuple[str, str], ...]
    branch_prefixes: tuple[tuple[str, str], ...]
    require_verified_lineage: bool

    def __init__(self, value: object):
        value = _mapping(value, {"enabled", "labels", "authors", "branch_prefixes",
                                 "require_verified_lineage"})
        for key in ("enabled", "require_verified_lineage"):
            flag = value.get(key, False)
            if type(flag) is not bool:
                raise ContractError(f"{key} must be boolean")
            object.__setattr__(self, key, flag)
        for key in ("labels", "authors", "branch_prefixes"):
            object.__setattr__(self, key, _aliases(value.get(key, {}), key))

    @classmethod
    def from_mapping(cls, value: object) -> Identity:
        return cls(value)

    @classmethod
    def from_text(cls, value: object) -> Identity:
        return cls(_json(value))

    def to_mapping(self) -> dict:
        return dict(enabled=self.enabled, labels=dict(self.labels), authors=dict(self.authors),
                    branch_prefixes=dict(self.branch_prefixes),
                    require_verified_lineage=self.require_verified_lineage)

    def with_reviewer_floor(self, reviewer: object, accounts: object) -> Identity:
        """Add explicit own-reviewer accounts and builder label; remapping rejects.

        The floor enables exclusion and retains every declared mapping and
        branch policy field. It does not grant marker trust to these accounts.
        """
        lane = _lane(reviewer)
        accounts = Authorities(accounts).accounts
        value = self.to_mapping()
        value["enabled"] = True
        for section, keys in (("authors", accounts), ("labels", (f"builder:{lane}",))):
            for key in keys:
                previous = value[section].get(key)
                if previous is not None and previous != lane:
                    raise ContractError("own-reviewer floor cannot remap a declared identity")
                value[section][key] = lane
        return Identity(value)


@dataclass(frozen=True, slots=True, init=False)
class Authorities:
    """An explicit immutable account set, independent of builder identities."""
    accounts: frozenset[str]

    def __init__(self, accounts: object):
        if not isinstance(accounts, (list, tuple, set, frozenset)):
            raise ContractError("authorities must be an explicit account collection")
        object.__setattr__(self, "accounts", frozenset(_account(a) for a in accounts))


@dataclass(frozen=True, slots=True)
class _Comment:
    body: str
    account: str | None


def _comment(value: object) -> _Comment:
    if not isinstance(value, Mapping):
        raise ContractError("comment must be a mapping")
    body = value.get("body", "")
    if not isinstance(body, str):
        raise ContractError("present comment body must be text")
    accounts = set()
    for field in ("user", "author"):
        if field not in value or value[field] is None:
            continue
        user = value[field]
        if not isinstance(user, Mapping):
            raise ContractError("present comment user/author must be an object or null")
        if "login" in user:
            accounts.add(_account(user["login"]))
    if len(accounts) > 1:
        raise ContractError("conflicting comment authors")
    return _Comment(body, next(iter(accounts), None))


@dataclass(frozen=True, slots=True, init=False)
class History:
    comments: tuple[_Comment, ...]

    def __init__(self, comments: object):
        if not isinstance(comments, list):
            raise ContractError("history must be an explicit list of raw comments")
        object.__setattr__(self, "comments", tuple(_comment(c) for c in comments))

    @classmethod
    def from_pages(cls, pages: object) -> History:
        if not isinstance(pages, list) or any(not isinstance(p, list) for p in pages):
            raise ContractError("slurped history must be a list of list pages")
        return cls([comment for page in pages for comment in page])


@dataclass(frozen=True, slots=True, init=False)
class Chain:
    """A validated Target-bound chain. Use from_arrivals, including for [].

    At most 560 arrivals are consumed, plus the first disallowed probe. Budget
    precedes canonicalization and deduplication. At most 32 episodes are kept.
    Sequence order is canonical, regardless of replay or arrival order.
    """
    target: Target
    episodes: tuple[Episode, ...]
    raw_arrival_count: int

    def __init__(self, *args, **kwargs):
        raise ContractError("use Chain.from_arrivals(target, raw_arrivals)")

    @classmethod
    def from_arrivals(cls, target: Target, arrivals: Iterable[Episode | Mapping]) -> Chain:
        if not isinstance(target, Target):
            raise ContractError("Chain requires an exact Target")
        if isinstance(arrivals, (str, bytes, Mapping, Chain)):
            raise ContractError("arrivals must be an episode iterable")
        try:
            iterator = iter(arrivals)
        except TypeError as exc:
            raise ContractError("arrivals must be an episode iterable") from exc
        episodes: dict[int, Episode] = {}
        count = 0
        for raw in iterator:
            count += 1
            if count > MAX_RAW_ARRIVALS:
                raise ContractError("raw episode arrival budget exceeded")
            episode = raw if isinstance(raw, Episode) else Episode.from_mapping(raw)
            if (episode.repo, episode.pr_number, episode.branch) != (
                    target.repo, target.pr_number, target.branch):
                raise ContractError("episode does not match the exact Target")
            previous = episodes.get(episode.sequence)
            if previous is not None and previous != episode:
                raise ContractError("conflicting duplicate episode")
            episodes[episode.sequence] = episode
            if len(episodes) > MAX_EPISODES:
                raise ContractError("distinct episode budget exceeded")
        ordered = tuple(episodes[key] for key in sorted(episodes))
        for index, episode in enumerate(ordered, 1):
            if episode.sequence != index:
                raise ContractError("episode sequences must be contiguous from 1")
            if index == 1:
                if episode.kind not in FIRST_EPISODE_KINDS:
                    raise ContractError("first episode must be a handoff or a creation")
            else:
                previous = ordered[index - 2]
                if (episode.expected_head, episode.source_lane) != (
                        previous.resulting_head, previous.destination_lane):
                    raise ContractError("episode head/lane continuity mismatch")
        chain = object.__new__(cls)
        object.__setattr__(chain, "target", target)
        object.__setattr__(chain, "episodes", ordered)
        object.__setattr__(chain, "raw_arrival_count", count)
        return chain


def parse_markers(history: History, authorities: Authorities) -> Iterable[Mapping]:
    """Yield raw trusted arrivals for a single subsequent Chain factory.

    Trust is checked only after History validates every transport record. This
    lazy stream never deduplicates, judges current heads, or resets a budget.
    Consume it with Chain.from_arrivals; do not materialize unbounded histories.
    An announced marker must be one complete, unique-key, nonempty JSON chain.
    """
    if not isinstance(history, History) or not isinstance(authorities, Authorities):
        raise ContractError("parsing requires History and Authorities")
    return _marker_arrivals(history, authorities)


def _marker_arrivals(history: History, authorities: Authorities) -> Iterable[Mapping]:
    for comment in history.comments:
        if comment.account not in authorities.accounts:
            continue
        controls = lineage_control_comments(comment.body)
        if not controls:
            continue
        if len(controls) != 1:
            raise ContractError("multiple announced lineage markers")
        match = re.fullmatch(
            re.escape(f"<!-- {LINEAGE_MARKER}: ") + r"(.+) -->",
            controls[0],
        )
        if match is None:
            raise ContractError("malformed or unterminated lineage marker")
        payload = _mapping(_json(match.group(1)), {"schema", "episodes"})
        if payload.get("schema") != LINEAGE_SCHEMA:
            raise ContractError("unsupported lineage schema")
        episodes = payload.get("episodes")
        if not isinstance(episodes, list) or not episodes:
            raise ContractError("marker must contain a nonempty episode list")
        # Never slice a snapshot. Even repeated arrivals count at the Chain.
        yield from episodes


def lineage_control_comments(body: object) -> tuple[str, ...]:
    """Return exact standalone lineage HTML controls outside Markdown fences.

    The marker name is public documentation as well as a reserved control name.
    Ordinary prose, inline code and fenced examples therefore cannot announce
    lineage.  A line beginning with the exact reserved HTML prefix is control
    data; once announced by a trusted authority it must parse completely or the
    caller fails closed.
    """
    if not isinstance(body, str):
        return ()
    controls = []
    fence_character = ""
    fence_length = 0
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if fence_character:
            if re.fullmatch(re.escape(fence_character) + "{" + str(fence_length) + ",}", line):
                fence_character, fence_length = "", 0
            continue
        fence = re.match(r"^(`{3,}|~{3,})", line)
        if fence:
            token = fence.group(1)
            fence_character, fence_length = token[0], len(token)
            continue
        if line.startswith(LINEAGE_CONTROL_PREFIX):
            controls.append(line)
    return tuple(controls)


def render(chain: Chain) -> str:
    """Render all episodes of a validated nonempty Chain, without truncation."""
    if not isinstance(chain, Chain) or not chain.episodes:
        raise ContractError("render requires a validated nonempty Chain")
    payload = dict(schema=LINEAGE_SCHEMA, episodes=[e.to_mapping() for e in chain.episodes])
    return f"<!-- {LINEAGE_MARKER}: {json.dumps(payload, sort_keys=True, separators=(',', ':'))} -->"


@dataclass(frozen=True, slots=True, init=False)
class Lineage:
    """Resolved decision; only resolve/resolve_identity_only construct decisions.

    ready admits unrelated reviewers; waiting and conflict never admit anyone.
    A target of None identifies the separate, explicit identity-only decision.
    """
    target: Target | None
    contributors: tuple[str, ...]
    current_writer: str | None
    status: str
    reason: str
    owner_action: str

    def __init__(self, *args, **kwargs):
        raise ContractError("Lineage decisions must be resolved")


def _decision(target: Target | None, contributors: Iterable[str], writer: str | None,
              status: str, reason: str, action: str = "") -> Lineage:
    decision = object.__new__(Lineage)
    for key, value in dict(target=target, contributors=tuple(sorted(set(contributors))),
                           current_writer=writer, status=status, reason=reason,
                           owner_action=action).items():
        object.__setattr__(decision, key, value)
    return decision


def _signals(identity: Identity, author: object, labels: object, branch: object) -> tuple[set, str | None]:
    if not isinstance(identity, Identity):
        raise ContractError("resolution requires Identity")
    # Empty author explicitly means no known author; malformed types still fail.
    account = None if author == "" else _account(author)
    if not isinstance(labels, (list, tuple, set, frozenset)):
        raise ContractError("labels must be an explicit collection")
    label_keys = tuple(_text(label, "label") for label in labels)
    branch = _branch(branch)
    if not identity.enabled:
        return set(), None
    label_map, author_map = dict(identity.labels), dict(identity.authors)
    lanes = {label_map[label] for label in label_keys if label in label_map}
    if account in author_map:
        lanes.add(author_map[account])
    prefixes = [(prefix, lane) for prefix, lane in identity.branch_prefixes
                if branch.lower().startswith(prefix)] if identity.require_verified_lineage else []
    branch_lane = max(prefixes, key=lambda item: len(item[0]))[1] if prefixes else None
    return lanes, branch_lane


def _identity_decision(target: Target | None, identity: Identity, author: object,
                       labels: object, branch: object) -> Lineage:
    lanes, branch_lane = _signals(identity, author, labels, branch)
    if branch_lane:
        lanes.add(branch_lane)
    if len(lanes) > 1:
        return _decision(target, lanes, None, "conflict", "identity_branch_conflict",
                         "Provide verified recorded lineage or correct identity metadata.")
    writer = next(iter(lanes), None)
    return _decision(target, lanes, writer, "ready", "identity_matched" if writer else "no_identity")


def resolve_identity_only(identity: Identity, author: object, labels: object, branch: object) -> Lineage:
    """Explicit identity-only control; accepts no Target, History or evidence."""
    return _identity_decision(None, identity, author, labels, branch)


def resolve(chain: Chain, identity: Identity, author: object, labels: object) -> Lineage:
    """Resolve against the Chain's own exact Target; stale final heads wait."""
    if not isinstance(chain, Chain):
        raise ContractError("exact resolution requires a validated Chain")
    if not chain.episodes:
        return _identity_decision(chain.target, identity, author, labels, chain.target.branch)
    lanes, branch_lane = _signals(identity, author, labels, chain.target.branch)
    contributors = {lane for e in chain.episodes for lane in (e.source_lane, e.destination_lane)}
    if branch_lane:
        lanes.add(branch_lane)
    writer = chain.episodes[-1].destination_lane
    if lanes - contributors:
        return _decision(chain.target, contributors | lanes, writer, "conflict",
                         "unrecorded_contributor", "Provide verified lineage for every contributor.")
    if chain.episodes[-1].resulting_head != chain.target.head_sha:
        return _decision(chain.target, contributors, writer, "waiting", "lineage_head_pending",
                         "Wait for verified lineage at the current PR head.")
    return _decision(chain.target, contributors, writer, "ready", "verified_lineage")


def admit(lineage: Lineage, reviewer: object) -> bool:
    """Only a ready full decision permits a reviewer outside all contributors."""
    lane = _lane(reviewer)
    if not isinstance(lineage, Lineage):
        raise ContractError("admission requires a resolved Lineage decision")
    return lineage.status == "ready" and lane not in lineage.contributors
