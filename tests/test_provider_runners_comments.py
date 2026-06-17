import unittest

from code_mower.provider_runners.comments import limit_comment_body


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


if __name__ == "__main__":
    unittest.main()
