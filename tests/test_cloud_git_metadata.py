from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import code_mower.cloud_client.git_metadata as git_metadata
from code_mower.cloud_client.errors import CloudBundleError
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


class MaterializedSymlinkTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = tempfile.TemporaryDirectory(prefix="code-mower-symlink-test-")
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.private_root = self.root / "private"
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "core.symlinks", "true"], cwd=self.repo, check=True)
        (self.repo / "tracked.txt").write_text("committed fixture\n", encoding="utf-8")

    @contextmanager
    def _materialize(self) -> Iterator[Path]:
        subprocess.run(["git", "add", "--all"], cwd=self.repo, check=True)
        subprocess.run(
            [
                "git", "-c", "user.name=Dev", "-c", "user.email=dev@example.com",
                "-c", "commit.gpgSign=false", "commit", "-qm", "fixture",
            ],
            cwd=self.repo,
            check=True,
        )
        head = run_git(self.repo, ["rev-parse", "HEAD"])
        self.private_root.mkdir()
        with patch.object(git_metadata.tempfile, "mkdtemp", return_value=str(self.private_root)):
            with git_metadata.materialized_commit_source(self.repo, head) as source:
                yield source

    def _assert_rejected(self) -> None:
        with self.assertRaises(CloudBundleError) as caught:
            with self._materialize():
                self.fail("an unsafe source was exposed to a consumer")
        self.assertIn(
            str(caught.exception),
            (
                "the private exact-commit source contains an unsafe symlink",
                "unable to validate symlinks in the private exact-commit source",
            ),
        )
        self.assertNotIn(str(self.root), str(caught.exception))
        self.assertFalse(self.private_root.exists())

    def test_rejects_absolute_external_symlink(self) -> None:
        external = self.root / "external.txt"
        external.write_text("external fixture\n", encoding="utf-8")
        original_mode = external.stat().st_mode
        (self.repo / "code-mower.yml").symlink_to(external)
        self._assert_rejected()
        self.assertEqual(external.stat().st_mode, original_mode)

    def test_rejects_relative_external_symlink(self) -> None:
        (self.root / "external.txt").write_text("external fixture\n", encoding="utf-8")
        (self.repo / "code-mower.yml").symlink_to("../../external.txt")
        self._assert_rejected()

    def test_rejects_external_hop_even_with_internal_destination(self) -> None:
        (self.root / "external").symlink_to(self.private_root / "source/tracked.txt")
        (self.repo / "code-mower.yml").symlink_to("../../external")
        self._assert_rejected()

    def test_rejects_external_directory_symlink_in_hidden_subdirectory(self) -> None:
        (self.root / "external").mkdir()
        hidden = self.repo / ".config"
        hidden.mkdir()
        (hidden / "settings").symlink_to("../../../external", target_is_directory=True)
        self._assert_rejected()

    def test_rejects_git_metadata_file_symlink(self) -> None:
        (self.repo / "code-mower.yml").symlink_to(".git/HEAD")
        self._assert_rejected()

    def test_rejects_git_metadata_directory_symlink(self) -> None:
        (self.repo / "metadata").symlink_to(".git", target_is_directory=True)
        self._assert_rejected()

    def test_rejects_git_metadata_traversal_even_with_internal_destination(self) -> None:
        (self.repo / "code-mower.yml").symlink_to(".git/../tracked.txt")
        self._assert_rejected()

    def test_rejects_git_metadata_case_alias_on_case_insensitive_filesystem(self) -> None:
        if not (self.repo / ".GIT").exists():
            self.skipTest("filesystem is case-sensitive")
        (self.repo / "code-mower.yml").symlink_to(".GIT/HEAD")
        self._assert_rejected()

    def test_rejects_dangling_symlink(self) -> None:
        (self.repo / "code-mower.yml").symlink_to("missing.txt")
        self._assert_rejected()

    def test_rejects_non_directory_target_with_trailing_slash(self) -> None:
        (self.repo / "code-mower.yml").symlink_to("tracked.txt/")
        self._assert_rejected()

    def test_rejects_non_directory_target_with_dot_suffix(self) -> None:
        (self.repo / "code-mower.yml").symlink_to("tracked.txt/.")
        self._assert_rejected()

    def test_rejects_symlink_cycle(self) -> None:
        (self.repo / "first").symlink_to("second")
        (self.repo / "second").symlink_to("first")
        self._assert_rejected()

    def test_rejects_chain_to_git_metadata(self) -> None:
        (self.repo / "code-mower.yml").symlink_to("settings")
        (self.repo / "settings").symlink_to(".git/config")
        self._assert_rejected()

    def test_preserves_safe_internal_file_directory_and_chained_symlinks(self) -> None:
        config = self.repo / "config"
        config.mkdir()
        (config / "settings.yml").write_text("fixture: true\n", encoding="utf-8")
        (config / "tracked").symlink_to("../tracked.txt")
        (config / "nested").mkdir()
        (config / "nested/keep.txt").write_text("tracked directory\n", encoding="utf-8")
        (self.repo / "deep").symlink_to("config/nested", target_is_directory=True)
        (self.repo / "parent-traversal").symlink_to("deep/../../tracked.txt")
        (self.repo / "settings").symlink_to("config", target_is_directory=True)
        (self.repo / "code-mower.yml").symlink_to("settings/settings.yml")
        with self._materialize() as source:
            self.assertTrue((source / "code-mower.yml").is_symlink())
            self.assertTrue((source / "settings").is_symlink())
            self.assertEqual((source / "code-mower.yml").read_text(), "fixture: true\n")
            self.assertEqual((source / "config/tracked").read_text(), "committed fixture\n")
            self.assertEqual((source / "parent-traversal").read_text(), "committed fixture\n")
            self.assertEqual((source / "code-mower.yml").stat().st_mode & 0o222, 0)
            self.assertTrue(git_metadata.checkout_provenance(source, required=True)["clean"])
        self.assertFalse(self.private_root.exists())

    def test_preserves_absolute_symlink_inside_private_source(self) -> None:
        (self.repo / "code-mower.yml").symlink_to(self.private_root / "source/tracked.txt")
        with self._materialize() as source:
            self.assertTrue((source / "code-mower.yml").is_symlink())
            self.assertEqual((source / "code-mower.yml").read_text(), "committed fixture\n")
        self.assertFalse(self.private_root.exists())

    def test_fails_closed_on_tree_traversal_error(self) -> None:
        with patch.object(git_metadata.os, "scandir", side_effect=PermissionError("fixture path")):
            with self.assertRaises(CloudBundleError) as caught:
                git_metadata._validate_materialized_symlinks(self.repo)
        self.assertEqual(
            str(caught.exception),
            "unable to validate symlinks in the private exact-commit source",
        )


if __name__ == "__main__":
    unittest.main()
