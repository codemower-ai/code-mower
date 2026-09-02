from __future__ import annotations

from contextlib import redirect_stderr
import http.client
import json
import socket
import subprocess
import tempfile
import threading
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from io import StringIO

from code_mower import board, board_store, lane_status


NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


def _gh_json(args: list[str]) -> object:
    if args[:2] == ["pr", "list"]:
        return [
            {
                "number": 7,
                "title": "Adopt board",
                "url": "https://github.com/owner/repo/pull/7",
                "headRefName": "codex/board",
                "headRefOid": "abcdef0123456789abcdef0123456789abcdef01",
                "author": {"login": "codex-bot"},
                "isDraft": False,
                "mergeStateStatus": "CLEAN",
                "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
                "labels": [{"name": "builder:codex"}, {"name": "claude-audit-done"}],
                "statusCheckRollup": [{"context": "code-mower/gate", "state": "SUCCESS"}],
            },
        ]
    if args[:2] == ["run", "list"]:
        return [
            {
                "databaseId": 77,
                "workflowName": "Code Mower gate",
                "displayTitle": "publish gate",
                "status": "completed",
                "conclusion": "success",
                "event": "pull_request",
                "headBranch": "codex/board",
                "createdAt": NOW.isoformat().replace("+00:00", "Z"),
                "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
                "url": "https://github.com/owner/repo/actions/runs/77",
            },
        ]
    raise lane_status.LaneStatusUnavailable("unexpected gh call")


def _command_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
    if args[:4] == ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"]:
        return _completed("p123\ncnode\nn127.0.0.1:5332\n")
    if args == ["ps", "-p", "123", "-o", "command="]:
        return _completed("node /tmp/bin/agenttrail /repo --no-open\n")
    if args == ["lsof", "-a", "-p", "123", "-d", "cwd", "-Fn"]:
        return _completed("p123\nn/tmp/lane-checkout\n")
    if args == ["ps", "-axo", "pid=,command="]:
        return _completed(" 456 codex exec review\n")
    if args == ["lsof", "-a", "-p", "456", "-d", "cwd", "-Fn"]:
        return _completed("p456\nn/tmp/codex-lane\n")
    return _completed("", returncode=1)


class BoardTests(TestCase):
    def test_render_board_html_contains_local_app_shell(self) -> None:
        html = board.render_board_html(board.BoardConfig(repo="owner/repo"))

        self.assertIn("Code Mower Board", html)
        self.assertIn("/api/status", html)
        self.assertIn("/api/events", html)
        self.assertIn("Open PRs", html)
        self.assertIn("Recent Local History", html)
        self.assertIn("const href", html)
        self.assertNotIn("AgentTrail", html)

    def test_render_board_html_escapes_script_terminators(self) -> None:
        html = board.render_board_html(board.BoardConfig(repo="owner/repo</script><b>bad</b>"))

        self.assertIn("owner/repo<\\/script><b>bad<\\/b>", html)
        self.assertNotIn("owner/repo</script><b>bad</b>", html)

    def test_status_payload_redacts_local_paths_by_default(self) -> None:
        payload = board.status_payload(
            board.BoardConfig(repo="owner/repo"),
            gh_json_runner=_gh_json,
            command_runner=_command_runner,
        )

        serialized = json.dumps(payload)
        self.assertEqual(payload["schema"], lane_status.LANE_STATUS_SCHEMA)
        self.assertEqual(payload["board"]["schema"], "code_mower.board.v1")
        self.assertEqual(payload["board"]["mode"], "local_read_only")
        self.assertEqual(payload["board"]["local_paths"], "redacted")
        self.assertIn(lane_status.LOCAL_PATH_REDACTION, serialized)
        self.assertNotIn("/tmp/lane-checkout", serialized)
        self.assertNotIn("/tmp/codex-lane", serialized)

    def test_status_payload_can_show_local_paths_for_debugging(self) -> None:
        payload = board.status_payload(
            board.BoardConfig(repo="owner/repo", show_local_paths=True),
            gh_json_runner=_gh_json,
            command_runner=_command_runner,
        )

        serialized = json.dumps(payload)
        self.assertEqual(payload["board"]["local_paths"], "shown")
        self.assertIn("/tmp/lane-checkout", serialized)
        self.assertIn("/tmp/codex-lane", serialized)

    def test_record_status_appends_redacted_local_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "events.jsonl"

            result = board.record_status(
                board.BoardConfig(repo="owner/repo", store_path=str(store_path)),
                gh_json_runner=_gh_json,
                command_runner=_command_runner,
            )
            report = board_store.event_report(path=store_path, limit=5)

        serialized = json.dumps(report)
        ack = board.record_result_payload(result)
        self.assertEqual(ack["schema"], board_store.BOARD_RECORD_SCHEMA)
        self.assertEqual(result.event["schema"], board_store.BOARD_EVENT_SCHEMA)
        self.assertEqual(result.event["snapshot_schema"], lane_status.LANE_STATUS_SCHEMA)
        self.assertEqual(result.event["board_schema"], "code_mower.board.v1")
        self.assertEqual(report["schema"], board_store.BOARD_EVENT_STORE_SCHEMA)
        self.assertEqual(report["event_count"], 1)
        self.assertEqual(report["events"][0]["summary"]["open_prs"], 1)
        self.assertIn(lane_status.LOCAL_PATH_REDACTION, serialized)
        self.assertNotIn("/tmp/lane-checkout", serialized)
        self.assertNotIn("/tmp/codex-lane", serialized)

    def test_store_retention_prunes_old_events_and_skips_malformed_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "events.jsonl"
            store_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "schema": board_store.BOARD_EVENT_SCHEMA,
                                "created_at": "2026-08-01T12:00:00Z",
                                "summary": {"next_action": "old"},
                            }
                        ),
                        "not json",
                        json.dumps(
                            {
                                "schema": board_store.BOARD_EVENT_SCHEMA,
                                "created_at": "not-a-time",
                                "summary": {"next_action": "bad time"},
                            }
                        ),
                        json.dumps(
                            {
                                "schema": board_store.BOARD_EVENT_SCHEMA,
                                "created_at": "2026-09-01T11:59:00Z",
                                "summary": {"next_action": "recent"},
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            result = board_store.append_snapshot(
                {"schema": lane_status.LANE_STATUS_SCHEMA, "repo": "owner/repo"},
                path=store_path,
                now=NOW,
                retention_days=1,
                max_events=2,
            )
            report = board_store.event_report(path=store_path, limit=10)
            empty_report = board_store.event_report(path=store_path, limit=0)

        self.assertEqual(result.malformed, 1)
        self.assertEqual(result.pruned, 2)
        self.assertEqual(result.kept, 2)
        self.assertEqual(report["malformed"], 0)
        self.assertEqual([event["created_at"] for event in report["events"]], ["2026-09-01T11:59:00Z", "2026-09-01T12:00:00Z"])
        self.assertEqual(empty_report["events"], [])

    def test_append_snapshot_preserves_store_when_existing_read_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "events.jsonl"
            original = json.dumps(
                {
                    "schema": board_store.BOARD_EVENT_SCHEMA,
                    "created_at": "2026-09-01T11:59:00Z",
                }
            ) + "\n"
            store_path.write_text(original, encoding="utf-8")

            with patch("code_mower.board_store._read_valid_events", side_effect=OSError("boom")):
                with self.assertRaises(board_store.BoardStoreError):
                    board_store.append_snapshot(
                        {"schema": lane_status.LANE_STATUS_SCHEMA, "repo": "owner/repo"},
                        path=store_path,
                        now=NOW,
                    )

            self.assertEqual(store_path.read_text(encoding="utf-8"), original)

    def test_event_report_degrades_when_existing_read_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "events.jsonl"
            store_path.write_text("", encoding="utf-8")

            with patch("code_mower.board_store._read_valid_events", side_effect=OSError("boom")):
                report = board_store.event_report(path=store_path, limit=10)

        self.assertFalse(report["available"])
        self.assertEqual(report["events"], [])
        self.assertIn("could not read local board event store", report["message"])

    def test_record_command_rejects_invalid_retention_before_collecting_status(self) -> None:
        err = StringIO()

        with redirect_stderr(err):
            code = board.main(["record", "--repo", "owner/repo", "--retention-days", "-1"])

        self.assertEqual(code, 2)
        self.assertIn("--retention-days", err.getvalue())

    def test_http_handler_serves_page_status_and_health(self) -> None:
        handler = board.make_handler(
            board.BoardConfig(repo="owner/repo"),
            gh_json_runner=_gh_json,
            command_runner=_command_runner,
        )
        server = board.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            with urllib.request.urlopen(f"{base_url}/", timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertIn("text/html", response.headers["Content-Type"])
            with urllib.request.urlopen(f"{base_url}/api/status", timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(payload["repo"], "owner/repo")
                self.assertEqual(payload["remote"]["pull_requests"][0]["number"], 7)
                self.assertNotIn("/tmp/lane-checkout", json.dumps(payload))
            with urllib.request.urlopen(f"{base_url}/api/events", timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(payload["schema"], board_store.BOARD_EVENT_STORE_SCHEMA)
            with urllib.request.urlopen(f"{base_url}/healthz", timeout=5) as response:
                self.assertEqual(response.status, 200)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_http_handler_serves_events_without_github(self) -> None:
        def unavailable_gh(_args: list[str]) -> object:
            raise AssertionError("events endpoint should not call GitHub")

        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "events.jsonl"
            board_store.append_snapshot(
                {"schema": lane_status.LANE_STATUS_SCHEMA, "repo": "owner/repo"},
                path=store_path,
                now=NOW,
            )
            handler = board.make_handler(
                board.BoardConfig(repo="owner/repo", store_path=str(store_path)),
                gh_json_runner=unavailable_gh,
                command_runner=_command_runner,
            )
            server = board.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_address[1]}"
                with urllib.request.urlopen(f"{base_url}/api/events", timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    self.assertEqual(response.status, 200)
                    self.assertEqual(payload["event_count"], 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_http_handler_rejects_non_loopback_host_and_origin(self) -> None:
        handler = board.make_handler(
            board.BoardConfig(repo="owner/repo"),
            gh_json_runner=_gh_json,
            command_runner=_command_runner,
        )
        server = board.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = int(server.server_address[1])
            self.assertEqual(
                self._http_status(port, "/api/status", {"Host": f"127.0.0.1:{port}"}),
                200,
            )
            self.assertEqual(
                self._http_status(port, "/api/status", {"Host": "evil.example"}),
                403,
            )
            self.assertEqual(
                self._http_status(
                    port,
                    "/api/status",
                    {"Host": f"127.0.0.1:{port}", "Origin": "https://evil.example"},
                ),
                403,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_non_loopback_hosts_are_rejected(self) -> None:
        self.assertTrue(board._is_loopback("127.0.0.1"))
        self.assertTrue(board._is_loopback("localhost"))
        self.assertTrue(board._is_loopback("::1"))
        self.assertFalse(board._is_loopback("0.0.0.0"))

    def test_ipv6_loopback_uses_ipv6_server_and_url(self) -> None:
        self.assertEqual(board._server_class("::1").address_family, socket.AF_INET6)
        self.assertEqual(board._server_url("::1", 5332), "http://[::1]:5332/")

    def _http_status(self, port: int, path: str, headers: dict[str, str]) -> int:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            response.read()
            return int(response.status)
        finally:
            connection.close()
