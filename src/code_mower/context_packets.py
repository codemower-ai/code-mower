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

from .context_connections import _backend, _state, authorize_locked
from .context_contract import (
    CAPABILITY_VERSION, PACKET_SCHEMA, ContextError, ContextRequest, _object,
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
        _object(entry, {"key", "handle", "reference", "generation", "usage"}, {"deliveries"})
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


def _request(spec, recipient=None):
    return ContextRequest(spec["repository"], spec["work_item"], recipient or spec["recipient"])


def _load(store, entry, policy, request, envelope):
    if entry["reference"] is None:
        raise ContextError("context retrieval did not complete; use an explicit refresh to try again")
    return load_packet(private_root=store.root, reference=entry["reference"], policy=policy,
                       request=request, authorize=lambda: envelope)


def fetch(store: ContextStore, name, spec, *, backend=None, refresh=False):
    """Retrieve once, or reauthorize and reuse; never redispatch automatically."""
    spec = request_spec(spec, name)
    policy = spec["policy"]
    started = time.monotonic()
    backend = backend or _backend()
    with store.locked(name, timeout_seconds=policy["timeout_seconds"]) as locked:
        left = policy["timeout_seconds"] - (time.monotonic() - started)
        if left <= 0:
            raise ContextError("context retrieval deadline exceeded before authorization")
        envelope = authorize_locked(locked, name, backend, timeout_seconds=min(left, 30))
        if spec["repository"] not in envelope["repositories"] or spec["recipient"] not in envelope["recipients"]:
            raise ContextError("context connection does not authorize this repository or recipient")
        fingerprint = _key({k: v for k, v in spec.items() if k != "recipient"})
        index_file, index = _index(locked)
        old = next((entry for entry in index["entries"] if entry["key"] == fingerprint), None)
        if old is not None and not refresh:
            packet = _load(store, old, policy, _request(spec), envelope)
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
        state = _state(locked.read(), name)
        credentials = locked.vault.get(state["credential_id"])
        try:
            result = backend.retrieve(credentials, spec["query"], spec["source"], policy, timeout_seconds=left)
            now = datetime.now(timezone.utc)
            expiry = min(_timestamp(envelope["expires_at"]), now + timedelta(seconds=policy["max_age_seconds"]))
            packet_data = {"schema": PACKET_SCHEMA, "capability_version": CAPABILITY_VERSION,
                           "provider": "coworker", "kind": "organization", "retrieved_at": now.isoformat(),
                           **{key: result[key] for key in ("documents", "completeness", "truncated", "source_revision", "source_built_at", "omissions")},
                           "binding": {**{key: envelope[key] for key in ("connection", "generation", "identity", "recipients")},
                                       "repository": spec["repository"], "work_item": spec["work_item"],
                                       "policy_version": policy["policy_version"], "expires_at": expiry.isoformat()}}
            locked.artifact("p-" + entry["handle"]).write(packet_data)
            raw = json.dumps(packet_data, allow_nan=False, separators=(",", ":")).encode()
            entry["reference"] = {"path": ".p-" + entry["handle"] + ".json", "sha256": hashlib.sha256(raw).hexdigest()}
            packet = _load(store, entry, policy, _request(spec), envelope)
            entry["usage"] = result["usage"]
            index_file.write(index)
            _index(locked)
            locked.write({**state, "capability_status": {"search": "available", "memory": "available"}})
        except Exception:
            entry["reference"] = None
            entry["usage"] = None
            index_file.write(index)
            locked.artifact("p-" + entry["handle"]).delete()
            locked.write({**state, "capability_status": {"search": "unavailable", "memory": "unavailable"}})
            raise ContextError("context search unavailable; no automatic retry; verify access or explicitly refresh") from None
        return {**packet.shareable_summary(), "status": "available", "packet_handle": entry["handle"],
                "reused": False, "usage": entry["usage"]}


def load_authorized(store, name, handle, policy, request: ContextRequest, *, backend=None):
    """Every participant replay obtains a new online authorization under lock."""
    _handle(handle)
    with store.locked(name) as locked:
        envelope = authorize_locked(locked, name, backend or _backend())
        _file, index = _index(locked)
        entry = next((entry for entry in index["entries"] if entry["handle"] == handle), None)
        if entry is None:
            raise ContextError("context packet is missing or was invalidated")
        return _load(store, entry, policy, request, envelope)


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
        result = fetch(ContextStore(args.state_dir), args.connection, spec, refresh=args.refresh)
        code = 0
    except (ContextError, OSError, ValueError):
        result = {"status": "required_unavailable" if required else "optional_unavailable",
                  "next_action": "verify the selected connection or explicitly refresh; no automatic retry"}
        code = 1 if required else 0
    print(json.dumps(result, sort_keys=True) if args.json else "\n".join(f"{k}: {v}" for k, v in result.items()))
    return code
