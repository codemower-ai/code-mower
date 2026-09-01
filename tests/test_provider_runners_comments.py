import unittest

from code_mower.provider_runners.comments import (
    AUDIT_RUN_TRAILER_PREFIX,
    format_audit_comment_header,
    limit_comment_body,
    normalize_calibration_badge,
)
from code_mower.provider_runners.exit_codes import audit_exit_code


class ProviderRunnerCommentTests(unittest.TestCase):
    def test_preserves_short_body(self) -> None:
        body = "## Codex audit\n\nPASS\n"
        self.assertEqual(
            limit_comment_body(
                body,
                "<!-- CODEX_AUDIT_STATE: codex-audit-done -->",
                provider_name="Codex",
                max_chars=200,
            ),
            body,
        )

    def test_truncates_long_body_and_keeps_trailer(self) -> None:
        trailer = "<!-- CODEX_AUDIT_STATE: codex-audit-done -->"
        body = "a" * 200 + "\n" + trailer + "\n"

        result = limit_comment_body(body, trailer, provider_name="Codex", max_chars=140)

        self.assertLessEqual(len(result), 140)
        self.assertIn("[Codex audit comment truncated", result)
        self.assertTrue(result.endswith(trailer + "\n"))

    def test_handles_tiny_budget(self) -> None:
        result = limit_comment_body(
            "abcdef" * 20,
            "<!-- LONG_TRAILER -->",
            provider_name="Tiny",
            max_chars=10,
        )

        self.assertLessEqual(len(result), 10)
        self.assertEqual(result, "AILER -->\n")

    def test_shared_audit_header_preserves_existing_fields(self) -> None:
        header = format_audit_comment_header(
            provider_name="Codex",
            head_sha="a" * 40,
            merge_authority=False,
            actions_run_id="12345",
            calibration_badge="  pilot   run  ",
            diff_notice="changed\nfiles",
            context_notice="diff over hard limit",
        )

        self.assertIn("## Codex audit (informational only)", header)
        self.assertIn("Head SHA: `" + "a" * 40 + "`", header)
        self.assertIn("Diff: changed files", header)
        self.assertIn("Context: diff over hard limit", header)
        self.assertIn("Calibration: pilot run", header)
        self.assertIn(f"{AUDIT_RUN_TRAILER_PREFIX} run_id=12345 -->", header)

    def test_shared_calibration_badge_is_compact(self) -> None:
        self.assertEqual(normalize_calibration_badge("  alpha\n beta  "), "alpha beta")
        self.assertEqual(len(normalize_calibration_badge("x" * 200)), 120)

    def test_unknown_audit_verdict_is_loud_exit_code(self) -> None:
        self.assertEqual(audit_exit_code("UNKNOWN"), 2)
        self.assertEqual(audit_exit_code("unknown"), 2)
        self.assertEqual(audit_exit_code("PASS"), 0)
        self.assertEqual(audit_exit_code("BLOCKED"), 0)
        self.assertEqual(audit_exit_code("STALE"), 0)


if __name__ == "__main__":
    unittest.main()
