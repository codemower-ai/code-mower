import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from code_mower.provider_runners.pr_worktree import (
    FetchedHeadMismatch,
    create_temp_worktree,
    fetch_base_ref,
    fetch_base_ref_sha,
    fetch_pr_head,
    fetch_pr_head_sha,
    fetch_pr_head_sha_or_raise,
    remove_worktree,
    run_git_text,
)


class ProviderRunnersPrWorktreeTests(unittest.TestCase):
    def _make_remote_and_checkout(self) -> tuple[Path, Path, str, str]:
        root = Path(tempfile.mkdtemp(prefix="code-mower-pr-worktree-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        remote = root / "remote.git"
        checkout = root / "checkout"
        subprocess.run(["git", "init", "-q", "--bare", remote], check=True)
        subprocess.run(["git", "clone", "-q", str(remote), str(checkout)], check=True)
        subprocess.run(
            ["git", "config", "user.name", "Code Mower Test"],
            cwd=checkout,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "code-mower-test@example.com"],
            cwd=checkout,
            check=True,
        )
        subprocess.run(["git", "config", "commit.gpgSign", "false"], cwd=checkout, check=True)

        (checkout / "README.md").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=checkout, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=checkout, check=True)
        subprocess.run(["git", "branch", "-M", "main"], cwd=checkout, check=True)
        subprocess.run(["git", "push", "-q", "origin", "main"], cwd=checkout, check=True)
        base_sha = run_git_text(checkout, ["rev-parse", "HEAD"]).strip()

        subprocess.run(["git", "checkout", "-q", "-b", "feature"], cwd=checkout, check=True)
        (checkout / "README.md").write_text("base\nfeature\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=checkout, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "feature"], cwd=checkout, check=True)
        pr_sha = run_git_text(checkout, ["rev-parse", "HEAD"]).strip()
        subprocess.run(
            ["git", "push", "-q", "origin", "HEAD:refs/pull/7/head"],
            cwd=checkout,
            check=True,
        )

        return remote, checkout, base_sha, pr_sha

    def test_fetch_base_ref_sha_resolves_remote_branch(self) -> None:
        _remote, checkout, base_sha, _pr_sha = self._make_remote_and_checkout()

        fetched_base = fetch_base_ref_sha(checkout, "origin/main")

        self.assertEqual(fetched_base, base_sha)

    def test_fetch_pr_head_sha_resolves_pull_ref(self) -> None:
        _remote, checkout, _base_sha, pr_sha = self._make_remote_and_checkout()

        fetched_head = fetch_pr_head_sha(checkout, 7)

        self.assertEqual(fetched_head, pr_sha)

    def test_fetch_pr_head_sha_or_raise_rejects_moved_head(self) -> None:
        _remote, checkout, _base_sha, pr_sha = self._make_remote_and_checkout()

        with self.assertRaises(FetchedHeadMismatch) as raised:
            fetch_pr_head_sha_or_raise(
                checkout,
                7,
                expected_head_sha="0" * 40,
            )

        self.assertEqual(raised.exception.expected_sha, "0" * 40)
        self.assertEqual(raised.exception.actual_sha, pr_sha)

    def test_fetch_helpers_keep_legacy_fetch_api_available(self) -> None:
        _remote, checkout, _base_sha, _pr_sha = self._make_remote_and_checkout()

        fetch_base_ref(checkout, "origin/main")
        fetch_pr_head(checkout, 7)

        self.assertEqual(
            run_git_text(checkout, ["rev-parse", "--verify", "FETCH_HEAD"]).strip(),
            fetch_pr_head_sha(checkout, 7),
        )

    def test_temp_worktree_create_and_remove(self) -> None:
        _remote, checkout, _base_sha, pr_sha = self._make_remote_and_checkout()

        worktree_path = create_temp_worktree(checkout, pr_sha, prefix="code-mower-test-")
        self.addCleanup(shutil.rmtree, worktree_path.parent, ignore_errors=True)

        self.assertTrue((worktree_path / "README.md").exists())
        self.assertEqual(run_git_text(worktree_path, ["rev-parse", "HEAD"]).strip(), pr_sha)

        parent = worktree_path.parent
        remove_worktree(checkout, worktree_path)

        self.assertFalse(worktree_path.exists())
        self.assertFalse(parent.exists())


if __name__ == "__main__":
    unittest.main()
