from pathlib import Path
import tempfile
import unittest


from code_mower.provider_runners.repo_paths import (
    parse_repo_paths,
    validate_repo_path_for_wrapper,
)


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
        with self.assertRaisesRegex(ValueError, "expected OWNER/REPO:/absolute/path"):
            parse_repo_paths("owner/app")

    def test_parse_repo_paths_rejects_relative_paths(self) -> None:
        with self.assertRaisesRegex(ValueError, "docs/local-audit-runner.md"):
            parse_repo_paths("owner/app:relative/path")

    def test_validate_repo_path_requires_existing_checkout(self) -> None:
        with self.assertRaisesRegex(ValueError, "not an existing directory"):
            validate_repo_path_for_wrapper(
                {"owner/app": Path("/tmp/code-mower-missing-path")},
                "owner/app",
            )

    def test_validate_repo_path_rejects_current_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            with self.assertRaisesRegex(ValueError, "separate PR-head checkout"):
                validate_repo_path_for_wrapper(
                    {"owner/app": path},
                    "owner/app",
                    cwd=path,
                )
