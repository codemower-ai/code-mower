#!/usr/bin/env python3
"""Tests for release qualification campaigns."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from code_mower import board, release_campaigns


def _mock_adoption_result(
    release_tag: str = "v1.0.0",
    provider: str = "claude",
    outcome: str = "pass",
) -> dict[str, Any]:
    return {
        "schema": "code_mower.adoptionResult.v1",
        "timestamp_utc": "2026-09-04T08:00:00Z",
        "release_tag": release_tag,
        "package_identity": "code-mower",
        "normalized_version": "1.0.0",
        "qualification_context": "cold_install",
        "starting_version": "",
        "ending_version": "1.0.0",
        "provider": provider,
        "executor": f"{provider}_cli",
        "host_class": "local",
        "runtime_class": "python_3.12",
        "execution_state": "executed",
        "elapsed_seconds": 12.34,
        "outcome": outcome,
        "steps": [
            {
                "id": "doctor",
                "status": "pass",
                "elapsed_seconds": 1.2,
                "warning_count": 0,
                "owner_action_count": 0,
            },
            {
                "id": "package_install",
                "status": outcome if outcome in {"pass", "fail"} else "pass",
                "elapsed_seconds": 11.14,
                "warning_count": 0,
                "owner_action_count": 0,
            },
        ],
    }


class ReleaseCampaignTests(unittest.TestCase):
    """Focused mocked tests for release qualification campaigns."""

    def test_dry_run_by_default(self) -> None:
        """Campaign creation defaults to dry-run and executes no mutations or calls."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            run_qual_mock = mock.MagicMock()
            cmd_runner_mock = mock.MagicMock()

            result = release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                campaigns_dir=campaigns_dir,
                apply=False,
                run_qualification_fn=run_qual_mock,
                command_runner=cmd_runner_mock,
            )
            self.assertEqual(result, 0)
            run_qual_mock.assert_not_called()
            cmd_runner_mock.assert_not_called()

            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
            self.assertIsNotNone(saved)
            assert saved is not None
            self.assertTrue(saved["dry_run"])
            self.assertEqual(saved["status"], "queued")
            self.assertIn("run with --apply", saved["next_action"])

            # Providers are queued or unavailable
            for p in saved["providers"]:
                self.assertIn(p["state"], {"queued", "unavailable"})
                self.assertEqual(p["dispatch_mode"], "dry_run")
                self.assertIsNone(p["dispatched_at"])
                self.assertIsNone(p["completed_at"])

    def test_apply_executes_available_local_provider(self) -> None:
        """Running with --apply executes qualification for available local CLI provider."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            adoption_res = _mock_adoption_result(release_tag="v1.0.0", provider="codex", outcome="pass")
            run_qual_mock = mock.MagicMock(return_value=adoption_res)

            result = release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["codex"],
                campaigns_dir=campaigns_dir,
                apply=True,
                which_fn=lambda _cmd: "/bin/codex",
                run_qualification_fn=run_qual_mock,
            )
            self.assertEqual(result, 0)
            self.assertEqual(run_qual_mock.call_count, 1)

            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
            self.assertIsNotNone(saved)
            assert saved is not None
            self.assertFalse(saved["dry_run"])
            self.assertEqual(saved["status"], "complete")

            provider_entry = saved["providers"][0]
            self.assertEqual(provider_entry["provider"], "codex")
            self.assertEqual(provider_entry["state"], "complete")
            self.assertEqual(provider_entry["next_action"], "none")
            self.assertIsNotNone(provider_entry["dispatched_at"])
            self.assertIsNotNone(provider_entry["completed_at"])
            self.assertEqual(provider_entry["adoption_result"]["outcome"], "pass")

    def test_idempotent_resume(self) -> None:
        """Resume does not re-dispatch already completed or running providers."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            adoption_res = _mock_adoption_result(release_tag="v1.0.0", provider="codex", outcome="pass")
            run_qual_mock = mock.MagicMock(return_value=adoption_res)

            # 1. Initial applied run
            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["codex"],
                campaigns_dir=campaigns_dir,
                apply=True,
                which_fn=lambda _cmd: "/bin/codex",
                run_qualification_fn=run_qual_mock,
            )
            self.assertEqual(run_qual_mock.call_count, 1)

            saved_before = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
            assert saved_before is not None
            idemp_key = saved_before["providers"][0]["idempotency_key"]

            # 2. Resume run - must NOT re-call qualification runner
            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                resume=True,
                apply=True,
                which_fn=lambda _cmd: "/bin/codex",
                run_qualification_fn=run_qual_mock,
            )
            self.assertEqual(run_qual_mock.call_count, 1)  # Still 1, not duplicated!

            saved_after = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
            assert saved_after is not None
            self.assertEqual(saved_after["providers"][0]["idempotency_key"], idemp_key)
            self.assertEqual(saved_after["providers"][0]["state"], "complete")

    def test_provider_unavailable(self) -> None:
        """Missing CLI tools or tokens degrade gracefully to unavailable with actionable next steps."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"

            # Neither binary nor tokens available
            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["antigravity", "devin"],
                campaigns_dir=campaigns_dir,
                apply=True,
                which_fn=lambda _cmd: None,
                env={},
            )

            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            self.assertEqual(saved["status"], "unavailable")

            providers_by_name = {p["provider"]: p for p in saved["providers"]}
            self.assertEqual(providers_by_name["antigravity"]["state"], "unavailable")
            self.assertIn("install agy CLI", providers_by_name["antigravity"]["next_action"])

            self.assertEqual(providers_by_name["devin"]["state"], "unavailable")
            self.assertIn("DEVIN_AUDIT_LABEL_TOKEN", providers_by_name["devin"]["next_action"])

    def test_github_unavailable(self) -> None:
        """GitHub network failure leaves useful local status without corrupting campaign."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"

            def failing_command_runner(args, **kwargs):
                class MockCompleted:
                    returncode = 1
                    stdout = ""
                    stderr = "error: could not resolve host: github.com"
                return MockCompleted()

            def failing_gh_json(args, **kwargs):
                return None, "network down: connection timeout"

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["cursor_bugbot"],
                campaigns_dir=campaigns_dir,
                repo_slug="owner/repo",
                issue="42",
                apply=True,
                command_runner=failing_command_runner,
                gh_json_runner=failing_gh_json,
                env={"CURSOR_BUGBOT_AUDIT_LABEL_TOKEN": "token"},
            )

            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            cursor_p = saved["providers"][0]
            self.assertEqual(cursor_p["state"], "unavailable")
            self.assertIn("retry cursor_bugbot dispatch when GitHub is available", cursor_p["next_action"])
            self.assertIn("github.com", cursor_p["error"])

    def test_local_only_status(self) -> None:
        """Campaign status inspects local state without requiring GitHub access."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"

            # Create campaign
            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                campaigns_dir=campaigns_dir,
                apply=False,
            )

            # Query status with a runner that would fail if called
            failing_gh = mock.MagicMock(side_effect=RuntimeError("GitHub was called!"))
            res = release_campaigns.campaign_command(
                status=True,
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                gh_json_runner=failing_gh,
            )
            self.assertEqual(res, 0)
            failing_gh.assert_not_called()

    def test_privacy_and_path_hygiene(self) -> None:
        """Campaign payload contains no private paths, raw output, diffs, or secrets."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            adoption_res = _mock_adoption_result(release_tag="v1.0.0", provider="codex", outcome="pass")

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["codex"],
                campaigns_dir=campaigns_dir,
                apply=True,
                which_fn=lambda _cmd: "/bin/codex",
                run_qualification_fn=lambda **kw: adoption_res,
            )

            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            serialized = json.dumps(saved)

            self.assertNotIn("/Users/", serialized)
            self.assertNotIn("/private/tmp", serialized)
            self.assertNotIn("token", serialized.lower())
            self.assertNotIn("secret", serialized.lower())
            self.assertNotIn("diff", serialized.lower())
            self.assertNotIn("prompt", serialized.lower())
            self.assertNotIn("stdout", serialized.lower())
            self.assertNotIn("stderr", serialized.lower())

    def test_board_projection(self) -> None:
        """Board payload accurately surfaces campaign cards with required states and fields."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            campaigns_dir = repo_path / ".code-mower" / "campaigns"

            # Create campaign
            release_campaigns.campaign_command(
                release_tag="v1.0.4",
                package_spec="code-mower==1.0.4",
                repo_path=repo_path,
                campaigns_dir=campaigns_dir,
                apply=False,
            )

            cfg = board.BoardConfig(repo="owner/repo", repo_path=str(repo_path))
            payload = board.release_campaigns_payload(cfg)

            self.assertEqual(payload["schema"], board.BOARD_RELEASE_CAMPAIGNS_SCHEMA)
            self.assertTrue(payload["available"])
            self.assertEqual(len(payload["campaigns"]), 1)

            proj_c = payload["campaigns"][0]
            self.assertEqual(proj_c["release_tag"], "v1.0.4")
            self.assertEqual(proj_c["status"], "queued")
            self.assertTrue(proj_c["dry_run"])

            cards = proj_c["cards"]
            self.assertEqual(len(cards), 6)
            for card in cards:
                self.assertIn("release", card)
                self.assertIn("provider", card)
                self.assertIn("environment", card)
                self.assertIn("state", card)
                self.assertIn("elapsed_seconds", card)
                self.assertIn("next_action", card)
                self.assertIn(card["state"], {"queued", "running", "blocked", "unavailable", "complete"})

    def test_selectable_providers_diversity(self) -> None:
        """Claude, Codex, Antigravity, Muse, Cursor/Grok Bot, and Devin are all included."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                campaigns_dir=campaigns_dir,
                apply=False,
            )

            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            provider_names = {p["provider"] for p in saved["providers"]}
            expected = {"claude", "codex", "antigravity", "muse", "cursor_bugbot", "devin"}
            self.assertEqual(provider_names, expected)

    def test_manual_adoption_result_recording(self) -> None:
        """Manual adoption result can be recorded into a campaign for explicit fallback."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            result_file = Path(tmp) / "result.json"

            adoption_res = _mock_adoption_result(release_tag="v1.0.0", provider="devin", outcome="pass")
            with result_file.open("w", encoding="utf-8") as fh:
                json.dump(adoption_res, fh)

            # Create campaign
            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["devin"],
                campaigns_dir=campaigns_dir,
                apply=False,
            )

            # Record manual result
            ret = release_campaigns.campaign_command(
                campaigns_dir=campaigns_dir,
                record_result=result_file,
                record_provider="devin",
                release_tag="v1.0.0",
            )
            self.assertEqual(ret, 0)

            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            self.assertEqual(saved["status"], "complete")
            self.assertEqual(saved["providers"][0]["state"], "complete")
            self.assertEqual(saved["providers"][0]["next_action"], "none")

    def test_poll_discovers_structured_github_comment(self) -> None:
        """Polling detects structured adoptionResult markers from GitHub issue comments."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            adoption_res = _mock_adoption_result(release_tag="v1.0.0", provider="cursor_bugbot", outcome="pass")
            marker = f"<!-- CODE_MOWER_ADOPTION_RESULT: {json.dumps(adoption_res)} -->"

            def mock_gh_json(args, **kwargs):
                return {
                    "comments": [
                        {"body": "Starting review..."},
                        {"body": f"Review complete!\n\n{marker}"},
                    ]
                }, ""

            # Create campaign
            campaign = release_campaigns.initialize_campaign(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["cursor_bugbot"],
                repo_slug="owner/repo",
            )
            campaign.status = "running"
            campaign.providers[0]["state"] = "running"
            campaign.providers[0]["dispatch_ref"] = {"issue_number": "99"}
            release_campaigns.save_campaign(campaign, campaigns_dir)

            # Resume/poll
            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                resume=True,
                repo_slug="owner/repo",
                gh_json_runner=mock_gh_json,
            )

            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            self.assertEqual(saved["providers"][0]["state"], "complete")
            self.assertEqual(saved["status"], "complete")


if __name__ == "__main__":
    unittest.main()
