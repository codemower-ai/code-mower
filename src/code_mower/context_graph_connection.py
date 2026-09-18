"""Graphify as a local repository connection for the shared context store.

The query module in ``context_graph_query`` answers one bounded question and
mints one packet. This module is what makes that answer reachable from the
ordinary guided route -- ``session context prepare``, ``deliver``, and the
reuse and attachment that follow -- without a second packet store, a second
delivery contract, or a second set of recipients.

Three things are deliberately absent, because a local graph has none of them:
there is no principal, no workspace, and no credential. Connecting names a
checkout and the repositories and recipients an operator approves for it, and
that is the whole of the connection's state. Nothing here reads the OS
credential vault, opens a browser, or contacts a network endpoint.

What replaces the credential is the graph itself. Authorization is not a saved
token that stays true until it expires; it is re-derived from current trusted
local state on every single load, from ``graph_status`` for the requested
revision, and the envelope it returns carries the *published generation* as its
``generation``. That one choice is what makes the freshness rules fall out of
the shared contract rather than out of new checks here:

* A rebuilt graph publishes a new generation, so a packet minted against the
  old one no longer matches the envelope, and ``load_packet`` refuses it.
* A moved ``HEAD`` makes ``graph_status`` report the published generation stale
  for that revision, so authorization fails outright and nothing is delivered.

A recipient therefore cannot be handed evidence from a graph that no longer
describes the code, and no recipient needs the provider, its pin, or any
Graphify tool to read what it is given.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from . import context_graph_lifecycle as lifecycle
from . import context_graph_query as query
from .context_contract import (
    CAPABILITY_VERSION, CONNECTION_SCHEMA, ContextError, _identifier, _object,
    _strings, _text, normalize_policy, validate_connection,
)
from .context_store import ContextStore

#: Saved state for one local repository connection. Distinct from the private
#: Coworker connection schema so a store can hold both and every reader can
#: tell which contract it is looking at before it validates anything.
GRAPH_SCHEMA = "code_mower.contextLocalGraphConnection.v1"
PROVIDER = "graphify"
CONNECTION_KIND = "repository"

#: Which bounded question a retrieval asks when the caller names none. The
#: guided route carries a free-text ``source`` for exactly this purpose.
DEFAULT_QUESTION = "symbol"

#: How long one live authorization stands. Short on purpose: it bounds a packet
#: that was already minted, and the graph behind it is re-checked on every load
#: regardless, so a longer window would buy nothing and hide a stale build for
#: longer if a check were ever skipped.
AUTHORIZATION_SECONDS = 3600


def is_graph(value: Any) -> bool:
    """Whether saved connection state belongs to this contract.

    Cheap and structural, so a caller can branch before validating. A state
    that claims this schema and then fails ``saved_state`` is an error, not a
    reason to fall through to the organization path.
    """
    return isinstance(value, Mapping) and value.get("schema") == GRAPH_SCHEMA


def connection_spec(value: Any) -> dict[str, Any]:
    """The operator's approval: one checkout, and who may read answers from it."""
    spec = _object(value, {"repository_root", "repositories", "recipients"})
    root = Path(_text(spec["repository_root"], maximum=4096))
    if not root.is_absolute():
        raise ContextError("local context repository root must be absolute")
    return {
        "repository_root": str(root),
        "repositories": list(_strings(spec["repositories"])),
        "recipients": list(_strings(spec["recipients"])),
    }


def saved_state(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        raise ContextError("context connection is missing; run context-graph connect")
    state = _object(value, {"schema", "connection", "provider", "kind", "state",
                            "repository_root", "repositories", "recipients"})
    if (state["schema"] != GRAPH_SCHEMA or state["connection"] != name
            or state["provider"] != PROVIDER or state["kind"] != CONNECTION_KIND
            or state["state"] not in {"verified", "disconnected"}):
        raise ContextError("unsupported local graph context state; reconnect")
    return {**state, **connection_spec({key: state[key] for key in
                                        ("repository_root", "repositories", "recipients")})}


def _summary(state: Mapping[str, Any], *, search: str = "available") -> dict[str, Any]:
    """The connection's shape. ``search`` is the provider capability by default.

    ``connect`` and ``disconnect`` report the capability the connection offers;
    ``status`` passes the observed readiness instead, because that is the one
    report an operator reads to decide whether a query will work now.
    """
    return {"schema": "code_mower.contextConnectionSummary.v1", "provider": PROVIDER,
            "kind": CONNECTION_KIND, "status": state["state"], "search": search,
            "memory": "unavailable", "credential_storage": "none"}


def connect(store: ContextStore, name: str, spec: Any) -> dict[str, Any]:
    """Record one approved checkout. No credential is created or stored."""
    name = _identifier(name)
    spec = connection_spec(spec)
    # Resolving now rather than at every load: the identity a packet binds is
    # the one the operator approved, and a root that later becomes a symlink
    # elsewhere must not quietly redirect the evidence.
    root = lifecycle.checkout_root(Path(spec["repository_root"]))
    with store.locked(name, timeout_seconds=1) as locked:
        old = locked.read()
        if old is not None and not is_graph(old):
            raise ContextError("this connection name already names a different context provider")
        if old is not None and saved_state(old, name)["state"] != "disconnected":
            raise ContextError("connection already exists; disconnect before changing its checkout or scope")
        if old is not None:
            # A reconnect retries packet cleanup under this same lock before
            # authorizing anything again, even when the disconnect that
            # preceded it already reported the cleanup complete: a packet or
            # delivery binding that survived that attempt must not become
            # authorized again just because the graph and scope are unchanged.
            try:
                from .context_packets import purge_connection
                purge_connection(locked)
            except Exception as exc:
                raise ContextError("pending packet cleanup failed; reconnect refused") from exc
        state = {"schema": GRAPH_SCHEMA, "connection": name, "provider": PROVIDER,
                 "kind": CONNECTION_KIND, "state": "verified", "repository_root": str(root),
                 "repositories": spec["repositories"], "recipients": spec["recipients"]}
        locked.write(state)
        return _summary(state)


def disconnect(store: ContextStore, name: str) -> dict[str, Any]:
    """Disable the connection first, then drop the packets it authorized."""
    with store.locked(name) as locked:
        state = {**saved_state(locked.read(), name), "state": "disconnected"}
        locked.write(state)
        packet_cleanup = "complete"
        try:
            from .context_packets import purge_connection
            purge_connection(locked)
        except Exception:
            packet_cleanup = "needs_attention"
        return {**_summary(state), "packet_cleanup": packet_cleanup}


def status(store: ContextStore, name: str, *, root: Path | None = None,
           revision: str = "HEAD") -> dict[str, Any]:
    """Report the connection and the graph behind it, without minting evidence."""
    with store.locked(name) as locked:
        state = saved_state(locked.read(), name)
    repository = Path(state["repository_root"])
    report = lifecycle.graph_status(repository, root=root, revision=revision)
    # The query's own read, not the lifecycle's verdict alone: a current,
    # complete generation this reader cannot consume must not be reported as
    # searchable and then fail on the first question asked of it. A
    # disconnected connection cannot search whatever the reader says, so the
    # graph artifact is not opened for it.
    verified = state["state"] == "verified"
    if verified:
        readiness = query.search_readiness(lifecycle.GraphStateRoot(repository, root=root), report)
    else:
        readiness = query.reader_not_checked("disconnected")
    search = readiness["search"]
    return {**_summary(state, search=search), "graph": report.shareable_summary(),
            "query_reader": readiness,
            "authorization": "available" if verified and report.usable
            and search == query.SEARCH_AVAILABLE else "unavailable"}


def _published(state: Mapping[str, Any], *, root: Path | None,
               revision: str) -> lifecycle.GenerationStatus:
    if state["state"] != "verified":
        raise ContextError("local graph context is disconnected; reconnect before using it")
    report = lifecycle.graph_status(Path(state["repository_root"]), root=root, revision=revision)
    if not report.usable or report.manifest is None:
        # The state word is the lifecycle's own -- ``stale``, ``partial``,
        # ``absent`` -- and it is the only detail worth carrying: it says
        # whether to rebuild, refresh, or build for the first time.
        raise ContextError(
            "local graph is not current for this revision (" + report.state + "); rebuild it"
        )
    return report


def current_generation(state: Mapping[str, Any], *, root: Path | None = None,
                       revision: str = "HEAD") -> str:
    """The published generation a packet must still match to be usable."""
    manifest = _published(state, root=root, revision=revision).manifest
    assert manifest is not None  # a usable status always carries one
    return manifest.generation


def authorized_revision(locked, name: str, *, root: Path | None = None, revision: str = "HEAD",
                        now: datetime | None = None) -> tuple[dict[str, Any], str]:
    """One envelope, and the commit the graph is bound to for ``revision``.

    The commit is returned rather than re-derived by the caller because it is
    the same ``graph_status`` read that authorized the load: asking twice would
    rehash the artifact and, worse, could answer differently across a rebuild,
    so the envelope and the revision a packet is checked against would then come
    from two different observations of the graph.

    ``revision`` is the *consuming* revision -- the commit whose work the
    evidence is for -- and not merely the registered checkout's ``HEAD``. A
    graph that was not built from exactly that commit resolves ``stale`` in
    ``_published`` and never reaches a recipient.
    """
    current = now or datetime.now(timezone.utc)
    state = saved_state(locked.read(), name)
    manifest = _published(state, root=root, revision=revision).manifest
    assert manifest is not None
    return _envelope(state, name, manifest, current), manifest.commit


def authorize_locked(locked, name: str, *, root: Path | None = None, revision: str = "HEAD",
                     now: datetime | None = None) -> dict[str, Any]:
    """Mint one envelope from current local state, under the caller's lock.

    This is the local counterpart of the organization connection's online
    refresh, and it is deliberately the same shape: every load and every replay
    calls it again, and none of them may reuse a previous answer. What it
    checks is not a token but the graph -- present, complete, and binding the
    requested revision -- and what it publishes as ``generation`` is the
    graph's, so the shared packet contract does the rest.
    """
    return authorized_revision(locked, name, root=root, revision=revision, now=now)[0]


def _envelope(state: Mapping[str, Any], name: str, manifest: Any,
              current: datetime) -> dict[str, Any]:
    envelope = {
        "schema": CONNECTION_SCHEMA, "capability_version": CAPABILITY_VERSION,
        "connection": name, "provider": PROVIDER, "kind": CONNECTION_KIND,
        "generation": manifest.generation, "state": "verified",
        "identity": {"repository_root": state["repository_root"]},
        "repositories": list(state["repositories"]), "recipients": list(state["recipients"]),
        "expires_at": (current + timedelta(seconds=AUTHORIZATION_SECONDS)).isoformat(),
        # No memory, and a real revision binding: the packet names the commit
        # the graph was built from, so a recipient asking about another one
        # resolves ``stale`` without decoding the evidence.
        "capabilities": {"search": True, "memory": False, "revision_binding": True},
    }
    return dict(validate_connection(envelope, now=current))


def question_and_target(spec: Mapping[str, Any]) -> tuple[str, str]:
    """Read one bounded question out of the shared retrieval request.

    The guided request carries a free-text ``query`` and an optional ``source``.
    For a graph those are the target and the question, and both are explicit on
    purpose: this connection answers about a named symbol or a repository-
    relative path, and guessing one out of a work item's prose would produce
    confident evidence about whatever happened to match.
    """
    question = spec.get("source") or DEFAULT_QUESTION
    if question not in query.QUESTIONS:
        raise ContextError(
            "local graph source must name a question: " + ", ".join(query.QUESTIONS)
        )
    target = _text(spec["query"], maximum=2000)
    return question, target


def retrieve(state: Mapping[str, Any], spec: Mapping[str, Any], *, envelope: Mapping[str, Any],
             root: Path | None = None, revision: str = "HEAD",
             now: datetime | None = None) -> dict[str, Any]:
    """Answer once from the published generation, or say why there is no answer.

    ``graph_context`` returns rather than raises when the graph cannot answer,
    because "no graph" is a normal state of an opt-in feature. The packet store
    is not a place where that distinction survives -- a reservation either
    produces a packet or it does not -- so the unavailable outcomes become the
    error the store already knows how to unwind, carrying the reason forward.
    """
    question, target = question_and_target(spec)
    policy = normalize_policy(spec["policy"])
    if policy is None:
        raise ContextError("context policy is not configured")
    outcome = query.graph_context(
        Path(state["repository_root"]),
        question=question,
        target=target,
        envelope=envelope,
        policy=policy,
        context_repository=spec["repository"],
        work_item=spec["work_item"],
        root=root,
        revision=revision,
        now=now,
    )
    if outcome.packet is None:
        raise ContextError(
            "local graph context is unavailable (" + str(outcome.summary.get("reason", "unknown")) + ")"
        )
    return outcome.packet


__all__ = (
    "AUTHORIZATION_SECONDS",
    "CONNECTION_KIND",
    "DEFAULT_QUESTION",
    "GRAPH_SCHEMA",
    "PROVIDER",
    "authorize_locked",
    "authorized_revision",
    "connect",
    "connection_spec",
    "current_generation",
    "disconnect",
    "is_graph",
    "question_and_target",
    "retrieve",
    "saved_state",
    "status",
)
