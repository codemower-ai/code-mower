from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from code_mower.cloud_client.git_metadata import (
    detect_repo_slug,
    repo_slug_from_remote,
    run_git,
)


class CloudGitMetadataTests(unittest.TestCase):
    def _make_repo(self, remote_url: str = "https://github.com/codemower-ai/code-mower.git") -> Path:
        root = Path(tempfile.mkdtemp(prefix="code-mower-cloud-git-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "remote", "add", "origin", remote_url], cwd=root, check=True)
        return root

    def test_repo_slug_from_remote_supports_common_github_forms(self) -> None:
        cases = {
            "git@github.com:codemower-ai/code-mower.git": "codemower-ai/code-mower",
            "https://github.com/codemower-ai/code-mower.git": "codemower-ai/code-mower",
            "http://github.com/codemower-ai/code-mower": "codemower-ai/code-mower",
            "https://github.com/codemower-ai/code-mower/pull/1": "codemower-ai/code-mower",
        }
        for remote, expected in cases.items():
            with self.subTest(remote=remote):
                self.assertEqual(repo_slug_from_remote(remote), expected)

    def test_repo_slug_from_remote_rejects_non_github_remotes(self) -> None:
        for remote in ("", "ssh://example.com/nope", "https://gitlab.com/owner/repo.git"):
            with self.subTest(remote=remote):
                self.assertEqual(repo_slug_from_remote(remote), "")

    def test_detect_repo_slug_reads_origin_remote(self) -> None:
        repo = self._make_repo("git@github.com:codemower-ai/code-mower.git")

        self.assertEqual(detect_repo_slug(repo), "codemower-ai/code-mower")

    def test_detect_repo_slug_returns_empty_for_non_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(detect_repo_slug(Path(tmp)), "")

    def test_run_git_returns_stdout_or_empty_string(self) -> None:
        repo = self._make_repo()

        self.assertEqual(
            run_git(repo, ["config", "--get", "remote.origin.url"]),
            "https://github.com/codemower-ai/code-mower.git",
        )
        self.assertEqual(run_git(repo, ["rev-parse", "--verify", "missing-ref"]), "")


if __name__ == "__main__":
    unittest.main()
