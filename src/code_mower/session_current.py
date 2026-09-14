"""Resolve the current local operating brief for a checkout from its live lease.

A cold operator inside a Git checkout should be able to answer "is there an
active Code Mower session here?" without knowing a session filename. The
working copy's orchestrator lease (see :mod:`session_lease`) proves
coordination ownership and names a session id; the matching saved brief is
``<state dir>/<session id>.json``. This module joins the two strictly
read-only: no lock, no directory creation, no renewal, and no guessing.

The resolver never chooses a brief by modification time or by scanning a
directory. It opens exactly the file the live lease names, refuses symlinks
and path escapes, validates the brief against the lease, and re-reads the
lease afterwards so a replacement or expiry during the read is rejected
instead of presented as current.
"""

from __future__ import annotations

import errno
import os
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Any

from . import context_session, session_lease
from .context_contract import ContextError


CURRENT_SESSION_SCHEMA = "code_mower.currentSession.v1"
DEFAULT_STATE_DIR = ".code-mower/sessions"

STATE_ACTIVE = "active"
STATE_NO_WORKING_COPY = "no_working_copy"
STATE_LEASE_ABSENT = "lease_absent"
STATE_LEASE_EXPIRED = "lease_expired"
STATE_LEASE_MALFORMED = "lease_malformed"
STATE_LEASE_UNAVAILABLE = "lease_unavailable"
STATE_BRIEF_MISSING = "brief_missing"
STATE_BRIEF_INVALID = "brief_invalid"
STATE_BRIEF_REFUSED = "brief_refused"
STATE_BRIEF_MISMATCH = "brief_mismatch"
STATE_LEASE_CHANGED = "lease_changed"

RESOLUTION_STATES = frozenset((
    STATE_ACTIVE,
    STATE_NO_WORKING_COPY,
    STATE_LEASE_ABSENT,
    STATE_LEASE_EXPIRED,
    STATE_LEASE_MALFORMED,
    STATE_LEASE_UNAVAILABLE,
    STATE_BRIEF_MISSING,
    STATE_BRIEF_INVALID,
    STATE_BRIEF_REFUSED,
    STATE_BRIEF_MISMATCH,
    STATE_LEASE_CHANGED,
))

_SESSION_ID = re.compile(r"[a-f0-9]{32}\Z")
_SUPPLY_FILE = (
    "pass the saved brief explicitly with `code-mower session show SESSION_FILE`, "
    "or name where it was saved with `code-mower session show --current --state-dir DIR`"
)
_GUIDANCE = {
    STATE_NO_WORKING_COPY: (
        "no Git working copy was found from the current directory; "
        "run this from inside a Git checkout or worktree, or pass a session file"
    ),
    STATE_LEASE_ABSENT: (
        "no mutating orchestrator lease is held in this working copy, so there is no current "
        "session to show; pass a session file to `code-mower session show SESSION_FILE`, "
        "or start one with `code-mower session start`"
    ),
    STATE_LEASE_EXPIRED: (
        "the orchestrator lease in this working copy has expired, so no session is current; "
        "`code-mower session lease show` reports the stale holder, and "
        "`code-mower session start` takes a fresh lease"
    ),
    STATE_LEASE_MALFORMED: (
        "the orchestrator lease file in this working copy is not a lease this version can read; "
        "inspect it with `code-mower session lease show` before relying on a current session"
    ),
    STATE_LEASE_UNAVAILABLE: (
        "the orchestrator lease file in this working copy could not be read; "
        "check permissions and inspect it with `code-mower session lease show`"
    ),
    STATE_BRIEF_MISSING: (
        f"a live orchestrator lease is held here but its saved brief was not found; {_SUPPLY_FILE}"
    ),
    STATE_BRIEF_INVALID: (
        "a live orchestrator lease is held here but the saved brief it names is unavailable "
        f"or invalid; {_SUPPLY_FILE}"
    ),
    STATE_BRIEF_REFUSED: (
        "a live orchestrator lease is held here but its saved brief path is a symlink or escapes "
        "the state directory; refusing to follow it"
    ),
    STATE_BRIEF_MISMATCH: (
        "a live orchestrator lease is held here but the saved brief it names records a different "
        f"session, repository, or orchestrator; {_SUPPLY_FILE}"
    ),
    STATE_LEASE_CHANGED: (
        "the orchestrator lease changed while the saved brief was being read; "
        "rerun `code-mower session show --current`"
    ),
}


def _safe_lease(observed: dict[str, Any], *, state: str) -> dict[str, Any]:
    """The metadata-only projection the Board already uses: no ids, no paths."""
    record = observed.get("record")
    if record is None:
        return {"state": state, "provider": None, "expires_at": None}
    return {"state": state, "provider": record["orchestrator"], "expires_at": observed["expires_at"]}


def _result(state: str, *, lease: dict[str, Any], session: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema": CURRENT_SESSION_SCHEMA,
        "state": state,
        "current": state == STATE_ACTIVE,
        "lease": lease,
        "guidance": None if state == STATE_ACTIVE else _GUIDANCE[state],
        "session": session,
    }


def _lease_result(observed: dict[str, Any]) -> dict[str, Any]:
    lease_state = observed["state"]
    if lease_state == session_lease.STATE_ABSENT:
        state = STATE_LEASE_ABSENT
    elif lease_state == session_lease.STATE_EXPIRED:
        state = STATE_LEASE_EXPIRED
    elif lease_state == "malformed":
        state = STATE_LEASE_MALFORMED
    else:
        state = STATE_LEASE_UNAVAILABLE
    return _result(state, lease=_safe_lease(observed, state=lease_state))


def _read_brief_without_following(path: Path, *, state_dir: Path) -> tuple[str, bytes | None]:
    """Open exactly ``path`` as a regular file inside ``state_dir``.

    ``O_NOFOLLOW`` makes a symlinked brief fail at open time rather than being
    checked and then swapped. The parent must be a real directory (not a
    symlink) whose resolved location contains the opened file, so neither the
    state directory nor the filename can redirect the read elsewhere.
    """
    if state_dir.is_symlink() or not state_dir.is_dir():
        return (STATE_BRIEF_REFUSED if state_dir.is_symlink() else STATE_BRIEF_MISSING), None
    if path.is_symlink():
        return STATE_BRIEF_REFUSED, None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return STATE_BRIEF_MISSING, None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return STATE_BRIEF_REFUSED, None
        return STATE_BRIEF_INVALID, None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return STATE_BRIEF_REFUSED, None
        try:
            if path.resolve(strict=True).parent != state_dir.resolve(strict=True):
                return STATE_BRIEF_REFUSED, None
        except OSError:
            return STATE_BRIEF_REFUSED, None
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            return "read", handle.read()
    except OSError:
        return STATE_BRIEF_INVALID, None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


_PARTICIPANT_FIELDS = ("name", "builder", "reviewer", "note")


def _renderable(brief: dict[str, Any]) -> bool:
    """A brief is current only if every field ``session show`` renders is present and shaped."""
    if not isinstance(brief.get("status"), str) or not brief["status"]:
        return False
    instructions = brief.get("instructions")
    if not isinstance(instructions, list) or not all(isinstance(line, str) for line in instructions):
        return False
    for member in brief["participants"]:
        if not isinstance(member, dict) or any(field not in member for field in _PARTICIPANT_FIELDS):
            return False
        if not isinstance(member["name"], str):
            return False
        if member["builder"] is not None and not isinstance(member["builder"], dict):
            return False
        reviewer = member["reviewer"]
        if reviewer is not None and (not isinstance(reviewer, dict) or not {"lane", "merge_authority"} <= set(reviewer)):
            return False
    return True


def _same_lease(first: dict[str, Any], second: dict[str, Any]) -> bool:
    """The owner renewing in place is still the same lease; anything else is not."""
    return all(first[field] == second[field] for field in ("repo", "orchestrator", "session_id", "acquired_at"))


def resolve_current_session(
    *,
    start: str | Path | None = None,
    state_dir: str | Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Join the checkout's live lease to its saved brief without mutating anything.

    Returns a closed ``code_mower.currentSession.v1`` object. ``current`` is
    True only when one live lease names a brief that exists at
    ``<state_dir>/<session id>.json``, validates as a saved session, matches
    the lease's repository, session id, and orchestrator, and the lease is
    still the same and still live after the brief was read. Every other
    outcome carries a closed ``state``, the Board's safe lease projection, and
    operator ``guidance``; ``session`` is None. ``state_dir`` defaults to
    ``.code-mower/sessions`` under the working-copy root and is never scanned:
    only the exact file the lease names is opened.
    """
    try:
        root = session_lease.find_working_copy_root(start)
    except session_lease.SessionLeaseError:
        return _result(STATE_NO_WORKING_COPY, lease={"state": "unavailable", "provider": None, "expires_at": None})
    first = session_lease.observe_lease_record(root=root, now=now)
    if first["state"] != session_lease.STATE_HELD:
        return _lease_result(first)
    record = first["record"]
    session_id = record["session_id"]
    active_lease = _safe_lease(first, state="active")
    if not _SESSION_ID.fullmatch(session_id):
        return _result(STATE_LEASE_MALFORMED, lease={**active_lease, "state": "malformed"})
    directory = Path(state_dir) if state_dir is not None else root / DEFAULT_STATE_DIR
    outcome, raw = _read_brief_without_following(directory / f"{session_id}.json", state_dir=directory)
    if raw is None:
        return _result(outcome, lease=active_lease)
    try:
        brief = context_session.parse_session(raw)
    except ContextError:
        return _result(STATE_BRIEF_INVALID, lease=active_lease)
    if not _renderable(brief):
        return _result(STATE_BRIEF_INVALID, lease=active_lease)
    if (
        brief["id"] != session_id
        or brief["repo"] != record["repo"]
        or brief["orchestrator"] != record["orchestrator"]
    ):
        return _result(STATE_BRIEF_MISMATCH, lease=active_lease)
    second = session_lease.observe_lease_record(root=root, now=now)
    if second["record"] is None or not _same_lease(record, second["record"]):
        return _result(STATE_LEASE_CHANGED, lease=_safe_lease(second, state=second["state"]))
    if second["state"] != session_lease.STATE_HELD:
        return _lease_result(second)
    brief["lease"] = {**second["record"], "state": session_lease.STATE_HELD, "mutating": True}
    return _result(STATE_ACTIVE, lease=_safe_lease(second, state="active"), session=brief)
