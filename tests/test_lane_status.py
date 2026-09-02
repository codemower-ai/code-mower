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

    def test_collect_status_degrades_when_github_unavailable_and_shows_local_state(self) -> None:
        def gh_json(_args: list[str]) -> object:
            raise lane_status.LaneStatusUnavailable("gh pr failed")

        def command_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
            if args[:4] == ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"]:
                return _completed("p123\ncnode\nn127.0.0.1:5330\n")
            if args == ["ps", "-p", "123", "-o", "command="]:
                return _completed("node /tmp/bin/agenttrail /repo --no-open\n")
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
        self.assertEqual(report["agenttrail"]["boards"][0]["port"], 5330)
        self.assertEqual(report["agenttrail"]["boards"][0]["cwd"], lane_status.LOCAL_PATH_REDACTION)
        self.assertTrue(report["agenttrail"]["boards"][0]["cwd_redacted"])
        self.assertEqual(report["local_processes"]["processes"][0]["provider"], "codex")
        self.assertEqual(report["local_processes"]["processes"][0]["cwd"], lane_status.LOCAL_PATH_REDACTION)
        self.assertTrue(report["local_processes"]["processes"][0]["cwd_redacted"])
        self.assertEqual(report["next_action"], "remote unavailable; inspect local lanes")
        rendered = lane_status.render_text(report)
        self.assertIn("Local boards:", rendered)
        self.assertNotIn("AgentTrail boards:", rendered)
        self.assertNotIn("/tmp/lane-checkout", rendered)

    def test_collect_status_can_include_local_paths_for_debugging(self) -> None:
        def gh_json(_args: list[str]) -> object:
            raise lane_status.LaneStatusUnavailable("gh pr failed")

        def command_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
            if args[:4] == ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"]:
                return _completed("p123\ncnode\nn127.0.0.1:5330\n")
            if args == ["ps", "-p", "123", "-o", "command="]:
                return _completed("node /tmp/bin/agenttrail /repo --no-open\n")
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

        self.assertEqual(report["agenttrail"]["boards"][0]["cwd"], "/tmp/lane-checkout")
        self.assertNotIn("cwd_redacted", report["agenttrail"]["boards"][0])
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
        self.assertEqual(payload["next_action"], "no active lanes")
