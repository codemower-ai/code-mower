"""Private, resumable work-item state for context-aware Code Mower sessions."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from . import session_lease
from .context_contract import ContextError, _object, _text, normalize_policy
from .context_store import ContextStore, default_context_root
from .participants import PARTICIPANTS, participant_id


ASSOCIATION_SCHEMA = "code_mower.contextSession.v1"
STATUS_SCHEMA = "code_mower.contextSessionStatus.v1"
STAGES = frozenset(("selected", "preparing", "prepared", "attached", "reviewed"))
ATTACHMENT_STATES = frozenset(("none", "pending", "published", "uncertain"))
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_HEX = re.compile(r"[a-f0-9]{32}\Z")
_SHA = re.compile(r"(?:[a-f0-9]{40}|[a-f0-9]{64})\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")


def default_association_root() -> Path:
    """Keep resumable private state beside, but separate from, connections."""
    return default_context_root() / "sessions"


def association_store(context_root: Path | None = None) -> ContextStore:
    root = Path(context_root) if context_root is not None else default_context_root()
    return ContextStore(root / "sessions")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session_id(value: Any) -> str:
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        raise ContextError("session context requires a valid saved session")
    try:
        if uuid.UUID(hex=value).hex != value:
            raise ValueError
    except ValueError:
        raise ContextError("session context requires a valid saved session") from None
    return value


def _store_key(session_id: str) -> str:
    return "session-" + _session_id(session_id)


def _repo(value: Any) -> str:
    if not isinstance(value, str) or not _REPO.fullmatch(value):
        raise ContextError("session context requires a valid repository")
    return value


def _optional_handle(value: Any, *, sha: bool = False) -> str | None:
    if value is None:
        return None
    pattern = _SHA if sha else _HEX
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ContextError("private session context state is invalid")
    return value


def _path_reference(value: Any) -> str | None:
    if value is None:
        return None
    value = _text(value, maximum=1000)
    path = PurePosixPath(value)
    if "\x00" in value or "\\" in value or path.is_absolute() or ".." in path.parts:
        raise ContextError("private session context state is invalid")
    return value


def work_order_reference(value: Any) -> str:
    reference = _path_reference(value)
    if reference is None:
        raise ContextError("private session context state is invalid")
    return reference


def validate(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the complete private association; callers never render it."""
    fields = {
        "schema", "session_id", "repo", "work_item", "connection", "policy",
        "host", "orchestrator", "participants", "builder", "generation", "stage",
        "request_hash", "packet", "work_order", "pr", "head", "revision", "attachment_state",
        "created_at", "updated_at",
    }
    value = _object(value, fields)
    if value["schema"] != ASSOCIATION_SCHEMA:
        raise ContextError("private session context state is from an unsupported version")
    session_id = _session_id(value["session_id"])
    repo = _repo(value["repo"])
    work_item = _text(value["work_item"], maximum=256)
    connection = value["connection"]
    policy = value["policy"]
    if connection is None:
        if policy is not None:
            raise ContextError("private session context policy has no selected connection")
    else:
        connection = _text(connection, maximum=80)
        policy = normalize_policy(policy)
        if policy is None or policy["connection"] != connection:
            raise ContextError("private session context policy does not match its connection")
    host = participant_id(value["host"])
    orchestrator = participant_id(value["orchestrator"])
    if not PARTICIPANTS[host].orchestrator or not PARTICIPANTS[orchestrator].orchestrator:
        raise ContextError("private session context requires an agent orchestrator")
    participants = value["participants"]
    if not isinstance(participants, list) or not participants:
        raise ContextError("private session context requires selected participants")
    normalized_participants = [participant_id(item) for item in participants]
    if len(set(normalized_participants)) != len(normalized_participants):
        raise ContextError("private session context has duplicate participants")
    builder = value["builder"]
    if builder is not None:
        builder = participant_id(builder)
        if builder not in normalized_participants or not PARTICIPANTS[builder].builder:
            raise ContextError("private session context builder is not an approved participant")
    generation = value["generation"]
    if type(generation) is not int or not 0 <= generation <= 1_000_000:
        raise ContextError("private session context generation is invalid")
    if value["stage"] not in STAGES or value["attachment_state"] not in ATTACHMENT_STATES:
        raise ContextError("private session context lifecycle state is invalid")
    request_hash = value["request_hash"]
    if request_hash is not None and (
        not isinstance(request_hash, str) or not _HASH.fullmatch(request_hash)
    ):
        raise ContextError("private session context state is invalid")
    packet = _optional_handle(value["packet"])
    work_order = _path_reference(value["work_order"])
    pr = value["pr"]
    if pr is not None and (type(pr) is not int or not 1 <= pr <= 1_000_000_000):
        raise ContextError("private session context pull request is invalid")
    head = _optional_handle(value["head"], sha=True)
    revision = _optional_handle(value["revision"])
    for timestamp in (value["created_at"], value["updated_at"]):
        try:
            parsed = datetime.fromisoformat(timestamp)
        except (TypeError, ValueError):
            raise ContextError("private session context timestamp is invalid") from None
        if parsed.tzinfo is None:
            raise ContextError("private session context timestamp is invalid")
    if value["stage"] == "selected" and any(
        item is not None for item in (builder, request_hash, packet, work_order, pr, head, revision)
    ):
        raise ContextError("private session context selected state has unexpected progress")
    if value["stage"] == "preparing" and (
        builder is None or request_hash is None or work_order is not None or any(
            item is not None for item in (pr, head, revision)
        )
    ):
        raise ContextError("private session context preparing state is invalid")
    if value["stage"] in {"prepared", "attached", "reviewed"} and any(
        item is None for item in (builder, request_hash, packet, work_order)
    ):
        raise ContextError("private session context prepared state is incomplete")
    if value["attachment_state"] == "none" and revision is not None:
        raise ContextError("private session context revision has no attachment")
    return {
        **dict(value), "session_id": session_id, "repo": repo, "work_item": work_item,
        "connection": connection, "policy": policy, "host": host,
        "orchestrator": orchestrator, "participants": normalized_participants, "builder": builder,
        "request_hash": request_hash, "packet": packet, "work_order": work_order, "pr": pr, "head": head,
        "revision": revision,
    }


def load_session(path: Path) -> dict[str, Any]:
    """Read the public operating brief needed to locate private state."""
    try:
        from .context_store import strict_json
        value = strict_json(Path(path).read_bytes())
    except (OSError, ContextError):
        raise ContextError("saved Code Mower session is unavailable or invalid") from None
    required = {"schema", "id", "repo", "host", "orchestrator", "participants", "lease"}
    if value.get("schema") != "code_mower.session.v1" or not required.issubset(value):
        raise ContextError("saved Code Mower session is unavailable or invalid")
    _session_id(value["id"])
    _repo(value["repo"])
    participant_id(value["host"])
    participant_id(value["orchestrator"])
    if not isinstance(value["participants"], list):
        raise ContextError("saved Code Mower session is unavailable or invalid")
    return value


def require_live_session(session: Mapping[str, Any], *, repo_root: Path | None = None) -> None:
    live = session_lease.verify_live_lease(
        repo=session["repo"], session_id=session["id"], orchestrator=session["orchestrator"],
        root=repo_root,
    )
    if not live.get("mutating"):
        raise ContextError("this session no longer holds the mutating lease; start or resume an authorized session")


def create(
    store: ContextStore,
    session: Mapping[str, Any],
    *,
    work_item: str,
    policy: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Create the association once; mismatched retries fail closed."""
    session_id = _session_id(session["id"])
    normalized_policy = normalize_policy(policy) if policy is not None else None
    connection = normalized_policy["connection"] if normalized_policy is not None else None
    now = _now()
    participants = [participant_id(row["id"]) for row in session["participants"]]
    record = validate({
        "schema": ASSOCIATION_SCHEMA, "session_id": session_id, "repo": session["repo"],
        "work_item": work_item, "connection": connection, "policy": normalized_policy,
        "host": session["host"], "orchestrator": session["orchestrator"],
        "participants": participants, "builder": None, "generation": 0, "stage": "selected",
        "request_hash": None, "packet": None, "work_order": None, "pr": None, "head": None,
        "revision": None, "attachment_state": "none", "created_at": now,
        "updated_at": now,
    })
    with store.locked(_store_key(session_id)) as locked:
        current = locked.read()
        if current is not None:
            existing = validate(current)
            binding_fields = (
                "schema", "session_id", "repo", "work_item", "connection", "policy",
                "host", "orchestrator", "participants",
            )
            if any(existing[field] != record[field] for field in binding_fields):
                raise ContextError("this session is already bound to a different work item")
            return existing
        locked.write(record)
    return record


def read(store: ContextStore, session_id: str) -> dict[str, Any] | None:
    with store.locked(_store_key(session_id)) as locked:
        value = locked.read()
    return validate(value) if value is not None else None


def delete(store: ContextStore, session_id: str) -> None:
    with store.locked(_store_key(session_id)) as locked:
        locked.delete()


def update(
    store: ContextStore,
    session_id: str,
    *,
    expected_generation: int,
    changes: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare-and-update private progress for later guided lifecycle stages."""
    immutable = {"schema", "session_id", "repo", "work_item", "connection", "policy",
                 "host", "orchestrator", "participants", "created_at"}
    if set(changes) & immutable or "generation" in changes or "updated_at" in changes:
        raise ContextError("private session context identity cannot be changed")
    with store.locked(_store_key(session_id)) as locked:
        current = locked.read()
        if current is None:
            raise ContextError("this session has no selected work item")
        record = validate(current)
        if record["generation"] != expected_generation:
            raise ContextError("session context changed concurrently; inspect status and retry")
        proposed = validate({
            **record, **dict(changes), "generation": expected_generation + 1,
            "updated_at": _now(),
        })
        locked.write(proposed)
    return proposed


def resolve_bound(name: str, *values: str | None) -> str | None:
    """Resolve one identity only when all available trusted sources agree."""
    present = [value for value in values if value not in (None, "")]
    if not present:
        return None
    if any(value != present[0] for value in present[1:]):
        raise ContextError(f"{name} conflicts with the saved session; start a new session or use the selected value")
    return present[0]


def status(record: Mapping[str, Any] | None, *, lease_live: bool) -> dict[str, Any]:
    """Return a closed, redacted status vocabulary with no private identifiers."""
    if record is None:
        return {
            "schema": STATUS_SCHEMA, "selected": False, "configured": False,
            "stage": "not_selected", "dependent_work": "usable", "owner_action": False,
            "next_action": "Start a session with --work-item to use guided context.",
        }
    record = validate(record)
    if not lease_live:
        return {
            "schema": STATUS_SCHEMA, "selected": True,
            "configured": record["connection"] is not None, "stage": "lease_inactive",
            "dependent_work": "paused", "owner_action": True,
            "next_action": "Start or resume the authorized mutating session before continuing.",
        }
    if record["connection"] is None:
        return {
            "schema": STATUS_SCHEMA, "selected": True, "configured": False,
            "stage": "not_configured", "dependent_work": "usable", "owner_action": False,
            "next_action": "Continue the ordinary workflow or configure an optional context connection.",
        }
    actions = {
        "selected": "Prepare bounded context for this work item.",
        "prepared": "Continue the build and attach context when a pull request exists.",
        "attached": "Run the current-head independent review.",
        "reviewed": "Read authorized private feedback or complete the work item.",
    }
    if record["stage"] == "preparing":
        resumable = record["packet"] is not None
        return {
            "schema": STATUS_SCHEMA,
            "selected": True,
            "configured": True,
            "stage": "preparing",
            "dependent_work": "paused" if record["policy"]["required"] else "usable",
            "owner_action": not resumable,
            "next_action": (
                "Resume context preparation; the completed retrieval will be reused."
                if resumable
                else "Verify the selected connection, then rerun prepare with --refresh."
            ),
        }
    return {
        "schema": STATUS_SCHEMA, "selected": True, "configured": True,
        "stage": record["stage"], "dependent_work": "usable", "owner_action": False,
        "next_action": actions[record["stage"]],
    }
