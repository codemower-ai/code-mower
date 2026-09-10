"""Local single-orchestrator lease for mutating Code Mower sessions.

A Code Mower session is agent-coordinated: the hosting agent drives builders and
reviewers through its own tools. Nothing stopped two agents from starting a
mutating session against the same working copy at the same time, and both would
then believe they were the orchestrator for it. This module is the local mutual
exclusion for that case -- one live lease per session state directory, taken
explicitly at session startup, and given up only by release, expiry, or an
owner's deliberate takeover.

The lease is local coordination state, not shared truth. It records exactly six
metadata fields plus its schema version: the repository slug, the normalized
orchestrator provider id, the session id, and the acquired/renewed/expires UTC
timestamps. It never holds issue text, credentials, diffs, or provider output;
it lives under the git-ignored ``.code-mower/`` tree; and no Code Mower export
or upload path reads it, so it is never sent anywhere.

Writes are serialized on a dedicated lock file through
:mod:`code_mower.file_locks` and land through a temporary file plus
``os.replace``, so the read-decide-write of an acquisition is indivisible and a
reader never observes a half-written lease. Both properties matter for the same
reason: under concurrent acquisition exactly one caller may win.

Expiry is what keeps a crashed or abandoned session from wedging a repository
forever. A lease past ``expires_at`` -- and a lease file that cannot be parsed
as this schema -- is treated as absent, so the next startup takes it over
without any owner action. A *live* lease held by another session is never taken
implicitly: the refusal names the holder and the owner's actions instead.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from .file_locks import exclusive_file_lock


LEASE_SCHEMA = "code_mower.session_lease.v1"
LEASE_FILE_NAME = "orchestrator-lease.json"

# Long enough that an ordinary agent session is never cut off mid-flight, short
# enough that an abandoned one recovers the same working day without an owner
# having to force a takeover.
DEFAULT_TTL_MINUTES = 720

# The complete lease record. Anything outside this tuple is not lease data, and
# the reader rejects records that do not carry exactly these fields.
LEASE_FIELDS = (
    "schema",
    "repo",
    "orchestrator",
    "session_id",
    "acquired_at",
    "renewed_at",
    "expires_at",
)

STATE_ABSENT = "absent"
STATE_HELD = "held"
STATE_EXPIRED = "expired"


class SessionLeaseError(RuntimeError):
    """Raised when a mutating session lease cannot be taken, renewed, or released.

    The message carries the owner-facing guidance verbatim, so callers print it
    rather than restating the conflict.
    """


def lease_path(state_dir: str | Path) -> Path:
    """Where the single lease for ``state_dir`` lives."""
    return Path(state_dir) / LEASE_FILE_NAME


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Serialize lease reads and writes on a lock file beside the lease.

    The lock file is never deleted, so an unlink of the lease itself cannot race
    a waiter that already opened the lock.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_file_lock(path.with_name(f"{path.name}.lock")):
        yield


def read_lease(path: str | Path) -> dict[str, Any] | None:
    """Return the stored lease, or None when there is nothing usable to honor.

    A missing file, unreadable file, malformed JSON, a foreign schema, or a
    record that is not exactly :data:`LEASE_FIELDS` of strings all read as
    absent. Refusing to honor a record this code cannot fully understand is what
    makes recovery safe: the worst case is that a takeover is allowed, never
    that a repository is wedged by a corrupt file.
    """
    lease_file = Path(path)
    try:
        raw = lease_file.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        record = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict) or record.get("schema") != LEASE_SCHEMA:
        return None
    if set(record) != set(LEASE_FIELDS):
        return None
    if not all(isinstance(record[field], str) and record[field] for field in LEASE_FIELDS):
        return None
    return record


def lease_state(record: Mapping[str, Any] | None, *, now: datetime | None = None) -> str:
    """Classify a lease as absent, held, or expired at ``now``."""
    if record is None:
        return STATE_ABSENT
    expires_at = _parse_timestamp(record.get("expires_at"))
    if expires_at is None:
        return STATE_EXPIRED
    return STATE_HELD if expires_at > (now or _now()) else STATE_EXPIRED


def _expires_in_seconds(record: Mapping[str, Any] | None, *, now: datetime) -> int | None:
    expires_at = _parse_timestamp((record or {}).get("expires_at"))
    if expires_at is None:
        return None
    return int((expires_at - now).total_seconds())


def _humanize(seconds: int) -> str:
    remaining = abs(seconds)
    hours, minutes = divmod(remaining // 60, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{remaining}s"


def _status(
    record: Mapping[str, Any] | None,
    *,
    now: datetime,
    path: Path,
    action: str,
    **extra: Any,
) -> dict[str, Any]:
    """One payload shape for every lease command, so one renderer serves them all."""
    payload: dict[str, Any] = {
        "schema": LEASE_SCHEMA,
        "action": action,
        "state": lease_state(record, now=now),
        "lease": dict(record) if record is not None else None,
        "lease_file": str(path.resolve()),
        "expires_in_seconds": _expires_in_seconds(record, now=now),
    }
    payload.update(extra)
    return payload


def refusal_text(record: Mapping[str, Any], *, now: datetime, requested_repo: str | None = None) -> str:
    """Concise owner-action guidance for a live lease held by another session."""
    remaining = _expires_in_seconds(record, now=now)
    expiry = record["expires_at"]
    if remaining is not None and remaining > 0:
        expiry = f"{expiry} (about {_humanize(remaining)} left)"
    lines = [
        f"another session already holds the mutating orchestrator lease for {record['repo']}",
        f"  holder: {record['orchestrator']} (session {record['session_id']})",
        f"  expires: {expiry}",
    ]
    if requested_repo and requested_repo != record["repo"]:
        lines.append(f"  requested: {requested_repo}")
    lines.extend(
        [
            "  owner actions:",
            "    inspect it:              code-mower session lease show",
            "    let its owner release:   code-mower session lease release --session-id <id>",
            "    take it over on purpose: code-mower session lease release --force",
            "  read-only briefs need no lease: add --dry-run or --no-lease to session start",
        ]
    )
    return "\n".join(lines)


def _write_lease(path: Path, record: Mapping[str, Any]) -> None:
    """Publish a lease atomically, so no reader sees a partial record."""
    temp_path = path.with_name(f"{path.name}.tmp")
    temp_path.write_text(json.dumps(dict(record), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp_path, path)


def acquire_lease(
    *,
    repo: str,
    orchestrator: str,
    session_id: str,
    state_dir: str | Path,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
    force: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Take the single mutating lease for ``state_dir``, or refuse.

    Re-acquiring under the same ``session_id`` renews in place and keeps the
    original ``acquired_at``. An absent or expired lease is taken over silently;
    a live lease held by another session is taken only with ``force``, which
    callers must gate on an explicit owner decision.
    """
    if ttl_minutes <= 0:
        raise SessionLeaseError("the lease TTL must be a positive number of minutes")
    moment = now or _now()
    path = lease_path(state_dir)
    with _locked(path):
        current = read_lease(path)
        state = lease_state(current, now=moment)
        held_by_us = state == STATE_HELD and current is not None and current["session_id"] == session_id
        if state == STATE_HELD and not held_by_us and not force:
            raise SessionLeaseError(refusal_text(current, now=moment, requested_repo=repo))
        record = {
            "schema": LEASE_SCHEMA,
            "repo": repo,
            "orchestrator": orchestrator,
            "session_id": session_id,
            "acquired_at": current["acquired_at"] if held_by_us else moment.isoformat(),
            "renewed_at": moment.isoformat(),
            "expires_at": (moment + timedelta(minutes=ttl_minutes)).isoformat(),
        }
        _write_lease(path, record)
    return record


def renew_lease(
    *,
    state_dir: str | Path,
    session_id: str,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Extend the caller's own live lease.

    An expired lease is not renewed. Another session may already have been told
    it is free to take over, so the owner has to come back through
    ``session start`` and win the acquisition again.
    """
    if ttl_minutes <= 0:
        raise SessionLeaseError("the lease TTL must be a positive number of minutes")
    moment = now or _now()
    path = lease_path(state_dir)
    with _locked(path):
        current = read_lease(path)
        state = lease_state(current, now=moment)
        if state == STATE_ABSENT:
            raise SessionLeaseError(
                "no mutating orchestrator lease is held here; run code-mower session start to take one"
            )
        if state == STATE_EXPIRED:
            raise SessionLeaseError(
                "this session lease has expired and cannot be renewed; "
                "run code-mower session start to take a fresh one"
            )
        if current["session_id"] != session_id:
            raise SessionLeaseError(refusal_text(current, now=moment))
        record = {
            **current,
            "renewed_at": moment.isoformat(),
            "expires_at": (moment + timedelta(minutes=ttl_minutes)).isoformat(),
        }
        _write_lease(path, record)
    return _status(record, now=moment, path=path, action="renew")


def release_lease(
    *,
    state_dir: str | Path,
    session_id: str | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Give up the lease.

    Releasing nothing succeeds; so does releasing an expired lease, which is
    already free. Releasing a live lease requires either its own ``session_id``
    or ``force`` -- the explicit takeover an owner authorizes after deciding the
    holding session is gone.
    """
    moment = now or _now()
    path = lease_path(state_dir)
    with _locked(path):
        current = read_lease(path)
        state = lease_state(current, now=moment)
        if state == STATE_HELD and not force and current["session_id"] != session_id:
            raise SessionLeaseError(refusal_text(current, now=moment))
        released = path.exists()
        path.unlink(missing_ok=True)
    return _status(
        None,
        now=moment,
        path=path,
        action="release",
        released=released,
        previous=dict(current) if current is not None else None,
        previous_state=state,
    )


def inspect_lease(*, state_dir: str | Path, now: datetime | None = None) -> dict[str, Any]:
    """Report the lease without changing it."""
    moment = now or _now()
    path = lease_path(state_dir)
    with _locked(path):
        current = read_lease(path)
    return _status(current, now=moment, path=path, action="inspect")


def render_lease(payload: Mapping[str, Any]) -> str:
    """Human-readable form of any lease command payload."""
    if payload.get("action") == "release":
        previous = payload.get("previous")
        if payload.get("released") and previous:
            head = (
                f"Released the mutating orchestrator lease for {previous['repo']} "
                f"(session {previous['session_id']})."
            )
        else:
            head = "No mutating orchestrator lease was present; nothing to release."
        return f"{head}\nLease file: {payload['lease_file']}\n"

    record = payload.get("lease")
    if record is None:
        lines = [
            "Session lease: none",
            f"State: {payload['state']}",
            "No mutating orchestrator lease is held for this session directory.",
            "Read-only briefs do not need one; code-mower session start takes one.",
        ]
    else:
        remaining = payload.get("expires_in_seconds")
        expiry = record["expires_at"]
        if isinstance(remaining, int) and remaining > 0:
            expiry = f"{expiry} (about {_humanize(remaining)} left)"
        lines = [
            f"Session lease: {record['repo']}",
            f"State: {payload['state']}",
            f"Orchestrator: {record['orchestrator']}",
            f"Session: {record['session_id']}",
            f"Acquired: {record['acquired_at']}",
            f"Renewed: {record['renewed_at']}",
            f"Expires: {expiry}",
        ]
        if payload["state"] == STATE_EXPIRED:
            lines.append("This lease has expired; the next code-mower session start takes it over.")
    lines.append(f"Lease file: {payload['lease_file']}")
    return "\n".join(lines) + "\n"
