from pathlib import Path
import unittest


from code_mower.provider_runners.repo_paths import parse_repo_paths


class RepoPathParsingTests(unittest.TestCase):
    def test_parse_repo_paths_accepts_multiple_entries(self) -> None:
        self.assertEqual(
            parse_repo_paths("owner/app:/tmp/app, owner/service:/tmp/service"),
            {
                "owner/app": Path("/tmp/app"),
                "owner/service": Path("/tmp/service"),
            },
        )

    def test_parse_repo_paths_ignores_empty_entries(self) -> None:
        self.assertEqual(
            parse_repo_paths(" owner/app:/tmp/app, ,"),
            {"owner/app": Path("/tmp/app")},
        )

    def test_parse_repo_paths_rejects_entries_without_separator(self) -> None:
        with self.assertRaisesRegex(ValueError, "expected owner/repo:/path"):
            parse_repo_paths("owner/app")
