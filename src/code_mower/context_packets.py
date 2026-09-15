"""Private, bounded retrieval and authorized packet reuse for a work item."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import context_graph_connection as graph_connection
from . import context_graph_lifecycle as lifecycle
from .context_connections import _backend, _state, authorize_locked
from .context_contract import (
    CAPABILITY_VERSION, PACKET_SCHEMA, ContextError, ContextRequest, ContextRetrievalError, _object,
    _text, _timestamp, load_packet, normalize_policy,
)
from .context_store import ContextStore, strict_json

INDEX_SCHEMA = "code_mower.contextPacketIndex.v1"
MAX_SAVED_PACKETS = 16


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def _key(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def request_spec(value, name):
    value = _object(value, {"repository", "work_item", "recipient", "query", "policy"}, {"source"})
    policy = normalize_policy(value["policy"])
    if policy is None or policy["connection"] != name:
        raise ContextError("context request must select its configured connection")
    return {**{k: _text(value[k]) for k in ("repository", "work_item", "recipient")},
            "query": _text(value["query"], maximum=2000),
            "source": _text(value["source"], maximum=80) if value.get("source") is not None else None,
            "policy": policy}


def consuming_revision(repo_root) -> str | None:
    """The commit the *consuming* checkout is at, or ``None`` if it has none.

    This is the revision prepared evidence is for, and it is read from the
    checkout doing the work rather than from whichever checkout a connection was
    registered against: the two are routinely different commits, and a local
    repository graph describing the other one does not describe this work.

    ``None`` rather than a raise, so a connection that has no use for a code
    revision -- an organization search, whose sources are documents with their
    own versions -- still prepares from a directory that is not a Git checkout.
    A repository-kind connection refuses instead of falling back.
    """
    try:
        commit, _tree = lifecycle.resolve_revision(Path(repo_root))
    except (ContextError, OSError, ValueError):
        return None
    return commit


def _handle(value):
    try:
        if uuid.UUID(hex=value).hex != value:
            raise ValueError("invalid")
    except (AttributeError, TypeError, ValueError):
        raise ContextError("context packet handle is invalid") from None
    return value


def _index(locked):
    artifact = locked.artifact("i-" + hashlib.sha256(locked.connection.encode()).hexdigest()[:48])
    saved = artifact.read()
    value = saved if saved is not None else {"schema": INDEX_SCHEMA, "entries": []}
    _object(value, {"schema", "entries"})
    if value["schema"] != INDEX_SCHEMA or not isinstance(value["entries"], list) or len(value["entries"]) > MAX_SAVED_PACKETS:
        raise ContextError("private context packet index is invalid")
    handles, keys = set(), set()
    for entry in value["entries"]:
        _object(entry, {"key", "handle", "reference", "generation", "usage"}, {"deliveries", "failure_reason"})
        if "failure_reason" in entry:
            ContextRetrievalError(entry["failure_reason"])
            if entry["reference"] is not None:
                raise ContextError("completed context packet cannot contain a failure reason")
        deliveries = entry.get("deliveries", [])
        if not isinstance(deliveries, list) or len(deliveries) > 8:
            raise ContextError("context packet delivery count exceeds its bound")
        for revision in deliveries:
            _handle(revision)
        _handle(entry["handle"])
        if (not isinstance(entry["key"], str) or len(entry["key"]) != 64
                or any(c not in "0123456789abcdef" for c in entry["key"])
                or entry["handle"] in handles or entry["key"] in keys):
            raise ContextError("private context packet index has invalid references")
        handles.add(entry["handle"])
        keys.add(entry["key"])
        _text(entry["generation"])
        if entry["reference"] is not None:
            ref = _object(entry["reference"], {"path", "sha256"})
            if ref["path"] != ".p-" + entry["handle"] + ".json":
                raise ContextError("private context packet path does not match its handle")
        if entry["usage"] is not None:
            _object(entry["usage"], {"requests", "pages", "elapsed_seconds", "cost_usd", "response_bytes"})
            if (type(entry["usage"]["requests"]) is not int or not 0 <= entry["usage"]["requests"] <= 20
                    or type(entry["usage"]["pages"]) is not int or not 0 <= entry["usage"]["pages"] <= 20
                    or type(entry["usage"]["elapsed_seconds"]) not in (float, int)
                    or not 0 <= entry["usage"]["elapsed_seconds"] <= 120
                    or type(entry["usage"]["response_bytes"]) is not int
                    or not 0 <= entry["usage"]["response_bytes"] <= 262_144
                    or entry["usage"]["cost_usd"] is not None):
                raise ContextError("private context usage metadata is invalid")
    return artifact, value


def purge_connection(locked):
    artifact, index = _index(locked)
    for entry in index["entries"]:
        _delete_entry(locked, entry)
    artifact.write({"schema": INDEX_SCHEMA, "entries": []})


def _delete_entry(locked, entry):
    for revision in entry.get("deliveries", []):
        locked.artifact("d-" + revision).delete()
    locked.artifact("p-" + entry["handle"]).delete()


def _request(spec, recipient=None, revision=None):
    return ContextRequest(spec["repository"], spec["work_item"], recipient or spec["recipient"],
                          revision)


def _load(store, entry, policy, request, envelope, *, bound_revision=None):
    """Load one saved packet, and for a local graph require the consuming revision.

    ``bound_revision`` is the commit the *consuming* work is at, resolved by the
    same authorization that produced ``envelope``. The shared contract already
    computes ``revision_state`` from it; what is decided here is what a
    mismatch means. For a local repository graph it is a refusal: the evidence
    describes one commit's code, so evidence for commit A handed to work on
    commit B is wrong rather than merely old, and the caller's own required or
    optional policy then decides whether that pauses or degrades the work.

    An organization connection passes ``None`` and is unaffected. Its source
    revision is an external document version that has no reason to equal a code
    commit, and requiring one would refuse every organization packet.
    """
    if entry["reference"] is None:
        if "failure_reason" in entry:
            raise ContextRetrievalError(entry["failure_reason"])
        raise ContextError("context retrieval did not complete; use an explicit refresh to try again")
    packet = load_packet(private_root=store.root, reference=entry["reference"], policy=policy,
                         request=request, authorize=lambda: envelope)
    if bound_revision is not None and packet.revision_state != "matching":
        raise ContextError("local graph evidence is not bound to the consuming revision")
    return packet


def fetch(store: ContextStore, name, spec, *, backend=None, refresh=False, revision=None):
    """Retrieve once, or reauthorize and reuse; never redispatch automatically.

    Which provider answers is the connection's own saved state, read here under
    the same lock that guards the retrieval. A local repository graph reaches
    the same index, the same packet files, and the same delivery contract as an
    organization connection; what differs is only where authorization and
    evidence come from, and neither kind can be mistaken for the other because
    the saved schema is checked before either path is taken.

    ``revision`` is the commit the *consuming* work is at, and it has no
    default: a caller that cannot name it gets a refusal from a repository
    graph rather than the registered checkout's ``HEAD``, which is a different
    checkout that moves independently. An organization connection never reads
    it, so the same default costs it nothing.
    """
    spec = request_spec(spec, name)
    policy = spec["policy"]
    started = time.monotonic()
    with store.locked(name, timeout_seconds=policy["timeout_seconds"]) as locked:
        local = graph_connection.is_graph(locked.read())
        left = policy["timeout_seconds"] - (time.monotonic() - started)
        if left <= 0:
            raise ContextError("context retrieval deadline exceeded before authorization")
        bound = None
        if local:
            if revision is None:
                # Default-deny rather than fall back to the registered
                # checkout's ``HEAD``: that fallback is exactly how evidence for
                # one commit reaches work on another.
                raise ContextError("local graph context requires the consuming checkout revision")
            envelope, bound = graph_connection.authorized_revision(
                locked, name, root=store.root, revision=revision,
            )
        else:
            # Deferred until the connection kind is known: a local graph must
            # not require the optional provider SDK to be installed at all.
            backend = backend or _backend()
            envelope = authorize_locked(locked, name, backend, timeout_seconds=min(left, 30))
        if spec["repository"] not in envelope["repositories"] or spec["recipient"] not in envelope["recipients"]:
            raise ContextError("context connection does not authorize this repository or recipient")
        fingerprint = _key({k: v for k, v in spec.items() if k != "recipient"})
        index_file, index = _index(locked)
        old = next((entry for entry in index["entries"] if entry["key"] == fingerprint), None)
        if old is not None and not refresh:
            packet = _load(store, old, policy, _request(spec, revision=bound), envelope,
                           bound_revision=bound)
            return {**packet.shareable_summary(), "status": "available", "packet_handle": old["handle"],
                    "reused": True, "usage": old["usage"]}
        # Reserve before any paid/read tool call. Restarting a failed attempt
        # cannot silently repeat it; the operator must explicitly refresh.
        entry = {"key": fingerprint, "handle": uuid.uuid4().hex, "reference": None,
                 "generation": envelope["generation"], "usage": None}
        removed = [old] if old else []
        entries = [item for item in index["entries"] if item is not old]
        if len(entries) >= MAX_SAVED_PACKETS:
            removed.append(entries.pop(0))
        for previous in removed:
            _delete_entry(locked, previous)
        index["entries"] = [*entries, entry]
        index_file.write(index)
        left = policy["timeout_seconds"] - (time.monotonic() - started)
        if left <= 0:
            raise ContextError("context retrieval deadline exceeded; no search was sent")
        state = graph_connection.saved_state(locked.read(), name) if local else _state(locked.read(), name)
        credentials = None if local else locked.vault.get(state["credential_id"])
        try:
            try:
                if local:
                    # The local graph mints its own packet: it decides what the
                    # evidence is, and the envelope above decides who may read it.
                    packet_data = graph_connection.retrieve(
                        state, spec, envelope=envelope, root=store.root, revision=revision,
                    )
                else:
                    result = backend.retrieve(credentials, spec["query"], spec["source"], policy, timeout_seconds=left)
            except Exception as exc:
                # Either provider failing to produce evidence is a retrieval
                # failure, told apart below from storage and packet validation.
                raise ContextRetrievalError(
                    exc.reason if isinstance(exc, ContextRetrievalError) else "retrieval_failed"
                ) from None
            usage = None
            if not local:
                now = datetime.now(timezone.utc)
                expiry = min(_timestamp(envelope["expires_at"]), now + timedelta(seconds=policy["max_age_seconds"]))
                packet_data = {"schema": PACKET_SCHEMA, "capability_version": CAPABILITY_VERSION,
                               "provider": "coworker", "kind": "organization", "retrieved_at": now.isoformat(),
                               **{key: result[key] for key in ("documents", "completeness", "truncated", "source_revision", "source_built_at", "omissions")},
                               "binding": {**{key: envelope[key] for key in ("connection", "generation", "identity", "recipients")},
                                           "repository": spec["repository"], "work_item": spec["work_item"],
                                           "policy_version": policy["policy_version"], "expires_at": expiry.isoformat()}}
                usage = result["usage"]
            locked.artifact("p-" + entry["handle"]).write(packet_data)
            raw = json.dumps(packet_data, allow_nan=False, separators=(",", ":")).encode()
            entry["reference"] = {"path": ".p-" + entry["handle"] + ".json", "sha256": hashlib.sha256(raw).hexdigest()}
            packet = _load(store, entry, policy, _request(spec, revision=bound), envelope,
                           bound_revision=bound)
            entry["usage"] = usage
            index_file.write(index)
            _index(locked)
            if not local:
                locked.write({**state, "capability_status": {"search": "available", "memory": "available"}})
        except Exception as exc:
            # Storage and local packet validation are not provider failures.
            failure = ContextRetrievalError(
                exc.reason if isinstance(exc, ContextRetrievalError)
                else "storage_unavailable" if isinstance(exc, OSError) else "packet_invalid"
            )
            entry["reference"] = None
            entry["usage"] = None
            entry["failure_reason"] = failure.reason
            try:
                index_file.write(index)
                locked.artifact("p-" + entry["handle"]).delete()
                if not local:
                    # A local graph connection keeps no provider capability state.
                    locked.write({**state, "capability_status": {"search": "unavailable", "memory": "unavailable"}})
            except (OSError, ContextError):
                entry["failure_reason"] = "storage_unavailable"
                try:
                    index_file.write(index)
                except (OSError, ContextError):
                    pass
                raise ContextRetrievalError("storage_unavailable") from None
            raise failure from None
        return {**packet.shareable_summary(), "status": "available", "packet_handle": entry["handle"],
                "reused": False, "usage": entry["usage"]}


def load_authorized(store, name, handle, policy, request: ContextRequest, *, backend=None,
                    revision=None):
    """Every participant replay obtains a new authorization under lock.

    For an organization connection that is a fresh online check. For a local
    repository graph it is a fresh read of current local state: the published
    generation for the *consuming* revision. Either way the envelope is minted
    here and now, so a packet whose graph was rebuilt or whose revision has
    moved on is refused by the shared contract rather than replayed.

    The consuming revision travels on the request the caller already builds --
    ``ContextRequest.revision`` -- and ``revision`` is the same value for the
    callers that hold it without holding a request, such as an attachment that
    knows only the trusted current PR head. Neither is defaulted to ``HEAD``
    for a graph: a replay that cannot name the revision it is for is refused,
    because the checkout the graph was registered from moves independently of
    the work consuming the evidence.
    """
    _handle(handle)
    with store.locked(name) as locked:
        bound = None
        if graph_connection.is_graph(locked.read()):
            consuming = request.revision or revision
            if consuming is None:
                raise ContextError("local graph context requires the consuming checkout revision")
            envelope, bound = graph_connection.authorized_revision(
                locked, name, root=store.root, revision=consuming,
            )
            # Resolved, so a symbolic consuming revision is compared as the
            # commit it names rather than as the word the caller typed.
            request = ContextRequest(request.repository, request.work_item, request.recipient, bound)
        else:
            envelope = authorize_locked(locked, name, backend or _backend())
        _file, index = _index(locked)
        entry = next((entry for entry in index["entries"] if entry["handle"] == handle), None)
        if entry is None:
            raise ContextError("context packet is missing or was invalidated")
        return _load(store, entry, policy, request, envelope, bound_revision=bound)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="code-mower context fetch")
    parser.add_argument("--connection", required=True)
    parser.add_argument("--request-stdin", action="store_true", required=True,
                        help="Read private repository/work-item/query/policy JSON from stdin")
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--refresh", action="store_true", help="Explicitly replace or retry a prior retrieval")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    required = True
    try:
        spec = request_spec(strict_json(sys.stdin.buffer.read(262_145)), args.connection)
        required = spec["policy"]["required"]
        # The consuming checkout is the one this command was run from, not the
        # one a connection was registered against. Leaving ``fetch`` to default
        # to ``HEAD`` resolves that word in the *registered* graph checkout, so a
        # graph for commit A could answer work at commit B -- the exact fallback
        # the guided route already refuses. ``None`` when the caller is not a Git
        # checkout at all: that refuses a repository graph here (it cannot name
        # the revision its evidence would be for) and is ignored by an
        # organization connection, whose sources version independently of code.
        result = fetch(ContextStore(args.state_dir), args.connection, spec, refresh=args.refresh,
                       revision=consuming_revision(Path.cwd()))
        code = 0
    except (ContextError, OSError, ValueError) as exc:
        result = {"status": "required_unavailable" if required else "optional_unavailable",
                  "next_action": "verify the selected connection or explicitly refresh; no automatic retry"}
        if isinstance(exc, ContextRetrievalError):
            result.update(exc.shareable_summary())
        code = 1 if required else 0
    print(json.dumps(result, sort_keys=True) if args.json else "\n".join(f"{k}: {v}" for k, v in result.items()))
    return code
