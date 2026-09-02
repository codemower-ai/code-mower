from __future__ import annotations

import http.client
import json
import socket
import subprocess
import threading
import urllib.request
from datetime import UTC, datetime
from unittest import TestCase

from code_mower import board, lane_status


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
        self.assertIn("Open PRs", html)
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
            with urllib.request.urlopen(f"{base_url}/healthz", timeout=5) as response:
                self.assertEqual(response.status, 200)
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
