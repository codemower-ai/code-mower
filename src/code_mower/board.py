#!/usr/bin/env python3
"""Local read-only Code Mower Board."""

from __future__ import annotations

import argparse
import json
import socket
import sys
import webbrowser
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


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5332


@dataclass(frozen=True)
class BoardConfig:
    repo: str
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    pr_limit: int = 50
    workflow_limit: int = 20
    stale_minutes: int = 30
    refresh_seconds: int = 15
    show_local_paths: bool = False
    repo_path: str = "."
    store_path: str | None = None
    event_limit: int = 20
    record_events: bool = False
    record_interval_seconds: int = 60
    retention_days: int = board_store.DEFAULT_RETENTION_DAYS
    max_events: int = board_store.DEFAULT_MAX_EVENTS


def _store_path(config: BoardConfig) -> Path:
    if config.store_path:
        return Path(config.store_path)
    return board_store.default_store_path(config.repo_path)


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
    <section><h2>Open PRs</h2><div class="rows" id="prs"></div></section>
    <section><h2>Gate Alerts</h2><div class="rows" id="alerts"></div></section>
    <section><h2>Recent Code Mower Workflows</h2><div class="rows" id="runs"></div></section>
    <section><h2>Recent Local History</h2><div class="rows" id="history"></div></section>
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
    function labels(groups) {{
      return Object.values(groups || {{}}).flat().map(pill).join(" ") || '<span class="muted">none</span>';
    }}
    function checks(list) {{
      return (list || []).map(c => `<span class="${{stateClass(c.state)}}">${{esc(c.name)}}=${{esc(c.state)}}</span>`).join(", ") || '<span class="muted">none</span>';
    }}
    function render(data) {{
      document.getElementById("repo").textContent = REPO;
      document.getElementById("generated").textContent = `Generated ${{data.generated_at || ""}}`;
      const prs = data.remote?.pull_requests || [];
      const runs = data.remote?.workflow_runs || [];
      const alerts = data.remote?.gate_health?.alerts || [];
      put("summary", [
        `<div class="metric"><span class="muted">Next action</span><b>${{esc(data.next_action || "inspect")}}</b></div>`,
        `<div class="metric"><span class="muted">GitHub</span><b class="${{data.remote?.available ? "ok" : "warn"}}">${{data.remote?.available ? "available" : "unavailable"}}</b></div>`,
        `<div class="metric"><span class="muted">Open PRs</span><b>${{prs.length}}</b></div>`,
        `<div class="metric"><span class="muted">Gate alerts</span><b class="${{alerts.length ? "warn" : "ok"}}">${{alerts.length}}</b></div>`
      ].join(""));
      put("prs", prs.length ? prs.map(pr => `<div class="row">
        <div class="line"><a href="${{esc(href(pr.url))}}">#${{esc(pr.number)}} ${{esc(pr.title)}}</a>${{pill(pr.merge_state)}}${{pr.is_draft ? pill("draft") : ""}}${{pr.stale ? pill("stale") : ""}}</div>
        <div class="muted">${{esc(pr.branch)}} by ${{esc(pr.author)}} updated ${{esc(pr.updated_at)}}</div>
        <div>labels: ${{labels(pr.labels)}}</div>
        <div>checks: ${{checks(pr.checks)}}</div>
        <div>next: <b>${{esc(pr.next_action)}}</b></div>
      </div>`).join("") : empty("No open pull requests."));
      put("alerts", alerts.length ? alerts.map(a => `<div class="row"><b class="warn">${{esc(a.kind)}}</b> ${{esc(a.message)}}</div>`).join("") : empty("No gate alerts."));
      put("runs", runs.length ? runs.slice(0, 8).map(run => `<div class="row"><div class="line"><a href="${{esc(href(run.url))}}">${{esc(run.workflow || "workflow")}}</a>${{pill(run.conclusion || run.status || "unknown")}}</div><div class="muted">${{esc(run.branch)}} updated ${{esc(run.updated_at)}}</div></div>`).join("") : empty("No recent Code Mower workflow runs."));
      const boards = data.agenttrail?.boards || [];
      const procs = data.local_processes?.processes || [];
      put("local", [...boards.map(b => `<div class="row">board localhost:${{esc(b.port)}} pid=${{esc(b.pid)}} cwd=<code>${{esc(b.cwd || "")}}</code></div>`), ...procs.slice(0, 8).map(p => `<div class="row">${{esc(p.provider)}} pid=${{esc(p.pid)}} cwd=<code>${{esc(p.cwd || "")}}</code></div>`)].join("") || empty("No local boards or lane processes visible."));
    }}
    function renderEvents(history) {{
      const events = history.events || [];
      put("history", events.length ? events.slice().reverse().map(event => {{
        const s = event.summary || {{}};
        const remote = s.remote_available ? "remote available" : "remote unavailable";
        return `<div class="row"><div class="line"><b>${{esc(event.created_at)}}</b>${{pill(remote)}}</div><div>next: <b>${{esc(s.next_action || "inspect")}}</b></div><div class="muted">PRs ${{esc(s.open_prs ?? 0)}} / alerts ${{esc(s.gate_alerts ?? 0)}} / local ${{esc((s.local_boards ?? 0) + (s.local_processes ?? 0))}}</div></div>`;
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
    server_type = _server_class(config.host)
    with server_type((config.host, config.port), handler) as server:
        port = int(server.server_address[1])
        url = _server_url(config.host, port)
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


def _record_store_display(args: argparse.Namespace) -> str:
    if args.store_path:
        return "custom store path"
    return board_store.DEFAULT_STORE_RELATIVE_PATH.as_posix()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="code-mower board")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--repo", required=True)
    serve_parser.add_argument("--host", default=DEFAULT_HOST)
    serve_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve_parser.add_argument("--pr-limit", type=int, default=50)
    serve_parser.add_argument("--workflow-limit", type=int, default=20)
    serve_parser.add_argument("--stale-minutes", type=int, default=30)
    serve_parser.add_argument("--refresh-seconds", type=int, default=15)
    serve_parser.add_argument("--show-local-paths", action="store_true")
    serve_parser.add_argument("--repo-path", default=".")
    serve_parser.add_argument("--store-path")
    serve_parser.add_argument("--event-limit", type=int, default=20)
    serve_parser.add_argument("--record-events", action="store_true")
    serve_parser.add_argument("--record-interval-seconds", type=int, default=60)
    serve_parser.add_argument("--retention-days", type=int, default=board_store.DEFAULT_RETENTION_DAYS)
    serve_parser.add_argument("--max-events", type=int, default=board_store.DEFAULT_MAX_EVENTS)
    serve_parser.add_argument("--open", action="store_true", help="open the local board in a browser")
    record_parser = subparsers.add_parser("record")
    record_parser.add_argument("--repo", required=True)
    record_parser.add_argument("--repo-path", default=".")
    record_parser.add_argument("--store-path")
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
    args = parser.parse_args(list(argv or ()))
    if args.command == "serve":
        return serve(
            BoardConfig(
                repo=args.repo,
                host=args.host,
                port=args.port,
                pr_limit=args.pr_limit,
                workflow_limit=args.workflow_limit,
                stale_minutes=args.stale_minutes,
                refresh_seconds=args.refresh_seconds,
                show_local_paths=args.show_local_paths,
                repo_path=args.repo_path,
                store_path=args.store_path,
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
    raise AssertionError(f"unhandled board command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
