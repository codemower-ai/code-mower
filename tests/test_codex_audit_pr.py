from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from code_mower import codex_audit_pr as cap


class CodexAuditPrTests(unittest.TestCase):
    def _run_mocked_audit(
        self,
        *,
        tmp_path: Path,
        pytest_current_test: str,
        parsed: cap.CodexVerdict | None = None,
    ) -> tuple[cap.AuditResult, mock.Mock]:
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        head_sha = "d" * 40
        pr_payload = {"head": {"sha": head_sha, "ref": "human/fix"}, "title": "Fix"}
        parsed = parsed or cap.CodexVerdict(
            verdict="PASS",
            prose="Summary:\n\nNo merge-blocking regressions found.\n\nFindings: none.",
        )
        config = cap.AuditConfig(
            "token",
            {"owner/repo": repo},
            include_plan_context=False,
        )

        with (
            mock.patch.dict(
                "os.environ",
                {
                    "PYTEST_CURRENT_TEST": pytest_current_test,
                    "CODE_MOWER_VERDICT_ARTIFACT_DIR": str(tmp_path / "verdicts"),
                    "GITHUB_RUN_ID": "",
                },
            ),
            mock.patch.object(cap, "fetch_pull_request", side_effect=[pr_payload, pr_payload]),
            mock.patch.object(cap, "preflight_codex_cli", return_value="codex-test"),
            mock.patch.object(cap, "_discover_venv", return_value=None),
            mock.patch.object(cap, "_fetch_pr_head"),
            mock.patch.object(cap, "_fetch_base_ref"),
            mock.patch.object(
                cap,
                "_build_review_context_diagnostics",
                return_value=mock.Mock(summary=lambda: "review context ok"),
            ),
            mock.patch.object(cap, "_create_temp_worktree", return_value=worktree),
            mock.patch.object(cap, "_remove_worktree"),
            mock.patch.object(cap, "run_codex_review", return_value=("review text", "")),
            mock.patch.object(
                cap,
                "run_codex_verdict_structuring",
                return_value=(parsed, '{"structured_output":"pass"}', ""),
            ),
            mock.patch.object(cap, "dump_cli_failure", return_value=tmp_path / "cli.log"),
            mock.patch.object(
                cap,
                "post_pr_comment",
                return_value={"html_url": "https://github.test/comment/1"},
            ) as post_comment,
        ):
            result = cap.audit_pr(config, "owner/repo", 42)
        return result, post_comment

    def test_codex_audit_posts_in_non_pytest_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, post_comment = self._run_mocked_audit(
                tmp_path=Path(tmp),
                pytest_current_test="",
            )

            self.assertEqual(result.verdict, "PASS")
            self.assertEqual(result.posted_comment_url, "https://github.test/comment/1")
            post_comment.assert_called_once()
            self.assertIsNotNone(result.verdict_artifact_path)
            assert result.verdict_artifact_path is not None
            artifact = json.loads(result.verdict_artifact_path.read_text(encoding="utf-8"))
            self.assertNotIn("quarantined", artifact)
            self.assertIn("duration_seconds", artifact)
            self.assertGreaterEqual(artifact["duration_seconds"], 0)

    def test_codex_audit_pytest_runtime_quarantines_without_posting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, post_comment = self._run_mocked_audit(
                tmp_path=Path(tmp),
                pytest_current_test="tests/test_live_guard.py::test_guard",
            )

            self.assertEqual(result.verdict, "PASS")
            self.assertIsNone(result.posted_comment_url)
            post_comment.assert_not_called()
            self.assertIsNotNone(result.verdict_artifact_path)
            assert result.verdict_artifact_path is not None
            self.assertIn("quarantine", str(result.verdict_artifact_path))
            artifact = json.loads(result.verdict_artifact_path.read_text(encoding="utf-8"))
            self.assertTrue(artifact["quarantined"])
            self.assertIn("PYTEST_CURRENT_TEST", artifact["quarantine_reason"])

    def test_codex_structured_fixture_pass_quarantines(self) -> None:
        fixture = cap.parse_structured_codex_verdict(
            {
                "schema": cap.CODEX_AUDIT_SCHEMA_ID,
                "verdict": "pass",
                "summary": "test",
                "findings": [
                    {
                        "severity": "P3",
                        "title": "test",
                        "file": "a.py",
                        "line": 1,
                        "detail": "test",
                    }
                ],
            }
        )

        self.assertEqual(fixture.verdict, "UNKNOWN")
        self.assertEqual(fixture.p3_count, 1)
        self.assertEqual(fixture.quarantine_reason, "fixture-shaped structured verdict")

    def test_codex_audit_fixture_verdict_quarantines_as_unknown(self) -> None:
        fixture = cap.parse_structured_codex_verdict(
            {
                "schema": cap.CODEX_AUDIT_SCHEMA_ID,
                "verdict": "blocked",
                "summary": "test",
                "findings": [
                    {
                        "severity": "P1",
                        "title": "test",
                        "file": "a.py",
                        "line": 1,
                        "detail": "test",
                    }
                ],
            }
        )
        self.assertEqual(fixture.verdict, "UNKNOWN")
        self.assertEqual(fixture.quarantine_reason, "fixture-shaped structured verdict")
        with tempfile.TemporaryDirectory() as tmp:
            result, post_comment = self._run_mocked_audit(
                tmp_path=Path(tmp),
                pytest_current_test="",
                parsed=fixture,
            )

            self.assertEqual(result.verdict, "UNKNOWN")
            self.assertEqual(result.posted_comment_url, "https://github.test/comment/1")
            post_comment.assert_called_once()
            self.assertIn("Runtime quarantine:", result.comment_body)
            self.assertIn("fixture-shaped structured verdict", result.comment_body)
            self.assertIsNotNone(result.verdict_artifact_path)
            assert result.verdict_artifact_path is not None
            self.assertIn("quarantine", str(result.verdict_artifact_path))
            artifact = json.loads(result.verdict_artifact_path.read_text(encoding="utf-8"))
            self.assertTrue(artifact["quarantined"])
            self.assertIn("fixture-shaped structured verdict", artifact["quarantine_reason"])

    def test_workflow_audit_comment_is_edited_with_bound_marker(self) -> None:
        body = (
            "## Codex audit\n"
            "<!-- CODE_MOWER_AUDIT_RUN: run_id=12345 -->\n"
            "<!-- CODEX_AUDIT_STATE: codex-audit-done -->"
        )
        with (
            mock.patch.object(
                cap,
                "post_pr_comment",
                return_value={"id": 67890, "html_url": "https://github.test/comment/1"},
            ) as post_comment,
            mock.patch.object(
                cap,
                "edit_pr_comment",
                return_value={"id": 67890, "html_url": "https://github.test/comment/1"},
            ) as edit_comment,
        ):
            posted, bound_body = cap._post_audit_comment(
                "owner/repo",
                42,
                body,
                token="token",
                actions_run_id="12345",
            )

        self.assertEqual(posted["html_url"], "https://github.test/comment/1")
        post_comment.assert_called_once()
        edit_comment.assert_called_once()
        self.assertRegex(
            bound_body,
            r"<!-- CODE_MOWER_AUDIT_RUN: run_id=12345 "
            r"comment_id=67890 body_sha256=[0-9a-f]{64} -->",
        )

    def test_audit_exit_code_keeps_stale_neutral_and_unknown_loud(self) -> None:
        self.assertEqual(cap._audit_exit_code("STALE"), 0)
        self.assertEqual(cap._audit_exit_code("stale"), 0)
        self.assertEqual(cap._audit_exit_code("PASS"), 0)
        self.assertEqual(cap._audit_exit_code("UNKNOWN"), 2)
        self.assertEqual(cap._audit_exit_code("unknown"), 2)

    def test_comment_header_separates_authority_from_calibration_badge(self) -> None:
        body = cap.format_comment(
            cap.CodexVerdict(
                verdict="PASS",
                prose="No merge-blocking regressions found.",
            ),
            "a" * 40,
            merge_authority=True,
            calibration_badge=" calibration phase - informational only\nfor CM-1 ",
        )

        first_line = body.splitlines()[0]
        self.assertEqual(first_line, "## Codex audit (merge-authority lane)")
        self.assertNotIn("calibration phase", first_line)
        self.assertIn("Calibration: calibration phase - informational only for CM-1", body)

    def test_requeue_comments_include_machine_readable_kind_markers(self) -> None:
        unknown = cap.format_comment(
            cap.CodexVerdict(verdict="UNKNOWN", prose="untrusted"),
            "a" * 40,
            is_unknown=True,
        )
        stale = cap.format_comment(
            cap.CodexVerdict(verdict="UNKNOWN", prose="stale"),
            "a" * 40,
            is_stale=True,
            stale_end_sha="b" * 40,
        )

        self.assertIn(cap.UNKNOWN_REQUEUE_MARKER, unknown)
        self.assertIn(cap.STALE_REQUEUE_MARKER, stale)


if __name__ == "__main__":
    unittest.main()
