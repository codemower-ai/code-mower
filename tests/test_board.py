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

from code_mower import board, board_store, lane_status, reviewer_spend


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
        self.assertIn("Owner Queue", html)
        self.assertIn("Agent Cards", html)
        self.assertIn("Open PRs", html)
        self.assertIn("Recent Local History", html)
        self.assertIn("Reviewer Verdict Timeline", html)
        self.assertIn("Spend And Latency", html)
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

    def test_status_payload_includes_empty_agent_adapters_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = board.status_payload(
                board.BoardConfig(repo="owner/repo", repo_path=tmp),
                gh_json_runner=_gh_json,
                command_runner=_command_runner,
            )

        self.assertEqual(payload["agent_adapters"]["schema"], board.BOARD_AGENT_ADAPTERS_SCHEMA)
        self.assertTrue(payload["agent_adapters"]["available"])
        self.assertFalse(payload["agent_adapters"]["path_exists"])
        self.assertEqual(payload["agent_adapters"]["agents"], [])
        self.assertEqual(payload["agent_adapters"]["message"], "no local agent adapter files found")

    def test_agent_adapters_payload_loads_cards_and_redacts_local_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter_dir = Path(tmp) / ".code-mower" / "board" / "agents"
            adapter_dir.mkdir(parents=True)
            (adapter_dir / "codex.json").write_text(
                json.dumps(
                    {
                        "provider": "codex",
                        "role": "builder",
                        "status": "running",
                        "lane": "builder:codex",
                        "repo": "owner/repo",
                        "pr_number": 7,
                        "issue_number": 521,
                        "pid": 123,
                        "cwd": "/tmp/private/checkout",
                        "head_sha": "abcdef0123456789",
                        "url": "https://github.com/owner/repo/pull/7",
                        "title": "Implement Board cards",
                        "next_action": "waiting for peer audit",
                    }
                ),
                encoding="utf-8",
            )

            payload = board.agent_adapters_payload(board.BoardConfig(repo="owner/repo", repo_path=tmp))

        serialized = json.dumps(payload)
        self.assertEqual(payload["schema"], board.BOARD_AGENT_ADAPTERS_SCHEMA)
        self.assertTrue(payload["path_exists"])
        self.assertEqual(payload["warnings"], [])
        self.assertEqual(payload["agents"][0]["source_file"], "codex.json")
        self.assertEqual(payload["agents"][0]["provider"], "codex")
        self.assertEqual(payload["agents"][0]["role"], "builder")
        self.assertEqual(payload["agents"][0]["status"], "running")
        self.assertEqual(payload["agents"][0]["pr_number"], 7)
        self.assertEqual(payload["agents"][0]["head_sha_prefix"], "abcdef012345")
        self.assertEqual(payload["agents"][0]["cwd"], lane_status.LOCAL_PATH_REDACTION)
        self.assertNotIn("/tmp/private/checkout", serialized)

    def test_agent_adapters_payload_handles_malformed_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter_dir = Path(tmp) / ".code-mower" / "board" / "agents"
            adapter_dir.mkdir(parents=True)
            (adapter_dir / "bad.json").write_text("{not json", encoding="utf-8")
            (adapter_dir / "binary.json").write_bytes(b'{"provider":"codex", "title":"bad \\xff"}')
            (adapter_dir / "empty.json").write_text("[]", encoding="utf-8")

            payload = board.agent_adapters_payload(board.BoardConfig(repo="owner/repo", repo_path=tmp))

        serialized = json.dumps(payload)
        self.assertEqual(payload["agents"], [])
        self.assertEqual(
            payload["warnings"],
            [
                {"file": "bad.json", "message": "could not parse agent adapter file"},
                {"file": "binary.json", "message": "could not parse agent adapter file"},
                {"file": "empty.json", "message": "agent adapter file had no cards"},
            ],
        )
        self.assertNotIn(str(Path(tmp)), serialized)

    def test_agent_adapters_payload_omits_secret_like_values(self) -> None:
        secret = "github_pat_abcdefghijklmnopqrstuvwxyz123456"
        with tempfile.TemporaryDirectory() as tmp:
            adapter_dir = Path(tmp) / ".code-mower" / "board" / "agents"
            adapter_dir.mkdir(parents=True)
            (adapter_dir / "claude.json").write_text(
                json.dumps(
                    {
                        "provider": "claude",
                        "role": "reviewer",
                        "status": "blocked",
                        "title": f"token {secret}",
                        "next_action": "fix audit finding",
                        "url": f"https://example.test/run?token={secret}",
                        "head_sha": secret,
                        "stdout": "raw output must not appear",
                        "token": secret,
                    }
                ),
                encoding="utf-8",
            )

            payload = board.agent_adapters_payload(board.BoardConfig(repo="owner/repo", repo_path=tmp))

        serialized = json.dumps(payload)
        self.assertEqual(payload["agents"][0]["title"], "[redacted]")
        self.assertNotIn("url", payload["agents"][0])
        self.assertNotIn("head_sha_prefix", payload["agents"][0])
        self.assertNotIn(secret, serialized)
        self.assertNotIn("raw output must not appear", serialized)

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

    def test_status_payload_marks_live_recording_disabled_by_default(self) -> None:
        payload = board.status_payload(
            board.BoardConfig(repo="owner/repo"),
            gh_json_runner=_gh_json,
            command_runner=_command_runner,
        )

        self.assertEqual(
            payload["board"]["recording"],
            {"enabled": False, "interval_seconds": 60},
        )

    def test_status_payload_includes_empty_owner_queue_for_clean_pr(self) -> None:
        payload = board.status_payload(
            board.BoardConfig(repo="owner/repo"),
            gh_json_runner=_gh_json,
            command_runner=_command_runner,
        )

        self.assertEqual(payload["owner_queue"]["schema"], board.BOARD_OWNER_QUEUE_SCHEMA)
        self.assertTrue(payload["owner_queue"]["available"])
        self.assertEqual(payload["owner_queue"]["entries"], [])
        self.assertEqual(payload["owner_queue"]["message"], "no owner queue items")

    def test_owner_queue_payload_detects_attention_states(self) -> None:
        payload = board.owner_queue_payload(
            {
                "remote": {
                    "available": True,
                    "pull_requests": [
                        {
                            "number": 1,
                            "title": "Owner decision",
                            "url": "https://github.com/owner/repo/pull/1",
                            "branch": "codex/one",
                            "author": "codex",
                            "updated_at": "2026-09-01T12:00:00Z",
                            "head_sha": "1111111111111111",
                            "labels": {"needs": ["needs-owner"], "blocked": []},
                            "checks": [],
                            "next_action": "waiting for audits or owner input",
                        },
                        {
                            "number": 2,
                            "title": "Blocked",
                            "url": "https://github.com/owner/repo/pull/2",
                            "branch": "codex/two",
                            "author": "codex",
                            "updated_at": "2026-09-01T12:00:00Z",
                            "head_sha": "2222222222222222",
                            "labels": {"needs": [], "blocked": ["claude-audit-blocked"]},
                            "checks": [],
                            "next_action": "fix BLOCKED audit",
                        },
                        {
                            "number": 3,
                            "title": "Stale",
                            "url": "https://github.com/owner/repo/pull/3",
                            "branch": "codex/three",
                            "author": "codex",
                            "updated_at": "2026-09-01T12:00:00Z",
                            "head_sha": "3333333333333333",
                            "labels": {"needs": [], "blocked": []},
                            "checks": [{"name": "code-mower/gate", "state": "success"}],
                            "stale": True,
                            "next_action": "waiting for checks",
                        },
                        {
                            "number": 4,
                            "title": "Failing",
                            "url": "https://github.com/owner/repo/pull/4",
                            "branch": "codex/four",
                            "author": "codex",
                            "updated_at": "2026-09-01T12:00:00Z",
                            "head_sha": "4444444444444444",
                            "labels": {"needs": [], "blocked": []},
                            "checks": [{"name": "package", "state": "failure"}],
                            "next_action": "fix failing check",
                        },
                        {
                            "number": 5,
                            "title": "Behind",
                            "url": "https://github.com/owner/repo/pull/5",
                            "branch": "codex/five",
                            "author": "codex",
                            "updated_at": "2026-09-01T12:00:00Z",
                            "head_sha": "5555555555555555",
                            "labels": {"needs": [], "blocked": []},
                            "checks": [],
                            "merge_state": "BEHIND",
                            "next_action": "rebase/behind",
                        },
                        {
                            "number": 6,
                            "title": "Draft",
                            "url": "file:///tmp/secret",
                            "branch": "codex/six",
                            "author": "codex",
                            "updated_at": "2026-09-01T12:00:00Z",
                            "head_sha": "6666666666666666",
                            "labels": {"needs": [], "blocked": []},
                            "checks": [],
                            "is_draft": True,
                            "next_action": "finish draft PR",
                        },
                    ],
                }
            }
        )

        kinds = {entry["kind"] for entry in payload["entries"]}
        self.assertEqual(
            kinds,
            {"needs-owner", "blocked-audit", "stale-gate", "failing-check", "rebase-needed", "draft"},
        )
        self.assertEqual(payload["count"], 6)
        self.assertEqual(payload["entries"][0]["priority"], 0)
        self.assertEqual(payload["entries"][-1]["kind"], "draft")
        self.assertNotIn("/tmp/secret", json.dumps(payload))

    def test_owner_queue_payload_reports_github_unavailable(self) -> None:
        payload = board.owner_queue_payload({"remote": {"available": False}})

        self.assertFalse(payload["available"])
        self.assertEqual(payload["entries"], [])
        self.assertIn("GitHub unavailable", payload["message"])

    def test_http_status_does_not_write_events_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "events.jsonl"
            handler = board.make_handler(
                board.BoardConfig(repo="owner/repo", store_path=str(store_path)),
                gh_json_runner=_gh_json,
                command_runner=_command_runner,
            )
            server = board.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_address[1]}"
                with urllib.request.urlopen(f"{base_url}/api/status", timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        self.assertFalse(store_path.exists())
        self.assertFalse(payload["board"]["recording"]["enabled"])

    def test_http_status_records_events_when_explicitly_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "events.jsonl"
            handler = board.make_handler(
                board.BoardConfig(
                    repo="owner/repo",
                    store_path=str(store_path),
                    record_events=True,
                    record_interval_seconds=0,
                ),
                gh_json_runner=_gh_json,
                command_runner=_command_runner,
            )
            server = board.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_address[1]}"
                with urllib.request.urlopen(f"{base_url}/api/status", timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
            report = board_store.event_report(path=store_path, limit=10)

        self.assertEqual(report["event_count"], 1)
        self.assertEqual(payload["board"]["recording"]["status"], "recorded")
        self.assertEqual(payload["board"]["recording"]["kept"], 1)

    def test_http_status_throttles_live_recording_by_interval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "events.jsonl"
            handler = board.make_handler(
                board.BoardConfig(
                    repo="owner/repo",
                    store_path=str(store_path),
                    record_events=True,
                    record_interval_seconds=3600,
                ),
                gh_json_runner=_gh_json,
                command_runner=_command_runner,
            )
            server = board.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_address[1]}"
                with urllib.request.urlopen(f"{base_url}/api/status", timeout=5) as response:
                    first = json.loads(response.read().decode("utf-8"))
                with urllib.request.urlopen(f"{base_url}/api/status", timeout=5) as response:
                    second = json.loads(response.read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
            report = board_store.event_report(path=store_path, limit=10)

        self.assertEqual(report["event_count"], 1)
        self.assertEqual(first["board"]["recording"]["status"], "recorded")
        self.assertEqual(second["board"]["recording"]["status"], "skipped")

    def test_http_status_live_record_error_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "events.jsonl"
            handler = board.make_handler(
                board.BoardConfig(repo="owner/repo", store_path=str(store_path), record_events=True),
                gh_json_runner=_gh_json,
                command_runner=_command_runner,
            )
            server = board.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_address[1]}"
                with patch(
                    "code_mower.board._record_live_snapshot",
                    side_effect=board_store.BoardStoreError("secret /tmp/private/path"),
                ):
                    with urllib.request.urlopen(f"{base_url}/api/status", timeout=5) as response:
                        payload = json.loads(response.read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        serialized = json.dumps(payload)
        self.assertEqual(payload["board"]["recording"]["status"], "error")
        self.assertIn("could not update local board event store", serialized)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("/tmp/private/path", serialized)

    def test_serve_rejects_invalid_recording_options_before_binding_port(self) -> None:
        err = StringIO()

        with redirect_stderr(err):
            code = board.main(["serve", "--repo", "owner/repo", "--record-events", "--max-events", "0"])

        self.assertEqual(code, 2)
        self.assertIn("--max-events", err.getvalue())

    def test_timelines_payload_summarizes_verdicts_and_spend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "events.jsonl"
            spend_path = Path(tmp) / "reviewer-spend.json"
            board_store.append_snapshot(
                {
                    "schema": lane_status.LANE_STATUS_SCHEMA,
                    "repo": "owner/repo",
                    "remote": {
                        "available": True,
                        "pull_requests": [
                            {
                                "number": 7,
                                "url": "https://github.com/owner/repo/pull/7",
                                "head_sha": "abcdef0123456789",
                                "labels": {
                                    "done": ["claude-audit-done"],
                                    "blocked": ["codex-audit-blocked"],
                                },
                            }
                        ],
                    },
                    "board": {"schema": "code_mower.board.v1"},
                },
                path=store_path,
                now=NOW,
            )
            spend_path.write_text(
                json.dumps(
                    {
                        "schema": reviewer_spend.SPEND_SCHEMA,
                        "runs": [
                            {
                                "created_at": "2026-09-01T12:01:00+00:00",
                                "lane": "claude-audit",
                                "repo": "owner/repo",
                                "pr_number": 7,
                                "head_sha": "abcdef0123456789",
                                "model": "sonnet",
                                "wall_seconds": 12.5,
                                "cost_usd": 0.125,
                                "total_tokens": 1000,
                                "verdict": "PASS",
                            },
                            {"repo": "owner/repo"},
                            "not a row",
                            {"lane": "claude-audit", "repo": "other/repo", "pr_number": 1, "verdict": "PASS"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            payload = board.timelines_payload(
                board.BoardConfig(repo="owner/repo", store_path=str(store_path), spend_path=str(spend_path)),
                limit=10,
            )

        self.assertEqual(payload["schema"], board.BOARD_TIMELINES_SCHEMA)
        self.assertEqual(
            [(entry["lane"], entry["verdict"], entry["head_sha_prefix"]) for entry in payload["verdicts"]["entries"]],
            [("claude-audit", "PASS", "abcdef012345"), ("codex-audit", "BLOCKED", "abcdef012345")],
        )
        self.assertEqual(payload["spend"]["skipped_rows"], 2)
        self.assertEqual(payload["spend"]["filtered_rows"], 1)
        self.assertEqual(payload["spend"]["groups"][0]["lane"], "claude-audit")
        self.assertEqual(payload["spend"]["groups"][0]["runs"], 1)
        self.assertEqual(payload["spend"]["groups"][0]["wall_seconds_total"], 12.5)
        self.assertEqual(payload["spend"]["groups"][0]["cost_usd_total"], 0.125)
        self.assertEqual(payload["spend"]["groups"][0]["total_tokens"], 1000)
        self.assertEqual(payload["spend"]["recent_runs"][0]["head_sha_prefix"], "abcdef012345")
        self.assertNotIn(str(Path(tmp)), json.dumps(payload))

    def test_timelines_payload_handles_missing_and_malformed_spend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = board.timelines_payload(
                board.BoardConfig(repo="owner/repo", repo_path=tmp, store_path=str(Path(tmp) / "events.jsonl")),
                limit=10,
            )
            spend_path = Path(tmp) / "reviewer-spend.json"
            spend_path.write_text("{not json", encoding="utf-8")
            malformed = board.timelines_payload(
                board.BoardConfig(repo="owner/repo", store_path=str(Path(tmp) / "events.jsonl"), spend_path=str(spend_path)),
                limit=10,
            )

        self.assertFalse(missing["spend"]["available"])
        self.assertIn("no reviewer spend file yet", missing["spend"]["message"])
        self.assertFalse(malformed["spend"]["available"])
        self.assertEqual(malformed["spend"]["message"], "could not read reviewer spend file")
        self.assertNotIn(str(Path(tmp)), json.dumps(malformed))

    def test_http_status_includes_local_timelines_when_github_is_unavailable(self) -> None:
        def unavailable_gh(_args: list[str]) -> object:
            raise lane_status.LaneStatusUnavailable("offline")

        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "events.jsonl"
            spend_path = Path(tmp) / "reviewer-spend.json"
            board_store.append_snapshot(
                {
                    "schema": lane_status.LANE_STATUS_SCHEMA,
                    "repo": "owner/repo",
                    "remote": {
                        "available": True,
                        "pull_requests": [
                            {
                                "number": 9,
                                "url": "https://github.com/owner/repo/pull/9",
                                "head_sha": "9999999999999999",
                                "labels": {"done": ["gitar-audit-done"]},
                            }
                        ],
                    },
                    "board": {"schema": "code_mower.board.v1"},
                },
                path=store_path,
                now=NOW,
            )
            spend_path.write_text(
                json.dumps(
                    {
                        "schema": reviewer_spend.SPEND_SCHEMA,
                        "runs": [
                            {
                                "created_at": "2026-09-01T12:02:00+00:00",
                                "lane": "gitar-audit",
                                "repo": "owner/repo",
                                "pr_number": 9,
                                "head_sha": "9999999999999999",
                                "wall_seconds": 1.0,
                                "verdict": "PASS",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            handler = board.make_handler(
                board.BoardConfig(repo="owner/repo", store_path=str(store_path), spend_path=str(spend_path)),
                gh_json_runner=unavailable_gh,
                command_runner=_command_runner,
            )
            server = board.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_address[1]}"
                with urllib.request.urlopen(f"{base_url}/api/status", timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        self.assertFalse(payload["remote"]["available"])
        self.assertFalse(payload["owner_queue"]["available"])
        self.assertEqual(payload["timelines"]["verdicts"]["entries"][0]["lane"], "gitar-audit")
        self.assertEqual(payload["timelines"]["spend"]["groups"][0]["lane"], "gitar-audit")

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
