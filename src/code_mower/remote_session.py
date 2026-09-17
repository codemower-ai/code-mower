"""Versioned remote work lifecycle. All durable data is private; projections are closed.

Adapters must not retry mutations. A durable intent precedes every remote write.
Only public_projection() is suitable for Board/cloud consumers, never store records.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
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
    def observe(self, binding: str) -> Session: ...
    def message(self, binding: str, prose: str) -> Session: ...
    def cancel(self, binding: str) -> Session: ...


class DevinProvider:
    name = "devin"

    def __init__(self, client: DevinClient, *, completion_schema: dict | None = None):
        self.completion_schema = completion_schema
        self.client = client
        self.account = client.org_id

    def create(self, prompt, repo, limit, checkpoint):
        payload = create_payload(prompt, repositories=(repo,), max_acu_limit=limit)
        if self.completion_schema is not None:
            payload.update(structured_output_required=True,
                           structured_output_schema=self.completion_schema)
        return self.client.create(
            payload,
            checkpoint=lambda attempt: checkpoint(asdict(attempt)),
        )

    def reconcile(self, checkpoint):
        result = self.client.reconcile(CreateCheckpoint(**checkpoint))
        return result.session_id if result.state == "matched" else None

    def get(self, binding):
        return self.client.get(binding)

    def observe(self, binding):
        return self.client.observe(binding)

    def message(self, binding, prose):
        return self.client.send_message(binding, prose)

    def cancel(self, binding):
        return self.client.terminate(binding)

    def usage(self, binding):
        return self.client.session_acu(binding)


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
            writer = value.get("writer_state", {
                "complete": "terminated", "terminated": "terminated", "suspended": "suspended",
            }.get(value["state"], "running"))
            return Session(binding, value["state"], value["reason"], value["result"], writer)

    def set_state(self, binding, state, *, reason="", result=None):
        """Local simulation control for embedding/tests; no public result channel."""
        with self.store.locked(binding) as locked:
            locked.write({"state": state, "reason": reason, "result": result})
        return self.get(binding)

    def observe(self, binding):
        value = self.store.read_only(binding)
        if value is None:
            raise RemoteError("provider_unavailable")
        return Session(binding, value["state"], value["reason"])

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


def _observe(record, snapshot, *, include_result=True):
    state = snapshot.state
    if state != "owner_action" and state not in STATES - {"uncertain"}:
        # A result must not turn an invalid provider state into valid completion.
        raise RemoteError("invalid_response")
    # Devin may keep a resumable session running, waiting or terminal after
    # accepting its schema-bound result.  Match the campaign adapter's result
    # precedence: failures, suspension and approval still win; otherwise the
    # private result is ready even though the raw session has not exited.
    if state in {"failed", "suspended"}:
        pass
    elif state == "owner_action" and snapshot.reason == "approval_required":
        state = "waiting_for_approval"
    elif include_result and snapshot.structured_output is not None:
        state = "complete"
    elif state == "owner_action":
        state = "waiting_for_user"
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


@dataclass(frozen=True)
class RemoteObservation:
    """Safe metadata from one read; generation is an opaque local correlation key.

    An embedding may retain this snapshot explicitly across refresh/restart.
    Observation itself writes nothing, including no lifecycle or result cache.
    """

    generation: str
    provider: str
    lifecycle: dict | None
    observed_at: datetime | None
    checked_at: datetime
    available: bool


@dataclass(frozen=True)
class RemoteWorkObservation:
    """Provider-neutral hosted work metadata; no completion body or provider ID."""

    generation: str
    repository: str
    issue: int
    round_number: int
    session: RemoteObservation
    pr_number: int | None = None
    head_sha: str | None = None
    pr_state: str = "unknown"
    github_available: bool = False
    implementation_verified: bool = False


def _observation_generation(record: dict, session: str) -> str:
    return _digest([session, *(record.get(key) for key in
                    ("schema", "provider", "account", "repo", "binding", "fingerprint",
                     "operations"))])


class RemoteSessions:
    def __init__(self, root: Path, provider: Provider):
        self.store = ContextStore(root)
        self.provider = provider

    def observe(self, session: str, *, repo: str,
                previous: RemoteObservation | None = None,
                now: datetime | None = None) -> RemoteObservation:
        """Read lifecycle only: no reconcile, mutation, lock or private result read.

        Re-read the durable intent after GET, so a concurrent fix/cancel cannot
        adopt a response or retained observation from an earlier generation.
        Providers must implement the metadata-only seam; never fall back to get.
        """
        instant = now or datetime.now(timezone.utc)
        if instant.tzinfo is None:
            raise RemoteError("invalid_request")
        key = _key(session)
        record = self.store.read_only(key)
        if (not record or record.get("schema") != SCHEMA
                or record.get("provider") != self.provider.name
                or record.get("account") != self.provider.account
                or record.get("repo") != repo):
            raise RemoteError("binding_mismatch")
        generation = _observation_generation(record, session)
        available = True
        observed = instant
        lifecycle = None
        try:
            if not record.get("binding"):
                lifecycle = public_projection({**record, "state": "uncertain",
                                               "reason": "reconcile_dispatch",
                                               "next_action": "inspect_provider"})
            elif any(op["state"] == "pending" for op in record["operations"].values()):
                lifecycle = public_projection({**record, "state": "uncertain",
                                               "reason": "inspect_provider_then_acknowledge",
                                               "next_action": "acknowledge_delivered"})
            else:
                snapshot = self.provider.observe(record["binding"])
                if snapshot.session_id != record["binding"]:
                    raise RemoteError("binding_mismatch")
                projected = dict(record)
                _observe(projected, snapshot, include_result=False)
                if snapshot.state == "archived":
                    # Archival alone is not evidence that implementation finished.
                    projected["reason"] = "result_not_ready"
                lifecycle = public_projection(projected)
        except Exception:
            available, observed = False, None
            if (isinstance(previous, RemoteObservation)
                    and previous.generation == generation
                    and previous.provider == self.provider.name
                    and previous.observed_at is not None
                    and previous.observed_at <= previous.checked_at <= instant
                    and previous.lifecycle is not None):
                lifecycle = public_projection(previous.lifecycle)
                observed = previous.observed_at
        current = self.store.read_only(key)
        if current is None or _observation_generation(current, session) != generation:
            raise RemoteError("binding_mismatch")
        return RemoteObservation(generation, self.provider.name, lifecycle, observed, instant, available)

    def writer_state(self, session: str, *, repo: str) -> str:
        """Observe the bound writer, never infer exit from collected results."""
        with self.store.locked(_key(session)) as locked:
            record = locked.read()
            self._writer_binding(record, repo)
            if any(op["state"] == "pending" for op in record["operations"].values()):
                return "unknown"
            return self._writer_observation(record)

    def _writer_binding(self, record: dict | None, repo: str) -> None:
        if (not record or record.get("schema") != SCHEMA
                or record.get("provider") != self.provider.name
                or record.get("account") != self.provider.account
                or record.get("repo", "").lower() != repo.lower()
                or not repo or not record.get("binding")):
            raise RemoteError("binding_mismatch")

    def _writer_observation(self, record: dict) -> str:
        state = _call(self.provider.get, record["binding"]).writer_state
        return state if state in {"running", "suspended", "terminated"} else "unknown"

    def retire_writer(self, session: str, *, repo: str, request: str) -> str:
        """Cancel through the existing durable lifecycle and prevent later resumes.

        Cancellation acceptance is insufficient: a fresh provider read must prove
        quiescence. Pending/uncertain writes remain owner work, never paid retries.
        """
        state = self.writer_state(session, repo=repo)
        if state not in {"suspended", "terminated"}:
            self.run("cancel", session, request=request, apply=True)
        with self.store.locked(_key(session)) as locked:
            record = locked.read()
            self._writer_binding(record, repo)
            if any(op["state"] == "pending" for op in record["operations"].values()):
                raise RemoteError("writer_quiescence_unverified")
            state = self._writer_observation(record)
            if state not in {"suspended", "terminated"}:
                raise RemoteError("writer_quiescence_unverified")
            record["writer_retired"] = True
            locked.write(record)
            return state

    def observed_acu(self, session: str) -> float | None:
        """Read billing metadata for the durable binding, never completion assertions."""
        with self.store.locked(_key(session)) as locked:
            record = locked.read()
            if (not record or record.get("provider") != self.provider.name
                    or record.get("account") != self.provider.account or not record.get("binding")):
                raise RemoteError("binding_mismatch")
            usage = getattr(self.provider, "usage", None)
            return _call(usage, record["binding"]) if usage else None

    def private_result(self, session: str) -> dict | None:
        """Explicit local consumer seam. NEVER pass this value to telemetry adapters."""
        key = _key(session)
        with self.store.locked(key) as locked:
            record = locked.read()
            if not record or record.get("provider") != self.provider.name or record.get("account") != self.provider.account:
                raise RemoteError("binding_mismatch")
            if (record["state"] != "complete" or record.get("reason") != "none"
                    or any(op["state"] == "pending" for op in record["operations"].values())):
                return None
            return locked.artifact(key).read() if record["counts"]["collect"] else None

    def discard_private_result(self, session: str, expected: dict | None) -> bool:
        """Release one rejected local result so a later provider result can be collected.

        This compare-bound recovery performs no provider call and changes only the
        local collect count and private artifact. A concurrent replacement is
        preserved rather than discarded.
        """
        key = _key(session)
        with self.store.locked(key) as locked:
            record = locked.read()
            if (not record or record.get("provider") != self.provider.name
                    or record.get("account") != self.provider.account
                    or not record.get("binding")):
                raise RemoteError("binding_mismatch")
            if record.get("counts", {}).get("collect") != 1:
                return False
            artifact = locked.artifact(key)
            if artifact.read() != expected:
                return False
            # Persist the zero count first. If the process stops before deletion,
            # private_result hides the rejected artifact and the next collect
            # atomically replaces it.
            record["counts"]["collect"] = 0
            locked.write(record)
            artifact.delete()
            return True

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
                              repo=repo,
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
            if command == "message" and record.get("writer_retired"):
                raise RemoteError("writer_retired: start a separately authorized work item")
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
                        # Resume/cancel intent invalidates completion before any remote write.
                        record["counts"]["collect"] = 0
                        locked.artifact(key).delete()
                        locked.write(record)
                        if command == "message":
                            _call(self.provider.message, record["binding"], prose)
                        else:
                            _call(self.provider.cancel, record["binding"])
                        record["operations"][opkey]["state"] = "done"
                        record["counts"][command] += 1
                        locked.write(record)
                try:
                    snapshot = _call(self.provider.get, record["binding"])
                    _observe(record, snapshot)
                except RemoteError:
                    record.update(state="uncertain", reason="provider_unavailable", next_action="status")
                    record["counts"]["collect"] = 0
                    locked.artifact(key).delete()
                    locked.write(record)
                    raise
                if record["state"] != "complete":
                    record["counts"]["collect"] = 0
                    locked.artifact(key).delete()
                if command == "collect":
                    if record["state"] != "complete":
                        record["reason"] = "result_not_ready"
                    elif snapshot.structured_output is None:
                        record["reason"] = "result_unavailable"
                    elif (not record["counts"]["collect"] and not any(
                            op["state"] == "pending" for op in record["operations"].values())):
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
