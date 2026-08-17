from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

from code_mower import claude_audit_pr, codex_audit_pr, reviewer_spend


def test_reviewer_spend_append_preserves_profiles_and_exports_event() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "reviewer-spend.json"
        path.write_text(
            json.dumps({"profiles": {"codex-audit": {"cost_usd": 0.25}}}),
            encoding="utf-8",
        )
        run = reviewer_spend.build_spend_run(
            lane="codex-audit",
            repo="owner/repo",
            pr_number=42,
            head_sha="abc123",
            model="gpt-5",
            wall_seconds=12.3456,
            verdict="PASS",
            usage={"input_tokens": 100, "output_tokens": "25", "cost_usd": "0.03"},
            created_at="2026-08-16T12:00:00Z",
        )

        payload = reviewer_spend.append_spend_run(path, run)

        assert payload["profiles"]["codex-audit"]["cost_usd"] == 0.25
        assert payload["runs"][0]["lane"] == "codex-audit"
        assert payload["runs"][0]["wall_seconds"] == 12.346
        assert payload["runs"][0]["input_tokens"] == 100
        assert payload["runs"][0]["output_tokens"] == 25
        text = path.read_text(encoding="utf-8")
        assert "stdout" not in text
        assert "diff" not in text
        events = reviewer_spend.spend_runs_to_events(
            payload,
            repo_slug="owner/repo",
            team_id="team",
            install_id="install",
            source="unit-test",
        )
        assert events[0]["event_type"] == "reviewer_run"
        assert events[0]["metrics"]["wall_seconds"] == 12.346
        assert events[0]["metrics"]["cost_usd"] == 0.03
        assert events[0]["tool"]["model"] == "gpt-5"
        assert events[0]["dimensions"]["pr_number"] == "42"


def test_reviewer_spend_append_waits_for_lock() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "reviewer-spend.json"
        lock_path = path.with_name(f".{path.name}.lock")
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        result: list[dict[str, object]] = []

        def append() -> None:
            result.append(
                reviewer_spend.append_spend_run(
                    path,
                    reviewer_spend.build_spend_run(
                        lane="claude-audit",
                        repo="owner/repo",
                        pr_number=42,
                        head_sha="abc123",
                        model="sonnet",
                        wall_seconds=1.0,
                        verdict="PASS",
                    ),
                )
            )

        thread = threading.Thread(target=append)
        thread.start()
        time.sleep(0.15)
        assert thread.is_alive()

        os.close(fd)
        lock_path.unlink()
        thread.join(timeout=2)

        assert not thread.is_alive()
        assert len(result[0]["runs"]) == 1


def test_usage_metrics_extracts_known_numbers_from_cli_json_only() -> None:
    metrics = reviewer_spend.extract_usage_metrics(
        json.dumps(
            {
                "result": "ok",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 2,
                },
                "total_cost_usd": 0.0123,
            }
        ),
        'noise\n{"usage": {"totalTokens": 7}, "costUSD": "$0.02"}\ndone',
        "Findings: none; total_tokens: 999",
    )

    assert metrics == {
        "cache_read_input_tokens": 2,
        "cost_usd": 0.02,
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 7,
    }


def test_codex_audit_main_appends_spend_metadata() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        spend = root / "reviewer-spend.json"
        result = codex_audit_pr.AuditResult(
            repo="owner/repo",
            pr_number=42,
            head_sha_start="abc123",
            head_sha_end="abc123",
            verdict="PASS",
            trailer="<!-- CODEX_AUDIT_STATE: codex-audit-done -->",
            comment_body="ok",
            codex_stdout="",
            codex_stderr=json.dumps({"usage": {"total_tokens": 123}}),
        )

        with (
            mock.patch.object(codex_audit_pr, "_resolve_github_token", return_value="token"),
            mock.patch.object(codex_audit_pr, "audit_pr", return_value=result),
        ):
            code = codex_audit_pr.main(
                [
                    "--repo",
                    "owner/repo",
                    "--pr",
                    "42",
                    "--repo-paths",
                    f"owner/repo:{root}",
                    "--spend-path",
                    str(spend),
                    "--dry-run",
                ]
            )

        payload = reviewer_spend.load_spend_file(spend)
        assert code == 0
        assert payload["runs"][0]["lane"] == "codex-audit"
        assert payload["runs"][0]["total_tokens"] == 123


def test_claude_audit_main_appends_spend_metadata() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        spend = root / "reviewer-spend.json"
        result = claude_audit_pr.ClaudeAuditResult(
            repo="owner/repo",
            pr_number=42,
            head_sha_start="abc123",
            head_sha_end="abc123",
            verdict="PASS",
            trailer="<!-- CLAUDE_AUDIT_STATE: claude-audit-done -->",
            comment_body="ok",
            claude_stdout="",
            claude_stderr=json.dumps({"usage": {"total_tokens": 456}}),
        )

        with (
            mock.patch.object(claude_audit_pr, "_resolve_github_token", return_value="token"),
            mock.patch.object(claude_audit_pr, "audit_pr", return_value=result),
        ):
            code = claude_audit_pr.main(
                [
                    "--repo",
                    "owner/repo",
                    "--pr",
                    "42",
                    "--repo-paths",
                    f"owner/repo:{root}",
                    "--spend-path",
                    str(spend),
                    "--dry-run",
                ]
            )

        payload = reviewer_spend.load_spend_file(spend)
        assert code == 0
        assert payload["runs"][0]["lane"] == "claude-audit"
        assert payload["runs"][0]["total_tokens"] == 456


def test_claude_audit_main_respects_no_spend_capture() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        spend = root / "reviewer-spend.json"
        result = claude_audit_pr.ClaudeAuditResult(
            repo="owner/repo",
            pr_number=42,
            head_sha_start="abc123",
            head_sha_end="abc123",
            verdict="PASS",
            trailer="<!-- CLAUDE_AUDIT_STATE: claude-audit-done -->",
            comment_body="ok",
            claude_stdout=json.dumps({"usage": {"input_tokens": 10}}),
        )

        with (
            mock.patch.object(claude_audit_pr, "_resolve_github_token", return_value="token"),
            mock.patch.object(claude_audit_pr, "audit_pr", return_value=result),
        ):
            code = claude_audit_pr.main(
                [
                    "--repo",
                    "owner/repo",
                    "--pr",
                    "42",
                    "--repo-paths",
                    f"owner/repo:{root}",
                    "--spend-path",
                    str(spend),
                    "--no-spend-capture",
                    "--dry-run",
                ]
            )

        assert code == 0
        assert not spend.exists()
