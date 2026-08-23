from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from code_mower import claude_audit_pr, codex_audit_pr, decisions


class DecisionAuditIntegrationTests(unittest.TestCase):
    def test_posted_marker_runs_codex_and_claude_lanes_to_verdicts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            worktree = tmp_path / "worktree"
            worktree.mkdir()
            head_sha = "a" * 40
            pr_payload = {"head": {"sha": head_sha, "ref": "human/fix"}, "title": "Fix"}
            marker_body = decisions.render_decision_comment(
                decisions.DecisionRecord(
                    id="ADR-007",
                    scope="finding",
                    resolves="",
                    by="owner",
                    finding_id="codex:b93829375d1f7c3d27fa",
                    ref="https://github.com/codemower-ai/code-mower/pull/420",
                )
            ) + decisions.render_decision_comment(
                decisions.DecisionRecord(
                    id="ADR-008",
                    scope="finding",
                    resolves="",
                    by="owner",
                    finding_id="claude:f790e9961b7db082159c",
                    ref="https://github.com/codemower-ai/code-mower/pull/420",
                )
            )
            comments = [
                {
                    "author_association": "MEMBER",
                    "user": {"login": "owner"},
                    "body": marker_body,
                }
            ]
            codex_contexts: list[str] = []
            claude_prompts: list[str] = []

            def fake_codex_structuring(
                _config: codex_audit_pr.AuditConfig,
                _review_text: str,
                trusted_context: str = "",
            ) -> tuple[codex_audit_pr.CodexVerdict, str, str]:
                codex_contexts.append(trusted_context)
                return (
                    codex_audit_pr.CodexVerdict(
                        verdict="PASS",
                        prose=(
                            "Summary:\n\nNo merge-blocking regressions found.\n\n"
                            "Findings: none."
                        ),
                    ),
                    '{"structured_output":"pass"}',
                    "",
                )

            def fake_claude_audit(
                _config: claude_audit_pr.ClaudeAuditConfig,
                prompt: str,
            ) -> tuple[claude_audit_pr.ClaudeVerdict, str, str]:
                claude_prompts.append(prompt)
                payload = {
                    "schema": claude_audit_pr.CLAUDE_AUDIT_SCHEMA_ID,
                    "verdict": "pass",
                    "summary": "No merge-blocking regressions found in this review.",
                    "findings": [],
                }
                return (
                    claude_audit_pr.parse_structured_claude_verdict(payload),
                    json.dumps(payload),
                    "",
                )

            codex_config = codex_audit_pr.AuditConfig(
                "token",
                {"owner/repo": repo},
                include_plan_context=False,
                include_decision_context=True,
                decision_authorities=("owner",),
            )
            codex_review_context = codex_audit_pr.ReviewContextDiagnostics(
                base_ref=codex_config.base_ref,
                head_sha=head_sha,
                changed_file_count=1,
                diff_bytes=128,
                requested_max_bytes=codex_config.max_diff_bytes,
                hard_limit_bytes=(
                    codex_config.max_diff_hard_limit_bytes
                    or codex_audit_pr.DEFAULT_MAX_DIFF_HARD_LIMIT_BYTES
                ),
                included_diff_bytes=128,
                effective_budget_usd=(
                    codex_config.max_budget_usd
                    or codex_audit_pr.DEFAULT_MAX_BUDGET_USD
                ),
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
                    codex_audit_pr,
                    "fetch_pull_request",
                    side_effect=[pr_payload, pr_payload],
                ),
                mock.patch.object(codex_audit_pr, "fetch_issue_comments", return_value=comments),
                mock.patch.object(codex_audit_pr, "preflight_codex_cli", return_value="codex"),
                mock.patch.object(codex_audit_pr, "_discover_venv", return_value=None),
                mock.patch.object(codex_audit_pr, "_fetch_pr_head"),
                mock.patch.object(codex_audit_pr, "_fetch_base_ref"),
                mock.patch.object(
                    codex_audit_pr,
                    "_build_review_context_diagnostics",
                    return_value=codex_review_context,
                ),
                mock.patch.object(codex_audit_pr, "_create_temp_worktree", return_value=worktree),
                mock.patch.object(codex_audit_pr, "_remove_worktree"),
                mock.patch.object(
                    codex_audit_pr,
                    "run_codex_review",
                    return_value=("review text", ""),
                ),
                mock.patch.object(
                    codex_audit_pr,
                    "run_codex_verdict_structuring",
                    side_effect=fake_codex_structuring,
                ),
                mock.patch.object(
                    codex_audit_pr,
                    "post_pr_comment",
                    return_value={"html_url": "https://github.test/codex"},
                ),
            ):
                codex_result = codex_audit_pr.audit_pr(codex_config, "owner/repo", 42)

            claude_config = claude_audit_pr.ClaudeAuditConfig(
                "token",
                {"owner/repo": repo},
                include_plan_context=False,
                include_decision_context=True,
                decision_authorities=("owner",),
            )
            diff_context = claude_audit_pr.DiffContext(
                "src/app.py | 1 +",
                "diff --git a/src/app.py b/src/app.py\n+safe = True",
                ("src/app.py",),
                False,
                1_000,
                1_000,
                40,
                40,
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
                    claude_audit_pr,
                    "fetch_pull_request",
                    side_effect=[pr_payload, pr_payload],
                ),
                mock.patch.object(claude_audit_pr, "fetch_issue_comments", return_value=comments),
                mock.patch.object(
                    claude_audit_pr,
                    "_decision_authorities_for_repo",
                    return_value=("owner",),
                ),
                mock.patch.object(claude_audit_pr, "_build_diff_context", return_value=diff_context),
                mock.patch.object(
                    claude_audit_pr.code_mower_prompts,
                    "load_review_prompt",
                    return_value="",
                ),
                mock.patch.object(
                    claude_audit_pr,
                    "run_claude_audit",
                    side_effect=fake_claude_audit,
                ),
                mock.patch.object(
                    claude_audit_pr,
                    "post_pr_comment",
                    return_value={"html_url": "https://github.test/claude"},
                ),
            ):
                claude_result = claude_audit_pr.audit_pr(claude_config, "owner/repo", 42)

        self.assertEqual(codex_result.verdict, "PASS")
        self.assertEqual(claude_result.verdict, "PASS")
        self.assertEqual(len(codex_contexts), 1)
        self.assertIn("ADR-007", codex_contexts[0])
        self.assertEqual(len(claude_prompts), 1)
        self.assertIn("ADR-008", claude_prompts[0])


if __name__ == "__main__":
    unittest.main()
