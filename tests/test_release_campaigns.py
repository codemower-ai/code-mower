#!/usr/bin/env python3
"""Tests for release qualification campaigns."""

from __future__ import annotations

import contextlib
import io
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from code_mower import board, file_locks, release_campaigns, release_qualify
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


def _mock_adoption_result_full(
    *,
    release_tag: str = "v1.0.0",
    normalized_version: str = "1.0.0",
    provider: str = "claude",
    qualification_context: str = "cold_install",
    starting_version: str = "",
    ending_version: str = "1.0.0",
    outcome: str = "pass",
) -> dict[str, Any]:
    """Like _mock_adoption_result but with full control over context/version fields."""
    return {
        "schema": "code_mower.adoptionResult.v1",
        "timestamp_utc": "2026-09-04T08:00:00Z",
        "release_tag": release_tag,
        "package_identity": "code-mower",
        "normalized_version": normalized_version,
        "qualification_context": qualification_context,
        "starting_version": starting_version,
        "ending_version": ending_version,
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


def _fake_hosted_bridge_lane(*, bot_authors: tuple[str, ...] = ()) -> ProviderLane:
    """A hosted_bridge lane with no registry defaults, for controlled trust tests."""
    return ProviderLane(
        lane_id="fake_hosted",
        lane_type="audit",
        driver="hosted_bridge",
        provider="devin",
        labels=LaneLabels(needs="needs-fake", done="fake-done", blocked="fake-blocked"),
        trigger_policy="manual",
        provider_config={"bot_authors": bot_authors} if bot_authors else {},
    )


class ResolveProviderLaneTests(unittest.TestCase):
    """resolve_provider_lane must fail closed on unknown provider names."""

    def test_known_alias_resolves(self) -> None:
        canonical, lane = release_campaigns.resolve_provider_lane("claude")
        self.assertEqual(canonical, "claude")
        self.assertEqual(lane.lane_id, "claude_audit")

    def test_unknown_provider_name_raises(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            release_campaigns.resolve_provider_lane("totally-made-up-provider")
        self.assertIn("unknown release campaign provider", str(ctx.exception))

    def test_unknown_provider_never_fabricates_manual_lane(self) -> None:
        """A typo'd provider must error, never silently become a manual audit lane."""
        with self.assertRaises(ValueError):
            release_campaigns.resolve_provider_lane("clawd")

    def test_cli_rejects_unknown_provider_in_campaign_creation(self) -> None:
        """CLI-facing: creating a campaign with an unknown provider name fails closed."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            result = release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["not-a-real-provider"],
                campaigns_dir=campaigns_dir,
                apply=False,
            )
            self.assertEqual(result, 1)
            self.assertIsNone(release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir))


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

            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
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

            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
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

            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
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

            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
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

            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            provider_entry = saved["providers"][0]
            self.assertEqual(provider_entry["state"], "blocked")
            self.assertEqual(provider_entry["error"], "adapter_result_mismatch")

    def test_adapter_result_context_mismatch_is_rejected(self) -> None:
        """An adapter result for the right provider/tag but wrong qualification_context

        (cold-install vs. upgrade) is rejected, not accepted as evidence.
        """
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            fake_lane = _fake_local_cli_lane()

            def cold_install_adapter_runner(argv, timeout):
                output_path = Path(argv[argv.index("--output") + 1])
                adoption_res = _mock_adoption_result_full(
                    release_tag="v1.1.0",
                    normalized_version="1.1.0",
                    provider="codex",
                    qualification_context="cold_install",
                    starting_version="",
                    ending_version="1.1.0",
                )
                with output_path.open("w", encoding="utf-8") as fh:
                    json.dump(adoption_res, fh)
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            with mock.patch.object(
                release_campaigns, "resolve_provider_lane", return_value=("codex", fake_lane)
            ):
                release_campaigns.campaign_command(
                    release_tag="v1.1.0",
                    package_spec="code-mower==1.1.0",
                    providers=["codex"],
                    qualification_context="upgrade",
                    starting_version="1.0.0",
                    campaigns_dir=campaigns_dir,
                    apply=True,
                    which_fn=lambda _cmd: "/bin/fake-provider-cli",
                    adapter_runner=cold_install_adapter_runner,
                )

            saved = release_campaigns.load_campaign_by_id("campaign-v1.1.0", campaigns_dir)
            assert saved is not None
            provider_entry = saved["providers"][0]
            self.assertEqual(provider_entry["state"], "blocked")
            self.assertEqual(provider_entry["error"], "adapter_result_mismatch")
            self.assertIsNone(provider_entry["adoption_result"])

    def test_adapter_result_starting_version_mismatch_is_rejected(self) -> None:
        """An upgrade adapter result from a different starting_version is rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            fake_lane = _fake_local_cli_lane()

            def wrong_start_adapter_runner(argv, timeout):
                output_path = Path(argv[argv.index("--output") + 1])
                adoption_res = _mock_adoption_result_full(
                    release_tag="v1.1.0",
                    normalized_version="1.1.0",
                    provider="codex",
                    qualification_context="upgrade",
                    starting_version="0.9.0",
                    ending_version="1.1.0",
                )
                with output_path.open("w", encoding="utf-8") as fh:
                    json.dump(adoption_res, fh)
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            with mock.patch.object(
                release_campaigns, "resolve_provider_lane", return_value=("codex", fake_lane)
            ):
                release_campaigns.campaign_command(
                    release_tag="v1.1.0",
                    package_spec="code-mower==1.1.0",
                    providers=["codex"],
                    qualification_context="upgrade",
                    starting_version="1.0.0",
                    campaigns_dir=campaigns_dir,
                    apply=True,
                    which_fn=lambda _cmd: "/bin/fake-provider-cli",
                    adapter_runner=wrong_start_adapter_runner,
                )

            saved = release_campaigns.load_campaign_by_id("campaign-v1.1.0", campaigns_dir)
            assert saved is not None
            provider_entry = saved["providers"][0]
            self.assertEqual(provider_entry["state"], "blocked")
            self.assertEqual(provider_entry["error"], "adapter_result_mismatch")
            self.assertIsNone(provider_entry["adoption_result"])

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

            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            provider_entry = saved["providers"][0]
            self.assertEqual(provider_entry["state"], "blocked")
            self.assertEqual(provider_entry["error"], "adapter_result_invalid")
            serialized = json.dumps(saved)
            self.assertNotIn("stdout", serialized.lower())

    def test_malformed_adapter_argv_template_degrades_safely(self) -> None:
        """An invalid campaign_adapter_argv placeholder never crashes or leaks the template."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            bad_lane = ProviderLane(
                lane_id="fake_cli",
                lane_type="audit",
                driver="local_cli",
                provider="codex",
                labels=LaneLabels(needs="needs-fake", done="fake-done", blocked="fake-blocked"),
                trigger_policy="manual",
                provider_config={
                    "command": "fake-provider-cli",
                    "campaign_adapter_argv": ("{command}", "--nonexistent-placeholder", "{bogus_field}"),
                },
            )
            adapter_mock = mock.MagicMock()

            with mock.patch.object(
                release_campaigns, "resolve_provider_lane", return_value=("codex", bad_lane)
            ):
                result = release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    providers=["codex"],
                    campaigns_dir=campaigns_dir,
                    apply=True,
                    which_fn=lambda _cmd: "/bin/fake-provider-cli",
                    adapter_runner=adapter_mock,
                )

            self.assertEqual(result, 0)
            adapter_mock.assert_not_called()

            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            provider_entry = saved["providers"][0]
            self.assertEqual(provider_entry["state"], "unavailable")
            self.assertEqual(provider_entry["error"], "adapter_configuration_invalid")
            serialized = json.dumps(saved)
            self.assertNotIn("bogus_field", serialized)
            self.assertNotIn("nonexistent-placeholder", serialized)

    def test_repo_config_overrides_campaign_adapter_argv_for_canonical_lane_id(self) -> None:
        """An adopter can wire up a shipped provider's adapter via code-mower.yml alone.

        muse_cli ships with no campaign_adapter_argv configured in
        provider_registry.py; the repo config override is the only way to run
        it without editing installed Python.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            campaigns_dir = repo_path / ".code-mower" / "campaigns"
            (repo_path / "code-mower.yml").write_text(
                "version: 1\n"
                "lanes:\n"
                "  muse_cli:\n"
                "    provider_config:\n"
                "      campaign_adapter_argv:\n"
                "        - \"{command}\"\n"
                "        - qualify\n"
                "        - --release-tag\n"
                "        - \"{release_tag}\"\n"
                "        - --package-spec\n"
                "        - \"{package_spec}\"\n"
                "        - --output\n"
                "        - \"{output}\"\n"
                "      campaign_adapter_timeout_seconds: 60\n",
                encoding="utf-8",
            )

            invocations: list[list[str]] = []

            def fake_adapter_runner(argv, timeout):
                invocations.append(list(argv))
                self.assertEqual(timeout, 60)
                output_path = Path(argv[argv.index("--output") + 1])
                adoption_res = _mock_adoption_result(release_tag="v1.0.0", provider="muse", outcome="pass")
                with output_path.open("w", encoding="utf-8") as fh:
                    json.dump(adoption_res, fh)
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["muse"],
                repo_path=repo_path,
                campaigns_dir=campaigns_dir,
                apply=True,
                which_fn=lambda _cmd: "/bin/muse",
                adapter_runner=fake_adapter_runner,
            )

            self.assertEqual(len(invocations), 1)
            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            self.assertEqual(saved["providers"][0]["state"], "complete")

    def test_repo_config_override_supports_provider_alias(self) -> None:
        """The repo config lookup accepts a provider alias, not just the canonical lane_id."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            campaigns_dir = repo_path / ".code-mower" / "campaigns"
            (repo_path / "code-mower.yml").write_text(
                "version: 1\n"
                "lanes:\n"
                "  muse:\n"
                "    provider_config:\n"
                "      campaign_adapter_argv:\n"
                "        - \"{command}\"\n"
                "        - qualify\n"
                "        - --output\n"
                "        - \"{output}\"\n",
                encoding="utf-8",
            )

            def fake_adapter_runner(argv, timeout):
                output_path = Path(argv[argv.index("--output") + 1])
                adoption_res = _mock_adoption_result(release_tag="v1.0.0", provider="muse", outcome="pass")
                with output_path.open("w", encoding="utf-8") as fh:
                    json.dump(adoption_res, fh)
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["muse"],
                repo_path=repo_path,
                campaigns_dir=campaigns_dir,
                apply=True,
                which_fn=lambda _cmd: "/bin/muse",
                adapter_runner=fake_adapter_runner,
            )

            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            self.assertEqual(saved["providers"][0]["state"], "complete")

    def test_repo_config_missing_is_no_override(self) -> None:
        """No code-mower.yml at repo_path means no override, not an error."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            campaigns_dir = repo_path / ".code-mower" / "campaigns"
            adapter_mock = mock.MagicMock()

            result = release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["muse"],
                repo_path=repo_path,
                campaigns_dir=campaigns_dir,
                apply=True,
                which_fn=lambda _cmd: "/bin/muse",
                adapter_runner=adapter_mock,
            )

            self.assertEqual(result, 0)
            adapter_mock.assert_not_called()
            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            self.assertEqual(saved["providers"][0]["error"], "no_campaign_adapter_configured")

    def test_repo_config_malformed_override_type_is_adapter_configuration_invalid(self) -> None:
        """A malformed campaign_adapter_argv override (not a list) degrades safely."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            campaigns_dir = repo_path / ".code-mower" / "campaigns"
            (repo_path / "code-mower.yml").write_text(
                "version: 1\n"
                "lanes:\n"
                "  muse_cli:\n"
                "    provider_config:\n"
                "      campaign_adapter_argv: not-a-list\n",
                encoding="utf-8",
            )
            adapter_mock = mock.MagicMock()

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["muse"],
                repo_path=repo_path,
                campaigns_dir=campaigns_dir,
                apply=True,
                which_fn=lambda _cmd: "/bin/muse",
                adapter_runner=adapter_mock,
            )

            adapter_mock.assert_not_called()
            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            self.assertEqual(saved["providers"][0]["error"], "adapter_configuration_invalid")

    def test_repo_config_unreadable_or_invalid_yaml_is_adapter_configuration_invalid(self) -> None:
        """An existing but structurally invalid code-mower.yml must not be treated as no override."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            campaigns_dir = repo_path / ".code-mower" / "campaigns"
            (repo_path / "code-mower.yml").write_text(
                "- this\n- is-a-list\n- not-a-mapping\n",
                encoding="utf-8",
            )
            adapter_mock = mock.MagicMock()

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["muse"],
                repo_path=repo_path,
                campaigns_dir=campaigns_dir,
                apply=True,
                which_fn=lambda _cmd: "/bin/muse",
                adapter_runner=adapter_mock,
            )

            adapter_mock.assert_not_called()
            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            self.assertEqual(saved["providers"][0]["error"], "adapter_configuration_invalid")
            serialized = json.dumps(saved)
            self.assertNotIn("not-a-mapping", serialized)

    def test_repo_config_non_mapping_lanes_is_adapter_configuration_invalid(self) -> None:
        """A present but non-mapping top-level `lanes` key must not be treated as no override."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            campaigns_dir = repo_path / ".code-mower" / "campaigns"
            (repo_path / "code-mower.yml").write_text(
                "version: 1\nlanes: not-a-mapping\n",
                encoding="utf-8",
            )
            adapter_mock = mock.MagicMock()

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["muse"],
                repo_path=repo_path,
                campaigns_dir=campaigns_dir,
                apply=True,
                which_fn=lambda _cmd: "/bin/muse",
                adapter_runner=adapter_mock,
            )

            adapter_mock.assert_not_called()
            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            self.assertEqual(saved["providers"][0]["error"], "adapter_configuration_invalid")
            serialized = json.dumps(saved)
            self.assertNotIn("not-a-mapping", serialized)

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

                saved_before = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
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

            saved_after = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved_after is not None
            self.assertEqual(saved_after["providers"][0]["idempotency_key"], idemp_key)
            self.assertEqual(saved_after["providers"][0]["state"], "complete")

    def test_failed_local_adapter_runs_once_then_only_on_explicit_retry(self) -> None:
        """A failed local adapter attempt is not silently repeated by ordinary resume."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            fake_lane = _fake_local_cli_lane()
            invocations: list[list[str]] = []

            def failing_adapter_runner(argv, timeout):
                invocations.append(list(argv))
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

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
                    adapter_runner=failing_adapter_runner,
                )
                self.assertEqual(len(invocations), 1)

                saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
                assert saved is not None
                self.assertEqual(saved["providers"][0]["state"], "blocked")
                self.assertEqual(saved["providers"][0]["error"], "adapter_exited_nonzero")
                self.assertIsNotNone(saved["providers"][0]["attempted_at"])

                # Ordinary resume must not repeat the failed attempt.
                release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    campaigns_dir=campaigns_dir,
                    resume=True,
                    apply=True,
                    which_fn=lambda _cmd: "/bin/fake-provider-cli",
                    adapter_runner=failing_adapter_runner,
                )
                self.assertEqual(len(invocations), 1)
                saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
                assert saved is not None
                self.assertIn("--retry-provider codex", saved["providers"][0]["next_action"])

                # Explicit --retry-provider runs it exactly once more.
                release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    campaigns_dir=campaigns_dir,
                    resume=True,
                    apply=True,
                    retry_provider="codex",
                    which_fn=lambda _cmd: "/bin/fake-provider-cli",
                    adapter_runner=failing_adapter_runner,
                )
                self.assertEqual(len(invocations), 2)

                # A further ordinary resume does not repeat the retried attempt either.
                release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    campaigns_dir=campaigns_dir,
                    resume=True,
                    apply=True,
                    which_fn=lambda _cmd: "/bin/fake-provider-cli",
                    adapter_runner=failing_adapter_runner,
                )
                self.assertEqual(len(invocations), 2)

    def test_retry_does_not_accept_stale_result_before_invoking(self) -> None:
        """An explicit retry must not be satisfied by a still-valid result file

        left on disk by the attempt being retried -- it must invoke exactly
        once more and reflect only the new adapter's result.
        """
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            fake_lane = _fake_local_cli_lane()
            invocations: list[list[str]] = []

            def failing_adapter_runner(argv, timeout):
                invocations.append(list(argv))
                output_path = Path(argv[argv.index("--output") + 1])
                adoption_res = _mock_adoption_result(release_tag="v1.0.0", provider="codex", outcome="fail")
                with output_path.open("w", encoding="utf-8") as fh:
                    json.dump(adoption_res, fh)
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            def passing_adapter_runner(argv, timeout):
                invocations.append(list(argv))
                output_path = Path(argv[argv.index("--output") + 1])
                adoption_res = _mock_adoption_result(release_tag="v1.0.0", provider="codex", outcome="pass")
                with output_path.open("w", encoding="utf-8") as fh:
                    json.dump(adoption_res, fh)
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            with mock.patch.object(
                release_campaigns, "resolve_provider_lane", return_value=("codex", fake_lane)
            ):
                # 1. Initial applied run produces a valid, but failing, result
                # file -- the adapter itself ran successfully and wrote a
                # schema-valid adoptionResult with outcome "fail".
                release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    providers=["codex"],
                    campaigns_dir=campaigns_dir,
                    apply=True,
                    which_fn=lambda _cmd: "/bin/fake-provider-cli",
                    adapter_runner=failing_adapter_runner,
                )
                self.assertEqual(len(invocations), 1)
                saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
                assert saved is not None
                self.assertEqual(saved["providers"][0]["state"], "blocked")
                assert saved["providers"][0]["adoption_result"] is not None
                self.assertEqual(saved["providers"][0]["adoption_result"]["outcome"], "fail")

                # 2. Explicit retry with a new adapter that now passes must
                # invoke exactly once more, and the campaign must reflect the
                # *new* result -- not the stale failing evidence still on disk.
                release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    campaigns_dir=campaigns_dir,
                    resume=True,
                    apply=True,
                    retry_provider="codex",
                    which_fn=lambda _cmd: "/bin/fake-provider-cli",
                    adapter_runner=passing_adapter_runner,
                )
                self.assertEqual(len(invocations), 2)
                saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
                assert saved is not None
                self.assertEqual(saved["providers"][0]["state"], "complete")
                self.assertEqual(saved["providers"][0]["adoption_result"]["outcome"], "pass")

    def test_drop_in_result_context_and_starting_version_binding(self) -> None:
        """A local drop-in result file must match this campaign's

        qualification_context and starting_version, not just provider/release_tag
        -- a cold-install result must not complete an upgrade campaign (or vice
        versa), and an upgrade result from a different starting version must
        not complete this one.
        """
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            results_dir = campaigns_dir / "results"
            results_dir.mkdir(parents=True)

            release_campaigns.campaign_command(
                release_tag="v1.1.0",
                package_spec="code-mower==1.1.0",
                providers=["devin"],
                qualification_context="upgrade",
                starting_version="1.0.0",
                campaigns_dir=campaigns_dir,
                apply=False,
            )

            drop_in_path = results_dir / "campaign-v1.1.0_devin.json"
            mismatches = {
                "context": _mock_adoption_result_full(
                    release_tag="v1.1.0",
                    normalized_version="1.1.0",
                    provider="devin",
                    qualification_context="cold_install",
                    starting_version="",
                    ending_version="1.1.0",
                ),
                "starting_version": _mock_adoption_result_full(
                    release_tag="v1.1.0",
                    normalized_version="1.1.0",
                    provider="devin",
                    qualification_context="upgrade",
                    starting_version="0.9.0",
                    ending_version="1.1.0",
                ),
            }

            for label, mismatched_result in mismatches.items():
                with self.subTest(mismatch=label):
                    with drop_in_path.open("w", encoding="utf-8") as fh:
                        json.dump(mismatched_result, fh)

                    release_campaigns.campaign_command(
                        release_tag="v1.1.0",
                        campaigns_dir=campaigns_dir,
                        resume=True,
                        apply=False,
                    )

                    saved = release_campaigns.load_campaign_by_id("campaign-v1.1.0", campaigns_dir)
                    assert saved is not None
                    provider = saved["providers"][0]
                    self.assertNotEqual(provider["state"], "complete")
                    self.assertIsNone(provider["adoption_result"])

    def test_failed_github_dispatch_runs_once_then_only_on_explicit_retry(self) -> None:
        """A failed hosted/GitHub dispatch attempt is not silently repeated by ordinary resume."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            dispatch_calls: list[Any] = []

            def failing_command_runner(args, **kwargs):
                dispatch_calls.append(args)

                class MockCompleted:
                    returncode = 1
                    stdout = ""
                    stderr = "error"

                return MockCompleted()

            common_kwargs = dict(
                package_spec="code-mower==1.0.0",
                providers=["cursor_bugbot"],
                campaigns_dir=campaigns_dir,
                repo_slug="owner/repo",
                issue="42",
                apply=True,
                command_runner=failing_command_runner,
                env={"CURSOR_BUGBOT_AUDIT_LABEL_TOKEN": "token"},
            )

            release_campaigns.campaign_command(release_tag="v1.0.0", **common_kwargs)
            self.assertEqual(len(dispatch_calls), 1)

            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            self.assertEqual(saved["providers"][0]["error"], "github_dispatch_failed")
            self.assertIsNotNone(saved["providers"][0]["attempted_at"])

            # Ordinary resume must not repeat the failed dispatch.
            release_campaigns.campaign_command(release_tag="v1.0.0", resume=True, **common_kwargs)
            self.assertEqual(len(dispatch_calls), 1)

            # Explicit --retry-provider dispatches exactly once more.
            release_campaigns.campaign_command(
                release_tag="v1.0.0", resume=True, retry_provider="cursor_bugbot", **common_kwargs
            )
            self.assertEqual(len(dispatch_calls), 2)

    def _running_cursor_bugbot_campaign(self, campaigns_dir: Path) -> "release_campaigns.ReleaseCampaign":
        campaign = release_campaigns.initialize_campaign(
            release_tag="v1.0.0",
            package_spec="code-mower==1.0.0",
            providers=["cursor_bugbot"],
            repo_slug="owner/repo",
        )
        campaign.status = "running"
        campaign.providers[0]["state"] = "running"
        campaign.providers[0]["dispatch_ref"] = {"issue_number": "99"}
        campaign.providers[0]["attempted_at"] = "2026-09-04T00:00:00Z"
        campaign.providers[0]["dispatched_at"] = "2026-09-04T00:00:00Z"
        release_campaigns.save_campaign(campaign, campaigns_dir)
        return campaign

    @staticmethod
    def _no_op_dispatch_command_runner(calls: list[Any]):
        def _run(args, **kwargs):
            calls.append(args)

            class MockCompleted:
                returncode = 0
                stdout = "{}"
                stderr = ""

            return MockCompleted()

        return _run

    def test_retry_provider_running_polls_first_and_completes_without_redispatch(self) -> None:
        """An explicit retry of a running provider polls first; a valid trusted result

        completes it and issues no redispatch.
        """
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            campaign = self._running_cursor_bugbot_campaign(campaigns_dir)

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
                return {"comments": [{"author": {"login": "cursor[bot]"}, "body": marker}]}, ""

            dispatch_calls: list[Any] = []

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                campaigns_dir=campaigns_dir,
                repo_slug="owner/repo",
                issue="99",
                resume=True,
                apply=True,
                retry_provider="cursor_bugbot",
                gh_json_runner=mock_gh_json,
                command_runner=self._no_op_dispatch_command_runner(dispatch_calls),
                env={"CURSOR_BUGBOT_AUDIT_LABEL_TOKEN": "token"},
            )

            self.assertEqual(dispatch_calls, [])
            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            self.assertEqual(saved["providers"][0]["state"], "complete")

    def test_retry_provider_running_redispatches_once_when_no_valid_result(self) -> None:
        """An explicit retry of a running provider with no valid trusted result

        proceeds to exactly one redispatch.
        """
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._running_cursor_bugbot_campaign(campaigns_dir)

            def mock_gh_json(args, **kwargs):
                return {"comments": []}, ""

            dispatch_calls: list[Any] = []

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                campaigns_dir=campaigns_dir,
                repo_slug="owner/repo",
                issue="99",
                resume=True,
                apply=True,
                retry_provider="cursor_bugbot",
                gh_json_runner=mock_gh_json,
                command_runner=self._no_op_dispatch_command_runner(dispatch_calls),
                env={"CURSOR_BUGBOT_AUDIT_LABEL_TOKEN": "token"},
            )

            self.assertEqual(len(dispatch_calls), 1)
            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            self.assertEqual(saved["providers"][0]["state"], "running")

    def test_retry_preview_of_running_provider_is_read_only(self) -> None:
        """--retry-provider without --apply must not dispatch or rewrite a still-

        running provider when the safe poll finds no new result -- it must
        preserve state/evidence and only point the operator at --apply.
        """
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._running_cursor_bugbot_campaign(campaigns_dir)

            def mock_gh_json(args, **kwargs):
                return {"comments": []}, ""

            dispatch_calls: list[Any] = []

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                repo_slug="owner/repo",
                issue="99",
                resume=True,
                apply=False,
                retry_provider="cursor_bugbot",
                gh_json_runner=mock_gh_json,
                command_runner=self._no_op_dispatch_command_runner(dispatch_calls),
                env={"CURSOR_BUGBOT_AUDIT_LABEL_TOKEN": "token"},
            )

            self.assertEqual(dispatch_calls, [])
            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            provider = saved["providers"][0]
            self.assertEqual(provider["state"], "running")
            self.assertEqual(provider["dispatched_at"], "2026-09-04T00:00:00Z")
            self.assertIn("--apply --retry-provider cursor_bugbot", provider["next_action"])

    def _blocked_codex_campaign(
        self, campaigns_dir: Path, *, error: str = "adapter_exited_nonzero"
    ) -> None:
        campaign = release_campaigns.initialize_campaign(
            release_tag="v1.0.0",
            package_spec="code-mower==1.0.0",
            providers=["codex"],
            repo_slug="owner/repo",
        )
        campaign.providers[0]["state"] = "blocked"
        campaign.providers[0]["attempted_at"] = "2026-09-04T00:00:00Z"
        campaign.providers[0]["error"] = error
        campaign.providers[0]["next_action"] = "inspect codex qualification failures"
        release_campaigns.save_campaign(campaign, campaigns_dir)

    def test_retry_preview_of_blocked_provider_is_read_only(self) -> None:
        """--retry-provider without --apply must not rewrite a previously

        blocked/unavailable attempted provider back to queued/unavailable,
        and must not invoke the adapter.
        """
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._blocked_codex_campaign(campaigns_dir)

            def forbidden_adapter_runner(argv, timeout):
                raise AssertionError("retry preview must not invoke the adapter")

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                resume=True,
                apply=False,
                retry_provider="codex",
                which_fn=lambda _cmd: "/bin/fake-provider-cli",
                adapter_runner=forbidden_adapter_runner,
            )

            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            provider = saved["providers"][0]
            self.assertEqual(provider["state"], "blocked")
            self.assertEqual(provider["error"], "adapter_exited_nonzero")
            self.assertIn("--apply --retry-provider codex", provider["next_action"])

    def test_ordinary_dry_run_resume_preserves_nonzero_adapter_failure(self) -> None:
        """Ordinary (non-retry) dry-run resume of an already-attempted provider

        blocked by a nonzero adapter exit must preserve its state, error, and
        attempted_at -- it must not merely observe it back into
        queued/unavailable capability guesswork.
        """
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._blocked_codex_campaign(campaigns_dir, error="adapter_exited_nonzero")

            def forbidden_adapter_runner(argv, timeout):
                raise AssertionError("dry-run resume must not invoke the adapter")

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                resume=True,
                apply=False,
                which_fn=lambda _cmd: None,
                adapter_runner=forbidden_adapter_runner,
            )

            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            provider = saved["providers"][0]
            self.assertEqual(provider["state"], "blocked")
            self.assertEqual(provider["error"], "adapter_exited_nonzero")
            self.assertEqual(provider["attempted_at"], "2026-09-04T00:00:00Z")
            self.assertIn("--apply --retry-provider codex", provider["next_action"])

    def test_ordinary_dry_run_resume_preserves_missing_output_failure(self) -> None:
        """Ordinary (non-retry) dry-run resume of an already-attempted provider

        blocked because the adapter produced no output file must preserve its
        state, error, and attempted_at.
        """
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._blocked_codex_campaign(campaigns_dir, error="adapter_produced_no_result")

            def forbidden_adapter_runner(argv, timeout):
                raise AssertionError("dry-run resume must not invoke the adapter")

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                resume=True,
                apply=False,
                which_fn=lambda _cmd: "/bin/fake-provider-cli",
                adapter_runner=forbidden_adapter_runner,
            )

            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            provider = saved["providers"][0]
            self.assertEqual(provider["state"], "blocked")
            self.assertEqual(provider["error"], "adapter_produced_no_result")
            self.assertEqual(provider["attempted_at"], "2026-09-04T00:00:00Z")
            self.assertIn("--apply --retry-provider codex", provider["next_action"])

    def test_ordinary_resume_of_running_provider_never_redispatches(self) -> None:
        """Ordinary resume (no --retry-provider) of a running provider stays poll-only,

        even when polling finds no valid trusted result.
        """
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._running_cursor_bugbot_campaign(campaigns_dir)

            def mock_gh_json(args, **kwargs):
                return {"comments": []}, ""

            dispatch_calls: list[Any] = []

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                campaigns_dir=campaigns_dir,
                repo_slug="owner/repo",
                issue="99",
                resume=True,
                apply=True,
                gh_json_runner=mock_gh_json,
                command_runner=self._no_op_dispatch_command_runner(dispatch_calls),
                env={"CURSOR_BUGBOT_AUDIT_LABEL_TOKEN": "token"},
            )

            self.assertEqual(dispatch_calls, [])
            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            self.assertEqual(saved["providers"][0]["state"], "running")

    def test_retry_provider_rejected_when_not_in_campaign(self) -> None:
        """--retry-provider must be validated against campaign membership."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["codex"],
                campaigns_dir=campaigns_dir,
                apply=False,
            )

            result = release_campaigns.campaign_command(
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                resume=True,
                apply=True,
                retry_provider="devin",
            )
            self.assertEqual(result, 1)

    def test_retry_provider_rejected_when_unknown(self) -> None:
        """--retry-provider must fail closed for a name that resolves to no provider at all."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["codex"],
                campaigns_dir=campaigns_dir,
                apply=False,
            )

            result = release_campaigns.campaign_command(
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                resume=True,
                apply=True,
                retry_provider="not-a-real-provider",
            )
            self.assertEqual(result, 1)

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

            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
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

            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
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

            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
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

    def test_board_projection_survives_malformed_persisted_campaigns(self) -> None:
        """One malformed campaign file never crashes or hides the Board payload."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            campaigns_dir = repo_path / ".code-mower" / "campaigns"
            campaigns_dir.mkdir(parents=True)

            healthy = {
                "schema": release_campaigns.CAMPAIGN_SCHEMA,
                "campaign_id": "campaign-v9.0.0",
                "release_tag": "v9.0.0",
                "package_spec": "code-mower==9.0.0",
                "qualification_context": "cold_install",
                "status": "running",
                "dry_run": False,
                "elapsed_seconds": 12.5,
                "next_action": "await provider result",
                "next_detail": "codex is running",
                "updated_at": "2026-09-04T09:00:00Z",
                "providers": [
                    {
                        "provider": "codex",
                        "lane_id": "codex_cli",
                        "environment": "local",
                        "state": "running",
                        "elapsed_seconds": 12.5,
                        "next_action": "await provider result",
                        "next_detail": "codex is running",
                    }
                ],
            }
            # Every scalar here is malformed in a different way: a missing
            # elapsed_seconds, a null, a nonnumeric string, NaN, infinity, and
            # a nested object where a string belongs. An older-schema file that
            # simply omits the newer fields is covered by the last provider.
            malformed = {
                "schema": release_campaigns.CAMPAIGN_SCHEMA,
                "campaign_id": "campaign-v8.0.0",
                "release_tag": "v8.0.0",
                "package_spec": "code-mower==8.0.0",
                "qualification_context": "upgrade",
                "status": "queued",
                "dry_run": "yes",
                "elapsed_seconds": "not-a-number",
                "next_action": {"raw": "nested object"},
                "next_detail": ["nested", "list"],
                "updated_at": "2026-09-04T08:00:00Z",
                "providers": [
                    {"provider": "claude", "state": "queued"},
                    {"provider": "muse", "state": "queued", "elapsed_seconds": None},
                    {"provider": "cursor", "state": "queued", "elapsed_seconds": "abc"},
                    {"provider": "devin", "state": "queued", "elapsed_seconds": float("nan")},
                    {"provider": "antigravity", "state": "queued", "elapsed_seconds": float("inf")},
                    {"provider": "gemini", "state": "queued", "elapsed_seconds": -5.0},
                    {"provider": "codex", "state": "queued", "elapsed_seconds": {"seconds": 3}},
                    "not-a-provider-object",
                ],
            }
            for campaign in (healthy, malformed):
                path = campaigns_dir / f"{campaign['campaign_id']}.json"
                with path.open("w", encoding="utf-8") as fh:
                    # allow_nan keeps NaN/Infinity literals, matching what a
                    # previously written file can already contain on disk.
                    json.dump(campaign, fh)

            before = sorted(f.read_text(encoding="utf-8") for f in campaigns_dir.glob("*.json"))
            cfg = board.BoardConfig(repo="owner/repo", repo_path=str(repo_path))
            payload = board.release_campaigns_payload(cfg)

            self.assertTrue(payload["available"])
            self.assertEqual(len(payload["campaigns"]), 2)
            projected = {c["campaign_id"]: c for c in payload["campaigns"]}

            healthy_projection = projected["campaign-v9.0.0"]
            self.assertEqual(healthy_projection["release_tag"], "v9.0.0")
            self.assertEqual(healthy_projection["status"], "running")
            self.assertFalse(healthy_projection["dry_run"])
            self.assertEqual(healthy_projection["elapsed_seconds"], 12.5)
            self.assertEqual(len(healthy_projection["cards"]), 1)
            self.assertEqual(healthy_projection["cards"][0]["elapsed_seconds"], 12.5)

            bad_projection = projected["campaign-v8.0.0"]
            self.assertEqual(bad_projection["elapsed_seconds"], 0.0)
            self.assertEqual(bad_projection["next_action"], "")
            self.assertEqual(bad_projection["next_detail"], "")
            self.assertTrue(bad_projection["dry_run"])
            # The one non-object provider entry is skipped; the rest survive.
            self.assertEqual(len(bad_projection["cards"]), 7)
            for card in bad_projection["cards"]:
                elapsed = card["elapsed_seconds"]
                self.assertIsInstance(elapsed, float)
                self.assertTrue(math.isfinite(elapsed))
                self.assertGreaterEqual(elapsed, 0.0)
                self.assertEqual(elapsed, 0.0)
                self.assertEqual(card["environment"], "local")
                self.assertEqual(card["lane_id"], card["provider"])
            self.assertEqual(payload["card_count"], 8)

            # Deterministic across repeated reads, and read-only on disk.
            self.assertEqual(payload, board.release_campaigns_payload(cfg))
            after = sorted(f.read_text(encoding="utf-8") for f in campaigns_dir.glob("*.json"))
            self.assertEqual(before, after)

            # Metadata-only: no traceback, raw source, or transcript content.
            serialized = json.dumps(payload)
            for leaked in ("Traceback", "not-a-number", "nested object", "not-a-provider-object", "NaN", "Infinity"):
                self.assertNotIn(leaked, serialized)

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

            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
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

            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
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

            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            # Rejected recording must leave the provider's prior (dry-run) state untouched.
            self.assertEqual(saved["providers"][0]["state"], "unavailable")
            self.assertIsNone(saved["providers"][0]["adoption_result"])

    def test_manual_result_context_mismatch_rejected(self) -> None:
        """A cold-install result must not be accepted for an upgrade campaign, or vice versa."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            result_file = Path(tmp) / "result.json"

            # Campaign expects an upgrade from 1.0.0, but the recorded result
            # is a cold-install result for the same provider/release_tag.
            adoption_res = _mock_adoption_result_full(
                release_tag="v1.1.0",
                normalized_version="1.1.0",
                provider="devin",
                qualification_context="cold_install",
                starting_version="",
                ending_version="1.1.0",
            )
            with result_file.open("w", encoding="utf-8") as fh:
                json.dump(adoption_res, fh)

            release_campaigns.campaign_command(
                release_tag="v1.1.0",
                package_spec="code-mower==1.1.0",
                providers=["devin"],
                qualification_context="upgrade",
                starting_version="1.0.0",
                campaigns_dir=campaigns_dir,
                apply=False,
            )

            ret = release_campaigns.campaign_command(
                campaigns_dir=campaigns_dir,
                record_result=result_file,
                record_provider="devin",
                release_tag="v1.1.0",
            )
            self.assertEqual(ret, 1)

            saved = release_campaigns.load_campaign_by_id("campaign-v1.1.0", campaigns_dir)
            assert saved is not None
            self.assertIsNone(saved["providers"][0]["adoption_result"])

    def test_manual_result_starting_version_mismatch_rejected(self) -> None:
        """An upgrade result for a different starting_version must not be accepted."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            result_file = Path(tmp) / "result.json"

            # Campaign expects an upgrade from 1.0.0; the recorded result is a
            # valid upgrade result, but from a different starting version.
            adoption_res = _mock_adoption_result_full(
                release_tag="v1.1.0",
                normalized_version="1.1.0",
                provider="devin",
                qualification_context="upgrade",
                starting_version="0.9.0",
                ending_version="1.1.0",
            )
            with result_file.open("w", encoding="utf-8") as fh:
                json.dump(adoption_res, fh)

            release_campaigns.campaign_command(
                release_tag="v1.1.0",
                package_spec="code-mower==1.1.0",
                providers=["devin"],
                qualification_context="upgrade",
                starting_version="1.0.0",
                campaigns_dir=campaigns_dir,
                apply=False,
            )

            ret = release_campaigns.campaign_command(
                campaigns_dir=campaigns_dir,
                record_result=result_file,
                record_provider="devin",
                release_tag="v1.1.0",
            )
            self.assertEqual(ret, 1)

            saved = release_campaigns.load_campaign_by_id("campaign-v1.1.0", campaigns_dir)
            assert saved is not None
            self.assertIsNone(saved["providers"][0]["adoption_result"])

    def test_record_result_missing_file_returns_bounded_cli_error(self) -> None:
        """A missing --record-result file must exit 1 with a bounded message, never a traceback."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            missing_path = Path(tmp) / "does-not-exist.json"

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["devin"],
                campaigns_dir=campaigns_dir,
                apply=False,
            )

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                ret = release_campaigns.campaign_command(
                    campaigns_dir=campaigns_dir,
                    record_result=missing_path,
                    record_provider="devin",
                    release_tag="v1.0.0",
                )
            self.assertEqual(ret, 1)
            output = stderr.getvalue()
            self.assertNotIn("Traceback", output)
            self.assertNotIn(str(missing_path), output)
            self.assertNotIn(tmp, output)

    def test_record_result_invalid_json_returns_bounded_cli_error(self) -> None:
        """An unreadable/invalid-JSON --record-result file exits 1, never a traceback."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            bad_json_path = Path(tmp) / "bad.json"
            bad_json_path.write_text("{not valid json", encoding="utf-8")

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["devin"],
                campaigns_dir=campaigns_dir,
                apply=False,
            )

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                ret = release_campaigns.campaign_command(
                    campaigns_dir=campaigns_dir,
                    record_result=bad_json_path,
                    record_provider="devin",
                    release_tag="v1.0.0",
                )
            self.assertEqual(ret, 1)
            output = stderr.getvalue()
            self.assertNotIn("Traceback", output)
            self.assertNotIn(str(bad_json_path), output)
            self.assertNotIn(tmp, output)

    def test_manual_result_with_unsafe_timestamp_never_enters_campaign_state(self) -> None:
        """A malicious timestamp_utc (path traversal / multiline) is rejected before it can be persisted."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            result_file = Path(tmp) / "result.json"

            for unsafe_timestamp in (
                "../../etc/passwd",
                "2026-09-04T08:00:00Z\nmalicious-injected-line",
                "2026-09-04T08:00:00",  # no UTC/offset designator
                "not-a-timestamp",
            ):
                with self.subTest(timestamp_utc=unsafe_timestamp):
                    adoption_res = _mock_adoption_result(
                        release_tag="v1.0.0", provider="devin", outcome="pass"
                    )
                    adoption_res["timestamp_utc"] = unsafe_timestamp
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

                    saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
                    assert saved is not None
                    self.assertIsNone(saved["providers"][0]["adoption_result"])
                    serialized = json.dumps(saved)
                    self.assertNotIn("malicious-injected-line", serialized)
                    self.assertNotIn("etc/passwd", serialized)

    def test_adapter_result_with_unsafe_timestamp_never_enters_campaign_state(self) -> None:
        """An adapter-produced result with an unsafe timestamp_utc is rejected, not persisted."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            fake_lane = _fake_local_cli_lane()

            def unsafe_timestamp_adapter_runner(argv, timeout):
                output_path = Path(argv[argv.index("--output") + 1])
                adoption_res = _mock_adoption_result(release_tag="v1.0.0", provider="codex", outcome="pass")
                adoption_res["timestamp_utc"] = "../../etc/passwd"
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
                    adapter_runner=unsafe_timestamp_adapter_runner,
                )

            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            provider_entry = saved["providers"][0]
            self.assertEqual(provider_entry["state"], "blocked")
            self.assertEqual(provider_entry["error"], "adapter_result_invalid")
            self.assertIsNone(provider_entry["adoption_result"])
            serialized = json.dumps(saved)
            self.assertNotIn("etc/passwd", serialized)

    def test_poll_requires_bound_comment_marker(self) -> None:
        """A bare adoptionResult JSON with no identity-bound wrapper is not accepted."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            adoption_res = _mock_adoption_result(release_tag="v1.0.0", provider="cursor_bugbot", outcome="pass")
            unbound_marker = f"<!-- CODE_MOWER_ADOPTION_RESULT: {json.dumps(adoption_res)} -->"

            def mock_gh_json(args, **kwargs):
                return {
                    "comments": [
                        {
                            "author": {"login": "cursor[bot]"},
                            "body": f"Review complete!\n\n{unbound_marker}",
                        }
                    ]
                }, ""

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

            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
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
                        {"author": {"login": "some-other-user"}, "body": "Starting review..."},
                        {"author": {"login": "cursor[bot]"}, "body": f"Review complete!\n\n{marker}"},
                    ]
                }, ""

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                resume=True,
                repo_slug="owner/repo",
                gh_json_runner=mock_gh_json,
            )

            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            self.assertEqual(saved["providers"][0]["state"], "complete")
            self.assertEqual(saved["status"], "complete")

    def test_idempotency_key_binds_starting_version(self) -> None:
        """Two upgrade dispatches that differ only by starting_version must get different keys.

        Otherwise a hosted comment result for one starting version could be
        replayed against a same-tag upgrade campaign from a different
        starting version, since the key would be identical.
        """
        key_a = release_campaigns._compute_idempotency_key(
            "campaign-v2.0.0", "claude", "v2.0.0", "upgrade", "1.0.0"
        )
        key_b = release_campaigns._compute_idempotency_key(
            "campaign-v2.0.0", "claude", "v2.0.0", "upgrade", "0.9.0"
        )
        self.assertNotEqual(key_a, key_b)

    def test_poll_rejects_upgrade_result_from_wrong_starting_version_despite_matching_key(
        self,
    ) -> None:
        """A same-tag upgrade result from starting version A cannot complete a campaign from B.

        This holds even when the wrapper's idempotency_key matches the real
        dispatch exactly -- the embedded adoption_result's own
        starting_version must independently match the campaign's.
        """
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"

            campaign = release_campaigns.initialize_campaign(
                release_tag="v2.0.0",
                package_spec="code-mower==2.0.0",
                qualification_context="upgrade",
                starting_version="1.0.0",
                providers=["cursor_bugbot"],
                repo_slug="owner/repo",
            )
            campaign.status = "running"
            campaign.providers[0]["state"] = "running"
            campaign.providers[0]["dispatch_ref"] = {"issue_number": "99"}
            release_campaigns.save_campaign(campaign, campaigns_dir)

            idempotency_key = campaign.providers[0]["idempotency_key"]
            adoption_res = _mock_adoption_result_full(
                release_tag="v2.0.0",
                normalized_version="2.0.0",
                provider="cursor_bugbot",
                qualification_context="upgrade",
                starting_version="0.9.0",
                ending_version="2.0.0",
                outcome="pass",
            )
            wrapper = {
                "schema": release_campaigns.RESULT_MARKER_SCHEMA,
                "campaign_id": campaign.campaign_id,
                "provider": "cursor_bugbot",
                "release_tag": "v2.0.0",
                "idempotency_key": idempotency_key,
                "adoption_result": adoption_res,
            }
            marker = f"<!-- CODE_MOWER_ADOPTION_RESULT: {json.dumps(wrapper)} -->"

            def mock_gh_json(args, **kwargs):
                return {"comments": [{"author": {"login": "cursor[bot]"}, "body": marker}]}, ""

            release_campaigns.campaign_command(
                release_tag="v2.0.0",
                campaigns_dir=campaigns_dir,
                resume=True,
                repo_slug="owner/repo",
                gh_json_runner=mock_gh_json,
            )

            saved = release_campaigns.load_campaign_by_id("campaign-v2.0.0", campaigns_dir)
            assert saved is not None
            self.assertEqual(saved["providers"][0]["state"], "running")
            self.assertIsNone(saved["providers"][0]["adoption_result"])

    def test_poll_rejects_cold_install_result_for_upgrade_campaign(self) -> None:
        """A cold-install adoption result cannot complete an upgrade campaign, even with a matching key."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"

            campaign = release_campaigns.initialize_campaign(
                release_tag="v2.0.0",
                package_spec="code-mower==2.0.0",
                qualification_context="upgrade",
                starting_version="1.0.0",
                providers=["cursor_bugbot"],
                repo_slug="owner/repo",
            )
            campaign.status = "running"
            campaign.providers[0]["state"] = "running"
            campaign.providers[0]["dispatch_ref"] = {"issue_number": "99"}
            release_campaigns.save_campaign(campaign, campaigns_dir)

            idempotency_key = campaign.providers[0]["idempotency_key"]
            adoption_res = _mock_adoption_result_full(
                release_tag="v2.0.0",
                normalized_version="2.0.0",
                provider="cursor_bugbot",
                qualification_context="cold_install",
                starting_version="",
                ending_version="2.0.0",
                outcome="pass",
            )
            wrapper = {
                "schema": release_campaigns.RESULT_MARKER_SCHEMA,
                "campaign_id": campaign.campaign_id,
                "provider": "cursor_bugbot",
                "release_tag": "v2.0.0",
                "idempotency_key": idempotency_key,
                "adoption_result": adoption_res,
            }
            marker = f"<!-- CODE_MOWER_ADOPTION_RESULT: {json.dumps(wrapper)} -->"

            def mock_gh_json(args, **kwargs):
                return {"comments": [{"author": {"login": "cursor[bot]"}, "body": marker}]}, ""

            release_campaigns.campaign_command(
                release_tag="v2.0.0",
                campaigns_dir=campaigns_dir,
                resume=True,
                repo_slug="owner/repo",
                gh_json_runner=mock_gh_json,
            )

            saved = release_campaigns.load_campaign_by_id("campaign-v2.0.0", campaigns_dir)
            assert saved is not None
            self.assertEqual(saved["providers"][0]["state"], "running")
            self.assertIsNone(saved["providers"][0]["adoption_result"])

    def test_poll_accepts_upgrade_result_matching_starting_version(self) -> None:
        """Sanity check: a correctly bound upgrade result still completes the campaign."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"

            campaign = release_campaigns.initialize_campaign(
                release_tag="v2.0.0",
                package_spec="code-mower==2.0.0",
                qualification_context="upgrade",
                starting_version="1.0.0",
                providers=["cursor_bugbot"],
                repo_slug="owner/repo",
            )
            campaign.status = "running"
            campaign.providers[0]["state"] = "running"
            campaign.providers[0]["dispatch_ref"] = {"issue_number": "99"}
            release_campaigns.save_campaign(campaign, campaigns_dir)

            idempotency_key = campaign.providers[0]["idempotency_key"]
            adoption_res = _mock_adoption_result_full(
                release_tag="v2.0.0",
                normalized_version="2.0.0",
                provider="cursor_bugbot",
                qualification_context="upgrade",
                starting_version="1.0.0",
                ending_version="2.0.0",
                outcome="pass",
            )
            wrapper = {
                "schema": release_campaigns.RESULT_MARKER_SCHEMA,
                "campaign_id": campaign.campaign_id,
                "provider": "cursor_bugbot",
                "release_tag": "v2.0.0",
                "idempotency_key": idempotency_key,
                "adoption_result": adoption_res,
            }
            marker = f"<!-- CODE_MOWER_ADOPTION_RESULT: {json.dumps(wrapper)} -->"

            def mock_gh_json(args, **kwargs):
                return {"comments": [{"author": {"login": "cursor[bot]"}, "body": marker}]}, ""

            release_campaigns.campaign_command(
                release_tag="v2.0.0",
                campaigns_dir=campaigns_dir,
                resume=True,
                repo_slug="owner/repo",
                gh_json_runner=mock_gh_json,
            )

            saved = release_campaigns.load_campaign_by_id("campaign-v2.0.0", campaigns_dir)
            assert saved is not None
            self.assertEqual(saved["providers"][0]["state"], "complete")

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
                return {"comments": [{"author": {"login": "cursor[bot]"}, "body": marker}]}, ""

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                resume=True,
                repo_slug="owner/repo",
                gh_json_runner=mock_gh_json,
            )

            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            self.assertEqual(saved["providers"][0]["state"], "running")

    def test_poll_rejects_spoofed_author(self) -> None:
        """A perfectly identity-bound marker from an untrusted commenter is ignored.

        The idempotency key, campaign_id, provider, and release_tag are all
        visible in the public dispatch comment, so anyone can copy them into a
        reply. Only a configured trusted author's comment may ever complete
        the provider.
        """
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
                        {"author": {"login": "random-attacker"}, "body": marker},
                    ]
                }, ""

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                resume=True,
                repo_slug="owner/repo",
                gh_json_runner=mock_gh_json,
            )

            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            self.assertEqual(saved["providers"][0]["state"], "running")
            self.assertIsNone(saved["providers"][0]["adoption_result"])

    def test_poll_rejects_marker_when_no_trusted_authors_configured(self) -> None:
        """A hosted_bridge lane with no bot_authors trusts nobody -- fail closed, not open."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"

            campaign = release_campaigns.initialize_campaign(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["devin"],
                repo_slug="owner/repo",
            )
            campaign.status = "running"
            campaign.providers[0]["state"] = "running"
            campaign.providers[0]["dispatch_ref"] = {"issue_number": "99"}
            release_campaigns.save_campaign(campaign, campaigns_dir)

            idempotency_key = campaign.providers[0]["idempotency_key"]
            adoption_res = _mock_adoption_result(release_tag="v1.0.0", provider="devin", outcome="pass")
            wrapper = {
                "schema": release_campaigns.RESULT_MARKER_SCHEMA,
                "campaign_id": campaign.campaign_id,
                "provider": "devin",
                "release_tag": "v1.0.0",
                "idempotency_key": idempotency_key,
                "adoption_result": adoption_res,
            }
            marker = f"<!-- CODE_MOWER_ADOPTION_RESULT: {json.dumps(wrapper)} -->"

            def mock_gh_json(args, **kwargs):
                return {"comments": [{"author": {"login": "devin-ai-integration[bot]"}, "body": marker}]}, ""

            fake_lane = _fake_hosted_bridge_lane()
            with mock.patch.object(release_campaigns, "resolve_provider_lane", return_value=("devin", fake_lane)):
                release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    campaigns_dir=campaigns_dir,
                    resume=True,
                    repo_slug="owner/repo",
                    gh_json_runner=mock_gh_json,
                )

            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            self.assertEqual(saved["providers"][0]["state"], "running")

    def test_devin_accepts_known_trusted_bot_author(self) -> None:
        """Devin's registry defaults trust devin-ai-integration[bot]'s bound reply."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"

            campaign = release_campaigns.initialize_campaign(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["devin"],
                repo_slug="owner/repo",
            )
            campaign.status = "running"
            campaign.providers[0]["state"] = "running"
            campaign.providers[0]["dispatch_ref"] = {"issue_number": "99"}
            release_campaigns.save_campaign(campaign, campaigns_dir)

            idempotency_key = campaign.providers[0]["idempotency_key"]
            adoption_res = _mock_adoption_result(release_tag="v1.0.0", provider="devin", outcome="pass")
            wrapper = {
                "schema": release_campaigns.RESULT_MARKER_SCHEMA,
                "campaign_id": campaign.campaign_id,
                "provider": "devin",
                "release_tag": "v1.0.0",
                "idempotency_key": idempotency_key,
                "adoption_result": adoption_res,
            }
            marker = f"<!-- CODE_MOWER_ADOPTION_RESULT: {json.dumps(wrapper)} -->"

            def mock_gh_json(args, **kwargs):
                return {"comments": [{"author": {"login": "devin-ai-integration[bot]"}, "body": marker}]}, ""

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                resume=True,
                repo_slug="owner/repo",
                gh_json_runner=mock_gh_json,
            )

            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            self.assertEqual(saved["providers"][0]["state"], "complete")

    def test_devin_rejects_spoofed_author(self) -> None:
        """A perfectly identity-bound marker from an untrusted commenter is ignored for Devin too."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"

            campaign = release_campaigns.initialize_campaign(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["devin"],
                repo_slug="owner/repo",
            )
            campaign.status = "running"
            campaign.providers[0]["state"] = "running"
            campaign.providers[0]["dispatch_ref"] = {"issue_number": "99"}
            release_campaigns.save_campaign(campaign, campaigns_dir)

            idempotency_key = campaign.providers[0]["idempotency_key"]
            adoption_res = _mock_adoption_result(release_tag="v1.0.0", provider="devin", outcome="pass")
            wrapper = {
                "schema": release_campaigns.RESULT_MARKER_SCHEMA,
                "campaign_id": campaign.campaign_id,
                "provider": "devin",
                "release_tag": "v1.0.0",
                "idempotency_key": idempotency_key,
                "adoption_result": adoption_res,
            }
            marker = f"<!-- CODE_MOWER_ADOPTION_RESULT: {json.dumps(wrapper)} -->"

            def mock_gh_json(args, **kwargs):
                return {"comments": [{"author": {"login": "random-attacker"}, "body": marker}]}, ""

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                resume=True,
                repo_slug="owner/repo",
                gh_json_runner=mock_gh_json,
            )

            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            self.assertEqual(saved["providers"][0]["state"], "running")
            self.assertIsNone(saved["providers"][0]["adoption_result"])

    def test_devin_bot_authors_env_override_adds_trusted_login(self) -> None:
        """DEVIN_BOT_AUTHORS extends (not replaces) Devin's default trusted authors."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"

            campaign = release_campaigns.initialize_campaign(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["devin"],
                repo_slug="owner/repo",
            )
            campaign.status = "running"
            campaign.providers[0]["state"] = "running"
            campaign.providers[0]["dispatch_ref"] = {"issue_number": "99"}
            release_campaigns.save_campaign(campaign, campaigns_dir)

            idempotency_key = campaign.providers[0]["idempotency_key"]
            adoption_res = _mock_adoption_result(release_tag="v1.0.0", provider="devin", outcome="pass")
            wrapper = {
                "schema": release_campaigns.RESULT_MARKER_SCHEMA,
                "campaign_id": campaign.campaign_id,
                "provider": "devin",
                "release_tag": "v1.0.0",
                "idempotency_key": idempotency_key,
                "adoption_result": adoption_res,
            }
            marker = f"<!-- CODE_MOWER_ADOPTION_RESULT: {json.dumps(wrapper)} -->"

            def mock_gh_json(args, **kwargs):
                return {"comments": [{"author": {"login": "self-hosted-devin-runner"}, "body": marker}]}, ""

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                resume=True,
                repo_slug="owner/repo",
                gh_json_runner=mock_gh_json,
                env={"DEVIN_BOT_AUTHORS": "self-hosted-devin-runner"},
            )

            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            self.assertEqual(saved["providers"][0]["state"], "complete")


def _dispatch_marker_from_body(body: str) -> dict[str, Any]:
    """Parse the machine-readable dispatch marker out of a posted comment body."""
    match = re.search(r"<!--\s*CODE_MOWER_RELEASE_CAMPAIGN:\s*(\{.*?\})\s*-->", body, re.DOTALL)
    assert match is not None, "dispatch comment is missing its campaign marker"
    parsed = json.loads(match.group(1))
    assert isinstance(parsed, dict)
    return parsed


def _capturing_dispatch_command_runner(bodies: list[str], *, returncode: int = 0):
    """A gh command runner that records the exact comment body it was asked to post."""

    def _run(args, **kwargs):
        argv = list(args)
        bodies.append(Path(argv[argv.index("--body-file") + 1]).read_text(encoding="utf-8"))

        class MockCompleted:
            pass

        completed = MockCompleted()
        completed.returncode = returncode
        completed.stdout = ""
        completed.stderr = ""
        return completed

    return _run


class RepeatedCampaignInvocationTests(unittest.TestCase):
    """A repeated invocation must never reinitialize and redispatch a live campaign."""

    def test_repeated_apply_without_resume_does_not_rerun_local_adapter(self) -> None:
        """Re-running the exact same --apply command must not re-invoke the adapter

        or discard the recorded provider state and evidence.
        """
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            fake_lane = _fake_local_cli_lane()
            invocations: list[list[str]] = []

            def fake_adapter_runner(argv, timeout):
                invocations.append(list(argv))
                output_path = Path(argv[argv.index("--output") + 1])
                adoption_res = _mock_adoption_result(
                    release_tag="v1.0.0", provider="codex", outcome="pass"
                )
                with output_path.open("w", encoding="utf-8") as fh:
                    json.dump(adoption_res, fh)
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            common_kwargs = dict(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["codex"],
                campaigns_dir=campaigns_dir,
                apply=True,
                which_fn=lambda _cmd: "/bin/fake-provider-cli",
                adapter_runner=fake_adapter_runner,
            )

            with mock.patch.object(
                release_campaigns, "resolve_provider_lane", return_value=("codex", fake_lane)
            ):
                release_campaigns.campaign_command(**common_kwargs)
                self.assertEqual(len(invocations), 1)
                first = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
                assert first is not None

                # Exactly the same command again, with no --resume.
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    ret = release_campaigns.campaign_command(**common_kwargs)

            self.assertEqual(ret, 0)
            self.assertEqual(len(invocations), 1)
            self.assertNotIn("Traceback", stderr.getvalue())

            self.assertEqual(len(release_campaigns.list_campaigns(campaigns_dir)), 1)
            second = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert second is not None
            self.assertEqual(second["created_at"], first["created_at"])
            self.assertEqual(second["providers"][0]["state"], "complete")
            self.assertEqual(
                second["providers"][0]["adoption_result"],
                first["providers"][0]["adoption_result"],
            )
            self.assertEqual(
                second["providers"][0]["attempted_at"], first["providers"][0]["attempted_at"]
            )
            self.assertEqual(
                second["providers"][0]["idempotency_key"],
                first["providers"][0]["idempotency_key"],
            )

    def test_repeated_apply_without_resume_does_not_repost_hosted_dispatch(self) -> None:
        """Re-running the exact same hosted --apply command must not repost the dispatch."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            bodies: list[str] = []

            def mock_gh_json(args, **kwargs):
                return {"comments": []}, ""

            common_kwargs = dict(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["cursor_bugbot"],
                campaigns_dir=campaigns_dir,
                repo_slug="owner/repo",
                issue="42",
                apply=True,
                command_runner=_capturing_dispatch_command_runner(bodies),
                gh_json_runner=mock_gh_json,
                env={"CURSOR_BUGBOT_AUDIT_LABEL_TOKEN": "token"},
            )

            release_campaigns.campaign_command(**common_kwargs)
            self.assertEqual(len(bodies), 1)
            first = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert first is not None
            self.assertEqual(first["providers"][0]["state"], "running")

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                ret = release_campaigns.campaign_command(**common_kwargs)

            self.assertEqual(ret, 0)
            self.assertEqual(len(bodies), 1)
            self.assertNotIn("Traceback", stderr.getvalue())

            self.assertEqual(len(release_campaigns.list_campaigns(campaigns_dir)), 1)
            second = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert second is not None
            self.assertEqual(second["providers"][0]["state"], "running")
            self.assertEqual(
                second["providers"][0]["attempted_at"], first["providers"][0]["attempted_at"]
            )
            self.assertEqual(
                second["providers"][0]["dispatch_ref"], first["providers"][0]["dispatch_ref"]
            )
            self.assertEqual(second["created_at"], first["created_at"])

    def test_create_action_on_existing_campaign_is_rejected(self) -> None:
        """`create` cannot be honored for an existing campaign, so it fails explicitly."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            fake_lane = _fake_local_cli_lane()
            invocations: list[list[str]] = []

            def fake_adapter_runner(argv, timeout):
                invocations.append(list(argv))
                output_path = Path(argv[argv.index("--output") + 1])
                adoption_res = _mock_adoption_result(
                    release_tag="v1.0.0", provider="codex", outcome="pass"
                )
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
                before = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)

                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    ret = release_campaigns.campaign_command(
                        action="create",
                        release_tag="v1.0.0",
                        package_spec="code-mower==1.0.0",
                        providers=["codex"],
                        campaigns_dir=campaigns_dir,
                        apply=True,
                        which_fn=lambda _cmd: "/bin/fake-provider-cli",
                        adapter_runner=fake_adapter_runner,
                    )

            self.assertEqual(ret, 1)
            self.assertEqual(len(invocations), 1)
            self.assertIn("already exists", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            after = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            self.assertEqual(after, before)

    def test_dispatch_action_advances_existing_campaign_idempotently(self) -> None:
        """`dispatch` is implemented as advance semantics, not re-creation."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            bodies: list[str] = []

            def mock_gh_json(args, **kwargs):
                return {"comments": []}, ""

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["cursor_bugbot"],
                campaigns_dir=campaigns_dir,
                repo_slug="owner/repo",
                issue="42",
                apply=False,
                command_runner=_capturing_dispatch_command_runner(bodies),
                env={"CURSOR_BUGBOT_AUDIT_LABEL_TOKEN": "token"},
            )
            self.assertEqual(bodies, [])

            dispatch_kwargs = dict(
                action="dispatch",
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                repo_slug="owner/repo",
                issue="42",
                apply=True,
                command_runner=_capturing_dispatch_command_runner(bodies),
                gh_json_runner=mock_gh_json,
                env={"CURSOR_BUGBOT_AUDIT_LABEL_TOKEN": "token"},
            )
            release_campaigns.campaign_command(**dispatch_kwargs)
            self.assertEqual(len(bodies), 1)

            release_campaigns.campaign_command(**dispatch_kwargs)
            self.assertEqual(len(bodies), 1)

            self.assertEqual(len(release_campaigns.list_campaigns(campaigns_dir)), 1)
            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            self.assertEqual(saved["providers"][0]["state"], "running")

    def test_dispatch_action_without_existing_campaign_is_rejected(self) -> None:
        """`dispatch` never silently falls through to creating and dispatching a campaign."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            command_runner = mock.MagicMock()
            adapter_runner = mock.MagicMock()

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                ret = release_campaigns.campaign_command(
                    action="dispatch",
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    campaigns_dir=campaigns_dir,
                    repo_slug="owner/repo",
                    issue="42",
                    apply=True,
                    command_runner=command_runner,
                    adapter_runner=adapter_runner,
                )

            self.assertEqual(ret, 1)
            command_runner.assert_not_called()
            adapter_runner.assert_not_called()
            self.assertIn("no existing campaign", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertEqual(release_campaigns.list_campaigns(campaigns_dir), [])

    def test_resume_without_existing_campaign_is_rejected(self) -> None:
        """`--resume` for a campaign that does not exist fails instead of creating one."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            command_runner = mock.MagicMock()

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                ret = release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    campaigns_dir=campaigns_dir,
                    resume=True,
                    apply=True,
                    command_runner=command_runner,
                )

            self.assertEqual(ret, 1)
            command_runner.assert_not_called()
            self.assertIn("no existing campaign", stderr.getvalue())
            self.assertEqual(release_campaigns.list_campaigns(campaigns_dir), [])

    def test_conflicting_context_for_existing_campaign_is_rejected(self) -> None:
        """Creation arguments describing a different campaign are rejected, not ignored."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            command_runner = mock.MagicMock()

            release_campaigns.campaign_command(
                release_tag="v2.0.0",
                package_spec="code-mower==2.0.0",
                providers=["cursor_bugbot"],
                campaigns_dir=campaigns_dir,
                apply=False,
            )
            before = release_campaigns.load_campaign_by_id("campaign-v2.0.0", campaigns_dir)

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                ret = release_campaigns.campaign_command(
                    release_tag="v2.0.0",
                    package_spec="code-mower==2.0.0",
                    qualification_context="upgrade",
                    starting_version="1.0.0",
                    providers=["cursor_bugbot"],
                    campaigns_dir=campaigns_dir,
                    repo_slug="owner/repo",
                    issue="42",
                    apply=True,
                    command_runner=command_runner,
                    env={"CURSOR_BUGBOT_AUDIT_LABEL_TOKEN": "token"},
                )

            self.assertEqual(ret, 1)
            command_runner.assert_not_called()
            self.assertIn("--qualification-context", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            after = release_campaigns.load_campaign_by_id("campaign-v2.0.0", campaigns_dir)
            self.assertEqual(after, before)

    def test_conflicting_providers_for_existing_campaign_is_rejected(self) -> None:
        """An existing campaign's provider set is fixed; a different set is rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["cursor_bugbot"],
                campaigns_dir=campaigns_dir,
                apply=False,
            )
            before = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                ret = release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    providers=["devin"],
                    campaigns_dir=campaigns_dir,
                    apply=False,
                )

            self.assertEqual(ret, 1)
            self.assertIn("--providers", stderr.getvalue())
            after = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            self.assertEqual(after, before)


class RemoteDispatchStartingVersionTests(unittest.TestCase):
    """Remote dispatches must advertise the exact starting_version they will accept."""

    def _dispatch_upgrade_campaign(
        self, campaigns_dir: Path, bodies: list[str]
    ) -> dict[str, Any]:
        release_campaigns.campaign_command(
            release_tag="v2.0.0",
            package_spec="code-mower==2.0.0",
            qualification_context="upgrade",
            starting_version="1.0.3",
            providers=["cursor_bugbot"],
            campaigns_dir=campaigns_dir,
            repo_slug="owner/repo",
            issue="42",
            apply=True,
            command_runner=_capturing_dispatch_command_runner(bodies),
            env={"CURSOR_BUGBOT_AUDIT_LABEL_TOKEN": "token"},
        )
        saved = release_campaigns.load_campaign_by_id("campaign-v2.0.0", campaigns_dir)
        assert saved is not None
        return saved

    def test_upgrade_dispatch_marker_and_instructions_carry_starting_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            bodies: list[str] = []
            saved = self._dispatch_upgrade_campaign(campaigns_dir, bodies)

            self.assertEqual(len(bodies), 1)
            body = bodies[0]
            marker = _dispatch_marker_from_body(body)
            self.assertEqual(marker["schema"], release_campaigns.DISPATCH_SCHEMA)
            self.assertEqual(marker["qualification_context"], "upgrade")
            self.assertEqual(marker["starting_version"], "1.0.3")
            self.assertEqual(marker["release_tag"], "v2.0.0")
            self.assertEqual(
                marker["idempotency_key"], saved["providers"][0]["idempotency_key"]
            )

            # Human-facing instructions must state the same starting version.
            self.assertIn("- **Starting Version:** `1.0.3`", body)
            self.assertIn("`starting_version` `1.0.3`", body)
            self.assertEqual(saved["providers"][0]["state"], "running")

    def test_cold_install_dispatch_omits_starting_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            bodies: list[str] = []

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["cursor_bugbot"],
                campaigns_dir=campaigns_dir,
                repo_slug="owner/repo",
                issue="42",
                apply=True,
                command_runner=_capturing_dispatch_command_runner(bodies),
                env={"CURSOR_BUGBOT_AUDIT_LABEL_TOKEN": "token"},
            )

            self.assertEqual(len(bodies), 1)
            marker = _dispatch_marker_from_body(bodies[0])
            self.assertEqual(marker["qualification_context"], "cold_install")
            self.assertNotIn("starting_version", marker)
            self.assertNotIn("Starting Version", bodies[0])

    def test_upgrade_dispatch_then_poll_completes_bound_remote_result(self) -> None:
        """The result a dispatched upgrade advertises is exactly the one polling accepts."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            bodies: list[str] = []
            saved = self._dispatch_upgrade_campaign(campaigns_dir, bodies)
            marker = _dispatch_marker_from_body(bodies[0])

            adoption_res = _mock_adoption_result_full(
                release_tag=marker["release_tag"],
                normalized_version="2.0.0",
                provider=marker["provider"],
                qualification_context=marker["qualification_context"],
                starting_version=marker["starting_version"],
                ending_version="2.0.0",
                outcome="pass",
            )
            wrapper = {
                "schema": release_campaigns.RESULT_MARKER_SCHEMA,
                "campaign_id": marker["campaign_id"],
                "provider": marker["provider"],
                "release_tag": marker["release_tag"],
                "idempotency_key": marker["idempotency_key"],
                "adoption_result": adoption_res,
            }
            reply = f"<!-- CODE_MOWER_ADOPTION_RESULT: {json.dumps(wrapper)} -->"

            def mock_gh_json(args, **kwargs):
                return {"comments": [{"author": {"login": "cursor[bot]"}, "body": reply}]}, ""

            dispatch_calls: list[Any] = []
            ret = release_campaigns.campaign_command(
                release_tag="v2.0.0",
                campaigns_dir=campaigns_dir,
                resume=True,
                repo_slug="owner/repo",
                issue="42",
                apply=True,
                command_runner=_capturing_dispatch_command_runner(dispatch_calls),
                gh_json_runner=mock_gh_json,
                env={"CURSOR_BUGBOT_AUDIT_LABEL_TOKEN": "token"},
            )

            self.assertEqual(ret, 0)
            self.assertEqual(dispatch_calls, [])
            polled = release_campaigns.load_campaign_by_id("campaign-v2.0.0", campaigns_dir)
            assert polled is not None
            self.assertEqual(polled["providers"][0]["state"], "complete")
            self.assertEqual(polled["status"], "complete")
            self.assertEqual(
                polled["providers"][0]["adoption_result"]["starting_version"], "1.0.3"
            )
            self.assertEqual(
                polled["providers"][0]["idempotency_key"],
                saved["providers"][0]["idempotency_key"],
            )

    def test_upgrade_dispatch_without_starting_version_fails_closed(self) -> None:
        """An upgrade campaign missing its starting_version is never dispatched."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            command_runner = mock.MagicMock()

            campaign = release_campaigns.initialize_campaign(
                release_tag="v2.0.0",
                package_spec="code-mower==2.0.0",
                qualification_context="upgrade",
                starting_version="1.0.3",
                providers=["cursor_bugbot"],
                repo_slug="owner/repo",
            )
            tampered = campaign.to_dict()
            tampered["starting_version"] = ""
            release_campaigns.save_campaign(tampered, campaigns_dir)

            release_campaigns.campaign_command(
                release_tag="v2.0.0",
                campaigns_dir=campaigns_dir,
                resume=True,
                repo_slug="owner/repo",
                issue="42",
                apply=True,
                command_runner=command_runner,
                env={"CURSOR_BUGBOT_AUDIT_LABEL_TOKEN": "token"},
            )

            command_runner.assert_not_called()
            saved = release_campaigns.load_campaign_by_id("campaign-v2.0.0", campaigns_dir)
            assert saved is not None
            self.assertEqual(saved["providers"][0]["state"], "unavailable")
            self.assertEqual(
                saved["providers"][0]["error"], "campaign_identity_incomplete"
            )
            self.assertIsNone(saved["providers"][0]["attempted_at"])
            self.assertIn("--starting-version", saved["providers"][0]["next_action"])

    def test_dispatch_helper_refuses_upgrade_without_starting_version(self) -> None:
        """The dispatch helper itself fails closed for direct callers too."""
        command_runner = mock.MagicMock()
        ok, ref, err = release_campaigns._dispatch_github_comment(
            "owner/repo",
            "42",
            "campaign-v2.0.0",
            "v2.0.0",
            "code-mower==2.0.0",
            "cursor_bugbot",
            "upgrade",
            "key",
            starting_version="",
            command_runner=command_runner,
        )
        self.assertFalse(ok)
        self.assertEqual(ref, {})
        self.assertEqual(err, "campaign_identity_incomplete")
        command_runner.assert_not_called()


class CampaignStatusIdentifierTests(unittest.TestCase):
    """Only an unqualified status request may fall back to the newest campaign."""

    @staticmethod
    def _seed_two_campaigns(campaigns_dir: Path) -> None:
        older = release_campaigns.initialize_campaign(
            release_tag="v1.0.0",
            package_spec="code-mower==1.0.0",
            providers=["cursor_bugbot"],
        ).to_dict()
        older["updated_at"] = "2026-09-03T12:00:00Z"
        newer = release_campaigns.initialize_campaign(
            release_tag="v1.1.0",
            package_spec="code-mower==1.1.0",
            providers=["cursor_bugbot"],
        ).to_dict()
        newer["updated_at"] = "2026-09-04T12:00:00Z"
        release_campaigns.save_campaign(older, campaigns_dir)
        release_campaigns.save_campaign(newer, campaigns_dir)

    def test_status_with_unknown_campaign_id_is_bounded_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._seed_two_campaigns(campaigns_dir)

            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                ret = release_campaigns.campaign_command(
                    status=True,
                    campaign_id="campaign-v9.9.9",
                    campaigns_dir=campaigns_dir,
                    emit_json=True,
                )

            self.assertEqual(ret, 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("no campaign found", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertNotIn("v1.1.0", stdout.getvalue())

    def test_status_with_unknown_release_tag_is_bounded_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._seed_two_campaigns(campaigns_dir)

            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                ret = release_campaigns.campaign_command(
                    action="status",
                    release_tag="v9.9.9",
                    campaigns_dir=campaigns_dir,
                )

            self.assertEqual(ret, 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("no campaign found", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())

    def test_status_rejects_campaign_id_and_release_tag_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._seed_two_campaigns(campaigns_dir)

            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                ret = release_campaigns.campaign_command(
                    status=True,
                    campaign_id="campaign-v1.0.0",
                    release_tag="v1.1.0",
                    campaigns_dir=campaigns_dir,
                )

            self.assertEqual(ret, 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("does not match", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())

    def test_unqualified_status_reports_latest_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._seed_two_campaigns(campaigns_dir)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                ret = release_campaigns.campaign_command(
                    status=True,
                    campaigns_dir=campaigns_dir,
                    emit_json=True,
                )

            self.assertEqual(ret, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["campaign_id"], "campaign-v1.1.0")
            self.assertEqual(payload["release_tag"], "v1.1.0")

    def test_status_with_known_identifier_reports_that_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._seed_two_campaigns(campaigns_dir)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                ret = release_campaigns.campaign_command(
                    status=True,
                    release_tag="v1.0.0",
                    campaigns_dir=campaigns_dir,
                    emit_json=True,
                )

            self.assertEqual(ret, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["release_tag"], "v1.0.0")



def _capturing_dispatch_argv_runner(calls: list[list[str]], *, returncode: int = 0):
    """A gh command runner that records the full argv of every dispatch call."""

    def _run(args, **kwargs):
        argv = list(args)
        calls.append(argv)

        class MockCompleted:
            pass

        completed = MockCompleted()
        completed.returncode = returncode
        completed.stdout = ""
        completed.stderr = ""
        return completed

    return _run


class DuplicateCampaignProviderTests(unittest.TestCase):
    """One provider may appear at most once in a campaign, under any spelling.

    Two participants for one canonical provider would share a single
    idempotency key and a single result file path, so one provider's evidence
    would satisfy both entries and be counted twice toward the campaign.
    """

    def test_exact_duplicate_provider_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            release_campaigns.initialize_campaign(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["claude", "claude"],
            )
        message = str(ctx.exception)
        self.assertIn("duplicate release campaign provider", message)
        self.assertIn("claude", message)

    def test_alias_collision_is_rejected(self) -> None:
        """Two different names for one canonical provider are still one provider."""
        for names, canonical in (
            (["claude", "claude_code"], "claude"),
            (["cursor", "grok_bot"], "cursor_bugbot"),
            (["codex", "cursor_bugbot", "cursor"], "cursor_bugbot"),
        ):
            with self.subTest(names=names):
                with self.assertRaises(ValueError) as ctx:
                    release_campaigns.initialize_campaign(
                        release_tag="v1.0.0",
                        package_spec="code-mower==1.0.0",
                        providers=names,
                    )
                message = str(ctx.exception)
                self.assertIn("duplicate release campaign provider", message)
                self.assertIn(canonical, message)

    def test_distinct_providers_are_accepted_and_canonicalized(self) -> None:
        """The normal case: distinct providers keep distinct keys and result paths."""
        campaign = release_campaigns.initialize_campaign(
            release_tag="v1.0.0",
            package_spec="code-mower==1.0.0",
            providers=["claude", "codex", "cursor"],
        )
        names = [p["provider"] for p in campaign.providers]
        self.assertEqual(names, ["claude", "codex", "cursor_bugbot"])
        keys = {p["idempotency_key"] for p in campaign.providers}
        self.assertEqual(len(keys), 3)
        result_files = {
            f"{campaign.campaign_id}_{p['provider']}.json" for p in campaign.providers
        }
        self.assertEqual(len(result_files), 3)

    def test_default_provider_set_has_no_duplicates(self) -> None:
        campaign = release_campaigns.initialize_campaign(
            release_tag="v1.0.0",
            package_spec="code-mower==1.0.0",
        )
        names = [p["provider"] for p in campaign.providers]
        self.assertEqual(len(names), len(set(names)))

    def test_cli_rejects_duplicate_providers_without_creating_a_campaign(self) -> None:
        """CLI-facing: the duplicate is an explicit error, not a silently deduped campaign."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            adapter_mock = mock.MagicMock()
            cmd_runner_mock = mock.MagicMock()

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                ret = release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    providers=["claude", "claude_code"],
                    campaigns_dir=campaigns_dir,
                    apply=True,
                    adapter_runner=adapter_mock,
                    command_runner=cmd_runner_mock,
                )

            self.assertEqual(ret, 1)
            self.assertIn("duplicate release campaign provider", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            adapter_mock.assert_not_called()
            cmd_runner_mock.assert_not_called()
            self.assertEqual(release_campaigns.list_campaigns(campaigns_dir), [])


class CampaignRepoSlugSupplyTests(unittest.TestCase):
    """A campaign created without a repo slug can be completed with one later.

    Filling an empty slug is the only mutation allowed: it makes an otherwise
    undispatchable campaign dispatchable without changing anything already
    recorded. A slug that conflicts with a non-empty stored one is refused.
    """

    _ENV = {"CURSOR_BUGBOT_AUDIT_LABEL_TOKEN": "token"}

    def _create_without_slug(self, campaigns_dir: Path) -> dict[str, Any]:
        ret = release_campaigns.campaign_command(
            action="create",
            release_tag="v1.0.0",
            package_spec="code-mower==1.0.0",
            providers=["cursor_bugbot"],
            campaigns_dir=campaigns_dir,
            apply=False,
            command_runner=mock.MagicMock(),
            env=self._ENV,
        )
        self.assertEqual(ret, 0)
        created = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
        assert created is not None
        self.assertEqual(created["repo_slug"], "")
        return created

    def test_supplied_slug_fills_empty_value_and_dispatches_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._create_without_slug(campaigns_dir)

            calls: list[list[str]] = []

            def mock_gh_json(args, **kwargs):
                return {"comments": []}, ""

            dispatch_kwargs = dict(
                action="dispatch",
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                repo_slug="owner/repo",
                issue="42",
                apply=True,
                command_runner=_capturing_dispatch_argv_runner(calls),
                gh_json_runner=mock_gh_json,
                env=self._ENV,
            )

            self.assertEqual(release_campaigns.campaign_command(**dispatch_kwargs), 0)

            self.assertEqual(len(calls), 1)
            self.assertIn("--repo", calls[0])
            self.assertEqual(calls[0][calls[0].index("--repo") + 1], "owner/repo")

            first = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert first is not None
            self.assertEqual(first["repo_slug"], "owner/repo")
            self.assertEqual(first["providers"][0]["state"], "running")

            # The filled slug is durable, and repeating the command is idempotent.
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                ret = release_campaigns.campaign_command(**dispatch_kwargs)

            self.assertEqual(ret, 0)
            self.assertEqual(len(calls), 1)
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertEqual(len(release_campaigns.list_campaigns(campaigns_dir)), 1)

            second = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert second is not None
            self.assertEqual(second["repo_slug"], "owner/repo")
            self.assertEqual(second["providers"][0]["state"], "running")
            self.assertEqual(
                second["providers"][0]["attempted_at"], first["providers"][0]["attempted_at"]
            )
            self.assertEqual(
                second["providers"][0]["dispatch_ref"], first["providers"][0]["dispatch_ref"]
            )
            self.assertEqual(
                second["providers"][0]["idempotency_key"],
                first["providers"][0]["idempotency_key"],
            )
            self.assertEqual(second["created_at"], first["created_at"])

    def test_supplied_slug_persists_from_a_dry_run_resume(self) -> None:
        """The fill is persisted even when nothing is dispatched."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._create_without_slug(campaigns_dir)
            command_runner = mock.MagicMock()

            ret = release_campaigns.campaign_command(
                action="resume",
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                repo_slug="owner/repo",
                issue="42",
                apply=False,
                command_runner=command_runner,
                env=self._ENV,
            )

            self.assertEqual(ret, 0)
            command_runner.assert_not_called()
            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            self.assertEqual(saved["repo_slug"], "owner/repo")

    def test_conflicting_slug_is_rejected_without_mutation_or_dispatch(self) -> None:
        """A non-empty stored slug is never repointed at another repository."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            calls: list[list[str]] = []

            self.assertEqual(
                release_campaigns.campaign_command(
                    action="create",
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    providers=["cursor_bugbot"],
                    campaigns_dir=campaigns_dir,
                    repo_slug="owner/repo",
                    apply=False,
                    command_runner=_capturing_dispatch_argv_runner(calls),
                    env=self._ENV,
                ),
                0,
            )
            before = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert before is not None
            self.assertEqual(before["repo_slug"], "owner/repo")

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                ret = release_campaigns.campaign_command(
                    action="dispatch",
                    release_tag="v1.0.0",
                    campaigns_dir=campaigns_dir,
                    repo_slug="attacker/repo",
                    issue="42",
                    apply=True,
                    command_runner=_capturing_dispatch_argv_runner(calls),
                    env=self._ENV,
                )

            self.assertEqual(ret, 1)
            self.assertEqual(calls, [])
            self.assertIn("does not match existing campaign repo slug", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            after = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            self.assertEqual(after, before)

    def test_repeating_the_same_slug_is_not_a_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self.assertEqual(
                release_campaigns._existing_campaign_conflict(
                    {"repo_slug": "owner/repo"},
                    package_spec="",
                    qualification_context="cold_install",
                    starting_version="",
                    providers=(),
                    repo_slug="owner/repo",
                ),
                "",
            )
            self.assertEqual(
                release_campaigns._existing_campaign_conflict(
                    {"repo_slug": ""},
                    package_spec="",
                    qualification_context="cold_install",
                    starting_version="",
                    providers=(),
                    repo_slug="owner/repo",
                ),
                "",
            )
            self.assertEqual(campaigns_dir.exists(), False)


class CampaignQualificationContextSupplyTests(unittest.TestCase):
    """`--qualification-context` is compared whenever it is supplied.

    `cold_install` is both the creation default and a context a caller can
    explicitly request, so the two are kept distinguishable: an omitted flag
    asserts nothing about a stored campaign, while an explicit `cold_install`
    against a stored upgrade campaign is an identity conflict and is rejected
    before any mutation, polling, or dispatch.
    """

    _ENV = {"CURSOR_BUGBOT_AUDIT_LABEL_TOKEN": "token"}

    def _create_upgrade(self, campaigns_dir: Path) -> dict[str, Any]:
        ret = release_campaigns.campaign_command(
            action="create",
            release_tag="v1.1.0",
            package_spec="code-mower==1.1.0",
            providers=["cursor_bugbot"],
            qualification_context="upgrade",
            starting_version="1.0.0",
            campaigns_dir=campaigns_dir,
            repo_slug="owner/repo",
            apply=False,
            command_runner=mock.MagicMock(),
            env=self._ENV,
        )
        self.assertEqual(ret, 0)
        created = release_campaigns.load_campaign_by_id("campaign-v1.1.0", campaigns_dir)
        assert created is not None
        self.assertEqual(created["qualification_context"], "upgrade")
        self.assertEqual(created["starting_version"], "1.0.0")
        return created

    def test_omitted_context_advances_existing_upgrade_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._create_upgrade(campaigns_dir)
            calls: list[list[str]] = []

            def mock_gh_json(args, **kwargs):
                return {"comments": []}, ""

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                ret = release_campaigns.campaign_command(
                    action="dispatch",
                    release_tag="v1.1.0",
                    campaigns_dir=campaigns_dir,
                    issue="42",
                    apply=True,
                    command_runner=_capturing_dispatch_argv_runner(calls),
                    gh_json_runner=mock_gh_json,
                    env=self._ENV,
                )

            self.assertEqual(ret, 0)
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertEqual(len(calls), 1)
            advanced = release_campaigns.load_campaign_by_id("campaign-v1.1.0", campaigns_dir)
            assert advanced is not None
            self.assertEqual(advanced["qualification_context"], "upgrade")
            self.assertEqual(advanced["starting_version"], "1.0.0")
            self.assertEqual(advanced["providers"][0]["state"], "running")

    def test_explicit_matching_upgrade_context_advances(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._create_upgrade(campaigns_dir)
            calls: list[list[str]] = []

            def mock_gh_json(args, **kwargs):
                return {"comments": []}, ""

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                ret = release_campaigns.campaign_command(
                    action="dispatch",
                    release_tag="v1.1.0",
                    qualification_context="upgrade",
                    starting_version="1.0.0",
                    campaigns_dir=campaigns_dir,
                    issue="42",
                    apply=True,
                    command_runner=_capturing_dispatch_argv_runner(calls),
                    gh_json_runner=mock_gh_json,
                    env=self._ENV,
                )

            self.assertEqual(ret, 0)
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertEqual(len(calls), 1)
            advanced = release_campaigns.load_campaign_by_id("campaign-v1.1.0", campaigns_dir)
            assert advanced is not None
            self.assertEqual(advanced["providers"][0]["state"], "running")

    def test_explicit_cold_install_against_upgrade_campaign_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            before = self._create_upgrade(campaigns_dir)
            calls: list[list[str]] = []

            def unexpected_gh_json(args, **kwargs):
                raise AssertionError("polled GitHub despite a conflicting context")

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                ret = release_campaigns.campaign_command(
                    action="dispatch",
                    release_tag="v1.1.0",
                    qualification_context="cold_install",
                    campaigns_dir=campaigns_dir,
                    issue="42",
                    apply=True,
                    command_runner=_capturing_dispatch_argv_runner(calls),
                    gh_json_runner=unexpected_gh_json,
                    env=self._ENV,
                )

            self.assertEqual(ret, 1)
            self.assertEqual(calls, [])
            self.assertIn("--qualification-context 'cold_install'", stderr.getvalue())
            self.assertIn("'upgrade'", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            after = release_campaigns.load_campaign_by_id("campaign-v1.1.0", campaigns_dir)
            self.assertEqual(after, before)

    def test_omitted_context_creates_cold_install_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            ret = release_campaigns.campaign_command(
                action="create",
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["cursor_bugbot"],
                campaigns_dir=campaigns_dir,
                apply=False,
                command_runner=mock.MagicMock(),
                env=self._ENV,
            )

            self.assertEqual(ret, 0)
            created = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert created is not None
            self.assertEqual(created["qualification_context"], "cold_install")
            self.assertEqual(created["starting_version"], "")

    def test_explicit_cold_install_creates_cold_install_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            ret = release_campaigns.campaign_command(
                action="create",
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["cursor_bugbot"],
                qualification_context="cold_install",
                campaigns_dir=campaigns_dir,
                apply=False,
                command_runner=mock.MagicMock(),
                env=self._ENV,
            )

            self.assertEqual(ret, 0)
            created = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert created is not None
            self.assertEqual(created["qualification_context"], "cold_install")

    def test_cli_preserves_the_unspecified_context_through_to_dispatch(self) -> None:
        """The CLI parser has no default of its own, so omission stays visible."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._create_upgrade(campaigns_dir)

            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                omitted = release_qualify.main(
                    [
                        "campaign",
                        "resume",
                        "--release-tag",
                        "v1.1.0",
                        "--campaigns-dir",
                        str(campaigns_dir),
                    ]
                )

            self.assertEqual(omitted, 0)
            self.assertNotIn("Traceback", stderr.getvalue())
            before = release_campaigns.load_campaign_by_id("campaign-v1.1.0", campaigns_dir)
            assert before is not None
            self.assertEqual(before["qualification_context"], "upgrade")

            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                conflicting = release_qualify.main(
                    [
                        "campaign",
                        "resume",
                        "--release-tag",
                        "v1.1.0",
                        "--campaigns-dir",
                        str(campaigns_dir),
                        "--qualification-context",
                        "cold_install",
                    ]
                )

            self.assertEqual(conflicting, 1)
            self.assertIn("--qualification-context 'cold_install'", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            after = release_campaigns.load_campaign_by_id("campaign-v1.1.0", campaigns_dir)
            self.assertEqual(after, before)


class CampaignIdContractTests(unittest.TestCase):
    """Campaign ids are storage keys: the id-to-filename mapping is one-to-one.

    Ids are never sanitized to fit a filename. Two ids that differ can never
    address one stored campaign, and an id can never address anything outside
    the campaign directory or any of its internal (dot-prefixed) files.
    """

    def _campaign_command(self, campaigns_dir: Path, **kwargs: Any) -> tuple[int, str]:
        stderr = io.StringIO()
        stdout = io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
            code = release_campaigns.campaign_command(
                campaigns_dir=campaigns_dir,
                **kwargs,
            )
        return code, stderr.getvalue()

    def test_valid_generated_default_id_is_accepted_and_names_its_own_file(self) -> None:
        """campaign-<release-tag> satisfies the contract and is used verbatim."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            code, stderr = self._campaign_command(campaigns_dir, release_tag="v1.0.0")
            self.assertEqual(code, 0, stderr)
            self.assertTrue((campaigns_dir / "campaign-v1.0.0.json").is_file())
            stored = json.loads((campaigns_dir / "campaign-v1.0.0.json").read_text())
            self.assertEqual(stored["campaign_id"], "campaign-v1.0.0")

    def test_valid_explicit_id_round_trips_through_storage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            code, stderr = self._campaign_command(
                campaigns_dir, release_tag="v1.0.0", campaign_id="rc.1_check-2"
            )
            self.assertEqual(code, 0, stderr)
            self.assertTrue((campaigns_dir / "rc.1_check-2.json").is_file())
            loaded = release_campaigns.load_campaign_by_id("rc.1_check-2", campaigns_dir)
            assert loaded is not None
            self.assertEqual(loaded["campaign_id"], "rc.1_check-2")

    def test_prerelease_tag_default_id_is_accepted(self) -> None:
        """The longest generated default id shape still satisfies the contract."""
        release_campaigns.validate_campaign_id("campaign-v999.999.999-alpha.999")

    def test_slash_id_no_longer_collides_with_underscore_id(self) -> None:
        """`campaign/a` must not resolve to, advance, or overwrite `campaign_a`."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            code, stderr = self._campaign_command(
                campaigns_dir, release_tag="v1.0.0", campaign_id="campaign_a"
            )
            self.assertEqual(code, 0, stderr)
            before = json.loads((campaigns_dir / "campaign_a.json").read_text())

            code, stderr = self._campaign_command(
                campaigns_dir,
                release_tag="v1.0.0",
                campaign_id="campaign/a",
                apply=True,
            )
            self.assertEqual(code, 1)
            self.assertIn("campaign_id", stderr)
            self.assertNotIn("Traceback", stderr)

            after = json.loads((campaigns_dir / "campaign_a.json").read_text())
            self.assertEqual(after, before)
            self.assertEqual(
                sorted(p.name for p in campaigns_dir.glob("*.json")), ["campaign_a.json"]
            )

    def test_uppercase_id_is_rejected_for_case_insensitive_filesystem_safety(self) -> None:
        """`Campaign-A` and `campaign-a` are one file on macOS/Windows; reject both spellings."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            code, stderr = self._campaign_command(
                campaigns_dir, release_tag="v1.0.0", campaign_id="campaign-a"
            )
            self.assertEqual(code, 0, stderr)
            before = json.loads((campaigns_dir / "campaign-a.json").read_text())

            for spelling in ("Campaign-A", "CAMPAIGN-A", "campaign-A"):
                code, stderr = self._campaign_command(
                    campaigns_dir,
                    release_tag="v1.0.0",
                    campaign_id=spelling,
                    apply=True,
                )
                self.assertEqual(code, 1, spelling)
                self.assertIn("lowercase", stderr)
                self.assertNotIn("Traceback", stderr)

            after = json.loads((campaigns_dir / "campaign-a.json").read_text())
            self.assertEqual(after, before)
            self.assertEqual(
                sorted(p.name for p in campaigns_dir.glob("*.json")), ["campaign-a.json"]
            )

    def test_traversal_ids_are_rejected_and_write_nothing_anywhere(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            campaigns_dir = root / "nested" / "campaigns"
            traversals = (
                "..",
                ".",
                "../evil",
                "../../etc/passwd",
                "campaign/../../evil",
                "/absolute/evil",
                "nested/child",
                "back\\slash",
                "null\x00byte",
            )
            for candidate in traversals:
                code, stderr = self._campaign_command(
                    campaigns_dir,
                    release_tag="v1.0.0",
                    campaign_id=candidate,
                    apply=True,
                )
                self.assertEqual(code, 1, candidate)
                self.assertNotIn("Traceback", stderr)

            # No mutation at all: rejection happens before the campaign
            # directory (or its lock file) is ever created.
            self.assertFalse(campaigns_dir.exists())
            self.assertEqual(sorted(p.name for p in root.iterdir()), [])

    def test_internal_storage_names_are_not_addressable_as_campaign_ids(self) -> None:
        """Dot-prefixed names -- the lock file and write-staging prefix -- are rejected."""
        for candidate in (
            release_campaigns.CAMPAIGNS_LOCK_FILENAME,
            ".tmp.campaign-v1.0.0",
            ".hidden",
        ):
            with self.assertRaises(ValueError):
                release_campaigns.validate_campaign_id(candidate)

    def test_overlong_id_is_rejected_with_a_bounded_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            overlong = "c" * (release_campaigns.MAX_CAMPAIGN_ID_LENGTH + 1)
            code, stderr = self._campaign_command(
                campaigns_dir, release_tag="v1.0.0", campaign_id=overlong, apply=True
            )
            self.assertEqual(code, 1)
            self.assertIn(str(release_campaigns.MAX_CAMPAIGN_ID_LENGTH), stderr)
            self.assertNotIn("Traceback", stderr)
            self.assertNotIn(overlong, stderr)
            self.assertFalse(campaigns_dir.exists())

            at_limit = "c" * release_campaigns.MAX_CAMPAIGN_ID_LENGTH
            self.assertEqual(release_campaigns.validate_campaign_id(at_limit), at_limit)

    def test_cli_rejects_an_invalid_campaign_id_without_a_traceback(self) -> None:
        """End-to-end through argparse: bounded error, non-zero exit, no state."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            stderr = io.StringIO()
            stdout = io.StringIO()
            with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
                code = release_qualify.main(
                    [
                        "campaign",
                        "--release-tag",
                        "v1.0.0",
                        "--campaign-id",
                        "campaign/a",
                        "--campaigns-dir",
                        str(campaigns_dir),
                        "--apply",
                    ]
                )
            self.assertEqual(code, 1)
            self.assertIn("error: campaign_id", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertFalse(campaigns_dir.exists())

    def test_cli_help_documents_the_campaign_id_contract(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit):
                release_qualify.main(["campaign", "--help"])
        # argparse rewraps help text, so compare on a whitespace-collapsed copy.
        help_text = " ".join(stdout.getvalue().split())
        self.assertIn("lowercase ASCII letters", help_text)
        self.assertIn(
            f"at most {release_campaigns.MAX_CAMPAIGN_ID_LENGTH} characters", help_text
        )
        self.assertIn("used verbatim as the stored file's name", help_text)
        self.assertIn("exclusive advisory lock on the campaign directory", help_text)
        # The serialization contract is scoped to mutations, and says so: help
        # that promised a lock on every invocation would contradict a `--status`
        # that deliberately takes none.
        self.assertIn("Mutating invocations", help_text)
        self.assertIn("are serialized", help_text)
        self.assertIn("Reads are lock-free", help_text)
        self.assertIn("need no writable campaign directory", help_text)

    def test_save_campaign_refuses_an_out_of_alphabet_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            campaign = release_campaigns.initialize_campaign(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["codex"],
            ).to_dict()
            campaign["campaign_id"] = "campaign/a"
            with self.assertRaises(ValueError):
                release_campaigns.save_campaign(campaign, campaigns_dir)
            self.assertEqual(list(campaigns_dir.glob("*")), [])

    def test_initialize_campaign_refuses_an_out_of_alphabet_id(self) -> None:
        for candidate in ("campaign/a", "Campaign-A", "..", "c" * 200):
            with self.assertRaises(ValueError):
                release_campaigns.initialize_campaign(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    providers=["codex"],
                    campaign_id=candidate,
                )

    def test_load_campaign_never_resolves_an_invalid_id_to_a_sanitized_neighbour(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            campaign = release_campaigns.initialize_campaign(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["codex"],
                campaign_id="campaign_a",
            )
            release_campaigns.save_campaign(campaign, campaigns_dir)
            self.assertIsNotNone(release_campaigns.load_campaign_by_id("campaign_a", campaigns_dir))
            self.assertIsNone(release_campaigns.load_campaign_by_id("campaign/a", campaigns_dir))
            self.assertIsNone(release_campaigns.load_campaign_by_id("Campaign_A", campaigns_dir))

    def test_release_tags_resolve_by_tag_lookup_and_never_by_id_lookup(self) -> None:
        """A release tag is not a campaign id; each selector has its own exact lookup."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            campaign = release_campaigns.initialize_campaign(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["codex"],
            )
            release_campaigns.save_campaign(campaign, campaigns_dir)

            found, ambiguity = release_campaigns.load_campaign_by_release_tag(
                "v1.0.0", campaigns_dir
            )
            self.assertEqual(ambiguity, "")
            assert found is not None
            self.assertEqual(found["campaign_id"], "campaign-v1.0.0")

            # `v1.0.0` is a well-formed campaign id, but no campaign is stored
            # under it, so the id lookup reports nothing rather than falling
            # back to the campaign that merely carries it as a release tag.
            self.assertIsNone(release_campaigns.load_campaign_by_id("v1.0.0", campaigns_dir))


class CampaignConcurrencyTests(unittest.TestCase):
    """Concurrent campaign commands are serialized across the whole attempt cycle."""

    def _lock_acquirable(self, campaigns_dir: Path, *, timeout: float = 30.0) -> bool:
        """Acquire the directory lock from an independent descriptor, bounded."""
        acquired = threading.Event()

        def acquire() -> None:
            with release_campaigns.locked_campaigns_dir(campaigns_dir):
                acquired.set()

        worker = threading.Thread(target=acquire, daemon=True)
        worker.start()
        worker.join(timeout=timeout)
        return acquired.is_set()

    def test_two_concurrent_applied_invocations_invoke_the_adapter_once(self) -> None:
        """The classic double-dispatch race, forced deterministically.

        The first invocation is frozen at its capability probe -- after it has
        read stored state, before it has claimed the attempt by persisting
        `attempted_at`. That is exactly the window in which an unserialized
        second invocation would read stale state and run the adapter a second
        time. With the directory lock held across the whole cycle, the second
        invocation cannot even begin reading until the first has persisted, so
        it observes `attempted_at` and declines the repeat.
        """
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            fake_lane = _fake_local_cli_lane()

            adapter_calls: list[list[str]] = []
            calls_guard = threading.Lock()

            def counting_adapter_runner(argv, timeout):
                with calls_guard:
                    adapter_calls.append(list(argv))
                output_path = Path(argv[argv.index("--output") + 1])
                with output_path.open("w", encoding="utf-8") as fh:
                    json.dump(_mock_adoption_result(provider="codex"), fh)
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            state_loaded = threading.Event()
            may_proceed = threading.Event()
            gate_spent = threading.Event()

            def gating_which_fn(_cmd: str) -> str:
                if not gate_spent.is_set():
                    gate_spent.set()
                    state_loaded.set()
                    may_proceed.wait(timeout=60)
                return "/bin/fake-provider-cli"

            def invoke(which_fn) -> None:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                    io.StringIO()
                ):
                    release_campaigns.campaign_command(
                        release_tag="v1.0.0",
                        package_spec="code-mower==1.0.0",
                        providers=["codex"],
                        campaigns_dir=campaigns_dir,
                        apply=True,
                        which_fn=which_fn,
                        adapter_runner=counting_adapter_runner,
                    )

            with mock.patch.object(
                release_campaigns, "resolve_provider_lane", return_value=("codex", fake_lane)
            ):
                first = threading.Thread(target=invoke, args=(gating_which_fn,), daemon=True)
                first.start()
                self.assertTrue(state_loaded.wait(timeout=60))

                second = threading.Thread(
                    target=invoke,
                    args=(lambda _cmd: "/bin/fake-provider-cli",),
                    daemon=True,
                )
                second.start()
                may_proceed.set()
                first.join(timeout=120)
                second.join(timeout=120)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())

            # Exactly one adapter side effect for one campaign/provider pair.
            self.assertEqual(len(adapter_calls), 1, adapter_calls)

            # And the campaign is left in valid, fully-persisted state.
            stored = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert stored is not None
            self.assertEqual(stored["schema"], release_campaigns.CAMPAIGN_SCHEMA)
            self.assertEqual(stored["status"], "complete")
            self.assertFalse(stored["dry_run"])
            self.assertEqual(len(stored["providers"]), 1)
            provider_entry = stored["providers"][0]
            self.assertEqual(provider_entry["state"], "complete")
            self.assertTrue(provider_entry["attempted_at"])
            self.assertEqual(provider_entry["error"], "")
            self.assertEqual(
                sorted(p.name for p in campaigns_dir.glob("*.json")),
                ["campaign-v1.0.0.json"],
            )
            self.assertEqual(
                [p.name for p in campaigns_dir.iterdir() if p.name.startswith(".tmp.")], []
            )

    def test_concurrent_hosted_dispatch_posts_one_comment(self) -> None:
        """The same race for a paid/hosted dispatch: exactly one GitHub comment."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            fake_lane = _fake_hosted_bridge_lane()

            posted: list[list[str]] = []
            posts_guard = threading.Lock()

            def counting_command_runner(argv, **_kwargs):
                with posts_guard:
                    posted.append(list(argv))
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            state_loaded = threading.Event()
            may_proceed = threading.Event()
            gate_spent = threading.Event()

            def gating_which_fn(_cmd: str) -> str:
                if not gate_spent.is_set():
                    gate_spent.set()
                    state_loaded.set()
                    may_proceed.wait(timeout=60)
                return "/bin/gh"

            def invoke(which_fn) -> None:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                    io.StringIO()
                ):
                    release_campaigns.campaign_command(
                        release_tag="v1.0.0",
                        package_spec="code-mower==1.0.0",
                        providers=["devin"],
                        repo_slug="owner/repo",
                        issue="42",
                        campaigns_dir=campaigns_dir,
                        apply=True,
                        which_fn=which_fn,
                        command_runner=counting_command_runner,
                        gh_json_runner=lambda *a, **k: (True, []),
                        env={"DEVIN_API_KEY": "token"},
                    )

            with mock.patch.object(
                release_campaigns, "resolve_provider_lane", return_value=("devin", fake_lane)
            ):
                first = threading.Thread(target=invoke, args=(gating_which_fn,), daemon=True)
                first.start()
                self.assertTrue(state_loaded.wait(timeout=60))
                second = threading.Thread(
                    target=invoke, args=(lambda _cmd: "/bin/gh",), daemon=True
                )
                second.start()
                may_proceed.set()
                first.join(timeout=120)
                second.join(timeout=120)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(len(posted), 1, posted)

            stored = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert stored is not None
            self.assertEqual(stored["schema"], release_campaigns.CAMPAIGN_SCHEMA)
            self.assertEqual(stored["providers"][0]["state"], "running")
            self.assertTrue(stored["providers"][0]["attempted_at"])

    def test_lock_is_released_after_an_exception_in_the_critical_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            with self.assertRaises(RuntimeError):
                with release_campaigns.locked_campaigns_dir(campaigns_dir):
                    raise RuntimeError("boom")
            self.assertTrue(self._lock_acquirable(campaigns_dir))

    def test_lock_is_released_when_a_campaign_command_raises(self) -> None:
        """An unexpected failure mid-command must not wedge the campaign directory."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            fake_lane = _fake_local_cli_lane()

            def exploding_which_fn(_cmd: str) -> str:
                raise RuntimeError("boom")

            with mock.patch.object(
                release_campaigns, "resolve_provider_lane", return_value=("codex", fake_lane)
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(RuntimeError):
                        release_campaigns.campaign_command(
                            release_tag="v1.0.0",
                            package_spec="code-mower==1.0.0",
                            providers=["codex"],
                            campaigns_dir=campaigns_dir,
                            apply=True,
                            which_fn=exploding_which_fn,
                        )

            self.assertTrue(self._lock_acquirable(campaigns_dir))

    def test_lock_is_released_when_the_holding_process_dies(self) -> None:
        """No stale-lock protocol: the OS drops a dead holder's advisory lock."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            campaigns_dir.mkdir(parents=True)
            lock_path = campaigns_dir / release_campaigns.CAMPAIGNS_LOCK_FILENAME
            script = (
                "import fcntl, os, sys\n"
                "handle = open(sys.argv[1], 'a+')\n"
                "fcntl.flock(handle.fileno(), fcntl.LOCK_EX)\n"
                "sys.stdout.write('locked')\n"
                "sys.stdout.flush()\n"
                "os._exit(9)\n"
            )
            proc = subprocess.run(
                [sys.executable, "-c", script, str(lock_path)],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(proc.stdout, "locked")
            self.assertEqual(proc.returncode, 9)
            self.assertTrue(lock_path.is_file())
            self.assertTrue(self._lock_acquirable(campaigns_dir))

    def test_unlockable_campaign_directory_reports_a_bounded_error(self) -> None:
        """A mutating command that cannot lock fails closed, without a traceback or a path."""
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "readonly"
            parent.mkdir()
            campaigns_dir = parent / "campaigns"
            parent.chmod(0o500)
            try:
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(
                    io.StringIO()
                ):
                    code = release_campaigns.campaign_command(
                        action="create",
                        release_tag="v1.0.0",
                        package_spec="code-mower==1.0.0",
                        providers=["codex"],
                        campaigns_dir=campaigns_dir,
                    )
            finally:
                parent.chmod(0o700)
            self.assertEqual(code, 1)
            self.assertIn("could not acquire", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertNotIn(str(campaigns_dir), stderr.getvalue())

    def test_lock_file_is_never_projected_as_a_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                release_campaigns.campaign_command(
                    release_tag="v1.0.0", campaigns_dir=campaigns_dir
                )
            self.assertTrue(
                (campaigns_dir / release_campaigns.CAMPAIGNS_LOCK_FILENAME).is_file()
            )
            listed = release_campaigns.list_campaigns(campaigns_dir)
            self.assertEqual([c["campaign_id"] for c in listed], ["campaign-v1.0.0"])


class CampaignLockFreeStatusTests(unittest.TestCase):
    """Read-only status answers without the directory lock, so it never blocks or writes.

    The serialization contract only needs to cover commands that can claim a
    provider attempt, run an adapter, post a dispatch, or otherwise write. A
    status request does none of those, and locking it would make `--status`
    queue behind a long applied run and demand a writable campaign directory to
    answer a question that writes nothing. Campaign files are published by a
    single atomic rename, so a lock-free reader still never sees partial JSON.
    """

    @staticmethod
    def _seed(campaigns_dir: Path) -> dict[str, Any]:
        campaign = release_campaigns.initialize_campaign(
            release_tag="v1.0.0",
            package_spec="code-mower==1.0.0",
            providers=["cursor_bugbot"],
        ).to_dict()
        release_campaigns.save_campaign(campaign, campaigns_dir)
        return campaign

    @staticmethod
    def _snapshot(campaigns_dir: Path) -> dict[str, bytes]:
        return {p.name: p.read_bytes() for p in sorted(campaigns_dir.iterdir())}

    @contextlib.contextmanager
    def _counting_lock(self) -> Any:
        """Count how many times a command enters the campaign directory lock."""
        real_lock = release_campaigns.locked_campaigns_dir
        entries: list[Path] = []

        @contextlib.contextmanager
        def counting(campaigns_dir: Path) -> Any:
            entries.append(campaigns_dir)
            with real_lock(campaigns_dir) as handle:
                yield handle

        with mock.patch.object(release_campaigns, "locked_campaigns_dir", counting):
            yield entries

    def test_status_reads_a_read_only_campaign_directory(self) -> None:
        """A read-only directory cannot hold a lock file, and status must not need one."""
        if os.geteuid() == 0:
            self.skipTest("root bypasses directory write permissions")
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._seed(campaigns_dir)
            before = self._snapshot(campaigns_dir)
            campaigns_dir.chmod(0o500)
            try:
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    ret = release_campaigns.campaign_command(
                        status=True,
                        release_tag="v1.0.0",
                        campaigns_dir=campaigns_dir,
                        emit_json=True,
                    )
                after = self._snapshot(campaigns_dir)
            finally:
                campaigns_dir.chmod(0o700)

            self.assertEqual(ret, 0)
            self.assertEqual(stderr.getvalue(), "")
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["campaign_id"], "campaign-v1.0.0")
            self.assertEqual(payload["schema"], release_campaigns.CAMPAIGN_SCHEMA)
            # Nothing was written: no lock file, no staging file, no rewritten campaign.
            self.assertEqual(after, before)
            self.assertNotIn(release_campaigns.CAMPAIGNS_LOCK_FILENAME, after)

    def test_status_never_creates_the_campaign_directory_or_a_lock_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                ret = release_campaigns.campaign_command(
                    action="status",
                    campaigns_dir=campaigns_dir,
                )
            self.assertEqual(ret, 1)
            self.assertIn("no campaigns found", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertFalse(campaigns_dir.exists())

    def test_status_completes_while_another_process_holds_the_lock(self) -> None:
        """`--status` during a long applied run answers instead of queueing behind it."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._seed(campaigns_dir)
            before = self._snapshot(campaigns_dir)
            lock_path = campaigns_dir / release_campaigns.CAMPAIGNS_LOCK_FILENAME
            script = (
                "import fcntl, sys\n"
                "handle = open(sys.argv[1], 'a+')\n"
                "fcntl.flock(handle.fileno(), fcntl.LOCK_EX)\n"
                "sys.stdout.write('locked\\n')\n"
                "sys.stdout.flush()\n"
                "sys.stdin.readline()\n"
            )
            holder = subprocess.Popen(
                [sys.executable, "-c", script, str(lock_path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
            )
            try:
                assert holder.stdout is not None
                self.assertEqual(holder.stdout.readline(), "locked\n")

                stdout = io.StringIO()
                result: list[int] = []

                def read_status() -> None:
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
                        io.StringIO()
                    ):
                        result.append(
                            release_campaigns.campaign_command(
                                status=True,
                                release_tag="v1.0.0",
                                campaigns_dir=campaigns_dir,
                                emit_json=True,
                            )
                        )

                reader = threading.Thread(target=read_status, daemon=True)
                reader.start()
                reader.join(timeout=60)
                # A lock-taking status would still be blocked on the held lock here.
                self.assertFalse(reader.is_alive())
            finally:
                assert holder.stdin is not None
                holder.stdin.close()
                holder.wait(timeout=60)
                if holder.stdout is not None:
                    holder.stdout.close()

            self.assertEqual(result, [0])
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["campaign_id"], "campaign-v1.0.0")
            after = self._snapshot(campaigns_dir)
            # The holder's lock file is the only new entry; the campaign is untouched.
            self.assertEqual(
                {k: v for k, v in after.items() if k != release_campaigns.CAMPAIGNS_LOCK_FILENAME},
                before,
            )
            self.assertEqual(
                [name for name in after if name.startswith(release_campaigns.CAMPAIGN_TEMP_PREFIX)],
                [],
            )

    def test_status_completes_while_another_thread_holds_the_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._seed(campaigns_dir)
            held = threading.Event()
            release = threading.Event()

            def hold() -> None:
                with release_campaigns.locked_campaigns_dir(campaigns_dir):
                    held.set()
                    release.wait(timeout=120)

            holder = threading.Thread(target=hold, daemon=True)
            holder.start()
            try:
                self.assertTrue(held.wait(timeout=60))
                stdout, stderr = io.StringIO(), io.StringIO()
                result: list[int] = []

                def read_status() -> None:
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                        result.append(
                            release_campaigns.campaign_command(
                                action="status",
                                campaigns_dir=campaigns_dir,
                            )
                        )

                reader = threading.Thread(target=read_status, daemon=True)
                reader.start()
                reader.join(timeout=60)
                self.assertFalse(reader.is_alive())
            finally:
                release.set()
                holder.join(timeout=60)

            self.assertEqual(result, [0])
            self.assertIn("Release Campaign: v1.0.0", stdout.getvalue())
            self.assertEqual(stderr.getvalue(), "")

    def test_status_takes_no_lock_while_mutating_spellings_still_do(self) -> None:
        """The routing split, asserted directly: only read-only status skips the lock."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._seed(campaigns_dir)

            def run(**kwargs: Any) -> tuple[int, int]:
                with self._counting_lock() as entries:
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                        io.StringIO()
                    ):
                        ret = release_campaigns.campaign_command(
                            campaigns_dir=campaigns_dir, **kwargs
                        )
                return ret, len(entries)

            # Read-only status, both spellings: no lock.
            for kwargs in (
                {"status": True, "release_tag": "v1.0.0"},
                {"action": "status"},
            ):
                with self.subTest(kwargs=kwargs):
                    _, locks = run(**kwargs)
                    self.assertEqual(locks, 0)

            # A status request carrying a mutating intent is not a mutating
            # spelling to be locked -- it is refused outright, before any lock
            # (see CampaignStatusIsReadOnlyTests).
            for kwargs in (
                {"status": True, "retry_provider": "cursor_bugbot", "release_tag": "v1.0.0"},
                {"status": True, "record_result": Path(tmp) / "result.json"},
            ):
                with self.subTest(rejected=kwargs):
                    ret, locks = run(**kwargs)
                    self.assertEqual(ret, 1)
                    self.assertEqual(locks, 0)

            # Every mutating spelling keeps the lock. All but the first are
            # rejected after the lock is taken, so nothing here writes either.
            mutating: tuple[tuple[dict[str, Any], int], ...] = (
                ({"action": "create", "release_tag": "v1.0.0"}, 1),
                ({"action": "resume", "release_tag": "v1.0.0", "package_spec": "other==2"}, 1),
                ({"action": "dispatch", "release_tag": "v1.0.0", "package_spec": "other==2"}, 1),
                ({"release_tag": "v1.0.0", "package_spec": "other==2"}, 1),
                ({"action": "resume", "release_tag": "v9.9.9"}, 1),
            )
            for kwargs, expected in mutating:
                with self.subTest(kwargs=kwargs):
                    ret, locks = run(**kwargs)
                    self.assertEqual(locks, 1)
                    self.assertEqual(ret, expected)

            # None of the above wrote to the campaign.
            stored = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert stored is not None
            self.assertEqual(stored["providers"][0]["state"], "queued")


class CampaignAtomicWriteTests(unittest.TestCase):
    """save_campaign stages through a per-write temp file and publishes atomically."""

    def _campaign(self, campaign_id: str = "") -> dict[str, Any]:
        return release_campaigns.initialize_campaign(
            release_tag="v1.0.0",
            package_spec="code-mower==1.0.0",
            providers=["codex"],
            campaign_id=campaign_id,
        ).to_dict()

    def test_each_save_stages_through_a_distinct_temp_file(self) -> None:
        """A shared temp filename would let two writers blend one another's payloads."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            campaign = self._campaign()
            staged: list[str] = []
            real_replace = os.replace

            def recording_replace(src, dst):
                staged.append(str(src))
                real_replace(src, dst)

            with mock.patch.object(release_campaigns.os, "replace", recording_replace):
                release_campaigns.save_campaign(campaign, campaigns_dir)
                release_campaigns.save_campaign(campaign, campaigns_dir)

            self.assertEqual(len(staged), 2)
            self.assertNotEqual(staged[0], staged[1])
            for path in staged:
                self.assertTrue(
                    Path(path).name.startswith(release_campaigns.CAMPAIGN_TEMP_PREFIX)
                )
                self.assertEqual(Path(path).parent, campaigns_dir)

    def test_concurrent_saves_of_one_campaign_leave_a_readable_file(self) -> None:
        """Interleaved direct saves never publish a torn or blended campaign."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            campaigns_dir.mkdir(parents=True)
            first = self._campaign()
            second = self._campaign()
            second["next_detail"] = "x" * 4096

            barrier = threading.Barrier(2)

            def save(payload: dict[str, Any]) -> None:
                barrier.wait(timeout=60)
                for _ in range(25):
                    release_campaigns.save_campaign(payload, campaigns_dir)

            workers = [
                threading.Thread(target=save, args=(first,), daemon=True),
                threading.Thread(target=save, args=(second,), daemon=True),
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=120)
                self.assertFalse(worker.is_alive())

            stored = json.loads((campaigns_dir / "campaign-v1.0.0.json").read_text())
            self.assertEqual(stored["schema"], release_campaigns.CAMPAIGN_SCHEMA)
            self.assertIn(stored, (first, second))
            self.assertEqual(
                [p.name for p in campaigns_dir.iterdir() if p.name.startswith(".tmp.")], []
            )

    def test_a_failed_write_leaves_no_temp_file_behind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            campaign = self._campaign()
            campaign["providers"] = [object()]  # not JSON-serializable
            with self.assertRaises(TypeError):
                release_campaigns.save_campaign(campaign, campaigns_dir)
            self.assertEqual(sorted(p.name for p in campaigns_dir.iterdir()), [])

    def test_a_stray_temp_file_is_never_read_as_a_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            campaigns_dir.mkdir(parents=True)
            release_campaigns.save_campaign(self._campaign(), campaigns_dir)
            stray = campaigns_dir / f"{release_campaigns.CAMPAIGN_TEMP_PREFIX}abc.other.json"
            stray.write_text(
                json.dumps({**self._campaign(), "campaign_id": "other"}), encoding="utf-8"
            )
            listed = release_campaigns.list_campaigns(campaigns_dir)
            self.assertEqual([c["campaign_id"] for c in listed], ["campaign-v1.0.0"])
            self.assertIsNone(release_campaigns.load_campaign_by_id("other", campaigns_dir))


class CampaignReleaseTagLookupTests(unittest.TestCase):
    """A release tag resolves only against stored release tags, never against a filename.

    A campaign id is a storage key -- `<id>.json` -- and a release tag is not.
    Resolving a tag-only request through the id-shaped lookup let a tag that is
    itself a well-formed campaign id (``v1.0.0``) hit that lookup's
    direct-filename shortcut and return whatever campaign a custom
    ``--campaign-id`` had stored under that name, even one for a different
    release. Status would then report another release's state, and resume or
    dispatch would advance and pay for it.
    """

    @staticmethod
    def _seed(
        campaigns_dir: Path,
        *,
        campaign_id: str,
        release_tag: str,
        normalized: str,
        repo_slug: str = "",
    ) -> dict[str, Any]:
        campaign = release_campaigns.initialize_campaign(
            release_tag=release_tag,
            package_spec=f"code-mower=={normalized}",
            providers=["cursor_bugbot"],
            campaign_id=campaign_id,
            repo_slug=repo_slug,
        ).to_dict()
        release_campaigns.save_campaign(campaign, campaigns_dir)
        return campaign

    @staticmethod
    def _snapshot(campaigns_dir: Path) -> dict[str, bytes]:
        """Stored campaigns only: a rejected mutating route may leave its lock file."""
        return {
            p.name: p.read_bytes()
            for p in sorted(campaigns_dir.iterdir())
            if p.name != release_campaigns.CAMPAIGNS_LOCK_FILENAME
        }

    def _collision(self, campaigns_dir: Path, *, repo_slug: str = "") -> dict[str, Any]:
        """A v2.0.0 campaign stored under the custom id ``v1.0.0`` -- also a valid id."""
        self.assertTrue(release_campaigns.is_valid_campaign_id("v1.0.0"))
        return self._seed(
            campaigns_dir,
            campaign_id="v1.0.0",
            release_tag="v2.0.0",
            normalized="2.0.0",
            repo_slug=repo_slug,
        )

    def test_lookup_by_release_tag_ignores_a_colliding_campaign_id(self) -> None:
        """The unit contract: the stored release_tag field is the only thing matched."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._collision(campaigns_dir)

            found, error = release_campaigns.load_campaign_by_release_tag(
                "v1.0.0", campaigns_dir
            )
            self.assertIsNone(found)
            self.assertEqual(error, "")

            # The id-shaped lookup is exactly what must not be used here: it
            # answers the same request with the unrelated v2.0.0 campaign.
            by_id = release_campaigns.load_campaign_by_id("v1.0.0", campaigns_dir)
            assert by_id is not None
            self.assertEqual(by_id["release_tag"], "v2.0.0")

            found, error = release_campaigns.load_campaign_by_release_tag(
                "v2.0.0", campaigns_dir
            )
            assert found is not None
            self.assertEqual(error, "")
            self.assertEqual(found["campaign_id"], "v1.0.0")

    def test_status_by_release_tag_never_reports_a_colliding_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._collision(campaigns_dir)

            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                ret = release_campaigns.campaign_command(
                    status=True,
                    release_tag="v1.0.0",
                    campaigns_dir=campaigns_dir,
                    emit_json=True,
                )

            self.assertEqual(ret, 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("no campaign found", stderr.getvalue())
            self.assertNotIn("v2.0.0", stdout.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())

    def test_resume_by_release_tag_never_advances_a_colliding_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._collision(campaigns_dir)
            before = self._snapshot(campaigns_dir)

            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                ret = release_campaigns.campaign_command(
                    action="resume",
                    release_tag="v1.0.0",
                    campaigns_dir=campaigns_dir,
                )

            self.assertEqual(ret, 1)
            self.assertIn("no existing campaign", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertEqual(self._snapshot(campaigns_dir), before)

    def test_dispatch_by_release_tag_never_dispatches_a_colliding_campaign(self) -> None:
        """The expensive case: an applied dispatch must not be posted for another release."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._collision(campaigns_dir, repo_slug="owner/repo")
            before = self._snapshot(campaigns_dir)

            calls: list[list[str]] = []
            polls: list[list[str]] = []

            def recording_gh_json(args, **kwargs):
                polls.append(list(args))
                return {"comments": []}, ""

            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                ret = release_campaigns.campaign_command(
                    action="dispatch",
                    release_tag="v1.0.0",
                    campaigns_dir=campaigns_dir,
                    repo_slug="owner/repo",
                    issue="99",
                    apply=True,
                    command_runner=_capturing_dispatch_argv_runner(calls),
                    gh_json_runner=recording_gh_json,
                )

            self.assertEqual(ret, 1)
            self.assertEqual(calls, [])
            self.assertEqual(polls, [])
            self.assertIn("no existing campaign", stderr.getvalue())
            self.assertEqual(self._snapshot(campaigns_dir), before)

    def test_unique_release_tag_resolves_a_custom_id_campaign(self) -> None:
        """The tag lookup is not merely restrictive: it finds the campaign that owns the tag."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._seed(
                campaigns_dir,
                campaign_id="hand-picked-name",
                release_tag="v1.0.0",
                normalized="1.0.0",
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                ret = release_campaigns.campaign_command(
                    status=True,
                    release_tag="v1.0.0",
                    campaigns_dir=campaigns_dir,
                    emit_json=True,
                )

            self.assertEqual(ret, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["campaign_id"], "hand-picked-name")
            self.assertEqual(payload["release_tag"], "v1.0.0")

    def test_duplicate_release_tag_is_rejected_as_ambiguous(self) -> None:
        """Two campaigns for one tag: fail boundedly instead of picking by directory order."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._seed(
                campaigns_dir,
                campaign_id="first-campaign",
                release_tag="v1.0.0",
                normalized="1.0.0",
            )
            self._seed(
                campaigns_dir,
                campaign_id="second-campaign",
                release_tag="v1.0.0",
                normalized="1.0.0",
            )
            before = self._snapshot(campaigns_dir)

            found, error = release_campaigns.load_campaign_by_release_tag(
                "v1.0.0", campaigns_dir
            )
            self.assertIsNone(found)
            self.assertIn("--campaign-id", error)

            for kwargs in (
                {"status": True},
                {"action": "resume"},
                {"action": "dispatch", "apply": True},
            ):
                with self.subTest(kwargs=kwargs):
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
                        stderr
                    ):
                        ret = release_campaigns.campaign_command(
                            release_tag="v1.0.0",
                            campaigns_dir=campaigns_dir,
                            **kwargs,
                        )
                    self.assertEqual(ret, 1)
                    self.assertEqual(stdout.getvalue(), "")
                    message = stderr.getvalue()
                    self.assertIn("matches 2 campaigns", message)
                    self.assertIn("--campaign-id", message)
                    self.assertNotIn("Traceback", message)
                    self.assertLess(len(message), 400)

            # Naming one of them resolves the ambiguity without touching the other.
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                ret = release_campaigns.campaign_command(
                    status=True,
                    campaign_id="second-campaign",
                    release_tag="v1.0.0",
                    campaigns_dir=campaigns_dir,
                    emit_json=True,
                )
            self.assertEqual(ret, 0)
            self.assertEqual(json.loads(stdout.getvalue())["campaign_id"], "second-campaign")
            self.assertEqual(self._snapshot(campaigns_dir), before)

    def test_ambiguous_release_tag_error_is_bounded_by_campaign_count(self) -> None:
        """However many campaigns share a tag, the message names at most a few ids."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            for index in range(release_campaigns.AMBIGUOUS_RELEASE_TAG_ID_LIMIT + 3):
                self._seed(
                    campaigns_dir,
                    campaign_id=f"campaign-{index}",
                    release_tag="v1.0.0",
                    normalized="1.0.0",
                )

            _, error = release_campaigns.load_campaign_by_release_tag("v1.0.0", campaigns_dir)
            named = [token for token in error.split() if token.startswith("campaign-")]
            self.assertLessEqual(
                len(named), release_campaigns.AMBIGUOUS_RELEASE_TAG_ID_LIMIT
            )
            self.assertLess(len(error), 400)

    def test_campaign_id_and_release_tag_must_still_both_match(self) -> None:
        """Supplying both identifiers keeps requiring both stored fields to agree."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._collision(campaigns_dir)
            before = self._snapshot(campaigns_dir)

            for kwargs in ({"status": True}, {"action": "resume"}):
                with self.subTest(kwargs=kwargs):
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
                        stderr
                    ):
                        ret = release_campaigns.campaign_command(
                            campaign_id="v1.0.0",
                            release_tag="v1.0.0",
                            campaigns_dir=campaigns_dir,
                            **kwargs,
                        )
                    self.assertEqual(ret, 1)
                    self.assertEqual(stdout.getvalue(), "")
                    self.assertIn("does not match", stderr.getvalue())
            self.assertEqual(self._snapshot(campaigns_dir), before)

    def test_creation_never_overwrites_a_file_holding_another_campaign(self) -> None:
        """Creating by tag must not publish over the id a foreign campaign already occupies."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._seed(
                campaigns_dir,
                campaign_id="campaign-v1.0.0",
                release_tag="v2.0.0",
                normalized="2.0.0",
            )
            before = self._snapshot(campaigns_dir)

            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                ret = release_campaigns.campaign_command(
                    action="create",
                    release_tag="v1.0.0",
                    campaigns_dir=campaigns_dir,
                )

            self.assertEqual(ret, 1)
            self.assertIn("already in use", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertEqual(self._snapshot(campaigns_dir), before)

            # A free id creates normally.
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                ret = release_campaigns.campaign_command(
                    action="create",
                    release_tag="v1.0.0",
                    campaign_id="campaign-one-oh",
                    campaigns_dir=campaigns_dir,
                )
            self.assertEqual(ret, 0)
            stored, error = release_campaigns.load_campaign_by_release_tag(
                "v1.0.0", campaigns_dir
            )
            assert stored is not None
            self.assertEqual(error, "")
            self.assertEqual(stored["campaign_id"], "campaign-one-oh")


class CampaignIdExactLookupTests(unittest.TestCase):
    """An explicit `--campaign-id` resolves to that id's file, or to nothing.

    The id lookup used to be dual-purpose: when `<id>.json` was absent it
    scanned the directory and matched the stored `campaign_id` *or* the stored
    `release_tag`. So naming an id that no campaign was stored under could be
    answered with an unrelated campaign that merely carried that text as its
    release tag -- and status would report it, while resume or dispatch would
    advance and pay for it.
    """

    @staticmethod
    def _seed(
        campaigns_dir: Path,
        *,
        campaign_id: str,
        release_tag: str,
        normalized: str,
    ) -> dict[str, Any]:
        campaign = release_campaigns.initialize_campaign(
            release_tag=release_tag,
            package_spec=f"code-mower=={normalized}",
            providers=["cursor_bugbot"],
            campaign_id=campaign_id,
        ).to_dict()
        release_campaigns.save_campaign(campaign, campaigns_dir)
        return campaign

    def test_an_id_lookup_never_falls_back_to_a_release_tag_match(self) -> None:
        """The audit's collision: `--campaign-id v1.0.0` with only a v1.0.0-tagged campaign."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._seed(
                campaigns_dir,
                campaign_id="campaign-one",
                release_tag="v1.0.0",
                normalized="1.0.0",
            )
            self.assertTrue(release_campaigns.is_valid_campaign_id("v1.0.0"))
            self.assertFalse((campaigns_dir / "v1.0.0.json").exists())

            self.assertIsNone(release_campaigns.load_campaign_by_id("v1.0.0", campaigns_dir))
            # The campaign is still reachable by the identifier it actually has.
            found = release_campaigns.load_campaign_by_id("campaign-one", campaigns_dir)
            assert found is not None
            self.assertEqual(found["release_tag"], "v1.0.0")

    def test_status_by_colliding_id_reports_not_found_rather_than_another_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._seed(
                campaigns_dir,
                campaign_id="campaign-one",
                release_tag="v1.0.0",
                normalized="1.0.0",
            )

            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                ret = release_campaigns.campaign_command(
                    status=True,
                    campaign_id="v1.0.0",
                    campaigns_dir=campaigns_dir,
                    emit_json=True,
                )

            self.assertEqual(ret, 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("no campaign found", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertNotIn("campaign-one", stderr.getvalue())

    def test_an_id_lookup_rejects_a_file_whose_stored_id_disagrees(self) -> None:
        """The file is authoritative about its own identity; a mismatch is not this campaign."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            campaign = self._seed(
                campaigns_dir,
                campaign_id="campaign-one",
                release_tag="v1.0.0",
                normalized="1.0.0",
            )
            # A hand-edited or copied file: stem says `campaign-two`, stored id
            # still says `campaign-one`.
            (campaigns_dir / "campaign-two.json").write_text(
                json.dumps(campaign, indent=2, sort_keys=True),
                encoding="utf-8",
            )

            self.assertIsNone(release_campaigns.load_campaign_by_id("campaign-two", campaigns_dir))
            self.assertIsNotNone(
                release_campaigns.load_campaign_by_id("campaign-one", campaigns_dir)
            )

    def test_release_tag_selection_is_unchanged_by_the_exact_id_lookup(self) -> None:
        """`--release-tag` still resolves through the stored release_tag field."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._seed(
                campaigns_dir,
                campaign_id="v1.0.0",
                release_tag="v2.0.0",
                normalized="2.0.0",
            )
            self._seed(
                campaigns_dir,
                campaign_id="campaign-one",
                release_tag="v1.0.0",
                normalized="1.0.0",
            )

            found, error = release_campaigns.load_campaign_by_release_tag(
                "v1.0.0", campaigns_dir
            )
            self.assertEqual(error, "")
            assert found is not None
            self.assertEqual(found["campaign_id"], "campaign-one")

            # And the id `v1.0.0` still names the campaign stored under it.
            by_id = release_campaigns.load_campaign_by_id("v1.0.0", campaigns_dir)
            assert by_id is not None
            self.assertEqual(by_id["release_tag"], "v2.0.0")


class AdapterTimeoutValidationTests(unittest.TestCase):
    """`campaign_adapter_timeout_seconds` is a positive integer, enforced as written.

    `int(value)` truncates, so `1.9` silently became a 1-second adapter budget
    and `0.5` became a zero-second one -- a value this function is meant to
    reject turning into an immediately-failing timeout. Non-finite floats fared
    worse: `int(float("nan"))` raises `ValueError` and `int(float("inf"))` raises
    `OverflowError`, which is not the bounded error every other malformed value
    gets.
    """

    def _rejects(self, value: Any) -> None:
        with self.assertRaises(ValueError) as ctx:
            release_campaigns._validate_adapter_timeout(value)
        self.assertEqual(
            str(ctx.exception),
            "campaign_adapter_timeout_seconds must be a positive integer",
        )

    def test_fractional_floats_are_rejected_not_truncated(self) -> None:
        for value in (1.9, 0.5, 900.0001, -0.5):
            with self.subTest(value=value):
                self._rejects(value)

    def test_non_finite_floats_are_rejected_with_the_bounded_error(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                self._rejects(value)

    def test_bools_are_rejected_even_though_they_are_ints(self) -> None:
        for value in (True, False):
            with self.subTest(value=value):
                self._rejects(value)

    def test_zero_and_negative_values_are_rejected(self) -> None:
        for value in (0, -1, 0.0, -30, "0", "-30"):
            with self.subTest(value=value):
                self._rejects(value)

    def test_non_numeric_values_are_rejected(self) -> None:
        for value in (None, "", "   ", "60s", "1.9", [60], {"seconds": 60}):
            with self.subTest(value=value):
                self._rejects(value)

    def test_integral_numeric_values_are_accepted(self) -> None:
        self.assertEqual(release_campaigns._validate_adapter_timeout(60), 60)
        self.assertEqual(release_campaigns._validate_adapter_timeout(60.0), 60)

    def test_base_ten_integer_strings_are_accepted(self) -> None:
        """The repo-config YAML subset leaves bare numbers as strings."""
        self.assertEqual(release_campaigns._validate_adapter_timeout("60"), 60)
        self.assertEqual(release_campaigns._validate_adapter_timeout("  60  "), 60)

    def test_a_fractional_repo_config_override_is_adapter_configuration_invalid(self) -> None:
        """End to end: the adapter never runs on a truncated timeout budget."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            campaigns_dir = repo_path / ".code-mower" / "campaigns"
            (repo_path / "code-mower.yml").write_text(
                "version: 1\n"
                "lanes:\n"
                "  muse_cli:\n"
                "    provider_config:\n"
                "      campaign_adapter_argv:\n"
                '        - "{command}"\n'
                "        - qualify\n"
                "        - --output\n"
                '        - "{output}"\n'
                "      campaign_adapter_timeout_seconds: 1.9\n",
                encoding="utf-8",
            )
            adapter_mock = mock.MagicMock()

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["muse"],
                repo_path=repo_path,
                campaigns_dir=campaigns_dir,
                apply=True,
                which_fn=lambda _cmd: "/bin/muse",
                adapter_runner=adapter_mock,
            )

            adapter_mock.assert_not_called()
            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            self.assertEqual(saved["providers"][0]["error"], "adapter_configuration_invalid")
            self.assertNotIn("1.9", json.dumps(saved))


class CampaignStatusIsReadOnlyTests(unittest.TestCase):
    """`status` is a read-only spelling; a mutating intent alongside it is refused.

    The read-only route deliberately skips the campaign directory lock, so
    honoring a mutation under a status spelling would also run it outside the
    serialization contract. The previous behavior did the opposite and dropped
    it: `--status --retry-provider` took the lock, fell into the status branch,
    printed the campaign, and exited 0 -- the retry never happened and nothing
    said so. Both are refused now, before any lock, mutation, poll, or dispatch.
    """

    @staticmethod
    def _seed(campaigns_dir: Path) -> dict[str, Any]:
        campaign = release_campaigns.initialize_campaign(
            release_tag="v1.0.0",
            package_spec="code-mower==1.0.0",
            providers=["cursor_bugbot"],
            repo_slug="owner/repo",
        ).to_dict()
        release_campaigns.save_campaign(campaign, campaigns_dir)
        return campaign

    @staticmethod
    def _snapshot(campaigns_dir: Path) -> dict[str, bytes]:
        return {p.name: p.read_bytes() for p in sorted(campaigns_dir.iterdir())}

    @contextlib.contextmanager
    def _counting_lock(self) -> Any:
        real_lock = release_campaigns.locked_campaigns_dir
        entries: list[Path] = []

        @contextlib.contextmanager
        def counting(campaigns_dir: Path) -> Any:
            entries.append(campaigns_dir)
            with real_lock(campaigns_dir) as handle:
                yield handle

        with mock.patch.object(release_campaigns, "locked_campaigns_dir", counting):
            yield entries

    def _mutating_intents(self, tmp: Path) -> tuple[tuple[str, dict[str, Any]], ...]:
        result_path = tmp / "result.json"
        result_path.write_text(
            json.dumps(_mock_adoption_result(provider="cursor_bugbot")), encoding="utf-8"
        )
        return (
            ("--retry-provider", {"retry_provider": "cursor_bugbot"}),
            (
                "--record-result",
                {"record_result": result_path, "record_provider": "cursor_bugbot"},
            ),
            ("--apply", {"apply": True}),
            ("--resume", {"resume": True}),
            ("the 'resume' action", {"action": "resume"}),
            ("the 'dispatch' action", {"action": "dispatch"}),
            ("the 'create' action", {"action": "create"}),
        )

    def _run_rejected(
        self,
        campaigns_dir: Path,
        kwargs: dict[str, Any],
    ) -> tuple[int, list[Path], list[list[str]], list[list[str]], str, str]:
        """Run one request with every outward effect recorded rather than performed."""
        gh_calls: list[list[str]] = []
        adapter_calls: list[list[str]] = []

        def recording_adapter(argv, timeout):
            adapter_calls.append(list(argv))
            raise AssertionError("adapter must not run for a status request")

        def recording_gh_json(args, **_kwargs):
            gh_calls.append(list(args))
            return {"comments": []}, ""

        stdout, stderr = io.StringIO(), io.StringIO()
        with self._counting_lock() as entries:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                ret = release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    campaigns_dir=campaigns_dir,
                    repo_slug="owner/repo",
                    issue="99",
                    command_runner=_capturing_dispatch_argv_runner(gh_calls),
                    gh_json_runner=recording_gh_json,
                    adapter_runner=recording_adapter,
                    **kwargs,
                )
        return ret, list(entries), gh_calls, adapter_calls, stdout.getvalue(), stderr.getvalue()

    def test_status_with_a_mutating_intent_is_rejected_before_any_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._seed(campaigns_dir)
            before = self._snapshot(campaigns_dir)

            for named, intent in self._mutating_intents(Path(tmp)):
                # Both spellings of a status request, except where the intent is
                # itself an action (which cannot be combined with `status`).
                spellings: tuple[dict[str, Any], ...] = (
                    ({"status": True},)
                    if "action" in intent
                    else ({"status": True}, {"action": "status"})
                )
                for spelling in spellings:
                    with self.subTest(intent=named, spelling=spelling):
                        ret, entries, gh_calls, adapter_calls, out, err = self._run_rejected(
                            campaigns_dir, {**spelling, **intent}
                        )

                        self.assertEqual(ret, 1)
                        self.assertEqual(entries, [])
                        self.assertEqual(gh_calls, [])
                        self.assertEqual(adapter_calls, [])
                        self.assertEqual(out, "")
                        self.assertIn("status is read-only", err)
                        self.assertIn(named, err)
                        self.assertNotIn("Traceback", err)
                        self.assertEqual(self._snapshot(campaigns_dir), before)

    def test_status_with_retry_provider_does_not_silently_drop_the_retry(self) -> None:
        """The exact regression: the retry is neither executed nor quietly discarded."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            campaign = self._seed(campaigns_dir)
            campaign["providers"][0]["state"] = "running"
            campaign["providers"][0]["attempted_at"] = "2026-09-04T08:00:00Z"
            campaign["providers"][0]["dispatch_ref"] = {"issue_number": "99"}
            release_campaigns.save_campaign(campaign, campaigns_dir)
            before = self._snapshot(campaigns_dir)

            gh_calls: list[list[str]] = []
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                ret = release_campaigns.campaign_command(
                    status=True,
                    retry_provider="cursor_bugbot",
                    release_tag="v1.0.0",
                    repo_slug="owner/repo",
                    issue="99",
                    apply=False,
                    campaigns_dir=campaigns_dir,
                    command_runner=_capturing_dispatch_argv_runner(gh_calls),
                )

            # Not executed: no dispatch, no state change.
            self.assertEqual(gh_calls, [])
            self.assertEqual(self._snapshot(campaigns_dir), before)
            # Not dropped: non-zero exit and an explicit reason, not a status report.
            self.assertEqual(ret, 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("--retry-provider", stderr.getvalue())

    def test_status_with_record_result_records_nothing(self) -> None:
        """The rejected record is genuinely not applied -- and still works without --status."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._seed(campaigns_dir)
            result_path = Path(tmp) / "result.json"
            result_path.write_text(
                json.dumps(_mock_adoption_result(provider="cursor_bugbot")), encoding="utf-8"
            )

            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                ret = release_campaigns.campaign_command(
                    status=True,
                    release_tag="v1.0.0",
                    campaigns_dir=campaigns_dir,
                    record_result=result_path,
                    record_provider="cursor_bugbot",
                )
            self.assertEqual(ret, 1)
            stored = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert stored is not None
            self.assertEqual(stored["providers"][0]["state"], "queued")

            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                ret = release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    campaigns_dir=campaigns_dir,
                    record_result=result_path,
                    record_provider="cursor_bugbot",
                )
            self.assertEqual(ret, 0)
            stored = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert stored is not None
            self.assertEqual(stored["providers"][0]["state"], "complete")

    def test_plain_status_still_reports_and_stays_lock_free(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._seed(campaigns_dir)
            before = self._snapshot(campaigns_dir)

            for spelling in ({"status": True}, {"action": "status"}):
                with self.subTest(spelling=spelling):
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with self._counting_lock() as entries:
                        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
                            stderr
                        ):
                            ret = release_campaigns.campaign_command(
                                release_tag="v1.0.0",
                                campaigns_dir=campaigns_dir,
                                **spelling,
                            )
                    self.assertEqual(ret, 0)
                    self.assertEqual(entries, [])
                    self.assertEqual(stderr.getvalue(), "")
                    self.assertIn("Release Campaign: v1.0.0", stdout.getvalue())
                    self.assertEqual(self._snapshot(campaigns_dir), before)

    def test_cli_help_documents_the_read_only_status_contract(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit):
                release_qualify.main(["campaign", "--help"])
        # argparse rewraps help text, so compare on a whitespace-collapsed copy.
        help_text = " ".join(stdout.getvalue().split())
        self.assertIn("Status is strictly read-only", help_text)
        self.assertIn("before any lock, mutation, poll, or dispatch", help_text)
        self.assertIn("rather than silently dropping the mutation", help_text)


class ResultMarkerParsingTests(unittest.TestCase):
    """The result marker is one line of JSON, matched end to end and parsed fail-closed.

    Markers are emitted as single-line HTML comments. The pattern anchors the
    whole line and captures greedily to the JSON object's own final brace, so a
    literal `-->` inside a permitted string value cannot end the capture early.
    The previous lazy, DOTALL pattern ended it at the first `}` followed by
    `-->`, which a quoted example of the marker format supplies -- truncating a
    correctly bound, trusted result into unparseable JSON and discarding it.
    """

    @staticmethod
    def _wrapper(campaign: Any, **extra: Any) -> dict[str, Any]:
        return {
            "schema": release_campaigns.RESULT_MARKER_SCHEMA,
            "campaign_id": campaign.campaign_id,
            "provider": "cursor_bugbot",
            "release_tag": "v1.0.0",
            "idempotency_key": campaign.providers[0]["idempotency_key"],
            "adoption_result": _mock_adoption_result(
                release_tag="v1.0.0", provider="cursor_bugbot", outcome="pass"
            ),
            **extra,
        }

    @staticmethod
    def _running_campaign(campaigns_dir: Path) -> Any:
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
        return campaign

    @staticmethod
    def _poll(campaigns_dir: Path, body: str) -> dict[str, Any]:
        def mock_gh_json(args, **kwargs):
            return {"comments": [{"author": {"login": "cursor[bot]"}, "body": body}]}, ""

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ):
            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                resume=True,
                repo_slug="owner/repo",
                gh_json_runner=mock_gh_json,
            )
        saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
        assert saved is not None
        return saved

    def test_regex_captures_json_containing_a_literal_comment_terminator(self) -> None:
        payload = {"note": "reply with <!-- CODE_MOWER_ADOPTION_RESULT: {...} -->", "n": 1}
        line = f"<!-- CODE_MOWER_ADOPTION_RESULT: {json.dumps(payload)} -->"
        match = release_campaigns.RESULT_MARKER_RE.search(f"Done!\n\n{line}\n")
        assert match is not None
        self.assertEqual(json.loads(match.group(1)), payload)

    def test_trusted_result_survives_a_comment_terminator_in_a_string_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            campaign = self._running_campaign(campaigns_dir)
            wrapper = self._wrapper(
                campaign,
                note="format: <!-- CODE_MOWER_ADOPTION_RESULT: {json} --> (one line)",
            )
            marker = f"<!-- CODE_MOWER_ADOPTION_RESULT: {json.dumps(wrapper)} -->"
            self.assertIn("} -->", json.dumps(wrapper))

            saved = self._poll(campaigns_dir, f"Review complete!\n\n{marker}\n")
            self.assertEqual(saved["providers"][0]["state"], "complete")
            self.assertEqual(saved["status"], "complete")
            # The free-form note is diagnostic only and never reaches campaign state.
            self.assertNotIn("note", json.dumps(saved))

    def test_bound_result_is_still_accepted_without_any_terminator_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            campaign = self._running_campaign(campaigns_dir)
            marker = f"<!-- CODE_MOWER_ADOPTION_RESULT: {json.dumps(self._wrapper(campaign))} -->"
            saved = self._poll(campaigns_dir, f"Review complete!\n\n{marker}\n")
            self.assertEqual(saved["providers"][0]["state"], "complete")

    def test_malformed_markers_fail_closed(self) -> None:
        """Unparseable or non-JSON markers are ignored; the provider keeps running."""
        bodies = (
            "<!-- CODE_MOWER_ADOPTION_RESULT: {not json at all} -->",
            '<!-- CODE_MOWER_ADOPTION_RESULT: {"schema": "x", -->',
            "<!-- CODE_MOWER_ADOPTION_RESULT: -->",
            "<!-- CODE_MOWER_ADOPTION_RESULT: [] -->",
        )
        for body in bodies:
            with self.subTest(body=body):
                with tempfile.TemporaryDirectory() as tmp:
                    campaigns_dir = Path(tmp) / "campaigns"
                    self._running_campaign(campaigns_dir)
                    saved = self._poll(campaigns_dir, body)
                    self.assertEqual(saved["providers"][0]["state"], "running")

    def test_a_marker_split_across_lines_is_not_accepted(self) -> None:
        """Markers are single-line by contract; a multi-line one fails closed."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            campaign = self._running_campaign(campaigns_dir)
            body = (
                "<!-- CODE_MOWER_ADOPTION_RESULT:\n"
                f"{json.dumps(self._wrapper(campaign))}\n"
                "-->"
            )
            saved = self._poll(campaigns_dir, body)
            self.assertEqual(saved["providers"][0]["state"], "running")


class CampaignLockContentionTests(unittest.TestCase):
    """Bounded contention from the portable lock backend is a bounded command error.

    The Windows lock backend cannot block in the kernel, so it gives up after a
    bounded wait and raises `FileLockError` -- a `RuntimeError`, not an
    `OSError`. The command's lock handler caught only `OSError`, so on Windows a
    contended campaign directory ended a mutating command in a raw traceback
    instead of the bounded, path-free refusal every other campaign error
    surface gives.
    """

    @staticmethod
    def _contended() -> Any:
        """Stand in for the lock helper, failing the way the Windows backend does."""
        return mock.patch.object(
            release_campaigns,
            "exclusive_file_lock",
            side_effect=file_locks.FileLockError("timed out waiting for an exclusive lock"),
        )

    def test_lock_contention_is_a_bounded_path_free_command_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            stdout, stderr = io.StringIO(), io.StringIO()
            with self._contended():
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = release_campaigns.campaign_command(
                        action="create",
                        release_tag="v1.0.0",
                        package_spec="code-mower==1.0.0",
                        providers=["codex"],
                        campaigns_dir=campaigns_dir,
                    )

            self.assertEqual(code, 1)
            message = stderr.getvalue()
            self.assertIn("could not acquire", message)
            self.assertNotIn("Traceback", message)
            self.assertNotIn("FileLockError", message)
            self.assertNotIn(str(campaigns_dir), message)
            self.assertNotIn(tmp, message)
            # Failing closed: contention writes no campaign at all.
            self.assertEqual(release_campaigns.list_campaigns(campaigns_dir), [])

    def test_lock_contention_through_the_cli_reports_the_contention_message(self) -> None:
        """The CLI surfaces the contention refusal itself, not a generic guard."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            stdout, stderr = io.StringIO(), io.StringIO()
            with self._contended():
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = release_qualify.main(
                        [
                            "campaign",
                            "create",
                            "--release-tag",
                            "v1.0.0",
                            "--package-spec",
                            "code-mower==1.0.0",
                            "--providers",
                            "codex",
                            "--campaigns-dir",
                            str(campaigns_dir),
                        ]
                    )

            self.assertEqual(code, 1)
            message = stderr.getvalue()
            self.assertIn(
                "could not acquire the release campaign directory lock", message
            )
            self.assertIn("another campaign command is holding it", message)
            self.assertNotIn("Traceback", message)
            self.assertNotIn(str(campaigns_dir), message)
            self.assertLessEqual(len(message.splitlines()), 1)

    def test_an_unlockable_directory_still_reports_the_writability_message(self) -> None:
        """The pre-existing OSError route is unchanged and still distinguishable."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.object(
                release_campaigns,
                "exclusive_file_lock",
                side_effect=PermissionError(13, "Permission denied"),
            ):
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = release_campaigns.campaign_command(
                        action="create",
                        release_tag="v1.0.0",
                        package_spec="code-mower==1.0.0",
                        providers=["codex"],
                        campaigns_dir=campaigns_dir,
                    )

            self.assertEqual(code, 1)
            message = stderr.getvalue()
            self.assertIn("exists and is writable", message)
            self.assertNotIn("Traceback", message)
            self.assertNotIn("Permission denied", message)

    def test_status_is_unaffected_by_contention(self) -> None:
        """Status takes no lock, so a wedged holder cannot block a read."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                release_campaigns.campaign_command(
                    action="create",
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    providers=["codex"],
                    campaigns_dir=campaigns_dir,
                )

            stdout, stderr = io.StringIO(), io.StringIO()
            with self._contended():
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = release_campaigns.campaign_command(
                        release_tag="v1.0.0",
                        campaigns_dir=campaigns_dir,
                        status=True,
                    )

            self.assertEqual(code, 0)
            self.assertEqual(stderr.getvalue(), "")
            self.assertIn("Release Campaign: v1.0.0", stdout.getvalue())


class AdapterArgvPlaceholderFailureTests(unittest.TestCase):
    """Every malformed `campaign_adapter_argv` placeholder is one bounded config error.

    `str.format` reports a bad placeholder in more ways than a missing field:
    `{repo_path.parent}` is an `AttributeError` and `{output[dir]}` is a
    `TypeError`, because the substitutions are plain strings. Only `KeyError`
    and `IndexError` were caught, so those two escaped the applied run *after*
    `attempted_at` had been persisted -- a raw traceback, and a provider left
    looking queued that an ordinary resume would then skip.
    """

    @staticmethod
    def _lane_with(*argv_tail: str) -> ProviderLane:
        return ProviderLane(
            lane_id="fake_cli",
            lane_type="audit",
            driver="local_cli",
            provider="codex",
            labels=LaneLabels(needs="needs-fake", done="fake-done", blocked="fake-blocked"),
            trigger_policy="manual",
            provider_config={
                "command": "fake-provider-cli",
                "campaign_adapter_argv": ("{command}", *argv_tail),
            },
        )

    def _run(self, campaigns_dir: Path, lane: ProviderLane) -> tuple[int, str, dict[str, Any]]:
        adapter_mock = mock.MagicMock()
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(
            release_campaigns, "resolve_provider_lane", return_value=("codex", lane)
        ):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    providers=["codex"],
                    campaigns_dir=campaigns_dir,
                    apply=True,
                    which_fn=lambda _cmd: "/bin/fake-provider-cli",
                    adapter_runner=adapter_mock,
                )
        adapter_mock.assert_not_called()
        saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
        assert saved is not None
        return code, stderr.getvalue(), saved

    def _assert_bounded_configuration_failure(self, saved: dict[str, Any]) -> None:
        provider_entry = saved["providers"][0]
        self.assertEqual(provider_entry["state"], "unavailable")
        self.assertEqual(provider_entry["error"], "adapter_configuration_invalid")
        # The attempt was claimed and is now reported as a failure, rather than
        # left looking like un-attempted queued work a resume would skip.
        self.assertTrue(provider_entry["attempted_at"])
        # Clear retry guidance: what to fix, and the manual way out.
        self.assertIn("campaign_adapter_argv", provider_entry["next_action"])
        self.assertIn("record manual result", provider_entry["next_action"])

    def test_an_attribute_access_placeholder_is_a_bounded_configuration_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            lane = self._lane_with("--repo", "{repo_path.parent}")
            code, stderr, saved = self._run(campaigns_dir, lane)

            self.assertEqual(code, 0)
            self.assertNotIn("Traceback", stderr)
            self.assertNotIn("AttributeError", stderr)
            self._assert_bounded_configuration_failure(saved)
            serialized = json.dumps(saved)
            self.assertNotIn("repo_path.parent", serialized)
            self.assertNotIn(tmp, serialized)

    def test_a_non_integer_subscript_placeholder_is_the_same_failure(self) -> None:
        """`{output[dir]}` raises TypeError, which escaped exactly the same way."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            lane = self._lane_with("--output", "{output[dir]}")
            code, stderr, saved = self._run(campaigns_dir, lane)

            self.assertEqual(code, 0)
            self.assertNotIn("Traceback", stderr)
            self.assertNotIn("TypeError", stderr)
            self._assert_bounded_configuration_failure(saved)
            self.assertNotIn("output[dir]", json.dumps(saved))

    def test_the_builder_raises_one_bounded_value_error_for_every_spelling(self) -> None:
        """At the unit boundary each spelling is one ValueError naming only the lane."""
        lane = _fake_local_cli_lane()
        for token in ("{repo_path.parent}", "{output[dir]}", "{bogus}", "{output[999]}"):
            with self.subTest(token=token):
                with self.assertRaises(ValueError) as ctx:
                    release_campaigns._build_adapter_argv(
                        lane,
                        "/bin/fake-provider-cli",
                        release_tag="v1.0.0",
                        package_spec="code-mower==1.0.0",
                        qualification_context="cold_install",
                        starting_version="",
                        output_path=Path("/tmp/out.json"),
                        repo_path=Path("/tmp/repo"),
                        argv_template=("{command}", token),
                    )
                message = str(ctx.exception)
                self.assertEqual(
                    message, "invalid campaign_adapter_argv template for lane 'fake_cli'"
                )
                self.assertNotIn(token, message)

    def test_a_resume_after_the_failure_reports_it_instead_of_skipping(self) -> None:
        """The provider stays visible with its error, not silently passed over."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            lane = self._lane_with("--repo", "{repo_path.parent}")
            self._run(campaigns_dir, lane)

            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    campaigns_dir=campaigns_dir,
                    status=True,
                )

            self.assertEqual(code, 0)
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertIn("codex: unavailable", stdout.getvalue())
            self.assertIn("fix campaign_adapter_argv", stdout.getvalue())


class CampaignCliExceptionGuardTests(unittest.TestCase):
    """An unexpected campaign exception ends as one bounded line, never a traceback.

    The `qualify` subcommand has always guarded its implementation call; the
    `campaign` subcommand did not, so anything the campaign implementation did
    not anticipate reached the interpreter and printed a raw traceback -- which
    on this metadata-only surface can also echo local paths.
    """

    @staticmethod
    def _argv(campaigns_dir: Path, *extra: str) -> list[str]:
        return [
            "campaign",
            "--release-tag",
            "v1.0.0",
            "--package-spec",
            "code-mower==1.0.0",
            "--providers",
            "codex",
            "--campaigns-dir",
            str(campaigns_dir),
            *extra,
        ]

    def test_an_unexpected_exception_is_bounded_and_non_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            secret = str(campaigns_dir / "private-detail")
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.object(
                release_campaigns,
                "campaign_command",
                side_effect=RuntimeError(f"boom {secret}"),
            ):
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = release_qualify.main(self._argv(campaigns_dir))

            self.assertEqual(code, 1)
            message = stderr.getvalue()
            self.assertEqual(message.strip(), "error: campaign failed: RuntimeError")
            self.assertNotIn("Traceback", message)
            self.assertNotIn(secret, message)
            self.assertNotIn(tmp, message)

    def test_an_unexpected_value_error_keeps_its_bounded_message(self) -> None:
        """ValueError is the campaign code's own bounded spelling, as for qualify."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.object(
                release_campaigns,
                "campaign_command",
                side_effect=ValueError("unregistered campaign error code: 'nope'"),
            ):
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = release_qualify.main(self._argv(campaigns_dir))

            self.assertEqual(code, 1)
            message = stderr.getvalue()
            self.assertEqual(
                message.strip(), "error: unregistered campaign error code: 'nope'"
            )
            self.assertNotIn("Traceback", message)

    def test_the_guard_does_not_swallow_a_process_exit(self) -> None:
        """SystemExit and KeyboardInterrupt are not application errors."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            for exc in (SystemExit(3), KeyboardInterrupt()):
                with self.subTest(exc=type(exc).__name__):
                    with mock.patch.object(
                        release_campaigns, "campaign_command", side_effect=exc
                    ):
                        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                            io.StringIO()
                        ):
                            with self.assertRaises(type(exc)):
                                release_qualify.main(self._argv(campaigns_dir))

    def test_a_specific_bounded_error_still_passes_through_unchanged(self) -> None:
        """The guard adds a floor; it does not reword what the command already says."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = release_qualify.main(
                    self._argv(campaigns_dir, "--campaign-id", "not a valid id")
                )

            self.assertEqual(code, 1)
            message = stderr.getvalue()
            self.assertIn("campaign_id must use only", message)
            self.assertNotIn("campaign failed", message)
            self.assertNotIn("Traceback", message)
            self.assertFalse(campaigns_dir.exists())

    def test_an_ordinary_campaign_still_succeeds_under_the_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = release_qualify.main(self._argv(campaigns_dir, "create"))

            self.assertEqual(code, 0)
            self.assertEqual(stderr.getvalue(), "")
            created = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert created is not None
            self.assertEqual(created["schema"], release_campaigns.CAMPAIGN_SCHEMA)


if __name__ == "__main__":
    unittest.main()
