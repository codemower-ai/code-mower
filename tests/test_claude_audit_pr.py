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
            )

            with (
                mock.patch.dict(
                    "os.environ",
                    {
                        "CODE_MOWER_VERDICT_ARTIFACT_DIR": str(tmp_path / "verdicts"),
                        "PYTEST_CURRENT_TEST": "",
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
            )

            with (
                mock.patch.dict(
                    "os.environ",
                    {
                        "PYTEST_CURRENT_TEST": "tests/test_live_guard.py::test_guard",
                        "CODE_MOWER_VERDICT_ARTIFACT_DIR": str(tmp_path / "verdicts"),
                        "CODE_MOWER_VERDICT_QUARANTINE_DIR": str(tmp_path / "quarantine"),
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
            )

            with (
                mock.patch.dict(
                    "os.environ",
                    {
                        "PYTEST_CURRENT_TEST": "",
                        "CODE_MOWER_VERDICT_ARTIFACT_DIR": str(tmp_path / "verdicts"),
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
                mock.patch.object(cap, "post_pr_comment") as post_comment,
            ):
                result = cap.audit_pr(config, "owner/repo", 42)

            self.assertEqual(result.verdict, "UNKNOWN")
            self.assertEqual(run_claude.call_count, cap.MAX_CLAUDE_AUDIT_ATTEMPTS)
            post_comment.assert_not_called()
            self.assertIsNotNone(result.verdict_artifact_path)
            assert result.verdict_artifact_path is not None
            self.assertIn("quarantine", str(result.verdict_artifact_path))
            artifact = json.loads(result.verdict_artifact_path.read_text(encoding="utf-8"))
            self.assertTrue(artifact["quarantined"])
            self.assertIn("fixture-shaped structured verdict", artifact["quarantine_reason"])


if __name__ == "__main__":
    unittest.main()
