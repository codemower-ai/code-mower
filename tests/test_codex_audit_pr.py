from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest import mock

from code_mower import codex_audit_pr as cap


class CodexAuditPrTests(unittest.TestCase):
    def test_main_missing_repo_paths_error_links_local_audit_docs(self) -> None:
        stderr = StringIO()
        with (
            mock.patch.object(cap, "_resolve_github_token", return_value="token"),
            redirect_stderr(stderr),
        ):
            code = cap.main(["--repo", "owner/repo", "--pr", "42"])

        self.assertEqual(code, 1)
        self.assertIn("--repo-paths or CODEX_AUDIT_REPO_PATHS is required", stderr.getvalue())
        self.assertIn("OWNER/REPO:/absolute/path", stderr.getvalue())
        self.assertIn("docs/local-audit-runner.md", stderr.getvalue())

    def test_main_rejects_current_directory_repo_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            stderr = StringIO()
            with (
                mock.patch.object(cap, "_resolve_github_token", return_value="token"),
                mock.patch("pathlib.Path.cwd", return_value=repo),
                redirect_stderr(stderr),
            ):
                code = cap.main(
                    [
                        "--repo",
                        "owner/repo",
                        "--pr",
                        "42",
                        "--repo-paths",
                        f"owner/repo:{repo}",
                    ]
                )

        self.assertEqual(code, 1)
        self.assertIn("separate PR-head checkout", stderr.getvalue())
        self.assertIn("docs/local-audit-runner.md", stderr.getvalue())

    def _run_mocked_audit(
        self,
        *,
        tmp_path: Path,
        pytest_current_test: str,
        parsed: cap.CodexVerdict | None = None,
        review_context: cap.ReviewContextDiagnostics | None = None,
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
            include_decision_context=False,
        )
        review_context = review_context or cap.ReviewContextDiagnostics(
            base_ref=config.base_ref,
            head_sha=head_sha,
            changed_file_count=1,
            diff_bytes=128,
            requested_max_bytes=config.max_diff_bytes,
            hard_limit_bytes=(
                config.max_diff_hard_limit_bytes
                or cap.DEFAULT_MAX_DIFF_HARD_LIMIT_BYTES
            ),
            included_diff_bytes=128,
            effective_budget_usd=config.max_budget_usd or cap.DEFAULT_MAX_BUDGET_USD,
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
                return_value=review_context,
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

    def test_codex_structured_prose_includes_stable_finding_id(self) -> None:
        title = "Unsafe auth bypass"
        parsed = cap.parse_structured_codex_verdict(
            {
                "schema": cap.CODEX_AUDIT_SCHEMA_ID,
                "verdict": "blocked",
                "summary": "Auth bypass in real code.",
                "findings": [
                    {
                        "severity": "P1",
                        "title": title,
                        "file": "src/auth.py",
                        "line": 17,
                        "detail": "The bypass accepts untrusted requests.",
                    }
                ],
            }
        )

        self.assertEqual(parsed.verdict, "BLOCKED")
        self.assertIn(
            cap.code_mower_decisions.stable_finding_id(
                "codex",
                title,
                "src/auth.py",
            ),
            parsed.prose,
        )
        findings = cap.code_mower_decisions.extract_audit_findings(
            parsed.prose,
            lane="codex",
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].title, title)

    def test_codex_truncated_diff_posts_unknown_without_running_codex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            worktree = tmp_path / "worktree"
            worktree.mkdir()
            head_sha = "d" * 40
            pr_payload = {
                "head": {"sha": head_sha, "ref": "human/fix"},
                "title": "Fix",
            }
            review_context = cap.ReviewContextDiagnostics(
                base_ref="origin/main",
                head_sha=head_sha,
                changed_file_count=2,
                diff_bytes=151,
                requested_max_bytes=100,
                hard_limit_bytes=150,
                included_diff_bytes=150,
                effective_budget_usd="3.00",
            )
            config = cap.AuditConfig(
                "token",
                {"owner/repo": repo},
                include_plan_context=False,
                include_decision_context=False,
                max_diff_bytes=100,
                max_diff_hard_limit_bytes=150,
            )

            with (
                mock.patch.dict(
                    "os.environ",
                    {
                        "PYTEST_CURRENT_TEST": "",
                        "CODE_MOWER_VERDICT_ARTIFACT_DIR": str(tmp_path / "verdicts"),
                        "GITHUB_RUN_ID": "",
                    },
                ),
                mock.patch.object(
                    cap,
                    "fetch_pull_request",
                    side_effect=[pr_payload, pr_payload],
                ),
                mock.patch.object(cap, "preflight_codex_cli", return_value="codex-test"),
                mock.patch.object(cap, "_discover_venv", return_value=None),
                mock.patch.object(cap, "_fetch_pr_head"),
                mock.patch.object(cap, "_fetch_base_ref"),
                mock.patch.object(
                    cap,
                    "_build_review_context_diagnostics",
                    return_value=review_context,
                ),
                mock.patch.object(
                    cap,
                    "_create_temp_worktree",
                    return_value=worktree,
                ) as create_worktree,
                mock.patch.object(cap, "_remove_worktree") as remove_worktree,
                mock.patch.object(
                    cap,
                    "run_codex_review",
                    return_value=("review text", ""),
                ) as run_review,
                mock.patch.object(
                    cap,
                    "run_codex_verdict_structuring",
                ) as structure_verdict,
                mock.patch.object(
                    cap,
                    "dump_cli_failure",
                    return_value=tmp_path / "cli.log",
                ) as dump_failure,
                mock.patch.object(
                    cap,
                    "post_pr_comment",
                    return_value={"html_url": "https://github.test/comment/1"},
                ) as post_comment,
            ):
                result = cap.audit_pr(config, "owner/repo", 42)

            self.assertEqual(result.verdict, "UNKNOWN")
            self.assertEqual(result.trailer, cap.STALE_TRAILER)
            self.assertEqual(result.posted_comment_url, "https://github.test/comment/1")
            self.assertIn(cap.UNKNOWN_REQUEUE_MARKER, result.comment_body)
            self.assertIn("Diff: exceeded wrapper hard limit", result.comment_body)
            self.assertIn(
                "measured at least 151 bytes; hard limit 150 bytes",
                result.comment_body,
            )
            self.assertIn("Codex review skipped", result.codex_stderr)
            self.assertIn("wrapper_truncated=yes", result.codex_stderr)
            create_worktree.assert_not_called()
            remove_worktree.assert_not_called()
            run_review.assert_not_called()
            structure_verdict.assert_not_called()
            dump_failure.assert_called_once()
            post_comment.assert_called_once()

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

    def test_codex_review_context_includes_decision_registry(self) -> None:
        comments = [
            {
                "author_association": "MEMBER",
                "user": {"login": "owner"},
                "body": (
                    '<!-- CODE_MOWER_DECISION: id=ADR-007 scope=finding '
                    'finding_id="codex:b93829375d1f7c3d27fa" by=owner ref=ADR-007 -->'
                ),
            }
        ]
        with mock.patch.object(cap, "fetch_issue_comments", return_value=comments):
            registry = cap._decision_registry_context(
                "owner/repo",
                42,
                token="token",
                authorities=("owner",),
            )

        prompt = cap._codex_review_context_prompt(None, registry)

        self.assertIn("Trusted Code Mower decision registry", prompt)
        self.assertIn("ADR-007", prompt)
        self.assertIn("codex:b93829375d1f7c3d27fa", prompt)

    def test_decision_registry_context_fetch_failure_degrades_to_empty(self) -> None:
        with mock.patch.object(
            cap,
            "fetch_issue_comments",
            side_effect=RuntimeError("pagination cap"),
        ):
            err = io.StringIO()
            with redirect_stderr(err):
                registry = cap._decision_registry_context(
                    "owner/repo",
                    42,
                    token="token",
                )

        self.assertEqual(registry, "")
        self.assertIn("decision registry: skipped", err.getvalue())

    def test_decision_authorities_load_from_trusted_ref_not_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            repo.joinpath("code-mower.yml").write_text(
                "owner_surface:\n  owner_login: attacker\n",
                encoding="utf-8",
            )
            with (
                mock.patch.object(
                    cap,
                    "_run_git_text",
                    return_value="owner_surface:\n  owner_login: owner\n",
                ) as run_git,
                mock.patch.object(
                    cap.code_mower_decisions,
                    "decision_authorities_from_env",
                    return_value=(),
                ),
            ):
                authorities = cap._decision_authorities_for_repo(
                    repo,
                    ("configured",),
                    trusted_ref="origin/main",
                )

        self.assertEqual(authorities, ("configured", "owner"))
        run_git.assert_called_once_with(repo, ["show", "origin/main:code-mower.yml"])


if __name__ == "__main__":
    unittest.main()
