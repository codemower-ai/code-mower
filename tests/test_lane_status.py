from __future__ import annotations
from lineage_consumer_fixtures import policy

import json
import subprocess
import tempfile
from contextlib import chdir, redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest import TestCase

from code_mower import board, lane_status


NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


class LaneStatusTests(TestCase):
    def test_collect_status_uses_newest_duplicate_check_run_for_current_state(self) -> None:
        def gh_json(args: list[str]) -> object:
            if args[:2] == ["pr", "list"]:
                return [
                    {
                        "number": 11,
                        "title": "Fix accessible label",
                        "url": "https://github.com/owner/repo/pull/11",
                        "headRefName": "fix/accessible-label",
                        "headRefOid": "abcdef0123456789abcdef0123456789abcdef01",
                        "author": {"login": "builder"},
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
                        "labels": [{"name": "claude-audit-done"}],
                        "statusCheckRollup": [
                            {
                                "__typename": "CheckRun",
                                "name": "validate-branch-name / validate-branch-name",
                                "workflowName": "branch-name-check",
                                "detailsUrl": "https://github.com/owner/repo/actions/runs/1",
                                "completedAt": "2026-09-01T11:40:00Z",
                                "conclusion": "FAILURE",
                            },
                            {
                                "__typename": "CheckRun",
                                "name": "validate-branch-name / validate-branch-name",
                                "workflowName": "branch-name-check",
                                "detailsUrl": "https://github.com/owner/repo/actions/runs/3",
                                "completedAt": "2026-09-01T11:43:00Z",
                                "conclusion": "SUCCESS",
                            },
                        ],
                    }
                ]
            if args[:2] == ["run", "list"]:
                return []
            if args[0] == "api" and "/comments?" in args[1]:
                return []
            raise lane_status.LaneStatusUnavailable("unexpected gh call")

        report = lane_status.collect_status(
            lineage_config=policy({}),
            repo="owner/repo",
            gh_json_runner=gh_json,
            command_runner=lambda _args: _completed(""),
            now=NOW,
        )

        pr = report["remote"]["pull_requests"][0]
        branch_checks = [
            check for check in pr["checks"] if check["name"].startswith("validate-branch-name")
        ]
        self.assertEqual(branch_checks, [{"name": "validate-branch-name / validate-branch-name", "state": "success"}])
        self.assertEqual(pr["next_action"], "ready for merge or auto-merge")
        self.assertEqual(report["remote"]["gate_health"]["alerts"], [])

    def test_check_normalization_keeps_distinct_failures_actionable(self) -> None:
        checks = lane_status._checks(
            [
                {
                    "__typename": "CheckRun",
                    "name": "package",
                    "workflowName": "quality",
                    "completedAt": "2026-09-01T11:40:00Z",
                    "conclusion": "FAILURE",
                },
                {
                    "__typename": "CheckRun",
                    "name": "package",
                    "workflowName": "quality",
                    "completedAt": "2026-09-01T11:43:00Z",
                    "conclusion": "SUCCESS",
                },
                {
                    "__typename": "CheckRun",
                    "name": "code-mower/gate",
                    "workflowName": "gate",
                    "completedAt": "2026-09-01T11:42:00Z",
                    "conclusion": "FAILURE",
                },
            ]
        )

        self.assertEqual(
            checks,
            [
                {"name": "package", "state": "success"},
                {"name": "code-mower/gate", "state": "failure"},
            ],
        )

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
            if args[0] == "api" and "/comments?" in args[1]:
                return []
            raise lane_status.LaneStatusUnavailable("unexpected gh call")

        report = lane_status.collect_status(
            lineage_config=policy({}),
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

    def test_readable_pr_without_lineage_policy_reports_optional_lineage(self) -> None:
        calls: list[list[str]] = []

        def gh_json(args: list[str]) -> object:
            calls.append(args)
            if args[:2] == ["pr", "list"]:
                return [
                    {
                        "number": 13,
                        "title": "Document optional lineage",
                        "url": "https://github.com/owner/repo/pull/13",
                        "headRefName": "docs/optional-lineage",
                        "headRefOid": "abcdef0123456789abcdef0123456789abcdef01",
                        "author": {"login": "alice"},
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
                        "labels": [{"name": "claude-audit-done"}],
                        "statusCheckRollup": [
                            {"context": "code-mower/gate", "state": "SUCCESS"},
                        ],
                    }
                ]
            if args[:2] == ["run", "list"]:
                return []
            raise lane_status.LaneStatusUnavailable("lineage comments must not be read without policy")

        report = lane_status.collect_status(
            repo="owner/repo",
            gh_json_runner=gh_json,
            command_runner=lambda _args: _completed(""),
            now=NOW,
        )

        pr = report["remote"]["pull_requests"][0]
        self.assertEqual(pr["lineage"]["status"], "optional")
        self.assertEqual(pr["lineage"]["reason"], "lineage_policy_not_configured")
        self.assertEqual(
            pr["lineage"]["next_action"],
            "pass --config code-mower.yml to evaluate lineage",
        )
        self.assertEqual(pr["next_action"], "ready for merge or auto-merge")
        self.assertFalse(any(args[0] == "api" for args in calls))
        rendered = lane_status.render_text(report)
        self.assertIn("lineage: optional (lineage_policy_not_configured)", rendered)
        self.assertIn("lineage next: pass --config code-mower.yml to evaluate lineage", rendered)
        self.assertIn("Recent Code Mower workflows: none", rendered)

    def test_configured_but_unreadable_lineage_has_precise_recovery(self) -> None:
        def gh_json(args: list[str]) -> object:
            if args[:2] == ["pr", "list"]:
                return [
                    {
                        "number": 14,
                        "title": "Recover lineage visibility",
                        "url": "https://github.com/owner/repo/pull/14",
                        "headRefName": "codex/lineage-recovery",
                        "headRefOid": "abcdef0123456789abcdef0123456789abcdef01",
                        "author": {"login": "alice"},
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
                        "labels": [{"name": "builder:codex"}],
                        "statusCheckRollup": [
                            {"context": "code-mower/gate", "state": "PENDING"},
                        ],
                    }
                ]
            if args[:2] == ["run", "list"]:
                return []
            if args[0] == "api":
                raise lane_status.LaneStatusUnavailable("comment access denied")
            raise lane_status.LaneStatusUnavailable("unexpected gh call")

        report = lane_status.collect_status(
            lineage_config=policy({}),
            repo="owner/repo",
            gh_json_runner=gh_json,
            command_runner=lambda _args: _completed(""),
            now=NOW,
        )

        pr = report["remote"]["pull_requests"][0]
        self.assertEqual(pr["lineage"]["status"], "unavailable")
        self.assertEqual(pr["lineage"]["reason"], "lineage_unreadable")
        self.assertEqual(pr["next_action"], "restore readable lineage metadata and rerun status")
        self.assertEqual(report["next_action"], pr["next_action"])
        self.assertIn("lineage unavailable: lineage_unreadable", pr["next_detail"])

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
            if args[0] == "api" and "/comments?" in args[1]:
                return []
            raise lane_status.LaneStatusUnavailable("unexpected gh call")

        report = lane_status.collect_status(
            lineage_config=policy({}),
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
            if args[0] == "api" and "/comments?" in args[1]:
                return []
            raise lane_status.LaneStatusUnavailable("unexpected gh call")

        report = lane_status.collect_status(
            lineage_config=policy({}),
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
            if args[0] == "api" and "/comments?" in args[1]:
                return []
            raise lane_status.LaneStatusUnavailable("unexpected gh call")

        report = lane_status.collect_status(
            lineage_config=policy({}),
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
            lineage_config=policy({}),
            repo="owner/repo",
            gh_json_runner=gh_json,
            command_runner=command_runner,
            now=NOW,
            board_inventory_collector=lambda **kwargs: lane_status.collect_local_boards(
                kwargs["command_runner"]
            ),
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
            lineage_config=policy({}),
            repo="owner/repo",
            gh_json_runner=gh_json,
            command_runner=command_runner,
            now=NOW,
            board_inventory_collector=lambda **kwargs: lane_status.collect_local_boards(
                kwargs["command_runner"]
            ),
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
            lineage_config=policy({}),
            repo="owner/repo",
            gh_json_runner=gh_json,
            command_runner=command_runner,
            now=NOW,
            board_inventory_collector=lambda **kwargs: lane_status.collect_local_boards(
                kwargs["command_runner"]
            ),
        )

        boards = report["local_boards"]["boards"]
        self.assertEqual([board["repo"] for board in boards], ["owner/one", "owner/two"])
        self.assertEqual([board["url"] for board in boards], ["http://127.0.0.1:5332/", "http://127.0.0.1:5333/"])
        self.assertNotIn("/tmp/one", json.dumps(report))

    def test_lanes_status_uses_inventory_versions_service_identity_and_guidance(self) -> None:
        inventory = {
            "schema": board.BOARD_INVENTORY_SCHEMA,
            "available": True,
            "message": "",
            "boards": [
                {
                    "port": 5332,
                    "pid": 123,
                    "process": "code-mower",
                    "confidence": "high",
                    "url": "http://127.0.0.1:5332/",
                    "repo": "owner/repo",
                    "invoking_version": "1.6.0",
                    "serving_version": "1.5.2",
                    "installed_version": "1.5.2",
                    "restart_recommended": True,
                    "managed": True,
                    "service_label": "ai.codemower.board.5332",
                    "service_supervision": "confirmed",
                    "restart_command": "code-mower board service restart --repo owner/repo --repo-path . --host 127.0.0.1 --port 5332 --replace",
                }
            ],
            "next_action": "restart stale managed Board",
            "next_detail": "restart it",
        }
        report = lane_status.collect_status(
            lineage_config=policy({}),
            repo="owner/repo",
            gh_json_runner=lambda args: [],
            command_runner=lambda args: _completed(""),
            now=NOW,
            board_inventory_collector=lambda **_kwargs: inventory,
        )

        self.assertEqual(report["local_boards"], inventory)
        rendered = lane_status.render_text(report)
        self.assertIn("invoking=1.6.0 serving=1.5.2 installed=1.5.2", rendered)
        self.assertIn("service=ai.codemower.board.5332", rendered)
        self.assertIn(f"restart: {inventory['boards'][0]['restart_command']}", rendered)

    def test_collect_status_never_reports_no_active_lanes_when_github_unavailable(
        self,
    ) -> None:
        def gh_json(_args: list[str]) -> object:
            raise lane_status.LaneStatusUnavailable("gh pr failed")

        report = lane_status.collect_status(
            lineage_config=policy({}),
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
            lineage_config=policy({}),
            repo="owner/repo",
            gh_json_runner=gh_json,
            command_runner=command_runner,
            now=NOW,
            show_local_paths=True,
        )

        self.assertEqual(report["local_boards"]["boards"][0]["cwd"], "/tmp/lane-checkout")
        self.assertNotIn("cwd_redacted", report["local_boards"]["boards"][0])
        self.assertEqual(report["local_processes"]["processes"][0]["cwd"], "/tmp/codex-lane")

    def test_main_implicit_invalid_or_non_jira_config_preserves_status(self):
        for content in ("tracker: [", "unrelated: true\n", "tracker:\n  kind: github\n"):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                Path(directory, "code-mower.yml").write_text(content)
                out, err = StringIO(), StringIO()
                with chdir(directory), redirect_stdout(out), redirect_stderr(err):
                    code = lane_status.main(
                        ["status", "--repo", "owner/repo", "--json"],
                        gh_json_runner=lambda args: [],
                        command_runner=lambda args: _completed(""),
                    )
                self.assertEqual(code, 0)
                self.assertTrue(json.loads(out.getvalue())["remote"]["available"])
                self.assertNotIn("tracker", json.loads(out.getvalue()))
                self.assertEqual(err.getvalue(), "")

    def test_main_explicit_invalid_config_fails_clearly(self):
        for content in ("tracker: [", "unrelated: true\n", None):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                path = Path(directory, "code-mower.yml")
                if content is not None:
                    path.write_text(content)
                out, err = StringIO(), StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = lane_status.main(
                        ["status", "--repo", "owner/repo", "--config", str(path)],
                        gh_json_runner=lambda args: self.fail("invalid config must stop before GitHub"),
                    )
                self.assertEqual(code, 2)
                self.assertIn("invalid Code Mower config", err.getvalue())
                self.assertEqual(out.getvalue(), "")

    def test_main_json_outputs_stable_shape(self) -> None:
        def gh_json(args: list[str]) -> object:
            if args[:2] == ["pr", "list"]:
                return []
            if args[:2] == ["run", "list"]:
                return []
            if args[0] == "api" and "/comments?" in args[1]:
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
                "orchestrator_lease",
                "next_action",
                "next_detail",
            },
        )
        self.assertEqual(set(payload["orchestrator_lease"]), {"state", "provider", "expires_at"})
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
            lineage_config=policy({}),
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


class ListenerInventoryAvailabilityTests(TestCase):
    """The empty inventory and the inventory that could not be taken, directly.

    Every other caller reaches this through a fake host; these go at
    `lane_status` itself, so the answered/unanswered distinction is asserted
    against real `CompletedProcess` return codes rather than a fake's flag.
    """

    def test_lsof_exiting_one_has_answered_that_nothing_is_listening(self) -> None:
        # `lsof` reports "nothing matched" by exiting 1 with no output. That is
        # an answer, and the port really is free -- even though the `ss`
        # fallback that follows it fails outright.
        def command_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
            if args[:1] == ["lsof"]:
                return _completed("", returncode=1)
            raise OSError("no ss here")

        inventory = lane_status.local_listener_inventory(command_runner)

        self.assertEqual((inventory["available"], inventory["listeners"]), (True, []))

    def test_an_ss_fallback_exiting_one_has_not_answered_for_a_missing_lsof(self) -> None:
        # No `lsof` on the host is not an answer, and neither is `ss` exiting 1:
        # `ss` has no "nothing matched" code, so 1 is a failure and occupancy
        # stays unknown. Reading 1 as an answer here is what let a host with no
        # working probe report every port free.
        def command_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
            if args[:1] == ["lsof"]:
                raise OSError("no lsof here")
            return _completed("", returncode=1)

        inventory = lane_status.local_listener_inventory(command_runner)

        self.assertEqual((inventory["available"], inventory["listeners"]), (False, []))

    def test_an_ss_fallback_exiting_zero_answers_for_a_missing_lsof(self) -> None:
        # `ss` says "nothing is listening" by exiting 0 with an empty table.
        # That is the fallback's answer, and it makes the inventory available.
        def command_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
            if args[:1] == ["lsof"]:
                raise OSError("no lsof here")
            return _completed("", returncode=0)

        inventory = lane_status.local_listener_inventory(command_runner)

        self.assertEqual((inventory["available"], inventory["listeners"]), (True, []))

    def test_lsof_exiting_one_with_an_error_has_not_answered(self) -> None:
        # Exit 1 is `lsof`'s "nothing matched" *and* one of its failure codes.
        # A diagnostic alongside it means the probe failed, so with no working
        # `ss` behind it occupancy is unknown -- not confirmed empty.
        def command_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
            if args[:1] == ["lsof"]:
                return subprocess.CompletedProcess(
                    [], 1, stdout="", stderr="lsof: no pwd entry for UID 501\n"
                )
            raise OSError("no ss here")

        inventory = lane_status.local_listener_inventory(command_runner)

        self.assertEqual((inventory["available"], inventory["listeners"]), (False, []))

    def test_an_lsof_warning_is_a_caveat_and_still_answers(self) -> None:
        # `lsof` warns about filesystems it could not stat while still answering
        # completely about TCP listeners, and it emits those warnings with the
        # same exit 1. Treating a warning as a failure would make every host
        # with an unreadable mount report its ports unknowable.
        def command_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
            if args[:1] == ["lsof"]:
                return subprocess.CompletedProcess(
                    [], 1, stdout="", stderr="lsof: WARNING: can't stat() nfs file system /net\n"
                )
            raise OSError("no ss here")

        inventory = lane_status.local_listener_inventory(command_runner)

        self.assertEqual((inventory["available"], inventory["listeners"]), (True, []))

    def test_an_lsof_error_falls_through_to_a_working_ss(self) -> None:
        # A failed `lsof` is not the end of the inventory: the fallback still
        # gets asked, and its answer is the one that counts.
        def command_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
            if args[:1] == ["lsof"]:
                return subprocess.CompletedProcess([], 1, stdout="", stderr="lsof: internal error\n")
            return _completed(
                'LISTEN 0 4096 127.0.0.1:5332 0.0.0.0:* users:(("code-mower",pid=900,fd=7))\n'
            )

        inventory = lane_status.local_listener_inventory(command_runner)

        self.assertTrue(inventory["available"])
        self.assertEqual([row["port"] for row in inventory["listeners"]], [5332])

    def test_neither_tool_answering_leaves_occupancy_unknown(self) -> None:
        # A timeout and a crash are not "nothing is listening": the same empty
        # list has to arrive marked unavailable so no caller reads it as free.
        def command_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
            if args[:1] == ["lsof"]:
                raise subprocess.TimeoutExpired(args, 3)
            return _completed("", returncode=127)

        inventory = lane_status.local_listener_inventory(command_runner)

        self.assertEqual((inventory["available"], inventory["listeners"]), (False, []))

    def test_pr_with_no_code_mower_markers_is_unmanaged(self) -> None:
        def gh_json(args: list[str]) -> object:
            if args[:2] == ["pr", "list"]:
                return [
                    {
                        "number": 42,
                        "title": "Regular human PR",
                        "url": "https://github.com/owner/repo/pull/42",
                        "headRefName": "feature/my-work",
                        "headRefOid": "abcdef0123456789abcdef0123456789abcdef01",
                        "author": {"login": "human-contributor"},
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
                        "labels": [],
                        "statusCheckRollup": [],
                    }
                ]
            if args[:2] == ["run", "list"]:
                return []
            if args[0] == "api" and "/comments?" in args[1]:
                return []
            raise lane_status.LaneStatusUnavailable("unexpected gh call")

        report = lane_status.collect_status(
            lineage_config=policy({}),
            repo="owner/repo",
            gh_json_runner=gh_json,
            command_runner=lambda _args: _completed(""),
            now=NOW,
        )

        pr = report["remote"]["pull_requests"][0]
        self.assertEqual(pr["lineage"]["status"], "unmanaged")
        self.assertEqual(pr["lineage"]["reason"], "no_code_mower_provenance")
        self.assertNotEqual(pr["next_action"], "owner action required")

    def test_dependabot_pr_is_unmanaged(self) -> None:
        def gh_json(args: list[str]) -> object:
            if args[:2] == ["pr", "list"]:
                return [
                    {
                        "number": 99,
                        "title": "Bump dependency version",
                        "url": "https://github.com/owner/repo/pull/99",
                        "headRefName": "dependabot/npm_and_yarn/deps-1234",
                        "headRefOid": "abcdef0123456789abcdef0123456789abcdef01",
                        "author": {"login": "dependabot[bot]"},
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
                        "labels": [{"name": "dependencies"}],
                        "statusCheckRollup": [],
                    }
                ]
            if args[:2] == ["run", "list"]:
                return []
            if args[0] == "api" and "/comments?" in args[1]:
                return []
            raise lane_status.LaneStatusUnavailable("unexpected gh call")

        report = lane_status.collect_status(
            lineage_config=policy({}),
            repo="owner/repo",
            gh_json_runner=gh_json,
            command_runner=lambda _args: _completed(""),
            now=NOW,
        )

        pr = report["remote"]["pull_requests"][0]
        self.assertEqual(pr["lineage"]["status"], "unmanaged")
        self.assertEqual(pr["lineage"]["reason"], "no_code_mower_provenance")
        self.assertNotEqual(pr["next_action"], "owner action required")

    def test_ordinary_pr_with_unavailable_history_is_unmanaged(self) -> None:
        def gh_json(args: list[str]) -> object:
            if args[:2] == ["pr", "list"]:
                return [
                    {
                        "number": 77,
                        "title": "Regular PR with network issues",
                        "url": "https://github.com/owner/repo/pull/77",
                        "headRefName": "feature/work",
                        "headRefOid": "abcdef0123456789abcdef0123456789abcdef01",
                        "author": {"login": "contributor"},
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
                        "labels": [],
                        "statusCheckRollup": [],
                    }
                ]
            if args[:2] == ["run", "list"]:
                return []
            if args[0] == "api" and "/comments?" in args[1]:
                raise lane_status.LaneStatusUnavailable("Network timeout")
            raise lane_status.LaneStatusUnavailable("unexpected gh call")

        report = lane_status.collect_status(
            lineage_config=policy({}),
            repo="owner/repo",
            gh_json_runner=gh_json,
            command_runner=lambda _args: _completed(""),
            now=NOW,
        )

        pr = report["remote"]["pull_requests"][0]
        self.assertEqual(pr["lineage"]["status"], "unmanaged")
        self.assertEqual(pr["lineage"]["reason"], "no_code_mower_provenance")
        self.assertNotEqual(pr["next_action"], "owner action required")

    def test_claimed_pr_with_unavailable_history_is_actionable(self) -> None:
        def gh_json(args: list[str]) -> object:
            if args[:2] == ["pr", "list"]:
                return [
                    {
                        "number": 88,
                        "title": "Code Mower PR with network issues",
                        "url": "https://github.com/owner/repo/pull/88",
                        "headRefName": "codex/work",
                        "headRefOid": "abcdef0123456789abcdef0123456789abcdef01",
                        "author": {"login": "source-bot"},
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
                        "labels": [{"name": "builder:codex"}],
                        "statusCheckRollup": [],
                    }
                ]
            if args[:2] == ["run", "list"]:
                return []
            if args[0] == "api" and "/comments?" in args[1]:
                raise lane_status.LaneStatusUnavailable("Network timeout")
            raise lane_status.LaneStatusUnavailable("unexpected gh call")

        report = lane_status.collect_status(
            lineage_config=policy({}),
            repo="owner/repo",
            gh_json_runner=gh_json,
            command_runner=lambda _args: _completed(""),
            now=NOW,
        )

        pr = report["remote"]["pull_requests"][0]
        self.assertEqual(pr["lineage"]["status"], "unavailable")
        self.assertEqual(pr["lineage"]["reason"], "lineage_unreadable")
        self.assertEqual(pr["next_action"], "restore readable lineage metadata and rerun status")

    def test_pr_with_generic_package_check_is_unmanaged(self) -> None:
        def gh_json(args: list[str]) -> object:
            if args[:2] == ["pr", "list"]:
                return [
                    {
                        "number": 111,
                        "title": "Normal PR with package check",
                        "url": "https://github.com/owner/repo/pull/111",
                        "headRefName": "fix/bug",
                        "headRefOid": "abcdef0123456789abcdef0123456789abcdef01",
                        "author": {"login": "developer"},
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
                        "labels": [],
                        "statusCheckRollup": [
                            {
                                "__typename": "CheckRun",
                                "name": "package / build",
                                "conclusion": "SUCCESS",
                            }
                        ],
                    }
                ]
            if args[:2] == ["run", "list"]:
                return []
            if args[0] == "api" and "/comments?" in args[1]:
                return []
            raise lane_status.LaneStatusUnavailable("unexpected gh call")

        report = lane_status.collect_status(
            lineage_config=policy({}),
            repo="owner/repo",
            gh_json_runner=gh_json,
            command_runner=lambda _args: _completed(""),
            now=NOW,
        )

        pr = report["remote"]["pull_requests"][0]
        self.assertEqual(pr["lineage"]["status"], "unmanaged")
        self.assertEqual(pr["lineage"]["reason"], "no_code_mower_provenance")

    def test_pr_with_configured_dispatch_alias_is_managed(self) -> None:
        def gh_json(args: list[str]) -> object:
            if args[:2] == ["pr", "list"]:
                return [
                    {
                        "number": 222,
                        "title": "Grok bot PR",
                        "url": "https://github.com/owner/repo/pull/222",
                        "headRefName": "grok/work",
                        "headRefOid": "abcdef0123456789abcdef0123456789abcdef01",
                        "author": {"login": "grok-bot[bot]"},
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
                        "labels": [{"name": "builder:grok-bot"}],
                        "statusCheckRollup": [],
                    }
                ]
            if args[:2] == ["run", "list"]:
                return []
            if args[0] == "api" and "/comments?" in args[1]:
                raise RuntimeError("Simulated error")
            raise lane_status.LaneStatusUnavailable("unexpected gh call")

        config_with_grok = policy({})
        config_with_grok["builder_identity"]["labels"]["builder:grok-bot"] = "cursor"

        report = lane_status.collect_status(
            lineage_config=config_with_grok,
            repo="owner/repo",
            gh_json_runner=gh_json,
            command_runner=lambda _args: _completed(""),
            now=NOW,
        )

        pr = report["remote"]["pull_requests"][0]
        self.assertEqual(pr["lineage"]["status"], "unknown")
        self.assertEqual(pr["lineage"]["reason"], "lineage_unreadable")

    def test_pr_with_builder_label_but_unreadable_lineage_is_actionable(self) -> None:
        def gh_json(args: list[str]) -> object:
            if args[:2] == ["pr", "list"]:
                return [
                    {
                        "number": 55,
                        "title": "Malformed Code Mower PR",
                        "url": "https://github.com/owner/repo/pull/55",
                        "headRefName": "codex/work",
                        "headRefOid": "abcdef0123456789abcdef0123456789abcdef01",
                        "author": {"login": "source-bot"},
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
                        "labels": [{"name": "builder:codex"}],
                        "statusCheckRollup": [],
                    }
                ]
            if args[:2] == ["run", "list"]:
                return []
            if args[0] == "api" and "/comments?" in args[1]:
                raise RuntimeError("Simulated history fetch failure")
            raise lane_status.LaneStatusUnavailable("unexpected gh call")

        report = lane_status.collect_status(
            lineage_config=policy({}),
            repo="owner/repo",
            gh_json_runner=gh_json,
            command_runner=lambda _args: _completed(""),
            now=NOW,
        )

        pr = report["remote"]["pull_requests"][0]
        self.assertEqual(pr["lineage"]["status"], "unknown")
        self.assertEqual(pr["lineage"]["reason"], "lineage_unreadable")
        self.assertEqual(pr["next_action"], "owner action required")

    def test_pr_with_dispatch_label_is_managed(self) -> None:
        def gh_json(args: list[str]) -> object:
            if args[:2] == ["pr", "list"]:
                return [
                    {
                        "number": 333,
                        "title": "Dispatched work",
                        "url": "https://github.com/owner/repo/pull/333",
                        "headRefName": "feature/work",
                        "headRefOid": "abcdef0123456789abcdef0123456789abcdef01",
                        "author": {"login": "human-contributor"},
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
                        "labels": [{"name": "dispatched:codex"}],
                        "statusCheckRollup": [],
                    }
                ]
            if args[:2] == ["run", "list"]:
                return []
            if args[0] == "api" and "/comments?" in args[1]:
                raise RuntimeError("Simulated error")
            raise lane_status.LaneStatusUnavailable("unexpected gh call")

        config = policy()
        from code_mower import config as policy_config
        self.assertEqual(policy_config.validate_config(config), [])

        report = lane_status.collect_status(
            lineage_config=config,
            repo="owner/repo",
            gh_json_runner=gh_json,
            command_runner=lambda _args: _completed(""),
            now=NOW,
        )

        pr = report["remote"]["pull_requests"][0]
        self.assertEqual(pr["lineage"]["status"], "unknown")
        self.assertEqual(pr["lineage"]["reason"], "lineage_unreadable")

    def test_pr_with_dispatch_alias_is_managed(self) -> None:
        def gh_json(args: list[str]) -> object:
            if args[:2] == ["pr", "list"]:
                return [
                    {
                        "number": 444,
                        "title": "Legacy dispatch alias",
                        "url": "https://github.com/owner/repo/pull/444",
                        "headRefName": "feature/legacy",
                        "headRefOid": "abcdef0123456789abcdef0123456789abcdef01",
                        "author": {"login": "human"},
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
                        "labels": [{"name": "dispatched:grok-bot"}],
                        "statusCheckRollup": [],
                    }
                ]
            if args[:2] == ["run", "list"]:
                return []
            if args[0] == "api" and "/comments?" in args[1]:
                raise RuntimeError("Simulated error")
            raise lane_status.LaneStatusUnavailable("unexpected gh call")

        config = policy()
        config["builder_identity"]["labels"]["builder:grok-bot"] = "cursor"
        from code_mower import config as policy_config
        self.assertEqual(policy_config.validate_config(config), [])

        report = lane_status.collect_status(
            lineage_config=config,
            repo="owner/repo",
            gh_json_runner=gh_json,
            command_runner=lambda _args: _completed(""),
            now=NOW,
        )

        pr = report["remote"]["pull_requests"][0]
        self.assertEqual(pr["lineage"]["status"], "unknown")
        self.assertEqual(pr["lineage"]["reason"], "lineage_unreadable")

    def test_pr_with_audit_need_label_is_managed(self) -> None:
        def gh_json(args: list[str]) -> object:
            if args[:2] == ["pr", "list"]:
                return [
                    {
                        "number": 555,
                        "title": "Needs audit",
                        "url": "https://github.com/owner/repo/pull/555",
                        "headRefName": "feature/needs-audit",
                        "headRefOid": "abcdef0123456789abcdef0123456789abcdef01",
                        "author": {"login": "contributor"},
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
                        "labels": [{"name": "needs-codex-audit"}],
                        "statusCheckRollup": [],
                    }
                ]
            if args[:2] == ["run", "list"]:
                return []
            if args[0] == "api" and "/comments?" in args[1]:
                raise RuntimeError("Simulated error")
            raise lane_status.LaneStatusUnavailable("unexpected gh call")

        config_with_audit = policy()
        config_with_audit["lanes"] = {
            "codex": {
                "type": "audit",
                "driver": "local_cli",
                "provider": "codex",
                "labels": {
                    "needs": "needs-codex-audit",
                    "done": "codex-audit-done",
                    "blocked": "codex-audit-blocked"
                }
            }
        }
        from code_mower import config as policy_config
        self.assertEqual(policy_config.validate_config(config_with_audit), [])

        report = lane_status.collect_status(
            lineage_config=config_with_audit,
            repo="owner/repo",
            gh_json_runner=gh_json,
            command_runner=lambda _args: _completed(""),
            now=NOW,
        )

        pr = report["remote"]["pull_requests"][0]
        self.assertEqual(pr["lineage"]["status"], "unknown")
        self.assertEqual(pr["lineage"]["reason"], "lineage_unreadable")

    def test_pr_with_generic_audit_label_is_unmanaged(self) -> None:
        def gh_json(args: list[str]) -> object:
            if args[:2] == ["pr", "list"]:
                return [
                    {
                        "number": 666,
                        "title": "Security audit needed",
                        "url": "https://github.com/owner/repo/pull/666",
                        "headRefName": "feature/security",
                        "headRefOid": "abcdef0123456789abcdef0123456789abcdef01",
                        "author": {"login": "developer"},
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
                        "labels": [{"name": "needs-security-audit"}],
                        "statusCheckRollup": [],
                    }
                ]
            if args[:2] == ["run", "list"]:
                return []
            if args[0] == "api" and "/comments?" in args[1]:
                return []
            raise lane_status.LaneStatusUnavailable("unexpected gh call")

        config_with_codex_audit = policy()
        config_with_codex_audit["lanes"] = {
            "codex": {
                "type": "audit",
                "driver": "local_cli",
                "provider": "codex",
                "labels": {
                    "needs": "needs-codex-audit",
                    "done": "codex-audit-done",
                    "blocked": "codex-audit-blocked"
                }
            }
        }
        from code_mower import config as policy_config
        self.assertEqual(policy_config.validate_config(config_with_codex_audit), [])

        report = lane_status.collect_status(
            lineage_config=config_with_codex_audit,
            repo="owner/repo",
            gh_json_runner=gh_json,
            command_runner=lambda _args: _completed(""),
            now=NOW,
        )

        pr = report["remote"]["pull_requests"][0]
        self.assertEqual(pr["lineage"]["status"], "unmanaged")
        self.assertEqual(pr["lineage"]["reason"], "no_code_mower_provenance")

    def test_configured_author_recognized_when_exclusion_disabled(self) -> None:
        def gh_json(args: list[str]) -> object:
            if args[:2] == ["pr", "list"]:
                return [
                    {
                        "number": 777,
                        "title": "Bot PR with exclusion disabled",
                        "url": "https://github.com/owner/repo/pull/777",
                        "headRefName": "bot/work",
                        "headRefOid": "abcdef0123456789abcdef0123456789abcdef01",
                        "author": {"login": "source-bot"},
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
                        "labels": [],
                        "statusCheckRollup": [],
                    }
                ]
            if args[:2] == ["run", "list"]:
                return []
            if args[0] == "api" and "/comments?" in args[1]:
                raise RuntimeError("Simulated error")
            raise lane_status.LaneStatusUnavailable("unexpected gh call")

        config_no_exclusion = policy()
        config_no_exclusion["merge_authority_excludes_author"] = False

        report = lane_status.collect_status(
            lineage_config=config_no_exclusion,
            repo="owner/repo",
            gh_json_runner=gh_json,
            command_runner=lambda _args: _completed(""),
            now=NOW,
        )

        pr = report["remote"]["pull_requests"][0]
        self.assertEqual(pr["lineage"]["status"], "unknown")
        self.assertEqual(pr["lineage"]["reason"], "lineage_unreadable")

    def test_configured_prefix_recognized_when_exclusion_disabled(self) -> None:
        def gh_json(args: list[str]) -> object:
            if args[:2] == ["pr", "list"]:
                return [
                    {
                        "number": 888,
                        "title": "Branch prefix with exclusion disabled",
                        "url": "https://github.com/owner/repo/pull/888",
                        "headRefName": "codex/prefix-work",
                        "headRefOid": "abcdef0123456789abcdef0123456789abcdef01",
                        "author": {"login": "human"},
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
                        "labels": [],
                        "statusCheckRollup": [],
                    }
                ]
            if args[:2] == ["run", "list"]:
                return []
            if args[0] == "api" and "/comments?" in args[1]:
                raise RuntimeError("Simulated error")
            raise lane_status.LaneStatusUnavailable("unexpected gh call")

        config_no_exclusion = policy()
        config_no_exclusion["merge_authority_excludes_author"] = False

        report = lane_status.collect_status(
            lineage_config=config_no_exclusion,
            repo="owner/repo",
            gh_json_runner=gh_json,
            command_runner=lambda _args: _completed(""),
            now=NOW,
        )

        pr = report["remote"]["pull_requests"][0]
        self.assertEqual(pr["lineage"]["status"], "unknown")
        self.assertEqual(pr["lineage"]["reason"], "lineage_unreadable")

    def test_non_builder_identity_label_does_not_generate_dispatch_claim(self) -> None:
        def gh_json(args: list[str]) -> object:
            if args[:2] == ["pr", "list"]:
                return [
                    {
                        "number": 999,
                        "title": "Custom identity PR",
                        "url": "https://github.com/owner/repo/pull/999",
                        "headRefName": "feature/custom",
                        "headRefOid": "abcdef0123456789abcdef0123456789abcdef01",
                        "author": {"login": "developer"},
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
                        "labels": [{"name": "dispatched:custom-bot"}],
                        "statusCheckRollup": [],
                    }
                ]
            if args[:2] == ["run", "list"]:
                return []
            if args[0] == "api" and "/comments?" in args[1]:
                return []
            raise lane_status.LaneStatusUnavailable("unexpected gh call")

        config = policy()
        config["builder_identity"]["labels"]["identity:custom-bot"] = "custom"
        from code_mower import config as policy_config
        self.assertEqual(policy_config.validate_config(config), [])

        report = lane_status.collect_status(
            lineage_config=config,
            repo="owner/repo",
            gh_json_runner=gh_json,
            command_runner=lambda _args: _completed(""),
            now=NOW,
        )

        pr = report["remote"]["pull_requests"][0]
        self.assertEqual(pr["lineage"]["status"], "unmanaged")
        self.assertEqual(pr["lineage"]["reason"], "no_code_mower_provenance")

    def test_pr_with_malformed_lineage_marker_remains_actionable(self) -> None:
        def gh_json(args: list[str]) -> object:
            if args[:2] == ["pr", "list"]:
                return [
                    {
                        "number": 888,
                        "title": "PR with malformed lineage",
                        "url": "https://github.com/owner/repo/pull/888",
                        "headRefName": "feature/malformed",
                        "headRefOid": "badc0ffeebadc0ffeebadc0ffeebadc0ffeebadc",
                        "author": {"login": "developer"},
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
                        "labels": [],
                        "statusCheckRollup": [],
                    }
                ]
            if args[:2] == ["run", "list"]:
                return []
            if args[0] == "api" and "/comments?" in args[1]:
                return [
                    {
                        "user": {"login": "lineage-publisher[bot]"},
                        "body": "<!-- CODE_MOWER_BUILDER_LINEAGE: {malformed json} -->",
                        "created_at": NOW.isoformat(),
                    }
                ]
            raise lane_status.LaneStatusUnavailable("unexpected gh call")

        config = policy()
        from code_mower import config as policy_config
        self.assertEqual(policy_config.validate_config(config), [])

        report = lane_status.collect_status(
            lineage_config=config,
            repo="owner/repo",
            gh_json_runner=gh_json,
            command_runner=lambda _args: _completed(""),
            now=NOW,
        )

        pr = report["remote"]["pull_requests"][0]
        self.assertEqual(pr["lineage"]["status"], "unknown")
        self.assertEqual(pr["lineage"]["reason"], "lineage_unreadable")

    def test_pr_with_conflicting_lineage_marker_remains_actionable(self) -> None:
        def gh_json(args: list[str]) -> object:
            if args[:2] == ["pr", "list"]:
                return [
                    {
                        "number": 777,
                        "title": "PR with conflicting lineage",
                        "url": "https://github.com/owner/repo/pull/777",
                        "headRefName": "feature/conflict",
                        "headRefOid": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                        "author": {"login": "developer"},
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
                        "labels": [],
                        "statusCheckRollup": [],
                    }
                ]
            if args[:2] == ["run", "list"]:
                return []
            if args[0] == "api" and "/comments?" in args[1]:
                return [
                    {
                        "user": {"login": "lineage-publisher[bot]"},
                        "body": '<!-- CODE_MOWER_BUILDER_LINEAGE: {"schema":"code_mower.builderLineage.v1","episodes":[{"sequence":1,"source_lane":"cursor","destination_lane":"cursor","head_sha":"deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"},{"sequence":1,"source_lane":"codex","destination_lane":"codex","head_sha":"differentsha0000000000000000000000000"}]} -->',
                        "created_at": NOW.isoformat(),
                    }
                ]
            raise lane_status.LaneStatusUnavailable("unexpected gh call")

        config = policy()
        from code_mower import config as policy_config
        self.assertEqual(policy_config.validate_config(config), [])

        report = lane_status.collect_status(
            lineage_config=config,
            repo="owner/repo",
            gh_json_runner=gh_json,
            command_runner=lambda _args: _completed(""),
            now=NOW,
        )

        pr = report["remote"]["pull_requests"][0]
        self.assertEqual(pr["lineage"]["status"], "unknown")
        self.assertEqual(pr["lineage"]["reason"], "lineage_unreadable")

    def test_pr_with_untrusted_lineage_marker_remains_unmanaged(self) -> None:
        def gh_json(args: list[str]) -> object:
            if args[:2] == ["pr", "list"]:
                return [
                    {
                        "number": 666,
                        "title": "PR with untrusted marker",
                        "url": "https://github.com/owner/repo/pull/666",
                        "headRefName": "feature/untrusted",
                        "headRefOid": "abcdef0123456789abcdef0123456789abcdef99",
                        "author": {"login": "random-user"},
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
                        "labels": [],
                        "statusCheckRollup": [],
                    }
                ]
            if args[:2] == ["run", "list"]:
                return []
            if args[0] == "api" and "/comments?" in args[1]:
                return [
                    {
                        "id": 1,
                        "user": {"login": "untrusted-user"},
                        "body": "<!-- CODE_MOWER_BUILDER_LINEAGE: malicious marker -->",
                        "created_at": NOW.isoformat(),
                    }
                ]
            raise lane_status.LaneStatusUnavailable("unexpected gh call")

        config = policy()
        from code_mower import config as policy_config
        self.assertEqual(policy_config.validate_config(config), [])

        report = lane_status.collect_status(
            lineage_config=config,
            repo="owner/repo",
            gh_json_runner=gh_json,
            command_runner=lambda _args: _completed(""),
            now=NOW,
        )

        pr = report["remote"]["pull_requests"][0]
        self.assertEqual(pr["lineage"]["status"], "unmanaged")
        self.assertEqual(pr["lineage"]["reason"], "no_code_mower_provenance")

    def test_pr_with_invalid_policy_remains_actionable(self) -> None:
        def gh_json(args: list[str]) -> object:
            if args[:2] == ["pr", "list"]:
                return [
                    {
                        "number": 555,
                        "title": "PR with invalid policy",
                        "url": "https://github.com/owner/repo/pull/555",
                        "headRefName": "feature/test",
                        "headRefOid": "abcdef0123456789abcdef0123456789abcdef01",
                        "author": {"login": "developer"},
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
                        "labels": [],
                        "statusCheckRollup": [],
                    }
                ]
            if args[:2] == ["run", "list"]:
                return []
            if args[0] == "api" and "/comments?" in args[1]:
                return []
            raise lane_status.LaneStatusUnavailable("unexpected gh call")

        invalid_config = {"version": "invalid", "project": {}}

        report = lane_status.collect_status(
            lineage_config=invalid_config,
            repo="owner/repo",
            gh_json_runner=gh_json,
            command_runner=lambda _args: _completed(""),
            now=NOW,
        )

        pr = report["remote"]["pull_requests"][0]
        self.assertEqual(pr["lineage"]["status"], "unknown")
        self.assertEqual(pr["lineage"]["reason"], "lineage_unreadable")

    def test_pr_with_malformed_labels_remains_actionable(self) -> None:
        def gh_json(args: list[str]) -> object:
            if args[:2] == ["pr", "list"]:
                return [
                    {
                        "number": 444,
                        "title": "PR with malformed labels",
                        "url": "https://github.com/owner/repo/pull/444",
                        "headRefName": "feature/test",
                        "headRefOid": "abcdef0123456789abcdef0123456789abcdef01",
                        "author": {"login": "developer"},
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
                        "labels": ["not-a-dict", {"wrong": "structure"}],
                        "statusCheckRollup": [],
                    }
                ]
            if args[:2] == ["run", "list"]:
                return []
            if args[0] == "api" and "/comments?" in args[1]:
                return []
            raise lane_status.LaneStatusUnavailable("unexpected gh call")

        config = policy()
        from code_mower import config as policy_config
        self.assertEqual(policy_config.validate_config(config), [])

        report = lane_status.collect_status(
            lineage_config=config,
            repo="owner/repo",
            gh_json_runner=gh_json,
            command_runner=lambda _args: _completed(""),
            now=NOW,
        )

        pr = report["remote"]["pull_requests"][0]
        self.assertEqual(pr["lineage"]["status"], "unknown")
        self.assertEqual(pr["lineage"]["reason"], "lineage_unreadable")

    def test_pr_with_malformed_author_remains_actionable(self) -> None:
        def gh_json(args: list[str]) -> object:
            if args[:2] == ["pr", "list"]:
                return [
                    {
                        "number": 333,
                        "title": "PR with malformed author",
                        "url": "https://github.com/owner/repo/pull/333",
                        "headRefName": "feature/test",
                        "headRefOid": "abcdef0123456789abcdef0123456789abcdef01",
                        "author": {"login": 12345},
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
                        "labels": [],
                        "statusCheckRollup": [],
                    }
                ]
            if args[:2] == ["run", "list"]:
                return []
            if args[0] == "api" and "/comments?" in args[1]:
                return []
            raise lane_status.LaneStatusUnavailable("unexpected gh call")

        config = policy()
        from code_mower import config as policy_config
        self.assertEqual(policy_config.validate_config(config), [])

        report = lane_status.collect_status(
            lineage_config=config,
            repo="owner/repo",
            gh_json_runner=gh_json,
            command_runner=lambda _args: _completed(""),
            now=NOW,
        )

        pr = report["remote"]["pull_requests"][0]
        self.assertEqual(pr["lineage"]["status"], "unknown")
        self.assertEqual(pr["lineage"]["reason"], "lineage_unreadable")

    def test_pr_with_malformed_visible_metadata_remains_actionable(self) -> None:
        base_pr = {
            "number": 334,
            "title": "PR with malformed identity metadata",
            "url": "https://github.com/owner/repo/pull/334",
            "headRefName": "feature/test",
            "headRefOid": "abcdef0123456789abcdef0123456789abcdef01",
            "author": {"login": "developer"},
            "isDraft": False,
            "mergeStateStatus": "CLEAN",
            "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
            "labels": [],
            "statusCheckRollup": [],
        }

        malformed_cases = (
            ("labels collection", {"labels": {"name": "not-a-list"}}),
            ("empty label", {"labels": [{"name": " "}]}),
            ("author login", {"author": {"login": " "}}),
            ("branch", {"headRefName": 123}),
            ("head SHA", {"headRefOid": 123}),
            ("PR number", {"number": "334"}),
        )
        for case, overrides in malformed_cases:
            with self.subTest(case=case):
                raw_pr = {**base_pr, **overrides}

                def gh_json(args: list[str], raw_pr: dict[str, object] = raw_pr) -> object:
                    if args[:2] == ["pr", "list"]:
                        return [raw_pr]
                    if args[:2] == ["run", "list"]:
                        return []
                    if args[0] == "api" and "/comments?" in args[1]:
                        return []
                    raise lane_status.LaneStatusUnavailable("unexpected gh call")

                report = lane_status.collect_status(
                    lineage_config=policy(),
                    repo="owner/repo",
                    gh_json_runner=gh_json,
                    command_runner=lambda _args: _completed(""),
                    now=NOW,
                )

                pr = report["remote"]["pull_requests"][0]
                self.assertEqual(pr["lineage"]["status"], "unknown")
                self.assertEqual(pr["lineage"]["reason"], "lineage_unreadable")
                self.assertEqual(pr["next_action"], "owner action required")

    def test_code_mower_claim_uses_all_raw_checks_not_bounded_projection(self) -> None:
        unrelated_checks = [
            {"__typename": "CheckRun", "name": f"package / shard-{index}", "conclusion": "SUCCESS"}
            for index in range(8)
        ]

        def gh_json(args: list[str]) -> object:
            if args[:2] == ["pr", "list"]:
                return [
                    {
                        "number": 335,
                        "title": "Claim after display limit",
                        "url": "https://github.com/owner/repo/pull/335",
                        "headRefName": "feature/test",
                        "headRefOid": "abcdef0123456789abcdef0123456789abcdef01",
                        "author": {"login": "developer"},
                        "isDraft": False,
                        "mergeStateStatus": "CLEAN",
                        "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
                        "labels": [],
                        "statusCheckRollup": [
                            *unrelated_checks,
                            {"__typename": "CheckRun", "name": "code-mower/gate", "conclusion": "SUCCESS"},
                        ],
                    }
                ]
            if args[:2] == ["run", "list"]:
                return []
            if args[0] == "api" and "/comments?" in args[1]:
                raise lane_status.LaneStatusUnavailable("history unavailable")
            raise lane_status.LaneStatusUnavailable("unexpected gh call")

        report = lane_status.collect_status(
            lineage_config=policy(),
            repo="owner/repo",
            gh_json_runner=gh_json,
            command_runner=lambda _args: _completed(""),
            now=NOW,
        )

        pr = report["remote"]["pull_requests"][0]
        self.assertEqual(len(pr["checks"]), 8)
        self.assertNotIn("code-mower/gate", {check["name"] for check in pr["checks"]})
        self.assertEqual(pr["lineage"]["status"], "unavailable")
        self.assertEqual(pr["lineage"]["reason"], "lineage_unreadable")

    def test_exact_check_identity_namespace_controls_provenance_claim(self) -> None:
        cases = (
            ("name", "code-mower/gate", True),
            ("context", " CODE_MOWER/GATE ", True),
            ("workflowName", "Code Mower CI", True),
            ("workflowName", "code_mower local audit request", True),
            ("name", "code-mower", True),
            ("name", "not-code-mower/gate", False),
            ("context", "code-mower-simulator", False),
            ("workflowName", "third-party code-mower compatibility", False),
            ("name", "code_mower_simulator", False),
            ("context", "code-mowerish/gate", False),
            ("workflowName", "Not Code Mower CI", False),
        )

        for field, value, expected in cases:
            with self.subTest(field=field, value=value):
                self.assertEqual(
                    lane_status._has_code_mower_check_claim([{field: value}]),
                    expected,
                )

    def test_check_identity_lookalikes_remain_unmanaged_for_all_history_states(self) -> None:
        lookalikes = (
            {"__typename": "CheckRun", "name": "not-code-mower/gate"},
            {"__typename": "StatusContext", "context": "code-mower-simulator"},
            {
                "__typename": "CheckRun",
                "name": "package",
                "workflowName": "third-party code-mower compatibility",
            },
        )

        for raw_check in lookalikes:
            for history_available in (True, False):
                with self.subTest(raw_check=raw_check, history_available=history_available):
                    def gh_json(
                        args: list[str],
                        raw_check: dict[str, object] = raw_check,
                        history_available: bool = history_available,
                    ) -> object:
                        if args[:2] == ["pr", "list"]:
                            return [
                                {
                                    "number": 340,
                                    "title": "Ordinary PR with unrelated check identity",
                                    "url": "https://github.com/owner/repo/pull/340",
                                    "headRefName": "feature/test",
                                    "headRefOid": "abcdef0123456789abcdef0123456789abcdef01",
                                    "author": {"login": "developer"},
                                    "isDraft": False,
                                    "mergeStateStatus": "CLEAN",
                                    "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
                                    "labels": [],
                                    "statusCheckRollup": [raw_check],
                                }
                            ]
                        if args[:2] == ["run", "list"]:
                            return []
                        if args[0] == "api" and "/comments?" in args[1]:
                            if history_available:
                                return []
                            raise lane_status.LaneStatusUnavailable("history unavailable")
                        raise lane_status.LaneStatusUnavailable("unexpected gh call")

                    report = lane_status.collect_status(
                        lineage_config=policy(),
                        repo="owner/repo",
                        gh_json_runner=gh_json,
                        command_runner=lambda _args: _completed(""),
                        now=NOW,
                    )

                    pr = report["remote"]["pull_requests"][0]
                    self.assertEqual(pr["lineage"]["status"], "unmanaged")
                    self.assertEqual(pr["lineage"]["reason"], "no_code_mower_provenance")

    def test_malformed_check_collection_or_identity_remains_actionable(self) -> None:
        malformed_rollups = (
            {"name": "code-mower/gate"},
            ["not-a-check-mapping"],
            [{"__typename": "CheckRun", "name": 123, "conclusion": "SUCCESS"}],
            [{"__typename": "StatusContext", "context": 123, "state": "SUCCESS"}],
            [{"__typename": "UnknownCheck", "name": "package", "conclusion": "SUCCESS"}],
            [{"__typename": "CheckRun", "name": "package", "app": "github-actions"}],
            [{"name": "package", "context": "external-ci"}],
            [{"__typename": "CheckRun", "name": "package", "app": {}}],
            [{"__typename": "CheckRun", "name": "package", "app": {"slug": ""}}],
            [{"__typename": "CheckRun", "name": "package", "app": {"owner": 42}}],
        )

        for raw_checks in malformed_rollups:
            with self.subTest(raw_checks=raw_checks):
                def gh_json(args: list[str], raw_checks: object = raw_checks) -> object:
                    if args[:2] == ["pr", "list"]:
                        return [
                            {
                                "number": 336,
                                "title": "PR with malformed checks",
                                "url": "https://github.com/owner/repo/pull/336",
                                "headRefName": "feature/test",
                                "headRefOid": "abcdef0123456789abcdef0123456789abcdef01",
                                "author": {"login": "developer"},
                                "isDraft": False,
                                "mergeStateStatus": "CLEAN",
                                "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
                                "labels": [],
                                "statusCheckRollup": raw_checks,
                            }
                        ]
                    if args[:2] == ["run", "list"]:
                        return []
                    if args[0] == "api" and "/comments?" in args[1]:
                        return []
                    raise lane_status.LaneStatusUnavailable("unexpected gh call")

                report = lane_status.collect_status(
                    lineage_config=policy(),
                    repo="owner/repo",
                    gh_json_runner=gh_json,
                    command_runner=lambda _args: _completed(""),
                    now=NOW,
                )

                pr = report["remote"]["pull_requests"][0]
                self.assertEqual(pr["lineage"]["status"], "unknown")
                self.assertEqual(pr["lineage"]["reason"], "lineage_unreadable")
                self.assertEqual(pr["next_action"], "owner action required")

    def test_valid_empty_and_unrelated_check_variants_remain_unmanaged(self) -> None:
        valid_rollups = (
            [],
            [
                {
                    "__typename": "CheckRun",
                    "name": "package",
                    "workflowName": "quality",
                    "detailsUrl": "https://github.com/owner/repo/actions/runs/1",
                    "startedAt": "2026-09-01T11:40:00Z",
                    "completedAt": "2026-09-01T11:41:00Z",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                    "app": {"slug": "github-actions", "name": "GitHub Actions", "databaseId": 15368},
                },
                {
                    "__typename": "StatusContext",
                    "context": "external-ci",
                    "targetUrl": "https://ci.example.test/build/1",
                    "startedAt": "2026-09-01T11:40:00Z",
                    "state": "SUCCESS",
                },
            ],
        )

        for raw_checks in valid_rollups:
            with self.subTest(raw_checks=raw_checks):
                def gh_json(args: list[str], raw_checks: list[dict[str, object]] = raw_checks) -> object:
                    if args[:2] == ["pr", "list"]:
                        return [
                            {
                                "number": 337,
                                "title": "Ordinary PR with readable checks",
                                "url": "https://github.com/owner/repo/pull/337",
                                "headRefName": "feature/test",
                                "headRefOid": "abcdef0123456789abcdef0123456789abcdef01",
                                "author": {"login": "developer"},
                                "isDraft": False,
                                "mergeStateStatus": "CLEAN",
                                "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
                                "labels": [],
                                "statusCheckRollup": raw_checks,
                            }
                        ]
                    if args[:2] == ["run", "list"]:
                        return []
                    if args[0] == "api" and "/comments?" in args[1]:
                        return []
                    raise lane_status.LaneStatusUnavailable("unexpected gh call")

                report = lane_status.collect_status(
                    lineage_config=policy(),
                    repo="owner/repo",
                    gh_json_runner=gh_json,
                    command_runner=lambda _args: _completed(""),
                    now=NOW,
                )

                pr = report["remote"]["pull_requests"][0]
                self.assertEqual(pr["lineage"]["status"], "unmanaged")
                self.assertEqual(pr["lineage"]["reason"], "no_code_mower_provenance")

    def test_status_check_union_and_app_identity_matrix(self) -> None:
        valid_rollups = (
            [],
            [{"__typename": "CheckRun", "name": "package"}],
            [{"__typename": "StatusContext", "context": "external-ci"}],
            [{"name": "package"}],
            [{"context": "external-ci"}],
            [{"__typename": "CheckRun", "name": "gate", "app": {"slug": "code-mower"}}],
            [{"__typename": "CheckRun", "name": "gate", "app": {"name": "Code Mower"}}],
            [{
                "__typename": "CheckRun",
                "name": "package",
                "app": {"slug": "github-actions", "name": "GitHub Actions"},
            }],
            [{"__typename": "CheckRun", "name": "package", "app": {"databaseId": 15368}}],
        )
        invalid_rollups = (
            None,
            {},
            [None],
            [{"name": "package", "context": "external-ci"}],
            [{"__typename": "CheckRun", "name": "package", "context": "external-ci"}],
            [{"__typename": "CheckRun", "context": "external-ci"}],
            [{"__typename": "CheckRun", "name": "package", "targetUrl": "https://ci.example.test"}],
            [{"__typename": "CheckRun", "name": "package", "state": "SUCCESS"}],
            [{"__typename": "StatusContext", "name": "package"}],
            [{"__typename": "StatusContext", "name": "package", "context": "external-ci"}],
            [{"__typename": "StatusContext", "context": "external-ci", "workflowName": "quality"}],
            [{"__typename": "StatusContext", "context": "external-ci", "detailsUrl": "https://ci.example.test"}],
            [{"__typename": "StatusContext", "context": "external-ci", "app": {"slug": "code-mower"}}],
            [{"context": "external-ci", "workflowName": "quality"}],
            [{"__typename": "Other", "name": "package"}],
            [{"__typename": "CheckRun", "name": "package", "app": {}}],
            [{"__typename": "CheckRun", "name": "package", "app": {"owner": 42}}],
            [{"__typename": "CheckRun", "name": "package", "app": {"slug": ""}}],
            [{"__typename": "CheckRun", "name": "package", "app": {"slug": "---"}}],
            [{"__typename": "CheckRun", "name": "package", "app": {"name": None}}],
            [{"__typename": "CheckRun", "name": "package", "app": {"databaseId": 0}}],
            [{"__typename": "CheckRun", "name": "package", "app": {"databaseId": False}}],
            [{
                "__typename": "CheckRun",
                "name": "gate",
                "app": {"slug": "github-actions", "name": "Code Mower"},
            }],
        )

        for rollup in valid_rollups:
            with self.subTest(valid=rollup):
                self.assertTrue(lane_status._status_check_rollup_is_readable(rollup))
        for rollup in invalid_rollups:
            with self.subTest(invalid=rollup):
                self.assertFalse(lane_status._status_check_rollup_is_readable(rollup))

    def test_exact_app_identity_controls_provenance_claim(self) -> None:
        cases = (
            ({"slug": "code-mower"}, "unavailable"),
            ({"name": "Code Mower"}, "unavailable"),
            ({"slug": "CODE_MOWER", "name": "Code Mower"}, "unavailable"),
            ({"slug": "code.mower"}, "unavailable"),
            ({"slug": "not-code-mower"}, "unmanaged"),
            ({"slug": "code-mower-simulator"}, "unmanaged"),
            ({"name": "Third Party Code Mower"}, "unmanaged"),
            ({"slug": "github-actions", "name": "GitHub Actions"}, "unmanaged"),
            ({"slug": "github-actions", "name": "Code Mower"}, "unknown"),
            ({"slug": "code-mower", "name": "Code Mower Simulator"}, "unknown"),
        )

        for app, expected_status in cases:
            with self.subTest(app=app):
                def gh_json(args: list[str], app: dict[str, object] = app) -> object:
                    if args[:2] == ["pr", "list"]:
                        return [
                            {
                                "number": 338,
                                "title": "App identity check",
                                "url": "https://github.com/owner/repo/pull/338",
                                "headRefName": "feature/test",
                                "headRefOid": "abcdef0123456789abcdef0123456789abcdef01",
                                "author": {"login": "developer"},
                                "isDraft": False,
                                "mergeStateStatus": "CLEAN",
                                "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
                                "labels": [],
                                "statusCheckRollup": [
                                    {
                                        "__typename": "CheckRun",
                                        "name": "gate",
                                        "app": app,
                                        "conclusion": "SUCCESS",
                                    }
                                ],
                            }
                        ]
                    if args[:2] == ["run", "list"]:
                        return []
                    if args[0] == "api" and "/comments?" in args[1]:
                        raise lane_status.LaneStatusUnavailable("history unavailable")
                    raise lane_status.LaneStatusUnavailable("unexpected gh call")

                report = lane_status.collect_status(
                    lineage_config=policy(),
                    repo="owner/repo",
                    gh_json_runner=gh_json,
                    command_runner=lambda _args: _completed(""),
                    now=NOW,
                )

                pr = report["remote"]["pull_requests"][0]
                self.assertEqual(pr["lineage"]["status"], expected_status)
                self.assertEqual(pr["lineage"]["reason"], "lineage_unreadable" if expected_status != "unmanaged" else "no_code_mower_provenance")

    def test_malformed_comment_history_stays_unknown_and_actionable(self) -> None:
        malformed_pages = (
            {"comments": []},
            [None],
            [{"body": None, "user": {"login": "developer"}}],
            [{"body": "", "user": "developer"}],
            [{"body": "", "user": {"login": 123}}],
            [{
                "body": "",
                "user": {"login": "developer"},
                "author": {"login": "other-developer"},
            }],
        )

        for malformed_page in malformed_pages:
            with self.subTest(malformed_page=malformed_page):
                def gh_json(args: list[str], malformed_page: object = malformed_page) -> object:
                    if args[:2] == ["pr", "list"]:
                        return [
                            {
                                "number": 339,
                                "title": "Malformed comment history",
                                "url": "https://github.com/owner/repo/pull/339",
                                "headRefName": "feature/test",
                                "headRefOid": "abcdef0123456789abcdef0123456789abcdef01",
                                "author": {"login": "developer"},
                                "isDraft": False,
                                "mergeStateStatus": "CLEAN",
                                "updatedAt": NOW.isoformat().replace("+00:00", "Z"),
                                "labels": [],
                                "statusCheckRollup": [],
                            }
                        ]
                    if args[:2] == ["run", "list"]:
                        return []
                    if args[0] == "api" and "/comments?" in args[1]:
                        return malformed_page
                    raise lane_status.LaneStatusUnavailable("unexpected gh call")

                report = lane_status.collect_status(
                    lineage_config=policy(),
                    repo="owner/repo",
                    gh_json_runner=gh_json,
                    command_runner=lambda _args: _completed(""),
                    now=NOW,
                )

                pr = report["remote"]["pull_requests"][0]
                self.assertEqual(pr["lineage"]["status"], "unknown")
                self.assertEqual(pr["lineage"]["reason"], "lineage_unreadable")
                self.assertEqual(pr["next_action"], "owner action required")
