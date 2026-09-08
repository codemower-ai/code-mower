from __future__ import annotations

import json
import subprocess
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from unittest import TestCase

from code_mower import lane_status


NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


class LaneStatusTests(TestCase):
    def test_collect_status_reports_pr_checks_labels_and_next_action(self) -> None:
        def gh_json(args: list[str]) -> object:
            if args[:2] == ["pr", "list"]:
                return [
                    {
                        "number": 12,
                        "title": "Fix build loop",
                        "url": "https://github.com/owner/repo/pull/12",
                        "headRefName": "codex/fix-build-loop",
                        "headRefOid": "abcdef0123456789abcdef0123456789abcdef01",
                        "author": {"login": "codex-bot"},
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "updatedAt": (NOW - timedelta(minutes=4)).isoformat().replace("+00:00", "Z"),
                        "labels": [
                            {"name": "builder:codex"},
                            {"name": "claude-audit-blocked"},
                        ],
                        "statusCheckRollup": [
                            {"name": "package", "status": "COMPLETED", "conclusion": "SUCCESS"},
                            {"context": "code-mower/gate", "state": "FAILURE"},
                        ],
                    }
                ]
            if args[:2] == ["run", "list"]:
                return [
                    {
                        "databaseId": 99,
                        "workflowName": "Code Mower gate",
                        "displayTitle": "publish gate",
                        "status": "completed",
                        "conclusion": "failure",
                        "event": "pull_request",
                        "headBranch": "codex/fix-build-loop",
                        "createdAt": NOW.isoformat().replace("+00:00", "Z"),
                        "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
                        "url": "https://github.com/owner/repo/actions/runs/99",
                    }
                ]
            raise lane_status.LaneStatusUnavailable("unexpected gh call")

        report = lane_status.collect_status(
            repo="owner/repo",
            gh_json_runner=gh_json,
            command_runner=lambda _args: _completed(""),
            now=NOW,
        )

        pr = report["remote"]["pull_requests"][0]
        self.assertEqual(report["schema"], lane_status.LANE_STATUS_SCHEMA)
        self.assertEqual(pr["labels"]["builder"], ["builder:codex"])
        self.assertEqual(pr["labels"]["blocked"], ["claude-audit-blocked"])
        self.assertEqual(pr["checks"][1]["name"], "code-mower/gate")
        self.assertEqual(pr["next_action"], "fix BLOCKED audit")
        self.assertEqual(report["next_action"], "fix BLOCKED audit")
        self.assertEqual(report["remote"]["gate_health"]["status"], "warn")
        self.assertIn("fix BLOCKED audit", lane_status.render_text(report))

    def test_render_text_includes_copy_pasteable_gate_rerun_command(self) -> None:
        def gh_json(args: list[str]) -> object:
            if args[:2] == ["pr", "list"]:
                return [
                    {
                        "number": 34,
                        "title": "Install Code Mower",
                        "url": "https://github.com/owner/repo/pull/34",
                        "headRefName": "chore/code-mower-reviewer-gate",
                        "headRefOid": "1234567890abcdef1234567890abcdef12345678",
                        "author": {"login": "alice"},
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
                        "labels": [{"name": "needs-claude-audit"}],
                        "statusCheckRollup": [
                            {"context": "code-mower/gate", "state": "PENDING"},
                        ],
                    }
                ]
            if args[:2] == ["run", "list"]:
                return []
            raise lane_status.LaneStatusUnavailable("unexpected gh call")

        report = lane_status.collect_status(
            repo="owner/repo",
            gh_json_runner=gh_json,
            command_runner=lambda _args: _completed(""),
            now=NOW,
        )

        pr = report["remote"]["pull_requests"][0]
        expected = (
            "gh workflow run code-mower-gate.yml --repo owner/repo "
            "-f pr_number=34 -f head_sha=1234567890abcdef1234567890abcdef12345678"
        )
        self.assertEqual(pr["gate_rerun_command"], expected)
        rendered = lane_status.render_text(report)
        self.assertIn("next: waiting for audits or owner input", rendered)
        self.assertIn(f"rerun gate: {expected}", rendered)

    def test_stale_needed_audit_names_runner_requeue_path(self) -> None:
        def gh_json(args: list[str]) -> object:
            if args[:2] == ["pr", "list"]:
                return [
                    {
                        "number": 35,
                        "title": "Refresh lane guidance",
                        "url": "https://github.com/owner/repo/pull/35",
                        "headRefName": "codex/stale-audit",
                        "headRefOid": "abcdef0123456789abcdef0123456789abcdef01",
                        "author": {"login": "alice"},
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "updatedAt": (NOW - timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
                        "labels": [{"name": "needs-codex-audit"}],
                        "statusCheckRollup": [{"context": "code-mower/gate", "state": "FAILURE"}],
                    }
                ]
            if args[:2] == ["run", "list"]:
                return []
            raise lane_status.LaneStatusUnavailable("unexpected gh call")

        report = lane_status.collect_status(
            repo="owner/repo",
            gh_json_runner=gh_json,
            command_runner=lambda _args: _completed(""),
            now=NOW,
        )

        pr = report["remote"]["pull_requests"][0]
        self.assertTrue(pr["stale"])
        self.assertEqual(pr["next_action"], "requeue stale audit")
        self.assertEqual(report["next_action"], "requeue stale audit")
        self.assertIn("codex", pr["next_detail"])
        self.assertIn("runner/dispatcher", pr["next_detail"])
        self.assertEqual(report["next_detail"], pr["next_detail"])
        rendered = lane_status.render_text(report)
        self.assertIn("next: requeue stale audit", rendered)
        self.assertIn("detail: stale audit request for codex", rendered)
        self.assertIn("Detail: stale audit request for codex", rendered)

    def test_stale_gate_only_wait_keeps_gate_rerun_command(self) -> None:
        def gh_json(args: list[str]) -> object:
            if args[:2] == ["pr", "list"]:
                return [
                    {
                        "number": 36,
                        "title": "Republish gate",
                        "url": "https://github.com/owner/repo/pull/36",
                        "headRefName": "codex/gate",
                        "headRefOid": "1234567890abcdef1234567890abcdef12345678",
                        "author": {"login": "alice"},
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "updatedAt": (NOW - timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
                        "labels": [{"name": "claude-audit-done"}],
                        "statusCheckRollup": [{"context": "code-mower/gate", "state": "PENDING"}],
                    }
                ]
            if args[:2] == ["run", "list"]:
                return []
            raise lane_status.LaneStatusUnavailable("unexpected gh call")

        report = lane_status.collect_status(
            repo="owner/repo",
            gh_json_runner=gh_json,
            command_runner=lambda _args: _completed(""),
            now=NOW,
        )

        pr = report["remote"]["pull_requests"][0]
        self.assertEqual(pr["next_action"], "rerun stale gate")
        self.assertEqual(report["next_action"], "rerun stale gate")
        self.assertIn("current head", pr["next_detail"])
        self.assertEqual(report["next_detail"], pr["next_detail"])
        rendered = lane_status.render_text(report)
        self.assertIn("next: rerun stale gate", rendered)
        self.assertIn("rerun gate: gh workflow run code-mower-gate.yml", rendered)

    def test_collect_status_degrades_when_github_unavailable_and_shows_local_state(self) -> None:
        def gh_json(_args: list[str]) -> object:
            raise lane_status.LaneStatusUnavailable("gh pr failed")

        def command_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
            if args[:4] == ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"]:
                return _completed("p123\ncnode\nn127.0.0.1:5330\n")
            if args == ["ps", "-p", "123", "-o", "command="]:
                return _completed("code-mower board serve --repo owner/repo\n")
            if args == ["lsof", "-a", "-p", "123", "-d", "cwd", "-Fn"]:
                return _completed("p123\nn/tmp/lane-checkout\n")
            if args == ["ps", "-axo", "pid=,command="]:
                return _completed(" 456 codex exec review\n")
            if args == ["lsof", "-a", "-p", "456", "-d", "cwd", "-Fn"]:
                return _completed("p456\nn/tmp/codex-lane\n")
            return _completed("", returncode=1)

        report = lane_status.collect_status(
            repo="owner/repo",
            gh_json_runner=gh_json,
            command_runner=command_runner,
            now=NOW,
        )

        self.assertFalse(report["remote"]["available"])
        self.assertEqual(report["local_boards"]["boards"][0]["port"], 5330)
        self.assertEqual(report["local_boards"]["boards"][0]["repo"], "owner/repo")
        self.assertEqual(report["local_boards"]["boards"][0]["url"], "http://127.0.0.1:5330/")
        self.assertEqual(report["local_boards"]["boards"][0]["cwd"], lane_status.LOCAL_PATH_REDACTION)
        self.assertTrue(report["local_boards"]["boards"][0]["cwd_redacted"])
        self.assertEqual(report["local_processes"]["processes"][0]["provider"], "codex")
        self.assertEqual(report["local_processes"]["processes"][0]["cwd"], lane_status.LOCAL_PATH_REDACTION)
        self.assertTrue(report["local_processes"]["processes"][0]["cwd_redacted"])
        self.assertEqual(report["next_action"], "remote unavailable; inspect local lanes")
        rendered = lane_status.render_text(report)
        self.assertIn("Local boards:", rendered)
        self.assertNotIn("/tmp/lane-checkout", rendered)

    def test_collect_status_detects_local_board_from_ss_when_lsof_unavailable(
        self,
    ) -> None:
        def gh_json(_args: list[str]) -> object:
            raise lane_status.LaneStatusUnavailable("gh pr failed")

        def command_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
            if args[:4] == ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"]:
                return _completed("", returncode=1)
            if args == ["ss", "-H", "-ltnp"]:
                return _completed(
                    'tcp LISTEN 0 4096 127.0.0.1:5332 0.0.0.0:* users:(("python3",pid=321,fd=3))\n'
                )
            if args == ["ps", "-p", "321", "-o", "command="]:
                return _completed("python3 -m code_mower.cli board serve --repo owner/repo\n")
            if args == ["lsof", "-a", "-p", "321", "-d", "cwd", "-Fn"]:
                return _completed("", returncode=1)
            if args == ["pwdx", "321"]:
                return _completed("321: /tmp/code-mower-board\n")
            if args == ["ps", "-axo", "pid=,command="]:
                return _completed("")
            return _completed("", returncode=1)

        report = lane_status.collect_status(
            repo="owner/repo",
            gh_json_runner=gh_json,
            command_runner=command_runner,
            now=NOW,
        )

        self.assertFalse(report["remote"]["available"])
        self.assertTrue(report["local_boards"]["available"])
        self.assertEqual(report["local_boards"]["boards"][0]["port"], 5332)
        self.assertEqual(report["local_boards"]["boards"][0]["process"], "python3")
        self.assertEqual(report["local_boards"]["boards"][0]["confidence"], "high")
        self.assertEqual(report["local_boards"]["boards"][0]["repo"], "owner/repo")
        self.assertEqual(report["local_boards"]["boards"][0]["url"], "http://127.0.0.1:5332/")
        self.assertEqual(report["local_boards"]["boards"][0]["cwd"], lane_status.LOCAL_PATH_REDACTION)
        self.assertEqual(report["next_action"], "remote unavailable; inspect local lanes")

    def test_collect_status_detects_multiple_local_boards_with_repo_hints(self) -> None:
        def gh_json(_args: list[str]) -> object:
            raise lane_status.LaneStatusUnavailable("gh pr failed")

        def command_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
            if args[:4] == ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"]:
                return _completed("p123\ncPython\nn127.0.0.1:5332\np124\ncPython\nn127.0.0.1:5333\n")
            if args == ["ps", "-p", "123", "-o", "command="]:
                return _completed("python -m code_mower.cli board serve --repo owner/one\n")
            if args == ["ps", "-p", "124", "-o", "command="]:
                return _completed("code-mower board serve --repo=owner/two\n")
            if args == ["lsof", "-a", "-p", "123", "-d", "cwd", "-Fn"]:
                return _completed("p123\nn/tmp/one\n")
            if args == ["lsof", "-a", "-p", "124", "-d", "cwd", "-Fn"]:
                return _completed("p124\nn/tmp/two\n")
            if args == ["ps", "-axo", "pid=,command="]:
                return _completed("")
            return _completed("", returncode=1)

        report = lane_status.collect_status(
            repo="owner/repo",
            gh_json_runner=gh_json,
            command_runner=command_runner,
            now=NOW,
        )

        boards = report["local_boards"]["boards"]
        self.assertEqual([board["repo"] for board in boards], ["owner/one", "owner/two"])
        self.assertEqual([board["url"] for board in boards], ["http://127.0.0.1:5332/", "http://127.0.0.1:5333/"])
        self.assertNotIn("/tmp/one", json.dumps(report))

    def test_collect_status_never_reports_no_active_lanes_when_github_unavailable(
        self,
    ) -> None:
        def gh_json(_args: list[str]) -> object:
            raise lane_status.LaneStatusUnavailable("gh pr failed")

        report = lane_status.collect_status(
            repo="owner/repo",
            gh_json_runner=gh_json,
            command_runner=lambda _args: _completed(""),
            now=NOW,
        )

        self.assertFalse(report["remote"]["available"])
        self.assertEqual(report["next_action"], "remote unavailable; fix GitHub access")
        rendered = lane_status.render_text(report)
        self.assertIn("Open PRs: unavailable", rendered)
        self.assertIn("Recent Code Mower workflows: unavailable", rendered)
        self.assertIn("Gate alerts: unavailable", rendered)
        self.assertNotIn("Open PRs: none", rendered)
        self.assertNotIn("Next: no active lanes", rendered)

    def test_collect_status_can_include_local_paths_for_debugging(self) -> None:
        def gh_json(_args: list[str]) -> object:
            raise lane_status.LaneStatusUnavailable("gh pr failed")

        def command_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
            if args[:4] == ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"]:
                return _completed("p123\ncnode\nn127.0.0.1:5330\n")
            if args == ["ps", "-p", "123", "-o", "command="]:
                return _completed("code-mower board serve --repo owner/repo\n")
            if args == ["lsof", "-a", "-p", "123", "-d", "cwd", "-Fn"]:
                return _completed("p123\nn/tmp/lane-checkout\n")
            if args == ["ps", "-axo", "pid=,command="]:
                return _completed(" 456 codex exec review\n")
            if args == ["lsof", "-a", "-p", "456", "-d", "cwd", "-Fn"]:
                return _completed("p456\nn/tmp/codex-lane\n")
            return _completed("", returncode=1)

        report = lane_status.collect_status(
            repo="owner/repo",
            gh_json_runner=gh_json,
            command_runner=command_runner,
            now=NOW,
            show_local_paths=True,
        )

        self.assertEqual(report["local_boards"]["boards"][0]["cwd"], "/tmp/lane-checkout")
        self.assertNotIn("cwd_redacted", report["local_boards"]["boards"][0])
        self.assertEqual(report["local_processes"]["processes"][0]["cwd"], "/tmp/codex-lane")

    def test_main_json_outputs_stable_shape(self) -> None:
        def gh_json(args: list[str]) -> object:
            if args[:2] == ["pr", "list"]:
                return []
            if args[:2] == ["run", "list"]:
                return []
            raise lane_status.LaneStatusUnavailable("unexpected gh call")

        out = StringIO()
        with redirect_stdout(out):
            exit_code = lane_status.main(
                ["status", "--repo", "owner/repo", "--json"],
                gh_json_runner=gh_json,
                command_runner=lambda _args: _completed(""),
            )

        payload = json.loads(out.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["schema"], lane_status.LANE_STATUS_SCHEMA)
        self.assertEqual(payload["repo"], "owner/repo")
        self.assertEqual(
            set(payload),
            {
                "schema",
                "repo",
                "generated_at",
                "remote",
                "local_boards",
                "local_processes",
                "next_action",
                "next_detail",
            },
        )
        self.assertEqual(payload["next_action"], "no active lanes")
        self.assertEqual(payload["next_detail"], "")

    def test_collect_lane_processes_identifies_muse_and_versioned_muse_bin(self) -> None:
        ps_output = (
            "101 /usr/local/bin/muse exec --prompt-file /tmp/secret-prompt.md --token secret-token-123\n"
            "102 /opt/local/bin/muse-bin-0.4.2 --sandbox --permission-mode autonomous\n"
            "103 muse-bin-v1.0.0-rc1 --prompt-path /tmp/prompt2.md\n"
        )

        def command_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
            if args[:1] == ["ps"] and "-axo" in args:
                return _completed(ps_output)
            if args[:1] == ["lsof"]:
                return _completed("n/tmp/muse-lane\n")
            return _completed("", returncode=1)

        report = lane_status.collect_lane_processes(command_runner)

        self.assertTrue(report["available"])
        self.assertEqual(len(report["processes"]), 3)
        for proc in report["processes"]:
            self.assertEqual(proc["provider"], "muse")
            self.assertEqual(proc["process"], "muse")
            self.assertEqual(proc["cwd"], "/tmp/muse-lane")
        serialized = json.dumps(report)
        self.assertNotIn("0.4.2", serialized)
        self.assertNotIn("v1.0.0", serialized)
        self.assertNotIn("muse-bin-", serialized)
        self.assertNotIn("secret-prompt", serialized)
        self.assertNotIn("secret-token", serialized)
        self.assertNotIn("/usr/local/bin", serialized)
        self.assertNotIn("/opt/local/bin", serialized)

    def test_collect_lane_processes_identifies_supervised_child_provider(self) -> None:
        ps_output = (
            "201 code-mower lane-delivery supervise --log /tmp/secret.log "
            "--timeout-seconds 1800 --cwd /tmp/muse-lane --status-file /tmp/status.json "
            "-- muse exec --prompt-file /tmp/prompt.md\n"
            "202 python3 -m code_mower.lane_delivery supervise --log /tmp/secret2.log "
            "--timeout-seconds 1800 --cwd /tmp/muse-lane -- /usr/local/bin/muse-bin-0.4.2 --sandbox\n"
            "203 code-mower lane-delivery supervise --log /tmp/codex.log "
            "--timeout-seconds 1800 --cwd /tmp/codex-lane -- codex exec --prompt-file /tmp/codex.md\n"
        )

        def command_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
            if args[:1] == ["ps"] and "-axo" in args:
                return _completed(ps_output)
            if args[:1] == ["lsof"]:
                return _completed("n/tmp/work-lane\n")
            return _completed("", returncode=1)

        report = lane_status.collect_lane_processes(command_runner)

        self.assertTrue(report["available"])
        self.assertEqual(len(report["processes"]), 3)
        self.assertEqual(report["processes"][0]["provider"], "muse")
        self.assertEqual(report["processes"][0]["process"], "muse")
        self.assertEqual(report["processes"][1]["provider"], "muse")
        self.assertEqual(report["processes"][1]["process"], "muse")
        self.assertEqual(report["processes"][2]["provider"], "codex")
        self.assertEqual(report["processes"][2]["process"], "codex")

        serialized = json.dumps(report)
        self.assertNotIn("secret.log", serialized)
        self.assertNotIn("secret2.log", serialized)
        self.assertNotIn("codex.log", serialized)
        self.assertNotIn("timeout-seconds", serialized)
        self.assertNotIn("prompt.md", serialized)
        self.assertNotIn("0.4.2", serialized)
        self.assertNotIn("muse-bin-", serialized)

    def test_collect_lane_processes_muse_false_positive_boundaries(self) -> None:
        ps_output = (
            "301 /usr/bin/museum --exhibit modern-art\n"
            "302 /usr/bin/amuse --joke funny\n"
            "303 muse-bin\n"
            "304 muse-bin-\n"
            "305 /usr/bin/mused\n"
            "306 /usr/bin/muse-tools\n"
            "307 code-mower lane-delivery supervise --log /tmp/l --timeout-seconds 10 -- museum --exhibit\n"
            "308 code-mower lane-delivery supervise --log /tmp/l --timeout-seconds 10 -- echo hello\n"
        )

        def command_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
            if args[:1] == ["ps"] and "-axo" in args:
                return _completed(ps_output)
            if args[:1] == ["lsof"]:
                return _completed("n/tmp/lane\n")
            return _completed("", returncode=1)

        report = lane_status.collect_lane_processes(command_runner)

        self.assertTrue(report["available"])
        self.assertEqual(report["processes"], [])

    def test_collect_status_discovers_supervised_muse_lane_with_redacted_paths(self) -> None:
        def gh_json(_args: list[str]) -> object:
            raise lane_status.LaneStatusUnavailable("gh pr failed")

        def command_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
            if args[:4] == ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"]:
                return _completed("")
            if args == ["ps", "-axo", "pid=,command="]:
                return _completed(
                    " 806 code-mower lane-delivery supervise --log /tmp/secret.log "
                    "--timeout-seconds 1800 --cwd /tmp/muse-lane -- muse exec\n"
                )
            if args == ["lsof", "-a", "-p", "806", "-d", "cwd", "-Fn"]:
                return _completed("p806\nn/tmp/muse-lane\n")
            return _completed("", returncode=1)

        report = lane_status.collect_status(
            repo="owner/repo",
            gh_json_runner=gh_json,
            command_runner=command_runner,
            now=NOW,
        )

        self.assertEqual(len(report["local_processes"]["processes"]), 1)
        proc = report["local_processes"]["processes"][0]
        self.assertEqual(proc["provider"], "muse")
        self.assertEqual(proc["process"], "muse")
        self.assertEqual(proc["cwd"], lane_status.LOCAL_PATH_REDACTION)
        self.assertTrue(proc["cwd_redacted"])
        self.assertEqual(report["next_action"], "remote unavailable; inspect local lanes")
        rendered = lane_status.render_text(report)
        self.assertIn("Local lane processes:\n- muse pid=806 process=muse cwd=[local path hidden]", rendered)
        self.assertNotIn("/tmp/muse-lane", rendered)
        self.assertNotIn("secret.log", rendered)
        self.assertNotIn("timeout-seconds", rendered)
