"""Versioned remote work lifecycle. All durable data is private; projections are closed.

Adapters must not retry mutations. A durable intent precedes every remote write.
Only public_projection() is suitable for Board/cloud consumers, never store records.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Protocol

from .context_store import ContextStore
from .devin_sessions import CreateCheckpoint, DevinClient, Session, create_payload

SCHEMA = "code_mower.remote_session.v1"
STATES = frozenset({
    "pending", "running", "waiting_for_user", "waiting_for_approval", "complete",
    "failed", "suspended", "terminated", "archived", "uncertain",
})
REASONS = frozenset({
    "none", "user_input_required", "approval_required", "session_failed",
    "session_suspended", "reconcile_dispatch", "inspect_provider_then_acknowledge",
    "provider_unavailable", "result_not_ready", "result_unavailable",
})
ACTIONS = frozenset({"none", "status", "inspect_provider", "acknowledge_delivered"})


class RemoteError(Exception):
    """Closed diagnostics only, including failures from third-party adapters."""


class Provider(Protocol):
    name: str
    account: str

    def create(self, prompt: str, repo: str, limit: int, checkpoint: Callable) -> str: ...
    def reconcile(self, checkpoint: dict) -> str | None: ...
    def get(self, binding: str) -> Session: ...
    def message(self, binding: str, prose: str) -> Session: ...
    def cancel(self, binding: str) -> Session: ...


class DevinProvider:
    name = "devin"

    def __init__(self, client: DevinClient):
        self.client = client
        self.account = client.org_id

    def create(self, prompt, repo, limit, checkpoint):
        return self.client.create(
            create_payload(prompt, repositories=(repo,), max_acu_limit=limit),
            checkpoint=lambda attempt: checkpoint(asdict(attempt)),
        )

    def reconcile(self, checkpoint):
        result = self.client.reconcile(CreateCheckpoint(**checkpoint))
        return result.session_id if result.state == "matched" else None

    def get(self, binding):
        return self.client.get(binding)

    def message(self, binding, prose):
        return self.client.send_message(binding, prose)

    def cancel(self, binding):
        return self.client.terminate(binding)


class FakeProvider:
    """Offline durable simulator, with the same independent remote-write crash window."""
    name = "fake"
    account = "offline"

    def __init__(self, root: Path):
        self.store = ContextStore(root)

    def create(self, prompt, repo, limit, checkpoint):
        import uuid
        binding = "f" + uuid.uuid4().hex
        checkpoint({"binding": binding})
        with self.store.locked(binding) as locked:
            locked.write({"state": "running", "reason": "", "result": None})
        return binding

    def reconcile(self, checkpoint):
        binding = checkpoint["binding"]
        with self.store.locked(binding) as locked:
            return binding if locked.read() is not None else None

    def get(self, binding):
        with self.store.locked(binding) as locked:
            value = locked.read()
            if value is None:
                raise RemoteError("provider_unavailable")
            return Session(binding, value["state"], value["reason"], value["result"])

    def set_state(self, binding, state, *, reason="", result=None):
        """Local simulation control for embedding/tests; no public result channel."""
        with self.store.locked(binding) as locked:
            locked.write({"state": state, "reason": reason, "result": result})
        return self.get(binding)

    def message(self, binding, prose):
        return self.set_state(binding, "running")

    def cancel(self, binding):
        return self.set_state(binding, "terminated")


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _key(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise RemoteError("invalid_request")
    return "s" + _digest(value)[:62]


def public_projection(record: dict) -> dict:
    """Construct an allowlist, never redact/forward an arbitrary provider payload."""
    state, reason = record.get("state"), record.get("reason", "none")
    action = record.get("next_action", "none")
    counts = record.get("counts", {})
    if (state not in STATES or reason not in REASONS or action not in ACTIONS
            or any(type(counts.get(k, 0)) is not int or not 0 <= counts.get(k, 0) <= 10000
                   for k in ("dispatch", "message", "cancel", "collect"))):
        raise RemoteError("invalid_state")
    return {
        "schema": SCHEMA, "state": state, "reason": reason, "next_action": action,
        "counts": {k: counts.get(k, 0) for k in ("dispatch", "message", "cancel", "collect")},
    }


def _observe(record, snapshot):
    state = snapshot.state
    if state == "owner_action":
        state = ("waiting_for_approval" if snapshot.reason == "approval_required"
                 else "waiting_for_user")
    if state not in STATES - {"uncertain"}:
        raise RemoteError("invalid_response")
    record["state"] = state
    record["reason"] = {
        "waiting_for_approval": "approval_required", "waiting_for_user": "user_input_required",
        "failed": "session_failed", "suspended": "session_suspended",
    }.get(state, "none")
    record["next_action"] = "none"


def _call(method, *args):
    try:
        return method(*args)
    except Exception:
        raise RemoteError("provider_unavailable: run session status; inspect pending requests") from None


class RemoteSessions:
    def __init__(self, root: Path, provider: Provider):
        self.store = ContextStore(root)
        self.provider = provider

    def private_result(self, session: str) -> dict | None:
        """Explicit local consumer seam. NEVER pass this value to telemetry adapters."""
        key = _key(session)
        with self.store.locked(key) as locked:
            record = locked.read()
            if not record or record.get("provider") != self.provider.name or record.get("account") != self.provider.account:
                raise RemoteError("binding_mismatch")
            return locked.artifact(key).read() if record["counts"]["collect"] else None

    def run(self, command: str, session: str, *, request: str = "", prose: str = "",
            repo: str = "", limit: int = 10, apply: bool = False, dry_run: bool = False,
            acknowledge_delivered: bool = False) -> dict:
        if command not in {"dispatch", "status", "message", "cancel", "collect"}:
            raise RemoteError("invalid_request")
        key = _key(session)
        if command in {"dispatch", "message"} and not acknowledge_delivered:
            if not isinstance(prose, str) or not prose.strip() or len(prose.encode()) > 65536:
                raise RemoteError("invalid_request")
        if command in {"message", "cancel"}:
            request_key = _key(request)
        else:
            request_key = command
        if command == "dispatch":
            # Validate fake and live requests identically, without network access.
            create_payload(prose, repositories=(repo,), max_acu_limit=limit)
        if acknowledge_delivered and command not in {"message", "cancel"}:
            raise RemoteError("invalid_request")
        if dry_run or (command != "status" and not apply):
            return {"schema": SCHEMA, "mode": "dry_run", "operation": command,
                    "apply_required": command != "status"}
        with self.store.locked(key) as locked:
            record = locked.read()
            if record is not None and (
                record.get("schema") != SCHEMA or record.get("provider") != self.provider.name
                or record.get("account") != self.provider.account
            ):
                raise RemoteError("binding_mismatch")
            if record is None:
                if command != "dispatch":
                    raise RemoteError("session_not_found")
                record = dict(schema=SCHEMA, provider=self.provider.name,
                              account=self.provider.account, binding=None, checkpoint=None,
                              fingerprint=_digest([prose, repo, limit]), state="uncertain",
                              reason="reconcile_dispatch", next_action="status", operations={},
                              counts=dict(dispatch=0, message=0, cancel=0, collect=0))
                locked.write(record)  # Reserve even before adapter validation/create.
                def checkpoint(value):
                    record["checkpoint"] = value
                    locked.write(record)  # fsync + directory fsync before paid POST.
                try:
                    record["binding"] = self.provider.create(prose, repo, limit, checkpoint)
                    record["counts"]["dispatch"] = 1
                    record.update(state="pending", reason="none", next_action="none")
                    locked.write(record)
                except Exception:
                    # Never repeat create, even if the adapter failed before its callback.
                    raise RemoteError("reconcile_dispatch: run session status; never redispatch") from None
            if command == "dispatch" and record["fingerprint"] != _digest([prose, repo, limit]):
                raise RemoteError("request_conflict: reuse the original dispatch input")
            try:
                if not record["binding"]:
                    if record["checkpoint"]:
                        record["binding"] = _call(self.provider.reconcile, record["checkpoint"])
                    if not record["binding"]:
                        record["next_action"] = "inspect_provider"
                        locked.write(record)
                        return public_projection(record)
                    record["counts"]["dispatch"] = 1
                    locked.write(record)
                if command in {"message", "cancel"}:
                    opkey = command + ":" + request_key
                    prior = record["operations"].get(opkey)
                    fingerprint = _digest(prose if command == "message" else "cancel")
                    if prior:
                        if acknowledge_delivered:
                            if prior["state"] == "pending":
                                prior["state"] = "done"
                                record["counts"][command] += 1
                                locked.write(record)
                        elif prior["fingerprint"] != fingerprint:
                            raise RemoteError("request_conflict: reuse the original request input")
                        elif prior["state"] == "pending":
                            raise RemoteError("inspect_provider_then_acknowledge: use --acknowledge-delivered --apply")
                    else:
                        if acknowledge_delivered:
                            raise RemoteError("request_not_found")
                        if any(op["state"] == "pending" for op in record["operations"].values()):
                            raise RemoteError("inspect_provider_then_acknowledge: resolve the pending request first")
                        if len(record["operations"]) >= 128:
                            raise RemoteError("request_limit_reached")
                        record["operations"][opkey] = {"state": "pending", "fingerprint": fingerprint}
                        locked.write(record)
                        if command == "message":
                            _call(self.provider.message, record["binding"], prose)
                        else:
                            _call(self.provider.cancel, record["binding"])
                        record["operations"][opkey]["state"] = "done"
                        record["counts"][command] += 1
                        locked.write(record)
                snapshot = _call(self.provider.get, record["binding"])
                _observe(record, snapshot)
                if command == "collect":
                    if record["state"] != "complete":
                        record["reason"] = "result_not_ready"
                    elif snapshot.structured_output is None:
                        record["reason"] = "result_unavailable"
                    elif not record["counts"]["collect"]:
                        # Result is only accessible through this protected store, never stdout.
                        locked.artifact(key).write(snapshot.structured_output)
                        record["counts"]["collect"] = 1
                if any(op["state"] == "pending" for op in record["operations"].values()):
                    record.update(state="uncertain", reason="inspect_provider_then_acknowledge",
                                  next_action="acknowledge_delivered")
                locked.write(record)
                return public_projection(record)
            except RemoteError:
                raise
            except Exception:
                raise RemoteError("provider_unavailable: run session status; inspect pending requests") from None


def default_root() -> Path:
    return Path.home() / ".local" / "share" / "code-mower" / "remote-sessions"
