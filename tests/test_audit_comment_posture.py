import unittest
from unittest.mock import patch

from code_mower.claude_audit_pr import (
    ClaudeVerdict,
    _parse_args as parse_claude_args,
    format_comment as format_claude_comment,
)
from code_mower.codex_audit_pr import (
    CodexVerdict,
    _parse_args as parse_codex_args,
    format_comment as format_codex_comment,
)


class AuditCommentPostureTests(unittest.TestCase):
    def test_codex_comment_defaults_to_merge_authority_header(self) -> None:
        comment = format_codex_comment(
            CodexVerdict(verdict="PASS", prose="Findings: none."),
            "a" * 40,
        )

        self.assertIn("## Codex audit (merge-authority lane)", comment)
        self.assertNotIn("calibration phase", comment)

    def test_codex_comment_can_render_informational_header(self) -> None:
        comment = format_codex_comment(
            CodexVerdict(verdict="PASS", prose="Findings: none."),
            "a" * 40,
            merge_authority=False,
        )

        self.assertIn("## Codex audit (informational only)", comment)

    def test_claude_comment_uses_configured_posture(self) -> None:
        comment = format_claude_comment(
            ClaudeVerdict(verdict="BLOCKED", prose="Finding."),
            "b" * 40,
            merge_authority=False,
        )

        self.assertIn("## Claude audit (informational only)", comment)
        self.assertIn("Claude Audit: BLOCKED", comment)

    def test_audit_comments_render_full_head_sha(self) -> None:
        codex_head = "0123456789abcdef0123456789abcdef01234567"
        claude_head = "fedcba9876543210fedcba9876543210fedcba98"

        codex_comment = format_codex_comment(
            CodexVerdict(verdict="PASS", prose="Findings: none."),
            codex_head,
        )
        claude_comment = format_claude_comment(
            ClaudeVerdict(verdict="PASS", prose="Findings: none."),
            claude_head,
        )

        self.assertIn(f"Head SHA: `{codex_head}`", codex_comment)
        self.assertIn(f"Head SHA: `{claude_head}`", claude_comment)
        self.assertNotIn(f"Head SHA: `{codex_head[:12]}`", codex_comment)
        self.assertNotIn(f"Head SHA: `{claude_head[:12]}`", claude_comment)

    def test_actions_run_marker_can_be_rendered_for_workflow_comments(self) -> None:
        codex_comment = format_codex_comment(
            CodexVerdict(verdict="PASS", prose="Findings: none."),
            "a" * 40,
            actions_run_id="12345",
        )
        claude_comment = format_claude_comment(
            ClaudeVerdict(verdict="PASS", prose="Findings: none."),
            "b" * 40,
            actions_run_id="67890",
        )

        self.assertIn("<!-- CODE_MOWER_AUDIT_RUN: run_id=12345 -->", codex_comment)
        self.assertIn("<!-- CODE_MOWER_AUDIT_RUN: run_id=67890 -->", claude_comment)

    def test_cli_posture_defaults_can_be_overridden(self) -> None:
        with patch.dict("os.environ", {"CODEX_AUDIT_MERGE_AUTHORITY": "false"}):
            self.assertFalse(parse_codex_args([]).merge_authority)
            self.assertTrue(parse_codex_args(["--merge-authority"]).merge_authority)

        with patch.dict("os.environ", {}, clear=True):
            self.assertTrue(parse_claude_args([]).merge_authority)
            self.assertFalse(parse_claude_args(["--informational"]).merge_authority)


if __name__ == "__main__":
    unittest.main()
