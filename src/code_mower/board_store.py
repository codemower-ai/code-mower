#!/usr/bin/env python3
"""Local metadata-only event store for Code Mower Board."""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from . import lane_status


BOARD_EVENT_SCHEMA = "code_mower.boardEvent.v1"
BOARD_EVENT_STORE_SCHEMA = "code_mower.boardEventStore.v1"
DEFAULT_RETENTION_DAYS = 14
DEFAULT_MAX_EVENTS = 500
DEFAULT_STORE_RELATIVE_PATH = Path(".code-mower") / "board" / "events.jsonl"


@dataclass(frozen=True)
class StoreWriteResult:
    path: Path
    event: dict[str, Any]
    kept: int
    pruned: int
    malformed: int


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def default_store_path(repo_path: str | Path = ".") -> Path:
    return Path(repo_path) / DEFAULT_STORE_RELATIVE_PATH


def _redact_local_paths(value: Any) -> Any:
    if isinstance(value, list):
        return [_redact_local_paths(item) for item in value]
    if not isinstance(value, dict):
        return value
    redacted: dict[str, Any] = {}
    for key, item in value.items():
        if key == "cwd" and isinstance(item, str) and item:
            redacted[key] = lane_status.LOCAL_PATH_REDACTION
            redacted["cwd_redacted"] = True
            continue
        redacted[key] = _redact_local_paths(item)
    return redacted


def _snapshot_summary(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    remote = snapshot.get("remote") if isinstance(snapshot.get("remote"), dict) else {}
    agenttrail = snapshot.get("agenttrail") if isinstance(snapshot.get("agenttrail"), dict) else {}
    local_processes = (
        snapshot.get("local_processes") if isinstance(snapshot.get("local_processes"), dict) else {}
    )
    pull_requests = remote.get("pull_requests") if isinstance(remote.get("pull_requests"), list) else []
    gate_health = remote.get("gate_health") if isinstance(remote.get("gate_health"), dict) else {}
    gate_alerts = gate_health.get("alerts") if isinstance(gate_health.get("alerts"), list) else []
    boards = agenttrail.get("boards") if isinstance(agenttrail.get("boards"), list) else []
    processes = (
        local_processes.get("processes")
        if isinstance(local_processes.get("processes"), list)
        else []
    )
    return {
        "next_action": str(snapshot.get("next_action") or "inspect"),
        "remote_available": bool(remote.get("available")),
        "open_prs": len(pull_requests),
        "gate_alerts": len(gate_alerts),
        "local_boards": len(boards),
        "local_processes": len(processes),
    }


def snapshot_event(snapshot: Mapping[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    created_at = _timestamp(now or _now())
    safe_snapshot = _redact_local_paths(copy.deepcopy(dict(snapshot)))
    board_meta = safe_snapshot.get("board") if isinstance(safe_snapshot.get("board"), dict) else {}
    return {
        "schema": BOARD_EVENT_SCHEMA,
        "type": "status_snapshot",
        "created_at": created_at,
        "repo": str(safe_snapshot.get("repo") or ""),
        "snapshot_schema": str(safe_snapshot.get("schema") or ""),
        "board_schema": str(board_meta.get("schema") or ""),
        "summary": _snapshot_summary(safe_snapshot),
        "snapshot": safe_snapshot,
    }


def _read_valid_events(path: Path) -> tuple[list[dict[str, Any]], int]:
    if not path.exists():
        return [], 0
    events: list[dict[str, Any]] = []
    malformed = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return [], 1
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if not isinstance(value, dict):
            malformed += 1
            continue
        events.append(value)
    return events, malformed


def _retained_events(
    events: list[dict[str, Any]],
    *,
    now: datetime,
    retention_days: int,
    max_events: int,
) -> list[dict[str, Any]]:
    cutoff = now - timedelta(days=retention_days)
    retained = [
        event
        for event in events
        if (created := _parse_timestamp(event.get("created_at"))) is None or created >= cutoff
    ]
    return retained[-max_events:]


def append_snapshot(
    snapshot: Mapping[str, Any],
    *,
    path: str | Path,
    now: datetime | None = None,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    max_events: int = DEFAULT_MAX_EVENTS,
) -> StoreWriteResult:
    if retention_days < 0:
        raise ValueError("retention_days must be non-negative")
    if max_events < 1:
        raise ValueError("max_events must be at least 1")
    observed_at = now or _now()
    store_path = Path(path)
    event = snapshot_event(snapshot, now=observed_at)
    existing, malformed = _read_valid_events(store_path)
    before = len(existing)
    retained = _retained_events(
        [*existing, event],
        now=observed_at,
        retention_days=retention_days,
        max_events=max_events,
    )
    store_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = store_path.with_name(f"{store_path.name}.tmp")
    tmp_path.write_text(
        "".join(json.dumps(item, sort_keys=True) + "\n" for item in retained),
        encoding="utf-8",
    )
    os.replace(tmp_path, store_path)
    pruned = before + 1 - len(retained)
    return StoreWriteResult(
        path=store_path,
        event=event,
        kept=len(retained),
        pruned=max(pruned, 0),
        malformed=malformed,
    )


def event_report(
    *,
    path: str | Path,
    limit: int = 20,
    show_store_path: bool = False,
) -> dict[str, Any]:
    store_path = Path(path)
    events, malformed = _read_valid_events(store_path)
    available = store_path.exists()
    selected = events[-limit:] if limit > 0 else []
    store: dict[str, Any] = {
        "path": str(store_path) if show_store_path else lane_status.LOCAL_PATH_REDACTION,
        "path_redacted": not show_store_path,
    }
    return {
        "schema": BOARD_EVENT_STORE_SCHEMA,
        "available": available,
        "store": store,
        "events": selected,
        "event_count": len(events),
        "malformed": malformed,
        "message": "" if available else "no local board event store yet",
    }
