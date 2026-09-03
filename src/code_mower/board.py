#!/usr/bin/env python3
"""Local read-only Code Mower Board."""

from __future__ import annotations

import argparse
import errno
import json
import re
import socket
import sys
import webbrowser
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import urlparse

from . import board_store
from . import lane_status
from . import reviewer_spend


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5332
BOARD_TIMELINES_SCHEMA = "code_mower.boardTimelines.v1"
BOARD_OWNER_QUEUE_SCHEMA = "code_mower.boardOwnerQueue.v1"
BOARD_AGENT_ADAPTERS_SCHEMA = "code_mower.boardAgentAdapters.v1"
BOARD_DOCTOR_SCHEMA = "code_mower.boardDoctor.v1"
DEFAULT_AGENT_ADAPTERS_RELATIVE_PATH = Path(".code-mower") / "board" / "agents"
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


def _explicit_port_conflict_message(host: str, port: int) -> str:
    suggestions = list(range(port + 1, min(65535, port + 3) + 1))
    suggestion_text = f" such as {', '.join(str(candidate) for candidate in suggestions)}" if suggestions else ""
    return (
        f"error: Code Mower Board port {port} is already in use on {host}. "
        f"Pass --port with a free loopback port{suggestion_text}."
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
        "refresh_seconds": config.refresh_seconds,
        "local_paths": "shown" if config.show_local_paths else "redacted",
        "recording": {
            "enabled": config.record_events,
            "interval_seconds": config.record_interval_seconds,
        },
    }
    payload["agent_adapters"] = agent_adapters_payload(config)
    payload["owner_queue"] = owner_queue_payload(payload)
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


def _record_live_snapshot(
    payload: dict[str, Any],
    config: BoardConfig,
    *,
    now: datetime,
) -> board_store.StoreWriteResult:
    return board_store.append_snapshot(
        payload,
        path=_store_path(config),
        now=now,
        retention_days=config.retention_days,
        max_events=config.max_events,
    )


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
    return int(number) if number is not None else None


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


def agent_adapters_payload(config: BoardConfig) -> dict[str, Any]:
    path = _agent_adapters_path(config)
    payload: dict[str, Any] = {
        "schema": BOARD_AGENT_ADAPTERS_SCHEMA,
        "available": True,
        "path": lane_status.LOCAL_PATH_REDACTION,
        "path_redacted": True,
        "path_exists": path.exists(),
        "agents": [],
        "warnings": [],
        "message": "no local agent adapter files found",
    }
    if not path.exists():
        return payload
    if not path.is_dir():
        payload["warnings"].append({"file": "", "message": "agent adapter path is not a directory"})
        payload["message"] = "could not read local agent adapter files"
        return payload
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
        payload["agents"].extend(cards)
    payload["message"] = "" if payload["agents"] else "no local agent adapter cards found"
    return payload


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
    <div><h1>Code Mower Board</h1><div class="muted" id="repo"></div></div>
    <div class="muted" id="generated">Loading...</div>
  </header>
  <main>
    <div class="summary" id="summary"></div>
    <section><h2>Owner Queue</h2><div class="rows" id="owner"></div></section>
    <section><h2>Agent Cards</h2><div class="rows" id="agents"></div></section>
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
    const text = (value) => String(value ?? "");
    const esc = (value) => text(value).replace(/[&<>"']/g, c => ({{"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#39;"}}[c]));
    const put = (id, html) => document.getElementById(id).innerHTML = html;
    const pill = (value) => `<span class="pill">${{esc(value)}}</span>`;
    const empty = (message) => `<div class="muted">${{esc(message)}}</div>`;
    const href = (value) => /^https?:\\/\\//i.test(text(value)) ? text(value) : "#";
    const stateClass = (value) => /fail|error|blocked/i.test(text(value)) ? "bad" : /warn|pending|waiting|queued|progress/i.test(text(value)) ? "warn" : "ok";
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
      document.getElementById("generated").innerHTML = data.generated_at ? `Generated ${{localTime(data.generated_at)}}` : "Loading...";
      const prs = data.remote?.pull_requests || [];
      const runs = data.remote?.workflow_runs || [];
      const alerts = data.remote?.gate_health?.alerts || [];
      const ownerQueue = data.owner_queue?.entries || [];
      const agentCards = data.agent_adapters?.agents || [];
      const timelines = data.timelines || {{}};
      const verdicts = timelines.verdicts?.entries || [];
      const spend = timelines.spend || {{}};
      const spendGroups = spend.groups || [];
      put("summary", [
        `<div class="metric"><span class="muted">Next action</span><b>${{esc(data.next_action || "inspect")}}</b></div>`,
        `<div class="metric"><span class="muted">GitHub</span><b class="${{data.remote?.available ? "ok" : "warn"}}">${{data.remote?.available ? "available" : "unavailable"}}</b></div>`,
        `<div class="metric"><span class="muted">Open PRs</span><b>${{prs.length}}</b></div>`,
        `<div class="metric"><span class="muted">Owner queue</span><b class="${{ownerQueue.length ? "warn" : "ok"}}">${{ownerQueue.length}}</b></div>`,
        `<div class="metric"><span class="muted">Agent cards</span><b>${{agentCards.length}}</b></div>`,
        `<div class="metric"><span class="muted">Gate alerts</span><b class="${{alerts.length ? "warn" : "ok"}}">${{alerts.length}}</b></div>`
      ].join(""));
      put("owner", ownerQueue.length ? ownerQueue.map(item => `<div class="row"><div class="line"><a href="${{esc(href(item.url))}}">#${{esc(item.pr_number)}} ${{esc(item.kind)}}</a>${{pill(item.next_action)}}${{pill(item.head_sha_prefix)}}</div><div class="muted">${{esc(item.branch)}} by ${{esc(item.author)}}${{item.updated_at ? ` updated ${{localTime(item.updated_at)}}` : ""}}</div></div>`).join("") : empty(data.owner_queue?.message || "No owner queue items."));
      put("agents", agentCards.length ? agentCards.map(agent => `<div class="row"><div class="line"><b>${{esc(agent.provider)}}</b>${{pill(agent.role)}}${{pill(agent.status)}}${{agent.lane ? pill(agent.lane) : ""}}${{agent.pr_number ? pill(`#${{agent.pr_number}}`) : ""}}</div><div>${{esc(agent.title || agent.next_action || "local agent")}}</div><div class="muted">${{esc(agent.branch || agent.repo || "")}}${{agent.pid ? ` pid=${{esc(agent.pid)}}` : ""}}${{agent.cwd ? ` cwd=${{esc(agent.cwd)}}` : ""}}${{agent.updated_at ? ` updated ${{localTime(agent.updated_at)}}` : ""}}</div></div>`).join("") : empty(data.agent_adapters?.message || "No local agent adapter cards."));
      put("prs", prs.length ? prs.map(pr => `<div class="row">
        <div class="line"><a href="${{esc(href(pr.url))}}">#${{esc(pr.number)}} ${{esc(pr.title)}}</a>${{pill(pr.merge_state)}}${{pr.is_draft ? pill("draft") : ""}}${{pr.stale ? pill("stale") : ""}}</div>
        <div class="muted">${{esc(pr.branch)}} by ${{esc(pr.author)}}${{pr.updated_at ? ` updated ${{localTime(pr.updated_at)}}` : ""}}</div>
        <div>labels: ${{labels(pr.labels)}}</div>
        <div>checks: ${{checks(pr.checks)}}</div>
        <div>next: <b>${{esc(pr.next_action)}}</b></div>
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
    async function load() {{
      try {{
        const [statusResponse, eventsResponse] = await Promise.all([
          fetch("/api/status", {{cache:"no-store"}}),
          fetch("/api/events", {{cache:"no-store"}})
        ]);
        render(await statusResponse.json());
        renderEvents(await eventsResponse.json());
      }} catch (error) {{
        put("summary", `<div class="metric"><span class="muted">Next action</span><b class="warn">reload board</b></div>`);
      }}
    }}
    load();
    setInterval(load, REFRESH_MS);
  </script>
</body>
</html>
"""


def make_handler(
    config: BoardConfig,
    *,
    gh_json_runner: lane_status.GitHubJsonRunner = lane_status.run_gh_json,
    command_runner: lane_status.CommandRunner = lane_status.run_command,
) -> type[BaseHTTPRequestHandler]:
    last_recorded_at: datetime | None = None
    recording_lock = Lock()

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
            nonlocal last_recorded_at

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
            if path == "/api/status":
                payload = status_payload(
                    config,
                    gh_json_runner=gh_json_runner,
                    command_runner=command_runner,
                )
                if config.record_events:
                    with recording_lock:
                        now = datetime.now(UTC).replace(microsecond=0)
                        if _recording_due(last_recorded_at, now, config.record_interval_seconds):
                            payload["board"]["recording"] = _recording_metadata(config, "recording")
                            try:
                                result = _record_live_snapshot(payload, config, now=now)
                            except (ValueError, board_store.BoardStoreError):
                                last_recorded_at = now
                                payload["board"]["recording"] = _recording_metadata(
                                    config,
                                    "error",
                                    message="could not update local board event store",
                                )
                            else:
                                last_recorded_at = now
                                payload["board"]["recording"] = _recording_metadata(
                                    config,
                                    "recorded",
                                    kept=result.kept,
                                    pruned=result.pruned,
                                    malformed=result.malformed,
                                )
                        else:
                            payload["board"]["recording"] = _recording_metadata(
                                config,
                                "skipped",
                                message="record interval not reached",
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
        snapshot,
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="code-mower board")
    subparsers = parser.add_subparsers(dest="command", required=True)
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
    serve_parser.add_argument("--refresh-seconds", type=int, default=15, help="browser refresh interval")
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
