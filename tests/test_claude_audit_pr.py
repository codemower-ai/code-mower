from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from code_mower import claude_audit_pr as cap


def _finding(**overrides: object) -> dict[str, object]:
    return {
        "severity": "P1",
        "title": "Real blocker",
        "file": "src/app.py",
        "line": 12,
        "detail": "This explains a concrete regression with enough context.",
        **overrides,
    }


def _payload(**overrides: object) -> dict[str, object]:
    return {
        "schema": cap.CLAUDE_AUDIT_SCHEMA_ID,
        "verdict": "blocked",
        "summary": "Real audit summary with enough detail.",
        "findings": [_finding()],
        **overrides,
    }


class ClaudeAuditPrTests(unittest.TestCase):
    def test_claude_guardrails_reject_untrusted_verdicts(self) -> None:
        cases = [
            (
                cap.parse_structured_claude_verdict(_payload(summary="test")),
                ("src/app.py",),
                "structured verdict summary matched a schema placeholder value",
            ),
            (
                cap.parse_structured_claude_verdict(
                    _payload(findings=[_finding(file="a.txt")])
                ),
                ("src/app.py",),
                "is not present in the PR diff",
            ),
            (
                cap.ClaudeVerdict(
                    verdict="BLOCKED",
                    prose="too short",
                    findings=(
                        {"title": "Real title", "file": "src/app.py", "detail": "Real"},
                    ),
                ),
                ("src/app.py",),
                "structured blocked verdict body is too short to be credible",
            ),
        ]

        for parsed, changed_files, expected in cases:
            with self.subTest(expected=expected):
                guarded, reason = cap._apply_claude_verdict_guardrails(
                    parsed,
                    changed_files,
                )
                self.assertEqual(guarded.verdict, "UNKNOWN")
                self.assertIn(expected, str(reason))

    def test_claude_guardrails_accept_path_variants_and_p3_misses(self) -> None:
        parsed = cap.parse_structured_claude_verdict(
            _payload(
                findings=[
                    _finding(file="b/src/app.py"),
                    _finding(severity="P3", file="notes.md"),
                ]
            )
        )

        guarded, reason = cap._apply_claude_verdict_guardrails(parsed, ("src/app.py",))

        self.assertEqual(guarded.verdict, "BLOCKED")
        self.assertIsNone(reason)

    def test_claude_audit_retries_guardrail_rejection_and_updates_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            head_sha = "a" * 40
            pr_payload = {"head": {"sha": head_sha, "ref": "human/fix"}, "title": "Fix"}
            placeholder = cap.parse_structured_claude_verdict(
                _payload(summary="test", findings=[_finding(title="test")])
            )
            clean = cap.parse_structured_claude_verdict(
                {
                    "schema": cap.CLAUDE_AUDIT_SCHEMA_ID,
                    "verdict": "pass",
                    "summary": "No merge-blocking regressions found in this review.",
                    "findings": [],
                }
            )
            diff_context = cap.DiffContext(
                "src/app.py | 1 +",
                "diff --git a/src/app.py b/src/app.py",
                ("src/app.py",),
                False,
                1_000,
                1_000,
                40,
                40,
            )
            config = cap.ClaudeAuditConfig(
                "token",
                {"owner/repo": repo},
                include_plan_context=False,
                include_decision_context=False,
            )

            with (
                mock.patch.dict(
                    "os.environ",
                    {
                        "CODE_MOWER_VERDICT_ARTIFACT_DIR": str(tmp_path / "verdicts"),
                        "PYTEST_CURRENT_TEST": "",
                        "GITHUB_RUN_ID": "",
                    },
                ),
                mock.patch.object(
                    cap,
                    "fetch_pull_request",
                    side_effect=[pr_payload, pr_payload],
                ),
                mock.patch.object(cap, "_build_diff_context", return_value=diff_context),
                mock.patch.object(
                    cap.code_mower_prompts,
                    "load_review_prompt",
                    return_value="",
                ),
                mock.patch.object(
                    cap,
                    "run_claude_audit",
                    side_effect=[
                        (placeholder, '{"structured_output":"placeholder"}', ""),
                        (clean, '{"structured_output":"pass"}', ""),
                    ],
                ) as run_claude,
                mock.patch.object(
                    cap,
                    "post_pr_comment",
                    return_value={"html_url": "https://github.test/comment/1"},
                ),
            ):
                result = cap.audit_pr(config, "owner/repo", 42)

            self.assertEqual(result.verdict, "PASS")
            self.assertEqual(run_claude.call_count, 2)
            self.assertIn(
                "Trusted wrapper retry instruction",
                run_claude.call_args_list[1].args[1],
            )
            self.assertEqual(result.posted_comment_url, "https://github.test/comment/1")
            self.assertIsNotNone(result.verdict_artifact_path)
            assert result.verdict_artifact_path is not None

            artifact = json.loads(result.verdict_artifact_path.read_text(encoding="utf-8"))
            self.assertEqual(
                artifact["posted_comment_url"],
                "https://github.test/comment/1",
            )
            self.assertIn("duration_seconds", artifact)
            self.assertGreaterEqual(artifact["duration_seconds"], 0)

            sidecar = result.verdict_artifact_path.with_suffix(".claude-raw-output.json")
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], cap.CLAUDE_RAW_OUTPUT_SCHEMA)
            self.assertTrue(payload["attempts"][0]["guardrail_rejection"])
            self.assertIsNone(payload["attempts"][1]["guardrail_rejection"])

    def test_claude_audit_pytest_runtime_quarantines_without_posting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            head_sha = "b" * 40
            pr_payload = {"head": {"sha": head_sha, "ref": "human/fix"}, "title": "Fix"}
            clean = cap.parse_structured_claude_verdict(
                {
                    "schema": cap.CLAUDE_AUDIT_SCHEMA_ID,
                    "verdict": "pass",
                    "summary": "No merge-blocking regressions found in this review.",
                    "findings": [],
                }
            )
            diff_context = cap.DiffContext(
                "src/app.py | 1 +",
                "diff --git a/src/app.py b/src/app.py",
                ("src/app.py",),
                False,
                1_000,
                1_000,
                40,
                40,
            )
            config = cap.ClaudeAuditConfig(
                "token",
                {"owner/repo": repo},
                include_plan_context=False,
                include_decision_context=False,
            )

            with (
                mock.patch.dict(
                    "os.environ",
                    {
                        "PYTEST_CURRENT_TEST": "tests/test_live_guard.py::test_guard",
                        "CODE_MOWER_VERDICT_ARTIFACT_DIR": str(tmp_path / "verdicts"),
                        "CODE_MOWER_VERDICT_QUARANTINE_DIR": str(tmp_path / "quarantine"),
                        "GITHUB_RUN_ID": "",
                    },
                ),
                mock.patch.object(
                    cap,
                    "fetch_pull_request",
                    side_effect=[pr_payload, pr_payload],
                ),
                mock.patch.object(cap, "_build_diff_context", return_value=diff_context),
                mock.patch.object(
                    cap.code_mower_prompts,
                    "load_review_prompt",
                    return_value="",
                ),
                mock.patch.object(
                    cap,
                    "run_claude_audit",
                    return_value=(clean, '{"structured_output":"pass"}', ""),
                ),
                mock.patch.object(cap, "post_pr_comment") as post_comment,
            ):
                result = cap.audit_pr(config, "owner/repo", 42)

            self.assertEqual(result.verdict, "PASS")
            post_comment.assert_not_called()
            self.assertIsNone(result.posted_comment_url)
            self.assertIsNotNone(result.verdict_artifact_path)
            assert result.verdict_artifact_path is not None
            self.assertIn("quarantine", str(result.verdict_artifact_path))
            artifact = json.loads(result.verdict_artifact_path.read_text(encoding="utf-8"))
            self.assertTrue(artifact["quarantined"])
            self.assertIn("PYTEST_CURRENT_TEST", artifact["quarantine_reason"])

    def test_claude_audit_fixture_verdict_quarantines_as_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            head_sha = "c" * 40
            pr_payload = {"head": {"sha": head_sha, "ref": "human/fix"}, "title": "Fix"}
            placeholder = cap.parse_structured_claude_verdict(
                _payload(summary="test", findings=[_finding(title="test", file="a.py")])
            )
            diff_context = cap.DiffContext(
                "a.py | 1 +",
                "diff --git a/a.py b/a.py",
                ("a.py",),
                False,
                1_000,
                1_000,
                40,
                40,
            )
            config = cap.ClaudeAuditConfig(
                "token",
                {"owner/repo": repo},
                include_plan_context=False,
                include_decision_context=False,
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
                mock.patch.object(cap, "_build_diff_context", return_value=diff_context),
                mock.patch.object(
                    cap.code_mower_prompts,
                    "load_review_prompt",
                    return_value="",
                ),
                mock.patch.object(
                    cap,
                    "run_claude_audit",
                    return_value=(placeholder, '{"structured_output":"placeholder"}', ""),
                ) as run_claude,
                mock.patch.object(
                    cap,
                    "post_pr_comment",
                    return_value={"html_url": "https://github.test/comment/1"},
                ) as post_comment,
            ):
                result = cap.audit_pr(config, "owner/repo", 42)

            self.assertEqual(result.verdict, "UNKNOWN")
            self.assertEqual(run_claude.call_count, cap.MAX_CLAUDE_AUDIT_ATTEMPTS)
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

    def test_claude_cli_failure_reason_sanitizes_login_json(self) -> None:
        raw = json.dumps(
            {
                "is_error": True,
                "result": "Not logged in · Please run /login",
                "terminal_reason": "api_error",
                "api_error_status": 401,
            }
        )

        self.assertEqual(cap._claude_cli_failure_reason(raw, ""), "Claude CLI: Not logged in")

    def test_claude_unknown_captures_cli_failure_and_comments_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            head_sha = "d" * 40
            pr_payload = {"head": {"sha": head_sha, "ref": "human/fix"}, "title": "Fix"}
            diff_context = cap.DiffContext(
                "src/app.py | 1 +",
                "diff --git a/src/app.py b/src/app.py",
                ("src/app.py",),
                False,
                1_000,
                1_000,
                40,
                40,
            )
            cli_json = json.dumps(
                {
                    "is_error": True,
                    "result": "Not logged in · Please run /login",
                    "terminal_reason": "auth",
                    "api_error_status": 401,
                }
            )
            config = cap.ClaudeAuditConfig(
                "token",
                {"owner/repo": repo},
                include_plan_context=False,
                include_decision_context=False,
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
                mock.patch.object(cap, "DEFAULT_CLAUDE_CLI_FAILURE_DIR", tmp_path / "failures"),
                mock.patch.object(
                    cap,
                    "fetch_pull_request",
                    side_effect=[pr_payload, pr_payload],
                ),
                mock.patch.object(cap, "_build_diff_context", return_value=diff_context),
                mock.patch.object(
                    cap.code_mower_prompts,
                    "load_review_prompt",
                    return_value="",
                ),
                mock.patch.object(
                    cap,
                    "run_claude_audit",
                    return_value=(cap._unknown_structured_verdict("Claude CLI exited 1"), cli_json, ""),
                ),
                mock.patch.object(
                    cap,
                    "post_pr_comment",
                    return_value={"html_url": "https://github.test/comment/1"},
                ),
            ):
                result = cap.audit_pr(config, "owner/repo", 42)

            self.assertEqual(result.verdict, "UNKNOWN")
            self.assertIn("Claude CLI: Not logged in", result.comment_body)
            dumps = list((tmp_path / "failures").glob("*_claude.log"))
            self.assertEqual(len(dumps), 1)
            dump_text = dumps[0].read_text(encoding="utf-8")
            self.assertIn("# claude_api_error_status: 401", dump_text)
            self.assertIn("Not logged in", dump_text)

    def test_claude_audit_uses_size_aware_default_budget_for_large_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            head_sha = "e" * 40
            pr_payload = {"head": {"sha": head_sha, "ref": "human/large"}, "title": "Fix"}
            clean = cap.parse_structured_claude_verdict(
                {
                    "schema": cap.CLAUDE_AUDIT_SCHEMA_ID,
                    "verdict": "pass",
                    "summary": "No merge-blocking regressions found in this review.",
                    "findings": [],
                }
            )
            diff_context = cap.DiffContext(
                "src/large.py | 1000 +",
                "diff --git a/src/large.py b/src/large.py\n" + ("+" * 999_950),
                ("src/large.py",),
                False,
                cap.DEFAULT_MAX_DIFF_BYTES,
                cap.DEFAULT_MAX_DIFF_HARD_LIMIT_BYTES,
                1_000_000,
                1_000_000,
                True,
            )
            config = cap.ClaudeAuditConfig(
                "token",
                {"owner/repo": repo},
                include_plan_context=False,
                include_decision_context=False,
            )

            def fake_run(
                run_config: cap.ClaudeAuditConfig,
                _prompt: str,
            ) -> tuple[cap.ClaudeVerdict, str, str]:
                self.assertEqual(run_config.max_budget_usd, "8.00")
                return clean, '{"structured_output":"pass"}', ""

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
                mock.patch.object(cap, "_build_diff_context", return_value=diff_context),
                mock.patch.object(
                    cap.code_mower_prompts,
                    "load_review_prompt",
                    return_value="",
                ),
                mock.patch.object(cap, "run_claude_audit", side_effect=fake_run),
                mock.patch.object(
                    cap,
                    "post_pr_comment",
                    return_value={"html_url": "https://github.test/comment/1"},
                ),
            ):
                result = cap.audit_pr(config, "owner/repo", 42)

            self.assertEqual(result.verdict, "PASS")
            self.assertNotIn("budget_exhausted", result.comment_body)

    def test_claude_truncated_diff_posts_unknown_without_blocking_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            head_sha = "f" * 40
            pr_payload = {"head": {"sha": head_sha, "ref": "human/huge"}, "title": "Fix"}
            diff_context = cap.DiffContext(
                "src/huge.py | 2000 +",
                "diff --git a/src/huge.py b/src/huge.py\n[diff truncated]",
                ("src/huge.py",),
                True,
                cap.DEFAULT_MAX_DIFF_BYTES,
                cap.DEFAULT_MAX_DIFF_HARD_LIMIT_BYTES,
                cap.DEFAULT_MAX_DIFF_HARD_LIMIT_BYTES + 1,
                cap.DEFAULT_MAX_DIFF_HARD_LIMIT_BYTES,
                False,
            )
            config = cap.ClaudeAuditConfig(
                "token",
                {"owner/repo": repo},
                include_plan_context=False,
                include_decision_context=False,
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
                mock.patch.object(cap, "DEFAULT_CLAUDE_CLI_FAILURE_DIR", tmp_path / "failures"),
                mock.patch.object(
                    cap,
                    "fetch_pull_request",
                    side_effect=[pr_payload, pr_payload],
                ),
                mock.patch.object(cap, "_build_diff_context", return_value=diff_context),
                mock.patch.object(cap, "run_claude_audit") as run_claude,
                mock.patch.object(
                    cap,
                    "post_pr_comment",
                    return_value={"html_url": "https://github.test/comment/1"},
                ),
            ):
                result = cap.audit_pr(config, "owner/repo", 42)

            self.assertEqual(result.verdict, "UNKNOWN")
            run_claude.assert_not_called()
            self.assertIn("Diff: truncated by wrapper", result.comment_body)
            self.assertIn(cap.UNKNOWN_REQUEUE_MARKER, result.comment_body)
            self.assertNotIn("Claude Audit: BLOCKED", result.comment_body)
            self.assertNotIn("- [P2]", result.comment_body)

    def test_audit_exit_code_keeps_stale_neutral_and_unknown_loud(self) -> None:
        self.assertEqual(cap._audit_exit_code("STALE"), 0)
        self.assertEqual(cap._audit_exit_code("stale"), 0)
        self.assertEqual(cap._audit_exit_code("PASS"), 0)
        self.assertEqual(cap._audit_exit_code("UNKNOWN"), 2)
        self.assertEqual(cap._audit_exit_code("unknown"), 2)

    def test_comment_header_separates_authority_from_calibration_badge(self) -> None:
        body = cap.format_comment(
            cap.ClaudeVerdict(
                verdict="PASS",
                prose="No merge-blocking regressions found.",
            ),
            "a" * 40,
            merge_authority=True,
            calibration_badge=" calibration phase - informational only\nfor CM-1 ",
        )

        first_line = body.splitlines()[0]
        self.assertEqual(first_line, "## Claude audit (merge-authority lane)")
        self.assertNotIn("calibration phase", first_line)
        self.assertIn("Calibration: calibration phase - informational only for CM-1", body)

    def test_requeue_comments_include_machine_readable_kind_markers(self) -> None:
        unknown = cap.format_comment(
            cap._unknown_structured_verdict("Claude CLI exited 1"),
            "a" * 40,
            is_unknown=True,
        )
        stale = cap.format_comment(
            cap._unknown_structured_verdict("stale"),
            "a" * 40,
            is_stale=True,
            stale_end_sha="b" * 40,
        )

        self.assertIn(cap.UNKNOWN_REQUEUE_MARKER, unknown)
        self.assertIn(cap.STALE_REQUEUE_MARKER, stale)

    def test_claude_prompt_includes_decision_registry(self) -> None:
        comments = [
            {
                "author_association": "MEMBER",
                "body": (
                    '<!-- CODE_MOWER_DECISION: id=ADR-007 scope=finding '
                    'resolves="HOST_DISPLAY_NAME" by=owner ref=ADR-007 -->'
                ),
            }
        ]
        with mock.patch.object(cap, "fetch_issue_comments", return_value=comments):
            registry = cap._decision_registry_context(
                "owner/repo",
                42,
                token="token",
            )

        prompt = cap._review_prompt(
            repo="owner/repo",
            pr_number=42,
            head_sha="a" * 40,
            base_ref="origin/main",
            branch_name="human/fix",
            title="Fix",
            diff_stat="src/app.py | 1 +",
            diff_text="diff --git a/src/app.py b/src/app.py",
            was_truncated=False,
            decision_registry_text=registry,
        )

        self.assertIn("Trusted Code Mower decision registry", prompt)
        self.assertIn("ADR-007", prompt)
        self.assertIn("HOST_DISPLAY_NAME", prompt)


if __name__ == "__main__":
    unittest.main()
