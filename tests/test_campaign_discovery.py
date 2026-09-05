#!/usr/bin/env python3
"""Cross-worktree release campaign discovery."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from code_mower import board, campaign_discovery, release_campaigns
from code_mower.git_identity import scratch_git_config_commands


class CampaignDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.state_dir = self.root / "state"
        env = patch.dict(os.environ, {"CODE_MOWER_STATE_DIR": str(self.state_dir)})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self._tmp.cleanup)

    def _init_repo(self, path: Path, remote: str = "git@github.com:owner/repo.git") -> Path:
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q"], cwd=path, check=True)
        for command in scratch_git_config_commands("git"):
            subprocess.run(command, cwd=path, check=True)
        (path / "README").write_text("repo\n", encoding="utf-8")
        subprocess.run(["git", "add", "README"], cwd=path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
        subprocess.run(["git", "remote", "add", "origin", remote], cwd=path, check=True)
        return path

    def _snapshot(self, campaigns_dir: Path) -> dict[str, bytes]:
        if not campaigns_dir.is_dir():
            return {}
        return {
            item.name: item.read_bytes()
            for item in sorted(campaigns_dir.iterdir())
            if item.name != release_campaigns.CAMPAIGNS_LOCK_FILENAME
        }

    def _create(
        self,
        repo_path: Path,
        *,
        release_tag: str = "v1.0.0",
        campaign_id: str = "",
        campaigns_dir: Path | None = None,
    ) -> int:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            return release_campaigns.campaign_command(
                release_tag=release_tag,
                package_spec=f"code-mower=={release_tag[1:]}",
                repo_path=repo_path,
                campaign_id=campaign_id,
                campaigns_dir=campaigns_dir,
                providers=["cursor_bugbot"],
            )

    def _status(
        self,
        repo_path: Path,
        *,
        release_tag: str = "",
        campaign_id: str = "",
        campaigns_dir: Path | None = None,
        emit_json: bool = True,
    ) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            ret = release_campaigns.campaign_command(
                status=True,
                release_tag=release_tag,
                campaign_id=campaign_id,
                repo_path=repo_path,
                campaigns_dir=campaigns_dir,
                emit_json=emit_json,
            )
        return ret, stdout.getvalue(), stderr.getvalue()

    def test_worktree_status_finds_campaign_without_explicit_path(self) -> None:
        primary = self._init_repo(self.root / "primary")
        other = self.root / "other"
        subprocess.run(
            ["git", "worktree", "add", "--detach", "-q", str(other)],
            cwd=primary,
            check=True,
        )
        self.assertEqual(self._create(primary), 0)
        campaigns_dir = primary / ".code-mower" / "campaigns"
        before = self._snapshot(campaigns_dir)

        ret, stdout, stderr = self._status(other, release_tag="v1.0.0")

        self.assertEqual(ret, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["campaign_id"], "campaign-v1.0.0")
        self.assertEqual(payload["release_tag"], "v1.0.0")
        self.assertEqual(self._snapshot(campaigns_dir), before)
        combined = stdout + stderr
        self.assertNotIn(str(primary), combined)
        self.assertNotIn(str(other), combined)
        self.assertNotIn(str(self.state_dir), combined)

    def test_ambiguous_tag_across_checkouts_names_ids_not_paths(self) -> None:
        first = self._init_repo(self.root / "first")
        second = self._init_repo(self.root / "second")
        self.assertEqual(self._create(first, campaign_id="first-campaign"), 0)
        self.assertEqual(self._create(second, campaign_id="second-campaign"), 0)

        ret, stdout, stderr = self._status(first, release_tag="v1.0.0")

        self.assertEqual(ret, 1)
        self.assertEqual(stdout, "")
        self.assertIn("matches 2 campaigns", stderr)
        self.assertIn("first-campaign", stderr)
        self.assertIn("second-campaign", stderr)
        self.assertIn("--campaign-id", stderr)
        self.assertNotIn(str(first), stderr)
        self.assertNotIn(str(second), stderr)
        self.assertLess(len(stderr), 400)

    def test_explicit_campaigns_dir_is_authoritative(self) -> None:
        primary = self._init_repo(self.root / "primary")
        other = self._init_repo(self.root / "other")
        self.assertEqual(self._create(primary), 0)
        isolated = other / "isolated-campaigns"
        self.assertEqual(
            self._create(
                other,
                campaign_id="isolated-campaign",
                campaigns_dir=isolated,
            ),
            0,
        )

        ret, stdout, stderr = self._status(
            other, release_tag="v1.0.0", campaigns_dir=isolated, emit_json=True
        )
        self.assertEqual(ret, 0, stderr)
        self.assertEqual(json.loads(stdout)["campaign_id"], "isolated-campaign")

    def test_missing_and_malformed_index_keep_repo_local_readable(self) -> None:
        repo = self._init_repo(self.root / "local")
        local_dir = repo / ".code-mower" / "campaigns"
        campaign = release_campaigns.initialize_campaign(
            release_tag="v1.0.0",
            package_spec="code-mower==1.0.0",
            providers=["cursor_bugbot"],
        ).to_dict()
        release_campaigns.save_campaign(campaign, local_dir)
        identity = campaign_discovery.resolve_repo_identity(repo)
        self.assertEqual(identity, "owner/repo")
        self.assertEqual(
            campaign_discovery.discover_campaign_directories(identity, local_dir=local_dir),
            [local_dir.resolve()],
        )

        index_path = campaign_discovery._index_path(identity)
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text("{not json", encoding="utf-8")
        found = campaign_discovery.discover_campaign_directories(
            identity, local_dir=local_dir
        )
        self.assertEqual(found, [local_dir.resolve()])

        index_path.write_text(
            json.dumps({"schema": "nope", "directories": [{"path": 1}]}),
            encoding="utf-8",
        )
        found = campaign_discovery.discover_campaign_directories(
            identity, local_dir=local_dir
        )
        self.assertEqual(found, [local_dir.resolve()])

        ret, stdout, stderr = self._status(repo, release_tag="v1.0.0")
        self.assertEqual(ret, 0, stderr)
        self.assertEqual(json.loads(stdout)["campaign_id"], "campaign-v1.0.0")

    def test_status_from_other_checkout_is_read_only_on_campaign_files(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root bypasses directory write permissions")
        primary = self._init_repo(self.root / "primary")
        other = self.root / "other"
        subprocess.run(
            ["git", "worktree", "add", "--detach", "-q", str(other)],
            cwd=primary,
            check=True,
        )
        self.assertEqual(self._create(primary), 0)
        campaigns_dir = primary / ".code-mower" / "campaigns"
        before = self._snapshot(campaigns_dir)
        campaigns_dir.chmod(0o500)
        try:
            ret, stdout, stderr = self._status(other, release_tag="v1.0.0")
            after = self._snapshot(campaigns_dir)
        finally:
            campaigns_dir.chmod(0o700)
        self.assertEqual(ret, 0, stderr)
        self.assertEqual(json.loads(stdout)["campaign_id"], "campaign-v1.0.0")
        self.assertEqual(after, before)

    def test_watch_upload_and_board_share_discovery(self) -> None:
        primary = self._init_repo(self.root / "primary")
        other = self.root / "other"
        subprocess.run(
            ["git", "worktree", "add", "--detach", "-q", str(other)],
            cwd=primary,
            check=True,
        )
        self.assertEqual(self._create(primary), 0)

        watch_out, watch_err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(watch_out), contextlib.redirect_stderr(watch_err):
            watch_ret = release_campaigns.campaign_command(
                action="watch",
                release_tag="v1.0.0",
                repo_path=other,
                timeout=0.05,
                interval=0.01,
                emit_json=True,
            )
        self.assertNotIn("no campaign found", watch_err.getvalue())
        watch_payload = json.loads(watch_out.getvalue())
        self.assertEqual(watch_payload["campaign_id"], "campaign-v1.0.0")
        self.assertNotEqual(watch_ret, 0)
        self.assertNotIn(str(primary), watch_out.getvalue() + watch_err.getvalue())

        upload_out, upload_err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(upload_out), contextlib.redirect_stderr(upload_err):
            upload_ret = release_campaigns.campaign_command(
                action="upload",
                release_tag="v1.0.0",
                repo_path=other,
                emit_json=True,
            )
        self.assertNotIn("no existing campaign", upload_err.getvalue())
        self.assertNotIn("no campaign found", upload_err.getvalue())
        upload_payload = json.loads(upload_out.getvalue())
        self.assertEqual(upload_payload.get("campaign_id", "campaign-v1.0.0"), "campaign-v1.0.0")
        self.assertIn(upload_ret, {0, 1})
        self.assertNotIn(str(primary), upload_out.getvalue() + upload_err.getvalue())

        payload = board.release_campaigns_payload(
            board.BoardConfig(repo="owner/repo", repo_path=str(other))
        )
        self.assertEqual(len(payload["campaigns"]), 1)
        self.assertEqual(payload["campaigns"][0]["campaign_id"], "campaign-v1.0.0")
        serialized = json.dumps(payload)
        self.assertNotIn(str(primary), serialized)
        self.assertNotIn(str(other), serialized)

    def test_same_campaign_id_in_two_directories_fails_closed(self) -> None:
        first = self._init_repo(self.root / "first")
        second = self._init_repo(self.root / "second")
        first_dir = first / ".code-mower" / "campaigns"
        second_dir = second / ".code-mower" / "campaigns"
        self.assertEqual(self._create(first, campaigns_dir=first_dir), 0)
        self.assertEqual(self._create(second, campaigns_dir=second_dir), 0)
        identity = campaign_discovery.resolve_repo_identity(first)
        campaign_discovery.publish_campaigns_directory(first_dir, identity)
        campaign_discovery.publish_campaigns_directory(second_dir, identity)

        ret, stdout, stderr = self._status(first, campaign_id="campaign-v1.0.0")
        self.assertEqual(ret, 1)
        self.assertEqual(stdout, "")
        self.assertIn("campaign-v1.0.0", stderr)
        self.assertIn("--campaigns-dir", stderr)
        self.assertNotIn(str(first), stderr)
        self.assertNotIn(str(second), stderr)


if __name__ == "__main__":
    unittest.main()
