#!/usr/bin/env python3
"""Local read-only Code Mower Board."""

from __future__ import annotations

import argparse
import copy
import errno
import json
import os
import re
import signal
import socket
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import metadata
from pathlib import Path
from threading import Lock, Thread
from typing import Any
from urllib.parse import urlparse

from . import __version__ as CODE_MOWER_VERSION
from . import board_store
from . import config as code_mower_config
from . import controller
from . import lane_status
from . import productivity_report
from . import reviewer_spend


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5332
BOARD_TIMELINES_SCHEMA = "code_mower.boardTimelines.v1"
BOARD_OWNER_QUEUE_SCHEMA = "code_mower.boardOwnerQueue.v1"
BOARD_AGENT_ADAPTERS_SCHEMA = "code_mower.boardAgentAdapters.v1"
BOARD_DOCTOR_SCHEMA = "code_mower.boardDoctor.v1"
BOARD_IDENTITY_SCHEMA = "code_mower.boardIdentity.v1"
BOARD_INVENTORY_SCHEMA = "code_mower.boardInventory.v1"
BOARD_STOP_SCHEMA = "code_mower.boardStop.v1"
BOARD_RELEASE_CAMPAIGNS_SCHEMA = "code_mower.boardReleaseCampaigns.v1"
DEFAULT_AGENT_ADAPTERS_RELATIVE_PATH = Path(".code-mower") / "board" / "agents"
DEFAULT_CAMPAIGNS_RELATIVE_PATH = Path(".code-mower") / "campaigns"
SECRET_VALUE_RE = re.compile(
    r"(github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{20,})"
)


@dataclass(frozen=True)
class BoardConfig:
    repo: str
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    port_was_default: bool = True
    pr_limit: int = 50
    workflow_limit: int = 20
    stale_minutes: int = 30
    refresh_seconds: int = 15
    show_local_paths: bool = False
    repo_path: str = "."
    store_path: str | None = None
    spend_path: str | None = None
    agent_adapters_path: str | None = None
    campaigns_path: str | None = None
    event_limit: int = 20
    record_events: bool = False
    record_interval_seconds: int = 60
    retention_days: int = board_store.DEFAULT_RETENTION_DAYS
    max_events: int = board_store.DEFAULT_MAX_EVENTS


def _store_path(config: BoardConfig) -> Path:
    if config.store_path:
        return Path(config.store_path)
    return board_store.default_store_path(config.repo_path)


def _spend_path(config: BoardConfig) -> Path:
    if config.spend_path:
        return Path(config.spend_path)
    return Path(config.repo_path) / reviewer_spend.DEFAULT_SPEND_PATH


def _agent_adapters_path(config: BoardConfig) -> Path:
    if config.agent_adapters_path:
        return Path(config.agent_adapters_path)
    return Path(config.repo_path) / DEFAULT_AGENT_ADAPTERS_RELATIVE_PATH


def _campaigns_path(config: BoardConfig) -> Path:
    if config.campaigns_path:
        return Path(config.campaigns_path)
    return Path(config.repo_path) / DEFAULT_CAMPAIGNS_RELATIVE_PATH


def _is_loopback(host: str) -> bool:
    return host in {"localhost", "::1"} or host.startswith("127.")


def _host_header_allowed(value: str | None) -> bool:
    if not value:
        return False
    try:
        host = urlparse(f"//{value}").hostname or ""
    except ValueError:
        return False
    return _is_loopback(host)


def _origin_header_allowed(value: str | None) -> bool:
    if not value:
        return True
    try:
        host = urlparse(value).hostname or ""
    except ValueError:
        return False
    return _is_loopback(host)


def _server_class(host: str) -> type[ThreadingHTTPServer]:
    class LocalBoardServer(ThreadingHTTPServer):
        address_family = socket.AF_INET6 if ":" in host else socket.AF_INET

    return LocalBoardServer


def _server_url(host: str, port: int) -> str:
    display_host = f"[{host}]" if ":" in host else host
    return f"http://{display_host}:{port}/"


def _candidate_ports(config: BoardConfig) -> list[int]:
    if not config.port_was_default:
        return [config.port]
    last_port = min(65535, config.port + 9)
    return list(range(config.port, last_port + 1))


def _installed_package_version() -> str:
    try:
        return metadata.version("code-mower")
    except metadata.PackageNotFoundError:
        return ""


def board_version_payload() -> dict[str, Any]:
    installed_version = _installed_package_version()
    return {
        "serving_version": CODE_MOWER_VERSION,
        "installed_version": installed_version,
        "restart_recommended": bool(installed_version and installed_version != CODE_MOWER_VERSION),
    }


def board_identity_payload(config: BoardConfig) -> dict[str, Any]:
    return {
        "schema": BOARD_IDENTITY_SCHEMA,
        "repo": config.repo,
        "board": {
            "schema": "code_mower.board.v1",
            "version": board_version_payload(),
            "local_paths": "shown" if config.show_local_paths else "redacted",
            "recording": {"enabled": config.record_events},
        },
    }


def _explicit_port_conflict_message(host: str, port: int) -> str:
    suggestions = list(range(port + 1, min(65535, port + 3) + 1))
    suggestion_text = f" such as {', '.join(str(candidate) for candidate in suggestions)}" if suggestions else ""
    return (
        f"error: Code Mower Board port {port} is already in use on {host}. "
        f"Run code-mower board list to inspect local Boards, stop a stale one with "
        f"code-mower board stop --port {port} --yes, or pass --port with a free "
        f"loopback port{suggestion_text}."
    )


def _bind_board_server(
    config: BoardConfig,
    handler: type[BaseHTTPRequestHandler],
) -> ThreadingHTTPServer | None:
    server_type = _server_class(config.host)
    tried: list[int] = []
    for port in _candidate_ports(config):
        tried.append(port)
        try:
            return server_type((config.host, port), handler)
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
            if not config.port_was_default:
                print(_explicit_port_conflict_message(config.host, port), file=sys.stderr)
                return None
    print(
        "error: Code Mower Board could not find a free loopback port in "
        f"{tried[0]}-{tried[-1]}; pass --port with a free port.",
        file=sys.stderr,
    )
    return None


def status_payload(
    config: BoardConfig,
    *,
    gh_json_runner: lane_status.GitHubJsonRunner = lane_status.run_gh_json,
    command_runner: lane_status.CommandRunner = lane_status.run_command,
) -> dict[str, Any]:
    payload = lane_status.collect_status(
        repo=config.repo,
        gh_json_runner=gh_json_runner,
        command_runner=command_runner,
        pr_limit=config.pr_limit,
        workflow_limit=config.workflow_limit,
        stale_minutes=config.stale_minutes,
        show_local_paths=config.show_local_paths,
    )
    payload["board"] = {
        "schema": "code_mower.board.v1",
        "mode": "local_recording" if config.record_events else "local_read_only",
        "version": board_version_payload(),
        "refresh_seconds": config.refresh_seconds,
        "local_paths": "shown" if config.show_local_paths else "redacted",
        "recording": {
            "enabled": config.record_events,
            "interval_seconds": config.record_interval_seconds,
        },
    }
    payload["agent_adapters"] = agent_adapters_payload(config)
    payload["release_campaigns"] = release_campaigns_payload(config)
    payload["owner_queue"] = owner_queue_payload(payload)
    payload["supervised_pilot"] = supervised_pilot_payload(
        config,
        payload,
        gh_json_runner=gh_json_runner,
    )
    payload["productivity"] = productivity_report.board_payload(
        repo=config.repo,
        repo_path=config.repo_path,
        store_path=_store_path(config),
        spend_path=_spend_path(config),
        current_status=payload,
        event_limit=config.event_limit,
    )
    return payload


def _recording_due(last_recorded_at: datetime | None, now: datetime, interval_seconds: int) -> bool:
    return last_recorded_at is None or interval_seconds <= 0 or (now - last_recorded_at).total_seconds() >= interval_seconds


def _recording_metadata(config: BoardConfig, status: str, **extra: Any) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "enabled": config.record_events,
        "interval_seconds": config.record_interval_seconds,
        "status": status,
    }
    metadata.update(extra)
    return metadata


def _recordable_payload(payload: dict[str, Any]) -> dict[str, Any]:
    snapshot = dict(payload)
    snapshot.pop("productivity", None)
    return snapshot


def _record_live_snapshot(
    payload: dict[str, Any],
    config: BoardConfig,
    *,
    now: datetime,
) -> board_store.StoreWriteResult:
    return board_store.append_snapshot(
        _recordable_payload(payload),
        path=_store_path(config),
        now=now,
        retention_days=config.retention_days,
        max_events=config.max_events,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


STATUS_CACHE_RETRY_BASE_SECONDS = 5.0
STATUS_CACHE_RETRY_MAX_SECONDS = 60.0


class StatusCache:
    """Thread-safe stale-while-refresh cache for the expensive Board status snapshot.

    ``get()`` always returns immediately: the cached snapshot (``None`` while
    cold) plus safe metadata about cache freshness. At most one background
    refresh runs at a time; a caller that finds the cache cold or stale starts
    that refresh, and concurrent callers just observe ``refresh_in_progress``.

    Every completed snapshot also bumps a monotonic integer ``generation``,
    reported alongside the snapshot in the metadata. It starts at 0 while the
    cache is cold, increments only after a refresh completes successfully, and
    is left untouched by a failed refresh or a failed refresh thread start, so
    consumers can identify *which* completed snapshot they are holding
    independently of whether that snapshot is still fresh.

    A failed refresh (or a failed refresh thread start) opens a bounded retry
    backoff window. Without it the cached snapshot stays stale while
    ``refresh_in_progress`` returns to false the instant the failure is
    recorded, so the very next request would start another expensive
    GitHub/local recomputation -- once per fast poll (~750ms) for as long as the
    failure persists. During the window ``get()`` still answers immediately from
    cold/stale metadata but starts nothing and reports ``refresh_in_progress``
    false, so the browser drops back to its normal interval. The window doubles
    per consecutive failure from ``retry_base_seconds`` up to
    ``retry_max_seconds`` and is cleared by the first success.
    """

    def __init__(
        self,
        compute: Any,
        *,
        ttl_seconds: float,
        clock: Any = time.monotonic,
        now: Any = _utc_now,
        start_thread: Any = None,
        retry_base_seconds: float = STATUS_CACHE_RETRY_BASE_SECONDS,
        retry_max_seconds: float = STATUS_CACHE_RETRY_MAX_SECONDS,
    ) -> None:
        self._compute = compute
        self._ttl_seconds = max(float(ttl_seconds), 0.0)
        self._clock = clock
        self._now = now
        self._start_thread = start_thread or self._default_start_thread
        self._retry_base_seconds = max(float(retry_base_seconds), 0.0)
        self._retry_max_seconds = max(float(retry_max_seconds), self._retry_base_seconds)
        self._lock = Lock()
        self._snapshot: dict[str, Any] | None = None
        self._computed_at: datetime | None = None
        self._computed_monotonic: float | None = None
        self._refreshing = False
        self._generation = 0
        self._last_error: str = ""
        self._last_error_at: datetime | None = None
        self._consecutive_failures = 0
        self._retry_after_monotonic: float | None = None

    @staticmethod
    def _default_start_thread(target: Any) -> None:
        Thread(target=target, daemon=True).start()

    @property
    def generation(self) -> int:
        """Monotonic count of completed snapshots; 0 while the cache is still cold.

        Only ``_refresh`` advances it, and only after ``compute()`` returned a
        snapshot, so a failed refresh leaves the generation -- and therefore the
        identity of the snapshot ``get()`` returns -- unchanged.
        """
        with self._lock:
            return self._generation

    def _retry_delay_locked(self) -> float:
        """Deterministic doubling backoff for the current failure streak, bounded by the max.

        The delay is ``retry_base_seconds`` doubled once per consecutive
        failure after the first, capped at ``retry_max_seconds``. The doubling
        is applied step by step and stops as soon as the cap is reached, so an
        arbitrarily long outage cannot overflow: evaluating
        ``retry_base_seconds * 2.0 ** consecutive_failures`` directly raises
        OverflowError once the streak passes ~1024, which for a Board left in
        persistent failure would turn every later request into a crash instead
        of a capped retry. Past the cap every further failure just returns the
        cap. A zero base (which ``__init__`` also forces when the max is zero)
        disables the backoff and always yields 0.0.
        """
        base = self._retry_base_seconds
        maximum = self._retry_max_seconds  # __init__ guarantees maximum >= base >= 0
        if base <= 0.0:
            return 0.0
        delay = base
        for _ in range(max(self._consecutive_failures - 1, 0)):
            if delay >= maximum:
                break
            delay *= 2.0
        return min(delay, maximum)

    def _record_failure_locked(self, exc: BaseException) -> None:
        """Record a safe failure summary and arm the retry backoff window.

        Callers must hold ``self._lock``. Only the exception class name is kept
        (see ``_cache_error_summary``), and the backoff deadline is monotonic,
        so nothing here can leak paths, output, or credentials.
        """
        self._refreshing = False
        self._last_error = _cache_error_summary(exc)
        self._last_error_at = self._now()
        self._consecutive_failures += 1
        self._retry_after_monotonic = self._clock() + self._retry_delay_locked()

    def get(self) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        with self._lock:
            snapshot = self._snapshot
            elapsed = self._clock()
            age = (
                max(elapsed - self._computed_monotonic, 0.0)
                if self._computed_monotonic is not None
                else None
            )
            is_stale = snapshot is None or age is None or age >= self._ttl_seconds
            retry_in = (
                max(self._retry_after_monotonic - elapsed, 0.0)
                if self._retry_after_monotonic is not None
                else 0.0
            )
            in_backoff = retry_in > 0.0
            should_start = is_stale and not self._refreshing and not in_backoff
            if should_start:
                self._refreshing = True
                self._retry_after_monotonic = None
            metadata_snapshot = {
                "state": "cold" if snapshot is None else ("stale" if is_stale else "fresh"),
                "generated_at": _format_timestamp(self._computed_at) if self._computed_at else "",
                "age_seconds": round(age, 3) if age is not None else None,
                "ttl_seconds": self._ttl_seconds,
                "generation": self._generation,
                "refresh_in_progress": self._refreshing,
                "retry_in_seconds": round(retry_in, 3) if in_backoff else None,
                "last_error": self._last_error,
                "last_error_at": _format_timestamp(self._last_error_at) if self._last_error_at else "",
            }
        if should_start:
            try:
                self._start_thread(self._refresh)
            except Exception as exc:  # noqa: BLE001 - a failed thread start must never crash the endpoint
                with self._lock:
                    self._record_failure_locked(exc)
                    metadata_snapshot["refresh_in_progress"] = False
                    metadata_snapshot["retry_in_seconds"] = round(self._retry_delay_locked(), 3)
                    metadata_snapshot["last_error"] = self._last_error
                    metadata_snapshot["last_error_at"] = _format_timestamp(self._last_error_at)
        return snapshot, metadata_snapshot

    def _refresh(self) -> None:
        try:
            result = self._compute()
        except Exception as exc:  # noqa: BLE001 - a background refresh must never crash the server
            with self._lock:
                self._record_failure_locked(exc)
            return
        with self._lock:
            self._snapshot = result
            self._computed_at = self._now()
            self._computed_monotonic = self._clock()
            self._generation += 1
            self._refreshing = False
            self._last_error = ""
            self._last_error_at = None
            self._consecutive_failures = 0
            self._retry_after_monotonic = None


def _pending_status_payload(config: BoardConfig) -> dict[str, Any]:
    """Metadata-only payload served while the status cache has no completed snapshot yet."""
    payload: dict[str, Any] = {
        "schema": lane_status.LANE_STATUS_SCHEMA,
        "repo": config.repo,
        "generated_at": "",
        "next_action": "warming first status snapshot",
        "next_detail": (
            "Code Mower Board is collecting the first GitHub and local snapshot in the "
            "background; reload shortly."
        ),
        "remote": {"available": False},
        "local_boards": {"available": False, "boards": []},
        "local_processes": {"available": False, "processes": []},
    }
    payload["board"] = {
        "schema": "code_mower.board.v1",
        "mode": "local_recording" if config.record_events else "local_read_only",
        "version": board_version_payload(),
        "refresh_seconds": config.refresh_seconds,
        "local_paths": "shown" if config.show_local_paths else "redacted",
        "recording": {"enabled": config.record_events, "interval_seconds": config.record_interval_seconds},
    }
    payload["agent_adapters"] = agent_adapters_payload(config)
    payload["owner_queue"] = owner_queue_payload(payload)
    payload["supervised_pilot"] = _supervised_disabled(
        "Board is warming its first status snapshot; supervised pilot state is not ready yet"
    )
    payload["productivity"] = productivity_report.board_payload(
        repo=config.repo,
        repo_path=config.repo_path,
        store_path=_store_path(config),
        spend_path=_spend_path(config),
        current_status=payload,
        event_limit=config.event_limit,
    )
    return payload


def _http_url(value: object) -> str:
    text = str(value or "").strip()
    if SECRET_VALUE_RE.search(text):
        return ""
    return text if text.startswith(("https://", "http://")) else ""


def _safe_text(value: object, *, limit: int = 160) -> str:
    text = " ".join(str(value or "").strip().split())
    if not text:
        return ""
    if SECRET_VALUE_RE.search(text):
        return "[redacted]"
    return text[:limit]


_SAFE_EXCEPTION_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _cache_error_summary(exc: BaseException) -> str:
    """Summarize a background refresh failure without leaking exception content.

    ``str(exc)`` can embed local filesystem paths, raw stdout/stderr, auth
    output, or secrets from whatever failed (a git command, an HTTP client,
    etc). The Board's metadata-only contract forbids all of that, so the
    cache only ever reports a stable, code-defined exception class name.
    """
    name = exc.__class__.__name__
    if not _SAFE_EXCEPTION_NAME_RE.match(name):
        name = "Exception"
    return f"status refresh failed: {name}"


def _head_prefix(value: object) -> str:
    text = str(value or "").strip()
    return "" if SECRET_VALUE_RE.search(text) else text[:12]


def _queue_base(pr: dict[str, Any], kind: str, priority: int, next_action: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "priority": priority,
        "pr_number": _int(pr.get("number")) or 0,
        "title": str(pr.get("title") or ""),
        "url": _http_url(pr.get("url")),
        "branch": str(pr.get("branch") or ""),
        "author": str(pr.get("author") or ""),
        "updated_at": str(pr.get("updated_at") or ""),
        "head_sha_prefix": _head_prefix(pr.get("head_sha")),
        "next_action": next_action,
    }


def _failing_checks(checks: object) -> list[str]:
    if not isinstance(checks, list):
        return []
    failing = []
    for check in checks:
        if not isinstance(check, dict):
            continue
        state = str(check.get("state") or "").lower()
        if state in {"failure", "failed", "error", "timed_out", "cancelled"}:
            failing.append(str(check.get("name") or "check"))
    return failing[:4]


def owner_queue_payload(status: dict[str, Any]) -> dict[str, Any]:
    remote = status.get("remote") if isinstance(status.get("remote"), dict) else {}
    if not remote.get("available"):
        return {
            "schema": BOARD_OWNER_QUEUE_SCHEMA,
            "available": False,
            "count": 0,
            "entries": [],
            "message": "GitHub unavailable; owner queue cannot inspect PR labels",
        }
    entries: list[dict[str, Any]] = []
    prs = remote.get("pull_requests") if isinstance(remote.get("pull_requests"), list) else []
    for pr in prs:
        if not isinstance(pr, dict):
            continue
        labels = pr.get("labels") if isinstance(pr.get("labels"), dict) else {}
        needs = [label for label in labels.get("needs") or [] if isinstance(label, str)]
        blocked = [label for label in labels.get("blocked") or [] if isinstance(label, str)]
        if any(label == "needs-owner" for label in needs):
            item = _queue_base(pr, "needs-owner", 0, str(pr.get("next_action") or "owner decision"))
            item["labels"] = [label for label in needs if label == "needs-owner"]
            entries.append(item)
        if blocked:
            item = _queue_base(pr, "blocked-audit", 0, "fix BLOCKED audit")
            item["labels"] = blocked
            entries.append(item)
        failing = _failing_checks(pr.get("checks"))
        if failing:
            item = _queue_base(pr, "failing-check", 1, "fix failing check")
            item["checks"] = failing
            entries.append(item)
        if pr.get("stale"):
            entries.append(_queue_base(pr, "stale-gate", 1, "rerun gate or inspect stuck audit"))
        merge_state = str(pr.get("merge_state") or "")
        if merge_state in {"BEHIND", "DIRTY"}:
            entries.append(_queue_base(pr, "rebase-needed", 1, "rebase/behind"))
        if pr.get("is_draft"):
            entries.append(_queue_base(pr, "draft", 2, "finish draft PR"))
    entries.sort(key=lambda item: (item["priority"], item["pr_number"], item["kind"]))
    return {
        "schema": BOARD_OWNER_QUEUE_SCHEMA,
        "available": True,
        "count": len(entries),
        "entries": entries,
        "message": "" if entries else "no owner queue items",
    }


def _supervised_disabled(message: str, *, cycle_state: str = "unavailable") -> dict[str, Any]:
    return {
        "schema": controller.SUPERVISED_PILOT_SCHEMA,
        "enabled": False,
        "cycle_state": cycle_state,
        "controller_mode": "dry_run",
        "decision": {},
        "queue": {"active_lanes": {}, "metrics": {}, "ready_issue_errors": []},
        "active_issues": [],
        "active_prs": [],
        "message": message,
    }


def _supervised_cycle_state(decision_state: object) -> str:
    state = str(decision_state or "")
    if state == "no_work":
        return "idle"
    if state == "dispatch_builder":
        return "dispatch"
    if state == "ready_to_merge":
        return "ready"
    if state == "owner_action":
        return "owner_action"
    if state in {"blocked_audit", "failing_check", "not_mergeable"}:
        return "blocked"
    if state in {"stale_evidence", "draft_pr", "waiting_for_evidence"}:
        return "waiting"
    return "unknown"


def _safe_bool(value: object) -> bool:
    return bool(value)


def _safe_reviewer_outcomes(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    outcomes = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        outcomes.append(
            {
                "lane_id": _safe_text(item.get("lane_id"), limit=60),
                "config_lane_id": _safe_text(item.get("config_lane_id"), limit=80),
                "verdict": _safe_text(item.get("verdict"), limit=20),
                "promoted": _safe_bool(item.get("promoted")),
            }
        )
    return outcomes


def _supervised_decision_payload(decision: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "decision_state": _safe_text(decision.get("decision_state"), limit=80),
        "next_action": _safe_text(decision.get("next_action"), limit=160),
        "next_detail": _safe_text(decision.get("next_detail"), limit=220),
        "stop_condition": _safe_text(decision.get("stop_condition"), limit=80),
        "owner_action_kind": _safe_text(decision.get("owner_action_kind"), limit=80),
        "lane_id": _safe_text(decision.get("lane_id"), limit=60),
        "gate_status": _safe_text(decision.get("gate_status"), limit=40),
        "branch": _safe_text(decision.get("branch"), limit=160),
        "author": _safe_text(decision.get("author"), limit=80),
        "head_sha_prefix": _head_prefix(decision.get("head_sha_prefix")),
        "merge_method": _safe_text(decision.get("merge_method"), limit=40),
        "author_lane_excluded": _safe_bool(decision.get("author_lane_excluded")),
        "promoted_reviewers_passed": _safe_bool(decision.get("promoted_reviewers_passed")),
        "would_mutate": _safe_bool(decision.get("would_mutate")),
        "reviewer_outcomes": _safe_reviewer_outcomes(decision.get("reviewer_outcomes")),
    }
    if pr_number := _int(decision.get("pr_number")):
        payload["pr_number"] = pr_number
    if issue_number := _int(decision.get("issue_number")):
        payload["issue_number"] = issue_number
    if pr_url := _http_url(decision.get("pr_url")):
        payload["pr_url"] = pr_url
    if issue_url := _http_url(decision.get("issue_url")):
        payload["issue_url"] = issue_url
    return {key: value for key, value in payload.items() if value not in (None, "", [])}


def _supervised_pr_payload(pr: Mapping[str, Any]) -> dict[str, Any]:
    labels = pr.get("labels") if isinstance(pr.get("labels"), Mapping) else {}
    payload: dict[str, Any] = {
        "number": _int(pr.get("number")) or 0,
        "title": _safe_text(pr.get("title"), limit=180),
        "url": _http_url(pr.get("url")),
        "branch": _safe_text(pr.get("branch"), limit=160),
        "author": _safe_text(pr.get("author"), limit=80),
        "updated_at": _safe_text(pr.get("updated_at"), limit=80),
        "head_sha_prefix": _head_prefix(pr.get("head_sha")),
        "merge_state": _safe_text(pr.get("merge_state"), limit=40),
        "is_draft": _safe_bool(pr.get("is_draft")),
        "stale": _safe_bool(pr.get("stale")),
        "next_action": _safe_text(pr.get("next_action"), limit=160),
        "next_detail": _safe_text(pr.get("next_detail"), limit=220),
        "labels": labels,
    }
    return {key: value for key, value in payload.items() if value not in (None, "", [])}


def _supervised_issue_payload(issue: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "number": _int(issue.get("number")) or 0,
        "url": _http_url(issue.get("url")),
        "author": _safe_text(issue.get("author"), limit=80),
        "updated_at": _safe_text(issue.get("updated_at"), limit=80),
        "builder_lane": _safe_text(issue.get("builder_lane"), limit=60),
        "assigned": _safe_bool(issue.get("assigned")),
        "dispatched": _safe_bool(issue.get("dispatched")),
        "owner_action": _safe_bool(issue.get("owner_action")),
        "labels": [label for label in issue.get("labels") or [] if isinstance(label, str)][:20],
    }
    return {key: value for key, value in payload.items() if value not in (None, "", [])}


def supervised_pilot_payload(
    config: BoardConfig,
    status: dict[str, Any],
    *,
    gh_json_runner: lane_status.GitHubJsonRunner = lane_status.run_gh_json,
) -> dict[str, Any]:
    config_path = Path(config.repo_path) / "code-mower.yml"
    if not config_path.is_file():
        return _supervised_disabled("code-mower.yml not found; supervised pilot state is unavailable")
    try:
        raw_config = code_mower_config.load_config(config_path)
        issues = code_mower_config.validate_config(raw_config)
    except (OSError, code_mower_config.ConfigError, ValueError):
        return _supervised_disabled("could not read Code Mower config; run code-mower config validate")
    if issues:
        return _supervised_disabled("Code Mower config is invalid; run code-mower config validate")

    remote = status.get("remote") if isinstance(status.get("remote"), Mapping) else {}
    if remote.get("available"):
        ready_issues = controller._collect_ready_issues(  # noqa: SLF001 - shared package policy surface for Board.
            repo=config.repo,
            config=raw_config,
            gh_json_runner=gh_json_runner,
            issue_limit=min(config.pr_limit, 50),
        )
    else:
        ready_issues = {"available": False, "errors": ["remote unavailable"], "issues": []}

    report = controller.evaluate_controller_report(
        status_report=status,
        ready_issues=ready_issues,
        config=raw_config,
        options=controller.ControllerOptions(repo=config.repo, mode="dry_run", issue_limit=min(config.pr_limit, 50)),
    )
    decision = report.get("decision") if isinstance(report.get("decision"), Mapping) else {}
    queue = report.get("queue") if isinstance(report.get("queue"), Mapping) else {}
    raw_prs = remote.get("pull_requests") if isinstance(remote.get("pull_requests"), list) else []
    issue_payload = ready_issues.get("issues") if isinstance(ready_issues.get("issues"), list) else []
    return {
        "schema": controller.SUPERVISED_PILOT_SCHEMA,
        "enabled": True,
        "cycle_state": _supervised_cycle_state(decision.get("decision_state")),
        "controller_mode": report.get("mode") or "dry_run",
        "generated_at": report.get("generated_at") or "",
        "decision": _supervised_decision_payload(decision),
        "queue": queue,
        "active_issues": [
            _supervised_issue_payload(issue)
            for issue in issue_payload
            if isinstance(issue, Mapping)
        ],
        "active_prs": [
            _supervised_pr_payload(pr)
            for pr in raw_prs
            if isinstance(pr, Mapping)
        ],
        "message": "",
    }


def _verdict_from_done_label(label: str) -> tuple[str, str] | None:
    if label.endswith("-done"):
        return label[: -len("-done")], "PASS"
    return None


def _verdict_from_blocked_label(label: str) -> tuple[str, str] | None:
    if label.endswith("-blocked"):
        return label[: -len("-blocked")], "BLOCKED"
    return None


def _verdict_timeline(events: list[dict[str, Any]], *, limit: int) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str, str]] = set()
    for event in reversed(events):
        snapshot = event.get("snapshot") if isinstance(event.get("snapshot"), dict) else {}
        remote = snapshot.get("remote") if isinstance(snapshot.get("remote"), dict) else {}
        prs = remote.get("pull_requests") if isinstance(remote.get("pull_requests"), list) else []
        for pr in prs:
            if not isinstance(pr, dict):
                continue
            labels = pr.get("labels") if isinstance(pr.get("labels"), dict) else {}
            label_verdicts: list[tuple[str, str]] = []
            for label in labels.get("done") or []:
                if isinstance(label, str) and (verdict := _verdict_from_done_label(label)):
                    label_verdicts.append(verdict)
            for label in labels.get("blocked") or []:
                if isinstance(label, str) and (verdict := _verdict_from_blocked_label(label)):
                    label_verdicts.append(verdict)
            for lane, verdict in label_verdicts:
                pr_number = _int(pr.get("number")) or 0
                head_sha_prefix = _head_prefix(pr.get("head_sha"))
                key = (lane, pr_number, head_sha_prefix, verdict)
                if key in seen:
                    continue
                seen.add(key)
                entries.append(
                    {
                        "created_at": str(event.get("created_at") or ""),
                        "lane": lane,
                        "pr_number": pr_number,
                        "head_sha_prefix": head_sha_prefix,
                        "verdict": verdict,
                        "url": _http_url(pr.get("url")),
                    }
                )
                if len(entries) >= limit:
                    return {"available": bool(entries), "entries": entries, "message": ""}
    return {
        "available": bool(entries),
        "entries": entries,
        "message": "" if entries else "no local reviewer verdict history yet",
    }


def _float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace("$", ""))
        except ValueError:
            return None
    return None


def _int(value: object) -> int | None:
    number = _float(value)
    if number is None:
        return None
    try:
        return int(number)
    except (OverflowError, ValueError):
        return None


def _positive_pid(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value.strip()):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _adapter_items(raw: object) -> list[Mapping[str, Any]]:
    if isinstance(raw, Mapping):
        agents = raw.get("agents")
        if isinstance(agents, list):
            return [item for item in agents if isinstance(item, Mapping)]
        return [raw]
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, Mapping)]
    return []


def _adapter_card(
    raw: Mapping[str, Any],
    *,
    source_file: str,
    show_local_paths: bool,
) -> dict[str, Any]:
    card: dict[str, Any] = {
        "source_file": source_file,
        "provider": _safe_text(raw.get("provider") or raw.get("agent") or raw.get("name"), limit=40) or "unknown",
        "role": _safe_text(raw.get("role"), limit=40) or "agent",
        "status": _safe_text(raw.get("status"), limit=40) or "unknown",
    }
    optional_text_fields = {
        "lane": 60,
        "label": 80,
        "repo": 120,
        "branch": 120,
        "title": 160,
        "next_action": 160,
        "started_at": 80,
        "updated_at": 80,
    }
    for field, limit in optional_text_fields.items():
        value = _safe_text(raw.get(field), limit=limit)
        if value:
            card[field] = value
    for field in ("pr_number", "issue_number", "pid"):
        value = _int(raw.get(field))
        if value is not None:
            card[field] = value
    if url := _http_url(raw.get("url")):
        card["url"] = url
    if head_sha := _head_prefix(raw.get("head_sha")):
        card["head_sha_prefix"] = head_sha
    if cwd := _safe_text(raw.get("cwd"), limit=240):
        if show_local_paths:
            card["cwd"] = cwd
        else:
            card["cwd"] = lane_status.LOCAL_PATH_REDACTION
            card["cwd_redacted"] = True
    return card


def agent_adapters_payload(
    config: BoardConfig,
    *,
    pid_alive: Callable[[int], bool] | None = None,
) -> dict[str, Any]:
    path = _agent_adapters_path(config)
    payload: dict[str, Any] = {
        "schema": BOARD_AGENT_ADAPTERS_SCHEMA,
        "available": True,
        "path": lane_status.LOCAL_PATH_REDACTION,
        "path_redacted": True,
        "path_exists": path.exists(),
        "agents": [],
        "warnings": [],
        "stale_cards": 0,
        "message": "no local agent adapter files found",
    }
    if not path.exists():
        return payload
    if not path.is_dir():
        payload["warnings"].append({"file": "", "message": "agent adapter path is not a directory"})
        payload["message"] = "could not read local agent adapter files"
        return payload
    probe = pid_alive or _default_pid_alive
    for adapter_file in sorted(path.glob("*.json"))[:50]:
        try:
            raw = json.loads(adapter_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            payload["warnings"].append({"file": adapter_file.name, "message": "could not parse agent adapter file"})
            continue
        cards = [
            _adapter_card(item, source_file=adapter_file.name, show_local_paths=config.show_local_paths)
            for item in _adapter_items(raw)
        ]
        if not cards:
            payload["warnings"].append({"file": adapter_file.name, "message": "agent adapter file had no cards"})
            continue
        for card in cards:
            pid = card.get("pid")
            # Safely ignore stale launcher metadata: a card whose process is
            # gone is marked stale instead of being treated as a live agent.
            if isinstance(pid, int) and not probe(pid):
                card["stale"] = True
                payload["stale_cards"] += 1
        payload["agents"].extend(cards)
    payload["message"] = "" if payload["agents"] else "no local agent adapter cards found"
    return payload


def prune_stale_agent_adapters(
    adapters_path: str | Path,
    *,
    pid_alive: Callable[[int], bool] | None = None,
) -> dict[str, Any]:
    """Delete only Code Mower-owned stale launcher metadata files.

    Every `*.json` file directly inside the agent-adapters directory is a
    candidate, and only when every pid-bearing card it contains refers to a
    process that is gone. Files with live pids, files without pid cards, and
    anything outside the directory are never touched. A symlinked adapters
    directory is refused before any listing or deletion.
    """

    probe = pid_alive or _default_pid_alive
    result: dict[str, Any] = {"pruned": [], "kept": [], "errors": []}
    directory = Path(adapters_path)
    try:
        if directory.is_symlink():
            result["errors"].append(
                {"file": "", "message": "refusing to prune agent adapter files through a symlink"}
            )
            return result
    except OSError:
        result["errors"].append({"file": "", "message": "could not list agent adapter files"})
        return result
    try:
        candidates = sorted(directory.glob("*.json"))
    except OSError:
        result["errors"].append({"file": "", "message": "could not list agent adapter files"})
        return result
    for adapter_file in candidates:
        try:
            raw = json.loads(adapter_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            result["kept"].append(adapter_file.name)
            continue
        pids = []
        invalid_pid = False
        for item in _adapter_items(raw):
            if "pid" not in item:
                continue
            parsed_pid = _positive_pid(item.get("pid"))
            if parsed_pid is None:
                invalid_pid = True
                break
            if parsed_pid is not None:
                pids.append(parsed_pid)
        if invalid_pid or not pids or any(probe(pid) for pid in pids):
            result["kept"].append(adapter_file.name)
            continue
        try:
            adapter_file.unlink()
        except OSError:
            result["errors"].append({"file": adapter_file.name, "message": "could not delete stale agent adapter file"})
        else:
            result["pruned"].append(adapter_file.name)
    return result


def release_campaigns_payload(config: BoardConfig) -> dict[str, Any]:
    try:
        from . import release_campaigns
    except ImportError:
        import release_campaigns  # type: ignore

    return release_campaigns.release_campaigns_board_payload(
        repo_path=config.repo_path,
        campaigns_dir=_campaigns_path(config),
    )


def _spend_timeline(config: BoardConfig, *, limit: int) -> dict[str, Any]:
    path = _spend_path(config)
    try:
        payload = reviewer_spend.load_spend_file(path)
        raw_runs = payload.get("runs", [])
        if raw_runs is None:
            raw_runs = []
        if not isinstance(raw_runs, list):
            raise ValueError("reviewer spend runs must be a list")
    except ValueError:
        return {
            "available": False,
            "path": lane_status.LOCAL_PATH_REDACTION,
            "path_redacted": True,
            "message": "could not read reviewer spend file",
            "groups": [],
            "recent_runs": [],
            "skipped_rows": 0,
            "filtered_rows": 0,
        }

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    recent: list[dict[str, Any]] = []
    skipped = 0
    filtered = 0
    for raw_run in raw_runs:
        if not isinstance(raw_run, dict):
            skipped += 1
            continue
        lane = str(raw_run.get("lane") or "").strip()
        verdict = str(raw_run.get("verdict") or "").strip().upper()
        repo = str(raw_run.get("repo") or "").strip()
        pr_number = _int(raw_run.get("pr_number"))
        if repo and repo != config.repo:
            filtered += 1
            continue
        if not lane or not verdict or pr_number is None:
            skipped += 1
            continue
        wall_seconds = _float(raw_run.get("wall_seconds"))
        cost_usd = _float(raw_run.get("cost_usd"))
        total_tokens = _int(raw_run.get("total_tokens"))
        group = groups.setdefault(
            (lane, verdict),
            {
                "lane": lane,
                "verdict": verdict,
                "runs": 0,
                "wall_seconds_total": 0.0,
                "wall_seconds_avg": None,
                "cost_usd_total": 0.0,
                "total_tokens": 0,
            },
        )
        group["runs"] += 1
        if wall_seconds is not None:
            group["wall_seconds_total"] += wall_seconds
            group["wall_seconds_avg"] = group["wall_seconds_total"] / group["runs"]
        if cost_usd is not None:
            group["cost_usd_total"] += cost_usd
        if total_tokens is not None:
            group["total_tokens"] += total_tokens
        recent.append(
            {
                "created_at": str(raw_run.get("created_at") or ""),
                "lane": lane,
                "pr_number": pr_number,
                "head_sha_prefix": _head_prefix(raw_run.get("head_sha")),
                "verdict": verdict,
                "model": str(raw_run.get("model") or ""),
                "wall_seconds": wall_seconds,
                "cost_usd": cost_usd,
                "total_tokens": total_tokens,
            }
        )

    normalized_groups = []
    for group in groups.values():
        if group["wall_seconds_avg"] is not None:
            group["wall_seconds_avg"] = round(group["wall_seconds_avg"], 3)
        group["wall_seconds_total"] = round(group["wall_seconds_total"], 3)
        group["cost_usd_total"] = round(group["cost_usd_total"], 6)
        normalized_groups.append(group)
    recent.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    available = path.is_file()
    message = ""
    if not available:
        message = "no reviewer spend file yet"
    elif not normalized_groups and not skipped and not filtered:
        message = "no reviewer spend rows for this repo yet"
    return {
        "available": available,
        "path": lane_status.LOCAL_PATH_REDACTION,
        "path_redacted": True,
        "message": message,
        "groups": sorted(normalized_groups, key=lambda item: (item["lane"], item["verdict"])),
        "recent_runs": recent[:limit],
        "skipped_rows": skipped,
        "filtered_rows": filtered,
    }


def timelines_payload(
    config: BoardConfig,
    *,
    limit: int | None = None,
    event_report_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event_limit = limit if limit is not None else config.event_limit
    report = event_report_payload or board_store.event_report(path=_store_path(config), limit=event_limit)
    events = report.get("events") if isinstance(report.get("events"), list) else []
    return {
        "schema": BOARD_TIMELINES_SCHEMA,
        "verdicts": _verdict_timeline([event for event in events if isinstance(event, dict)], limit=event_limit),
        "spend": _spend_timeline(config, limit=event_limit),
        "source": {
            "events_available": bool(report.get("available")),
            "events_message": str(report.get("message") or ""),
        },
    }


def render_board_html(config: BoardConfig) -> str:
    repo_json = json.dumps(config.repo).replace("</", "<\\/")
    refresh_json = json.dumps(config.refresh_seconds * 1000)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Code Mower Board</title>
  <style>
    :root {{ color-scheme: light; --bg:#f7f8f5; --ink:#1d2520; --muted:#66736b; --line:#d8ded7; --ok:#137a42; --warn:#9a5b00; --bad:#aa2e25; --panel:#ffffff; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--bg); color: var(--ink); font: 14px/1.45 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    header {{ display:flex; align-items:flex-end; justify-content:space-between; gap:24px; padding:24px 32px 18px; border-bottom:1px solid var(--line); background:var(--panel); }}
    h1 {{ margin:0; font-size:24px; font-weight:720; letter-spacing:0; }}
    h2 {{ margin:0 0 10px; font-size:15px; letter-spacing:0; }}
    main {{ max-width:1180px; margin:0 auto; padding:24px 20px 40px; display:grid; gap:18px; }}
    .summary {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(170px, 1fr)); gap:10px; }}
    .metric, section {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px; }}
    .metric b {{ display:block; font-size:20px; margin-top:4px; }}
    .muted {{ color:var(--muted); }}
    .rows {{ display:grid; gap:10px; }}
    .row {{ border-top:1px solid var(--line); padding-top:10px; }}
    .row:first-child {{ border-top:0; padding-top:0; }}
    .line {{ display:flex; flex-wrap:wrap; gap:8px 14px; align-items:center; }}
    .pill {{ border:1px solid var(--line); border-radius:999px; padding:2px 8px; color:var(--muted); white-space:nowrap; }}
    .ok {{ color:var(--ok); }} .warn {{ color:var(--warn); }} .bad {{ color:var(--bad); }}
    code {{ background:#eef2ec; border-radius:4px; padding:1px 4px; }}
    a {{ color:#145ea8; text-decoration:none; }} a:hover {{ text-decoration:underline; }}
  </style>
</head>
<body>
  <header>
    <div><h1>Code Mower Board</h1><div class="muted" id="repo"></div><div class="muted" id="version"></div></div>
    <div class="muted" id="generated">Loading...</div>
  </header>
  <main>
    <div class="summary" id="summary"></div>
    <section><h2>Supervised Pilot</h2><div class="rows" id="supervised"></div></section>
    <section><h2>Productivity</h2><div class="rows" id="productivity"></div></section>
    <section><h2>Owner Queue</h2><div class="rows" id="owner"></div></section>
    <section><h2>Agent Cards</h2><div class="rows" id="agents"></div></section>
    <section><h2>Release Campaigns</h2><div class="rows" id="campaigns"></div></section>
    <section><h2>Open PRs</h2><div class="rows" id="prs"></div></section>
    <section><h2>Gate Alerts</h2><div class="rows" id="alerts"></div></section>
    <section><h2>Recent Code Mower Workflows</h2><div class="rows" id="runs"></div></section>
    <section><h2>Recent Local History</h2><div class="rows" id="history"></div></section>
    <section><h2>Reviewer Verdict Timeline</h2><div class="rows" id="verdicts"></div></section>
    <section><h2>Spend And Latency</h2><div class="rows" id="spend"></div></section>
    <section><h2>Local Activity</h2><div class="rows" id="local"></div></section>
  </main>
  <script>
    const REPO = {repo_json};
    const REFRESH_MS = {refresh_json};
    const FAST_POLL_MS = 750;
    const FAST_POLL_MAX_ATTEMPTS = 20;
    // Floor for a TTL-derived delay: a snapshot that is fresh by a hair must
    // not spin load() in a zero-delay loop.
    const MIN_POLL_MS = 250;
    // The server answers a cold cache with a metadata-only payload and a stale
    // cache with the previous snapshot; while a background refresh is actually
    // running, wait ~750ms for it instead of a full REFRESH_MS tick. Both
    // states also occur with no refresh in flight -- the refresh thread failed
    // to start, or a failed refresh armed the server's retry backoff -- and
    // then nothing is coming, so fast polling would only burn requests.
    const awaitingRefresh = (cache) => (cache?.state === "cold" || cache?.state === "stale") && cache?.refresh_in_progress === true;
    // Only real JSON numbers are trusted. null, "", a numeric string, or a
    // missing key must fall back to the configured interval rather than coerce
    // to 0 and schedule a burst of pointless requests.
    const finiteNumber = (value) => (typeof value === "number" && Number.isFinite(value) ? value : null);
    // Cache age starts when a background refresh completes, not when the page
    // loaded, so a browser-fixed interval drifts out of phase with the TTL: a
    // tick can land just under the TTL, see the same fresh snapshot, and only
    // pick up the next one a full interval later -- an update every ~two
    // intervals. A fresh response therefore schedules itself near its own
    // remaining TTL. null means "no usable metadata": use the normal interval.
    const freshDelayMs = (cache) => {{
      if (cache?.state !== "fresh") return null;
      const ttl = finiteNumber(cache?.ttl_seconds);
      const age = finiteNumber(cache?.age_seconds);
      if (ttl === null || age === null || ttl <= 0 || age < 0) return null;
      return Math.min(Math.max((ttl - age) * 1000, MIN_POLL_MS), REFRESH_MS);
    }};
    const text = (value) => String(value ?? "");
    const esc = (value) => text(value).replace(/[&<>"']/g, c => ({{"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#39;"}}[c]));
    const put = (id, html) => document.getElementById(id).innerHTML = html;
    const pill = (value) => `<span class="pill">${{esc(value)}}</span>`;
    const empty = (message) => `<div class="muted">${{esc(message)}}</div>`;
    const href = (value) => /^https?:\\/\\//i.test(text(value)) ? text(value) : "#";
    const stateClass = (value) => /fail|error|blocked/i.test(text(value)) ? "bad" : /warn|pending|waiting|queued|progress/i.test(text(value)) ? "warn" : "ok";
    const display = (value) => value === null || value === undefined || value === "" ? "unknown" : text(value);
    const seconds = (value) => Number.isFinite(Number(value)) ? `${{Number(value).toFixed(1)}}s` : "n/a";
    const money = (value) => Number.isFinite(Number(value)) ? `$${{Number(value).toFixed(3)}}` : "n/a";
    const localTime = (value) => {{
      const raw = text(value);
      if (!raw) return "";
      const date = new Date(raw);
      if (Number.isNaN(date.getTime())) return esc(raw);
      const local = new Intl.DateTimeFormat(undefined, {{year:"numeric", month:"short", day:"numeric", hour:"numeric", minute:"2-digit", second:"2-digit", timeZoneName:"short"}}).format(date);
      return `<time datetime="${{esc(raw)}}" title="UTC ${{esc(raw)}}">${{esc(local)}}</time>`;
    }};
    function labels(groups) {{
      return Object.values(groups || {{}}).flat().map(pill).join(" ") || '<span class="muted">none</span>';
    }}
    function checks(list) {{
      return (list || []).map(c => `<span class="${{stateClass(c.state)}}">${{esc(c.name)}}=${{esc(c.state)}}</span>`).join(", ") || '<span class="muted">none</span>';
    }}
    function render(data) {{
      document.getElementById("repo").textContent = REPO;
      const version = data.board?.version || {{}};
      const servingVersion = version.serving_version || "unknown";
      const installedVersion = version.installed_version || servingVersion;
      document.getElementById("version").textContent = version.restart_recommended
        ? `serving ${{servingVersion}}; installed ${{installedVersion}} available after restart`
        : `serving ${{servingVersion}}`;
      document.getElementById("generated").innerHTML = data.generated_at ? `Generated ${{localTime(data.generated_at)}}` : "Loading...";
      const prs = data.remote?.pull_requests || [];
      const runs = data.remote?.workflow_runs || [];
      const alerts = data.remote?.gate_health?.alerts || [];
      const ownerQueue = data.owner_queue?.entries || [];
      const agentCards = data.agent_adapters?.agents || [];
      const supervised = data.supervised_pilot || {{}};
      const supervisedDecision = supervised.decision || {{}};
      const supervisedQueue = supervised.queue || {{}};
      const supervisedMetrics = supervisedQueue.metrics || {{}};
      const supervisedPRs = supervised.active_prs || [];
      const supervisedIssues = supervised.active_issues || [];
      const timelines = data.timelines || {{}};
      const verdicts = timelines.verdicts?.entries || [];
      const spend = timelines.spend || {{}};
      const spendGroups = spend.groups || [];
      const productivity = data.productivity || {{}};
      const productivityMetrics = productivity.metrics || {{}};
      const productivityCurrent = productivity.current || {{}};
      const productivityWindow = productivity.window?.local_history || {{}};
      const productivitySpend = productivity.spend || {{}};
      const productivityQuality = productivity.quality || {{}};
      put("summary", [
        `<div class="metric"><span class="muted">Next action</span><b>${{esc(data.next_action || "inspect")}}</b></div>`,
        data.next_detail ? `<div class="metric"><span class="muted">Detail</span><b>${{esc(data.next_detail)}}</b></div>` : "",
        `<div class="metric"><span class="muted">GitHub</span><b class="${{data.remote?.available ? "ok" : "warn"}}">${{data.remote?.available ? "available" : "unavailable"}}</b></div>`,
        `<div class="metric"><span class="muted">Open PRs</span><b>${{prs.length}}</b></div>`,
        `<div class="metric"><span class="muted">Pilot</span><b class="${{stateClass(supervised.cycle_state || "")}}">${{esc(supervised.cycle_state || "off")}}</b></div>`,
        `<div class="metric"><span class="muted">Productivity</span><b class="${{stateClass(productivity.status || "")}}">${{esc(productivity.status || "unknown")}}</b></div>`,
        `<div class="metric"><span class="muted">Owner queue</span><b class="${{ownerQueue.length ? "warn" : "ok"}}">${{ownerQueue.length}}</b></div>`,
        `<div class="metric"><span class="muted">Agent cards</span><b>${{agentCards.length}}</b></div>`,
        `<div class="metric"><span class="muted">Campaigns</span><b class="${{(data.release_campaigns?.campaigns || []).length ? "ok" : "muted"}}">${{(data.release_campaigns?.campaigns || []).length}}</b></div>`,
        `<div class="metric"><span class="muted">Gate alerts</span><b class="${{alerts.length ? "warn" : "ok"}}">${{alerts.length}}</b></div>`
      ].join(""));
      const reviewerOutcomes = supervisedDecision.reviewer_outcomes || [];
      const supervisedRows = supervised.enabled ? [
        `<div class="row"><div class="line"><b class="${{stateClass(supervised.cycle_state)}}">${{esc(supervised.cycle_state || "unknown")}}</b>${{pill(supervised.controller_mode || "dry_run")}}${{supervisedDecision.decision_state ? pill(supervisedDecision.decision_state) : ""}}</div><div>next: <b>${{esc(supervisedDecision.next_action || "inspect")}}</b></div>${{supervisedDecision.next_detail ? `<div class="muted">${{esc(supervisedDecision.next_detail)}}</div>` : ""}}</div>`,
        `<div class="row"><div class="line">${{pill(`open PRs ${{supervisedMetrics.open_pr_count ?? supervisedPRs.length}}`)}}${{pill(`ready issues ${{supervisedMetrics.ready_issue_count ?? supervisedIssues.length}}`)}}${{pill(`active lanes ${{supervisedMetrics.active_lane_count ?? 0}}`)}}${{pill(`stale ${{supervisedMetrics.stale_evidence_count ?? 0}}`)}}</div></div>`,
        supervisedDecision.pr_number ? `<div class="row"><div class="line"><a href="${{esc(href(supervisedDecision.pr_url))}}">Selected PR #${{esc(supervisedDecision.pr_number)}}</a>${{supervisedDecision.lane_id ? pill(supervisedDecision.lane_id) : ""}}${{supervisedDecision.gate_status ? pill(`gate ${{supervisedDecision.gate_status}}`) : ""}}${{supervisedDecision.author_lane_excluded ? pill("author excluded") : ""}}</div><div class="muted">${{esc(supervisedDecision.branch || "")}}${{supervisedDecision.head_sha_prefix ? ` @ ${{esc(supervisedDecision.head_sha_prefix)}}` : ""}}</div></div>` : "",
        supervisedDecision.issue_number ? `<div class="row"><div class="line"><a href="${{esc(href(supervisedDecision.issue_url))}}">Selected issue #${{esc(supervisedDecision.issue_number)}}</a>${{supervisedDecision.lane_id ? pill(supervisedDecision.lane_id) : ""}}</div></div>` : "",
        reviewerOutcomes.length ? `<div class="row"><b>Reviewer Evidence</b><div class="muted">${{reviewerOutcomes.map(outcome => `${{esc(outcome.lane_id || outcome.config_lane_id)}}=${{esc(outcome.verdict)}}`).join(", ")}}</div></div>` : "",
        supervisedIssues.length ? `<div class="row"><b>Ready Issues</b><div class="muted">${{supervisedIssues.slice(0, 5).map(issue => `#${{esc(issue.number)}} ${{esc(issue.builder_lane || "")}}`).join(", ")}}</div></div>` : "",
        supervisedPRs.length ? `<div class="row"><b>Active PRs</b><div class="muted">${{supervisedPRs.slice(0, 5).map(pr => `#${{esc(pr.number)}} ${{esc(pr.merge_state || "")}}${{pr.stale ? " stale" : ""}}${{pr.is_draft ? " draft" : ""}}`).join(", ")}}</div></div>` : ""
      ].filter(Boolean).join("") : empty(supervised.message || "Supervised pilot state unavailable.");
      put("supervised", supervisedRows || empty(supervised.message || "No supervised pilot activity."));
      const productivityRows = [
        `<div class="row"><div>next: <b>${{esc(productivity.next_action || "inspect")}}</b></div></div>`,
        `<div class="row"><b>Current</b><div class="line">${{pill(`open PRs ${{display(productivityCurrent.open_pr_count)}}`)}}${{pill(`active lanes ${{display(productivityCurrent.active_lane_count)}}`)}}${{pill(`blocked ${{display(productivityCurrent.blocked_pr_count)}}`)}}${{pill(`owner actions ${{display(productivityCurrent.owner_action_count)}}`)}}</div></div>`,
        `<div class="row"><b>Throughput</b><div class="line">${{pill(`merged ${{display(productivityMetrics.merged_pr_count)}}`)}}${{pill(`cycle ${{seconds(productivityMetrics.cycle_time_seconds)}}`)}}${{pill(`active ${{seconds(productivityMetrics.active_time_seconds)}}`)}}${{pill(`wait ${{seconds(productivityMetrics.wait_time_seconds)}}`)}}</div><div class="muted">local window ${{display(productivityWindow.start)}} to ${{display(productivityWindow.end)}} (${{seconds(productivityWindow.duration_seconds)}})</div></div>`,
        `<div class="row"><b>Quality</b><div class="line">${{pill(`reviews ${{display(productivityMetrics.reviewer_run_count)}}`)}}${{pill(`PASS ${{display(productivityQuality.audit_pass_count)}}`)}}${{pill(`BLOCKED ${{display(productivityQuality.audit_blocked_count)}}`)}}${{pill(`catches ${{display(productivityQuality.reviewer_catch_count)}}`)}}${{pill(`fix rounds ${{display(productivityQuality.fix_round_count)}}`)}}</div></div>`,
        `<div class="row"><b>Cost And Latency</b><div class="line">${{pill(`${{seconds(productivitySpend.wall_seconds)}} reviewer wall`)}}${{pill(`${{display(productivitySpend.total_tokens)}} tokens`)}}${{pill(money(productivitySpend.cost_usd))}}</div></div>`,
        (productivity.warnings || []).length ? `<div class="row muted">${{esc((productivity.warnings || []).slice(0, 3).join("; "))}}</div>` : ""
      ].filter(Boolean).join("");
      put("productivity", productivityRows || empty("No local productivity signals yet."));
      put("owner", ownerQueue.length ? ownerQueue.map(item => `<div class="row"><div class="line"><a href="${{esc(href(item.url))}}">#${{esc(item.pr_number)}} ${{esc(item.kind)}}</a>${{pill(item.next_action)}}${{pill(item.head_sha_prefix)}}</div><div class="muted">${{esc(item.branch)}} by ${{esc(item.author)}}${{item.updated_at ? ` updated ${{localTime(item.updated_at)}}` : ""}}</div></div>`).join("") : empty(data.owner_queue?.message || "No owner queue items."));
      put("agents", agentCards.length ? agentCards.map(agent => `<div class="row"><div class="line"><b>${{esc(agent.provider)}}</b>${{pill(agent.role)}}${{pill(agent.status)}}${{agent.stale ? pill("stale") : ""}}${{agent.lane ? pill(agent.lane) : ""}}${{agent.pr_number ? pill(`#${{agent.pr_number}}`) : ""}}</div><div>${{esc(agent.title || agent.next_action || "local agent")}}</div><div class="muted">${{esc(agent.branch || agent.repo || "")}}${{agent.pid ? ` pid=${{esc(agent.pid)}}` : ""}}${{agent.cwd ? ` cwd=${{esc(agent.cwd)}}` : ""}}${{agent.updated_at ? ` updated ${{localTime(agent.updated_at)}}` : ""}}</div></div>`).join("") : empty(data.agent_adapters?.message || "No local agent adapter cards."));
      const campaignsData = data.release_campaigns || {{}};
      const campaigns = campaignsData.campaigns || [];
      const campaignRows = campaigns.flatMap(c => {{
        const header = `<div class="row"><div class="line"><b>Release ${{esc(c.release_tag)}}</b>${{pill(c.status)}}${{c.dry_run ? pill("dry-run") : pill("applied")}}${{pill(c.qualification_context)}}<span>${{seconds(c.elapsed_seconds)}}</span></div><div>next: <b>${{esc(c.next_action)}}</b></div></div>`;
        const cardRows = (c.cards || []).map(card => `<div class="row" style="margin-left:16px"><div class="line"><b>${{esc(card.provider)}}</b><span class="${{stateClass(card.state)}}">${{esc(card.state)}}</span>${{pill(card.environment)}}${{card.transport_verified === false ? pill("transport unverified") : ""}}<span>${{seconds(card.elapsed_seconds)}}</span></div><div>next: <b>${{esc(card.next_action)}}</b></div>${{card.next_detail ? `<div class="muted">${{esc(card.next_detail)}}</div>` : ""}}${{card.response_deadline_at ? `<div class="muted" title="${{esc(card.response_deadline_at)}}">response deadline ${{localTime(card.response_deadline_at)}}</div>` : ""}}</div>`);
        return [header, ...cardRows];
      }}).join("");
      put("campaigns", campaignRows || empty(campaignsData.message || "No release campaigns."));
      put("prs", prs.length ? prs.map(pr => `<div class="row">
        <div class="line"><a href="${{esc(href(pr.url))}}">#${{esc(pr.number)}} ${{esc(pr.title)}}</a>${{pill(pr.merge_state)}}${{pr.is_draft ? pill("draft") : ""}}${{pr.stale ? pill("stale") : ""}}</div>
        <div class="muted">${{esc(pr.branch)}} by ${{esc(pr.author)}}${{pr.updated_at ? ` updated ${{localTime(pr.updated_at)}}` : ""}}</div>
        <div>labels: ${{labels(pr.labels)}}</div>
        <div>checks: ${{checks(pr.checks)}}</div>
        <div>next: <b>${{esc(pr.next_action)}}</b></div>
        ${{pr.next_detail ? `<div class="muted">${{esc(pr.next_detail)}}</div>` : ""}}
      </div>`).join("") : empty("No open pull requests."));
      put("alerts", alerts.length ? alerts.map(a => `<div class="row"><b class="warn">${{esc(a.kind)}}</b> ${{esc(a.message)}}</div>`).join("") : empty("No gate alerts."));
      put("runs", runs.length ? runs.slice(0, 8).map(run => `<div class="row"><div class="line"><a href="${{esc(href(run.url))}}">${{esc(run.workflow || "workflow")}}</a>${{pill(run.conclusion || run.status || "unknown")}}</div><div class="muted">${{esc(run.branch)}}${{run.updated_at ? ` updated ${{localTime(run.updated_at)}}` : ""}}</div></div>`).join("") : empty("No recent Code Mower workflow runs."));
      put("verdicts", verdicts.length ? verdicts.map(v => `<div class="row"><div class="line"><a href="${{esc(href(v.url))}}">#${{esc(v.pr_number)}} ${{esc(v.lane)}}</a>${{pill(v.verdict)}}${{pill(v.head_sha_prefix)}}</div><div class="muted">${{localTime(v.created_at)}}</div></div>`).join("") : empty(timelines.verdicts?.message || "No local reviewer verdict history yet."));
      const spendRows = [
        ...spendGroups.map(g => `<div class="row"><div class="line"><b>${{esc(g.lane)}}</b>${{pill(g.verdict)}}${{pill(`${{g.runs}} runs`)}}</div><div class="muted">${{seconds(g.wall_seconds_total)}} total / ${{seconds(g.wall_seconds_avg)}} avg / ${{money(g.cost_usd_total)}} / ${{esc(g.total_tokens || 0)}} tokens</div></div>`),
        spend.skipped_rows ? `<div class="row muted">Skipped ${{esc(spend.skipped_rows)}} malformed spend row(s).</div>` : "",
        spend.filtered_rows ? `<div class="row muted">Filtered ${{esc(spend.filtered_rows)}} spend row(s) from other repos.</div>` : ""
      ].filter(Boolean);
      put("spend", spendRows.length ? spendRows.join("") : empty(spend.message || "No reviewer spend rows for this repo yet."));
      const boards = data.local_boards?.boards || [];
      const procs = data.local_processes?.processes || [];
      put("local", [...boards.map(b => `<div class="row">board localhost:${{esc(b.port)}} pid=${{esc(b.pid)}} cwd=<code>${{esc(b.cwd || "")}}</code></div>`), ...procs.slice(0, 8).map(p => `<div class="row">${{esc(p.provider)}} pid=${{esc(p.pid)}} cwd=<code>${{esc(p.cwd || "")}}</code></div>`)].join("") || empty("No local boards or lane processes visible."));
    }}
    function renderEvents(history) {{
      const events = history.events || [];
      put("history", events.length ? events.slice().reverse().map(event => {{
        const s = event.summary || {{}};
        const remote = s.remote_available ? "remote available" : "remote unavailable";
        return `<div class="row"><div class="line"><b>${{localTime(event.created_at)}}</b>${{pill(remote)}}</div><div>next: <b>${{esc(s.next_action || "inspect")}}</b></div><div class="muted">PRs ${{esc(s.open_prs ?? 0)}} / alerts ${{esc(s.gate_alerts ?? 0)}} / local ${{esc((s.local_boards ?? 0) + (s.local_processes ?? 0))}}</div></div>`;
      }}).join("") : empty(history.message || "No local board events recorded yet."));
    }}
    let pollTimer = null;
    let fastPollAttempts = 0;
    // The only place a timer is ever armed, and it always clears the pending
    // one first, so exactly one load() is scheduled at a time and no fixed
    // interval can race the self-scheduled poll into stacked timers.
    function scheduleNextLoad(delayMs) {{
      if (pollTimer !== null) {{
        clearTimeout(pollTimer);
      }}
      pollTimer = setTimeout(load, delayMs);
    }}
    function nextDelayMs(cache) {{
      if (awaitingRefresh(cache)) {{
        // Only a response that is no longer awaiting a refresh resets the
        // budget. An exhausted counter must stay exhausted while the same
        // refresh is still pending, otherwise every normal-interval poll would
        // start a fresh 20-attempt burst and repeat that forever.
        if (fastPollAttempts >= FAST_POLL_MAX_ATTEMPTS) return REFRESH_MS;
        fastPollAttempts += 1;
        return FAST_POLL_MS;
      }}
      fastPollAttempts = 0;
      return freshDelayMs(cache) ?? REFRESH_MS;
    }}
    async function load() {{
      // A failed fetch, a malformed payload, and cache metadata that is absent
      // or unusable all fall back to the configured interval.
      let delayMs = REFRESH_MS;
      try {{
        const [statusResponse, eventsResponse] = await Promise.all([
          fetch("/api/status", {{cache:"no-store"}}),
          fetch("/api/events", {{cache:"no-store"}})
        ]);
        const statusData = await statusResponse.json();
        render(statusData);
        renderEvents(await eventsResponse.json());
        delayMs = nextDelayMs(statusData.board?.cache);
      }} catch (error) {{
        put("summary", `<div class="metric"><span class="muted">Next action</span><b class="warn">reload board</b></div>`);
      }}
      scheduleNextLoad(delayMs);
    }}
    load();
  </script>
</body>
</html>
"""


def _probe_board_status(board: Mapping[str, Any], *, timeout: float = 0.75) -> dict[str, Any]:
    url = str(board.get("url") or "")
    if not url:
        return {"available": False, "message": "Board URL unavailable"}
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/api/identity", timeout=timeout) as response:
            raw = response.read().decode("utf-8") or "{}"
    except urllib.error.HTTPError as exc:
        if exc.code in {404, 405}:
            return {
                "available": False,
                "reason": "legacy_identity_endpoint_missing",
                "message": "Board identity endpoint is missing",
            }
        return {
            "available": False,
            "reason": "identity_probe_failed",
            "message": "Board identity probe failed",
        }
    except (OSError, TimeoutError, urllib.error.URLError):
        return {
            "available": False,
            "reason": "identity_probe_failed",
            "message": "Board identity probe failed",
        }
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "available": False,
            "reason": "legacy_identity_malformed",
            "message": "Board identity response was not JSON",
        }
    return (
        payload
        if isinstance(payload, dict)
        else {
            "available": False,
            "reason": "legacy_identity_malformed",
            "message": "Board status was not an object",
        }
    )


def _inventory_next_action(boards: list[dict[str, Any]], available: bool) -> tuple[str, str]:
    if not available:
        return "fix local process inspection", "install lsof or ss, or grant this shell permission to inspect local listeners"
    if not boards:
        return "start Board", "run code-mower board serve --repo OWNER/REPO"
    stale = [board for board in boards if board.get("restart_recommended")]
    if stale:
        ports = ", ".join(str(board.get("port")) for board in stale)
        return "restart stale Board", f"stop stale Board port(s) {ports}, then restart with code-mower board serve --repo OWNER/REPO"
    unresponsive = [board for board in boards if board.get("health") == "unresponsive"]
    if unresponsive:
        ports = ", ".join(str(board.get("port")) for board in unresponsive)
        return "inspect unresponsive Board", f"Board listener port(s) {ports} did not answer /api/identity"
    return "use listed localhost URL", "open the Board URL for the repo you want"


def _redact_inventory_paths(payload: dict[str, Any]) -> None:
    for key in ("boards", "matches", "stopped"):
        for board in payload.get(key) or []:
            if board.get("cwd"):
                board["cwd"] = lane_status.LOCAL_PATH_REDACTION
                board["cwd_redacted"] = True


def _command_looks_like_board(command: str) -> bool:
    return lane_status.command_looks_like_code_mower_board(command)


def _default_pid_alive(pid: int) -> bool:
    """Best-effort liveness probe that never signals the target process."""

    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def board_inventory_payload(
    *,
    show_local_paths: bool = False,
    command_runner: lane_status.CommandRunner = lane_status.run_command,
    status_probe: Any = _probe_board_status,
) -> dict[str, Any]:
    local = lane_status.collect_local_boards(command_runner)
    boards: list[dict[str, Any]] = []
    for discovered in local.get("boards") or []:
        if not isinstance(discovered, Mapping):
            continue
        item = dict(discovered)
        probed = status_probe(item) if status_probe else {}
        if isinstance(probed, Mapping) and probed.get("schema") in {BOARD_IDENTITY_SCHEMA, lane_status.LANE_STATUS_SCHEMA}:
            board_meta = probed.get("board") if isinstance(probed.get("board"), Mapping) else {}
            version = board_meta.get("version") if isinstance(board_meta.get("version"), Mapping) else {}
            item["health"] = "ok"
            item["repo"] = str(probed.get("repo") or item.get("repo") or "")
            item["serving_version"] = str(version.get("serving_version") or "")
            item["installed_version"] = str(version.get("installed_version") or "")
            item["restart_recommended"] = bool(version.get("restart_recommended"))
        elif isinstance(probed, Mapping) and not probed.get("available", True):
            reason = str(probed.get("reason") or "")
            if reason.startswith("legacy_"):
                item["health"] = "legacy"
                item["status_message"] = (
                    "legacy / restart recommended: "
                    f"{probed.get('message') or 'Board identity unavailable'}"
                )
                item["restart_recommended"] = True
            else:
                item["health"] = "unresponsive"
                item["status_message"] = str(probed.get("message") or "Board status unavailable")
                item.setdefault("restart_recommended", False)
        else:
            item["health"] = "unknown"
            item.setdefault("restart_recommended", False)
        boards.append(item)
    next_action, next_detail = _inventory_next_action(boards, bool(local.get("available")))
    payload = {
        "schema": BOARD_INVENTORY_SCHEMA,
        "available": bool(local.get("available")),
        "message": str(local.get("message") or ""),
        "boards": boards,
        "next_action": next_action,
        "next_detail": next_detail,
    }
    if not show_local_paths:
        _redact_inventory_paths(payload)
    return payload


def render_inventory_text(payload: Mapping[str, Any]) -> str:
    lines = ["Code Mower local Boards"]
    if not payload.get("available"):
        lines.append(f"Inventory: unavailable ({payload.get('message') or 'local process inspection failed'})")
    boards = payload.get("boards") if isinstance(payload.get("boards"), list) else []
    if not boards and payload.get("available"):
        lines.append("Boards: none visible")
    for board_item in boards:
        repo = board_item.get("repo") or "unknown repo"
        version = board_item.get("serving_version") or "unknown version"
        health = board_item.get("health") or "unknown"
        if board_item.get("health") == "legacy" and board_item.get("restart_recommended"):
            health = "legacy / restart recommended"
            restart = ""
        else:
            restart = " restart recommended" if board_item.get("restart_recommended") else ""
        cwd = f" cwd={board_item.get('cwd')}" if board_item.get("cwd") else ""
        lines.append(
            f"- {board_item.get('url') or 'localhost'} pid={board_item.get('pid')} "
            f"repo={repo} version={version} health={health}{restart}{cwd}"
        )
    lines.extend(["", f"Next: {payload.get('next_action') or 'inspect'}"])
    if payload.get("next_detail"):
        lines.append(f"Detail: {payload['next_detail']}")
    return "\n".join(lines) + "\n"


def _revalidated_board_command(
    pid: int,
    command_runner: lane_status.CommandRunner,
) -> str:
    """Re-read a target command line so stop never signals a reused pid."""

    try:
        completed = command_runner(["ps", "-p", str(pid), "-o", "command="])
    except (OSError, ValueError):
        return ""
    stdout = getattr(completed, "stdout", "") or ""
    return str(stdout).strip()


def stop_board(
    *,
    port: int | None = None,
    pid: int | None = None,
    yes: bool = False,
    show_local_paths: bool = False,
    command_runner: lane_status.CommandRunner = lane_status.run_command,
    killer: Any = os.kill,
    prune_stale_agents: bool = False,
    agent_adapters_path: str | Path | None = None,
    pid_alive: Callable[[int], bool] | None = None,
) -> dict[str, Any]:
    prune_only = port is None and pid is None
    if prune_only:
        if not prune_stale_agents:
            return {
                "schema": BOARD_STOP_SCHEMA,
                "status": "invalid_selector",
                "message": "pass exactly one of --port or --pid",
                "stopped": [],
                "errors": [],
            }
        if not yes:
            return {
                "schema": BOARD_STOP_SCHEMA,
                "status": "confirmation_required",
                "message": "pass --yes to prune stale launcher metadata",
                "stopped": [],
                "errors": [],
            }
        adapters_dir = (
            Path(agent_adapters_path)
            if agent_adapters_path
            else Path(".") / DEFAULT_AGENT_ADAPTERS_RELATIVE_PATH
        )
        pruned_agents = prune_stale_agent_adapters(
            adapters_dir,
            pid_alive=pid_alive,
        )
        prune_errors = pruned_agents.get("errors") or []
        if prune_errors:
            prune_status = "partial" if pruned_agents.get("pruned") else "failed"
            prune_message = "stale launcher metadata pruning encountered errors"
        else:
            prune_status = "pruned"
            prune_message = "pruned stale launcher metadata without stopping any Board listener"
        payload: dict[str, Any] = {
            "schema": BOARD_STOP_SCHEMA,
            "status": prune_status,
            "message": prune_message,
            "selector": {"port": None, "pid": None},
            "matches": [],
            "stopped": [],
            "errors": [],
            "pruned_agents": pruned_agents,
        }
        if not show_local_paths:
            _redact_inventory_paths(payload)
        return payload
    if (port is None) == (pid is None):
        return {
            "schema": BOARD_STOP_SCHEMA,
            "status": "invalid_selector",
            "message": "pass exactly one of --port or --pid",
            "stopped": [],
            "errors": [],
        }
    inventory = board_inventory_payload(
        show_local_paths=True,
        command_runner=command_runner,
        status_probe=None,
    )
    boards = inventory.get("boards") if isinstance(inventory.get("boards"), list) else []
    matches = [
        board_item
        for board_item in boards
        if board_item.get("confidence") == "high"
        and (
            (port is not None and int(board_item.get("port") or -1) == port)
            or (pid is not None and int(board_item.get("pid") or -1) == pid)
        )
    ]
    payload: dict[str, Any] = {
        "schema": BOARD_STOP_SCHEMA,
        "selector": {"port": port, "pid": pid},
        "matches": matches,
        "stopped": [],
        "errors": [],
    }
    if not matches:
        payload["status"] = "not_found"
        payload["message"] = "no matching high-confidence Code Mower Board listener found"
    elif not yes:
        payload["status"] = "confirmation_required"
        payload["message"] = "pass --yes to stop matching Code Mower Board listener(s)"
    else:
        seen: set[int] = set()
        for board_item in matches:
            target_pid = int(board_item.get("pid") or 0)
            if not target_pid or target_pid in seen:
                continue
            seen.add(target_pid)
            # Guard against pid reuse between inventory and signal: only a
            # still-matching Board command line may be signaled, never a
            # broad or unrelated process that recycled the pid.
            if not _command_looks_like_board(
                _revalidated_board_command(target_pid, command_runner)
            ):
                payload["errors"].append(
                    {
                        "pid": target_pid,
                        "message": "target no longer matches a Code Mower Board listener; refusing to signal",
                    }
                )
                continue
            try:
                killer(target_pid, signal.SIGTERM)
                payload["stopped"].append(
                    {
                        "pid": target_pid,
                        "port": board_item.get("port"),
                        "repo": board_item.get("repo", ""),
                        "cwd": board_item.get("cwd", ""),
                    }
                )
            except ProcessLookupError:
                payload["errors"].append({"pid": target_pid, "message": "process no longer exists"})
            except PermissionError:
                payload["errors"].append({"pid": target_pid, "message": "permission denied"})
            except OSError:
                payload["errors"].append(
                    {"pid": target_pid, "message": "could not signal Board process"}
                )
        payload["status"] = "stopped" if payload["stopped"] and not payload["errors"] else "partial" if payload["stopped"] else "failed"
        payload["message"] = "stopped matching Code Mower Board listener(s)" if payload["stopped"] else "could not stop matching Code Mower Board listener(s)"
    if prune_stale_agents and yes:
        adapters_dir = (
            Path(agent_adapters_path)
            if agent_adapters_path
            else Path(".") / DEFAULT_AGENT_ADAPTERS_RELATIVE_PATH
        )
        payload["pruned_agents"] = prune_stale_agent_adapters(
            adapters_dir,
            pid_alive=pid_alive,
        )
        if payload["pruned_agents"].get("errors"):
            if payload.get("stopped"):
                payload["status"] = "partial"
                payload["message"] = (
                    "stopped matching Code Mower Board listener(s), but stale launcher "
                    "metadata pruning encountered errors"
                )
            else:
                payload["status"] = "failed"
                payload["message"] = "stale launcher metadata pruning encountered errors"
    if not show_local_paths:
        _redact_inventory_paths(payload)
    return payload


def render_stop_text(payload: Mapping[str, Any]) -> str:
    lines = [f"Code Mower Board stop: {payload.get('status') or 'unknown'}", str(payload.get("message") or "")]
    stopped = payload.get("stopped") if isinstance(payload.get("stopped"), list) else []
    for item in stopped:
        lines.append(f"- stopped pid={item.get('pid')} port={item.get('port')} repo={item.get('repo') or 'unknown repo'}")
    errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
    for item in errors:
        lines.append(f"- error pid={item.get('pid')}: {item.get('message')}")
    pruned = payload.get("pruned_agents") if isinstance(payload.get("pruned_agents"), Mapping) else {}
    for name in pruned.get("pruned") or []:
        lines.append(f"- pruned stale launcher metadata: {name}")
    for item in pruned.get("errors") or []:
        if isinstance(item, Mapping):
            lines.append(f"- prune error {item.get('file') or 'unknown file'}: {item.get('message')}")
    return "\n".join(line for line in lines if line) + "\n"


def make_handler(
    config: BoardConfig,
    *,
    gh_json_runner: lane_status.GitHubJsonRunner = lane_status.run_gh_json,
    command_runner: lane_status.CommandRunner = lane_status.run_command,
) -> type[BaseHTTPRequestHandler]:
    last_recorded_at: datetime | None = None
    last_recorded_generation = 0
    recording_lock = Lock()
    status_cache = StatusCache(
        lambda: status_payload(config, gh_json_runner=gh_json_runner, command_runner=command_runner),
        ttl_seconds=config.refresh_seconds,
    )

    class BoardHandler(BaseHTTPRequestHandler):
        server_version = "CodeMowerBoard/0.1"

        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            nonlocal last_recorded_at, last_recorded_generation

            if not _host_header_allowed(self.headers.get("Host")):
                self._send(HTTPStatus.FORBIDDEN, b"forbidden\n", "text/plain; charset=utf-8")
                return
            if not _origin_header_allowed(self.headers.get("Origin")):
                self._send(HTTPStatus.FORBIDDEN, b"forbidden\n", "text/plain; charset=utf-8")
                return
            path = urlparse(self.path).path
            if path in {"", "/", "/index.html"}:
                self._send(HTTPStatus.OK, render_board_html(config).encode("utf-8"), "text/html; charset=utf-8")
                return
            if path == "/api/identity":
                body = json.dumps(board_identity_payload(config), indent=2, sort_keys=True).encode("utf-8")
                self._send(HTTPStatus.OK, body, "application/json; charset=utf-8")
                return
            if path == "/api/status":
                snapshot, cache_metadata = status_cache.get()
                payload = copy.deepcopy(snapshot) if snapshot is not None else _pending_status_payload(config)
                payload["board"]["cache"] = cache_metadata
                if config.record_events:
                    # Recording identity is the cache generation, not cache freshness.
                    # ``snapshot`` and ``generation`` are read together under the cache
                    # lock, so the generation always names the completed snapshot in
                    # hand. Browser polling slower than the TTL can hand us a snapshot
                    # that a background refresh completed and that then aged out before
                    # the next request, so gating on ``state == "fresh"`` dropped whole
                    # generations from local history; gating on the generation records
                    # each completed snapshot exactly once whether it is still fresh or
                    # already stale.
                    generation = cache_metadata.get("generation") or 0
                    if snapshot is None or generation <= 0:
                        payload["board"]["recording"] = _recording_metadata(
                            config,
                            "pending",
                            message="waiting for first completed status snapshot",
                        )
                    else:
                        with recording_lock:
                            now = _utc_now()
                            if generation <= last_recorded_generation:
                                # Already persisted, including while a newer refresh is
                                # in flight. Concurrent requests observing the same
                                # generation serialize here, so only the first records.
                                payload["board"]["recording"] = _recording_metadata(
                                    config,
                                    "skipped",
                                    message="snapshot already recorded",
                                )
                            elif not _recording_due(last_recorded_at, now, config.record_interval_seconds):
                                # Leave this generation eligible: whichever generation is
                                # current once the interval elapses gets recorded then.
                                payload["board"]["recording"] = _recording_metadata(
                                    config,
                                    "skipped",
                                    message="record interval not reached",
                                )
                            else:
                                try:
                                    result = _record_live_snapshot(snapshot, config, now=now)
                                except (ValueError, board_store.BoardStoreError):
                                    # Same throttling as a successful write: consume the
                                    # interval and the generation so a failing event
                                    # store is not retried on every request.
                                    last_recorded_at = now
                                    last_recorded_generation = generation
                                    payload["board"]["recording"] = _recording_metadata(
                                        config,
                                        "error",
                                        message="could not update local board event store",
                                    )
                                else:
                                    last_recorded_at = now
                                    last_recorded_generation = generation
                                    payload["board"]["recording"] = _recording_metadata(
                                        config,
                                        "recorded",
                                        kept=result.kept,
                                        pruned=result.pruned,
                                        malformed=result.malformed,
                                    )
                payload["timelines"] = timelines_payload(config)
                body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
                self._send(HTTPStatus.OK, body, "application/json; charset=utf-8")
                return
            if path == "/api/events":
                payload = board_store.event_report(path=_store_path(config), limit=config.event_limit)
                body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
                self._send(HTTPStatus.OK, body, "application/json; charset=utf-8")
                return
            if path == "/healthz":
                self._send(HTTPStatus.OK, b'{"ok":true}\n', "application/json; charset=utf-8")
                return
            self._send(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain; charset=utf-8")

    return BoardHandler


def serve(config: BoardConfig, *, open_browser: bool = False) -> int:
    if not _is_loopback(config.host):
        print("error: board host must be loopback; use 127.0.0.1 or localhost", file=sys.stderr)
        return 2
    if not 0 <= config.port <= 65535:
        print("error: --port must be between 0 and 65535", file=sys.stderr)
        return 2
    if config.record_interval_seconds < 0:
        print("error: --record-interval-seconds must be non-negative", file=sys.stderr)
        return 2
    if config.retention_days < 0:
        print("error: --retention-days must be non-negative", file=sys.stderr)
        return 2
    if config.max_events < 1:
        print("error: --max-events must be at least 1", file=sys.stderr)
        return 2
    handler = make_handler(config)
    server = _bind_board_server(config, handler)
    if server is None:
        return 2
    with server:
        port = int(server.server_address[1])
        url = _server_url(config.host, port)
        if config.port_was_default and port != config.port:
            print(f"Code Mower Board: default port {config.port} was busy; using {port}", file=sys.stderr)
        print(f"Code Mower Board: {url}", flush=True)
        if open_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nCode Mower Board stopped")
    return 0


def record_status(
    config: BoardConfig,
    *,
    retention_days: int = board_store.DEFAULT_RETENTION_DAYS,
    max_events: int = board_store.DEFAULT_MAX_EVENTS,
    gh_json_runner: lane_status.GitHubJsonRunner = lane_status.run_gh_json,
    command_runner: lane_status.CommandRunner = lane_status.run_command,
) -> board_store.StoreWriteResult:
    snapshot = status_payload(
        config,
        gh_json_runner=gh_json_runner,
        command_runner=command_runner,
    )
    return board_store.append_snapshot(
        _recordable_payload(snapshot),
        path=_store_path(config),
        retention_days=retention_days,
        max_events=max_events,
    )


def render_events_text(report: dict[str, Any]) -> str:
    lines = ["Code Mower board events"]
    if not report.get("available"):
        lines.append(report.get("message") or "no local board event store yet")
        return "\n".join(lines) + "\n"
    lines.append(f"Events: {report.get('event_count', 0)}")
    if report.get("malformed"):
        lines.append(f"Malformed lines skipped: {report['malformed']}")
    for event in report.get("events") or []:
        summary = event.get("summary") or {}
        lines.append(
            "- "
            f"{event.get('created_at')} "
            f"{summary.get('next_action', 'inspect')} "
            f"prs={summary.get('open_prs', 0)} "
            f"alerts={summary.get('gate_alerts', 0)}"
        )
    return "\n".join(lines) + "\n"


def record_result_payload(result: board_store.StoreWriteResult) -> dict[str, Any]:
    return {
        "schema": board_store.BOARD_RECORD_SCHEMA,
        "status": "recorded",
        "store_path": lane_status.LOCAL_PATH_REDACTION,
        "store_path_redacted": True,
        "event": result.event,
        "kept": result.kept,
        "pruned": result.pruned,
        "malformed": result.malformed,
    }


def reset_result_payload(result: board_store.StoreResetResult) -> dict[str, Any]:
    return {
        "schema": board_store.BOARD_RESET_SCHEMA,
        "status": "reset" if result.deleted else "noop",
        "store_path": lane_status.LOCAL_PATH_REDACTION,
        "store_path_redacted": True,
        "deleted": result.deleted,
    }


def _doctor_check(check_id: str, status: str, message: str, **extra: Any) -> dict[str, Any]:
    check = {"id": check_id, "status": status, "message": message}
    check.update({key: value for key, value in extra.items() if value not in (None, "", [])})
    return check


def _doctor_overall(checks: list[dict[str, Any]]) -> str:
    statuses = {str(check.get("status") or "") for check in checks}
    if "fail" in statuses:
        return "fail"
    if "warn" in statuses:
        return "warn"
    return "pass"


def doctor_payload(
    config: BoardConfig,
    *,
    gh_json_runner: lane_status.GitHubJsonRunner = lane_status.run_gh_json,
    command_runner: lane_status.CommandRunner = lane_status.run_command,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    repo_path = Path(config.repo_path)
    checks.append(
        _doctor_check(
            "repo.path",
            "pass" if repo_path.is_dir() else "fail",
            "repository path is readable" if repo_path.is_dir() else "repository path is not readable",
            path=str(repo_path) if config.show_local_paths else lane_status.LOCAL_PATH_REDACTION,
            path_redacted=not config.show_local_paths,
        )
    )

    status = status_payload(
        config,
        gh_json_runner=gh_json_runner,
        command_runner=command_runner,
    )
    remote = status.get("remote") if isinstance(status.get("remote"), dict) else {}
    checks.append(
        _doctor_check(
            "github.remote",
            "pass" if remote.get("available") else "warn",
            "GitHub metadata is available"
            if remote.get("available")
            else "GitHub unavailable; Board will render local-only state",
            errors=len(remote.get("errors") or []),
        )
    )

    gate_health = remote.get("gate_health") if isinstance(remote.get("gate_health"), dict) else {}
    gate_alerts = gate_health.get("alerts") if isinstance(gate_health.get("alerts"), list) else []
    checks.append(
        _doctor_check(
            "gate.health",
            "warn" if gate_alerts else "pass",
            f"{len(gate_alerts)} gate alert(s) need attention" if gate_alerts else "no gate alerts",
            alerts=len(gate_alerts),
        )
    )

    store_report = board_store.event_report(path=_store_path(config), limit=config.event_limit)
    store_malformed = int(store_report.get("malformed") or 0)
    store_available = bool(store_report.get("available"))
    store_message = str(store_report.get("message") or "")
    if store_malformed:
        store_status = "warn"
        store_text = f"local board event store has {store_malformed} malformed line(s)"
    elif store_message == "could not read local board event store":
        store_status = "warn"
        store_text = store_message
    elif store_available:
        store_status = "pass"
        store_text = f"local board event store has {store_report.get('event_count', 0)} event(s)"
    else:
        store_status = "pass"
        store_text = "no local board event store yet; run board record or board serve --record-events"
    checks.append(
        _doctor_check(
            "store.events",
            store_status,
            store_text,
            events=int(store_report.get("event_count") or 0),
            malformed=store_malformed,
        )
    )

    owner_queue = status.get("owner_queue") if isinstance(status.get("owner_queue"), dict) else {}
    owner_count = int(owner_queue.get("count") or 0)
    checks.append(
        _doctor_check(
            "owner.queue",
            "warn" if owner_count else "pass",
            f"owner queue has {owner_count} item(s)" if owner_count else "owner queue is empty",
            entries=owner_count,
        )
    )

    adapters = status.get("agent_adapters") if isinstance(status.get("agent_adapters"), dict) else {}
    adapter_warnings = adapters.get("warnings") if isinstance(adapters.get("warnings"), list) else []
    adapter_agents = adapters.get("agents") if isinstance(adapters.get("agents"), list) else []
    checks.append(
        _doctor_check(
            "agent.adapters",
            "warn" if adapter_warnings else "pass",
            f"{len(adapter_warnings)} local agent adapter warning(s)"
            if adapter_warnings
            else (
                f"{len(adapter_agents)} local agent adapter card(s)"
                if adapter_agents
                else "optional local agent adapters are not configured"
            ),
            agents=len(adapter_agents),
            warnings=len(adapter_warnings),
        )
    )

    timelines = timelines_payload(config, event_report_payload=store_report)
    spend = timelines.get("spend") if isinstance(timelines.get("spend"), dict) else {}
    spend_message = str(spend.get("message") or "")
    spend_status = "warn" if spend_message == "could not read reviewer spend file" else "pass"
    checks.append(
        _doctor_check(
            "spend.timeline",
            spend_status,
            spend_message or f"{len(spend.get('groups') or [])} spend group(s) available",
            groups=len(spend.get("groups") or []),
            skipped_rows=int(spend.get("skipped_rows") or 0),
        )
    )

    return {
        "schema": BOARD_DOCTOR_SCHEMA,
        "repo": config.repo,
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "status": _doctor_overall(checks),
        "checks": checks,
        "summary": {
            "open_prs": len(remote.get("pull_requests") or []),
            "workflow_runs": len(remote.get("workflow_runs") or []),
            "gate_alerts": len(gate_alerts),
            "owner_queue": owner_count,
            "agent_cards": len(adapter_agents),
            "local_events": int(store_report.get("event_count") or 0),
            "next_action": str(status.get("next_action") or "inspect"),
        },
        "next_action": _board_doctor_next_action(config.repo, status, checks),
    }


def _board_doctor_next_action(repo: str, status: dict[str, Any], checks: list[dict[str, Any]]) -> str:
    if any(check.get("status") == "fail" for check in checks):
        return "fix failed Board diagnostic"
    action = str(status.get("next_action") or "")
    if action and action != "no active lanes":
        return action
    if any(check.get("id") == "store.events" and "no local board event store" in str(check.get("message")) for check in checks):
        return f"run code-mower board serve --repo {repo} --record-events to build local history"
    return action or "no active lanes"


def render_doctor_text(payload: dict[str, Any]) -> str:
    lines = [
        f"Code Mower board doctor for {payload['repo']}",
        f"Status: {payload['status']}",
        "",
    ]
    for check in payload.get("checks") or []:
        lines.append(
            f"{str(check.get('status') or '').upper():5} "
            f"{str(check.get('id') or ''):16} "
            f"{check.get('message') or ''}"
        )
    lines.extend(["", f"Next: {payload.get('next_action') or 'inspect'}"])
    return "\n".join(lines) + "\n"


def _record_store_display(args: argparse.Namespace) -> str:
    if args.store_path:
        return "custom store path"
    return board_store.DEFAULT_STORE_RELATIVE_PATH.as_posix()


def _positive_int(value: str) -> int:
    """argparse type for options that must be a positive (>0) integer."""
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid int value: {value!r}") from None
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {value!r}")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="code-mower board")
    subparsers = parser.add_subparsers(dest="command", required=True)
    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--show-local-paths", action="store_true", help="show local cwd paths for debugging")
    list_parser.add_argument("--json", action="store_true")
    stop_parser = subparsers.add_parser("stop")
    stop_selector = stop_parser.add_mutually_exclusive_group(required=False)
    stop_selector.add_argument("--port", type=int, help="loopback port serving the Board")
    stop_selector.add_argument("--pid", type=int, help="process id serving the Board")
    stop_parser.add_argument("--yes", action="store_true", help="stop the matching Board listener")
    stop_parser.add_argument(
        "--prune-stale-agents",
        action="store_true",
        help="with --yes, delete only stale launcher metadata files whose pids are gone",
    )
    stop_parser.add_argument("--show-local-paths", action="store_true", help="show local cwd paths for debugging")
    stop_parser.add_argument("--json", action="store_true")
    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--repo", required=True)
    serve_parser.add_argument("--host", default=DEFAULT_HOST, help="loopback host to bind; default: 127.0.0.1")
    serve_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="loopback port; the default auto-falls forward when busy",
    )
    serve_parser.add_argument("--pr-limit", type=int, default=50, help="open PRs to show")
    serve_parser.add_argument("--workflow-limit", type=int, default=20, help="recent Code Mower workflow runs to show")
    serve_parser.add_argument("--stale-minutes", type=int, default=30, help="minutes before gate evidence is stale")
    serve_parser.add_argument(
        "--refresh-seconds", type=_positive_int, default=15, help="browser refresh interval (must be positive)"
    )
    serve_parser.add_argument("--show-local-paths", action="store_true", help="show local cwd paths for debugging")
    serve_parser.add_argument("--repo-path", default=".", help="repository checkout used for local Board files")
    serve_parser.add_argument("--store-path", help="custom local Board event store path")
    serve_parser.add_argument("--spend-path", help="custom reviewer spend ledger path")
    serve_parser.add_argument("--agent-adapters-path", help="custom local agent card directory")
    serve_parser.add_argument("--event-limit", type=int, default=20, help="local history events to show")
    serve_parser.add_argument("--record-events", action="store_true", help="append local history while the Board is open")
    serve_parser.add_argument("--record-interval-seconds", type=int, default=60, help="minimum seconds between records")
    serve_parser.add_argument(
        "--retention-days",
        type=int,
        default=board_store.DEFAULT_RETENTION_DAYS,
        help="local history retention window",
    )
    serve_parser.add_argument("--max-events", type=int, default=board_store.DEFAULT_MAX_EVENTS, help="maximum local events")
    serve_parser.add_argument("--open", action="store_true", help="open the local board in a browser")
    record_parser = subparsers.add_parser("record")
    record_parser.add_argument("--repo", required=True)
    record_parser.add_argument("--repo-path", default=".")
    record_parser.add_argument("--store-path")
    record_parser.add_argument("--agent-adapters-path")
    record_parser.add_argument("--pr-limit", type=int, default=50)
    record_parser.add_argument("--workflow-limit", type=int, default=20)
    record_parser.add_argument("--stale-minutes", type=int, default=30)
    record_parser.add_argument("--retention-days", type=int, default=board_store.DEFAULT_RETENTION_DAYS)
    record_parser.add_argument("--max-events", type=int, default=board_store.DEFAULT_MAX_EVENTS)
    record_parser.add_argument("--json", action="store_true")
    events_parser = subparsers.add_parser("events")
    events_parser.add_argument("--repo-path", default=".")
    events_parser.add_argument("--store-path")
    events_parser.add_argument("--limit", type=int, default=20)
    events_parser.add_argument("--show-store-path", action="store_true")
    events_parser.add_argument("--json", action="store_true")
    doctor_parser = subparsers.add_parser("doctor")
    doctor_parser.add_argument("--repo", required=True)
    doctor_parser.add_argument("--repo-path", default=".")
    doctor_parser.add_argument("--store-path")
    doctor_parser.add_argument("--spend-path")
    doctor_parser.add_argument("--agent-adapters-path")
    doctor_parser.add_argument("--pr-limit", type=int, default=50)
    doctor_parser.add_argument("--workflow-limit", type=int, default=20)
    doctor_parser.add_argument("--stale-minutes", type=int, default=30)
    doctor_parser.add_argument("--event-limit", type=int, default=20)
    doctor_parser.add_argument("--show-local-paths", action="store_true")
    doctor_parser.add_argument("--json", action="store_true")
    reset_parser = subparsers.add_parser("reset")
    reset_parser.add_argument("--repo", required=True)
    reset_parser.add_argument("--repo-path", default=".")
    reset_parser.add_argument("--store-path")
    reset_parser.add_argument("--yes", action="store_true", help="delete the local board event store")
    reset_parser.add_argument("--json", action="store_true")
    args = parser.parse_args(list(argv or ()))
    if args.command == "list":
        payload = board_inventory_payload(show_local_paths=args.show_local_paths)
        output = json.dumps(payload, indent=2, sort_keys=True) + "\n" if args.json else render_inventory_text(payload)
        print(output, end="")
        return 0 if payload.get("available") else 1
    if args.command == "stop":
        payload = stop_board(
            port=args.port,
            pid=args.pid,
            yes=args.yes,
            show_local_paths=args.show_local_paths,
            prune_stale_agents=args.prune_stale_agents,
        )
        output = json.dumps(payload, indent=2, sort_keys=True) + "\n" if args.json else render_stop_text(payload)
        print(output, end="")
        return 0 if payload.get("status") in {"stopped", "pruned"} else 2 if payload.get("status") in {"invalid_selector", "confirmation_required"} else 1
    if args.command == "serve":
        return serve(
            BoardConfig(
                repo=args.repo,
                host=args.host,
                port=DEFAULT_PORT if args.port is None else args.port,
                port_was_default=args.port is None,
                pr_limit=args.pr_limit,
                workflow_limit=args.workflow_limit,
                stale_minutes=args.stale_minutes,
                refresh_seconds=args.refresh_seconds,
                show_local_paths=args.show_local_paths,
                repo_path=args.repo_path,
                store_path=args.store_path,
                spend_path=args.spend_path,
                agent_adapters_path=args.agent_adapters_path,
                event_limit=args.event_limit,
                record_events=args.record_events,
                record_interval_seconds=args.record_interval_seconds,
                retention_days=args.retention_days,
                max_events=args.max_events,
            ),
            open_browser=args.open,
        )
    if args.command == "record":
        if args.retention_days < 0:
            print("error: --retention-days must be non-negative", file=sys.stderr)
            return 2
        if args.max_events < 1:
            print("error: --max-events must be at least 1", file=sys.stderr)
            return 2
        try:
            result = record_status(
                BoardConfig(
                    repo=args.repo,
                    pr_limit=args.pr_limit,
                    workflow_limit=args.workflow_limit,
                    stale_minutes=args.stale_minutes,
                    repo_path=args.repo_path,
                    store_path=args.store_path,
                    agent_adapters_path=args.agent_adapters_path,
                ),
                retention_days=args.retention_days,
                max_events=args.max_events,
            )
        except board_store.BoardStoreError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(record_result_payload(result), indent=2, sort_keys=True))
        else:
            print(
                f"Recorded board status to {_record_store_display(args)} "
                f"(kept {result.kept}, pruned {result.pruned})."
            )
        return 0
    if args.command == "events":
        report = board_store.event_report(
            path=Path(args.store_path) if args.store_path else board_store.default_store_path(args.repo_path),
            limit=args.limit,
            show_store_path=args.show_store_path,
        )
        output = json.dumps(report, indent=2, sort_keys=True) + "\n" if args.json else render_events_text(report)
        print(output, end="")
        return 0
    if args.command == "doctor":
        payload = doctor_payload(
            BoardConfig(
                repo=args.repo,
                pr_limit=args.pr_limit,
                workflow_limit=args.workflow_limit,
                stale_minutes=args.stale_minutes,
                show_local_paths=args.show_local_paths,
                repo_path=args.repo_path,
                store_path=args.store_path,
                spend_path=args.spend_path,
                agent_adapters_path=args.agent_adapters_path,
                event_limit=args.event_limit,
            )
        )
        output = json.dumps(payload, indent=2, sort_keys=True) + "\n" if args.json else render_doctor_text(payload)
        print(output, end="")
        return 1 if payload["status"] == "fail" else 0
    if args.command == "reset":
        if not args.yes:
            print("error: board reset only deletes local board history when --yes is passed", file=sys.stderr)
            return 2
        try:
            result = board_store.reset_store(
                path=Path(args.store_path) if args.store_path else board_store.default_store_path(args.repo_path)
            )
        except board_store.BoardStoreError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        payload = reset_result_payload(result)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            action = "deleted" if result.deleted else "nothing to delete"
            print(f"Board local history reset: {action}.")
        return 0
    raise AssertionError(f"unhandled board command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
