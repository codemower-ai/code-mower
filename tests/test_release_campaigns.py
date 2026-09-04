#!/usr/bin/env python3
"""Tests for release qualification campaigns."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from code_mower import board, release_campaigns
from code_mower.provider_registry import LaneLabels, ProviderLane


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


def _fake_local_cli_lane(*, command: str = "fake-provider-cli") -> ProviderLane:
    """A local_cli lane with a real, registry-configured campaign adapter."""
    return ProviderLane(
        lane_id="fake_cli",
        lane_type="audit",
        driver="local_cli",
        provider="codex",
        labels=LaneLabels(needs="needs-fake", done="fake-done", blocked="fake-blocked"),
        trigger_policy="manual",
        provider_config={
            "command": command,
            "campaign_adapter_argv": (
                "{command}",
                "qualify",
                "--release-tag",
                "{release_tag}",
                "--package-spec",
                "{package_spec}",
                "--output",
                "{output}",
            ),
        },
    )


class ReleaseCampaignTests(unittest.TestCase):
    """Focused mocked tests for release qualification campaigns."""

    def test_dry_run_by_default(self) -> None:
        """Campaign creation defaults to dry-run and executes no mutations or calls."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            adapter_mock = mock.MagicMock()
            cmd_runner_mock = mock.MagicMock()

            result = release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                campaigns_dir=campaigns_dir,
                apply=False,
                adapter_runner=adapter_mock,
                command_runner=cmd_runner_mock,
            )
            self.assertEqual(result, 0)
            adapter_mock.assert_not_called()
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

    def test_local_cli_without_adapter_cannot_complete(self) -> None:
        """A local_cli provider with no configured campaign adapter never fabricates a result."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            adapter_mock = mock.MagicMock()

            result = release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["codex"],
                campaigns_dir=campaigns_dir,
                apply=True,
                which_fn=lambda _cmd: "/bin/codex",
                adapter_runner=adapter_mock,
            )
            self.assertEqual(result, 0)
            adapter_mock.assert_not_called()

            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            provider_entry = saved["providers"][0]
            self.assertEqual(provider_entry["state"], "unavailable")
            self.assertEqual(provider_entry["error"], "no_campaign_adapter_configured")
            self.assertIsNone(provider_entry["adoption_result"])
            self.assertIn("record manual result", provider_entry["next_action"])

    def test_apply_invokes_configured_adapter_before_completion(self) -> None:
        """A configured local_cli adapter must actually run (argv only) before a provider completes."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            fake_lane = _fake_local_cli_lane()
            invocations: list[list[str]] = []

            def fake_adapter_runner(argv, timeout):
                invocations.append(list(argv))
                output_path = Path(argv[argv.index("--output") + 1])
                adoption_res = _mock_adoption_result(release_tag="v1.0.0", provider="codex", outcome="pass")
                with output_path.open("w", encoding="utf-8") as fh:
                    json.dump(adoption_res, fh)
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            with mock.patch.object(
                release_campaigns, "resolve_provider_lane", return_value=("codex", fake_lane)
            ):
                result = release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    providers=["codex"],
                    campaigns_dir=campaigns_dir,
                    apply=True,
                    which_fn=lambda _cmd: "/bin/fake-provider-cli",
                    adapter_runner=fake_adapter_runner,
                )

            self.assertEqual(result, 0)
            self.assertEqual(len(invocations), 1)
            self.assertEqual(invocations[0][0], "/bin/fake-provider-cli")
            self.assertIn("v1.0.0", invocations[0])

            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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

    def test_adapter_invocation_without_result_file_cannot_complete(self) -> None:
        """If the adapter runs but writes no result file, the provider is blocked, not complete."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            fake_lane = _fake_local_cli_lane()

            def no_output_adapter_runner(argv, timeout):
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            with mock.patch.object(
                release_campaigns, "resolve_provider_lane", return_value=("codex", fake_lane)
            ):
                release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    providers=["codex"],
                    campaigns_dir=campaigns_dir,
                    apply=True,
                    which_fn=lambda _cmd: "/bin/fake-provider-cli",
                    adapter_runner=no_output_adapter_runner,
                )

            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            provider_entry = saved["providers"][0]
            self.assertEqual(provider_entry["state"], "blocked")
            self.assertEqual(provider_entry["error"], "adapter_produced_no_result")
            self.assertIsNone(provider_entry["adoption_result"])

    def test_adapter_result_identity_mismatch_is_rejected(self) -> None:
        """An adapter result for the wrong provider/tag is rejected, not accepted as evidence."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            fake_lane = _fake_local_cli_lane()

            def mismatched_adapter_runner(argv, timeout):
                output_path = Path(argv[argv.index("--output") + 1])
                # Wrong provider label -- must not be silently relabeled/accepted.
                adoption_res = _mock_adoption_result(release_tag="v1.0.0", provider="claude", outcome="pass")
                with output_path.open("w", encoding="utf-8") as fh:
                    json.dump(adoption_res, fh)
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            with mock.patch.object(
                release_campaigns, "resolve_provider_lane", return_value=("codex", fake_lane)
            ):
                release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    providers=["codex"],
                    campaigns_dir=campaigns_dir,
                    apply=True,
                    which_fn=lambda _cmd: "/bin/fake-provider-cli",
                    adapter_runner=mismatched_adapter_runner,
                )

            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            provider_entry = saved["providers"][0]
            self.assertEqual(provider_entry["state"], "blocked")
            self.assertEqual(provider_entry["error"], "adapter_result_mismatch")

    def test_adapter_result_with_extra_fields_is_rejected(self) -> None:
        """Undeclared fields (e.g. raw output or paths) in an adapter result are rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            fake_lane = _fake_local_cli_lane()

            def dirty_adapter_runner(argv, timeout):
                output_path = Path(argv[argv.index("--output") + 1])
                adoption_res = _mock_adoption_result(release_tag="v1.0.0", provider="codex", outcome="pass")
                adoption_res["stdout"] = "some raw provider output"
                with output_path.open("w", encoding="utf-8") as fh:
                    json.dump(adoption_res, fh)
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            with mock.patch.object(
                release_campaigns, "resolve_provider_lane", return_value=("codex", fake_lane)
            ):
                release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    providers=["codex"],
                    campaigns_dir=campaigns_dir,
                    apply=True,
                    which_fn=lambda _cmd: "/bin/fake-provider-cli",
                    adapter_runner=dirty_adapter_runner,
                )

            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            provider_entry = saved["providers"][0]
            self.assertEqual(provider_entry["state"], "blocked")
            self.assertEqual(provider_entry["error"], "adapter_result_invalid")
            serialized = json.dumps(saved)
            self.assertNotIn("stdout", serialized.lower())

    def test_idempotent_resume_does_not_reinvoke_adapter(self) -> None:
        """Resume does not re-dispatch already completed providers."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            fake_lane = _fake_local_cli_lane()
            invocations: list[list[str]] = []

            def fake_adapter_runner(argv, timeout):
                invocations.append(list(argv))
                output_path = Path(argv[argv.index("--output") + 1])
                adoption_res = _mock_adoption_result(release_tag="v1.0.0", provider="codex", outcome="pass")
                with output_path.open("w", encoding="utf-8") as fh:
                    json.dump(adoption_res, fh)
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            with mock.patch.object(
                release_campaigns, "resolve_provider_lane", return_value=("codex", fake_lane)
            ):
                # 1. Initial applied run
                release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    providers=["codex"],
                    campaigns_dir=campaigns_dir,
                    apply=True,
                    which_fn=lambda _cmd: "/bin/fake-provider-cli",
                    adapter_runner=fake_adapter_runner,
                )
                self.assertEqual(len(invocations), 1)

                saved_before = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
                assert saved_before is not None
                idemp_key = saved_before["providers"][0]["idempotency_key"]

                # 2. Resume run - must NOT re-invoke the adapter
                release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    campaigns_dir=campaigns_dir,
                    resume=True,
                    apply=True,
                    which_fn=lambda _cmd: "/bin/fake-provider-cli",
                    adapter_runner=fake_adapter_runner,
                )
                self.assertEqual(len(invocations), 1)  # Still 1, not duplicated!

            saved_after = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
            assert saved_after is not None
            self.assertEqual(saved_after["providers"][0]["idempotency_key"], idemp_key)
            self.assertEqual(saved_after["providers"][0]["state"], "complete")

    def test_provider_unavailable(self) -> None:
        """Missing prerequisites degrade gracefully to unavailable with actionable next steps."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"

            # Neither a configured adapter, CLI binary, nor tokens are available
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
            # antigravity is local_cli but has no campaign adapter configured, so it
            # must fail closed on that basis rather than suggesting a CLI install
            # that would never actually be invoked.
            self.assertEqual(providers_by_name["antigravity"]["state"], "unavailable")
            self.assertEqual(providers_by_name["antigravity"]["error"], "no_campaign_adapter_configured")
            self.assertIn("record manual result", providers_by_name["antigravity"]["next_action"])

            self.assertEqual(providers_by_name["devin"]["state"], "unavailable")
            self.assertIn("DEVIN_AUDIT_LABEL_TOKEN", providers_by_name["devin"]["next_action"])

    def test_github_dispatch_failure_persists_only_a_safe_error_code(self) -> None:
        """GitHub dispatch failure leaves useful local status without persisting raw gh output."""
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
            self.assertEqual(cursor_p["error"], "github_dispatch_failed")
            serialized = json.dumps(saved)
            self.assertNotIn("github.com", serialized)
            self.assertNotIn("resolve host", serialized)

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
            fake_lane = _fake_local_cli_lane()

            def fake_adapter_runner(argv, timeout):
                output_path = Path(argv[argv.index("--output") + 1])
                adoption_res = _mock_adoption_result(release_tag="v1.0.0", provider="codex", outcome="pass")
                with output_path.open("w", encoding="utf-8") as fh:
                    json.dump(adoption_res, fh)
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            with mock.patch.object(
                release_campaigns, "resolve_provider_lane", return_value=("codex", fake_lane)
            ):
                release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    providers=["codex"],
                    campaigns_dir=campaigns_dir,
                    apply=True,
                    which_fn=lambda _cmd: "/bin/fake-provider-cli",
                    adapter_runner=fake_adapter_runner,
                )

            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            serialized = json.dumps(saved)

            self.assertNotIn(str(Path(tmp)), serialized)
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

    def test_manual_adoption_result_provider_mismatch_rejected(self) -> None:
        """A manual result labeled for a different provider is rejected, not relabeled."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            result_file = Path(tmp) / "result.json"

            adoption_res = _mock_adoption_result(release_tag="v1.0.0", provider="claude", outcome="pass")
            with result_file.open("w", encoding="utf-8") as fh:
                json.dump(adoption_res, fh)

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["devin"],
                campaigns_dir=campaigns_dir,
                apply=False,
            )

            ret = release_campaigns.campaign_command(
                campaigns_dir=campaigns_dir,
                record_result=result_file,
                record_provider="devin",
                release_tag="v1.0.0",
            )
            self.assertEqual(ret, 1)

            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            # Rejected recording must leave the provider's prior (dry-run) state untouched.
            self.assertEqual(saved["providers"][0]["state"], "unavailable")
            self.assertIsNone(saved["providers"][0]["adoption_result"])

    def test_poll_requires_bound_comment_marker(self) -> None:
        """A bare adoptionResult JSON with no identity-bound wrapper is not accepted."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            adoption_res = _mock_adoption_result(release_tag="v1.0.0", provider="cursor_bugbot", outcome="pass")
            unbound_marker = f"<!-- CODE_MOWER_ADOPTION_RESULT: {json.dumps(adoption_res)} -->"

            def mock_gh_json(args, **kwargs):
                return {"comments": [{"body": f"Review complete!\n\n{unbound_marker}"}]}, ""

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

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                resume=True,
                repo_slug="owner/repo",
                gh_json_runner=mock_gh_json,
            )

            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            self.assertEqual(saved["providers"][0]["state"], "running")

    def test_poll_discovers_identity_bound_github_comment(self) -> None:
        """Polling detects a result marker only when campaign/provider/tag/idempotency all bind."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"

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

            idempotency_key = campaign.providers[0]["idempotency_key"]
            adoption_res = _mock_adoption_result(release_tag="v1.0.0", provider="cursor_bugbot", outcome="pass")
            wrapper = {
                "schema": release_campaigns.RESULT_MARKER_SCHEMA,
                "campaign_id": campaign.campaign_id,
                "provider": "cursor_bugbot",
                "release_tag": "v1.0.0",
                "idempotency_key": idempotency_key,
                "adoption_result": adoption_res,
            }
            marker = f"<!-- CODE_MOWER_ADOPTION_RESULT: {json.dumps(wrapper)} -->"

            def mock_gh_json(args, **kwargs):
                return {
                    "comments": [
                        {"body": "Starting review..."},
                        {"body": f"Review complete!\n\n{marker}"},
                    ]
                }, ""

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

    def test_poll_rejects_wrong_idempotency_key(self) -> None:
        """A result marker with a mismatched idempotency_key is rejected (replay/stale protection)."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"

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

            adoption_res = _mock_adoption_result(release_tag="v1.0.0", provider="cursor_bugbot", outcome="pass")
            wrapper = {
                "schema": release_campaigns.RESULT_MARKER_SCHEMA,
                "campaign_id": campaign.campaign_id,
                "provider": "cursor_bugbot",
                "release_tag": "v1.0.0",
                "idempotency_key": "wrong-key",
                "adoption_result": adoption_res,
            }
            marker = f"<!-- CODE_MOWER_ADOPTION_RESULT: {json.dumps(wrapper)} -->"

            def mock_gh_json(args, **kwargs):
                return {"comments": [{"body": marker}]}, ""

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                resume=True,
                repo_slug="owner/repo",
                gh_json_runner=mock_gh_json,
            )

            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            self.assertEqual(saved["providers"][0]["state"], "running")


if __name__ == "__main__":
    unittest.main()
