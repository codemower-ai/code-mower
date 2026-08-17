from __future__ import annotations

import unittest

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from code_mower import cloud as code_mower_cloud
from code_mower import code_mower_telemetry, reviewer_metrics
from code_mower.provider_runners import verdict_artifacts
from code_mower.provider_runners import write_audit_verdict_artifact


class ReviewerMetricsCoreTests(unittest.TestCase):
    def test_metrics_combine_dispositions_spend_latency_and_events(self) -> None:
        report = {
            "mode": "reviewer-evidence-calibration",
            "profiles": {
                "codex-audit": {
                    "model": "codex",
                    "runs": 2,
                    "duration_seconds_total": 30.0,
                    "known_blocked_runs": 1,
                    "known_blocked_caught_runs": 1,
                    "dispositions": {
                        "true_positive": 1,
                        "false_positive": 1,
                        "useful": 1,
                        "unknown": 2,
                    },
                }
            },
        }
        event_summary = {
            "mode": "telemetry-summary",
            "lanes": {
                "codex-audit": {
                    "events": 4,
                    "finished": 3,
                    "pass": 2,
                    "blocked": 1,
                    "failed": 0,
                    "findings": 2,
                    "observed_pr_count": 2,
                }
            },
        }

        metrics = reviewer_metrics.build_reviewer_metrics(
            [report],
            spend={
                "profiles": {"codex-audit": {"cost_usd": 1.0}},
                "runs": [
                    {
                        "lane": "codex-audit",
                        "repo": "owner/repo",
                        "pr_number": 42,
                        "head_sha": "abc123",
                        "wall_seconds": 50.0,
                        "cost_usd": 0.25,
                        "total_tokens": 900,
                    }
                ],
            },
            event_summaries=[event_summary],
        )

        profile = metrics["profiles"]["codex-audit"]
        self.assertEqual(profile["runs"], 2)
        self.assertEqual(profile["useful_findings"], 2)
        self.assertEqual(profile["negative_findings"], 1)
        self.assertEqual(profile["precision"], 0.5)
        self.assertEqual(profile["useful_rate"], 0.6667)
        self.assertEqual(profile["cost_per_useful_finding"], 0.625)
        self.assertEqual(profile["spend_run_count"], 1)
        self.assertEqual(profile["spend_wall_seconds_total"], 50.0)
        self.assertEqual(profile["seconds_per_run"], 25.0)
        self.assertEqual(profile["total_tokens"], 900)
        self.assertEqual(profile["event_log"]["blocked"], 1)
        self.assertEqual(profile["event_log"]["observed_pr_count"], 2)

    def test_unsupported_report_mode_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "reviewer metrics expects local-llm-calibration or reviewer-evidence-calibration",
        ):
            reviewer_metrics.build_reviewer_metrics(
                [{"mode": "old-value-report", "profiles": {}}],
            )


class VerdictArtifactEventExportTests(unittest.TestCase):
    def _write_artifact(
        self,
        root: Path,
        *,
        repo: str = "owner/repo",
        pr_number: int = 42,
        lane_id: str = "codex-audit",
        verdict: str = "BLOCKED",
        created_at: str = "2026-06-15T12:00:00Z",
        head_sha: str = "abcdef0123456789abcdef0123456789abcdef01",
        comment_body: str | None = None,
        trailer: str = "<!-- CODEX_AUDIT_STATE: codex-audit-blocked -->",
        duration_seconds: float | None = None,
    ) -> Path:
        artifact_dir = (
            root
            / repo.replace("/", "__")
            / f"pr-{pr_number}"
            / head_sha[:16]
        )
        artifact_dir.mkdir(parents=True)
        path = artifact_dir / f"20260615T120000Z-{lane_id}-{verdict.lower()}.json"
        body = comment_body
        if body is None:
            body = (
                "Findings: P0=0, P1=1, P2=2, P3=3 "
                "(blocker policy: any P0/P1/P2 -> BLOCKED)"
            )
        payload = {
            "schema": code_mower_telemetry.VERDICT_ARTIFACT_SCHEMA,
            "repo": repo,
            "pr_number": pr_number,
            "lane_id": lane_id,
            "verdict": verdict,
            "created_at": created_at,
            "head_sha_start": head_sha,
            "head_sha_end": head_sha,
            "comment_body": body,
            "trailer": trailer,
        }
        if duration_seconds is not None:
            payload["duration_seconds"] = duration_seconds
        path.write_text(
            json.dumps(
                payload,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return path

    def test_verdict_artifacts_export_metadata_only_reviewer_run_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_artifact(root, duration_seconds=12.3456)

            events = code_mower_telemetry.export_reviewer_run_events_from_verdicts(root)

            self.assertEqual(len(events), 1)
            event = events[0]
            self.assertEqual(event["schema"], "code_mower.benchmarkEvent.v1")
            self.assertEqual(event["event_type"], "reviewer_run")
            self.assertEqual(event["repo_slug"], "owner/repo")
            self.assertEqual(event["provider"], "codex")
            self.assertEqual(event["lens"], "base")
            self.assertEqual(event["status"], "blocked")
            self.assertEqual(event["metrics"]["finding_count"], 6)
            self.assertEqual(event["metrics"]["p1_count"], 1)
            self.assertEqual(event["metrics"]["p2_count"], 2)
            self.assertEqual(event["metrics"]["duration_seconds_total"], 12.346)
            self.assertEqual(event["dimensions"]["lane_id"], "codex-audit")
            self.assertEqual(event["dimensions"]["duration_source"], "verdict_artifact")
            self.assertEqual(event["dimensions"]["audit_comment_lane_id"], "codex-audit")
            self.assertEqual(event["dimensions"]["audit_comment_identity_source"], "trailer")
            self.assertEqual(
                event["dimensions"]["audit_comment_trailer_prefix"],
                "CODEX_AUDIT_STATE",
            )
            self.assertEqual(event["dimensions"]["pr_number"], 42)
            self.assertFalse(event["dimensions"]["git_ref_included"])
            serialized = json.dumps(event, sort_keys=True)
            self.assertNotIn("comment_body", serialized)
            self.assertNotIn("abcdef0123456789", serialized)
            self.assertNotIn("Findings:", serialized)

    def test_verdict_artifacts_export_skips_fixture_shaped_cache_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_artifact(root, pr_number=41)
            self._write_artifact(
                root,
                pr_number=42,
                lane_id="claude-audit",
                comment_body=(
                    "## Claude audit (merge-authority lane)\n\n"
                    "Head SHA: `abcdef0`\n"
                    "Findings: P0=0, P1=1, P2=0, P3=0 "
                    "(blocker policy: any P0/P1/P2 -> BLOCKED)\n\n"
                    "Claude Audit: BLOCKED\n\n"
                    "Summary:\n\n"
                    "test\n\n"
                    "Findings:\n\n"
                    "- [P1] test -- `a.py:1`\n"
                    "  test\n\n"
                    "<!-- CLAUDE_AUDIT_STATE: claude-audit-blocked -->\n"
                ),
                trailer="<!-- CLAUDE_AUDIT_STATE: claude-audit-blocked -->",
            )

            events = code_mower_telemetry.export_reviewer_run_events_from_verdicts(root)

            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["dimensions"]["pr_number"], 41)

    def test_verdict_artifact_export_can_opt_into_git_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_artifact(root)

            events = code_mower_telemetry.export_reviewer_run_events_from_verdicts(
                root,
                include_git_ref=True,
            )

            self.assertTrue(events[0]["dimensions"]["git_ref_included"])
            self.assertEqual(
                events[0]["dimensions"]["head_sha"],
                "abcdef0123456789abcdef0123456789abcdef01",
            )

    def test_verdict_artifact_export_supports_offset_for_backfill_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_artifact(root, pr_number=41, created_at="2026-06-15T12:00:00Z")
            self._write_artifact(root, pr_number=42, created_at="2026-06-15T13:00:00Z")
            self._write_artifact(root, pr_number=43, created_at="2026-06-15T14:00:00Z")

            events = code_mower_telemetry.export_reviewer_run_events_from_verdicts(
                root,
                limit=1,
                offset=1,
            )

            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["dimensions"]["pr_number"], 42)

    def test_verdict_event_id_does_not_depend_on_head_sha_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self._write_artifact(root)
            payload = json.loads(path.read_text(encoding="utf-8"))
            other_payload = dict(payload)
            other_payload["head_sha_start"] = "1111111111111111111111111111111111111111"
            other_payload["head_sha_end"] = "2222222222222222222222222222222222222222"

            first = code_mower_telemetry.reviewer_run_event_from_verdict_artifact(
                payload,
                path=path,
            )
            second = code_mower_telemetry.reviewer_run_event_from_verdict_artifact(
                other_payload,
                path=path,
            )

            self.assertEqual(first["event_id"], second["event_id"])

    def test_verdict_artifact_export_prefers_structured_severity_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self._write_artifact(
                root,
                comment_body="Findings: P0=0, P1=9, P2=9, P3=9",
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["severity_counts"] = {"P1": 1, "p2": 2}
            path.write_text(json.dumps(payload), encoding="utf-8")

            events = code_mower_telemetry.export_reviewer_run_events_from_verdicts(root)

            self.assertEqual(events[0]["metrics"]["finding_count"], 3)
            self.assertEqual(events[0]["metrics"]["p1_count"], 1)
            self.assertEqual(events[0]["metrics"]["p2_count"], 2)
            self.assertEqual(
                events[0]["dimensions"]["severity_count_source"],
                "structured",
            )

    def test_verdict_artifact_export_accumulates_duplicate_severity_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_artifact(
                root,
                comment_body="Findings: P2=1\nMore detail: P2=2, P3=1",
            )

            events = code_mower_telemetry.export_reviewer_run_events_from_verdicts(root)

            self.assertEqual(events[0]["metrics"]["p2_count"], 3)
            self.assertEqual(events[0]["metrics"]["finding_count"], 4)

    def test_verdict_artifact_export_filters_by_repo_and_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_artifact(root, repo="owner/first", pr_number=1)
            self._write_artifact(
                root,
                repo="owner/second",
                pr_number=2,
                created_at="2026-06-15T10:00:00Z",
            )
            self._write_artifact(
                root,
                repo="owner/second",
                pr_number=3,
                created_at="2026-06-15T11:00:00Z",
            )

            events = code_mower_telemetry.export_reviewer_run_events_from_verdicts(
                root,
                repo="owner/second",
                limit=1,
            )

            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["repo_slug"], "owner/second")
            self.assertEqual(events[0]["dimensions"]["pr_number"], 3)

    def test_telemetry_summary_understands_exported_reviewer_run_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_artifact(root)
            events = code_mower_telemetry.export_reviewer_run_events_from_verdicts(root)

            summary = code_mower_telemetry.summarize_events(events)

            self.assertEqual(summary["events_by_type"], {"reviewer_run": 1})
            lane = summary["lanes"]["codex-audit"]
            self.assertEqual(lane["finished"], 1)
            self.assertEqual(lane["blocked"], 1)
            self.assertEqual(lane["findings"], 6)
            self.assertEqual(lane["repositories"], ["owner/repo"])
            self.assertEqual(lane["pull_requests"], ["42"])

    def test_empty_verdict_export_does_not_clobber_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "events.jsonl"
            output.write_text("keep me\n", encoding="utf-8")

            status = code_mower_telemetry.main(
                [
                    "export-verdict-events",
                    str(root / "missing-verdicts"),
                    "--output",
                    str(output),
                ]
            )

            self.assertEqual(status, 0)
            self.assertEqual(output.read_text(encoding="utf-8"), "keep me\n")

    def test_default_verdict_artifact_dir_honors_environment_override(self) -> None:
        old_value = os.environ.get(code_mower_telemetry.VERDICT_ARTIFACT_DIR_ENV)
        try:
            os.environ[code_mower_telemetry.VERDICT_ARTIFACT_DIR_ENV] = "~/custom-verdicts"

            path = code_mower_telemetry.default_verdict_artifact_dir()

            self.assertEqual(path, Path("~/custom-verdicts").expanduser())
        finally:
            if old_value is None:
                os.environ.pop(code_mower_telemetry.VERDICT_ARTIFACT_DIR_ENV, None)
            else:
                os.environ[code_mower_telemetry.VERDICT_ARTIFACT_DIR_ENV] = old_value

    def test_default_verdict_artifact_dir_honors_xdg_cache_home(self) -> None:
        old_artifact_dir = os.environ.get(code_mower_telemetry.VERDICT_ARTIFACT_DIR_ENV)
        old_xdg = os.environ.get("XDG_CACHE_HOME")
        try:
            os.environ.pop(code_mower_telemetry.VERDICT_ARTIFACT_DIR_ENV, None)
            os.environ["XDG_CACHE_HOME"] = "/tmp/code-mower-cache-test"

            path = code_mower_telemetry.default_verdict_artifact_dir()

            self.assertEqual(
                path,
                Path("/tmp/code-mower-cache-test/code-mower-audits/verdicts"),
            )
        finally:
            if old_artifact_dir is None:
                os.environ.pop(code_mower_telemetry.VERDICT_ARTIFACT_DIR_ENV, None)
            else:
                os.environ[code_mower_telemetry.VERDICT_ARTIFACT_DIR_ENV] = (
                    old_artifact_dir
                )
            if old_xdg is None:
                os.environ.pop("XDG_CACHE_HOME", None)
            else:
                os.environ["XDG_CACHE_HOME"] = old_xdg

    def test_quarantine_dir_defaults_next_to_pinned_verdict_dir(self) -> None:
        old_artifact_dir = os.environ.get(code_mower_telemetry.VERDICT_ARTIFACT_DIR_ENV)
        old_quarantine_dir = os.environ.get("CODE_MOWER_VERDICT_QUARANTINE_DIR")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                verdict_dir = Path(tmp) / "state" / "verdicts"
                os.environ[code_mower_telemetry.VERDICT_ARTIFACT_DIR_ENV] = str(verdict_dir)
                os.environ.pop("CODE_MOWER_VERDICT_QUARANTINE_DIR", None)

                path = write_audit_verdict_artifact(
                    lane_id="claude-audit",
                    repo="owner/repo",
                    pr_number=42,
                    head_sha_start="a" * 40,
                    head_sha_end="a" * 40,
                    verdict="UNKNOWN",
                    trailer="<!-- CLAUDE_AUDIT_STATE: needs-claude-audit -->",
                    comment_body="requeued",
                    quarantine_reason="fixture-shaped audit verdict comment",
                )

                self.assertIsNotNone(path)
                assert path is not None
                self.assertTrue(
                    path.is_relative_to(Path(tmp) / "state" / "quarantine" / "verdicts")
                )
        finally:
            if old_artifact_dir is None:
                os.environ.pop(code_mower_telemetry.VERDICT_ARTIFACT_DIR_ENV, None)
            else:
                os.environ[code_mower_telemetry.VERDICT_ARTIFACT_DIR_ENV] = (
                    old_artifact_dir
                )
            if old_quarantine_dir is None:
                os.environ.pop("CODE_MOWER_VERDICT_QUARANTINE_DIR", None)
            else:
                os.environ["CODE_MOWER_VERDICT_QUARANTINE_DIR"] = old_quarantine_dir

    def test_repost_verdict_artifact_binds_actions_run_comment_marker(self) -> None:
        old_artifact_dir = os.environ.get(code_mower_telemetry.VERDICT_ARTIFACT_DIR_ENV)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.environ[code_mower_telemetry.VERDICT_ARTIFACT_DIR_ENV] = str(
                    Path(tmp) / "verdicts"
                )
                path = write_audit_verdict_artifact(
                    lane_id="claude-audit",
                    repo="owner/repo",
                    pr_number=42,
                    head_sha_start="a" * 40,
                    head_sha_end="a" * 40,
                    verdict="PASS",
                    trailer="<!-- CLAUDE_AUDIT_STATE: claude-audit-done -->",
                    comment_body=(
                        "<!-- CODE_MOWER_AUDIT_RUN: run_id=12345 -->\n"
                        "<!-- CLAUDE_AUDIT_STATE: claude-audit-done -->"
                    ),
                )
                assert path is not None

                with (
                    mock.patch.object(
                        verdict_artifacts,
                        "post_pr_comment",
                        return_value={"id": 67890, "html_url": "https://github.test/c/1"},
                    ) as post_comment,
                    mock.patch.object(
                        verdict_artifacts,
                        "edit_pr_comment",
                        return_value={"id": 67890, "html_url": "https://github.test/c/1"},
                    ) as edit_comment,
                ):
                    posted = verdict_artifacts.repost_audit_verdict_artifact(
                        path,
                        token="token",
                    )

                self.assertEqual(posted["html_url"], "https://github.test/c/1")
                post_comment.assert_called_once()
                edit_comment.assert_called_once()
                edited_body = edit_comment.call_args.args[2]
                self.assertRegex(
                    edited_body,
                    r"<!-- CODE_MOWER_AUDIT_RUN: run_id=12345 "
                    r"comment_id=67890 body_sha256=[0-9a-f]{64} -->",
                )
        finally:
            if old_artifact_dir is None:
                os.environ.pop(code_mower_telemetry.VERDICT_ARTIFACT_DIR_ENV, None)
            else:
                os.environ[code_mower_telemetry.VERDICT_ARTIFACT_DIR_ENV] = (
                    old_artifact_dir
                )

    def test_cloud_reviewer_runs_builds_dry_run_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            verdicts = root / "verdicts"
            self._write_artifact(verdicts)
            output_dir = root / "bundle"

            result = code_mower_cloud._reviewer_runs_upload(
                repo_path=root,
                verdicts=verdicts,
                output_dir=output_dir,
                repo_slug="owner/repo",
                team_id="team-test",
                install_id="install-test",
                limit=10,
                endpoint="https://codemower.com/api/ingest",
                token_env="MISSING_TOKEN_FOR_TEST",
                yes=False,
                timeout=1,
                include_git_ref=False,
            )

            self.assertEqual(result["status"], "dry_run")
            self.assertEqual(result["mode"], "cloud-reviewer-runs")
            self.assertEqual(result["event_count"], 1)
            self.assertEqual(result["export"]["event_count"], 1)
            self.assertEqual(result["upload"]["event_count"], 1)

    def test_cloud_reviewer_runs_offset_chunks_bundle_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            verdicts = root / "verdicts"
            self._write_artifact(verdicts, pr_number=41, created_at="2026-06-15T12:00:00Z")
            self._write_artifact(verdicts, pr_number=42, created_at="2026-06-15T13:00:00Z")
            self._write_artifact(verdicts, pr_number=43, created_at="2026-06-15T14:00:00Z")
            output_dir = root / "bundle"

            result = code_mower_cloud._reviewer_runs_upload(
                repo_path=root,
                verdicts=verdicts,
                output_dir=output_dir,
                repo_slug="owner/repo",
                team_id="team-test",
                install_id="install-test",
                limit=1,
                offset=1,
                endpoint="https://codemower.com/api/ingest",
                token_env="MISSING_TOKEN_FOR_TEST",
                yes=False,
                timeout=1,
                include_git_ref=False,
            )

            payload = code_mower_cloud.build_upload_payload(bundle_dir=output_dir)
            self.assertEqual(result["status"], "dry_run")
            self.assertEqual(result["event_count"], 1)
            self.assertEqual(result["offset"], 1)
            self.assertEqual(payload["events"][0]["dimensions"]["pr_number"], 42)

    def test_cloud_reviewer_runs_reports_no_events_without_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "bundle"

            result = code_mower_cloud._reviewer_runs_upload(
                repo_path=root,
                verdicts=root / "missing-verdicts",
                output_dir=output_dir,
                repo_slug="owner/repo",
                team_id="",
                install_id="",
                limit=10,
                endpoint="https://codemower.com/api/ingest",
                token_env="MISSING_TOKEN_FOR_TEST",
                yes=False,
                timeout=1,
                include_git_ref=False,
            )

            self.assertEqual(result["status"], "no_events")
            self.assertFalse(output_dir.exists())

    def test_cloud_reviewer_runs_converts_invalid_artifacts_to_cloud_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            verdicts = root / "verdicts"
            verdicts.mkdir()
            (verdicts / "bad.json").write_text("[not-json", encoding="utf-8")

            with self.assertRaises(code_mower_cloud.CloudBundleError):
                code_mower_cloud._reviewer_runs_upload(
                    repo_path=root,
                    verdicts=verdicts,
                    output_dir=root / "bundle",
                    repo_slug="owner/repo",
                    team_id="",
                    install_id="",
                    limit=10,
                    endpoint="https://codemower.com/api/ingest",
                    token_env="MISSING_TOKEN_FOR_TEST",
                    yes=False,
                    timeout=1,
                    include_git_ref=False,
                )


if __name__ == "__main__":
    unittest.main()
