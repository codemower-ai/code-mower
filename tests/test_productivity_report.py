from __future__ import annotations

from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
import json
import tempfile
from pathlib import Path
from unittest import TestCase

from code_mower import board_store, lane_status, productivity_report, reviewer_spend


NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def _productivity_event(
    *,
    event_id: str,
    created_at: str,
    metrics: dict[str, object],
    dimensions: dict[str, object] | None = None,
) -> dict[str, object]:
    event_dimensions = {
        "productivity_schema": "code_mower.productivityMetrics.v1",
        "repo_slug": "owner/repo",
        "window_start": "2026-09-03T10:00:00Z",
        "window_end": "2026-09-03T12:00:00Z",
        "window_granularity": "release",
        "aggregation_subject": "repo",
        "aggregation_key": "owner/repo",
        "release": "v1.0.0",
        "pilot_posture": "supervised",
        "event_source": "local-report",
    }
    event_dimensions.update(dimensions or {})
    return {
        "schema": "code_mower.benchmarkEvent.v1",
        "event_id": event_id,
        "event_type": "productivity_summary",
        "created_at": created_at,
        "repo_slug": "owner/repo",
        "team_id": "team",
        "install_id": "install",
        "source": "fixture",
        "provider": "code-mower",
        "lens": "productivity",
        "status": "observed",
        "tool": {
            "role": "reporter",
            "tool_name": "code-mower",
            "tool_version": "1.0.1",
            "provider": "code-mower",
            "model": "",
            "model_source": "not_applicable",
            "version_source": "package_version",
            "integration": "cli",
            "runtime_environment": "local",
        },
        "metrics": metrics,
        "dimensions": event_dimensions,
    }


def _write_cloud_events(root: Path, events: list[dict[str, object]] | None = None) -> Path:
    path = root / "productivity-events.json"
    path.write_text(
        json.dumps(
            {
                "productivity_summary_events": events
                or [
                    _productivity_event(
                        event_id="productivity-repo-release-1",
                        created_at="2026-09-03T12:10:00Z",
                        metrics={
                            "cycle_time_seconds": 7200,
                            "merged_pr_count": 4,
                            "reviewer_run_count": 9,
                            "reviewer_catch_count": 2,
                            "blocked_finding_count": 2,
                            "fix_round_count": 2,
                            "cost_usd": 3.21,
                            "total_tokens": 12000,
                        },
                    )
                ]
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _status(*, verdict_label: str, owner_actions: int = 0) -> dict[str, object]:
    return {
        "schema": lane_status.LANE_STATUS_SCHEMA,
        "repo": "owner/repo",
        "generated_at": NOW.isoformat().replace("+00:00", "Z"),
        "remote": {
            "available": True,
            "errors": [],
            "pull_requests": [
                {
                    "number": 42,
                    "url": "https://github.com/owner/repo/pull/42",
                    "branch": "codex/productivity",
                    "author": "codex-bot",
                    "head_sha": "abcdef0123456789abcdef0123456789abcdef01",
                    "is_draft": False,
                    "merge_state": "CLEAN",
                    "updated_at": NOW.isoformat().replace("+00:00", "Z"),
                    "labels": {
                        "builder": ["builder:codex"],
                        "dispatched": [],
                        "needs": [],
                        "done": [verdict_label] if verdict_label.endswith("-done") else [],
                        "blocked": [verdict_label] if verdict_label.endswith("-blocked") else [],
                    },
                    "checks": [{"name": "code-mower/gate", "state": "success"}],
                    "stale": False,
                    "next_action": "ready for merge or auto-merge",
                    "next_detail": "",
                }
            ],
            "workflow_runs": [],
            "gate_health": {"status": "pass", "alerts": []},
        },
        "local_boards": {"available": True, "boards": [], "message": ""},
        "local_processes": {"available": True, "processes": [], "message": ""},
        "owner_queue": {
            "schema": "code_mower.boardOwnerQueue.v1",
            "available": True,
            "count": owner_actions,
            "entries": [],
            "message": "",
        },
        "agent_adapters": {
            "schema": "code_mower.boardAgentAdapters.v1",
            "available": True,
            "agents": [{"provider": "codex", "status": "running"}],
            "warnings": [],
            "message": "",
        },
        "supervised_pilot": {
            "schema": "code_mower.supervisedPilot.v1",
            "enabled": True,
            "queue": {"metrics": {"active_lane_count": 1}},
        },
        "next_action": "ready for merge or auto-merge",
        "next_detail": "",
    }


class ProductivityReportTests(TestCase):
    def test_report_combines_board_spend_and_cloud_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_path = root / "events.jsonl"
            spend_path = root / "reviewer-spend.json"
            board_store.append_snapshot(
                _status(verdict_label="codex-audit-blocked", owner_actions=1),
                path=store_path,
                now=NOW,
            )
            board_store.append_snapshot(
                _status(verdict_label="codex-audit-done"),
                path=store_path,
                now=NOW + timedelta(minutes=2),
            )
            spend_path.write_text(
                json.dumps(
                    {
                        "schema": reviewer_spend.SPEND_SCHEMA,
                        "runs": [
                            {
                                "created_at": NOW.isoformat().replace("+00:00", "Z"),
                                "lane": "codex-audit",
                                "repo": "owner/repo",
                                "pr_number": 42,
                                "head_sha": "abcdef0123456789",
                                "model": "gpt-5.6",
                                "wall_seconds": 30.0,
                                "verdict": "BLOCKED",
                                "total_tokens": 600,
                                "cost_usd": 0.12,
                            },
                            {
                                "created_at": (NOW + timedelta(minutes=1))
                                .isoformat()
                                .replace("+00:00", "Z"),
                                "lane": "codex-audit",
                                "repo": "owner/repo",
                                "pr_number": 42,
                                "head_sha": "abcdef0123456789",
                                "model": "gpt-5.6",
                                "wall_seconds": 45.0,
                                "verdict": "PASS",
                                "total_tokens": 800,
                                "cost_usd": 0.18,
                            },
                            {
                                "created_at": NOW.isoformat().replace("+00:00", "Z"),
                                "lane": "claude-audit",
                                "repo": "other/repo",
                                "pr_number": 7,
                                "head_sha": "0123456789ab",
                                "model": "claude",
                                "wall_seconds": 10.0,
                                "verdict": "PASS",
                            },
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            report = productivity_report.build_report(
                repo="owner/repo",
                repo_path=root,
                store_path=store_path,
                spend_path=spend_path,
                cloud_event_paths=[_write_cloud_events(root)],
                now=NOW,
            )

        serialized = json.dumps(report)
        text = productivity_report.render_text(report)
        self.assertEqual(report["schema"], productivity_report.PRODUCTIVITY_REPORT_SCHEMA)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["current"]["open_pr_count"], 1)
        self.assertEqual(report["current"]["active_lane_count"], 1)
        self.assertEqual(report["metrics"]["reviewer_run_count"], 2)
        self.assertEqual(report["metrics"]["audit_blocked_count"], 1)
        self.assertEqual(report["metrics"]["audit_pass_count"], 1)
        self.assertEqual(report["metrics"]["fix_round_count"], 1)
        self.assertEqual(report["metrics"]["cycle_time_seconds"], 7200)
        self.assertEqual(report["metrics"]["merged_pr_count"], 4)
        self.assertEqual(report["spend"]["wall_seconds"], 75.0)
        self.assertEqual(report["spend"]["total_tokens"], 1400)
        self.assertEqual(report["spend"]["by_lane"][0]["provider"], "codex")
        self.assertEqual(report["spend"]["by_lane"][0]["role"], "reviewer")
        self.assertEqual(report["spend"]["by_lane"][0]["cost_usd"], 0.3)
        self.assertNotIn("cost_reported", report["spend"]["by_lane"][0])
        self.assertNotIn("token_reported", report["spend"]["by_lane"][0])
        self.assertNotIn("wall_reported", report["spend"]["by_lane"][0])
        self.assertEqual(report["source"]["reviewer_spend"]["filtered_rows"], 1)
        self.assertEqual(report["providers"]["schema"], "code_mower.providerScorecards.v1")
        self.assertEqual(report["providers"]["promotion_policy"], "docs/lane-promotion-policy.md")
        self.assertEqual(report["providers"]["scorecards"][0]["provider"], "codex")
        self.assertEqual(report["providers"]["scorecards"][0]["role"], "reviewer")
        self.assertEqual(report["providers"]["scorecards"][0]["metrics"]["reviewer_run_count"], 2)
        self.assertEqual(report["providers"]["scorecards"][0]["metrics"]["audit_blocked_count"], 1)
        self.assertEqual(report["providers"]["scorecards"][0]["metrics"]["cost_usd"], 0.3)
        self.assertEqual(report["providers"]["scorecards"][0]["rates"]["audit_block_rate"], 0.5)
        self.assertEqual(
            report["providers"]["scorecards"][0]["promotion"]["recommendation"],
            "informational",
        )
        self.assertIn(
            "docs/lane-promotion-policy.md",
            report["providers"]["scorecards"][0]["promotion"]["policy"],
        )
        self.assertEqual(report["next_action"], "ready for merge or auto-merge")
        self.assertIn("Provider scorecards:", text)
        self.assertIn("codex reviewer", text)
        self.assertIn(lane_status.LOCAL_PATH_REDACTION, serialized)
        self.assertNotIn(str(Path(tmp)), serialized)

    def test_provider_scorecards_include_cloud_provider_evidence_without_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spend_path = root / "reviewer-spend.json"
            spend_path.write_text(
                json.dumps(
                    {
                        "schema": reviewer_spend.SPEND_SCHEMA,
                        "runs": [
                            {
                                "created_at": NOW.isoformat().replace("+00:00", "Z"),
                                "lane": "claude-audit",
                                "repo": "owner/repo",
                                "pr_number": 42,
                                "head_sha": "abcdef0123456789",
                                "model": "sonnet",
                                "wall_seconds": 60.0,
                                "verdict": "BLOCKED",
                            },
                            {
                                "created_at": (NOW + timedelta(minutes=2))
                                .isoformat()
                                .replace("+00:00", "Z"),
                                "lane": "claude-audit",
                                "repo": "owner/repo",
                                "pr_number": 42,
                                "head_sha": "abcdef0123456789",
                                "model": "sonnet",
                                "wall_seconds": 40.0,
                                "verdict": "PASS",
                            },
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            cloud_events = _write_cloud_events(
                root,
                events=[
                    _productivity_event(
                        event_id="productivity-provider-claude",
                        created_at="2026-09-03T12:15:00Z",
                        metrics={
                            "reviewer_run_count": 2,
                            "audit_pass_count": 1,
                            "audit_blocked_count": 1,
                            "reviewer_catch_count": 1,
                            "blocked_finding_count": 1,
                            "false_blocker_count": 0,
                            "fix_round_count": 2,
                            "missed_blocker_count": 0,
                            "checks_failed_count": 1,
                            "cost_usd": 0,
                            "total_tokens": 1000,
                        },
                        dimensions={
                            "aggregation_subject": "provider",
                            "aggregation_key": "claude",
                            "provider": "claude",
                            "role": "reviewer",
                        },
                    )
                ],
            )

            report = productivity_report.build_report(
                repo="owner/repo",
                repo_path=root,
                spend_path=spend_path,
                cloud_event_paths=[cloud_events],
                now=NOW,
            )

        card = report["providers"]["scorecards"][0]
        self.assertEqual(card["provider"], "claude")
        self.assertEqual(card["sources"]["reviewer_spend_runs"], 2)
        self.assertEqual(card["sources"]["cloud_events"], 1)
        self.assertEqual(card["metrics"]["reviewer_run_count"], 2)
        self.assertEqual(card["metrics"]["audit_pass_count"], 1)
        self.assertEqual(card["metrics"]["audit_blocked_count"], 1)
        self.assertEqual(card["metrics"]["reviewer_catch_count"], 1)
        self.assertEqual(card["metrics"]["fix_round_count"], 2)
        self.assertEqual(card["metrics"]["cost_usd"], 0)
        self.assertEqual(card["metrics"]["total_tokens"], 1000)
        self.assertEqual(card["metrics"]["infra_failure_count"], 1)
        self.assertTrue(card["reported"]["false_blockers"])
        self.assertTrue(card["reported"]["missed_blockers"])
        self.assertTrue(card["reported"]["checks_failed"])
        self.assertEqual(card["promotion"]["recommendation"], "stabilize_infra")

    def test_report_operates_without_local_or_cloud_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = productivity_report.build_report(
                repo="owner/repo",
                repo_path=tmp,
                now=NOW,
            )

        serialized = json.dumps(report)
        self.assertEqual(report["status"], "warn")
        self.assertFalse(report["source"]["board_events"]["available"])
        self.assertIsNone(report["metrics"]["reviewer_run_count"])
        self.assertIn("board serve", report["next_action"])
        self.assertIn(lane_status.LOCAL_PATH_REDACTION, serialized)
        self.assertNotIn(str(Path(tmp)), serialized)

    def test_known_zero_spend_counts_stay_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spend_path = root / "reviewer-spend.json"
            spend_path.write_text(
                json.dumps({"schema": reviewer_spend.SPEND_SCHEMA, "runs": []}) + "\n",
                encoding="utf-8",
            )

            report = productivity_report.build_report(
                repo="owner/repo",
                repo_path=root,
                spend_path=spend_path,
                now=NOW,
            )

        self.assertEqual(report["metrics"]["reviewer_run_count"], 0)
        self.assertEqual(report["metrics"]["audit_pass_count"], 0)
        self.assertEqual(report["metrics"]["audit_blocked_count"], 0)

    def test_cloud_duration_metrics_do_not_sum_across_windows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = productivity_report.build_report(
                repo="owner/repo",
                repo_path=root,
                cloud_event_paths=[
                    _write_cloud_events(
                        root,
                        events=[
                            _productivity_event(
                                event_id="productivity-older-window",
                                created_at="2026-09-03T12:00:00Z",
                                metrics={
                                    "cycle_time_seconds": 60,
                                    "reviewer_run_count": 2,
                                },
                            ),
                            _productivity_event(
                                event_id="productivity-newer-window",
                                created_at="2026-09-03T12:10:00Z",
                                metrics={"merged_pr_count": 3},
                            ),
                        ],
                    )
                ],
                now=NOW,
            )

        self.assertIsNone(report["metrics"]["cycle_time_seconds"])
        self.assertEqual(report["metrics"]["reviewer_run_count"], 2)
        self.assertEqual(report["metrics"]["merged_pr_count"], 3)

    def test_cloud_provider_scorecards_do_not_double_count_headline_totals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = productivity_report.build_report(
                repo="owner/repo",
                repo_path=root,
                cloud_event_paths=[
                    _write_cloud_events(
                        root,
                        events=[
                            _productivity_event(
                                event_id="productivity-repo-release",
                                created_at="2026-09-03T12:10:00Z",
                                metrics={
                                    "merged_pr_count": 4,
                                    "reviewer_run_count": 9,
                                    "reviewer_catch_count": 2,
                                    "cost_usd": 3.21,
                                    "total_tokens": 12000,
                                },
                            ),
                            _productivity_event(
                                event_id="productivity-provider-codex",
                                created_at="2026-09-03T12:11:00Z",
                                metrics={
                                    "reviewer_run_count": 5,
                                    "reviewer_catch_count": 1,
                                    "cost_usd": 1.5,
                                    "total_tokens": 6000,
                                },
                                dimensions={
                                    "aggregation_subject": "provider",
                                    "aggregation_key": "codex",
                                    "provider": "codex",
                                    "role": "reviewer",
                                },
                            ),
                        ],
                    )
                ],
                now=NOW,
            )

        self.assertEqual(report["metrics"]["merged_pr_count"], 4)
        self.assertEqual(report["metrics"]["reviewer_run_count"], 9)
        self.assertEqual(report["metrics"]["reviewer_catch_count"], 2)
        self.assertEqual(report["metrics"]["cost_usd"], 3.21)
        self.assertEqual(report["metrics"]["total_tokens"], 12000)
        self.assertEqual(report["providers"]["scorecards"][0]["provider"], "codex")
        self.assertEqual(
            report["providers"]["scorecards"][0]["metrics"]["reviewer_run_count"],
            5,
        )

    def test_cloud_headline_totals_use_one_summary_subject(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = productivity_report.build_report(
                repo="owner/repo",
                repo_path=root,
                cloud_event_paths=[
                    _write_cloud_events(
                        root,
                        events=[
                            _productivity_event(
                                event_id="productivity-repo-release",
                                created_at="2026-09-03T12:10:00Z",
                                metrics={"merged_pr_count": 4, "reviewer_run_count": 9},
                            ),
                            _productivity_event(
                                event_id="productivity-pr-42",
                                created_at="2026-09-03T12:11:00Z",
                                metrics={"merged_pr_count": 1, "reviewer_run_count": 3},
                                dimensions={
                                    "aggregation_subject": "pr",
                                    "aggregation_key": "42",
                                    "pr_number": "42",
                                },
                            ),
                        ],
                    )
                ],
                now=NOW,
            )

        self.assertEqual(report["metrics"]["merged_pr_count"], 4)
        self.assertEqual(report["metrics"]["reviewer_run_count"], 9)
        self.assertEqual(report["cloud_aggregate"]["summary_event_count"], 1)

    def test_cli_report_outputs_json_and_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stdout = StringIO()
            with redirect_stdout(stdout):
                code = productivity_report.main(
                    ["report", "--repo", "owner/repo", "--repo-path", tmp, "--json"]
                )
            parsed = json.loads(stdout.getvalue())

            text_out = StringIO()
            with redirect_stdout(text_out):
                text_code = productivity_report.main(
                    ["report", "--repo", "owner/repo", "--repo-path", tmp]
                )

        self.assertEqual(code, 0)
        self.assertEqual(text_code, 0)
        self.assertEqual(parsed["repo"], "owner/repo")
        self.assertIn("Code Mower productivity report for owner/repo", text_out.getvalue())
        self.assertIn("Next:", text_out.getvalue())

    def test_invalid_cloud_event_warning_uses_filename_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            event_path = root / "private-productivity.json"
            event_path.write_text('{"event_type":"productivity_summary"}\n', encoding="utf-8")

            report = productivity_report.build_report(
                repo="owner/repo",
                repo_path=root,
                cloud_event_paths=[event_path],
                now=NOW,
            )

        serialized = json.dumps(report)
        self.assertIn("private-productivity.json", serialized)
        self.assertNotIn(str(Path(tmp)), serialized)
