#!/usr/bin/env python3
"""Tests for release qualification campaigns."""

from __future__ import annotations

import contextlib
import io
import json
import re
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
            self.assertIsNone(release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir))


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

            saved = release_campaigns.load_campaign("campaign-v1.1.0", campaigns_dir)
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

            saved = release_campaigns.load_campaign("campaign-v1.1.0", campaigns_dir)
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

            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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

            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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
            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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

            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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
            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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
            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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
            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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
            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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

                saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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
                saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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
                saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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
                saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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

                    saved = release_campaigns.load_campaign("campaign-v1.1.0", campaigns_dir)
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

            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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
            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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
            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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
            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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

            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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

            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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

            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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
            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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

            saved = release_campaigns.load_campaign("campaign-v1.1.0", campaigns_dir)
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

            saved = release_campaigns.load_campaign("campaign-v1.1.0", campaigns_dir)
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

                    saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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

            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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

            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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

            saved = release_campaigns.load_campaign("campaign-v2.0.0", campaigns_dir)
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

            saved = release_campaigns.load_campaign("campaign-v2.0.0", campaigns_dir)
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

            saved = release_campaigns.load_campaign("campaign-v2.0.0", campaigns_dir)
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

            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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

            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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

            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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

            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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

            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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

            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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
                first = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
                assert first is not None

                # Exactly the same command again, with no --resume.
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    ret = release_campaigns.campaign_command(**common_kwargs)

            self.assertEqual(ret, 0)
            self.assertEqual(len(invocations), 1)
            self.assertNotIn("Traceback", stderr.getvalue())

            self.assertEqual(len(release_campaigns.list_campaigns(campaigns_dir)), 1)
            second = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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
            first = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
            assert first is not None
            self.assertEqual(first["providers"][0]["state"], "running")

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                ret = release_campaigns.campaign_command(**common_kwargs)

            self.assertEqual(ret, 0)
            self.assertEqual(len(bodies), 1)
            self.assertNotIn("Traceback", stderr.getvalue())

            self.assertEqual(len(release_campaigns.list_campaigns(campaigns_dir)), 1)
            second = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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
                before = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)

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
            after = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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
            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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
            before = release_campaigns.load_campaign("campaign-v2.0.0", campaigns_dir)

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
            after = release_campaigns.load_campaign("campaign-v2.0.0", campaigns_dir)
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
            before = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)

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
            after = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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
        saved = release_campaigns.load_campaign("campaign-v2.0.0", campaigns_dir)
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
            polled = release_campaigns.load_campaign("campaign-v2.0.0", campaigns_dir)
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
            saved = release_campaigns.load_campaign("campaign-v2.0.0", campaigns_dir)
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
        created = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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

            first = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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

            second = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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
            saved = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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
            before = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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
            after = release_campaigns.load_campaign("campaign-v1.0.0", campaigns_dir)
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

if __name__ == "__main__":
    unittest.main()
