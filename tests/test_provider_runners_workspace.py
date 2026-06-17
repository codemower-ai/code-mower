import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from code_mower.provider_runners.workspace import (
    ProviderWorkspaceError,
    verify_checkout_at_head,
    working_tree_status,
)


class ProviderRunnersWorkspaceTests(unittest.TestCase):
    def _make_repo(self) -> Path:
        root = Path(tempfile.mkdtemp(prefix="code-mower-provider-workspace-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Code Mower Test"], cwd=root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "code-mower-test@example.com"],
            cwd=root,
            check=True,
        )
        subprocess.run(["git", "config", "commit.gpgSign", "false"], cwd=root, check=True)
        (root / "README.md").write_text("one\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=root, check=True)
        return root

    def _head_sha(self, repo: Path) -> str:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            text=True,
        ).strip()

    def test_working_tree_status_returns_porcelain_output(self) -> None:
        repo = self._make_repo()
        (repo / "README.md").write_text("one\ntwo\n", encoding="utf-8")

        self.assertEqual(working_tree_status(repo), " M README.md\n")

    def test_verify_checkout_at_head_accepts_clean_expected_head(self) -> None:
        repo = self._make_repo()
        head = self._head_sha(repo)

        checkout = verify_checkout_at_head(
            repo,
            expected_head_sha=head,
            purpose="test runner",
        )

        self.assertEqual(checkout["repo_path"], str(repo))
        self.assertEqual(checkout["local_head_sha"], head)
        self.assertFalse(checkout["dirty"])

    def test_verify_checkout_at_head_rejects_wrong_head(self) -> None:
        repo = self._make_repo()

        with self.assertRaisesRegex(
            ProviderWorkspaceError,
            r"local checkout must be at the PR head for test runner",
        ):
            verify_checkout_at_head(
                repo,
                expected_head_sha="0" * 40,
                purpose="test runner",
            )

    def test_verify_checkout_at_head_rejects_dirty_checkout_by_default(self) -> None:
        repo = self._make_repo()
        (repo / "README.md").write_text("dirty\n", encoding="utf-8")

        with self.assertRaisesRegex(
            ProviderWorkspaceError,
            "local checkout has uncommitted changes",
        ):
            verify_checkout_at_head(repo, expected_head_sha=self._head_sha(repo))

    def test_verify_checkout_at_head_can_allow_dirty_checkout(self) -> None:
        repo = self._make_repo()
        (repo / "README.md").write_text("dirty\n", encoding="utf-8")

        checkout = verify_checkout_at_head(
            repo,
            expected_head_sha=self._head_sha(repo),
            allow_dirty=True,
        )

        self.assertTrue(checkout["dirty"])

    def test_verify_checkout_at_head_rejects_missing_path(self) -> None:
        with self.assertRaisesRegex(ProviderWorkspaceError, "repo path does not exist"):
            verify_checkout_at_head(
                Path("/tmp/code-mower-provider-workspace-missing"),
                expected_head_sha="0" * 40,
            )


if __name__ == "__main__":
    unittest.main()
