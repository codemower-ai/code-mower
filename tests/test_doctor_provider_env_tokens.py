import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from code_mower.doctor_checks.provider_env import check_token_env
from code_mower.doctor_checks.provider_env_tokens import provider_token_status


class ProviderEnvTokenStatusTests(unittest.TestCase):
    def test_no_token_declarations_are_skipped(self) -> None:
        status = provider_token_status({})

        self.assertFalse(status.declares_tokens)
        self.assertTrue(status.all_present)
        [check] = check_token_env("manual-review", {})
        self.assertEqual(check.status, "skip")
        self.assertEqual(check.message, "lane declares no token env vars")

    def test_direct_required_token_is_present(self) -> None:
        with patch.dict(os.environ, {"CLAUDE_API_KEY": "secret"}, clear=True):
            status = provider_token_status({"token_env": ["CLAUDE_API_KEY"]})

        self.assertTrue(status.declares_tokens)
        self.assertTrue(status.all_present)
        self.assertEqual(status.token_env, ("CLAUDE_API_KEY",))
        self.assertEqual(status.missing, ())

    def test_review_hygiene_token_env_is_required_when_primary_token_env_missing(self) -> None:
        lane = {"review_hygiene": {"token_env": "GITHUB_TOKEN"}}

        with patch.dict(os.environ, {}, clear=True):
            status = provider_token_status(lane)
            [check] = check_token_env("gitar-audit", lane)

        self.assertEqual(status.token_env, ("GITHUB_TOKEN",))
        self.assertEqual(status.missing, ("GITHUB_TOKEN",))
        self.assertEqual(check.status, "warn")
        self.assertIn("GITHUB_TOKEN", check.message)
        self.assertEqual(check.detail["missing"], ["GITHUB_TOKEN"])

    def test_supported_token_file_satisfies_any_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            secret_path = Path(tmp) / "gemini.env"
            secret_path.write_text("export GEMINI_API_KEY='from-file'\n", encoding="utf-8")
            lane = {"token_env_any": [["GEMINI_API_KEY", "GOOGLE_API_KEY"]]}

            with patch.dict(
                os.environ,
                {"GEMINI_API_KEY_FILE": str(secret_path)},
                clear=True,
            ):
                status = provider_token_status(lane)
                [check] = check_token_env("gemini-audit", lane)

        self.assertTrue(status.all_present)
        self.assertEqual(status.token_file_env, ("GEMINI_API_KEY_FILE",))
        self.assertEqual(status.missing_any, ())
        self.assertEqual(check.status, "pass")
        self.assertEqual(check.detail["token_file_env"], ["GEMINI_API_KEY_FILE"])

    def test_token_file_with_unsupported_assignment_does_not_satisfy_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            secret_path = Path(tmp) / "wrong.env"
            secret_path.write_text("export OPENAI_API_KEY='wrong-key'\n", encoding="utf-8")
            lane = {"token_env_any": [["GEMINI_API_KEY", "GOOGLE_API_KEY"]]}

            with patch.dict(
                os.environ,
                {"GEMINI_API_KEY_FILE": str(secret_path)},
                clear=True,
            ):
                status = provider_token_status(lane)
                [check] = check_token_env("gemini-audit", lane)

        self.assertFalse(status.all_present)
        self.assertEqual(
            status.missing_any,
            (("GEMINI_API_KEY", "GOOGLE_API_KEY"),),
        )
        self.assertEqual(status.token_file_env, ("GEMINI_API_KEY_FILE",))
        self.assertEqual(check.status, "warn")
        self.assertEqual(
            check.detail["missing_any"],
            [["GEMINI_API_KEY", "GOOGLE_API_KEY"]],
        )


if __name__ == "__main__":
    unittest.main()
