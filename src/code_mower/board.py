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
from . import board_observation
from . import board_store
from . import config as code_mower_config
from . import controller
from . import lane_status
from . import productivity_report
from . import reviewer_spend
from . import session_lease


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
BOARD_OBSERVATIONS_SCHEMA = "code_mower.boardObservations.v1"
DEFAULT_AGENT_ADAPTERS_RELATIVE_PATH = Path(".code-mower") / "board" / "agents"
DEFAULT_OBSERVATIONS_RELATIVE_PATH = Path(".code-mower") / "board" / "observations"
DEFAULT_CAMPAIGNS_RELATIVE_PATH = Path(".code-mower") / "campaigns"
# Bounded so a directory left full of records cannot turn one page load into an
# unbounded read. The contract itself bounds each record to MAX_BYTES.
MAX_OBSERVATION_FILES = 32
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
    observations_path: str | None = None
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


def _observations_path(config: BoardConfig) -> Path:
    if config.observations_path:
        return Path(config.observations_path)
    return Path(config.repo_path) / DEFAULT_OBSERVATIONS_RELATIVE_PATH


def _campaigns_path(config: BoardConfig) -> Path:
    if config.campaigns_path:
        return Path(config.campaigns_path)
    return Path(config.repo_path) / DEFAULT_CAMPAIGNS_RELATIVE_PATH


def resolved_metadata_paths(config: BoardConfig) -> dict[str, str]:
    """Return the local metadata input paths one config resolves to.

    A caller that collects against a different source tree can bind these
    explicitly, so the live Code Mower metadata inputs stay the ones it means
    rather than following the collection source's repo path.
    """

    return {
        "store_path": str(_store_path(config)),
        "spend_path": str(_spend_path(config)),
        "agent_adapters_path": str(_agent_adapters_path(config)),
        "observations_path": str(_observations_path(config)),
        "campaigns_path": str(_campaigns_path(config)),
    }


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
    jira_reader: controller.tracker_queue.JiraQueueReader | None = None,
    tracker_links: Mapping[tuple[str, str, str], int] | None = None,
) -> dict[str, Any]:
    policy = None
    try:
        candidate = code_mower_config.load_config(Path(config.repo_path) / "code-mower.yml")
        if not code_mower_config.validate_config(candidate):
            policy = candidate
    except (OSError, ValueError):
        pass
    payload = lane_status.collect_status(
        lineage_config=policy,
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
    payload["orchestrator_lease"] = session_lease.observe_lease(start=config.repo_path)
    payload["agent_adapters"] = agent_adapters_payload(config)
    payload["observations"] = observations_payload(config)
    payload["release_campaigns"] = release_campaigns_payload(config)
    payload["owner_queue"] = owner_queue_payload(payload)
    payload["supervised_pilot"] = supervised_pilot_payload(
        config,
        payload,
        gh_json_runner=gh_json_runner,
        jira_reader=jira_reader,
        tracker_links=tracker_links,
    )
    if "tracker" in payload["supervised_pilot"]:
        payload["tracker"] = payload["supervised_pilot"]["tracker"]
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
    snapshot.pop("orchestrator_lease", None)
    # Observations are a live read model carrying local session and worktree
    # identity. The Board renders them; it does not copy them into persisted
    # local history, so replayed history keeps the shape it already had.
    snapshot.pop("observations", None)
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
STATUS_CACHE_REFRESH_TIMEOUT_SECONDS = 120.0
STATUS_CACHE_TIMEOUT_SECONDS = STATUS_CACHE_REFRESH_TIMEOUT_SECONDS


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

    A failed refresh (including abnormal thread exits or non-``Exception``
    terminations), a dead/abandoned refresh that exceeds the recovery timeout,
    or a failed refresh thread start opens a bounded retry backoff window.
    Without it the cached snapshot stays stale while ``refresh_in_progress``
    returns to false the instant the failure is recorded, so the very next
    request would start another expensive GitHub/local recomputation -- once
    per fast poll (~750ms) for as long as the failure persists. During the
    window ``get()`` still answers immediately from cold/stale metadata but
    starts nothing and reports ``refresh_in_progress`` false, so the browser
    drops back to its normal interval. The window doubles per consecutive
    failure from ``retry_base_seconds`` up to ``retry_max_seconds`` and is
    cleared by the first success. Ownership tokens ensure that late-exiting or
    abandoned threads cannot overwrite newer generations or clear the
    in-progress flag of a replacement refresh.
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
        refresh_timeout_seconds: float = STATUS_CACHE_REFRESH_TIMEOUT_SECONDS,
        timeout_seconds: float | None = None,
    ) -> None:
        self._compute = compute
        self._ttl_seconds = max(float(ttl_seconds), 0.0)
        self._clock = clock
        self._now = now
        self._start_thread = start_thread or self._default_start_thread
        self._retry_base_seconds = max(float(retry_base_seconds), 0.0)
        self._retry_max_seconds = max(float(retry_max_seconds), self._retry_base_seconds)
        if timeout_seconds is not None:
            refresh_timeout_seconds = timeout_seconds
        self._refresh_timeout_seconds = max(float(refresh_timeout_seconds), 0.0)
        self._lock = Lock()
        self._snapshot: dict[str, Any] | None = None
        self._computed_at: datetime | None = None
        self._computed_monotonic: float | None = None
        self._refreshing = False
        self._refresh_started_monotonic: float | None = None
        self._refresh_owner = 0
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
        self._refresh_started_monotonic = None
        self._refresh_owner += 1
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
            if (
                self._refreshing
                and self._refresh_started_monotonic is not None
                and self._refresh_timeout_seconds > 0.0
                and max(elapsed - self._refresh_started_monotonic, 0.0) >= self._refresh_timeout_seconds
            ):
                self._record_failure_locked(TimeoutError("status refresh timed out"))
            retry_in = (
                max(self._retry_after_monotonic - elapsed, 0.0)
                if self._retry_after_monotonic is not None
                else 0.0
            )
            in_backoff = retry_in > 0.0
            should_start = is_stale and not self._refreshing and not in_backoff
            owner: int | None = None
            if should_start:
                self._refreshing = True
                self._refresh_owner += 1
                owner = self._refresh_owner
                self._refresh_started_monotonic = elapsed
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
        if should_start and owner is not None:
            def refresh_worker() -> None:
                self._refresh(owner)

            try:
                self._start_thread(refresh_worker)
            except (Exception, SystemExit) as exc:  # noqa: BLE001 - a failed thread start must never crash the endpoint
                with self._lock:
                    if self._refresh_owner == owner:
                        self._record_failure_locked(exc)
                        metadata_snapshot["refresh_in_progress"] = False
                        metadata_snapshot["retry_in_seconds"] = round(self._retry_delay_locked(), 3)
                        metadata_snapshot["last_error"] = self._last_error
                        metadata_snapshot["last_error_at"] = _format_timestamp(self._last_error_at)
        return snapshot, metadata_snapshot

    def _refresh(self, owner: int | None = None) -> None:
        if owner is None:
            with self._lock:
                owner = self._refresh_owner
        completed = False
        try:
            result = self._compute()
            with self._lock:
                if self._refresh_owner == owner:
                    self._snapshot = result
                    self._computed_at = self._now()
                    self._computed_monotonic = self._clock()
                    self._generation += 1
                    self._refreshing = False
                    self._refresh_started_monotonic = None
                    self._last_error = ""
                    self._last_error_at = None
                    self._consecutive_failures = 0
                    self._retry_after_monotonic = None
                    self._refresh_owner += 1
                    completed = True
        except BaseException as exc:  # noqa: BLE001 - a background refresh must never crash the server
            with self._lock:
                if self._refresh_owner == owner:
                    self._record_failure_locked(exc)
                    completed = True
            return
        finally:
            if not completed:
                with self._lock:
                    if self._refresh_owner == owner and self._refreshing:
                        self._record_failure_locked(RuntimeError("refresh thread exited abnormally"))


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
    payload["orchestrator_lease"] = session_lease.observe_lease(start=config.repo_path)
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
    lineage = decision.get("lineage")
    if isinstance(lineage, Mapping):
        payload["lineage"] = {
            "status": _safe_text(lineage.get("status"), limit=20),
            "reason": _safe_text(lineage.get("reason"), limit=80),
            "current_writer": _safe_text(lineage.get("current_writer"), limit=40) if lineage.get("status") == "ready" else None,
            "contributors": [_safe_text(item, limit=40) for item in (lineage.get("contributors") or [])[:32]],
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
    if issue.get("source_kind") == "jira_cloud":
        return {"source_kind": "jira_cloud", "work_item": issue["work_item"],
                "builder_lane": _safe_text(issue.get("builder_lane"), limit=60)}
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
    status: Mapping[str, Any],
    *,
    gh_json_runner: lane_status.GitHubJsonRunner = lane_status.run_gh_json,
    jira_reader: controller.tracker_queue.JiraQueueReader | None = None,
    tracker_links: Mapping[tuple[str, str, str], int] | None = None,
) -> dict[str, Any]:
    """Return pilot state and its optional tracker view without modifying status."""
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
    tracker_view = None
    if controller.tracker_queue.jira_enabled(raw_config):
        tracker_view = controller.tracker_queue.queue_view(
            controller.tracker_queue.collect_queue(raw_config, reader=jira_reader),
            config=raw_config, remote=remote, links=tracker_links,
            stale_minutes=config.stale_minutes,
        )
    if remote.get("available") or controller.tracker_queue.jira_enabled(raw_config):
        ready_issues = controller._collect_ready_issues(  # noqa: SLF001 - shared package policy surface for Board.
            repo=config.repo,
            config=raw_config,
            gh_json_runner=gh_json_runner,
            issue_limit=min(config.pr_limit, 50),
            tracker_view=tracker_view,
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
        **({"tracker": report["tracker"]} if "tracker" in report else {}),
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


def observations_payload(config: BoardConfig) -> dict[str, Any]:
    """Read locally recorded Board observations without producing any.

    This is a consumer of the frozen ``code_mower.boardObservation.v1``
    contract: every record is decoded by :mod:`board_observation` and a record
    that fails that contract is dropped with its own bounded diagnostic instead
    of being repaired, widened, or rendered. The Board never writes these
    files, never resolves a session, and never contacts a provider to fill one
    in; an empty directory is reported as "nothing recorded yet", which is a
    different statement from "no work".
    """

    path = _observations_path(config)
    payload: dict[str, Any] = {
        "schema": BOARD_OBSERVATIONS_SCHEMA,
        "record_schema": board_observation.SCHEMA,
        "available": True,
        "path": lane_status.LOCAL_PATH_REDACTION,
        "path_redacted": True,
        "path_exists": path.exists(),
        "records": [],
        "warnings": [],
        "rejected": 0,
        "message": "no local Board observations recorded yet",
    }
    if not path.exists():
        return payload
    if not path.is_dir():
        payload["available"] = False
        payload["warnings"].append({"file": "", "message": "observation path is not a directory"})
        payload["message"] = "could not read local Board observations"
        return payload
    try:
        candidates = sorted(path.glob("*.json"))[:MAX_OBSERVATION_FILES]
    except OSError:
        payload["available"] = False
        payload["warnings"].append({"file": "", "message": "could not list local Board observations"})
        payload["message"] = "could not read local Board observations"
        return payload
    for record_file in candidates:
        # The contract bounds a record to MAX_BYTES, so at most one byte past
        # that bound is ever read: an oversize file is rejected on the length
        # of what was asked for, without the remainder being loaded or decoded.
        try:
            with record_file.open("rb") as handle:
                raw = handle.read(board_observation.MAX_BYTES + 1)
        except OSError:
            payload["rejected"] += 1
            payload["warnings"].append({"file": record_file.name, "message": "could not read observation file"})
            continue
        if len(raw) > board_observation.MAX_BYTES:
            # The same closed diagnostic the contract itself raises for an
            # over-long record; it names no path and repeats no value.
            payload["rejected"] += 1
            payload["warnings"].append({"file": record_file.name, "message": "invalid_contract"})
            continue
        try:
            payload["records"].append(board_observation.decode(raw))
        except board_observation.BoardObservationError as exc:
            # The contract's diagnostics are a fixed closed vocabulary that
            # deliberately omits observed values and local paths.
            payload["rejected"] += 1
            payload["warnings"].append({"file": record_file.name, "message": str(exc)})
    if payload["records"]:
        payload["message"] = ""
    elif payload["rejected"]:
        payload["message"] = "no local Board observation passed the observation contract"
    return payload


def release_campaigns_payload(
    config: BoardConfig,
    *,
    now: Any = None,
) -> dict[str, Any]:
    try:
        from . import release_campaigns
    except ImportError:
        import release_campaigns  # type: ignore

    return release_campaigns.release_campaigns_board_payload(
        repo_path=config.repo_path,
        campaigns_dir=_campaigns_path(config) if config.campaigns_path else None,
        repo_slug=config.repo,
        now=now,
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


# The whole Board page as one template. It is plain text rather than an
# f-string so the shipped CSS and JavaScript read exactly as the browser
# receives them, with no doubled braces between the source and the page the
# tests execute. Only the two placeholders below are substituted.
_BOARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Code Mower Board</title>
  <style>
    :root { color-scheme: light; --bg:#f7f8f5; --ink:#1d2520; --muted:#66736b; --line:#d8ded7; --ok:#137a42; --warn:#9a5b00; --bad:#aa2e25; --panel:#ffffff; --focus:#145ea8; }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--ink); font: 14px/1.45 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    /* One visible focus ring for every interactive control, including the
       work rows and the view tabs, so keyboard position is never invisible. */
    :focus-visible { outline: 3px solid var(--focus); outline-offset: 2px; }
    header { display:flex; align-items:flex-end; justify-content:space-between; gap:16px; flex-wrap:wrap; padding:14px 20px 10px; border-bottom:1px solid var(--line); background:var(--panel); }
    h1 { margin:0; font-size:19px; font-weight:720; letter-spacing:0; }
    h2 { margin:0 0 10px; font-size:15px; letter-spacing:0; }
    h3 { margin:0 0 6px; font-size:14px; }
    h4 { margin:12px 0 4px; font-size:12px; text-transform:uppercase; letter-spacing:0.04em; color:var(--muted); }
    main { max-width:1180px; margin:0 auto; padding:14px 16px 40px; display:grid; gap:14px; }
    .chrome { position:sticky; top:0; z-index:2; background:var(--panel); border-bottom:1px solid var(--line); padding:8px 20px; display:grid; gap:8px; }
    /* Counts that the summary strip immediately below already carries. At
       phone widths the persistent chrome keeps only the next action, how
       current the snapshot is, and the view tabs, so it stays compact enough
       to remain pinned without eating the viewport. */
    @media (max-width: 699px) { .wide { display:none; } }
    .summary { display:grid; grid-template-columns:repeat(auto-fit, minmax(150px, 1fr)); gap:8px; }
    .metric, section.card { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; }
    .metric { padding:8px 10px; }
    .metric b { display:block; font-size:17px; margin-top:2px; }
    .muted { color:var(--muted); }
    .rows { display:grid; gap:10px; }
    .row { border-top:1px solid var(--line); padding-top:10px; }
    .row:first-child { border-top:0; padding-top:0; }
    .line { display:flex; flex-wrap:wrap; gap:8px 14px; align-items:center; }
    .pill { border:1px solid var(--line); border-radius:999px; padding:2px 8px; color:var(--muted); white-space:nowrap; }
    .ok { color:var(--ok); } .warn { color:var(--warn); } .bad { color:var(--bad); }
    .cue { font-weight:700; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
    code { background:#eef2ec; border-radius:4px; padding:1px 4px; }
    a { color:#145ea8; text-decoration:none; } a:hover { text-decoration:underline; }
    /* Views. Tabs are real buttons in a real tablist; panels are real
       tabpanels toggled with the hidden property, so an unselected view is
       removed from the accessibility tree rather than merely painted away. */
    .tabs { display:flex; flex-wrap:wrap; gap:6px; }
    .tab { font:inherit; border:1px solid var(--line); background:var(--bg); color:var(--muted); border-radius:999px; padding:6px 14px; cursor:pointer; }
    .tab[aria-selected="true"] { background:var(--panel); color:var(--ink); border-color:var(--ink); font-weight:650; }
    [hidden] { display:none !important; }
    /* An announcement region that carries text for assistive technology
       without occupying layout. */
    .sr { position:absolute; width:1px; height:1px; margin:-1px; padding:0; overflow:hidden; clip:rect(0 0 0 0); white-space:nowrap; border:0; }
    /* Work rows. Mobile first: the detail panel is a child of the selected
       row, so it always follows the row it belongs to in source order and in
       a single-column layout. */
    .workrows { list-style:none; margin:0; padding:0; display:grid; gap:8px; }
    .workrow { border:1px solid var(--line); border-radius:8px; background:var(--panel); }
    .workrow.selected { border-color:var(--ink); }
    .rowbtn { display:block; width:100%; text-align:left; font:inherit; color:inherit; background:none; border:0; border-radius:8px; padding:10px 12px; cursor:pointer; }
    .rowbtn .line { margin-bottom:4px; }
    .rowbtn .ref { font-weight:650; }
    .workdetail { border-top:1px solid var(--line); padding:12px; display:grid; gap:6px; }
    .evgroup { border-top:1px solid var(--line); padding-top:6px; }
    .evgroup:first-of-type { border-top:0; }
    .link { font:inherit; color:#145ea8; background:none; border:0; padding:0; cursor:pointer; text-decoration:underline; }
    /* Desktop: the same selected-row detail becomes an adjacent column beside
       the row it belongs to instead of moving in the DOM, so selection, focus
       order and announcements are identical at both widths. The row itself is
       the two-column grid and the detail is one of its items, so the detail
       stays in normal flow: the row, the list and the section are always at
       least as tall as the detail, and a list of one or two rows can never let
       the detail overlap the sections below it. Every row reserves the second
       column, so the row buttons keep one width whichever row is selected, and
       the row's frame moves onto the button so an unselected row is not drawn
       around an empty reserved column. */
    @media (min-width: 900px) {
      .workrow { display:grid; grid-template-columns:minmax(0, 1fr) 356px; column-gap:16px; align-items:start; border:0; border-radius:0; background:none; }
      .rowbtn { grid-column:1; grid-row:1; border:1px solid var(--line); background:var(--panel); }
      .workrow.selected .rowbtn { border-color:var(--ink); }
      .workdetail { grid-column:2; grid-row:1; max-height:70vh; overflow:auto; border:1px solid var(--line); border-radius:8px; background:var(--panel); }
    }
  </style>
</head>
<body>
  <header>
    <div><h1>Code Mower Board</h1><div class="muted" id="repo"></div><div class="muted" id="version"></div></div>
    <div class="muted" id="generated">Loading...</div>
  </header>
  <!-- Persistent compact chrome: the one next step, how current this snapshot
       is, and the view tabs stay on screen in every view and at every width. -->
  <div class="chrome">
    <div class="line" id="chrome"></div>
    <nav id="tabs" class="tabs" role="tablist" aria-label="Board views"></nav>
  </div>
  <!-- Meaningful changes are announced here. An unchanged poll writes nothing
       at all, so a screen reader is not told the same state every refresh. -->
  <div class="sr" id="announce" role="status" aria-live="polite" aria-atomic="true"></div>
  <main>
    <section class="view" id="panel-now" role="tabpanel" aria-labelledby="tab-now" tabindex="0">
      <div class="summary" id="summary"></div>
      <!-- Work first: current work, its evidence, and who is responsible come
           before aggregate productivity and release history, so the first
           viewport answers "what needs doing now", not "what happened". -->
      <section class="card"><h2 id="work-heading">Work</h2><div id="worklist"></div></section>
      <section class="card"><h2>Work Now</h2><div class="rows" id="worknow"></div></section>
      <section class="card"><h2>Participants</h2><div class="rows" id="participants"></div></section>
      <section class="card"><h2>Owner Queue</h2><div class="rows" id="owner"></div></section>
      <section class="card"><h2>Lane Work</h2><div class="rows" id="lanework"></div></section>
      <section class="card"><h2>Supervised Pilot</h2><div class="rows" id="supervised"></div></section>
      <section class="card"><h2>Open PRs</h2><div class="rows" id="prs"></div></section>
    </section>
    <section class="view" id="panel-timeline" role="tabpanel" aria-labelledby="tab-timeline" tabindex="0" hidden>
      <section class="card"><h2>Recent Changes</h2><div class="rows" id="changes"></div></section>
      <section class="card"><h2>Recent Local History</h2><div class="rows" id="history"></div></section>
      <section class="card"><h2>Reviewer Verdict Timeline</h2><div class="rows" id="verdicts"></div></section>
      <section class="card"><h2>Recent Code Mower Workflows</h2><div class="rows" id="runs"></div></section>
    </section>
    <section class="view" id="panel-releases" role="tabpanel" aria-labelledby="tab-releases" tabindex="0" hidden>
      <section class="card"><h2>Release Campaigns</h2><div class="rows" id="campaigns"></div></section>
      <section class="card"><h2>Productivity</h2><div class="rows" id="productivity"></div></section>
      <section class="card"><h2>Spend And Latency</h2><div class="rows" id="spend"></div></section>
    </section>
    <section class="view" id="panel-health" role="tabpanel" aria-labelledby="tab-health" tabindex="0" hidden>
      <section class="card"><h2>Connections And Sources</h2><div class="rows" id="sources"></div></section>
      <section class="card"><h2>Board Process And Version</h2><div class="rows" id="diagnostics"></div></section>
      <section class="card"><h2>Gate Alerts</h2><div class="rows" id="alerts"></div></section>
      <section class="card"><h2>Local Orchestrator Lease</h2><div class="rows" id="lease"></div></section>
      <section class="card"><h2>Agent Cards</h2><div class="rows" id="agents"></div></section>
      <section class="card"><h2>Local Activity</h2><div class="rows" id="local"></div></section>
    </section>
  </main>
  <script>
    const REPO = __REPO_JSON__;
    const REFRESH_MS = __REFRESH_MS_JSON__;
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
    const freshDelayMs = (cache) => {
      if (cache?.state !== "fresh") return null;
      const ttl = finiteNumber(cache?.ttl_seconds);
      const age = finiteNumber(cache?.age_seconds);
      if (ttl === null || age === null || ttl <= 0 || age < 0) return null;
      return Math.min(Math.max((ttl - age) * 1000, MIN_POLL_MS), REFRESH_MS);
    };
    const text = (value) => String(value ?? "");
    const esc = (value) => text(value).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#39;"}[c]));
    const put = (id, html) => document.getElementById(id).innerHTML = html;
    const pill = (value) => `<span class="pill">${esc(value)}</span>`;
    const statePill = (value, cls) => `<span class="pill ${esc(cls || "muted")}">${esc(value)}</span>`;
    const empty = (message) => `<div class="muted">${esc(message)}</div>`;
    const href = (value) => /^https?:\\/\\//i.test(text(value)) ? text(value) : "#";
    // --- presentation truth helpers (BEGIN) ---
    // Pure, DOM-free projections of the existing /api/status payload. They add
    // no fields; they only stop the page from asserting more than the payload
    // records. Kept self-contained so the shipped code can be executed
    // directly by the tests instead of restated in Python.
    const NOT_RECORDED = "not recorded";
    const GATE_CONTEXT = "code-mower/gate";
    // A snapshot this much older than now is reported by age alone, whatever
    // the payload calls its source: a wedged refresh must not keep presenting
    // an old observation as the current state of the world.
    const STALE_OBSERVATION_SECONDS = 600;
    // Only a real JSON number is a recorded measurement. Number(null),
    // Number("") and Number(false) are all a finite 0, so the usual
    // Number.isFinite(Number(v)) test silently reports "no data" as zero.
    const measured = (value) => (typeof value === "number" && Number.isFinite(value) ? value : null);
    const display = (value) => (value === null || value === undefined || value === "" ? NOT_RECORDED : String(value));
    const seconds = (value) => {
      const number = measured(value);
      return number === null ? NOT_RECORDED : `${number.toFixed(1)}s`;
    };
    const money = (value) => {
      const number = measured(value);
      return number === null ? NOT_RECORDED : `$${number.toFixed(3)}`;
    };
    const countOf = (available, value) => (available ? String(value) : NOT_RECORDED);
    // An absent, unknown or unavailable state is neutral, never green and
    // never "pass". Only a state the payload actually reports as good earns
    // the ok colour.
    const UNKNOWN_STATE_RE = /^(unknown|unavailable|absent|none|not recorded|no data|off|n\\/a)$/i;
    const stateClass = (value) => {
      const state = text(value).trim();
      if (state === "" || UNKNOWN_STATE_RE.test(state)) return "muted";
      if (/fail|error|blocked|expired|overdue/i.test(state)) return "bad";
      if (/warn|pending|waiting|queued|progress|stale|unverified|last reported/i.test(state)) return "warn";
      return "ok";
    };
    const parseMs = (value) => {
      const parsed = Date.parse(text(value));
      return Number.isFinite(parsed) ? parsed : null;
    };
    const ageText = (secs) => {
      const number = measured(secs);
      if (number === null) return NOT_RECORDED;
      if (number >= 3600) return `${(number / 3600).toFixed(1)}h`;
      if (number >= 60) return `${(number / 60).toFixed(0)}m`;
      return `${Math.max(number, 0).toFixed(0)}s`;
    };
    const ageSeconds = (value, nowMs) => {
      const at = parseMs(value);
      return at === null ? null : Math.max((nowMs - at) / 1000, 0);
    };
    // The `code-mower/gate` commit status is the verdict. The only thing that
    // publishes it is the canonical gate workflow and its publishing job, so
    // the publisher is an allowlist of those two names rather than anything
    // gate-shaped: an unrelated `security-gate` check is an ordinary check,
    // not a Code Mower publisher.
    const GATE_PUBLISHER_NAMES = ["code mower gate", "publish code mower gate status"];
    // Case- and whitespace-insensitive comparison for payload identifiers.
    const normalized = (value) => text(value).trim().toLowerCase().replace(/\\s+/g, " ");
    const isGateContext = (name) => normalized(name) === GATE_CONTEXT;
    const isGatePublisher = (name) => GATE_PUBLISHER_NAMES.includes(normalized(name));
    function gateVerdict(pr) {
      const list = Array.isArray(pr?.checks) ? pr.checks : [];
      const verdict = list.find(check => isGateContext(check?.name));
      if (!verdict) return {state: NOT_RECORDED, recorded: false, class: "muted"};
      const state = text(verdict.state).trim() || "unknown";
      return {state, recorded: true, class: stateClass(state)};
    }
    // Reasons the Board can raise for one work item, and the role that clears
    // each one. Rebase, CI repair, audit fixes and re-review are routine lane
    // work owned by the builder or the orchestrator. Owner attention is
    // reserved for reasons carrying explicit permission, budget, policy,
    // product-decision or owner-request evidence in the payload's own labels.
    const ATTENTION_REASONS = {
      "needs-owner": {rank: 0, role: "owner"},
      "blocked-audit": {rank: 1, role: "builder"},
      "failing-check": {rank: 2, role: "builder"},
      "rebase-needed": {rank: 3, role: "builder"},
      "stale-gate": {rank: 4, role: "orchestrator"},
      "draft": {rank: 5, role: "builder"}
    };
    const UNKNOWN_REASON = {rank: 8, role: "orchestrator"};
    const OWNER_EVIDENCE_RE = /^(needs-owner|owner-request|owner-decision|owner-approval|needs-permission|permission-required|needs-budget|budget-approval|needs-policy|policy-decision|needs-product-decision|product-decision)$/i;
    function ownerEvidence(...sources) {
      const names = [];
      for (const source of sources) {
        const values = Array.isArray(source) ? source : Object.values(source || {}).flat();
        for (const value of values) {
          const name = text(value).trim();
          if (OWNER_EVIDENCE_RE.test(name) && !names.includes(name)) names.push(name);
        }
      }
      return names;
    }
    // One PR is one work item. The owner queue payload emits a separate entry
    // per reason, so several reasons for the same PR are grouped here instead
    // of rendering as unrelated rows and inflating the owner count.
    function attentionItems(entries, prs) {
      const byNumber = new Map();
      for (const pr of Array.isArray(prs) ? prs : []) byNumber.set(pr?.number, pr);
      const items = new Map();
      for (const entry of Array.isArray(entries) ? entries : []) {
        const number = entry?.pr_number;
        const reason = ATTENTION_REASONS[entry?.kind] || UNKNOWN_REASON;
        let item = items.get(number);
        if (!item) {
          const pr = byNumber.get(number) || {};
          item = {
            pr_number: number,
            title: entry?.title || pr.title || "",
            url: entry?.url || pr.url || "",
            branch: entry?.branch || pr.branch || "",
            author: entry?.author || pr.author || "",
            updated_at: entry?.updated_at || pr.updated_at || "",
            head_sha_prefix: entry?.head_sha_prefix || "",
            gate: gateVerdict(pr),
            evidence: ownerEvidence(pr.labels, entry?.labels),
            reasons: [],
            next_action: "",
            rank: UNKNOWN_REASON.rank + 1
          };
          items.set(number, item);
        }
        const kind = text(entry?.kind).trim() || "attention";
        if (!item.reasons.some(existing => existing.kind === kind)) {
          item.reasons.push({kind, role: reason.role, next_action: text(entry?.next_action)});
        }
        for (const name of ownerEvidence(entry?.labels)) {
          if (!item.evidence.includes(name)) item.evidence.push(name);
        }
        if (reason.rank < item.rank) {
          item.rank = reason.rank;
          item.next_action = text(entry?.next_action);
        }
      }
      return [...items.values()].map(item => {
        // A reason only a builder or the orchestrator can clear never promotes
        // to owner attention, and a reason that claims owner attention without
        // explicit evidence falls back to orchestrator triage.
        const claimsOwner = item.reasons.some(reason => reason.role === "owner");
        const role = claimsOwner && item.evidence.length
          ? "owner"
          : item.reasons.every(reason => reason.role === "orchestrator") || claimsOwner
            ? "orchestrator"
            : "builder";
        return {...item, role, next_action: item.next_action || item.reasons[0]?.next_action || "inspect"};
      }).sort((a, b) => (a.role === b.role ? 0 : a.role === "owner" ? -1 : b.role === "owner" ? 1 : 0)
        || a.rank - b.rank
        || (a.pr_number ?? 0) - (b.pr_number ?? 0));
    }
    // Board snapshots can be replayed from local history, served from a cache
    // the server has not confirmed, or carry no observation time at all. Each
    // of those may only report what was last observed; none of them may claim
    // that anything is running right now.
    function observation(data, nowMs) {
      const current = data?.productivity?.current || {};
      const cache = data?.board?.cache || {};
      const observedAt = text(current.observed_at) || text(data?.generated_at);
      const observedAge = ageSeconds(observedAt, nowMs);
      const cacheAge = measured(cache.age_seconds);
      // Only `fresh` is a snapshot the server has confirmed as current. It
      // answers a cold cache with metadata only and a stale one with the
      // previous snapshot, so any other reported state -- including a future
      // one this page does not know -- is serving unconfirmed data, however
      // recent the embedded observation time looks.
      const cacheState = normalized(cache.state);
      const unconfirmed = cacheState !== "" && cacheState !== "fresh";
      // Take the older of the two recorded ages so an unconfirmed snapshot can
      // never understate how old what is on screen actually is.
      const age = observedAge === null
        ? cacheAge
        : cacheAge === null ? observedAge : Math.max(observedAge, cacheAge);
      // No parseable observation time anywhere is not evidence of freshness,
      // so it may not produce a "live" claim or a synthetic last-observed age.
      const unknownAge = age === null;
      const historical = current.source === "historical_board_snapshot" || current.historical === true;
      const remoteAvailable = data?.remote?.available === true;
      const aged = age !== null && age > STALE_OBSERVATION_SECONDS;
      const stale = historical || aged || unconfirmed || unknownAge || !remoteAvailable;
      return {
        age_text: ageText(age),
        historical,
        aged,
        unconfirmed,
        live: !stale,
        label: unknownAge
          ? "observation time not recorded"
          : stale ? `last observed ${ageText(age)} ago` : `live, observed ${ageText(age)} ago`,
        class: unknownAge ? "muted" : stale ? "warn" : "ok",
        detail: historical
          ? "Replayed from the last recorded local Board snapshot; nothing here is evidence of work running now."
          : unconfirmed
            ? `The Board server is serving a ${cacheState} cached snapshot it has not confirmed; nothing here is evidence of work running now.`
            : unknownAge
              ? "No observation time is recorded, so this snapshot cannot be shown as current."
              : remoteAvailable
                ? ""
                : "GitHub is unavailable, so remote counts below are last observed rather than current."
      };
    }
    // Which local-only inputs the snapshot actually carries. Fresh GitHub data
    // stays useful when they are missing, but the page has to say so rather
    // than render their absence as a zero.
    function localSources(data) {
      const adapters = data?.agent_adapters || {};
      // No adapter directory at all is "never measured", which is a different
      // statement from an empty directory reporting zero live agents.
      const adaptersAvailable = adapters.available !== false && adapters.path_exists === true;
      const missing = [];
      if (!adaptersAvailable) missing.push("agent adapter cards");
      if (text(data?.orchestrator_lease?.state) !== "active") missing.push("orchestrator lease");
      if (!(data?.timelines?.verdicts?.entries || []).length) missing.push("reviewer verdict history");
      if (!(data?.timelines?.spend?.groups || []).length) missing.push("reviewer spend rows");
      return {
        adapters_available: adaptersAvailable,
        missing,
        message: missing.length
          ? `Local session data unavailable: ${missing.join(", ")}. GitHub data above is unaffected.`
          : "Local session data available."
      };
    }
    const CLAIMS_RUNNING_RE = /^(running|dispatched|in_progress)$/i;
    // The provider states in which a card is still waiting for a response.
    // Of release_campaigns' five valid provider states, `complete` and
    // `blocked` are terminal qualification evidence (its
    // TERMINAL_EVIDENCE_STATES) and `unavailable` never dispatched, so only
    // `queued` and `running` are awaiting one.
    const AWAITING_CARD_STATE_RE = /^(queued|running|dispatched|in_progress)$/i;
    // A campaign file records accumulated provider work time, not liveness.
    // The one wall-clock signal it carries is a provider response deadline:
    // once that has passed -- or was never recorded -- nothing in the payload
    // shows the provider still working.
    function cardLiveness(card, nowMs) {
      const state = text(card?.state).trim() || "unknown";
      const deadline = parseMs(card?.response_deadline_at);
      const awaiting = AWAITING_CARD_STATE_RE.test(state);
      // A card that already answered, or never dispatched, can still carry the
      // deadline it was given. That retained timestamp says nothing about a
      // late provider, so it must not mark the card overdue and repaint a
      // terminal state -- turning a passed `complete` yellow, or downgrading a
      // failed `blocked` from red to yellow.
      const overdue = awaiting && deadline !== null && deadline < nowMs;
      const suppressed = CLAIMS_RUNNING_RE.test(state) && (overdue || deadline === null);
      return {
        label: suppressed ? `last reported ${state}` : state,
        awaiting,
        overdue,
        deadline_recorded: deadline !== null,
        overdue_for: overdue ? ageText((nowMs - deadline) / 1000) : "",
        class: suppressed || overdue ? "warn" : stateClass(state)
      };
    }
    function campaignLiveness(campaign, nowMs) {
      const status = text(campaign?.status).trim() || "unknown";
      const cards = (Array.isArray(campaign?.cards) ? campaign.cards : []).map(card => cardLiveness(card, nowMs));
      // Only a card actually awaiting a response can evidence a live campaign:
      // an unexpired deadline retained by a finished card proves nothing.
      const unverified = CLAIMS_RUNNING_RE.test(status)
        && !cards.some(card => card.awaiting && card.deadline_recorded && !card.overdue);
      return {
        cards,
        unverified,
        label: unverified ? `last reported ${status}` : status,
        class: unverified ? "warn" : stateClass(status)
      };
    }
    // --- presentation truth helpers (END) ---
    // --- work view model (BEGIN) ---
    // Pure, DOM-free projections of the frozen local observation contract
    // `code_mower.boardObservation.v1`. They read what a record states and add
    // nothing to it: no percentage, no ETA, no inferred liveness, no derived
    // remote identity, and no route for a record that carries none.
    const STAGE_LABELS = {
      unknown: "not recorded",
      queued: "queued",
      building: "building",
      in_review: "in review",
      changes_requested: "changes requested",
      ready_for_human_review: "ready for human review",
      ready_to_merge: "ready to merge",
      merged: "merged"
    };
    const ACTION_LABELS = {
      none: "no next action recorded",
      respond_to_approval: "respond to approval",
      answer_question: "answer the question",
      restore_source: "restore the source",
      connect_session: "connect the session",
      refresh_evidence: "refresh evidence",
      inspect_failure: "inspect the failure",
      inspect_provider: "inspect the provider",
      inspect_cancellation: "inspect the cancellation",
      address_findings: "address review findings",
      update_branch: "update the branch",
      fix_checks: "fix failing checks",
      resolve_gate: "resolve the gate",
      request_review: "request review",
      review_current_head: "review the current head",
      finish_review: "finish the review",
      wait_for_checks: "wait for checks",
      wait_for_gate: "wait for the gate",
      review_change: "review the change",
      merge: "merge"
    };
    const ACTOR_LABELS = {
      none: "no responsible role recorded",
      owner: "owner",
      orchestrator: "orchestrator",
      builder: "builder",
      reviewer: "reviewer",
      automation: "automation",
      maintainer: "maintainer"
    };
    // The contract's own phase vocabulary. Nothing renders from these two
    // tables directly: they are the base the lifecycle-aware run-state tables
    // below are derived from, and every display reads those instead.
    const PHASE_LABELS = {
      assigned: "assigned",
      dispatched: "dispatched",
      observed_running: "observed running",
      provider_progress: "provider reported progress",
      waiting_for_user: "waiting for an answer",
      waiting_for_approval: "waiting for approval",
      implementation_complete: "implementation complete",
      failed: "failed",
      cancelled: "cancelled"
    };
    const PHASE_CLASSES = {
      assigned: "muted",
      dispatched: "warn",
      observed_running: "warn",
      provider_progress: "warn",
      waiting_for_user: "warn",
      waiting_for_approval: "warn",
      implementation_complete: "ok",
      failed: "bad",
      cancelled: "warn"
    };
    // The frozen contract records a session the provider paused as the
    // `suspended` lifecycle state, and it requires that state to carry the
    // `failed` phase -- so any display built from the phase alone reports a
    // paused session as a failed one. A lifecycle state is mapped here only
    // where it means something its phase cannot say; every other state is
    // already told truthfully by its own phase and is used unchanged, so
    // cancelled stays cancelled, an actual lifecycle failure stays failed,
    // and complete and running keep reading as themselves.
    const LIFECYCLE_RUN_STATES = {suspended: "suspended"};
    const RUN_STATE_LABELS = {...PHASE_LABELS, suspended: "suspended"};
    const RUN_STATE_CLASSES = {...PHASE_CLASSES, suspended: "warn"};
    // The one lifecycle-aware reading of one run. Everything that names,
    // colours, groups, counts or summarizes a run reads this, so a run can
    // never be called one thing in the headline and another in the evidence,
    // the assignments or the participant summary. Raw `phase` survives only
    // where it is labelled as contract evidence rather than operator status.
    function runState(run) {
      return lookup(LIFECYCLE_RUN_STATES, run?.lifecycle?.state, text(run?.phase));
    }
    function runDisplay(run, fallback) {
      const state = runState(run);
      const cls = lookup(RUN_STATE_CLASSES, state, "muted");
      return {
        state,
        label: lookup(RUN_STATE_LABELS, state, state || text(fallback) || "unknown"),
        class: cls,
        cue: cueFor(cls)
      };
    }
    const BASIS_LABELS = {
      configured: "from configuration",
      requested: "from a request",
      observed: "observed directly",
      provider_reported: "reported by the provider"
    };
    // Colour is never the only carrier: every state renders a text label and
    // one of these text cues, so an unknown state stays visibly neutral.
    const CUES = {ok: "+", warn: "~", bad: "!", muted: "?"};
    const cueFor = (cls) => CUES[cls] || CUES.muted;
    const lookup = (table, value, fallback) => {
      const key = text(value);
      return Object.prototype.hasOwnProperty.call(table, key) ? table[key] : fallback;
    };
    // An observation older than this is reported by age alone, whatever its
    // sources claim: a producer that stopped writing must not keep a screen
    // asserting the state of the world.
    const OBSERVATION_STALE_SECONDS = 600;
    const FRESHNESS_RANK = {fresh: 0, stale: 1, unavailable: 2};
    const SOURCE_COVERAGE_RANK = {complete: 0, partial: 1, unavailable: 2};
    const FRESHNESS_CLASSES = {fresh: "ok", stale: "warn", unavailable: "bad"};
    const arrayOf = (value) => (Array.isArray(value) ? value : []);
    const records = (data) => arrayOf(data?.observations?.records);
    const sourceIndex = (record) => {
      const index = {};
      for (const source of arrayOf(record?.sources)) index[text(source?.id)] = source;
      return index;
    };
    const newestMs = (values) => {
      let best = null;
      for (const value of values) {
        const at = parseMs(value);
        if (at !== null && (best === null || at > best)) best = at;
      }
      return best;
    };
    // Which of two recorded times is the later one, answered as the recorded
    // string rather than as a number, so a consolidated reading keeps the exact
    // timestamp a producer wrote instead of a reformatting of it.
    const laterTimestamp = (left, right) => {
      const at = parseMs(left);
      const other = parseMs(right);
      if (other === null) return at === null ? null : left;
      if (at === null) return right;
      return other > at ? right : left;
    };
    // `checked_at` advances on every poll whether or not anything happened, so
    // it can never be the last meaningful update. Only a recorded event time
    // can be -- and, when a record carries none, the time something was last
    // observed, reported as such.
    function lastMeaningfulUpdate(record) {
      const sources = arrayOf(record?.sources);
      const runs = arrayOf(record?.work?.runs);
      const event = newestMs([...sources.map(item => item?.event_at), ...runs.map(item => item?.event_at)]);
      if (event !== null) return {at: event, basis: "event"};
      const observed = newestMs([
        ...sources.map(item => item?.observed_at),
        ...runs.map(item => item?.observed_at),
        ...arrayOf(record?.unlinked).map(item => item?.observed_at)
      ]);
      return observed === null ? {at: null, basis: "none"} : {at: observed, basis: "observation"};
    }
    function updateText(update, nowMs) {
      if (update.at === null) return NOT_RECORDED;
      const age = ageText(Math.max((nowMs - update.at) / 1000, 0));
      return update.basis === "event" ? `${age} ago` : `observed ${age} ago`;
    }
    function worstFreshness(sources) {
      if (!sources.length) return "unavailable";
      let worst = "fresh";
      for (const source of sources) {
        const value = text(source?.freshness);
        if ((FRESHNESS_RANK[value] ?? 3) > (FRESHNESS_RANK[worst] ?? 3)) worst = value;
      }
      return worst;
    }
    // How current one record is. A record is only allowed to read as current
    // when every source behind it is fresh and the record itself is recent;
    // anything else is reported as the last observation it is.
    function recordFreshness(record, nowMs) {
      const sources = arrayOf(record?.sources);
      const createdAt = parseMs(record?.created_at);
      const ageSeconds = createdAt === null ? null : Math.max((nowMs - createdAt) / 1000, 0);
      const worst = worstFreshness(sources);
      const aged = ageSeconds === null || ageSeconds > OBSERVATION_STALE_SECONDS;
      const current = worst === "fresh" && !aged;
      const unavailable = sources.filter(item => text(item?.freshness) === "unavailable").map(item => text(item?.kind));
      const stale = sources.filter(item => text(item?.freshness) === "stale").map(item => text(item?.kind));
      const partial = sources.filter(item => text(item?.coverage) === "partial").map(item => text(item?.kind));
      const notes = [];
      if (unavailable.length) notes.push(`Source unavailable: ${unavailable.join(", ")}. The last recorded observation is shown and is not evidence of work running now.`);
      if (stale.length) notes.push(`Source stale: ${stale.join(", ")}. What follows is what was last observed there.`);
      if (partial.length) notes.push(`Partial coverage: ${partial.join(", ")} reported part of what it covers, so counts below are what was observed, not a total.`);
      if (ageSeconds === null) notes.push("No observation time is recorded, so this record cannot be shown as current.");
      else if (aged) notes.push(`This observation is ${ageText(ageSeconds)} old, so it is shown as last observed rather than current.`);
      return {
        state: worst,
        current,
        age_text: ageSeconds === null ? NOT_RECORDED : ageText(ageSeconds),
        label: ageSeconds === null
          ? "observation time not recorded"
          : current ? `observed ${ageText(ageSeconds)} ago` : `last observed ${ageText(ageSeconds)} ago`,
        class: ageSeconds === null ? "muted" : current ? "ok" : unavailable.length ? "bad" : "warn",
        unavailable_sources: unavailable,
        stale_sources: stale,
        partial_sources: partial,
        detail: notes.join(" ")
      };
    }
    // Every distinct truth the contract can record about one work item gets its
    // own label, so review requested, review observed running, stale review,
    // changes requested, implementation complete, ready for human review,
    // ready to merge and merged can never collapse into one another. The
    // lowest-ranked match is the row headline; the rest stay as state cues.
    const STATE_RULES = [
      {rank: 0, label: "merged", cls: "ok", when: (w) => w.stage === "merged" || w.merge === "merged"},
      {rank: 1, label: "ready to merge", cls: "ok", when: (w) => w.stage === "ready_to_merge" || w.reasons.includes("ready_to_merge")},
      {rank: 2, label: "changes requested", cls: "bad", when: (w) => w.stage === "changes_requested" || w.reasons.includes("changes_requested") || w.review === "blocked"},
      // The branch cannot merge until it is updated. Nothing has failed, so
      // this is not reported as a failure, but the builder still owes the
      // update before anything else about this work can move.
      {rank: 3, label: "branch update required", cls: "warn", when: (w) => w.reasons.includes("update_required")},
      {rank: 4, label: "CI failed", cls: "bad", when: (w) => w.ci === "failed" || w.reasons.includes("ci_failed")},
      {rank: 5, label: "gate failed", cls: "bad", when: (w) => w.gate === "failed" || w.reasons.includes("gate_failed")},
      {rank: 6, label: "provider run failed", cls: "bad", when: (w) => w.failed || w.reasons.includes("provider_failed")},
      // The contract records a suspended session as the `suspended` lifecycle
      // state, and only ever alongside the `failed` phase -- so reading the
      // phase alone would report a session the provider paused as a session
      // that failed. Suspension is reported as itself, and the failure rule
      // above is narrowed to runs that are not suspended, so neither claim is
      // ever made on the other's evidence.
      {rank: 7, label: "provider run suspended", cls: "warn", when: (w) => w.suspended || w.reasons.includes("provider_suspended")},
      {rank: 8, label: "provider run cancelled", cls: "warn", when: (w) => w.phases.includes("cancelled") || w.reasons.includes("cancelled")},
      {rank: 9, label: "source unavailable", cls: "bad", when: (w) => w.reasons.includes("source_unavailable")},
      {rank: 10, label: "waiting for approval", cls: "warn", when: (w) => w.reasons.includes("approval_required") || w.phases.includes("waiting_for_approval")},
      {rank: 11, label: "waiting for an answer", cls: "warn", when: (w) => w.reasons.includes("user_input_required") || w.phases.includes("waiting_for_user")},
      {rank: 12, label: "stale observation", cls: "warn", when: (w) => w.reasons.includes("stale_observation")},
      {rank: 13, label: "stale review", cls: "warn", when: (w) => w.review === "stale" || w.reasons.includes("review_stale")},
      {rank: 14, label: "ready for human review", cls: "warn", when: (w) => w.stage === "ready_for_human_review" || w.reasons.includes("human_review_required")},
      {rank: 15, label: "review observed running", cls: "warn", when: (w) => w.review === "running" || w.reasons.includes("review_in_progress")},
      {rank: 16, label: "implementation complete", cls: "ok", when: (w) => w.phases.includes("implementation_complete")},
      // A requested review that already has a verdict is no longer waiting on
      // one, so the request stops being reported as an outstanding state.
      {rank: 17, label: "review requested", cls: "warn", when: (w) => (w.request === "requested" || w.reasons.includes("review_requested")) && !["pass", "blocked", "stale", "running"].includes(w.review)},
      {rank: 18, label: "CI pending", cls: "warn", when: (w) => w.ci === "pending" || w.reasons.includes("ci_pending")},
      {rank: 19, label: "gate pending", cls: "warn", when: (w) => w.gate === "pending" || w.reasons.includes("gate_pending")},
      {rank: 20, label: "review passed", cls: "ok", when: (w) => w.review === "pass"},
      {rank: 21, label: "provider reported progress", cls: "warn", when: (w) => w.phases.includes("provider_progress")},
      {rank: 22, label: "provider run observed", cls: "warn", when: (w) => w.phases.includes("observed_running")},
      {rank: 23, label: "dispatched", cls: "warn", when: (w) => w.phases.includes("dispatched")},
      {rank: 24, label: "assigned", cls: "muted", when: (w) => w.phases.includes("assigned") || w.assignment === "assigned"},
      {rank: 25, label: "identity unlinked", cls: "warn", when: (w) => w.reasons.includes("identity_unlinked")}
    ];
    function workStates(work) {
      const evidence = work?.evidence || {};
      const runs = arrayOf(work?.runs);
      // The same lifecycle-aware reading the run-level displays use, so the
      // headline and the evidence, assignment and participant displays can
      // only ever agree about what one run is.
      const runStates = runs.map(runState);
      const facts = {
        stage: text(work?.stage),
        reasons: arrayOf(work?.reasons).map(text),
        phases: runStates,
        suspended: runStates.includes("suspended"),
        failed: runStates.includes("failed"),
        review: text(evidence.review?.state),
        request: text(evidence.review_request?.state),
        ci: text(evidence.ci?.state),
        gate: text(evidence.gate?.state),
        merge: text(evidence.merge?.state),
        assignment: text(evidence.assignment?.state)
      };
      const states = STATE_RULES.filter(rule => rule.when(facts)).map(rule => ({label: rule.label, class: rule.cls, cue: cueFor(rule.cls)}));
      return states.length ? states : [{label: "state not recorded", class: "muted", cue: CUES.muted}];
    }
    // The opaque identity a selection is kept against. It is built only from
    // identity the contract already fixes -- session, worktree and work id --
    // so it survives a refresh that reorders, adds or drops rows.
    function workKey(record) {
      const scope = record?.scope || {};
      const kind = text(record?.kind);
      if (kind === "work") return `work:${text(scope.session_id)}:${text(scope.worktree_id)}:${text(record?.work?.id)}`;
      if (kind === "no_work") return `idle:${text(scope.session_id)}:${text(scope.worktree_id)}`;
      return `unlinked:${text(scope.repository)}`;
    }
    // Everything that makes one poll meaningfully different from the last.
    // `created_at`, every `checked_at`, every `observed_at` and every
    // `heartbeat_at` are excluded on purpose: they advance on every successful
    // poll whether or not anything happened, and including them would make an
    // unchanged snapshot look like news in the Timeline and to a screen reader.
    function workSignature(record) {
      const parts = [text(record?.kind), text(record?.display?.authorized), text(record?.display?.session_label)];
      const work = record?.work;
      if (work) {
        parts.push(
          text(work.stage),
          arrayOf(work.reasons).map(text).join("+"),
          text(work.primary?.actor),
          text(work.primary?.action),
          text(work.pull_request?.number),
          text(work.pull_request?.head_sha)
        );
        const evidence = work.evidence || {};
        for (const name of Object.keys(evidence).sort()) {
          const item = evidence[name] || {};
          parts.push(`${name}=${text(item.state)}:${text(item.source_id)}:${text(item.head_sha)}:${text(item.coverage)}`);
        }
        const measurements = work.measurements || {};
        for (const name of Object.keys(measurements).sort()) {
          const item = measurements[name] || {};
          parts.push(`${name}=${text(item.value)}:${text(item.coverage)}:${text(item.observed)}:${text(item.total)}`);
        }
        // Raw contract evidence, not operator status: the recorded phase and
        // the recorded lifecycle state are both included so a run that moves
        // between them -- suspended to failed, or back -- is detected as a
        // change even though neither reading is rendered from here.
        for (const run of arrayOf(work.runs).slice().sort((a, b) => text(a?.id).localeCompare(text(b?.id)))) {
          parts.push(`run:${text(run?.id)}=${text(run?.provider)}:${text(run?.role)}:${text(run?.phase)}:${text(run?.basis)}:${text(run?.reported_stage)}:${text(run?.event_at)}:${text(run?.lifecycle?.state)}:${text(run?.lifecycle?.reason)}`);
        }
      }
      for (const run of arrayOf(record?.unlinked).slice().sort((a, b) => text(a?.id).localeCompare(text(b?.id)))) {
        parts.push(`unlinked:${text(run?.id)}=${text(run?.provider)}:${text(run?.role)}`);
      }
      for (const source of arrayOf(record?.sources).slice().sort((a, b) => text(a?.id).localeCompare(text(b?.id)))) {
        parts.push(`source:${text(source?.id)}=${text(source?.kind)}:${text(source?.freshness)}:${text(source?.coverage)}:${text(source?.event_at)}`);
      }
      return parts.join("|");
    }
    function measurementText(measurement, unit) {
      const coverage = text(measurement?.coverage);
      if (coverage === "unavailable") return NOT_RECORDED;
      const value = measured(measurement?.value);
      if (value === null) return NOT_RECORDED;
      const shown = unit === "usd" ? money(value) : unit === "seconds" ? seconds(value) : String(value);
      const observed = measured(measurement?.observed);
      const total = measured(measurement?.total);
      // Partial coverage is reported as the counted evidence it is. Nothing is
      // extrapolated to a whole and no ratio is turned into a percentage.
      return coverage === "partial" && observed !== null && total !== null
        ? `${shown} from ${observed} of ${total} recorded`
        : shown;
    }
    function sourceNote(source) {
      if (!source) return "source not recorded";
      const freshness = text(source.freshness) || "unknown";
      const coverage = text(source.coverage) || "unknown";
      return `${text(source.kind) || "source"}, ${freshness}, ${coverage} coverage`;
    }
    // The contract's evidence states are a closed vocabulary, so each one is
    // classified explicitly. Anything unrecognised -- and every not-started,
    // absent or unverifiable reading -- stays neutral rather than defaulting
    // into a colour that would read as a pass.
    const EVIDENCE_STATE_CLASSES = {
      unknown: "muted",
      none: "muted",
      not_started: "muted",
      unverifiable: "muted",
      absent: "muted",
      unassigned: "muted",
      not_requested: "muted",
      draft: "muted",
      open: "muted",
      held: "ok",
      assigned: "ok",
      pass: "ok",
      ready: "ok",
      merged: "ok",
      requested: "warn",
      running: "warn",
      pending: "warn",
      stale: "warn",
      expired: "bad",
      blocked: "bad",
      failed: "bad",
      closed_unmerged: "bad"
    };
    function evidenceItem(name, label, item, source, options) {
      const state = text(item?.state) || "unknown";
      const settings = options || {};
      const cls = settings.class || lookup(EVIDENCE_STATE_CLASSES, state, "muted");
      return {
        name,
        label,
        state: state.replace(/_/g, " "),
        class: cls,
        cue: cueFor(cls),
        source: sourceNote(source),
        head: text(item?.head_sha) ? text(item.head_sha).slice(0, 7) : "",
        coverage: text(item?.coverage),
        note: settings.note || ""
      };
    }
    // Six independent readings of the same work item. Each one names the
    // source it came from, so a green publisher run, an assignment record and
    // a merge state can never stand in for one another.
    function evidenceGroups(record) {
      const work = record?.work || {};
      const evidence = work.evidence || {};
      const sources = sourceIndex(record);
      const runs = arrayOf(work.runs);
      const reasons = arrayOf(work.reasons).map(text);
      const builder = runs.length
        ? runs.map(run => {
            const presentation = runDisplay(run);
            return {
              name: "run",
              label: `${text(run?.provider) || "provider"} ${text(run?.role) || "role"}`,
              state: presentation.label,
              class: presentation.class,
              cue: presentation.cue,
              source: sourceNote(sources[text(run?.source_id)]),
              head: "",
              coverage: "",
              note: `${lookup(BASIS_LABELS, run?.basis, text(run?.basis))}${text(run?.reported_stage) ? `; provider reported stage ${text(run.reported_stage)}` : ""}${run?.heartbeat_at ? "; heartbeat recorded" : "; no heartbeat recorded"}`
            };
          })
        : [{name: "run", label: "provider run", state: NOT_RECORDED, class: "muted", cue: CUES.muted, source: "no run recorded", head: "", coverage: "", note: "No provider run is recorded for this work item."}];
      const policyItems = [
        evidenceItem("lease", "orchestrator lease", evidence.lease, sources[text(evidence.lease?.source_id)]),
        evidenceItem("assignment", "assignment", evidence.assignment, sources[text(evidence.assignment?.source_id)], {note: "An assignment is a record of intent, not of execution."})
      ];
      for (const reason of ["approval_required", "user_input_required", "human_review_required"]) {
        if (reasons.includes(reason)) {
          policyItems.push({
            name: reason,
            label: "human policy",
            state: reason.replace(/_/g, " "),
            class: "warn",
            cue: CUES.warn,
            source: "recorded reason",
            head: "",
            coverage: "",
            note: "A person has to act before this can move."
          });
        }
      }
      return [
        {name: "builder", label: "Builder", items: builder},
        {name: "review", label: "Review", items: [
          evidenceItem("review_request", "review requested", evidence.review_request, sources[text(evidence.review_request?.source_id)]),
          evidenceItem("review", "review verdict", evidence.review, sources[text(evidence.review?.source_id)])
        ]},
        {name: "ci", label: "CI", items: [
          evidenceItem("ci", "checks", evidence.ci, sources[text(evidence.ci?.source_id)])
        ]},
        {name: "gate", label: "Gate", items: [
          evidenceItem("gate", "code-mower/gate verdict", evidence.gate, sources[text(evidence.gate?.source_id)]),
          evidenceItem("gate_publisher", "gate publisher run", evidence.gate_publisher, sources[text(evidence.gate_publisher?.source_id)], {note: "Publisher execution only; it is not the gate verdict."})
        ]},
        {name: "merge", label: "Merge", items: [
          evidenceItem("merge", "merge state", evidence.merge, sources[text(evidence.merge?.source_id)])
        ]},
        {name: "policy", label: "Human policy", items: policyItems}
      ];
    }
    function workRow(record, nowMs) {
      const kind = text(record?.kind);
      const key = workKey(record);
      const freshness = recordFreshness(record, nowMs);
      const update = lastMeaningfulUpdate(record);
      // Every run this row is built from, with the freshness of the source
      // behind it resolved here, where that record's source index is already in
      // hand. The participant summary reads these rather than the records on
      // disk, so it counts exactly the runs the rendered rows were built from
      // and nothing a newer observation has already superseded.
      const runSources = sourceIndex(record);
      const participants = [...arrayOf(record?.work?.runs), ...arrayOf(record?.unlinked)].map(run => ({
        provider: text(run?.provider) || "unknown",
        role: text(run?.role) || "unknown",
        // The lifecycle-aware run state, not the raw phase, so a participant
        // is summarized as exactly what its row and its evidence panel say.
        // An unlinked run records no phase at all and claims none here.
        state: runState(run) || "not linked to a session",
        freshness: text(runSources[text(run?.source_id)]?.freshness) || "unavailable"
      }));
      const base = {
        key,
        kind,
        participants,
        signature: workSignature(record),
        repository: text(record?.scope?.repository),
        freshness,
        update,
        update_text: updateText(update, nowMs),
        session_label: record?.display?.authorized === true ? text(record?.display?.session_label) : "",
        record
      };
      if (kind === "work") {
        const work = record.work || {};
        const states = workStates(work);
        return {
          ...base,
          reference: text(work.reference) || "work",
          stage: text(work.stage),
          stage_label: lookup(STAGE_LABELS, work.stage, "not recorded"),
          states,
          headline: states[0].label,
          headline_class: states[0].class,
          pr_number: work.pull_request?.number ?? null,
          head_sha: text(work.pull_request?.head_sha),
          action_label: lookup(ACTION_LABELS, work.primary?.action, "no next action recorded"),
          actor_label: lookup(ACTOR_LABELS, work.primary?.actor, "no responsible role recorded"),
          assignments: arrayOf(work.runs).map(run => `${text(run?.provider)} ${text(run?.role)} ${runDisplay(run).label}`),
          reasons: arrayOf(work.reasons).map(text),
          groups: evidenceGroups(record),
          measurements: [
            {label: "elapsed", value: measurementText(work.measurements?.elapsed_seconds, "seconds")},
            {label: "cost", value: measurementText(work.measurements?.cost_usd, "usd")},
            {label: "quality", value: measurementText(work.measurements?.quality_score, "count")},
            {label: "productivity", value: measurementText(work.measurements?.productivity_count, "count")},
            {label: "provenance", value: measurementText(work.measurements?.provenance_count, "count")}
          ]
        };
      }
      if (kind === "no_work") {
        const covered = arrayOf(record?.sources)
          .filter(source => text(source?.freshness) === "fresh" && text(source?.coverage) === "complete")
          .map(source => text(source?.kind));
        return {
          ...base,
          reference: base.session_label || "this session",
          stage: "",
          stage_label: "no work recorded",
          states: [{label: "idle with complete coverage", class: "ok", cue: CUES.ok}],
          headline: "idle with complete coverage",
          headline_class: "ok",
          pr_number: null,
          head_sha: "",
          action_label: "nothing to do in this session",
          actor_label: "no responsible role recorded",
          assignments: [],
          reasons: [],
          groups: [{name: "coverage", label: "Coverage", items: [{
            name: "coverage",
            label: "observed complete",
            state: covered.length ? covered.join(", ") : NOT_RECORDED,
            class: covered.length ? "ok" : "muted",
            cue: cueFor(covered.length ? "ok" : "muted"),
            source: "session sources",
            head: "",
            coverage: "complete",
            note: "This session is idle because the session, work queue and run registry were all observed complete, not because nothing was looked at."
          }]}],
          measurements: []
        };
      }
      const runs = arrayOf(record?.unlinked);
      return {
        ...base,
        reference: "unlinked local runs",
        stage: "",
        stage_label: "not linked to a session",
        states: [{label: "identity unlinked", class: "warn", cue: CUES.warn}],
        headline: "identity unlinked",
        headline_class: "warn",
        pr_number: null,
        head_sha: "",
        // The contract records no route for an unlinked observation, so none
        // is derived for it here.
        action_label: "no next action recorded",
        actor_label: "no responsible role recorded",
        assignments: runs.map(run => `${text(run?.provider)} ${text(run?.role)}`),
        reasons: [],
        groups: [{name: "unlinked", label: "Unlinked runs", items: runs.map(run => ({
          name: "unlinked",
          label: `${text(run?.provider) || "provider"} ${text(run?.role) || "role"}`,
          state: "observed, not linked to a session",
          class: "warn",
          cue: CUES.warn,
          source: sourceNote(sourceIndex(record)[text(run?.source_id)]),
          head: "",
          coverage: "",
          note: "Nothing binds this run to Code Mower work, so no stage is claimed for it."
        }))}],
        measurements: []
      };
    }
    // Row order answers a different question from the headline. The headline
    // precedence in STATE_RULES says which recorded truth describes a work
    // item best, and "merged" wins that contest outright because a merged item
    // is not also usefully described as, say, "CI pending". Row order asks who
    // still has to do something, and by that question a merged item is the
    // least urgent thing on the board. Reusing headline precedence for the row
    // list therefore floats finished work to the top and -- because the first
    // row is what an operator who has made no choice is shown -- opens the
    // Board on work nobody can act on while blocked work waits below it.
    //
    // So urgency is its own explicit ranking. Blocked work comes first, then
    // work waiting on a named person, then work whose evidence cannot be
    // trusted, then work that is simply in flight, and terminal work last.
    //
    // A record routinely carries several of these at once, and the headline
    // reports only one of them. Ranking a row by its headline alone would
    // therefore lose the rest: an item recorded as both ready to merge and
    // waiting for approval reads as "ready to merge" -- correctly, that is the
    // truth that describes it best -- and would then be ordered as if the
    // approval nobody has given yet were not recorded at all. So urgency is
    // computed over every state a row records, and the ranking below is a
    // ranking of states rather than of headlines.
    const ROW_URGENCY_BANDS = [
      // Blocked: an operator has to unblock this before anything else moves.
      {name: "blocked", demanding: true, labels: [
        "source unavailable",
        "changes requested",
        // A branch that has to be updated and a session the provider paused
        // are blockers like the ones they sit beside: nothing about the work
        // moves until someone acts, so neither may sort below work that is
        // merely progressing or waiting on checks. Each is placed next to the
        // state it is closest to -- the builder's own rework, and the other
        // two provider-run outcomes -- rather than at the band's edge.
        "branch update required",
        "CI failed",
        "gate failed",
        "provider run failed",
        "provider run suspended",
        "provider run cancelled"
      ]},
      // Actionable: a named person is the only thing this is waiting on.
      {name: "actionable", demanding: true, labels: [
        "waiting for approval",
        "waiting for an answer",
        "ready to merge",
        "ready for human review",
        "review requested"
      ]},
      // Untrustworthy evidence: not known to be moving, not known to be stuck.
      // Nobody is named as owing anything here, so evidence this weak orders a
      // row but never overrides a terminal state the same row records.
      {name: "untrusted", demanding: false, labels: [
        "stale observation",
        "stale review",
        "identity unlinked",
        "state not recorded"
      ]},
      // In flight: recorded as progressing, so nothing is owed right now.
      {name: "in_flight", demanding: false, labels: [
        "review observed running",
        "CI pending",
        "gate pending",
        "provider reported progress",
        "provider run observed",
        "dispatched",
        "implementation complete",
        "review passed",
        "assigned"
      ]}
    ];
    const ROW_URGENCY_ORDER = ROW_URGENCY_BANDS.flatMap(band => band.labels);
    // The states that keep a record out of the terminal band: something is
    // blocked, or a named person still has to act. Merged work that is also
    // recorded as waiting for approval, failing or unreadable is not finished
    // work, so it is ordered by what it still owes.
    const DEMANDING_ROW_STATES = new Set(
      ROW_URGENCY_BANDS.filter(band => band.demanding).flatMap(band => band.labels));
    // Terminal work is placed last explicitly rather than by falling off the
    // end of the ranking, so nothing can be finished and urgent at once.
    const TERMINAL_ROW_HEADLINES = ["merged", "idle with complete coverage"];
    const ROW_URGENCY = new Map(ROW_URGENCY_ORDER.map((label, index) => [label, index]));
    // A state nobody ranked is neither promoted above recorded work nor
    // buried under finished work: it sorts after everything named above and
    // before the terminal band, and it counts as something owed, because an
    // unranked state is not evidence that a record is finished.
    const UNRANKED_ROW_URGENCY = ROW_URGENCY_ORDER.length;
    const TERMINAL_ROW_URGENCY = new Map(
      TERMINAL_ROW_HEADLINES.map((label, index) => [label, UNRANKED_ROW_URGENCY + 1 + index]));
    function stateUrgency(label) {
      const terminal = TERMINAL_ROW_URGENCY.get(label);
      if (terminal !== undefined) return terminal;
      return ROW_URGENCY.get(label) ?? UNRANKED_ROW_URGENCY;
    }
    function isDemandingState(label) {
      if (TERMINAL_ROW_URGENCY.has(label)) return false;
      return DEMANDING_ROW_STATES.has(label) || !ROW_URGENCY.has(label);
    }
    // Where one row sorts, decided by everything it records rather than by the
    // one state that reads best. The most urgent recorded state wins, so the
    // order never depends on which state the headline rules happened to pick,
    // nor on the order the states were recorded in. The single exception is
    // terminal work: a record that is terminal and owes nothing stays last,
    // and a record that is terminal while also carrying a blocker or an
    // outstanding action requirement is ordered by that requirement instead.
    function rowUrgency(row) {
      const labels = new Set(arrayOf(row?.states).map(state => text(state?.label)).filter(Boolean));
      let owed = null;
      let terminal = null;
      let demanding = false;
      for (const label of labels) {
        const urgency = stateUrgency(label);
        if (TERMINAL_ROW_URGENCY.has(label)) {
          terminal = terminal === null ? urgency : Math.min(terminal, urgency);
          continue;
        }
        owed = owed === null ? urgency : Math.min(owed, urgency);
        if (isDemandingState(label)) demanding = true;
      }
      if (terminal !== null && !demanding) return terminal;
      if (owed !== null) return owed;
      return terminal === null ? UNRANKED_ROW_URGENCY : terminal;
    }
    // How recent one observation of a linked identity is. `created_at` is when
    // the observation itself was recorded, so it is what orders two
    // observations of the same work item; the last meaningful update breaks a
    // tie, and the signature breaks that, so the winner never depends on the
    // order the directory happened to be listed in.
    function observationOrder(record) {
      const created = parseMs(record?.created_at);
      const update = lastMeaningfulUpdate(record);
      return [created === null ? -Infinity : created, update.at === null ? -Infinity : update.at];
    }
    function isNewerObservation(candidate, existing) {
      const [candidateCreated, candidateUpdate] = observationOrder(candidate);
      const [existingCreated, existingUpdate] = observationOrder(existing);
      if (candidateCreated !== existingCreated) return candidateCreated > existingCreated;
      if (candidateUpdate !== existingUpdate) return candidateUpdate > existingUpdate;
      return workSignature(candidate).localeCompare(workSignature(existing)) > 0;
    }
    // The one deduplicated observation set the work-first views read. The work
    // list, the participant summary and change tracking all consume this and
    // nothing else, so none of them can go on counting an observation the
    // others have already dropped.
    //
    // A linked identity is one work item however many observations of it are on
    // disk, so only the newest is retained and the superseded ones are dropped.
    // Several unlinked observations describe one condition in one repository,
    // so all of them are retained and consolidated into one row. Records within
    // a group are ordered by what they record, never by the order the directory
    // happened to list the files in.
    function observationGroups(data) {
      const groups = new Map();
      for (const record of records(data)) {
        const key = workKey(record);
        const existing = groups.get(key);
        if (existing === undefined) {
          groups.set(key, {key, kind: text(record?.kind), records: [record]});
          continue;
        }
        if (existing.kind === "unlinked") existing.records.push(record);
        else if (isNewerObservation(record, existing.records[0])) existing.records = [record];
      }
      for (const group of groups.values()) {
        group.records.sort((a, b) =>
          workSignature(a).localeCompare(workSignature(b))
          || text(a?.created_at).localeCompare(text(b?.created_at))
          || text(a?.scope?.repository).localeCompare(text(b?.scope?.repository)));
      }
      return [...groups.values()];
    }
    // One source id names one source, so a source observed in several retained
    // files is one source here too. Its consolidated reading is the
    // conservative one -- the worst freshness and the worst coverage any
    // retained file reported -- carrying the newest time each of them recorded.
    // Combining a fresh observation with an unavailable one can therefore
    // neither hide the unavailability nor lose the later update.
    function consolidatedSource(existing, source) {
      if (existing === undefined) return {...source};
      const merged = {...existing};
      if ((FRESHNESS_RANK[text(source?.freshness)] ?? 3) > (FRESHNESS_RANK[text(existing.freshness)] ?? 3)) merged.freshness = source.freshness;
      if ((SOURCE_COVERAGE_RANK[text(source?.coverage)] ?? 3) > (SOURCE_COVERAGE_RANK[text(existing.coverage)] ?? 3)) merged.coverage = source.coverage;
      merged.event_at = laterTimestamp(existing.event_at, source?.event_at);
      merged.observed_at = laterTimestamp(existing.observed_at, source?.observed_at);
      merged.heartbeat_at = laterTimestamp(existing.heartbeat_at, source?.heartbeat_at);
      merged.checked_at = laterTimestamp(existing.checked_at, source?.checked_at);
      return merged;
    }
    // The single record one row is read off. One retained observation is
    // itself; several retained observations of one unlinked condition are
    // consolidated into one, so the row's freshness, age, last meaningful
    // update and signature are recomputed from all of the evidence behind it
    // rather than inherited from whichever file happened to be read first.
    function consolidatedRecord(group) {
      const [first, ...rest] = group.records;
      if (!rest.length) return first;
      const sources = new Map();
      const unlinked = new Map();
      for (const record of group.records) {
        for (const source of arrayOf(record?.sources)) {
          const id = text(source?.id);
          sources.set(id, consolidatedSource(sources.get(id), source));
        }
        // One run observed in several files is one run, not one run per file.
        // The observation kept for it is the worst-attested one, so a run whose
        // source has since gone unavailable cannot go on reading as fresh
        // because an earlier file still had it.
        const runSources = sourceIndex(record);
        for (const run of arrayOf(record?.unlinked)) {
          const id = text(run?.id);
          const rank = FRESHNESS_RANK[text(runSources[text(run?.source_id)]?.freshness)] ?? 3;
          const existing = unlinked.get(id);
          if (existing === undefined || rank > existing.rank) unlinked.set(id, {rank, run});
        }
      }
      // The oldest retained observation sets the age, and an observation that
      // records no time at all leaves the consolidated row unable to claim one,
      // so a consolidated row never reads as more current than the oldest
      // evidence in it.
      const created = group.records.map(record => ({raw: text(record?.created_at), at: parseMs(record?.created_at)}));
      const byId = (a, b) => text(a?.id).localeCompare(text(b?.id));
      return {
        ...first,
        created_at: created.some(item => item.at === null)
          ? ""
          : created.reduce((oldest, item) => (item.at < oldest.at ? item : oldest)).raw,
        sources: [...sources.values()].sort(byId),
        unlinked: [...unlinked.values()].map(item => item.run).sort(byId)
      };
    }
    // Deterministic order: most urgent first, then a stable tiebreak on the
    // reference and the opaque identity, so an unchanged snapshot never
    // reshuffles the list.
    function workRows(data, nowMs) {
      return observationGroups(data)
        .map(group => workRow(consolidatedRecord(group), nowMs))
        .sort((a, b) =>
          rowUrgency(a) - rowUrgency(b)
          || a.reference.localeCompare(b.reference)
          || a.key.localeCompare(b.key));
    }
    // Who is recorded as taking part, and in what state. Nothing is inferred:
    // a participant is only ever reported in the states its own runs record,
    // alongside how fresh the source behind them is. The summary is built from
    // the same deduplicated rows the work list renders, so an observation a
    // newer file has replaced can neither count its runs a second time nor keep
    // reporting a state the work has already moved past. Each run is counted
    // under its lifecycle-aware state, so one suspended run and one failed run
    // are one of each and never two failures.
    function participantSummary(rows) {
      const summary = new Map();
      for (const row of rows) {
        for (const run of row.participants) {
          const key = `${run.provider}/${run.role}`;
          const entry = summary.get(key) || {provider: run.provider, role: run.role, count: 0, freshness: "fresh", states: new Map()};
          entry.states.set(run.state, (entry.states.get(run.state) || 0) + 1);
          entry.count += 1;
          if ((FRESHNESS_RANK[run.freshness] ?? 3) > (FRESHNESS_RANK[entry.freshness] ?? 3)) entry.freshness = run.freshness;
          summary.set(key, entry);
        }
      }
      return [...summary.values()]
        .map(entry => ({
          provider: entry.provider,
          role: entry.role,
          count: entry.count,
          freshness: entry.freshness,
          class: FRESHNESS_CLASSES[entry.freshness] || "muted",
          states: [...entry.states.entries()]
            .sort((a, b) => a[0].localeCompare(b[0]))
            .map(([state, count]) => ({state, count, label: lookup(RUN_STATE_LABELS, state, state)}))
        }))
        .sort((a, b) => a.provider.localeCompare(b.provider) || a.role.localeCompare(b.role));
    }
    // Every source behind every record, so a connection can be inspected on
    // its own terms in the Health view. Several records routinely observe the
    // same source, so identical readings collapse into one connection with the
    // number of records behind it: repeating one lease five times would read
    // as five connections rather than as one.
    function sourceRows(data, nowMs) {
      const grouped = new Map();
      for (const record of records(data)) {
        for (const source of arrayOf(record?.sources)) {
          const freshness = text(source?.freshness) || "unknown";
          const checked = parseMs(source?.checked_at);
          const row = {
            id: text(source?.id),
            kind: text(source?.kind) || "source",
            freshness,
            class: FRESHNESS_CLASSES[freshness] || "muted",
            cue: cueFor(FRESHNESS_CLASSES[freshness] || "muted"),
            coverage: text(source?.coverage) || "unknown",
            event_at: text(source?.event_at),
            observed_at: text(source?.observed_at),
            heartbeat_at: text(source?.heartbeat_at),
            checked_text: checked === null ? NOT_RECORDED : `${ageText(Math.max((nowMs - checked) / 1000, 0))} ago`,
            records: 1
          };
          const key = [row.id, row.kind, row.freshness, row.coverage, row.event_at, row.observed_at, row.heartbeat_at, row.checked_text].join("|");
          const existing = grouped.get(key);
          if (existing === undefined) grouped.set(key, row);
          else existing.records += 1;
        }
      }
      return [...grouped.values()].sort((a, b) => a.kind.localeCompare(b.kind) || a.id.localeCompare(b.id));
    }
    // What changed since the previous snapshot. The first snapshot of a page
    // is not a change, and a snapshot whose every signature matches produces
    // nothing at all -- that is what keeps an unchanged poll out of the
    // Timeline and out of the announcement region.
    function meaningfulChanges(previous, current) {
      if (previous === null) return [];
      const changes = [];
      for (const [key, row] of current) {
        const before = previous.get(key);
        if (before === undefined) changes.push({key, reference: row.reference, headline: row.headline, kind: "appeared"});
        else if (before.signature !== row.signature) changes.push({key, reference: row.reference, headline: row.headline, kind: "changed", from: before.headline});
      }
      for (const [key, before] of previous) {
        if (!current.has(key)) changes.push({key, reference: before.reference, headline: before.headline, kind: "gone"});
      }
      return changes.sort((a, b) => a.reference.localeCompare(b.reference) || a.key.localeCompare(b.key));
    }
    function changeSentence(change) {
      if (change.kind === "appeared") return `${change.reference} appeared as ${change.headline}`;
      if (change.kind === "gone") return `${change.reference} is no longer recorded`;
      return change.from && change.from !== change.headline
        ? `${change.reference} moved from ${change.from} to ${change.headline}`
        : `${change.reference} changed while staying ${change.headline}`;
    }
    // A bounded sentence, so one refresh that touches many items does not read
    // an unbounded list out loud.
    function changeAnnouncement(changes) {
      if (!changes.length) return "";
      const shown = changes.slice(0, 3).map(changeSentence);
      const remaining = changes.length - shown.length;
      return `${shown.join("; ")}${remaining > 0 ? `; and ${remaining} more work item${remaining === 1 ? "" : "s"} changed` : ""}.`;
    }
    // Keyboard movement, kept pure so the rules are testable without a browser.
    // Tabs wrap, as the tabs pattern expects; the vertical row list clamps at
    // its ends so Down on the last row does not jump back to the top.
    function nextTabIndex(key, index, count) {
      if (count < 1) return -1;
      if (key === "ArrowRight" || key === "ArrowDown") return (index + 1) % count;
      if (key === "ArrowLeft" || key === "ArrowUp") return (index - 1 + count) % count;
      if (key === "Home") return 0;
      if (key === "End") return count - 1;
      return -1;
    }
    function nextRowIndex(key, index, count) {
      if (count < 1) return -1;
      if (key === "ArrowDown") return Math.min(index + 1, count - 1);
      if (key === "ArrowUp") return Math.max(index - 1, 0);
      if (key === "Home") return 0;
      if (key === "End") return count - 1;
      return -1;
    }
    // A selection is kept by opaque work identity, never by position. An
    // explicit choice survives a refresh that reorders the list, and is only
    // replaced on screen -- not forgotten -- while the work it names is absent.
    function resolveSelection(rows, chosenKey) {
      if (chosenKey !== null && rows.some(row => row.key === chosenKey)) return chosenKey;
      return rows.length ? rows[0].key : null;
    }
    // --- work view model (END) ---
    const localTime = (value) => {
      const raw = text(value);
      if (!raw) return "";
      const date = new Date(raw);
      if (Number.isNaN(date.getTime())) return esc(raw);
      const local = new Intl.DateTimeFormat(undefined, {year:"numeric", month:"short", day:"numeric", hour:"numeric", minute:"2-digit", second:"2-digit", timeZoneName:"short"}).format(date);
      return `<time datetime="${esc(raw)}" title="UTC ${esc(raw)}">${esc(local)}</time>`;
    };
    function labels(groups) {
      return Object.values(groups || {}).flat().map(pill).join(" ") || '<span class="muted">none</span>';
    }
    function checks(list) {
      // The canonical publishing job is marked so a green publisher run is
      // never read as a green verdict. Other checks, including unrelated ones
      // whose name happens to contain "gate", render normally.
      return (list || []).map(c => {
        const publisher = isGatePublisher(c.name);
        const suffix = publisher ? ' <span class="muted">(publisher job, not the verdict)</span>' : "";
        return `<span class="${stateClass(c.state)}">${esc(c.name)}=${esc(display(c.state))}</span>${suffix}`;
      }).join(", ") || '<span class="muted">none</span>';
    }
    function attentionRow(item) {
      const reasons = item.reasons.map(reason => pill(`${reason.kind} -> ${reason.role}`)).join(" ");
      const evidence = item.evidence.length ? `<div class="muted">owner evidence: ${esc(item.evidence.join(", "))}</div>` : "";
      return `<div class="row">
        <div class="line"><a href="${esc(href(item.url))}">#${esc(item.pr_number)} ${esc(item.title)}</a>${statePill(item.role, item.role === "owner" ? "warn" : "muted")}${statePill(`gate ${item.gate.state}`, item.gate.class)}${item.head_sha_prefix ? pill(item.head_sha_prefix) : ""}</div>
        <div>next: <b>${esc(item.next_action)}</b></div>
        <div class="line">reasons (${item.reasons.length}): ${reasons}</div>
        ${evidence}
        <div class="muted">${esc(item.branch)} by ${esc(item.author)}${item.updated_at ? ` updated ${localTime(item.updated_at)}` : ""}</div>
      </div>`;
    }
    function renderLease(lease) {
      const messages = {absent: "No orchestrator lease in this working copy.", expired: "Lease expired.", malformed: "Local lease is malformed.", unavailable: "Local lease is unavailable."};
      const state = lease?.state || "unavailable";
      return `<div class="row"><div class="line">${pill(state)}<b>Provider: ${esc(lease?.provider || "none")}</b></div><div>Expires: ${lease?.expires_at ? localTime(lease.expires_at) : "n/a"}</div><div class="muted">${esc(messages[state] || "Local orchestration lease is active.")}</div></div>`;
    }
    // --- view chrome and work rendering (BEGIN) ---
    const VIEWS = [
      {id: "now", label: "Now", panel: "panel-now"},
      {id: "timeline", label: "Timeline", panel: "panel-timeline"},
      {id: "releases", label: "Releases", panel: "panel-releases"},
      {id: "health", label: "Health", panel: "panel-health"}
    ];
    let activeView = "now";
    // The operator's explicit choice, kept as the opaque work identity rather
    // than a row position and never cleared by a refresh.
    let selectedWorkKey = null;
    let workState = {rows: [], prs: [], message: ""};
    let previousSignatures = null;
    let changeLog = [];
    let wired = false;
    // A row's element id is derived from its opaque work identity rather than
    // its position, so `aria-labelledby` and restored keyboard focus follow the
    // work item across a refresh that reorders the list. The encoding is
    // injective for any key whatsoever: a letter, digit or hyphen stands for
    // itself, and every other code unit -- including the `_` that introduces an
    // escape, and code units outside ASCII -- becomes `_<hex>_`. A produced id
    // therefore decodes back to exactly one key, so keys that differ only in
    // punctuation (`owner/re.po` against `owner/re-po`) can never collapse onto
    // one id, one `aria-labelledby` target or one focus lookup.
    const keySlug = (key) => text(key).replace(/[^A-Za-z0-9-]/g, c => `_${c.charCodeAt(0).toString(16)}_`);
    const rowElementId = (key) => `workrow-${keySlug(key)}`;
    // The detail region of the selected row is replaced wholesale on every
    // poll, so its actions need identities of their own for the same reason
    // the rows do. The action name comes before the encoded work identity and
    // every name is a single hyphen-free token, so the first hyphen after the
    // prefix always ends the name: an action id decodes back to exactly one
    // (action, key) pair however a work identity happens to be spelled, and the
    // `workaction-` prefix keeps them clear of the row buttons.
    const actionElementId = (key, name) => `workaction-${name}-${keySlug(key)}`;
    // A background refresh replaces the tab strip and the row list. Without
    // this the focused control is destroyed mid-navigation and focus falls to
    // the document body, so the keyboard position is silently lost every poll.
    // Restoring never scrolls: an explicit key press moves the view, a refresh
    // must not.
    function withFocusPreserved(update) {
      const active = document.activeElement;
      const activeId = active && typeof active.id === "string" ? active.id : "";
      // Read off the element that is about to be destroyed, so the fallback
      // survives the update that removes it.
      const fallbackId = active && active.dataset ? text(active.dataset.focusFallback) : "";
      update();
      if (!activeId) return;
      const restored = document.getElementById(activeId);
      if (restored && typeof restored.focus === "function") {
        restored.focus({preventScroll: true});
        return;
      }
      // The control the keyboard was on no longer exists. Focus moves only to
      // the one element that control named as its owner -- the row it belonged
      // to -- and never to whatever happens to occupy its former position, so
      // an action that disappears can never hand the keyboard to an unrelated
      // control. If the owner is gone too, focus is left where the browser put
      // it rather than guessed at.
      if (!fallbackId) return;
      const owner = document.getElementById(fallbackId);
      if (owner && typeof owner.focus === "function") owner.focus({preventScroll: true});
    }
    // The other piece of ephemeral state a refresh destroys. On the desktop
    // layout the detail region scrolls on its own, and every poll replaces it
    // -- so an operator reading down the evidence of one work item was sent
    // back to the top of the panel on the next poll, including the polls that
    // observed nothing new at all.
    //
    // The offset is kept against the same opaque work identity the selection
    // is kept against, never against a row position, so it is restored only
    // while the operator is still reading the same work item. A different
    // identity starts at the top of its own evidence rather than inheriting
    // someone else's position, and a selection that stops being rendered has
    // nothing to restore onto.
    const detailKey = (detail) => (detail && detail.dataset ? text(detail.dataset.key) : "");
    function detailScrollState() {
      const detail = document.getElementById("workdetail");
      if (!detail) return null;
      const top = Number(detail.scrollTop);
      return {key: detailKey(detail), top: Number.isFinite(top) && top > 0 ? top : 0};
    }
    function withDetailScrollPreserved(update) {
      const before = detailScrollState();
      update();
      if (before === null || !before.top) return;
      const detail = document.getElementById("workdetail");
      if (detail === null || detailKey(detail) !== before.key) return;
      // Restoring is clamped to what the replacement can actually scroll, so a
      // refresh that shortens the evidence lands at the end of what is now
      // there instead of at an offset that no longer exists.
      const overflow = Number(detail.scrollHeight) - Number(detail.clientHeight);
      detail.scrollTop = Math.min(before.top, Number.isFinite(overflow) && overflow > 0 ? overflow : 0);
    }
    const cuePill = (label, cls) => `<span class="pill ${esc(cls || "muted")}"><span class="cue" aria-hidden="true">${esc(cueFor(cls))}</span> ${esc(label)}</span>`;
    const stateCue = (state) => cuePill(state.label, state.class);
    function tabsHtml() {
      return VIEWS.map(view => {
        const selected = view.id === activeView;
        return `<button type="button" role="tab" class="tab" id="tab-${esc(view.id)}" data-view="${esc(view.id)}" aria-selected="${selected}" aria-controls="${esc(view.panel)}" tabindex="${selected ? "0" : "-1"}">${esc(view.label)}</button>`;
      }).join("");
    }
    function applyView() {
      withFocusPreserved(() => put("tabs", tabsHtml()));
      for (const view of VIEWS) {
        document.getElementById(view.panel).hidden = view.id !== activeView;
      }
    }
    function selectView(id) {
      if (!VIEWS.some(view => view.id === id)) return;
      activeView = id;
      applyView();
    }
    // Only a URL the payload actually recorded, for a record that names this
    // Board's own repository, is ever offered. A PR number observed locally is
    // never turned into a remote address the Board has not been told about,
    // and an observation directory that holds a record for another repository
    // never borrows this repository's pull request just because the numbers
    // happen to match.
    function recordedPrUrl(row, prs) {
      if (row.pr_number === null || row.pr_number === undefined) return "";
      if (row.repository !== REPO) return "";
      const match = arrayOf(prs).find(pr => pr?.number === row.pr_number);
      const url = match ? text(match.url) : "";
      return href(url) === "#" ? "" : url;
    }
    function workActionsHtml(row, prs) {
      const url = recordedPrUrl(row, prs);
      const actions = [];
      // Every focusable action carries an identity derived from the work it
      // acts on, and names the row button as where the keyboard should land if
      // the action itself stops being offered.
      const owner = rowElementId(row.key);
      const identity = (name) => `id="${esc(actionElementId(row.key, name))}" data-focus-fallback="${esc(owner)}"`;
      if (url) actions.push(`<a ${identity("openpr")} href="${esc(href(url))}">Open PR #${esc(row.pr_number)}</a>`);
      // A foreign record is still shown for what it is; what it does not get
      // is a link this Board has no record of.
      else if (row.pr_number !== null && row.pr_number !== undefined && row.repository !== REPO) actions.push(`<span class="muted">PR #${esc(row.pr_number)} in ${esc(row.repository || "an unrecorded repository")}, not this repository; no local link recorded</span>`);
      else if (row.pr_number !== null && row.pr_number !== undefined) actions.push(`<span class="muted">PR #${esc(row.pr_number)}, no local link recorded</span>`);
      actions.push(`<button type="button" class="link" ${identity("inspect")} data-view="health">Inspect connection</button>`);
      actions.push(`<button type="button" class="link" ${identity("changes")} data-view="timeline">View recent changes</button>`);
      return `<div class="line">${actions.join("")}</div>
        <div class="muted">Read-only. This Board never merges, requeues, cancels, retries, restarts or takes a lease.</div>`;
    }
    function evidenceGroupHtml(group) {
      const items = group.items.map(item => `<div class="line"><span>${esc(item.label)}</span>${cuePill(item.state, item.class)}${item.head ? `<span class="pill">head ${esc(item.head)}</span>` : ""}${item.coverage ? `<span class="pill">coverage ${esc(item.coverage)}</span>` : ""}</div><div class="muted">${esc(item.source)}${item.note ? ` -- ${esc(item.note)}` : ""}</div>`).join("");
      return `<div class="evgroup"><h4>${esc(group.label)}</h4>${items}</div>`;
    }
    function workDetailHtml(row, labelId, prs) {
      const measurements = row.measurements.length
        ? `<h4>Measurements</h4><div class="line">${row.measurements.map(item => `<span class="pill">${esc(item.label)} ${esc(item.value)}</span>`).join("")}</div>`
        : "";
      const reasons = row.reasons.length
        ? `<div class="line">${row.reasons.map(reason => pill(reason.replace(/_/g, " "))).join("")}</div>`
        : "";
      return `<div class="workdetail" id="workdetail" data-key="${esc(row.key)}" role="region" aria-labelledby="${esc(labelId)}">
        <h3>${esc(row.reference)}</h3>
        <div class="line">${row.states.map(stateCue).join("")}</div>
        <div>next: <b>${esc(row.action_label)}</b> <span class="muted">responsible: ${esc(row.actor_label)}</span></div>
        <div class="muted">stage: ${esc(row.stage_label)}; last meaningful update ${esc(row.update_text)}${row.head_sha ? `; head ${esc(row.head_sha.slice(0, 7))}` : ""}</div>
        ${row.freshness.detail ? `<div class="muted">${esc(row.freshness.detail)}</div>` : ""}
        ${reasons}
        <h4>Independent evidence</h4>
        ${row.groups.map(evidenceGroupHtml).join("")}
        ${measurements}
        <h4>Actions</h4>
        ${workActionsHtml(row, prs)}
      </div>`;
    }
    function workRowHtml(row, selected, prs) {
      const id = rowElementId(row.key);
      const assignments = row.assignments.length ? `assignments: ${row.assignments.join("; ")}` : "assignments: not recorded";
      return `<li class="workrow${selected ? " selected" : ""}">
        <button type="button" class="rowbtn" id="${id}" data-key="${esc(row.key)}" aria-expanded="${selected}" aria-controls="workdetail">
          <span class="line"><span class="ref">${esc(row.reference)}</span>${stateCue(row.states[0])}<span class="pill">stage: ${esc(row.stage_label)}</span>${cuePill(row.freshness.label, row.freshness.class)}</span>
          <span class="line"><span>next: <b>${esc(row.action_label)}</b></span><span class="muted">responsible: ${esc(row.actor_label)}</span><span class="muted">last update: ${esc(row.update_text)}</span></span>
          <span class="line muted">${esc(assignments)}</span>
        </button>
        ${selected ? workDetailHtml(row, id, prs) : ""}
      </li>`;
    }
    function renderWork() {
      const rows = workState.rows;
      const activeKey = resolveSelection(rows, selectedWorkKey);
      // One refresh has to carry both pieces of ephemeral state at once, so
      // they are composed rather than alternatives. The scroll offset is
      // restored after focus is: focus restoration asks not to scroll, and
      // restoring the offset last means a browser that ignores that request
      // still cannot leave the panel somewhere the operator did not put it.
      withDetailScrollPreserved(() => withFocusPreserved(() => put("worklist", rows.length
        ? `<ul class="workrows" role="list" aria-labelledby="work-heading">${rows.map(row => workRowHtml(row, row.key === activeKey, workState.prs)).join("")}</ul>`
        : empty(workState.message))));
    }
    function selectWork(key) {
      if (!key) return;
      selectedWorkKey = key;
      renderWork();
    }
    // Handlers are attached once to the two containers that survive every
    // re-render, so replacing their contents can never leave a row or a tab
    // unresponsive, and nothing outside an interaction touches the document.
    function wire() {
      if (wired) return;
      wired = true;
      const tabs = document.getElementById("tabs");
      tabs.onclick = (event) => {
        const button = event.target.closest("[data-view]");
        if (button) selectView(button.dataset.view);
      };
      tabs.onkeydown = (event) => {
        const buttons = [...tabs.querySelectorAll("[role=tab]")];
        const current = buttons.indexOf(event.target.closest("[role=tab]"));
        const next = nextTabIndex(event.key, current < 0 ? 0 : current, buttons.length);
        if (next < 0) return;
        event.preventDefault();
        selectView(buttons[next].dataset.view);
        const moved = document.getElementById(`tab-${activeView}`);
        if (moved) moved.focus();
      };
      const list = document.getElementById("worklist");
      list.onclick = (event) => {
        const view = event.target.closest("[data-view]");
        if (view) {
          selectView(view.dataset.view);
          // The control that asked for the view sits in the panel this has
          // just hidden. Leaving the keyboard on it would park focus inside
          // hidden content, which browsers resolve by dropping focus to the
          // document body -- so activating "Inspect connection" from the
          // keyboard would silently send the operator back to the top of the
          // page. Focus moves to the tab for the view that was opened, which
          // is where the operator now is.
          const tab = document.getElementById(`tab-${view.dataset.view}`);
          if (tab && typeof tab.focus === "function") tab.focus();
          return;
        }
        const button = event.target.closest(".rowbtn");
        if (button) selectWork(button.dataset.key);
      };
      list.onkeydown = (event) => {
        const buttons = [...list.querySelectorAll(".rowbtn")];
        const current = buttons.indexOf(event.target.closest(".rowbtn"));
        if (current < 0) return;
        const next = nextRowIndex(event.key, current, buttons.length);
        if (next < 0) return;
        event.preventDefault();
        selectWork(buttons[next].dataset.key);
        const moved = list.querySelectorAll(".rowbtn")[next];
        if (moved) moved.focus();
      };
    }
    function renderChanges() {
      put("changes", changeLog.length
        ? changeLog.map(entry => `<div class="row"><div class="line"><b>${localTime(entry.at)}</b>${pill(entry.kind)}</div><div>${esc(entry.sentence)}</div></div>`).join("")
        : empty("No meaningful change has been observed since this page loaded. Polls that repeat the same observation are not listed here."));
    }
    // Announce only what actually changed. An unchanged poll writes nothing at
    // all, so the live region stays silent instead of repeating itself.
    function noteChanges(rows, nowMs) {
      const current = new Map(rows.map(row => [row.key, row]));
      const changes = meaningfulChanges(previousSignatures, current);
      previousSignatures = new Map(rows.map(row => [row.key, {signature: row.signature, reference: row.reference, headline: row.headline}]));
      if (!changes.length) return;
      const at = new Date(nowMs).toISOString();
      changeLog = [...changes.map(change => ({at, kind: change.kind, sentence: changeSentence(change)})), ...changeLog].slice(0, 20);
      document.getElementById("announce").textContent = changeAnnouncement(changes);
    }
    // --- view chrome and work rendering (END) ---
    function render(data) {
      put("lease", renderLease(data.orchestrator_lease));
      document.getElementById("repo").textContent = REPO;
      const version = data.board?.version || {};
      const servingVersion = version.serving_version || "unknown";
      const installedVersion = version.installed_version || servingVersion;
      document.getElementById("version").textContent = version.restart_recommended
        ? `serving ${servingVersion}; installed ${installedVersion} available after restart`
        : `serving ${servingVersion}`;
      document.getElementById("generated").innerHTML = data.generated_at ? `Generated ${localTime(data.generated_at)}` : "Loading...";
      const prs = data.remote?.pull_requests || [];
      const runs = data.remote?.workflow_runs || [];
      const alerts = data.remote?.gate_health?.alerts || [];
      const ownerQueue = data.owner_queue?.entries || [];
      const agentCards = data.agent_adapters?.agents || [];
      const supervised = data.supervised_pilot || {};
      const supervisedDecision = supervised.decision || {};
      const supervisedQueue = supervised.queue || {};
      const supervisedMetrics = supervisedQueue.metrics || {};
      const supervisedPRs = supervised.active_prs || [];
      const supervisedIssues = supervised.active_issues || [];
      const timelines = data.timelines || {};
      const verdicts = timelines.verdicts?.entries || [];
      const spend = timelines.spend || {};
      const spendGroups = spend.groups || [];
      const productivity = data.productivity || {};
      const productivityMetrics = productivity.metrics || {};
      const productivityCurrent = productivity.current || {};
      const productivityWindow = productivity.window?.local_history || {};
      const productivitySpend = productivity.spend || {};
      const productivityQuality = productivity.quality || {};
      const nowMs = Date.now();
      const remoteAvailable = data.remote?.available === true;
      const obs = observation(data, nowMs);
      const sources = localSources(data);
      const attention = attentionItems(ownerQueue, prs);
      const ownerItems = attention.filter(item => item.role === "owner");
      const laneItems = attention.filter(item => item.role !== "owner");
      const leadItem = attention[0];
      put("summary", [
        `<div class="metric"><span class="muted">Next action</span><b>${esc(data.next_action || "inspect")}</b></div>`,
        data.next_detail ? `<div class="metric"><span class="muted">Detail</span><b>${esc(data.next_detail)}</b></div>` : "",
        `<div class="metric"><span class="muted">Observation</span><b class="${obs.class}">${esc(obs.label)}</b></div>`,
        `<div class="metric"><span class="muted">GitHub</span><b class="${remoteAvailable ? "ok" : "warn"}">${remoteAvailable ? "available" : "unavailable"}</b></div>`,
        `<div class="metric"><span class="muted">Open PRs</span><b class="${remoteAvailable ? "" : "muted"}">${esc(countOf(remoteAvailable, prs.length))}</b></div>`,
        `<div class="metric"><span class="muted">Owner decisions</span><b class="${remoteAvailable ? (ownerItems.length ? "warn" : "ok") : "muted"}">${esc(countOf(remoteAvailable, ownerItems.length))}</b></div>`,
        `<div class="metric"><span class="muted">Lane work</span><b class="${remoteAvailable ? (laneItems.length ? "warn" : "ok") : "muted"}">${esc(countOf(remoteAvailable, laneItems.length))}</b></div>`,
        `<div class="metric"><span class="muted">Gate alerts</span><b class="${remoteAvailable ? (alerts.length ? "warn" : "ok") : "muted"}">${esc(countOf(remoteAvailable, alerts.length))}</b></div>`,
        `<div class="metric"><span class="muted">Pilot</span><b class="${stateClass(supervised.cycle_state)}">${esc(display(supervised.cycle_state))}</b></div>`,
        `<div class="metric"><span class="muted">Productivity</span><b class="${stateClass(productivity.status)}">${esc(display(productivity.status))}</b></div>`,
        `<div class="metric"><span class="muted">Agent cards</span><b class="${sources.adapters_available ? "" : "muted"}">${esc(countOf(sources.adapters_available, agentCards.length))}</b></div>`,
        `<div class="metric"><span class="muted">Campaigns</span><b class="muted">${(data.release_campaigns?.campaigns || []).length}</b></div>`
      ].join(""));
      put("worknow", [
        leadItem
          ? `<div class="row"><div class="line">Do next: <a href="${esc(href(leadItem.url))}">#${esc(leadItem.pr_number)}</a><b>${esc(leadItem.next_action)}</b>${statePill(leadItem.role, leadItem.role === "owner" ? "warn" : "muted")}${statePill(`gate ${leadItem.gate.state}`, leadItem.gate.class)}</div><div class="muted">${esc(leadItem.title)}</div></div>`
          : `<div class="row"><div class="line">Do next: <b>${esc(data.next_action || "inspect")}</b></div>${data.next_detail ? `<div class="muted">${esc(data.next_detail)}</div>` : ""}</div>`,
        `<div class="row"><div class="line">${pill(`owner decisions ${countOf(remoteAvailable, ownerItems.length)}`)}${pill(`lane work ${countOf(remoteAvailable, laneItems.length)}`)}${pill(`open PRs ${countOf(remoteAvailable, prs.length)}`)}${statePill(obs.label, obs.class)}</div>${obs.detail ? `<div class="muted">${esc(obs.detail)}</div>` : ""}</div>`,
        `<div class="row muted">${esc(sources.message)}</div>`
      ].join(""));
      const reviewerOutcomes = supervisedDecision.reviewer_outcomes || [];
      const supervisedRows = supervised.enabled ? [
        `<div class="row"><div class="line"><b class="${stateClass(supervised.cycle_state)}">${esc(supervised.cycle_state || "unknown")}</b>${pill(supervised.controller_mode || "dry_run")}${supervisedDecision.decision_state ? pill(supervisedDecision.decision_state) : ""}</div><div>next: <b>${esc(supervisedDecision.next_action || "inspect")}</b></div>${supervisedDecision.next_detail ? `<div class="muted">${esc(supervisedDecision.next_detail)}</div>` : ""}</div>`,
        `<div class="row"><div class="line">${pill(`open PRs ${display(supervisedMetrics.open_pr_count ?? supervisedPRs.length)}`)}${pill(`ready issues ${display(supervisedMetrics.ready_issue_count ?? supervisedIssues.length)}`)}${pill(`active lanes ${display(supervisedMetrics.active_lane_count)}`)}${pill(`stale ${display(supervisedMetrics.stale_evidence_count)}`)}</div></div>`,
        supervisedDecision.pr_number ? `<div class="row"><div class="line"><a href="${esc(href(supervisedDecision.pr_url))}">Selected PR #${esc(supervisedDecision.pr_number)}</a>${supervisedDecision.lane_id ? pill(supervisedDecision.lane_id) : ""}${supervisedDecision.gate_status ? pill(`gate ${supervisedDecision.gate_status}`) : ""}${supervisedDecision.author_lane_excluded ? pill("author excluded") : ""}</div><div class="muted">${esc(supervisedDecision.branch || "")}${supervisedDecision.head_sha_prefix ? ` @ ${esc(supervisedDecision.head_sha_prefix)}` : ""}</div></div>` : "",
        supervisedDecision.issue_number ? `<div class="row"><div class="line"><a href="${esc(href(supervisedDecision.issue_url))}">Selected issue #${esc(supervisedDecision.issue_number)}</a>${supervisedDecision.lane_id ? pill(supervisedDecision.lane_id) : ""}</div></div>` : "",
        reviewerOutcomes.length ? `<div class="row"><b>Reviewer Evidence</b><div class="muted">${reviewerOutcomes.map(outcome => `${esc(outcome.lane_id || outcome.config_lane_id)}=${esc(outcome.verdict)}`).join(", ")}</div></div>` : "",
        supervisedIssues.length ? `<div class="row"><b>Ready Issues</b><div class="muted">${supervisedIssues.slice(0, 5).map(issue => `${esc(issue.work_item?.identity?.issue_key || `#${issue.number}`)} ${esc(issue.builder_lane || "")}`).join(", ")}</div></div>` : "",
        supervisedPRs.length ? `<div class="row"><b>Active PRs</b><div class="muted">${supervisedPRs.slice(0, 5).map(pr => `#${esc(pr.number)} ${esc(pr.merge_state || "")}${pr.stale ? " stale" : ""}${pr.is_draft ? " draft" : ""}`).join(", ")}</div></div>` : ""
      ].filter(Boolean).join("") : empty(supervised.message || "Supervised pilot state unavailable.");
      const trackerRows = data.tracker ? `<div class="row"><b>Tracker: ${esc(data.tracker.source_kind)}</b> ${pill(data.tracker.freshness)}<div>${esc((data.tracker.errors || []).join(", "))}</div></div>` + (data.tracker.items || []).map(row => `<div class="row"><a href="${esc(href(row.work_item.url))}">${esc(row.work_item.identity.issue_key || row.work_item.identity.issue_id)}</a> ${pill(row.work_item.lifecycle_category)} ${pill(row.freshness)} ${pill(row.lane_id || "unassigned")}<div>PR ${esc(row.linked_pr_number || "-")} gate=${esc(row.gate_status)} (${esc(row.pr_freshness)})</div><div>next: ${esc(row.next_action)}</div></div>`).join("") : "";
      put("supervised", trackerRows + supervisedRows || empty(supervised.message || "No supervised pilot activity."));
      const productivityRows = [
        `<div class="row"><div>next: <b>${esc(productivity.next_action || "inspect")}</b></div></div>`,
        // Aggregates are only as current as the snapshot they were computed
        // from, so the observation comes before the numbers rather than after.
        `<div class="row"><div class="line">${pill(`source ${display(productivityCurrent.source)}`)}${statePill(obs.label, obs.class)}${obs.historical ? statePill("historical snapshot", "warn") : ""}</div><div class="muted">observed ${esc(display(productivityCurrent.observed_at))}${obs.historical ? "; these aggregates replay the last recorded snapshot and are not evidence of work running now" : ""}</div></div>`,
        `<div class="row"><b>Current</b><div class="line">${pill(`open PRs ${display(productivityCurrent.open_pr_count)}`)}${pill(`active lanes ${display(productivityCurrent.active_lane_count)}`)}${pill(`blocked ${display(productivityCurrent.blocked_pr_count)}`)}${pill(`owner actions ${display(productivityCurrent.owner_action_count)}`)}</div></div>`,
        `<div class="row"><b>Throughput</b><div class="line">${pill(`merged ${display(productivityMetrics.merged_pr_count)}`)}${pill(`cycle ${seconds(productivityMetrics.cycle_time_seconds)}`)}${pill(`active ${seconds(productivityMetrics.active_time_seconds)}`)}${pill(`wait ${seconds(productivityMetrics.wait_time_seconds)}`)}</div><div class="muted">local window ${display(productivityWindow.start)} to ${display(productivityWindow.end)} (${seconds(productivityWindow.duration_seconds)})</div></div>`,
        `<div class="row"><b>Quality</b><div class="line">${pill(`reviews ${display(productivityMetrics.reviewer_run_count)}`)}${pill(`PASS ${display(productivityQuality.audit_pass_count)}`)}${pill(`BLOCKED ${display(productivityQuality.audit_blocked_count)}`)}${pill(`catches ${display(productivityQuality.reviewer_catch_count)}`)}${pill(`fix rounds ${display(productivityQuality.fix_round_count)}`)}</div></div>`,
        `<div class="row"><b>Cost And Latency</b><div class="line">${pill(`${seconds(productivitySpend.wall_seconds)} reviewer wall`)}${pill(`${display(productivitySpend.total_tokens)} tokens`)}${pill(money(productivitySpend.cost_usd))}</div></div>`,
        (productivity.warnings || []).length ? `<div class="row muted">${esc((productivity.warnings || []).slice(0, 3).join("; "))}</div>` : ""
      ].filter(Boolean).join("");
      put("productivity", productivityRows || empty("No local productivity signals yet."));
      put("owner", ownerItems.length
        ? ownerItems.map(attentionRow).join("")
        : empty(remoteAvailable
          ? "No PR carries explicit permission, budget, policy, product-decision or owner-request evidence."
          : (data.owner_queue?.message || "GitHub unavailable; owner decisions not recorded.")));
      put("lanework", laneItems.length
        ? laneItems.map(attentionRow).join("")
        : empty(remoteAvailable ? "No builder or orchestrator work items." : "GitHub unavailable; lane work not recorded."));
      put("agents", agentCards.length ? agentCards.map(agent => `<div class="row"><div class="line"><b>${esc(agent.provider)}</b>${pill(agent.role)}${pill(agent.status)}${agent.stale ? pill("stale") : ""}${agent.lane ? pill(agent.lane) : ""}${agent.pr_number ? pill(`#${agent.pr_number}`) : ""}</div><div>${esc(agent.title || agent.next_action || "local agent")}</div><div class="muted">${esc(agent.branch || agent.repo || "")}${agent.pid ? ` pid=${esc(agent.pid)}` : ""}${agent.cwd ? ` cwd=${esc(agent.cwd)}` : ""}${agent.updated_at ? ` updated ${localTime(agent.updated_at)}` : ""}</div></div>`).join("") : empty(data.agent_adapters?.message || "No local agent adapter cards."));
      const campaignsData = data.release_campaigns || {};
      const campaigns = campaignsData.campaigns || [];
      const campaignRows = campaigns.flatMap(c => {
        const live = campaignLiveness(c, nowMs);
        const header = `<div class="row"><div class="line"><b>Release ${esc(c.release_tag)}</b>${statePill(live.label, live.class)}${c.dry_run ? pill("dry-run") : pill("applied")}${pill(c.qualification_context)}<span>recorded work ${seconds(c.elapsed_seconds)}</span></div><div>next: <b>${esc(c.next_action)}</b></div>${live.unverified ? `<div class="muted">No unexpired provider response deadline is recorded, so this campaign is shown as last reported rather than currently running.</div>` : ""}</div>`;
        const cardRows = (c.cards || []).map((card, index) => {
          const cardLive = live.cards[index] || cardLiveness(card, nowMs);
          return `<div class="row" style="margin-left:16px"><div class="line"><b>${esc(card.provider)}</b>${pill(card.posture || "required")}<span class="${cardLive.class}">${esc(cardLive.label)}</span>${pill(card.environment)}${card.transport_verified === false ? pill("transport unverified") : ""}${cardLive.overdue ? statePill(`deadline passed ${cardLive.overdue_for} ago`, "warn") : ""}<span>recorded work ${seconds(card.elapsed_seconds)}</span></div><div>next: <b>${esc(card.next_action)}</b></div>${card.next_detail ? `<div class="muted">${esc(card.next_detail)}</div>` : ""}<div class="muted">${card.response_deadline_at ? `response deadline ${localTime(card.response_deadline_at)}` : `response deadline ${esc(NOT_RECORDED)}`}</div></div>`;
        });
        return [header, ...cardRows];
      }).join("");
      put("campaigns", campaignRows || empty(campaignsData.message || "No release campaigns."));
      put("prs", prs.length ? prs.map(pr => `<div class="row">
        <div class="line"><a href="${esc(href(pr.url))}">#${esc(pr.number)} ${esc(pr.title)}</a>${pill(pr.merge_state)}${pr.is_draft ? pill("draft") : ""}${pr.stale ? pill("stale") : ""}</div>
        <div class="muted">${esc(pr.branch)} by ${esc(pr.author)}${pr.updated_at ? ` updated ${localTime(pr.updated_at)}` : ""}</div>
        <div>labels: ${labels(pr.labels)}</div>
        <div>checks: ${checks(pr.checks)}</div>
        <div>next: <b>${esc(pr.next_action)}</b></div>
        ${pr.next_detail ? `<div class="muted">${esc(pr.next_detail)}</div>` : ""}
      </div>`).join("") : empty("No open pull requests."));
      put("alerts", !remoteAvailable
        ? empty("GitHub unavailable; gate alerts not recorded.")
        : alerts.length
          ? alerts.map(a => `<div class="row"><b class="warn">${esc(a.kind)}</b> ${esc(a.message)}</div>`).join("")
          : empty("No gate alerts."));
      put("runs", runs.length ? runs.slice(0, 8).map(run => {
        // A run of the workflow that publishes `code-mower/gate` succeeds when
        // the publisher job finished, whatever verdict it published. Say so
        // here so a green row is never read as a green gate.
        // Matched on the workflow name only: the run title is the commit
        // subject, and a PR that merely mentions the gate is not a publisher.
        const publisher = isGatePublisher(run.workflow);
        const state = run.conclusion || run.status;
        return `<div class="row"><div class="line"><a href="${esc(href(run.url))}">${esc(run.workflow || "workflow")}</a>${statePill(display(state), stateClass(state))}${publisher ? pill("gate publisher") : ""}</div>${publisher ? `<div class="muted">Publisher execution only; the ${esc(GATE_CONTEXT)} verdict is the commit status listed under each PR.</div>` : ""}<div class="muted">${esc(run.branch)}${run.updated_at ? ` updated ${localTime(run.updated_at)}` : ""}</div></div>`;
      }).join("") : empty("No recent Code Mower workflow runs."));
      put("verdicts", verdicts.length ? verdicts.map(v => `<div class="row"><div class="line"><a href="${esc(href(v.url))}">#${esc(v.pr_number)} ${esc(v.lane)}</a>${pill(v.verdict)}${pill(v.head_sha_prefix)}</div><div class="muted">${localTime(v.created_at)}</div></div>`).join("") : empty(timelines.verdicts?.message || "No local reviewer verdict history yet."));
      const spendRows = [
        ...spendGroups.map(g => `<div class="row"><div class="line"><b>${esc(g.lane)}</b>${pill(display(g.verdict))}${pill(`${display(g.runs)} runs`)}</div><div class="muted">${seconds(g.wall_seconds_total)} total / ${seconds(g.wall_seconds_avg)} avg / ${money(g.cost_usd_total)} / ${esc(display(g.total_tokens))} tokens</div></div>`),
        spend.skipped_rows ? `<div class="row muted">Skipped ${esc(spend.skipped_rows)} malformed spend row(s).</div>` : "",
        spend.filtered_rows ? `<div class="row muted">Filtered ${esc(spend.filtered_rows)} spend row(s) from other repos.</div>` : ""
      ].filter(Boolean);
      put("spend", spendRows.length ? spendRows.join("") : empty(spend.message || "No reviewer spend rows for this repo yet."));
      const boards = data.local_boards?.boards || [];
      const procs = data.local_processes?.processes || [];
      put("local", [...boards.map(b => `<div class="row">board localhost:${esc(b.port)} pid=${esc(b.pid)} cwd=<code>${esc(b.cwd || "")}</code></div>`), ...procs.slice(0, 8).map(p => `<div class="row">${esc(p.provider)} pid=${esc(p.pid)} cwd=<code>${esc(p.cwd || "")}</code></div>`)].join("") || empty("No local boards or lane processes visible."));
      // Work first, from the frozen local observation contract. These views
      // read the records the Board was given; they never produce one, resolve
      // a session, or reach a provider to fill a gap in one.
      const observations = data.observations || {};
      const observationRows = workRows(data, nowMs);
      workState = {
        rows: observationRows,
        prs,
        // empty() escapes what it is given, so these stay plain text here.
        message: observations.available === false
          ? text(observations.message) || "Local Board observations could not be read."
          : observations.path_exists === true
            ? text(observations.message) || "No local Board observation passed the observation contract."
            : "No local Board observation is recorded yet, so no work row is shown. The queues below still summarize the GitHub snapshot."
      };
      renderWork();
      wire();
      applyView();
      const attentionRows = observationRows.filter(row => ["owner", "maintainer", "reviewer", "builder", "orchestrator"].includes(text(row.record?.work?.primary?.actor)));
      put("chrome", [
        `<span>Now: <b>${esc(data.next_action || "inspect")}</b></span>`,
        cuePill(obs.label, obs.class),
        `<span class="pill wide">${esc(countOf(remoteAvailable, prs.length))} open PRs</span>`,
        `<span class="pill wide">${esc(observationRows.length)} observed work item${observationRows.length === 1 ? "" : "s"}</span>`,
        attentionRows.length ? `<span class="pill warn wide"><span class="cue" aria-hidden="true">~</span> ${esc(attentionRows.length)} awaiting a named role</span>` : ""
      ].filter(Boolean).join(""));
      const participants = participantSummary(observationRows);
      put("participants", participants.length
        ? participants.map(participant => `<div class="row"><div class="line"><b>${esc(participant.provider)}</b>${pill(participant.role)}${cuePill(`worst source ${participant.freshness}`, participant.class)}</div><div class="line">${participant.states.map(state => pill(`${state.label} ${state.count}`)).join("")}</div><div class="muted">${esc(participant.count)} recorded run${participant.count === 1 ? "" : "s"}; run states are what the records state, not a claim that anything is running now.</div></div>`).join("")
        : empty("No participant run is recorded in any local observation."));
      const sourceList = sourceRows(data, nowMs);
      put("sources", sourceList.length
        ? sourceList.map(source => `<div class="row"><div class="line"><b>${esc(source.kind)}</b>${cuePill(source.freshness, source.class)}${pill(`coverage ${source.coverage}`)}${source.records > 1 ? pill(`${source.records} records`) : ""}</div><div class="muted">last event ${source.event_at ? localTime(source.event_at) : esc(NOT_RECORDED)}; last observed ${source.observed_at ? localTime(source.observed_at) : esc(NOT_RECORDED)}; heartbeat ${source.heartbeat_at ? localTime(source.heartbeat_at) : esc(NOT_RECORDED)}; checked ${esc(source.checked_text)}</div></div>`).join("")
        : empty("No observation source is recorded. Connection state below is from the GitHub snapshot only."));
      const cache = data.board?.cache || {};
      put("diagnostics", [
        `<div class="row"><div class="line"><b>Board version</b>${pill(`serving ${servingVersion}`)}${pill(`installed ${installedVersion}`)}${version.restart_recommended ? cuePill("restart recommended", "warn") : ""}</div></div>`,
        `<div class="row"><div class="line"><b>Snapshot cache</b>${cuePill(display(cache.state), stateClass(cache.state))}${pill(`generation ${display(cache.generation)}`)}${pill(`age ${ageText(cache.age_seconds)}`)}${cache.refresh_in_progress === true ? pill("refresh in progress") : ""}${measured(cache.retry_in_seconds) === null ? "" : pill(`retry in ${ageText(cache.retry_in_seconds)}`)}</div>${cache.last_error ? `<div class="muted">${esc(cache.last_error)}</div>` : ""}</div>`,
        `<div class="row"><div class="line"><b>GitHub</b>${cuePill(remoteAvailable ? "available" : "unavailable", remoteAvailable ? "ok" : "warn")}</div></div>`,
        `<div class="row"><div class="line"><b>Observations</b>${pill(`${observationRows.length} recorded`)}${measured(observations.rejected) ? cuePill(`${observations.rejected} rejected by the observation contract`, "warn") : ""}</div>${observations.message ? `<div class="muted">${esc(observations.message)}</div>` : ""}${(observations.warnings || []).length ? `<div class="muted">${esc((observations.warnings || []).slice(0, 3).map(warning => `${warning.file}: ${warning.message}`).join("; "))}</div>` : ""}</div>`,
        `<div class="row muted">${esc(sources.message)}</div>`
      ].join(""));
      noteChanges(observationRows, nowMs);
      renderChanges();
    }
    function renderEvents(history) {
      const events = history.events || [];
      put("history", events.length ? events.slice().reverse().map(event => {
        const s = event.summary || {};
        const remote = s.remote_available ? "remote available" : "remote unavailable";
        const locals = measured(s.local_boards) === null && measured(s.local_processes) === null
          ? NOT_RECORDED
          : String((measured(s.local_boards) ?? 0) + (measured(s.local_processes) ?? 0));
        return `<div class="row"><div class="line"><b>${localTime(event.created_at)}</b>${pill(remote)}</div><div>next: <b>${esc(s.next_action || "inspect")}</b></div><div class="muted">PRs ${esc(display(s.open_prs))} / alerts ${esc(display(s.gate_alerts))} / local ${esc(locals)}</div></div>`;
      }).join("") : empty(history.message || "No local board events recorded yet."));
    }
    let pollTimer = null;
    let fastPollAttempts = 0;
    // The only place a timer is ever armed, and it always clears the pending
    // one first, so exactly one load() is scheduled at a time and no fixed
    // interval can race the self-scheduled poll into stacked timers.
    function scheduleNextLoad(delayMs) {
      if (pollTimer !== null) {
        clearTimeout(pollTimer);
      }
      pollTimer = setTimeout(load, delayMs);
    }
    function nextDelayMs(cache) {
      if (awaitingRefresh(cache)) {
        // Only a response that is no longer awaiting a refresh resets the
        // budget. An exhausted counter must stay exhausted while the same
        // refresh is still pending, otherwise every normal-interval poll would
        // start a fresh 20-attempt burst and repeat that forever.
        if (fastPollAttempts >= FAST_POLL_MAX_ATTEMPTS) return REFRESH_MS;
        fastPollAttempts += 1;
        return FAST_POLL_MS;
      }
      fastPollAttempts = 0;
      return freshDelayMs(cache) ?? REFRESH_MS;
    }
    async function load() {
      // A failed fetch, a malformed payload, and cache metadata that is absent
      // or unusable all fall back to the configured interval.
      let delayMs = REFRESH_MS;
      try {
        const [statusResponse, eventsResponse] = await Promise.all([
          fetch("/api/status", {cache:"no-store"}),
          fetch("/api/events", {cache:"no-store"})
        ]);
        const statusData = await statusResponse.json();
        render(statusData);
        renderEvents(await eventsResponse.json());
        delayMs = nextDelayMs(statusData.board?.cache);
      } catch (error) {
        put("summary", `<div class="metric"><span class="muted">Next action</span><b class="warn">reload board</b></div>`);
      }
      scheduleNextLoad(delayMs);
    }
    load();
  </script>
</body>
</html>
"""


def render_board_html(config: BoardConfig) -> str:
    repo_json = json.dumps(config.repo).replace("</", "<\\/")
    refresh_json = json.dumps(config.refresh_seconds * 1000)
    # Refresh first: the repo slug is substituted afterwards so a slug that
    # happens to contain a placeholder name cannot be expanded again.
    return _BOARD_HTML.replace("__REFRESH_MS_JSON__", refresh_json).replace(
        "__REPO_JSON__", repo_json
    )


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
    status_cache: StatusCache | None = None,
) -> type[BaseHTTPRequestHandler]:
    last_recorded_at: datetime | None = None
    last_recorded_generation = 0
    recording_lock = Lock()
    if status_cache is None:
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
                if "tracker" in payload and cache_metadata["state"] == "stale":
                    payload["tracker"]["freshness"] = "historical"
                    for row in payload["tracker"]["items"]:
                        row["freshness"] = "historical"
                        row["pr_freshness"] = "historical"
                        row["eligible"] = False
                        row["next_action"] = "refresh current state"
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
    serve_parser.add_argument(
        "--observations-path",
        help="custom local Board observation directory (read-only)",
    )
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
                observations_path=args.observations_path,
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
