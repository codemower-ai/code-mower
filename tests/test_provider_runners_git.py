import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from code_mower.provider_runners.git import fetch_local_checkout_diff, local_head_sha, run_git


class ProviderRunnersGitTests(unittest.TestCase):
    def _make_repo(self) -> Path:
        root = Path(tempfile.mkdtemp(prefix="code-mower-provider-git-"))
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
        (root / "README.md").write_text("one\ntwo\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "update"], cwd=root, check=True)
        return root

    def test_run_git_returns_captured_text(self) -> None:
        repo = self._make_repo()

        completed = run_git(repo, ["status", "--porcelain"])

        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr, "")

    def test_run_git_can_skip_checking_return_code(self) -> None:
        repo = self._make_repo()

        completed = run_git(repo, ["rev-parse", "--verify", "missing-ref"], check=False)

        self.assertNotEqual(completed.returncode, 0)

    def test_local_head_sha_and_checkout_diff(self) -> None:
        repo = self._make_repo()

        head_sha = local_head_sha(repo)
        diff_head, diff = fetch_local_checkout_diff(repo, base_ref="HEAD~1")

        self.assertEqual(diff_head, head_sha)
        self.assertIn("README.md", diff)
        self.assertIn("+two", diff)

    def test_fetch_local_checkout_diff_rejects_missing_repo(self) -> None:
        with self.assertRaises(ValueError):
            fetch_local_checkout_diff(Path("/tmp/code-mower-missing-repo"), base_ref="main")


if __name__ == "__main__":
    unittest.main()
