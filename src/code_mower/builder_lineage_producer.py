"""Explicit, staged lineage producers. No default CLI or runner calls this module.

The broker owns policy, transport bindings, private stores and I/O adapters.
Provider text is never authority. Compatibility is refusal, not migration.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import chain
from pathlib import Path
import re
import subprocess

from .builder_lineage import (
    Authorities, Chain, ContractError, Episode, FIRST_EPISODE_KINDS, History, Identity,
    LINEAGE_MARKER, Target, parse_markers, render, resolve,
)
from .context_store import ContextStore, strict_json


class ProducerRefusal(ContractError):
    """A bounded owner action, including any effects already attempted."""

    def __init__(self, reason, *, comment_posted=False, labels_attempted=False):
        super().__init__(reason)
        self.comment_posted = comment_posted
        self.labels_attempted = labels_attempted


def _supported(value, kind):
    """The single compatibility boundary; unsupported history must be re-recorded."""
    legacy = False
    if kind == "history":
        legacy = any(LINEAGE_MARKER in c.body and not re.search(
            LINEAGE_MARKER + r":", c.body) for c in value.comments)
    elif kind == "episode":
        legacy = isinstance(value, dict) and (
            "schema" in value or value.get("writer_state") == "self_quiescent")
    elif kind == "record":
        legacy = not isinstance(value, dict) or value.get("schema") != "code_mower.lineageProducer.v1"
    if legacy:
        raise ProducerRefusal("Unsupported lineage history; obtain verified re-recording.")
    return value


def _arrivals(public, private, authorities):
    _supported(public, "history")
    for raw in chain(parse_markers(public, authorities), private):
        yield _supported(raw, "episode")


@dataclass(frozen=True)
class Observation:
    chain: Chain
    decision: object


def observe(target, identity, authorities, history, private, *, author, labels):
    """Consume all raw arrivals in one budget, then resolve exactly once."""
    if not isinstance(target, Target) or not isinstance(identity, Identity):
        raise ProducerRefusal("Exact target and explicit identity policy required.")
    if not isinstance(authorities, Authorities) or not authorities.accounts:
        raise ProducerRefusal("Explicit publication authority required.")
    if not isinstance(history, History):
        raise ProducerRefusal("Readable authenticated history required.")
    bound = Chain.from_arrivals(target, _arrivals(history, private, authorities))
    decision = resolve(bound, identity, author, labels)
    if decision.status != "ready":
        raise ProducerRefusal("Lineage is not ready: " + decision.reason)
    return Observation(bound, decision)


def selected_history(payload, *, selected):
    """An explicitly selected raw list; REST's numeric comments count is unrelated."""
    if not isinstance(payload, dict):
        raise ProducerRefusal("PR metadata must be an object.")
    return History(selected)


def decode_transport(raw):
    """Strict JSON without normalizing null, duplicate keys or malformed ingress."""
    if not isinstance(raw, str):
        raise ProducerRefusal("Transport must return JSON text.")
    return strict_json('{"value":' + raw + '}')["value"]


def fetch_history(fetch_page, *, page_size=100, max_pages=8):
    """Finite explicit requests, with one empty extra-page proof at a full cap."""
    if type(page_size) is not int or not 1 <= page_size <= 100:
        raise ProducerRefusal("Invalid history page size.")
    if type(max_pages) is not int or not 1 <= max_pages <= 8:
        raise ProducerRefusal("Invalid history page cap.")
    pages = []
    for page in range(1, max_pages + 2):
        try:
            raw = fetch_page(page, page_size)
        except Exception:
            raise ProducerRefusal("Authenticated history request failed.") from None
        History(raw)  # Validate *before* shape/terminal-page decisions.
        if len(raw) > page_size or (page > max_pages and raw):
            raise ProducerRefusal("Complete history exceeds the page cap.")
        if page <= max_pages:
            pages.append(raw)
        if len(raw) < page_size:
            return History.from_pages(pages)
    raise AssertionError("finite page probe must terminate")


@dataclass(frozen=True)
class Snapshot:
    target: Target
    author: str
    labels: tuple[str, ...]

    def __post_init__(self):
        if not isinstance(self.target, Target) or not isinstance(self.author, str):
            raise ProducerRefusal("Exact target and author read required.")
        if not isinstance(self.labels, tuple) or any(
                not isinstance(label, str) or not label.strip() for label in self.labels):
            raise ProducerRefusal("Readable explicit labels required.")


def exact_snapshot(io, target):
    snapshot = io.snapshot(target)
    if not isinstance(snapshot, Snapshot) or snapshot.target != target:
        raise ProducerRefusal("Fresh target, exact branch or head changed.")
    return snapshot


def label_plan(observation, snapshot, identity):
    if snapshot.target != observation.chain.target or observation.decision.status != "ready":
        raise ProducerRefusal("Label plan requires the ready exact observation.")
    writer = observation.decision.current_writer
    desired = f"builder:{writer}"
    if not writer or dict(identity.labels).get(desired) != writer:
        raise ProducerRefusal("Current writer requires an explicit builder label mapping.")
    active = [s for s in snapshot.labels if s.lower().startswith("builder:")]
    remove = tuple(s for s in active if s != desired)
    return desired, remove, desired not in active


@dataclass(frozen=True)
class Publication:
    observation: Observation
    comment_posted: bool
    labels_attempted: bool


def publish(io, target, identity, authorities, private):
    """Explicit POST -> public-only semantic readback -> fresh labels -> verification.

    A refusal after POST truthfully reports the partial effect. No retry occurs.
    """
    posted = attempted = False
    try:
        initial = exact_snapshot(io, target)
        public = io.history(target)
        observation = observe(target, identity, authorities, public, private,
                              author=initial.author, labels=initial.labels)
        expected = observation.chain.episodes
        body = render(observation.chain)  # Empty chains cannot be published.
        # Validate the plan before POST, including configured destination label.
        label_plan(observation, initial, identity)
        public_only = Chain.from_arrivals(target, _arrivals(public, (), authorities))
        fresh = exact_snapshot(io, target)
        if fresh != initial:
            raise ProducerRefusal("Target metadata changed before publication.")
        if public_only.episodes != expected:
            io.post(target, body)
            posted = True
        readback = io.history(target)
        # Private evidence must never repair a missing final public publication.
        verified = Chain.from_arrivals(target, _arrivals(readback, (), authorities))
        if verified.episodes != expected:
            raise ProducerRefusal("Public semantic lineage readback differs.")
        fresh = exact_snapshot(io, target)
        if fresh.author != initial.author:
            raise ProducerRefusal("Author changed before reconciliation.")
        desired, remove, add = label_plan(observation, fresh, identity)
        if remove or add:
            attempted = True
            io.labels(target, desired, remove, add)
        final = exact_snapshot(io, target)
        active = [s for s in final.labels if s.lower().startswith("builder:")]
        if active != [desired]:
            raise ProducerRefusal("One active builder label was not verified.")
        return Publication(observation, posted, attempted)
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ContractError) else "Producer I/O unavailable; inspect partial outcome."
        raise ProducerRefusal(reason, comment_posted=posted, labels_attempted=attempted) from None


@dataclass(frozen=True)
class Transport:
    """Observed transport metadata supplied by the trusted launch/record adapter."""
    lane: str
    provider: str
    executor: str
    integration: str

    def __post_init__(self):
        allowed = {
            ("codex", "codex", "codex_cli", "local_cli"),
            ("claude", "claude", "claude_cli", "local_cli"),
            ("devin", "devin_cli", "devin_cli", "local_cli"),
            ("devin", "devin", "devin", "hosted_async_builder"),
        }
        if (self.lane, self.provider, self.executor, self.integration) not in allowed:
            raise ProducerRefusal("Unsupported observed builder transport.")


def require_producer(transport, config, runtime_observation):
    """Reuse role qualification; runtime is obtained from the broker's observer."""
    from .role_eligibility import decide_role, require_role
    if not isinstance(transport, Transport):
        raise ProducerRefusal("Observed transport required.")
    runtime = runtime_observation()
    decision = decide_role(transport.lane, "builder", config=config,
                           transport="devin_api_v3" if transport.integration == "hosted_async_builder"
                           else transport.executor, runtime=runtime, bounded=True)
    require_role(decision, execution=True)
    return decision


@dataclass(frozen=True, init=False)
class VerifiedDelivery:
    """Only the trusted handoff/delivery converters mint this internal receipt."""
    episode: Episode
    writer: str
    round_id: str
    transport: Transport


def _delivery(episode, writer, round_id, transport):
    for value in (writer, round_id):
        if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", value):
            raise ProducerRefusal("Named writer and round identifiers required.")
    if not isinstance(episode, Episode) or not isinstance(transport, Transport):
        raise ProducerRefusal("Validated episode and observed transport required.")
    if episode.destination_lane != transport.lane:
        raise ProducerRefusal("Actual writer lane differs from delivery.")
    receipt = object.__new__(VerifiedDelivery)
    for key, value in dict(episode=episode, writer=writer, round_id=round_id, transport=transport).items():
        object.__setattr__(receipt, key, value)
    return receipt


class ProducerStore:
    """Bounded metadata in the existing private, atomic, descriptor-anchored store.

    Missing records refuse on read. Creation is explicit and only accepts a
    verified first takeover; falsey/malformed existing payloads never mean empty.
    """
    def __init__(self, root):
        self.store = ContextStore(root)

    def _key(self, target):
        from .lane_handoff import key
        return key([target.repo, target.pr_number, target.branch])

    def _read(self, raw, target):
        _supported(raw, "record")
        if set(raw) != {"schema", "repo", "pr_number", "branch", "episodes", "writer", "round_id", "transport"}:
            raise ProducerRefusal("Private lineage record fields differ.")
        if (raw["repo"], raw["pr_number"], raw["branch"]) != (target.repo, target.pr_number, target.branch):
            raise ProducerRefusal("Private lineage target differs.")
        if not isinstance(raw["episodes"], list) or not raw["episodes"] or len(raw["episodes"]) > 32:
            raise ProducerRefusal("Private lineage episode list is invalid.")
        transport = Transport(**raw["transport"])
        # Metadata validation without a separate Chain/budget or resolution.
        _delivery(Episode.from_mapping(_supported(raw["episodes"][-1], "episode")),
                  raw["writer"], raw["round_id"], transport)
        return raw

    def read(self, target):
        with self.store.locked(self._key(target)) as locked:
            return self._read(locked.read(), target)

    def record(self, delivery, target, identity, authorities, history, *, author, labels,
               config, runtime_observation, create=False):
        if not isinstance(delivery, VerifiedDelivery):
            raise ProducerRefusal("Verified supervised delivery required.")
        require_producer(delivery.transport, config, runtime_observation)
        with self.store.locked(self._key(target)) as locked:
            raw = locked.read()
            if raw is None and create:
                previous = []
                # A chain starts either at a verified takeover of an existing PR
                # or at the verified creation of the PR by this very lane.
                if delivery.episode.sequence != 1 or delivery.episode.kind not in FIRST_EPISODE_KINDS:
                    raise ProducerRefusal("Creation requires a first verified takeover or PR creation.")
            else:
                raw = self._read(raw, target)
                previous = raw["episodes"]
                if delivery.episode.kind == "continuation" and (
                        raw["writer"] != delivery.writer or raw["transport"] != delivery.transport.__dict__):
                    raise ProducerRefusal("Continuation must retain the actual writer and transport.")
                if raw["round_id"] == delivery.round_id and previous[-1] != delivery.episode.to_mapping():
                    raise ProducerRefusal("Conflicting replay of a supervised round.")
            observation = observe(target, identity, authorities, history,
                                  chain(previous, (delivery.episode,)), author=author, labels=labels)
            episodes = [e.to_mapping() for e in observation.chain.episodes]
            updated = dict(schema="code_mower.lineageProducer.v1", repo=target.repo,
                           pr_number=target.pr_number, branch=target.branch, episodes=episodes,
                           writer=delivery.writer, round_id=delivery.round_id,
                           transport=delivery.transport.__dict__)
            if raw == updated:
                return False
            # An old round may not replace current writer metadata on an idempotent replay.
            if previous and delivery.episode.sequence != len(episodes):
                raise ProducerRefusal("Stale delivery replay.")
            locked.write(updated)
            return True


def projection(observation):
    return {"status": observation.decision.status, "current_writer": observation.decision.current_writer,
            "contributors": list(observation.decision.contributors),
            "head_sha": observation.chain.target.head_sha,
            "episode_count": len(observation.chain.episodes)}


class GitHub:
    """Authenticated gh transport with finite requests; no checkout or config execution."""
    def _json(self, endpoint, *args):
        result = subprocess.run(["gh", "api", endpoint, *args], check=True, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
        return decode_transport(result.stdout)

    def snapshot(self, target):
        raw = self._json(f"repos/{target.repo}/pulls/{target.pr_number}")
        if not isinstance(raw, dict) or raw.get("state") != "open":
            raise ProducerRefusal("Open PR target read required.")
        observed = Target(raw["base"]["repo"]["full_name"], raw["number"],
                          raw["head"]["ref"], raw["head"]["sha"])
        labels = self._json(f"repos/{target.repo}/issues/{target.pr_number}/labels?per_page=100")
        if not isinstance(labels, list) or len(labels) >= 100:
            raise ProducerRefusal("Complete readable label list required.")
        if any(not isinstance(item, dict) or not isinstance(item.get("name"), str) for item in labels):
            raise ProducerRefusal("Malformed labels.")
        return Snapshot(observed, raw["user"]["login"], tuple(item["name"] for item in labels))

    def history(self, target):
        return fetch_history(lambda page, size: self._json(
            f"repos/{target.repo}/issues/{target.pr_number}/comments?per_page={size}&page={page}"))

    def pulls_for_branch(self, repo, branch):
        """Every PR ever opened from one same-repository branch, in one finite read.

        ``state=all`` is deliberate: a closed or superseded pull request on the
        same branch still makes the creation ambiguous, and a caller that only
        saw the open one would bind a creation episode to the wrong PR.
        """
        owner = repo.split("/")[0]
        raw = self._json(f"repos/{repo}/pulls?state=all&per_page=100"
                         f"&head={owner}:{branch}")
        if not isinstance(raw, list) or len(raw) >= 100:
            raise ProducerRefusal("Complete readable created pull request list required.")
        return raw

    def pull_frontier(self, repo):
        """The highest pull request number this repository had at the time of the read.

        GitHub allocates issue and pull request numbers from one monotone
        per-repository sequence, so a pull request opened after this read is
        numbered strictly above every pull request that already existed. Read
        before a creation round launches, this is the independent evidence that
        separates a pull request the round created from one it merely found.
        """
        raw = self._json(f"repos/{repo}/pulls?state=all&sort=created&direction=desc&per_page=100")
        if not isinstance(raw, list) or any(not isinstance(item, dict)
                                            or type(item.get("number")) is not int for item in raw):
            raise ProducerRefusal("Readable pull request frontier required.")
        # The newest page already contains the maximum; older pages cannot exceed it.
        return max((item["number"] for item in raw), default=0)

    def post(self, target, body):
        self._json(f"repos/{target.repo}/issues/{target.pr_number}/comments",
                   "--method", "POST", "-f", "body=" + body)

    def labels(self, target, desired, remove, add):
        argv = ["gh", "pr", "edit", str(target.pr_number), "--repo", target.repo]
        if add:
            argv.extend(["--add-label", desired])
        for label in remove:
            argv.extend(["--remove-label", label])
        subprocess.run(argv, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)


def staged_record(environ, *, io=None, clock=None):
    """Real API for the independent staged artifacts; retrieval/attribution only.

    Policy/authority and observed transport are reviewed broker inputs. The
    immutable policy revision is mandatory metadata, never a moving-ref read.
    """
    from .builder_runs import record_lineage_builder
    from datetime import datetime, timezone
    io = io if io is not None else GitHub()
    target = Target.from_mapping(decode_transport(environ["LINEAGE_TARGET_JSON"]))
    policy = decode_transport(environ["LINEAGE_POLICY_JSON"])
    authority = Authorities(decode_transport(environ["LINEAGE_AUTHORITY_JSON"]))
    identity = Identity.from_mapping(policy["identity"])
    # Validate a reviewed immutable base; neither base nor PR code is executed.
    Target(target.repo, target.pr_number, target.branch, policy["base_sha"])
    transport = Transport(**decode_transport(environ["LINEAGE_TRANSPORT_JSON"]))
    from .role_eligibility import decide_role, require_role
    # Attribution does not launch a provider or claim a runtime/containment probe.
    require_role(decide_role(transport.lane, "builder", config=policy["roles"],
        transport="devin_api_v3" if transport.integration == "hosted_async_builder"
        else transport.executor, bounded=True))
    initial = exact_snapshot(io, target)
    history = io.history(target)
    observation = observe(target, identity, authority, history, (),
                          author=initial.author, labels=initial.labels)
    if exact_snapshot(io, target) != initial:
        raise ProducerRefusal("Snapshot changed before attribution.")
    now = clock() if clock else datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return record_lineage_builder(observation, transport, Path(environ["LINEAGE_OUTPUT"]), created_at=now)
