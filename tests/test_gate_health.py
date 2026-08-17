from __future__ import annotations

import contextlib
import io
import json
import subprocess
import unittest
from datetime import datetime, timezone

from code_mower.gate_health import (
    Alert,
    evaluate,
    fetch_per_pr,
    format_alert_comments,
    gh_api_list,
    recent_alert_keys,
)

NOW = datetime(2026, 8, 17, 5, 0, tzinfo=timezone.utc)
SHA = "a" * 40
LOCAL_AUDIT_WORKFLOW_PATH = ".github/workflows/local-cli-audit.yml"
LANES = [
    {
        "id": "codex",
        "needs": "needs-codex-audit",
        "done": "codex-audit-done",
        "blocked": "codex-audit-blocked",
    },
    {
        "id": "claude",
        "needs": "needs-claude-audit",
        "done": "claude-audit-done",
        "blocked": "claude-audit-blocked",
    },
]


def audit_check(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "name": "audit",
        "workflow_path": LOCAL_AUDIT_WORKFLOW_PATH,
        "status": "completed",
        "created_at": "2026-08-17T04:00:00Z",
    }
    base.update(overrides)
    return base


def audit_comment(kind: str, *, created_at: str) -> dict[str, object]:
    body = f"Head SHA: `{SHA}`\n"
    if kind == "unknown":
        body += "Could not validate a Claude structured verdict artifact.\n"
    elif kind == "stale":
        body += "Head SHA changed during review (`aaaaaaaa` -> `bbbbbbbb`).\n"
    elif kind == "terminal":
        body += "Claude Audit: PASS\n<!-- CLAUDE_AUDIT_STATE: claude-audit-done -->"
        return {
            "id": created_at,
            "created_at": created_at,
            "user": {"login": "trusted-audit-bot"},
            "body": body,
        }
    else:
        body += "Claude Audit: PASS\n"
    body += "<!-- CLAUDE_AUDIT_STATE: needs-claude-audit -->"
    return {
        "id": created_at,
        "created_at": created_at,
        "user": {"login": "trusted-audit-bot"},
        "body": body,
    }


def issue_comment(number: int, comment: dict[str, object]) -> dict[str, object]:
    return {
        **comment,
        "issue_url": f"https://api.github.com/repos/owner/repo/issues/{number}",
    }


def pr(labels: list[str]) -> dict[str, object]:
    return {
        "number": 9,
        "title": "Test PR",
        "headRefOid": SHA,
        "labels": [{"name": label} for label in labels],
    }


def evaluate_case(**overrides: object) -> list[Alert]:
    defaults = {
        "now": NOW,
        "lanes": LANES,
        "prs": [pr([])],
        "timelines": {9: []},
        "comments": {9: []},
        "check_runs": {SHA: []},
        "head_times": {},
        "runners": [{"name": "mac", "status": "online", "labels": [{"name": "bridge-pro-audit"}]}],
        "status_comments": [],
        "stale_minutes": 45,
        "dedupe_hours": 6,
        "runner_label": "bridge-pro-audit",
    }
    defaults.update(overrides)
    return evaluate(**defaults)  # type: ignore[arg-type]


class GateHealthTests(unittest.TestCase):
    def test_stale_needs_label_without_head_bound_verdict_alerts(self) -> None:
        alerts = evaluate_case(
            prs=[pr(["needs-codex-audit"])],
            timelines={
                9: [
                    {
                        "event": "labeled",
                        "label": {"name": "needs-codex-audit"},
                        "created_at": "2026-08-17T03:00:00Z",
                    }
                ]
            },
            status_comments=[
                {
                    "created_at": "2026-08-17T04:00:00Z",
                    "body": "<!-- CODE_MOWER_GATE_HEALTH_ALERT key=pr-9-codex-bbbbbbbbbbbb-stale -->",
                }
            ],
        )

        self.assertEqual([alert.key for alert in alerts], [f"pr-9-codex-{SHA[:12]}-stale"])
        self.assertTrue(alerts[0].gate_stalled)

    def test_head_bound_verdict_suppresses_stale_alert(self) -> None:
        lanes = [
            {
                **LANES[0],
                "bot_authors": "trusted-audit-bot",
            }
        ]
        alerts = evaluate_case(
            lanes=lanes,
            prs=[pr(["needs-codex-audit"])],
            timelines={
                9: [
                    {
                        "event": "labeled",
                        "label": {"name": "needs-codex-audit"},
                        "created_at": "2026-08-17T03:00:00Z",
                    }
                ]
            },
            comments={
                9: [
                    {
                        "user": {"login": "trusted-audit-bot"},
                        "body": (
                            f"Head SHA: `{SHA}`\n"
                            "<!-- CODEX_AUDIT_STATE: codex-audit-done -->"
                        )
                    }
                ]
            },
        )

        self.assertEqual(alerts, [])

    def test_missing_trusted_authors_do_not_suppress_stale_alert(self) -> None:
        alerts = evaluate_case(
            prs=[pr(["needs-codex-audit"])],
            timelines={
                9: [
                    {
                        "event": "labeled",
                        "label": {"name": "needs-codex-audit"},
                        "created_at": "2026-08-17T03:00:00Z",
                    }
                ]
            },
            comments={
                9: [
                    {
                        "user": {"login": "random-user"},
                        "body": (
                            f"Head SHA: `{SHA}`\n"
                            "<!-- CODEX_AUDIT_STATE: codex-audit-done -->"
                        ),
                    }
                ]
            },
        )

        self.assertEqual([alert.key for alert in alerts], [f"pr-9-codex-{SHA[:12]}-stale"])

    def test_untrusted_terminal_comment_does_not_suppress_stale_alert(self) -> None:
        lanes = [
            {
                **LANES[0],
                "bot_authors": "trusted-audit-bot",
            }
        ]
        alerts = evaluate_case(
            lanes=lanes,
            prs=[pr(["needs-codex-audit"])],
            timelines={
                9: [
                    {
                        "event": "labeled",
                        "label": {"name": "needs-codex-audit"},
                        "created_at": "2026-08-17T03:00:00Z",
                    }
                ]
            },
            comments={
                9: [
                    {
                        "user": {"login": "untrusted-bot"},
                        "body": (
                            f"Head SHA: `{SHA}`\n"
                            "<!-- CODEX_AUDIT_STATE: codex-audit-done -->"
                        ),
                    }
                ]
            },
        )

        self.assertEqual([alert.key for alert in alerts], [f"pr-9-codex-{SHA[:12]}-stale"])

    def test_failed_local_audit_check_alerts(self) -> None:
        alerts = evaluate_case(
            check_runs={
                SHA: [
                    audit_check(
                        conclusion="failure",
                        created_at="2026-08-17T04:55:00Z",
                        completed_at="2026-08-17T04:55:00Z",
                    )
                ]
            },
        )

        self.assertEqual([alert.key for alert in alerts], [f"pr-9-{SHA[:12]}-local-audit-failed"])
        self.assertFalse(alerts[0].gate_stalled)

    def test_cancelled_local_audit_check_alerts(self) -> None:
        alerts = evaluate_case(
            check_runs={
                SHA: [
                    audit_check(
                        conclusion="cancelled",
                        created_at="2026-08-17T04:55:00Z",
                        completed_at="2026-08-17T04:55:00Z",
                    )
                ]
            },
        )

        self.assertEqual([alert.key for alert in alerts], [f"pr-9-{SHA[:12]}-local-audit-failed"])

    def test_new_head_time_resets_stale_clock(self) -> None:
        alerts = evaluate_case(
            prs=[pr(["needs-codex-audit"])],
            timelines={
                9: [
                    {
                        "event": "labeled",
                        "label": {"name": "needs-codex-audit"},
                        "created_at": "2026-08-17T03:00:00Z",
                    }
                ]
            },
            head_times={SHA: datetime(2026, 8, 17, 4, 30, tzinfo=timezone.utc)},
        )

        self.assertEqual(alerts, [])

    def test_newer_pending_audit_suppresses_older_failure(self) -> None:
        alerts = evaluate_case(
            check_runs={
                SHA: [
                    audit_check(
                        conclusion="failure",
                        created_at="2026-08-17T04:00:00Z",
                        completed_at="2026-08-17T04:00:00Z",
                    ),
                    audit_check(
                        status="pending",
                        conclusion=None,
                        created_at="2026-08-17T04:30:00Z",
                        started_at="2026-08-17T04:30:00Z",
                    ),
                ]
            }
        )

        self.assertEqual(alerts, [])

    def test_older_pending_audit_does_not_suppress_newer_failure(self) -> None:
        alerts = evaluate_case(
            check_runs={
                SHA: [
                    audit_check(
                        status="pending",
                        conclusion=None,
                        created_at="2026-08-17T04:00:00Z",
                        started_at="2026-08-17T04:00:00Z",
                    ),
                    audit_check(
                        conclusion="failure",
                        created_at="2026-08-17T04:30:00Z",
                        completed_at="2026-08-17T04:30:00Z",
                    ),
                ]
            }
        )

        self.assertEqual([alert.key for alert in alerts], [f"pr-9-{SHA[:12]}-local-audit-failed"])

    def test_newer_success_suppresses_older_failure_even_if_failure_completes_later(self) -> None:
        alerts = evaluate_case(
            check_runs={
                SHA: [
                    audit_check(
                        conclusion="failure",
                        created_at="2026-08-17T04:00:00Z",
                        completed_at="2026-08-17T04:55:00Z",
                    ),
                    audit_check(
                        conclusion="success",
                        created_at="2026-08-17T04:30:00Z",
                        completed_at="2026-08-17T04:40:00Z",
                    ),
                ]
            }
        )

        self.assertEqual(alerts, [])

    def test_non_code_mower_audit_check_is_ignored(self) -> None:
        alerts = evaluate_case(
            check_runs={
                SHA: [
                    audit_check(
                        workflow_path=".github/workflows/other-audit.yml",
                        conclusion="failure",
                        created_at="2026-08-17T04:30:00Z",
                        completed_at="2026-08-17T04:30:00Z",
                    )
                ]
            }
        )

        self.assertEqual(alerts, [])

    def test_repeated_unknown_comments_alert_per_lane(self) -> None:
        lanes = [{**LANES[1], "bot_authors": "trusted-audit-bot"}]
        alerts = evaluate_case(
            lanes=lanes,
            comments={
                9: [
                    audit_comment("unknown", created_at="2026-08-17T04:01:00Z"),
                    audit_comment("unknown", created_at="2026-08-17T04:02:00Z"),
                    audit_comment("unknown", created_at="2026-08-17T04:03:00Z"),
                ]
            },
            repo="owner/repo",
        )

        self.assertEqual(len(alerts), 1)
        self.assertIn("repeated UNKNOWN", alerts[0].title)
        self.assertIn("not STALE", alerts[0].body)

    def test_stale_comment_breaks_unknown_streak(self) -> None:
        lanes = [{**LANES[1], "bot_authors": "trusted-audit-bot"}]
        alerts = evaluate_case(
            lanes=lanes,
            comments={
                9: [
                    audit_comment("unknown", created_at="2026-08-17T04:01:00Z"),
                    audit_comment("stale", created_at="2026-08-17T04:02:00Z"),
                    audit_comment("unknown", created_at="2026-08-17T04:03:00Z"),
                ]
            },
            repo="owner/repo",
        )

        self.assertEqual(alerts, [])

    def test_terminal_comment_breaks_unknown_streak(self) -> None:
        lanes = [{**LANES[1], "bot_authors": "trusted-audit-bot"}]
        alerts = evaluate_case(
            lanes=lanes,
            comments={
                9: [
                    audit_comment("unknown", created_at="2026-08-17T04:01:00Z"),
                    audit_comment("terminal", created_at="2026-08-17T04:02:00Z"),
                    audit_comment("unknown", created_at="2026-08-17T04:03:00Z"),
                ]
            },
            repo="owner/repo",
        )

        self.assertEqual(alerts, [])

    def test_runner_offline_alert_dedupes(self) -> None:
        alerts = evaluate_case(
            prs=[],
            timelines={},
            comments={},
            check_runs={},
            runners=[{"name": "mac", "status": "offline", "labels": [{"name": "bridge-pro-audit"}]}],
            status_comments=[
                {
                    "created_at": "2026-08-17T04:00:00Z",
                    "body": "<!-- CODE_MOWER_GATE_HEALTH_ALERT key=runner-offline -->",
                }
            ],
        )

        self.assertEqual(alerts, [])

    def test_paginated_gh_api_list_flattens_object_wrapped_pages(self) -> None:
        import code_mower.gate_health as gate_health

        original = gate_health.gh_json
        try:
            gate_health.gh_json = lambda _args, env=None: [
                {"check_runs": [{"name": "a"}]},
                {"check_runs": [{"name": "b"}]},
            ]
            self.assertEqual(
                gh_api_list("owner/repo", "commits/x/check-runs", "check_runs"),
                [{"name": "a"}, {"name": "b"}],
            )
        finally:
            gate_health.gh_json = original

    def test_fetch_check_runs_uses_pr_numbers_for_duplicate_head_shas(self) -> None:
        import code_mower.gate_health as gate_health

        original = gate_health.gh_api_list
        try:
            gate_health.gh_api_list = lambda _repo, _path, key=None, env=None: [{"name": "audit"}]
            failures: list[str] = []
            result = fetch_per_pr(
                "owner/repo",
                [
                    {"number": 9, "headRefOid": SHA},
                    {"number": 10, "headRefOid": SHA},
                ],
                "checks",
                failures,
            )
        finally:
            gate_health.gh_api_list = original

        self.assertEqual(sorted(result), [9, 10])
        self.assertEqual(failures, [])

    def test_fetch_check_runs_enriches_actions_workflow_metadata(self) -> None:
        import code_mower.gate_health as gate_health

        original_gh_api_list = gate_health.gh_api_list
        original_gh_json = gate_health.gh_json
        try:
            gate_health.gh_api_list = lambda _repo, _path, key=None, env=None: [
                {
                    "name": "audit",
                    "details_url": "https://github.com/owner/repo/actions/runs/123/job/456",
                }
            ]
            gate_health.gh_json = lambda _args, env=None: {
                "path": LOCAL_AUDIT_WORKFLOW_PATH,
                "name": "Code Mower Local CLI Audits",
            }
            failures: list[str] = []
            result = fetch_per_pr(
                "owner/repo",
                [{"number": 9, "headRefOid": SHA}],
                "checks",
                failures,
            )
        finally:
            gate_health.gh_api_list = original_gh_api_list
            gate_health.gh_json = original_gh_json

        self.assertEqual(result[9][0]["workflow_path"], LOCAL_AUDIT_WORKFLOW_PATH)
        self.assertEqual(result[9][0]["workflow_name"], "Code Mower Local CLI Audits")
        self.assertEqual(result[9][0]["run_id"], "123")
        self.assertEqual(failures, [])

    def test_aggregate_comment_can_dedupe_multiple_keys(self) -> None:
        body = (
            "<!-- CODE_MOWER_GATE_HEALTH_ALERT key=one -->\n"
            "<!-- CODE_MOWER_GATE_HEALTH_ALERT key=two -->"
        )

        self.assertEqual(
            recent_alert_keys([{"created_at": "2026-08-17T04:00:00Z", "body": body}], NOW, 6),
            {"one", "two"},
        )

    def test_alert_marker_dedupes_without_space_before_close(self) -> None:
        body = "<!-- CODE_MOWER_GATE_HEALTH_ALERT key=one-->"

        self.assertEqual(
            recent_alert_keys([{"created_at": "2026-08-17T04:00:00Z", "body": body}], NOW, 6),
            {"one"},
        )

    def test_aggregate_comment_chunks_every_marked_alert(self) -> None:
        alerts = [Alert(f"key-{index}", f"title {index}", f"body {index}") for index in range(21)]

        bodies = format_alert_comments(alerts)

        self.assertEqual(len(bodies), 2)
        self.assertIn("title 20: body 20", bodies[1])
        self.assertIn("<!-- CODE_MOWER_GATE_HEALTH_ALERT key=key-20 -->", bodies[1])

    def test_main_skips_status_comment_when_status_issue_placeholder(self) -> None:
        import code_mower.gate_health as gate_health

        original_gh_json = gate_health.gh_json
        original_gh_api_list = gate_health.gh_api_list
        original_post_comment = gate_health.post_comment
        original_add_gate_stalled = gate_health.add_gate_stalled
        posted: list[int] = []
        stalled: list[int] = []

        def fake_gh_json(args: list[str], env: dict[str, str] | None = None) -> object:
            if args[:2] == ["pr", "list"]:
                return [pr(["needs-codex-audit"])]
            if args[:1] == ["api"] and args[1] == f"repos/owner/repo/commits/{SHA}":
                return {"commit": {"committer": {"date": "2000-01-01T00:00:00Z"}}}
            raise AssertionError(args)

        def fake_gh_api_list(
            repo: str,
            path: str,
            key: str | None = None,
            *,
            env: dict[str, str] | None = None,
        ) -> list[dict[str, object]]:
            if path.startswith("issues/TODO_STATUS_ISSUE/"):
                raise AssertionError(path)
            if path.startswith("issues/comments?"):
                return []
            if path == "issues/9/timeline?per_page=100":
                return [
                    {
                        "event": "labeled",
                        "label": {"name": "needs-codex-audit"},
                        "created_at": "2000-01-01T00:00:00Z",
                    }
                ]
            if path == "issues/9/comments?per_page=100":
                return []
            if path == f"commits/{SHA}/check-runs?per_page=100":
                return []
            raise AssertionError(path)

        try:
            gate_health.gh_json = fake_gh_json  # type: ignore[assignment]
            gate_health.gh_api_list = fake_gh_api_list  # type: ignore[assignment]
            gate_health.post_comment = lambda _repo, issue, _body: posted.append(issue) or True
            gate_health.add_gate_stalled = lambda _repo, number: stalled.append(number) or True
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = gate_health.main(
                    [
                        "--repo",
                        "owner/repo",
                        "--status-issue",
                        "TODO_STATUS_ISSUE",
                        "--lanes-json",
                        json.dumps(LANES),
                        "--stale-minutes",
                        "1",
                        "--runner-check",
                        "disabled",
                    ]
                )
        finally:
            gate_health.gh_json = original_gh_json
            gate_health.gh_api_list = original_gh_api_list
            gate_health.post_comment = original_post_comment
            gate_health.add_gate_stalled = original_add_gate_stalled

        payload = json.loads(stdout.getvalue().strip().splitlines()[-1])
        self.assertEqual(code, 0)
        self.assertIn(f"pr-9-codex-{SHA[:12]}-stale", payload["alerts"])
        self.assertEqual(payload["failed_alerts"], [])
        self.assertEqual(posted, [])
        self.assertEqual(stalled, [9])

    def test_main_fetches_comments_for_non_gate_prs_to_reset_unknown_streak(self) -> None:
        import code_mower.gate_health as gate_health

        original_gh_json = gate_health.gh_json
        original_gh_api_list = gate_health.gh_api_list
        lanes = [{**LANES[1], "bot_authors": "trusted-audit-bot"}]

        def fake_gh_json(args: list[str], env: dict[str, str] | None = None) -> object:
            if args[:2] == ["pr", "list"]:
                return [
                    {
                        "number": 9,
                        "title": "Recovered",
                        "headRefOid": SHA,
                        "labels": [],
                    },
                    {**pr(["needs-claude-audit"]), "number": 10},
                ]
            if args[:1] == ["api"] and args[1] == f"repos/owner/repo/commits/{SHA}":
                return {"commit": {"committer": {"date": "2000-01-01T00:00:00Z"}}}
            raise AssertionError(args)

        def fake_gh_api_list(
            repo: str,
            path: str,
            key: str | None = None,
            *,
            env: dict[str, str] | None = None,
        ) -> list[dict[str, object]]:
            if path.startswith("issues/comments?"):
                return [
                    issue_comment(
                        9,
                        audit_comment("terminal", created_at="2026-08-17T04:04:00Z"),
                    ),
                    issue_comment(
                        10,
                        audit_comment("unknown", created_at="2026-08-17T04:01:00Z"),
                    ),
                    issue_comment(
                        10,
                        audit_comment("unknown", created_at="2026-08-17T04:02:00Z"),
                    ),
                    issue_comment(
                        10,
                        audit_comment("unknown", created_at="2026-08-17T04:03:00Z"),
                    ),
                ]
            if path == "issues/9/comments?per_page=100":
                raise AssertionError("non-gate PR comments should use the batch endpoint")
            if path == "issues/9/timeline?per_page=100":
                raise AssertionError("non-gate PR timelines should not be fetched")
            if path == "issues/10/comments?per_page=100":
                return []
            if path == "issues/10/timeline?per_page=100":
                return [
                    {
                        "event": "labeled",
                        "label": {"name": "needs-claude-audit"},
                        "created_at": "2026-08-17T04:59:00Z",
                    }
                ]
            if path == f"commits/{SHA}/check-runs?per_page=100":
                return []
            raise AssertionError(path)

        try:
            gate_health.gh_json = fake_gh_json  # type: ignore[assignment]
            gate_health.gh_api_list = fake_gh_api_list  # type: ignore[assignment]
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = gate_health.main(
                    [
                        "--repo",
                        "owner/repo",
                        "--lanes-json",
                        json.dumps(lanes),
                        "--stale-minutes",
                        "9999",
                        "--runner-check",
                        "disabled",
                        "--dry-run",
                    ]
                )
        finally:
            gate_health.gh_json = original_gh_json
            gate_health.gh_api_list = original_gh_api_list

        payload = json.loads(stdout.getvalue().strip().splitlines()[-1])
        self.assertEqual(code, 0)
        self.assertEqual(payload["alerts"], [])

    def test_main_skips_stale_eval_when_gate_comments_fetch_fails(self) -> None:
        import code_mower.gate_health as gate_health

        original_gh_json = gate_health.gh_json
        original_gh_api_list = gate_health.gh_api_list

        def fake_gh_json(args: list[str], env: dict[str, str] | None = None) -> object:
            if args[:2] == ["pr", "list"]:
                return [pr(["needs-codex-audit"])]
            raise AssertionError(args)

        def fake_gh_api_list(
            repo: str,
            path: str,
            key: str | None = None,
            *,
            env: dict[str, str] | None = None,
        ) -> list[dict[str, object]]:
            if path.startswith("issues/comments?"):
                return []
            if path == "issues/9/timeline?per_page=100":
                return [
                    {
                        "event": "labeled",
                        "label": {"name": "needs-codex-audit"},
                        "created_at": "2000-01-01T00:00:00Z",
                    }
                ]
            if path == "issues/9/comments?per_page=100":
                raise ValueError("comments unavailable")
            if path == f"commits/{SHA}/check-runs?per_page=100":
                return []
            raise AssertionError(path)

        try:
            gate_health.gh_json = fake_gh_json  # type: ignore[assignment]
            gate_health.gh_api_list = fake_gh_api_list  # type: ignore[assignment]
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = gate_health.main(
                    [
                        "--repo",
                        "owner/repo",
                        "--lanes-json",
                        json.dumps(LANES),
                        "--stale-minutes",
                        "1",
                        "--runner-check",
                        "disabled",
                        "--dry-run",
                    ]
                )
        finally:
            gate_health.gh_json = original_gh_json
            gate_health.gh_api_list = original_gh_api_list

        payload = json.loads(stdout.getvalue().strip().splitlines()[-1])
        self.assertEqual(code, 1)
        self.assertIn("comments:pr-9", payload["fetch_failures"])
        self.assertNotIn(f"pr-9-codex-{SHA[:12]}-stale", payload["alerts"])

    def test_main_counts_recent_closed_pr_comments_for_unknown_streak(self) -> None:
        import code_mower.gate_health as gate_health

        original_gh_json = gate_health.gh_json
        original_gh_api_list = gate_health.gh_api_list
        lanes = [{**LANES[1], "bot_authors": "trusted-audit-bot"}]

        def fake_gh_json(args: list[str], env: dict[str, str] | None = None) -> object:
            if args[:2] == ["pr", "list"] and "--state" in args:
                state = args[args.index("--state") + 1]
                if state == "open":
                    return []
                if state == "closed":
                    return [
                        {
                            "number": 12,
                            "title": "Closed broken lane",
                            "headRefOid": SHA,
                            "labels": [],
                        }
                    ]
            raise AssertionError(args)

        def fake_gh_api_list(
            repo: str,
            path: str,
            key: str | None = None,
            *,
            env: dict[str, str] | None = None,
        ) -> list[dict[str, object]]:
            if path.startswith("issues/comments?"):
                return [
                    issue_comment(
                        12,
                        audit_comment("unknown", created_at="2026-08-17T04:01:00Z"),
                    ),
                    issue_comment(
                        12,
                        audit_comment("unknown", created_at="2026-08-17T04:02:00Z"),
                    ),
                    issue_comment(
                        12,
                        audit_comment("unknown", created_at="2026-08-17T04:03:00Z"),
                    ),
                ]
            raise AssertionError(path)

        try:
            gate_health.gh_json = fake_gh_json  # type: ignore[assignment]
            gate_health.gh_api_list = fake_gh_api_list  # type: ignore[assignment]
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = gate_health.main(
                    [
                        "--repo",
                        "owner/repo",
                        "--lanes-json",
                        json.dumps(lanes),
                        "--runner-check",
                        "disabled",
                        "--dry-run",
                    ]
                )
        finally:
            gate_health.gh_json = original_gh_json
            gate_health.gh_api_list = original_gh_api_list

        payload = json.loads(stdout.getvalue().strip().splitlines()[-1])
        self.assertEqual(code, 0)
        self.assertEqual(payload["alerts"], ["lane-claude-unknown-20260817T040300"])

    def test_main_does_not_emit_runner_missing_when_runner_check_disabled(self) -> None:
        import code_mower.gate_health as gate_health

        original_gh_json = gate_health.gh_json
        original_gh_api_list = gate_health.gh_api_list
        original_post_comment = gate_health.post_comment
        posted: list[int] = []
        try:
            gate_health.gh_json = lambda _args, env=None: []
            gate_health.gh_api_list = lambda _repo, _path, key=None, env=None: []
            gate_health.post_comment = lambda _repo, issue, _body: posted.append(issue) or True
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = gate_health.main(
                    [
                        "--repo",
                        "owner/repo",
                        "--status-issue",
                        "6",
                        "--runner-label",
                        "bridge-pro-audit",
                        "--alert-runner-api-unavailable",
                    ]
                )
        finally:
            gate_health.gh_json = original_gh_json
            gate_health.gh_api_list = original_gh_api_list
            gate_health.post_comment = original_post_comment

        payload = json.loads(stdout.getvalue().strip().splitlines()[-1])
        self.assertEqual(code, 0)
        self.assertEqual(payload["alerts"], ["runner-check-disabled"])
        self.assertEqual(posted, [6])

    def test_main_fetches_checks_for_unlabeled_prs(self) -> None:
        import code_mower.gate_health as gate_health

        original_gh_json = gate_health.gh_json
        original_gh_api_list = gate_health.gh_api_list
        original_post_comment = gate_health.post_comment
        posted: list[int] = []

        def fake_gh_json(args: list[str], env: dict[str, str] | None = None) -> object:
            if args[:2] == ["pr", "list"]:
                return [pr([])]
            raise AssertionError(args)

        def fake_gh_api_list(
            repo: str,
            path: str,
            key: str | None = None,
            *,
            env: dict[str, str] | None = None,
        ) -> list[dict[str, object]]:
            if path.startswith("issues/6/comments?"):
                return []
            if path.startswith("issues/comments?"):
                return []
            if path == f"commits/{SHA}/check-runs?per_page=100":
                return [
                    audit_check(
                        conclusion="failure",
                        created_at="2026-08-17T04:30:00Z",
                        completed_at="2026-08-17T04:30:00Z",
                    )
                ]
            raise AssertionError(path)

        try:
            gate_health.gh_json = fake_gh_json  # type: ignore[assignment]
            gate_health.gh_api_list = fake_gh_api_list  # type: ignore[assignment]
            gate_health.post_comment = lambda _repo, issue, _body: posted.append(issue) or True
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = gate_health.main(
                    [
                        "--repo",
                        "owner/repo",
                        "--status-issue",
                        "6",
                        "--runner-check",
                        "disabled",
                    ]
                )
        finally:
            gate_health.gh_json = original_gh_json
            gate_health.gh_api_list = original_gh_api_list
            gate_health.post_comment = original_post_comment

        payload = json.loads(stdout.getvalue().strip().splitlines()[-1])
        self.assertEqual(code, 0)
        self.assertEqual(payload["alerts"], [f"pr-9-{SHA[:12]}-local-audit-failed"])
        self.assertEqual(posted, [6])

    def test_required_runner_check_returns_clean_error(self) -> None:
        import code_mower.gate_health as gate_health

        original_gh_json = gate_health.gh_json
        original_gh_api_list = gate_health.gh_api_list

        def fake_gh_api_list(
            repo: str,
            path: str,
            key: str | None = None,
            *,
            env: dict[str, str] | None = None,
        ) -> list[dict[str, object]]:
            if path.startswith("issues/6/comments?"):
                return []
            if path == "actions/runners?per_page=100":
                raise subprocess.CalledProcessError(1, ["gh", "api"])
            raise AssertionError(path)

        try:
            gate_health.gh_json = lambda _args, env=None: []
            gate_health.gh_api_list = fake_gh_api_list  # type: ignore[assignment]
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = gate_health.main(
                    [
                        "--repo",
                        "owner/repo",
                        "--status-issue",
                        "6",
                        "--runner-label",
                        "bridge-pro-audit",
                        "--runner-check",
                        "required",
                    ]
                )
        finally:
            gate_health.gh_json = original_gh_json
            gate_health.gh_api_list = original_gh_api_list

        output = stdout.getvalue()
        payload = json.loads(output.strip().splitlines()[-1])
        self.assertEqual(code, 1)
        self.assertIn("error: required runner check failed", output)
        self.assertEqual(payload["fetch_failures"], ["runners"])
        self.assertEqual(payload["runner_check"], "unavailable")

    def test_add_gate_stalled_fails_when_label_cannot_be_verified_or_created(self) -> None:
        import code_mower.gate_health as gate_health

        original_run_gh = gate_health.run_gh
        calls: list[list[str]] = []
        try:
            gate_health.run_gh = lambda args, payload=None, quiet=False: calls.append(list(args)) or False
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = gate_health.add_gate_stalled("owner/repo", 9)
        finally:
            gate_health.run_gh = original_run_gh

        self.assertFalse(result)
        self.assertEqual(len(calls), 2)
        self.assertIn("failed to create or verify gate-stalled label", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
