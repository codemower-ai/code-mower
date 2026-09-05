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
import time
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
        "executor": provider,
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
        "executor": provider,
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


def _fake_hosted_bridge_lane(
    *,
    bot_authors: tuple[str, ...] = (),
    response_timeout: int | None = None,
) -> ProviderLane:
    """A hosted_bridge lane with no registry defaults, for controlled trust tests."""
    return ProviderLane(
        lane_id="fake_hosted",
        lane_type="audit",
        driver="hosted_bridge",
        provider="devin",
        labels=LaneLabels(needs="needs-fake", done="fake-done", blocked="fake-blocked"),
        trigger_policy="manual",
        provider_config={
            **({"bot_authors": bot_authors} if bot_authors else {}),
            **(
                {"campaign_response_timeout_seconds": response_timeout}
                if response_timeout is not None
                else {}
            ),
        },
    )


class _ProcessInterrupted(BaseException):
    """Stands in for an abrupt process exit: SIGINT/SIGTERM, `kill`, or an OOM.

    Deliberately a `BaseException`, like `KeyboardInterrupt` and `SystemExit`:
    the whole point of a crash-window test is a failure that no ordinary
    `except Exception` handling converts into a recorded provider error. What
    the campaign persisted *before* the interruption is the entire record of
    the attempt.
    """


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
            # The aggregate headline follows the providers: "run with --apply"
            # is only honest while applying would actually dispatch something.
            if any(p["state"] == "queued" for p in saved["providers"]):
                self.assertEqual(saved["status"], "queued")
                self.assertIn("run with --apply", saved["next_action"])
            else:
                self.assertEqual(saved["status"], "unavailable")
                self.assertIn("configure prerequisites", saved["next_action"])

            # Providers are queued or unavailable
            for p in saved["providers"]:
                self.assertIn(p["state"], {"queued", "unavailable"})
                self.assertEqual(p["dispatch_mode"], "dry_run")
                self.assertIsNone(p["dispatched_at"])
                self.assertIsNone(p["completed_at"])

    def test_local_cli_without_adapter_cannot_complete(self) -> None:
        """A local_cli lane with no maintained adapter never fabricates a result.

        Uses aider, which ships no campaign adapter: codex/claude/antigravity/
        muse now carry maintained adapters and would invoke them here.
        """
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            adapter_mock = mock.MagicMock()

            result = release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["aider"],
                campaigns_dir=campaigns_dir,
                apply=True,
                which_fn=lambda _cmd: "/bin/aider",
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

    def test_explicit_local_retry_checkpoints_running_before_adapter_returns(self) -> None:
        """A retry replaces stale terminal state while its local adapter is active."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            fake_lane = _fake_local_cli_lane()
            campaign = release_campaigns.initialize_campaign(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["codex"],
            ).to_dict()
            provider = campaign["providers"][0]
            provider["state"] = "unavailable"
            provider["attempted_at"] = "2026-09-04T18:00:00Z"
            provider["error"] = "adapter_exited_nonzero"
            campaign["dry_run"] = False
            release_campaigns.save_campaign(campaign, campaigns_dir)

            def retry_adapter(argv, timeout):
                checkpoint = release_campaigns.load_campaign_by_id(
                    "campaign-v1.0.0", campaigns_dir
                )
                assert checkpoint is not None
                active = checkpoint["providers"][0]
                self.assertEqual(active["state"], "running")
                self.assertEqual(active["error"], "")
                self.assertIn("poll codex local process", active["next_action"])
                output_path = Path(argv[argv.index("--output") + 1])
                output_path.write_text(
                    json.dumps(_mock_adoption_result(provider="codex")),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            with mock.patch.object(
                release_campaigns, "resolve_provider_lane", return_value=("codex", fake_lane)
            ):
                result = release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    campaigns_dir=campaigns_dir,
                    apply=True,
                    retry_provider="codex",
                    which_fn=lambda _cmd: "/bin/fake-provider-cli",
                    adapter_runner=retry_adapter,
                )

            self.assertEqual(result, 0)
            stored = release_campaigns.load_campaign_by_id(
                "campaign-v1.0.0", campaigns_dir
            )
            assert stored is not None
            self.assertEqual(stored["providers"][0]["state"], "complete")

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

    def test_repo_config_can_enable_or_disable_maintained_adapter(self) -> None:
        """Bare YAML booleans control the maintained adapter without becoming strings."""
        _, lane = release_campaigns.resolve_provider_lane("muse")
        for enabled in (False, True):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as tmp:
                repo_path = Path(tmp)
                (repo_path / "code-mower.yml").write_text(
                    "version: 1\n"
                    "lanes:\n"
                    "  muse_cli:\n"
                    "    provider_config:\n"
                    f"      campaign_adapter_enabled: {str(enabled).lower()}\n",
                    encoding="utf-8",
                )

                overrides, error, detail = release_campaigns._load_campaign_adapter_overrides(
                    lane,
                    repo_path,
                )
                self.assertEqual(error, "")
                self.assertEqual(detail, "")
                self.assertIs(overrides["campaign_adapter_enabled"], enabled)

                argv_template, timeout_value, error, detail = (
                    release_campaigns._resolve_campaign_adapter_config(lane, repo_path)
                )
                self.assertEqual(error, "")
                self.assertEqual(detail, "")
                if enabled:
                    self.assertTrue(argv_template)
                    self.assertEqual(timeout_value, 900)
                else:
                    self.assertIsNone(argv_template)
                    self.assertIsNone(timeout_value)

    def test_repo_config_missing_is_no_override(self) -> None:
        """No code-mower.yml at repo_path means no override, not an error.

        Uses aider (no maintained adapter); muse would run its maintained
        default adapter here.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            campaigns_dir = repo_path / ".code-mower" / "campaigns"
            adapter_mock = mock.MagicMock()

            result = release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["aider"],
                repo_path=repo_path,
                campaigns_dir=campaigns_dir,
                apply=True,
                which_fn=lambda _cmd: "/bin/aider",
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

    def test_failed_retry_removes_stale_result_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            output_path = repo_path / "result.json"
            output_path.write_text(
                json.dumps(
                    _mock_adoption_result(
                        release_tag="v1.0.0", provider="codex", outcome="pass"
                    )
                ),
                encoding="utf-8",
            )
            lane = _fake_local_cli_lane()

            result, error, _detail = release_campaigns._invoke_local_adapter(
                lane,
                "codex",
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                qualification_context="cold_install",
                starting_version="",
                output_path=output_path,
                repo_path=repo_path,
                which_fn=lambda _cmd: "/bin/fake-provider-cli",
                adapter_runner=lambda argv, _timeout: subprocess.CompletedProcess(
                    argv, 1, stdout="", stderr=""
                ),
            )

            self.assertIsNone(result)
            self.assertEqual(error, "adapter_exited_nonzero")
            self.assertFalse(output_path.exists())

    def test_campaign_boundary_rejects_mismatched_executor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            output_path = repo_path / "result.json"
            lane = _fake_local_cli_lane()

            def mismatched_executor(argv, _timeout):
                result = _mock_adoption_result(
                    release_tag="v1.0.0", provider="codex", outcome="pass"
                )
                result["executor"] = "claude"
                output_path.write_text(json.dumps(result), encoding="utf-8")
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            result, error, _detail = release_campaigns._invoke_local_adapter(
                lane,
                "codex",
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                qualification_context="cold_install",
                starting_version="",
                output_path=output_path,
                repo_path=repo_path,
                which_fn=lambda _cmd: "/bin/fake-provider-cli",
                adapter_runner=mismatched_executor,
            )

            self.assertIsNone(result)
            self.assertEqual(error, "adapter_result_mismatch")

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

            # Cursor BugBot has trigger_comments, so 2 calls: dispatch + trigger
            self.assertEqual(len(dispatch_calls), 2)
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
            # antigravity now ships a maintained adapter but its CLI is missing
            # here, so it must fail closed pointing at the install, not claim
            # a result it never produced.
            self.assertEqual(providers_by_name["antigravity"]["state"], "unavailable")
            self.assertEqual(providers_by_name["antigravity"]["error"], "command_not_found")
            self.assertIn("install agy CLI", providers_by_name["antigravity"]["next_action"])

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
            self.assertTrue(proj_c["dry_run"])

            # The Board shows the stored aggregate verbatim, so it inherits the
            # honest verdict rather than a second, softer one of its own.
            stored = release_campaigns.load_campaign_by_id("campaign-v1.0.4", campaigns_dir)
            assert stored is not None
            self.assertEqual(proj_c["status"], stored["status"])
            self.assertEqual(proj_c["next_action"], stored["next_action"])

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
            self.assertEqual(
                bad_projection["next_action"],
                "run with --apply to dispatch providers",
            )
            self.assertIn("7 queued", bad_projection["next_detail"])
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

    def test_cursor_bugbot_accepts_known_trusted_bot_author(self) -> None:
        """Cursor BugBot's registry defaults trust cursor[bot]'s bound reply."""
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
            self.assertEqual(saved["providers"][0]["state"], "complete")

    def test_cursor_bugbot_rejects_spoofed_author(self) -> None:
        """A perfectly identity-bound marker from an untrusted commenter is ignored for Cursor BugBot too."""
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

    def test_cursor_bugbot_bot_authors_env_override_adds_trusted_login(self) -> None:
        """CURSOR_BUGBOT_BOT_AUTHORS extends (not replaces) Cursor BugBot's default trusted authors."""
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
                return {"comments": [{"author": {"login": "self-hosted-cursor-runner"}, "body": marker}]}, ""

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                resume=True,
                repo_slug="owner/repo",
                gh_json_runner=mock_gh_json,
                env={"CURSOR_BUGBOT_BOT_AUTHORS": "self-hosted-cursor-runner"},
            )

            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            self.assertEqual(saved["providers"][0]["state"], "complete")

    def test_devin_dispatch_includes_trigger_comments(self) -> None:
        """Devin dispatch body includes trigger_comments so the remote knows how to start."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            bodies: list[str] = []

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["devin"],
                campaigns_dir=campaigns_dir,
                repo_slug="owner/repo",
                issue="99",
                apply=True,
                command_runner=_capturing_dispatch_command_runner(bodies),
                env={"DEVIN_AUDIT_LABEL_TOKEN": "token"},
            )

            self.assertEqual(len(bodies), 2)
            dispatch_body = bodies[0]
            trigger_body = bodies[1]

            # Dispatch body should document the trigger commands
            self.assertIn("@devin run", dispatch_body)
            self.assertIn("devin run", dispatch_body)
            self.assertIn("**Trigger comments:**", dispatch_body)
            self.assertIn("`@devin run`, `devin run`", dispatch_body)

            # Trigger body should be just the trigger command itself
            self.assertEqual(trigger_body.splitlines()[0], "@devin run")
            self.assertIn("CODE_MOWER_RELEASE_TRIGGER", trigger_body)

    def test_cursor_bugbot_dispatch_posts_trigger_comment(self) -> None:
        """Cursor BugBot dispatch posts the trigger command as a separate actionable comment."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            bodies: list[str] = []

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["cursor_bugbot"],
                campaigns_dir=campaigns_dir,
                repo_slug="owner/repo",
                issue="99",
                apply=True,
                command_runner=_capturing_dispatch_command_runner(bodies),
                env={"CURSOR_BUGBOT_AUDIT_LABEL_TOKEN": "token"},
            )

            self.assertEqual(len(bodies), 2)
            dispatch_body = bodies[0]
            trigger_body = bodies[1]

            # Dispatch body should document the trigger commands
            self.assertIn("bugbot run", dispatch_body)
            self.assertIn("@cursor review", dispatch_body)

            # The actionable command stays first; the hidden marker makes a
            # crash-after-post retry externally idempotent.
            self.assertEqual(trigger_body.splitlines()[0], "bugbot run")
            self.assertIn("CODE_MOWER_RELEASE_TRIGGER", trigger_body)

    def test_failed_trigger_post_retries_on_resume(self) -> None:
        """Failed trigger posts are persisted and retried on resume without redispatching."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            bodies: list[str] = []
            call_count = {"dispatch": 0, "trigger": 0}

            def failing_then_succeeding_runner(args, **kwargs):
                """First trigger post fails, second succeeds."""
                argv = list(args)
                body_path = Path(argv[argv.index("--body-file") + 1])
                body = body_path.read_text(encoding="utf-8")
                bodies.append(body)

                # Detect if this is dispatch or trigger based on body content
                if "CODE_MOWER_RELEASE_CAMPAIGN" in body:
                    call_count["dispatch"] += 1
                    is_trigger = False
                else:
                    call_count["trigger"] += 1
                    is_trigger = True

                class MockCompleted:
                    pass

                completed = MockCompleted()
                # First trigger fails, second succeeds
                completed.returncode = 1 if (is_trigger and call_count["trigger"] == 1) else 0
                completed.stdout = ""
                completed.stderr = ""
                return completed

            def mock_gh_json(args, **kwargs):
                return {"comments": []}, ""

            # Initial dispatch with trigger failure
            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["cursor_bugbot"],
                campaigns_dir=campaigns_dir,
                repo_slug="owner/repo",
                issue="42",
                apply=True,
                command_runner=failing_then_succeeding_runner,
                gh_json_runner=mock_gh_json,
                env={"CURSOR_BUGBOT_AUDIT_LABEL_TOKEN": "token"},
            )

            # Should have dispatch comment + failed trigger attempt
            self.assertEqual(len(bodies), 2)
            self.assertEqual(call_count["dispatch"], 1)
            self.assertEqual(call_count["trigger"], 1)

            campaign = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert campaign is not None
            self.assertEqual(campaign["providers"][0]["state"], "running")
            self.assertEqual(campaign["providers"][0]["trigger_posted"], False)
            self.assertIn("trigger comment may not have posted", campaign["providers"][0]["next_detail"])

            # Resume should retry trigger without redispatching
            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                resume=True,
                apply=True,
                gh_json_runner=mock_gh_json,
                command_runner=failing_then_succeeding_runner,
                env={"CURSOR_BUGBOT_AUDIT_LABEL_TOKEN": "token"},
            )

            # Should have 1 more trigger attempt, no new dispatch
            self.assertEqual(len(bodies), 3)
            self.assertEqual(call_count["dispatch"], 1)  # Still 1
            self.assertEqual(call_count["trigger"], 2)  # Now 2

            retried = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert retried is not None
            self.assertEqual(retried["providers"][0]["trigger_posted"], True)
            self.assertIn("poll cursor_bugbot remote progress marker", retried["providers"][0]["next_action"])

    def test_crash_after_dispatch_before_trigger_is_retriable(self) -> None:
        """Simulates process crash after dispatch but before trigger is recorded."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"

            # Create campaign that simulates a crash after dispatch checkpoint
            # but before trigger post completes
            campaign = release_campaigns.initialize_campaign(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["cursor_bugbot"],
                repo_slug="owner/repo",
            )
            campaign.status = "running"
            campaign.providers[0]["state"] = "running"
            campaign.providers[0]["attempted_at"] = "2024-01-01T00:00:00Z"
            campaign.providers[0]["dispatched_at"] = "2024-01-01T00:00:00Z"
            campaign.providers[0]["trigger_posted"] = False  # Crash before trigger recorded
            campaign.providers[0]["dispatch_ref"] = {"issue_number": "42", "comment_posted": True}
            release_campaigns.save_campaign(campaign, campaigns_dir)

            bodies: list[str] = []

            def mock_gh_json(args, **kwargs):
                return {"comments": []}, ""

            # Resume should retry the trigger
            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                resume=True,
                apply=True,
                command_runner=_capturing_dispatch_command_runner(bodies),
                gh_json_runner=mock_gh_json,
                env={"CURSOR_BUGBOT_AUDIT_LABEL_TOKEN": "token"},
            )

            # Should have posted exactly 1 trigger (no redispatch)
            self.assertEqual(len(bodies), 1)
            self.assertIn("bugbot run", bodies[0])
            self.assertNotIn("CODE_MOWER_RELEASE_CAMPAIGN", bodies[0])

            resumed = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert resumed is not None
            self.assertEqual(resumed["providers"][0]["trigger_posted"], True)

    def test_resume_withholds_trigger_until_dispatch_is_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            campaign = release_campaigns.initialize_campaign(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["cursor_bugbot"],
                repo_slug="owner/repo",
            )
            provider = campaign.providers[0]
            campaign.status = "running"
            provider["state"] = "running"
            provider["attempted_at"] = "2024-01-01T00:00:00Z"
            provider["trigger_posted"] = False
            provider["dispatch_reconciliation_key"] = "dispatch-key"
            provider["trigger_reconciliation_key"] = "trigger-key"
            provider["dispatch_ref"] = {"issue_number": "42", "comment_posted": False}
            release_campaigns.save_campaign(campaign, campaigns_dir)
            bodies: list[str] = []
            forged_marker = json.dumps(
                {
                    "schema": release_campaigns.DISPATCH_SCHEMA,
                    "campaign_id": campaign.campaign_id,
                    "provider": "cursor_bugbot",
                    "idempotency_key": provider["idempotency_key"],
                },
                sort_keys=True,
            )
            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                resume=True,
                command_runner=_capturing_dispatch_command_runner(bodies),
                gh_json_runner=lambda args, **kwargs: (
                    {
                        "comments": [
                            {
                                "author": {"login": "untrusted-user"},
                                "body": (
                                    "<!-- CODE_MOWER_RELEASE_CAMPAIGN: "
                                    f"{forged_marker} -->"
                                ),
                            }
                        ]
                    },
                    "",
                ),
                env={"CURSOR_BUGBOT_AUDIT_LABEL_TOKEN": "token"},
            )

            self.assertEqual(bodies, [])
            resumed = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert resumed is not None
            self.assertFalse(resumed["providers"][0]["trigger_posted"])
            self.assertIn("retry the dispatch", resumed["providers"][0]["next_action"])

    def test_poll_failure_never_recommends_uncertain_redispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            campaign = release_campaigns.initialize_campaign(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["devin"],
                repo_slug="owner/repo",
            )
            provider = campaign.providers[0]
            campaign.status = "running"
            provider["state"] = "running"
            provider["attempted_at"] = "2024-01-01T00:00:00Z"
            provider["trigger_posted"] = False
            provider["dispatch_reconciliation_key"] = "dispatch-key"
            provider["trigger_reconciliation_key"] = "trigger-key"
            provider["dispatch_ref"] = {"issue_number": "42", "comment_posted": False}
            release_campaigns.save_campaign(campaign, campaigns_dir)
            bodies: list[str] = []

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                resume=True,
                apply=True,
                command_runner=_capturing_dispatch_command_runner(bodies),
                gh_json_runner=lambda args, **kwargs: (None, "GitHub unavailable"),
                env={"DEVIN_AUDIT_LABEL_TOKEN": "token"},
            )

            self.assertEqual(bodies, [])
            resumed = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert resumed is not None
            action = resumed["providers"][0]["next_action"]
            self.assertIn("reconciliation", action)
            self.assertNotIn("retry the dispatch", action)

    def test_resume_reconciles_posted_trigger_marker_without_reposting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            campaign = release_campaigns.initialize_campaign(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["cursor_bugbot"],
                repo_slug="owner/repo",
            )
            provider = campaign.providers[0]
            campaign.status = "running"
            provider["state"] = "running"
            provider["attempted_at"] = "2024-01-01T00:00:00Z"
            provider["trigger_posted"] = False
            provider["trigger_reconciliation_key"] = "trigger-key"
            provider["dispatch_ref"] = {"issue_number": "42", "comment_posted": True}
            release_campaigns.save_campaign(campaign, campaigns_dir)
            trigger_marker = json.dumps(
                {
                    "schema": release_campaigns.TRIGGER_MARKER_SCHEMA,
                    "campaign_id": "campaign-v1.0.0",
                    "provider": "cursor_bugbot",
                    "reconciliation_key": "trigger-key",
                },
                sort_keys=True,
            )
            bodies: list[str] = []

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                resume=True,
                command_runner=_capturing_dispatch_command_runner(bodies),
                gh_json_runner=lambda args, **kwargs: (
                    {
                        "comments": [
                            {
                                "author": {"login": "cursor[bot]"},
                                "body": "bugbot run\n\n"
                                f"<!-- CODE_MOWER_RELEASE_TRIGGER: {trigger_marker} -->",
                            }
                        ]
                    },
                    "",
                ),
                env={"CURSOR_BUGBOT_AUDIT_LABEL_TOKEN": "token"},
            )

            self.assertEqual(bodies, [])
            resumed = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert resumed is not None
            self.assertTrue(resumed["providers"][0]["trigger_posted"])

    def test_dispatch_nonce_cannot_forge_trigger_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            campaign = release_campaigns.initialize_campaign(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["cursor_bugbot"],
                repo_slug="owner/repo",
            )
            provider = campaign.providers[0]
            campaign.status = "running"
            provider["state"] = "running"
            provider["attempted_at"] = "2024-01-01T00:00:00Z"
            provider["trigger_posted"] = False
            provider["dispatch_reconciliation_key"] = "public-dispatch-key"
            provider["trigger_reconciliation_key"] = "private-trigger-key"
            provider["dispatch_ref"] = {"issue_number": "42", "comment_posted": True}
            release_campaigns.save_campaign(campaign, campaigns_dir)
            forged_marker = json.dumps(
                {
                    "schema": release_campaigns.TRIGGER_MARKER_SCHEMA,
                    "campaign_id": campaign.campaign_id,
                    "provider": "cursor_bugbot",
                    "reconciliation_key": "public-dispatch-key",
                },
                sort_keys=True,
            )
            bodies: list[str] = []

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                resume=True,
                apply=True,
                command_runner=_capturing_dispatch_command_runner(bodies),
                gh_json_runner=lambda args, **kwargs: (
                    {
                        "comments": [
                            {
                                "body": "bugbot run\n\n"
                                f"<!-- CODE_MOWER_RELEASE_TRIGGER: {forged_marker} -->"
                            }
                        ]
                    },
                    "",
                ),
                env={"CURSOR_BUGBOT_AUDIT_LABEL_TOKEN": "token"},
            )

            self.assertEqual(len(bodies), 1)
            self.assertIn("bugbot run", bodies[0])
            self.assertIn("private-trigger-key", bodies[0])
            self.assertNotIn("public-dispatch-key", bodies[0])
            resumed = release_campaigns.load_campaign_by_id(
                "campaign-v1.0.0", campaigns_dir
            )
            assert resumed is not None
            self.assertTrue(resumed["providers"][0]["trigger_posted"])

    def test_completed_result_is_consumed_before_trigger_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            campaign = release_campaigns.initialize_campaign(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["cursor_bugbot"],
                repo_slug="owner/repo",
            )
            provider = campaign.providers[0]
            campaign.status = "running"
            provider["state"] = "running"
            provider["attempted_at"] = "2024-01-01T00:00:00Z"
            provider["trigger_posted"] = False
            provider["trigger_reconciliation_key"] = "trigger-key"
            provider["dispatch_ref"] = {"issue_number": "42", "comment_posted": True}
            release_campaigns.save_campaign(campaign, campaigns_dir)
            wrapper = {
                "schema": release_campaigns.RESULT_MARKER_SCHEMA,
                "campaign_id": campaign.campaign_id,
                "provider": "cursor_bugbot",
                "release_tag": "v1.0.0",
                "idempotency_key": provider["idempotency_key"],
                "adoption_result": _mock_adoption_result(
                    release_tag="v1.0.0",
                    provider="cursor_bugbot",
                    outcome="pass",
                ),
            }
            result_marker = f"<!-- CODE_MOWER_ADOPTION_RESULT: {json.dumps(wrapper)} -->"
            bodies: list[str] = []

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                resume=True,
                apply=True,
                command_runner=_capturing_dispatch_command_runner(bodies),
                gh_json_runner=lambda args, **kwargs: (
                    {
                        "comments": [
                            {"author": {"login": "cursor[bot]"}, "body": result_marker}
                        ]
                    },
                    "",
                ),
                env={"CURSOR_BUGBOT_AUDIT_LABEL_TOKEN": "token"},
            )

            self.assertEqual(bodies, [])
            resumed = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert resumed is not None
            self.assertEqual(resumed["providers"][0]["state"], "complete")

    def test_resume_without_apply_never_posts_missing_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            campaign = release_campaigns.initialize_campaign(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["cursor_bugbot"],
                repo_slug="owner/repo",
            )
            provider = campaign.providers[0]
            campaign.status = "running"
            provider["state"] = "running"
            provider["attempted_at"] = "2024-01-01T00:00:00Z"
            provider["trigger_posted"] = False
            provider["trigger_reconciliation_key"] = "trigger-key"
            provider["dispatch_ref"] = {"issue_number": "42", "comment_posted": True}
            release_campaigns.save_campaign(campaign, campaigns_dir)
            bodies: list[str] = []

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                resume=True,
                command_runner=_capturing_dispatch_command_runner(bodies),
                gh_json_runner=lambda args, **kwargs: ({"comments": []}, ""),
                env={"CURSOR_BUGBOT_AUDIT_LABEL_TOKEN": "token"},
            )

            self.assertEqual(bodies, [])
            resumed = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert resumed is not None
            self.assertFalse(resumed["providers"][0]["trigger_posted"])
            self.assertIn("--resume --apply", resumed["providers"][0]["next_action"])

    def test_resume_treats_legacy_missing_trigger_field_as_unposted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            campaign = release_campaigns.initialize_campaign(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["cursor_bugbot"],
                repo_slug="owner/repo",
            )
            provider = campaign.providers[0]
            campaign.status = "running"
            provider["state"] = "running"
            provider["attempted_at"] = "2024-01-01T00:00:00Z"
            provider.pop("trigger_posted", None)
            provider["dispatch_ref"] = {"issue_number": "42", "comment_posted": True}
            release_campaigns.save_campaign(campaign, campaigns_dir)
            bodies: list[str] = []

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                resume=True,
                command_runner=_capturing_dispatch_command_runner(bodies),
                gh_json_runner=lambda args, **kwargs: ({"comments": []}, ""),
                env={"CURSOR_BUGBOT_AUDIT_LABEL_TOKEN": "token"},
            )

            self.assertEqual(bodies, [])
            resumed = release_campaigns.load_campaign_by_id(
                "campaign-v1.0.0", campaigns_dir
            )
            assert resumed is not None
            self.assertFalse(resumed["providers"][0]["trigger_posted"])
            self.assertIn("--resume --apply", resumed["providers"][0]["next_action"])

    def test_explicit_retry_does_not_duplicate_trigger(self) -> None:
        """Explicit --retry-provider should not post trigger twice in one run."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"

            # Create campaign with failed trigger
            campaign = release_campaigns.initialize_campaign(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["cursor_bugbot"],
                repo_slug="owner/repo",
            )
            campaign.status = "running"
            campaign.providers[0]["state"] = "running"
            campaign.providers[0]["attempted_at"] = "2024-01-01T00:00:00Z"
            campaign.providers[0]["dispatched_at"] = "2024-01-01T00:00:00Z"
            campaign.providers[0]["trigger_posted"] = False
            campaign.providers[0]["dispatch_ref"] = {"issue_number": "42", "comment_posted": True}
            release_campaigns.save_campaign(campaign, campaigns_dir)

            bodies: list[str] = []
            call_count = {"dispatch": 0, "trigger": 0}

            def counting_runner(args, **kwargs):
                argv = list(args)
                body_path = Path(argv[argv.index("--body-file") + 1])
                body = body_path.read_text(encoding="utf-8")
                bodies.append(body)

                if "CODE_MOWER_RELEASE_CAMPAIGN" in body:
                    call_count["dispatch"] += 1
                else:
                    call_count["trigger"] += 1

                class MockCompleted:
                    pass
                completed = MockCompleted()
                completed.returncode = 0
                completed.stdout = ""
                completed.stderr = ""
                return completed

            def mock_gh_json(args, **kwargs):
                return {"comments": []}, ""

            # Explicit retry with apply
            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                retry_provider="cursor_bugbot",
                apply=True,
                repo_slug="owner/repo",
                issue="42",
                command_runner=counting_runner,
                gh_json_runner=mock_gh_json,
                env={"CURSOR_BUGBOT_AUDIT_LABEL_TOKEN": "token"},
            )

            # Should have 1 dispatch + 1 trigger (not 2 triggers)
            self.assertEqual(call_count["dispatch"], 1)
            self.assertEqual(call_count["trigger"], 1)
            self.assertEqual(len(bodies), 2)


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
            # Cursor BugBot has trigger_comments, so 2 bodies: dispatch + trigger
            self.assertEqual(len(bodies), 2)
            first = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert first is not None
            self.assertEqual(first["providers"][0]["state"], "running")

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                ret = release_campaigns.campaign_command(**common_kwargs)

            self.assertEqual(ret, 0)
            # No new bodies during idempotent redispatch, still 2 total
            self.assertEqual(len(bodies), 2)
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
            # Cursor BugBot has trigger_comments, so 2 bodies: dispatch + trigger
            self.assertEqual(len(bodies), 2)

            release_campaigns.campaign_command(**dispatch_kwargs)
            # No new bodies during idempotent redispatch, still 2 total
            self.assertEqual(len(bodies), 2)

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

            # Cursor BugBot has trigger_comments, so 2 bodies: dispatch + trigger
            self.assertEqual(len(bodies), 2)
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

            # Cursor BugBot has trigger_comments, so 2 bodies: dispatch + trigger
            self.assertEqual(len(bodies), 2)
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


class HostedDryRunIssuePrerequisiteTests(unittest.TestCase):
    """A hosted dry-run preview must judge the --issue prerequisite exactly as --apply does.

    Hosted dispatch is a comment on a specific GitHub issue, so a hosted
    provider with valid credentials but no issue number can never be
    dispatched. The preview used to call that provider queued and tell the
    operator to rerun with --apply; the applied run then immediately marked it
    unavailable. Both spellings now report the same unavailable state, the same
    bounded ``missing_issue_number`` error, and the same actionable next step.
    """

    def _preview(
        self,
        campaigns_dir: Path,
        *,
        provider: str,
        token_env: str,
        issue: str = "",
        repo_slug: str = "owner/repo",
        resume: bool = False,
    ) -> tuple[dict[str, Any], mock.MagicMock, mock.MagicMock, mock.MagicMock]:
        """Run one no-apply campaign command and return its saved state plus the runners."""
        command_runner = mock.MagicMock()
        gh_json_runner = mock.MagicMock()
        adapter_runner = mock.MagicMock()
        transport_vars = {
            "devin": "CODE_MOWER_DEVIN_CAMPAIGN_TRANSPORT_READY",
            "cursor_bugbot": "CODE_MOWER_CURSOR_BUGBOT_CAMPAIGN_TRANSPORT_READY",
        }
        ret = release_campaigns.campaign_command(
            release_tag="v1.0.0",
            package_spec="code-mower==1.0.0",
            providers=() if resume else (provider,),
            campaigns_dir=campaigns_dir,
            repo_slug=repo_slug,
            issue=issue,
            apply=False,
            resume=resume,
            command_runner=command_runner,
            gh_json_runner=gh_json_runner,
            adapter_runner=adapter_runner,
            env={token_env: "token", transport_vars[provider]: "1"},
        )
        self.assertEqual(ret, 0)
        saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
        assert saved is not None
        return saved, command_runner, gh_json_runner, adapter_runner

    def _assert_no_dispatch(self, provider_data: dict[str, Any], *runners: mock.MagicMock) -> None:
        """No network call, no adapter invocation, and no dispatch bookkeeping."""
        for runner in runners:
            runner.assert_not_called()
        self.assertEqual(provider_data["dispatch_mode"], "dry_run")
        self.assertIsNone(provider_data["attempted_at"])
        self.assertIsNone(provider_data["dispatched_at"])
        self.assertIsNone(provider_data["completed_at"])
        self.assertEqual(provider_data["dispatch_ref"], {})
        self.assertIsNone(provider_data["adoption_result"])

    def test_hosted_dry_run_without_issue_is_unavailable(self) -> None:
        """Credentials alone are not readiness: the preview names the missing --issue."""
        for provider, token_env in (
            ("devin", "DEVIN_AUDIT_LABEL_TOKEN"),
            ("cursor_bugbot", "CURSOR_BUGBOT_AUDIT_LABEL_TOKEN"),
        ):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as tmp:
                campaigns_dir = Path(tmp) / "campaigns"
                saved, command_runner, gh_json_runner, adapter_runner = self._preview(
                    campaigns_dir, provider=provider, token_env=token_env
                )

                entry = saved["providers"][0]
                self.assertEqual(entry["provider"], provider)
                self.assertEqual(entry["state"], "unavailable")
                self.assertEqual(entry["error"], "missing_issue_number")
                self.assertIn(entry["error"], release_campaigns.SAFE_ERROR_CODES)
                self.assertEqual(
                    entry["next_action"],
                    f"provide GitHub issue number via --issue for {provider} dispatch",
                )
                self.assertEqual(entry["next_detail"], "missing issue number")
                # Nothing about the preview claims the provider is dispatchable.
                self.assertNotIn("--apply", entry["next_action"])
                self.assertNotIn("--apply", entry["next_detail"])
                self.assertTrue(saved["dry_run"])
                # The single provider cannot be dispatched, so neither can the
                # campaign: the aggregate says so instead of "run with --apply".
                self.assertEqual(saved["status"], "unavailable")
                self.assertEqual(
                    saved["next_action"],
                    f"configure prerequisites for unavailable providers: {provider}",
                )
                self.assertEqual(saved["next_detail"], "1 provider(s) unavailable")
                self.assertNotIn("--apply", saved["next_action"])
                self._assert_no_dispatch(entry, command_runner, gh_json_runner, adapter_runner)

    def test_hosted_dry_run_with_issue_is_queued(self) -> None:
        """With credentials and an issue the preview is still a preview -- queued, not dispatched."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            saved, command_runner, gh_json_runner, adapter_runner = self._preview(
                campaigns_dir, provider="devin", token_env="DEVIN_AUDIT_LABEL_TOKEN", issue="42"
            )

            entry = saved["providers"][0]
            self.assertEqual(entry["state"], "queued")
            self.assertEqual(entry["error"], "")
            self.assertIn("--apply", entry["next_action"])
            self.assertTrue(saved["dry_run"])
            self.assertEqual(saved["status"], "queued")
            self.assertEqual(saved["next_action"], "run with --apply to dispatch providers")
            self.assertEqual(
                saved["next_detail"],
                "dry-run preview with 1 queued and 0 unavailable provider(s)",
            )
            self._assert_no_dispatch(entry, command_runner, gh_json_runner, adapter_runner)

    def test_hosted_transport_is_reported_unverified_without_blocking_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            ret = release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=("devin",),
                campaigns_dir=campaigns_dir,
                repo_slug="owner/repo",
                issue="42",
                env={"DEVIN_AUDIT_LABEL_TOKEN": "token"},
            )

            self.assertEqual(ret, 0)
            saved = release_campaigns.load_campaign_by_id(
                "campaign-v1.0.0", campaigns_dir
            )
            assert saved is not None
            entry = saved["providers"][0]
            self.assertEqual(entry["state"], "queued")
            self.assertFalse(entry["transport_verified"])
            self.assertIn(
                "CODE_MOWER_DEVIN_CAMPAIGN_TRANSPORT_READY=1",
                entry["next_detail"],
            )

    def test_hosted_dry_run_without_repo_slug_is_unavailable(self) -> None:
        """An issue number with no repo slug addresses nothing, exactly as under --apply."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            saved, command_runner, gh_json_runner, adapter_runner = self._preview(
                campaigns_dir,
                provider="devin",
                token_env="DEVIN_AUDIT_LABEL_TOKEN",
                issue="42",
                repo_slug="",
            )

            entry = saved["providers"][0]
            self.assertEqual(entry["state"], "unavailable")
            self.assertEqual(entry["error"], "missing_issue_number")
            self.assertIn("--issue", entry["next_action"])
            self._assert_no_dispatch(entry, command_runner, gh_json_runner, adapter_runner)

    def test_missing_credentials_still_reported_before_the_issue_check(self) -> None:
        """The credential check keeps precedence: no token is still missing_credentials."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            command_runner = mock.MagicMock()
            gh_json_runner = mock.MagicMock()
            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["devin"],
                campaigns_dir=campaigns_dir,
                repo_slug="owner/repo",
                apply=False,
                command_runner=command_runner,
                gh_json_runner=gh_json_runner,
                env={},
            )
            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            entry = saved["providers"][0]
            self.assertEqual(entry["state"], "unavailable")
            self.assertIn("DEVIN_AUDIT_LABEL_TOKEN", entry["next_action"])
            self._assert_no_dispatch(entry, command_runner, gh_json_runner)

    def test_dry_run_preview_matches_the_applied_outcome(self) -> None:
        """The preview's verdict is the verdict --apply reaches, minus the mutation."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            preview, command_runner, gh_json_runner, _ = self._preview(
                campaigns_dir, provider="devin", token_env="DEVIN_AUDIT_LABEL_TOKEN"
            )
            preview_entry = preview["providers"][0]

            applied_runner = mock.MagicMock()
            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                resume=True,
                repo_slug="owner/repo",
                apply=True,
                command_runner=applied_runner,
                env={"DEVIN_AUDIT_LABEL_TOKEN": "token"},
            )
            applied_runner.assert_not_called()
            applied = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert applied is not None
            applied_entry = applied["providers"][0]

            for field_name in ("state", "error", "next_action", "next_detail"):
                self.assertEqual(preview_entry[field_name], applied_entry[field_name])
            # The applied run refused before claiming an attempt, so a later
            # run with --issue is still free to dispatch.
            self.assertIsNone(applied_entry["attempted_at"])
            self.assertEqual(command_runner.call_count, 0)
            self.assertEqual(gh_json_runner.call_count, 0)

    def test_dry_run_resume_reevaluates_the_issue_prerequisite(self) -> None:
        """Resume without --apply shares the branch: unavailable without --issue, queued with it."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._preview(
                campaigns_dir, provider="devin", token_env="DEVIN_AUDIT_LABEL_TOKEN", issue="42"
            )

            resumed, command_runner, gh_json_runner, adapter_runner = self._preview(
                campaigns_dir,
                provider="devin",
                token_env="DEVIN_AUDIT_LABEL_TOKEN",
                resume=True,
            )
            entry = resumed["providers"][0]
            self.assertEqual(entry["state"], "unavailable")
            self.assertEqual(entry["error"], "missing_issue_number")
            self.assertIn("--issue", entry["next_action"])
            self._assert_no_dispatch(entry, command_runner, gh_json_runner, adapter_runner)

            requeued, command_runner, gh_json_runner, adapter_runner = self._preview(
                campaigns_dir,
                provider="devin",
                token_env="DEVIN_AUDIT_LABEL_TOKEN",
                issue="42",
                resume=True,
            )
            requeued_entry = requeued["providers"][0]
            self.assertEqual(requeued_entry["state"], "queued")
            self.assertEqual(requeued_entry["error"], "")
            self._assert_no_dispatch(requeued_entry, command_runner, gh_json_runner, adapter_runner)


class CampaignAggregateStatusHonestyTests(unittest.TestCase):
    """The aggregate headline only promises a dispatch that --apply could make.

    "queued / run with --apply to dispatch providers" is a claim that applying
    would dispatch something. A dry run used to print it even when every
    provider had failed a prerequisite check, so the operator was sent to a
    command that could dispatch nothing. The aggregate now reports
    ``unavailable`` and the prerequisite work whenever no provider is
    dispatchable -- for a missing issue number, a missing repo slug, missing
    credentials or an unconfigured adapter alike, because each of those already
    lands its provider in ``unavailable``.
    """

    @staticmethod
    def _providers(*states: str) -> list[dict[str, Any]]:
        return [
            {"provider": f"p{index}", "state": state} for index, state in enumerate(states, start=1)
        ]

    def test_all_unavailable_is_unavailable_in_a_dry_run(self) -> None:
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                status, action, detail = release_campaigns._aggregate_campaign_status(
                    self._providers("unavailable", "unavailable"), dry_run=dry_run
                )
                self.assertEqual(status, "unavailable")
                self.assertEqual(
                    action, "configure prerequisites for unavailable providers: p1, p2"
                )
                self.assertEqual(detail, "2 provider(s) unavailable")
                self.assertNotIn("--apply", action)

    def test_a_dry_run_with_one_queued_provider_stays_queued(self) -> None:
        """Mixed previews are still dispatchable, so they keep the --apply headline."""
        status, action, detail = release_campaigns._aggregate_campaign_status(
            self._providers("queued", "unavailable"), dry_run=True
        )
        self.assertEqual(status, "queued")
        self.assertEqual(action, "run with --apply to dispatch providers")
        self.assertEqual(detail, "dry-run preview with 1 queued and 1 unavailable provider(s)")

    def test_an_applied_mixed_campaign_names_the_queued_providers(self) -> None:
        status, action, detail = release_campaigns._aggregate_campaign_status(
            self._providers("queued", "unavailable"), dry_run=False
        )
        self.assertEqual(status, "queued")
        self.assertEqual(action, "dispatch queued providers: p1")
        self.assertEqual(detail, "1 provider(s) waiting for dispatch")

    def test_completed_providers_do_not_mask_an_undispatchable_remainder(self) -> None:
        status, action, _ = release_campaigns._aggregate_campaign_status(
            self._providers("complete", "unavailable"), dry_run=True
        )
        self.assertEqual(status, "unavailable")
        self.assertEqual(action, "configure prerequisites for unavailable providers: p2")

    def test_all_complete_is_complete_even_in_a_dry_run(self) -> None:
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                status, action, detail = release_campaigns._aggregate_campaign_status(
                    self._providers("complete", "complete"), dry_run=dry_run
                )
                self.assertEqual(status, "complete")
                self.assertEqual(action, "campaign complete; all providers passed")
                self.assertEqual(detail, "all 2 provider(s) qualified successfully")

    def test_blocked_and_running_keep_precedence_in_a_dry_run(self) -> None:
        blocked_status, blocked_action, _ = release_campaigns._aggregate_campaign_status(
            self._providers("blocked", "unavailable"), dry_run=True
        )
        self.assertEqual(blocked_status, "blocked")
        self.assertIn("p1", blocked_action)

        running_status, running_action, _ = release_campaigns._aggregate_campaign_status(
            self._providers("running", "unavailable"), dry_run=True
        )
        self.assertEqual(running_status, "running")
        self.assertIn("p1", running_action)

    def test_an_empty_campaign_asks_for_providers(self) -> None:
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                self.assertEqual(
                    release_campaigns._aggregate_campaign_status([], dry_run=dry_run),
                    ("queued", "add providers to campaign", ""),
                )

    def _preview_status(self, campaigns_dir: Path, **kwargs: Any) -> dict[str, Any]:
        """Run one no-apply campaign command with mocked runners and return its state."""
        no_dispatch = mock.MagicMock()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            ret = release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                campaigns_dir=campaigns_dir,
                apply=False,
                command_runner=no_dispatch,
                gh_json_runner=no_dispatch,
                adapter_runner=no_dispatch,
                **kwargs,
            )
        self.assertEqual(ret, 0)
        no_dispatch.assert_not_called()
        saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
        assert saved is not None
        return saved

    def test_every_dry_run_prerequisite_failure_reaches_the_aggregate(self) -> None:
        """One rule, no special cases: missing issue, slug, credentials or adapter."""
        scenarios = (
            (
                "missing_issue",
                "devin",
                dict(
                    providers=["devin"],
                    repo_slug="owner/repo",
                    env={"DEVIN_AUDIT_LABEL_TOKEN": "token"},
                ),
            ),
            (
                "missing_repo_slug",
                "devin",
                dict(
                    providers=["devin"],
                    repo_slug="",
                    issue="42",
                    env={"DEVIN_AUDIT_LABEL_TOKEN": "token"},
                ),
            ),
            (
                "missing_credentials",
                "devin",
                dict(providers=["devin"], repo_slug="owner/repo", issue="42", env={}),
            ),
            (
                # aider ships no maintained campaign adapter, so a present CLI
                # still previews as unavailable; muse/codex/claude/antigravity
                # now carry maintained adapters and would preview as queued.
                "no_adapter_configured",
                "aider",
                dict(
                    providers=["aider"],
                    which_fn=lambda cmd: "/bin/aider" if cmd == "aider" else None,
                    env={},
                ),
            ),
        )
        for label, provider, kwargs in scenarios:
            with self.subTest(scenario=label), tempfile.TemporaryDirectory() as tmp:
                campaigns_dir = Path(tmp) / "campaigns"
                saved = self._preview_status(
                    campaigns_dir, repo_path=Path(tmp), **kwargs
                )
                self.assertTrue(saved["dry_run"])
                self.assertEqual(saved["providers"][0]["state"], "unavailable")
                self.assertEqual(saved["status"], "unavailable")
                self.assertEqual(
                    saved["next_action"],
                    f"configure prerequisites for unavailable providers: {provider}",
                )
                self.assertEqual(saved["next_detail"], "1 provider(s) unavailable")
                rendered = release_campaigns.render_campaign_text(saved)
                self.assertIn("Status: unavailable (dry-run)", rendered)
                self.assertNotIn("run with --apply to dispatch providers", rendered)

    def test_a_mixed_preview_still_advertises_apply(self) -> None:
        """One dispatchable provider is enough to make --apply the right advice.

        Uses aider for the unavailable leg: it ships no maintained adapter,
        while muse now does and would preview as queued here.
        """
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            saved = self._preview_status(
                campaigns_dir,
                repo_path=Path(tmp),
                providers=["devin", "aider"],
                repo_slug="owner/repo",
                issue="42",
                which_fn=lambda cmd: "/bin/aider" if cmd == "aider" else None,
                env={"DEVIN_AUDIT_LABEL_TOKEN": "token"},
            )
            states = {p["provider"]: p["state"] for p in saved["providers"]}
            self.assertEqual(states, {"devin": "queued", "aider": "unavailable"})
            self.assertEqual(saved["status"], "queued")
            self.assertEqual(saved["next_action"], "run with --apply to dispatch providers")
            self.assertEqual(
                saved["next_detail"],
                "dry-run preview with 1 queued and 1 unavailable provider(s)",
            )

    def test_the_board_shows_the_corrected_aggregate(self) -> None:
        """The Board reprints the stored verdict, so it cannot soften it.

        Uses aider (no maintained adapter) so the stored verdict is
        unavailable; muse would preview as queued with its maintained adapter.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            campaigns_dir = repo_path / ".code-mower" / "campaigns"
            saved = self._preview_status(
                campaigns_dir,
                repo_path=repo_path,
                providers=["aider"],
                which_fn=lambda cmd: "/bin/aider" if cmd == "aider" else None,
                env={},
            )
            self.assertEqual(saved["status"], "unavailable")

            payload = release_campaigns.release_campaigns_board_payload(
                campaigns_dir=campaigns_dir
            )
            projected = payload["campaigns"][0]
            self.assertTrue(projected["dry_run"])
            self.assertEqual(projected["status"], "unavailable")
            self.assertEqual(
                projected["next_action"],
                "configure prerequisites for unavailable providers: aider",
            )
            self.assertEqual(payload["next_action"], projected["next_action"])


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

            # Cursor BugBot has trigger_comments, so 2 calls: dispatch + trigger
            self.assertEqual(len(calls), 2)
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
            # No new calls during idempotent redispatch, still 2 total
            self.assertEqual(len(calls), 2)
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
            # Cursor BugBot has trigger_comments, so 2 calls: dispatch + trigger
            self.assertEqual(len(calls), 2)
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
            # Cursor BugBot has trigger_comments, so 2 calls: dispatch + trigger
            self.assertEqual(len(calls), 2)
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


class ContradictoryCampaignIntentTests(unittest.TestCase):
    """One invocation states one campaign action, or it is refused outright.

    `create` starts a campaign; `--resume` advances an existing one. Asked for
    together they contradict each other, and there is no honest way to run
    either: whichever one the command body happened to test first would answer a
    request nobody made. `create --resume` reached the body with both
    `is_create` and `is_resume` true and was answered by the resume branch, so
    an explicit `create` of a campaign that did not exist failed with "no
    existing campaign to resume" -- after taking the directory lock, which
    created the campaign directory and a lock file for a request that was never
    going to run.

    Command intent is now decided by one authoritative table before the command
    touches the campaign directory at all, so a contradictory request exits
    non-zero with a bounded conflict message and leaves no directory, no lock
    file, no campaign state, and no adapter call or network request behind. The
    compatible spellings -- an action alongside the legacy flag that names the
    *same* action -- keep working exactly as before.
    """

    @staticmethod
    def _seed(campaigns_dir: Path) -> dict[str, Any]:
        campaigns_dir.mkdir(parents=True, exist_ok=True)
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
        if not campaigns_dir.exists():
            return {}
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

    def _run(
        self,
        campaigns_dir: Path,
        kwargs: dict[str, Any],
    ) -> tuple[int, list[Path], list[list[str]], list[list[str]], str, str]:
        """Run one request with every outward effect recorded rather than performed."""
        gh_calls: list[list[str]] = []
        adapter_calls: list[list[str]] = []

        def recording_adapter(argv, timeout):
            adapter_calls.append(list(argv))
            raise AssertionError("no adapter may run for a contradictory request")

        def recording_gh_json(args, **_kwargs):
            gh_calls.append(list(args))
            return {"comments": []}, ""

        stdout, stderr = io.StringIO(), io.StringIO()
        with self._counting_lock() as entries:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                ret = release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    providers=["cursor_bugbot"],
                    campaigns_dir=campaigns_dir,
                    repo_slug="owner/repo",
                    issue="99",
                    command_runner=_capturing_dispatch_argv_runner(gh_calls),
                    gh_json_runner=recording_gh_json,
                    adapter_runner=recording_adapter,
                    **kwargs,
                )
        return ret, list(entries), gh_calls, adapter_calls, stdout.getvalue(), stderr.getvalue()

    # The contradictory half of the action/legacy-flag matrix, with the phrases
    # the refusal must name so the caller can see which two intents collided.
    _CONTRADICTIONS: tuple[tuple[str, dict[str, Any], tuple[str, ...]], ...] = (
        ("create + --resume", {"action": "create", "resume": True}, ("'create'", "--resume")),
        ("create + --status", {"action": "create", "status": True}, ("'create'",)),
        ("resume + --status", {"action": "resume", "status": True}, ("'resume'",)),
        ("dispatch + --status", {"action": "dispatch", "status": True}, ("'dispatch'",)),
        ("status + --resume", {"action": "status", "resume": True}, ("--resume",)),
        ("--status + --resume", {"status": True, "resume": True}, ("--resume",)),
    )

    def test_create_with_resume_is_rejected_before_any_campaign_state_exists(self) -> None:
        """The exact regression: a nonexistent campaign, an explicit create, --resume."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"

            ret, entries, gh_calls, adapter_calls, out, err = self._run(
                campaigns_dir, {"action": "create", "resume": True}
            )

            self.assertEqual(ret, 1)
            # Nothing was looked up, locked, created, dispatched, or polled.
            self.assertEqual(entries, [])
            self.assertFalse(campaigns_dir.exists())
            self.assertEqual(gh_calls, [])
            self.assertEqual(adapter_calls, [])
            self.assertEqual(out, "")
            # The refusal names the conflict, not one side's ordinary failure.
            self.assertIn("'create'", err)
            self.assertIn("--resume", err)
            self.assertNotIn("no existing campaign", err)
            self.assertNotIn("Traceback", err)
            self.assertNotIn(tmp, err)
            self.assertLessEqual(len(err.splitlines()), 2)

    def test_create_with_resume_is_rejected_for_an_existing_campaign_too(self) -> None:
        """Not an "already exists" report either: the request itself is incoherent."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._seed(campaigns_dir)
            before = self._snapshot(campaigns_dir)

            ret, entries, gh_calls, adapter_calls, out, err = self._run(
                campaigns_dir, {"action": "create", "resume": True}
            )

            self.assertEqual(ret, 1)
            self.assertEqual(entries, [])
            self.assertEqual(gh_calls, [])
            self.assertEqual(adapter_calls, [])
            self.assertEqual(out, "")
            self.assertIn("'create'", err)
            self.assertIn("--resume", err)
            self.assertNotIn("already exists", err)
            self.assertNotIn("Traceback", err)
            # The stored campaign is untouched, and no lock file was added.
            self.assertEqual(self._snapshot(campaigns_dir), before)

    def test_every_contradiction_is_refused_without_touching_the_directory(self) -> None:
        for named, kwargs, phrases in self._CONTRADICTIONS:
            for seeded in (False, True):
                with self.subTest(intent=named, seeded=seeded):
                    with tempfile.TemporaryDirectory() as tmp:
                        campaigns_dir = Path(tmp) / "campaigns"
                        if seeded:
                            self._seed(campaigns_dir)
                        before = self._snapshot(campaigns_dir)

                        ret, entries, gh_calls, adapter_calls, out, err = self._run(
                            campaigns_dir, kwargs
                        )

                        self.assertEqual(ret, 1)
                        self.assertEqual(entries, [])
                        self.assertEqual(gh_calls, [])
                        self.assertEqual(adapter_calls, [])
                        self.assertEqual(out, "")
                        self.assertEqual(self._snapshot(campaigns_dir), before)
                        if not seeded:
                            self.assertFalse(campaigns_dir.exists())
                        for phrase in phrases:
                            self.assertIn(phrase, err)
                        self.assertNotIn("Traceback", err)
                        self.assertNotIn(tmp, err)

    def test_the_valid_action_matrix_still_works(self) -> None:
        """Backwards compatibility, stated explicitly: each action and its legacy flag."""
        # (name, kwargs, needs_existing_campaign)
        valid: tuple[tuple[str, dict[str, Any], bool], ...] = (
            ("create", {"action": "create"}, False),
            ("implicit create", {}, False),
            ("resume action", {"action": "resume"}, True),
            ("--resume", {"resume": True}, True),
            ("resume action + --resume", {"action": "resume", "resume": True}, True),
            ("dispatch action", {"action": "dispatch"}, True),
            ("dispatch action + --resume", {"action": "dispatch", "resume": True}, True),
            ("implicit advance", {}, True),
            ("status action", {"action": "status"}, True),
            ("--status", {"status": True}, True),
            ("status action + --status", {"action": "status", "status": True}, True),
        )
        for named, kwargs, needs_existing in valid:
            with self.subTest(spelling=named):
                with tempfile.TemporaryDirectory() as tmp:
                    campaigns_dir = Path(tmp) / "campaigns"
                    if needs_existing:
                        self._seed(campaigns_dir)

                    ret, _entries, _gh, adapter_calls, out, _err = self._run(
                        campaigns_dir, kwargs
                    )

                    self.assertEqual(ret, 0)
                    self.assertEqual(adapter_calls, [])
                    self.assertIn("Release Campaign: v1.0.0", out)
                    stored = release_campaigns.load_campaign_by_id(
                        "campaign-v1.0.0", campaigns_dir
                    )
                    assert stored is not None
                    self.assertEqual(stored["release_tag"], "v1.0.0")

    def test_the_table_decides_every_action_and_legacy_flag_pair(self) -> None:
        """The matrix is data, not a branch: every pair is covered by one table."""
        self.assertEqual(
            sorted(release_campaigns._COMPATIBLE_LEGACY_FLAGS),
            sorted(release_campaigns.CAMPAIGN_ACTIONS),
        )
        expected_conflict = {
            ("create", False, True),
            ("create", True, False),
            ("create", True, True),
            ("status", False, True),
            ("status", True, True),
            ("resume", True, False),
            ("resume", True, True),
            ("dispatch", True, False),
            ("dispatch", True, True),
            ("upload", False, True),
            ("upload", True, False),
            ("upload", True, True),
            ("watch", False, True),
            ("watch", True, False),
            ("watch", True, True),
            (None, True, True),
        }
        for action in (None, *release_campaigns.CAMPAIGN_ACTIONS):
            for status in (False, True):
                for resume in (False, True):
                    with self.subTest(action=action, status=status, resume=resume):
                        conflict = release_campaigns._command_intent_conflict(
                            action=action,
                            record_result=None,
                            retry_provider="",
                            apply=False,
                            resume=resume,
                            status=status,
                        )
                        if (action, status, resume) in expected_conflict:
                            self.assertTrue(conflict)
                            self.assertNotIn("\n", conflict)
                        else:
                            self.assertEqual(conflict, "")

    def test_cli_create_with_resume_exits_non_zero_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = release_qualify.main(
                    [
                        "campaign",
                        "create",
                        "--resume",
                        "--release-tag",
                        "v1.0.0",
                        "--package-spec",
                        "code-mower==1.0.0",
                        "--providers",
                        "codex",
                        "--campaigns-dir",
                        str(campaigns_dir),
                        "--repo-path",
                        tmp,
                    ]
                )

            self.assertEqual(code, 1)
            self.assertEqual(stdout.getvalue(), "")
            # No campaign directory, so no campaign file and no `.campaigns.lock`.
            self.assertFalse(campaigns_dir.exists())
            message = stderr.getvalue()
            self.assertIn("'create'", message)
            self.assertIn("--resume", message)
            self.assertNotIn("Traceback", message)
            self.assertNotIn("campaign failed", message)
            self.assertNotIn(tmp, message)
            self.assertEqual(len(message.strip().splitlines()), 1)

    def test_cli_create_with_resume_is_rejected_beside_an_existing_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._seed(campaigns_dir)
            before = self._snapshot(campaigns_dir)

            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = release_qualify.main(
                    [
                        "campaign",
                        "create",
                        "--resume",
                        "--release-tag",
                        "v1.0.0",
                        "--campaigns-dir",
                        str(campaigns_dir),
                        "--repo-path",
                        tmp,
                    ]
                )

            self.assertEqual(code, 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(self._snapshot(campaigns_dir), before)
            self.assertNotIn("Traceback", stderr.getvalue())

    def test_cli_help_documents_the_action_flag_contract(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit):
                release_qualify.main(["campaign", "--help"])
        help_text = " ".join(stdout.getvalue().split())
        self.assertIn("`create --resume` is rejected, not resolved", help_text)


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


class AppliedCampaignIdentityIsMonotonicTests(unittest.TestCase):
    """Applied is a one-way transition: a poll without --apply never undoes it.

    A dry-run campaign becomes applied the first time it is dispatched with
    `--apply`. A later `resume` or `--status` poll that simply omits the flag
    is not a claim that the dispatches and attempts already made never
    happened, so it must not rewrite the campaign back into a preview -- which
    relabelled real evidence as a dry run in stored state, in the rendered
    text, and on the Board, and made the aggregate status regress to "run with
    --apply to dispatch providers" for providers that had already been
    dispatched.
    """

    @staticmethod
    def _muse_kwargs(repo_path: Path, campaigns_dir: Path, **extra: Any) -> dict[str, Any]:
        # Deterministic discovery: only the muse CLI resolves, everything else
        # is missing. muse ships a maintained campaign adapter, so a present
        # CLI previews as dispatchable (queued), not unavailable.
        return dict(
            release_tag="v1.0.0",
            campaigns_dir=campaigns_dir,
            repo_path=repo_path,
            which_fn=lambda cmd: "/bin/muse" if cmd == "muse" else None,
            **extra,
        )

    @staticmethod
    def _failing_adapter_runner(argv: Any, timeout: int) -> Any:
        """Deterministic stand-in for the real provider invocation: no network, no spend."""
        return subprocess.CompletedProcess(list(argv), 1, stdout="", stderr="")

    def _create_and_apply(self, repo_path: Path, campaigns_dir: Path) -> dict[str, Any]:
        """Create a dry-run campaign, then advance the same campaign with --apply."""
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            release_campaigns.campaign_command(
                **self._muse_kwargs(
                    repo_path,
                    campaigns_dir,
                    action="create",
                    package_spec="code-mower==1.0.0",
                    providers=["muse"],
                )
            )
        created = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
        assert created is not None
        # Create-dry-run: nothing has been applied yet. muse ships a maintained
        # campaign adapter and its CLI is present here, so the preview honestly
        # reports a dispatchable queued provider instead of unavailable.
        self.assertTrue(created["dry_run"])
        self.assertEqual(created["providers"][0]["dispatch_mode"], "dry_run")
        self.assertEqual(created["providers"][0]["state"], "queued")
        self.assertIn("(dry-run)", release_campaigns.render_campaign_text(created))

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            release_campaigns.campaign_command(
                **self._muse_kwargs(
                    repo_path,
                    campaigns_dir,
                    resume=True,
                    apply=True,
                    adapter_runner=self._failing_adapter_runner,
                )
            )
        applied = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
        assert applied is not None
        # Dry-run-to-applied: the one transition that is allowed. The mocked
        # adapter exits non-zero, so the applied provider records blocked --
        # the point here is the applied identity, preserved below.
        self.assertFalse(applied["dry_run"])
        self.assertEqual(applied["providers"][0]["dispatch_mode"], "applied")
        self.assertEqual(applied["providers"][0]["state"], "blocked")
        return applied

    def test_applied_resume_without_apply_keeps_applied_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            campaigns_dir = repo_path / ".code-mower" / "campaigns"
            applied = self._create_and_apply(repo_path, campaigns_dir)
            applied_status = applied["status"]
            self.assertEqual(applied_status, "blocked")

            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = release_campaigns.campaign_command(
                    **self._muse_kwargs(repo_path, campaigns_dir, resume=True)
                )

            # The mocked adapter failed qualification, so the applied campaign
            # reports blocked with a non-zero exit -- the preserved identity
            # below is what this test guards.
            self.assertEqual(code, 1)
            polled = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert polled is not None
            self.assertFalse(polled["dry_run"])
            self.assertEqual(polled["providers"][0]["dispatch_mode"], "applied")
            # Persisted status must not regress from the applied campaign's real
            # state back to a dry-run preview's "queued / run with --apply".
            self.assertEqual(polled["status"], applied_status)
            self.assertNotIn("run with --apply to dispatch providers", polled["next_action"])
            self.assertIn("(applied)", release_campaigns.render_campaign_text(polled))

    def test_board_projection_keeps_an_applied_campaign_applied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            campaigns_dir = repo_path / ".code-mower" / "campaigns"
            self._create_and_apply(repo_path, campaigns_dir)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                release_campaigns.campaign_command(
                    **self._muse_kwargs(repo_path, campaigns_dir, resume=True)
                )

            payload = release_campaigns.release_campaigns_board_payload(
                campaigns_dir=campaigns_dir
            )
            card_campaign = payload["campaigns"][0]
            self.assertFalse(card_campaign["dry_run"])
            self.assertEqual(card_campaign["status"], "blocked")

    def test_polling_a_dispatched_hosted_provider_keeps_applied_identity(self) -> None:
        """The literal reported case: a hosted dispatch, then a poll without --apply."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            bodies: list[str] = []

            def mock_gh_json(args, **kwargs):
                return {"comments": []}, ""

            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                release_campaigns.campaign_command(
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
            # Cursor BugBot has trigger_comments, so 2 comments posted: dispatch + trigger
            self.assertEqual(len(bodies), 2)
            dispatched = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert dispatched is not None
            self.assertFalse(dispatched["dry_run"])
            self.assertEqual(dispatched["providers"][0]["state"], "running")

            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    campaigns_dir=campaigns_dir,
                    resume=True,
                    gh_json_runner=mock_gh_json,
                    env={"CURSOR_BUGBOT_AUDIT_LABEL_TOKEN": "token"},
                )

            polled = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert polled is not None
            # No new comments during poll, still 2 total
            self.assertEqual(len(bodies), 2)
            self.assertFalse(polled["dry_run"])
            self.assertEqual(polled["providers"][0]["dispatch_mode"], "applied")
            self.assertEqual(polled["providers"][0]["state"], "running")
            self.assertIn("(applied)", release_campaigns.render_campaign_text(polled))

    def test_a_lost_top_level_flag_is_recovered_from_provider_dispatch_mode(self) -> None:
        """Two independent records answer "has this been applied"; either suffices."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            campaigns_dir = repo_path / ".code-mower" / "campaigns"
            applied = self._create_and_apply(repo_path, campaigns_dir)

            # A hand-edited or older campaign file whose flag no longer records
            # the applied dispatches its providers still carry.
            applied["dry_run"] = True
            release_campaigns.save_campaign(applied, campaigns_dir)

            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                release_campaigns.campaign_command(
                    **self._muse_kwargs(repo_path, campaigns_dir, resume=True)
                )

            polled = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert polled is not None
            self.assertFalse(polled["dry_run"])

    def test_a_never_applied_campaign_stays_a_dry_run_preview(self) -> None:
        """The invariant only preserves applied state; it never invents it.

        muse ships a maintained adapter and its CLI is present here, so the
        never-applied preview is an honest dispatchable queued preview.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            campaigns_dir = repo_path / ".code-mower" / "campaigns"
            for _ in range(2):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                    io.StringIO()
                ):
                    release_campaigns.campaign_command(
                        **self._muse_kwargs(
                            repo_path,
                            campaigns_dir,
                            package_spec="code-mower==1.0.0",
                            providers=["muse"],
                        )
                    )
            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            self.assertTrue(saved["dry_run"])
            self.assertEqual(saved["providers"][0]["dispatch_mode"], "dry_run")
            self.assertEqual(saved["providers"][0]["state"], "queued")
            self.assertEqual(saved["status"], "queued")
            self.assertEqual(
                saved["next_action"],
                "run with --apply to dispatch providers",
            )
            self.assertIn("(dry-run)", release_campaigns.render_campaign_text(saved))

    def test_campaign_has_been_applied_reads_malformed_state_as_dry_run(self) -> None:
        self.assertFalse(release_campaigns.campaign_has_been_applied({}))
        self.assertFalse(release_campaigns.campaign_has_been_applied({"dry_run": True}))
        for malformed in ("false", 0, None, [], {}):
            with self.subTest(malformed=malformed):
                self.assertFalse(
                    release_campaigns.campaign_has_been_applied({"dry_run": malformed})
                )
        self.assertTrue(release_campaigns.campaign_has_been_applied({"dry_run": False}))
        self.assertTrue(
            release_campaigns.campaign_has_been_applied(
                {"dry_run": True, "providers": [{"dispatch_mode": "applied"}]}
            )
        )
        self.assertFalse(
            release_campaigns.campaign_has_been_applied(
                {"dry_run": True, "providers": "not-a-list"}
            )
        )


class CampaignPackageIdentityBindingTests(unittest.TestCase):
    """Results are bound to the package the campaign's exact spec names.

    The campaign command deliberately accepts exact package specs, so the
    expected package identity is derived from that spec. Binding every result
    to a hard-coded `code-mower` both accepted results for the wrong
    distribution and refused every legitimate campaign for another one.
    """

    ADAPTER_CONFIG = (
        "version: 1\n"
        "lanes:\n"
        "  muse_cli:\n"
        "    provider_config:\n"
        "      campaign_adapter_argv:\n"
        "        - \"{command}\"\n"
        "        - qualify\n"
        "        - --output\n"
        "        - \"{output}\"\n"
    )

    def _run_adapter_campaign(
        self, *, package_spec: str, result_package_identity: str
    ) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            campaigns_dir = repo_path / ".code-mower" / "campaigns"
            (repo_path / "code-mower.yml").write_text(self.ADAPTER_CONFIG, encoding="utf-8")

            def fake_adapter_runner(argv, timeout):
                output_path = Path(argv[argv.index("--output") + 1])
                adoption_res = _mock_adoption_result(
                    release_tag="v1.0.0", provider="muse", outcome="pass"
                )
                adoption_res["package_identity"] = result_package_identity
                with output_path.open("w", encoding="utf-8") as fh:
                    json.dump(adoption_res, fh)
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    package_spec=package_spec,
                    providers=["muse"],
                    repo_path=repo_path,
                    campaigns_dir=campaigns_dir,
                    apply=True,
                    which_fn=lambda _cmd: "/bin/muse",
                    adapter_runner=fake_adapter_runner,
                )
            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            return saved

    def test_exact_non_code_mower_package_spec_completes_on_a_matching_result(self) -> None:
        saved = self._run_adapter_campaign(
            package_spec="other-widget==1.0.0", result_package_identity="other-widget"
        )
        self.assertEqual(saved["package_spec"], "other-widget==1.0.0")
        self.assertEqual(saved["package_identity"], "other-widget")
        self.assertEqual(saved["providers"][0]["state"], "complete")
        self.assertEqual(saved["providers"][0]["error"], "")

    def test_a_result_for_another_package_is_rejected(self) -> None:
        saved = self._run_adapter_campaign(
            package_spec="other-widget==1.0.0", result_package_identity="code-mower"
        )
        self.assertEqual(saved["providers"][0]["state"], "blocked")
        self.assertEqual(saved["providers"][0]["error"], "adapter_result_invalid")
        self.assertIsNone(saved["providers"][0]["adoption_result"])

    def test_a_code_mower_campaign_still_rejects_another_package_result(self) -> None:
        saved = self._run_adapter_campaign(
            package_spec="code-mower==1.0.0", result_package_identity="other-widget"
        )
        self.assertEqual(saved["providers"][0]["state"], "blocked")
        self.assertEqual(saved["providers"][0]["error"], "adapter_result_invalid")

    def test_drop_in_result_file_is_bound_to_the_campaign_package(self) -> None:
        for identity, expected_state in (
            ("other-widget", "complete"),
            ("code-mower", "unavailable"),
        ):
            with self.subTest(identity=identity):
                with tempfile.TemporaryDirectory() as tmp:
                    campaigns_dir = Path(tmp) / "campaigns"
                    campaign = release_campaigns.initialize_campaign(
                        release_tag="v1.0.0",
                        package_spec="other-widget==1.0.0",
                        providers=["muse"],
                    )
                    release_campaigns.save_campaign(campaign, campaigns_dir)
                    results_dir = campaigns_dir / "results"
                    results_dir.mkdir(parents=True, exist_ok=True)
                    adoption_res = _mock_adoption_result(
                        release_tag="v1.0.0", provider="muse", outcome="pass"
                    )
                    adoption_res["package_identity"] = identity
                    (results_dir / "campaign-v1.0.0_muse.json").write_text(
                        json.dumps(adoption_res), encoding="utf-8"
                    )

                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                        io.StringIO()
                    ):
                        release_campaigns.campaign_command(
                            release_tag="v1.0.0",
                            campaigns_dir=campaigns_dir,
                            resume=True,
                            which_fn=lambda _cmd: None,
                        )
                    saved = release_campaigns.load_campaign_by_id(
                        "campaign-v1.0.0", campaigns_dir
                    )
                    assert saved is not None
                    self.assertEqual(saved["providers"][0]["state"], expected_state)

    def test_github_comment_result_is_bound_to_the_campaign_package(self) -> None:
        for identity, expected_state in (("other-widget", "complete"), ("code-mower", "running")):
            with self.subTest(identity=identity):
                with tempfile.TemporaryDirectory() as tmp:
                    campaigns_dir = Path(tmp) / "campaigns"
                    campaign = release_campaigns.initialize_campaign(
                        release_tag="v1.0.0",
                        package_spec="other-widget==1.0.0",
                        providers=["cursor_bugbot"],
                        repo_slug="owner/repo",
                    )
                    campaign.status = "running"
                    campaign.providers[0]["state"] = "running"
                    campaign.providers[0]["dispatch_ref"] = {"issue_number": "99"}
                    release_campaigns.save_campaign(campaign, campaigns_dir)

                    adoption_res = _mock_adoption_result(
                        release_tag="v1.0.0", provider="cursor_bugbot", outcome="pass"
                    )
                    adoption_res["package_identity"] = identity
                    wrapper = {
                        "schema": release_campaigns.RESULT_MARKER_SCHEMA,
                        "campaign_id": campaign.campaign_id,
                        "provider": "cursor_bugbot",
                        "release_tag": "v1.0.0",
                        "idempotency_key": campaign.providers[0]["idempotency_key"],
                        "adoption_result": adoption_res,
                    }
                    marker = f"<!-- CODE_MOWER_ADOPTION_RESULT: {json.dumps(wrapper)} -->"

                    def mock_gh_json(args, _body=f"done\n\n{marker}", **kwargs):
                        return {
                            "comments": [{"author": {"login": "cursor[bot]"}, "body": _body}]
                        }, ""

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
                    saved = release_campaigns.load_campaign_by_id(
                        "campaign-v1.0.0", campaigns_dir
                    )
                    assert saved is not None
                    self.assertEqual(saved["providers"][0]["state"], expected_state)

    def test_recorded_result_for_another_package_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            campaign = release_campaigns.initialize_campaign(
                release_tag="v1.0.0",
                package_spec="other-widget==1.0.0",
                providers=["codex"],
            )
            release_campaigns.save_campaign(campaign, campaigns_dir)
            adoption_res = _mock_adoption_result(
                release_tag="v1.0.0", provider="codex", outcome="pass"
            )
            adoption_res["package_identity"] = "code-mower"
            result_path = Path(tmp) / "result.json"
            result_path.write_text(json.dumps(adoption_res), encoding="utf-8")

            stderr = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                code = release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    campaigns_dir=campaigns_dir,
                    record_result=result_path,
                    record_provider="codex",
                )

            self.assertEqual(code, 1)
            message = stderr.getvalue()
            self.assertIn("does not match the campaign package", message)
            self.assertNotIn("Traceback", message)
            self.assertNotIn(tmp, message)
            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            self.assertEqual(saved["providers"][0]["state"], "queued")

    def test_a_campaign_whose_stored_spec_is_unusable_binds_nothing(self) -> None:
        """A hand-edited spec with no package identity fails closed, not open."""
        self.assertEqual(release_campaigns.campaign_package_identity("code-mower==1.0.0"), "code-mower")
        self.assertEqual(release_campaigns.campaign_package_identity(""), "")
        self.assertEqual(release_campaigns.campaign_package_identity("/tmp/checkout"), "")

        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            campaign = release_campaigns.initialize_campaign(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["muse"],
            )
            stored = campaign.to_dict()
            stored["package_spec"] = "not-an-exact-spec"
            release_campaigns.save_campaign(stored, campaigns_dir)
            results_dir = campaigns_dir / "results"
            results_dir.mkdir(parents=True, exist_ok=True)
            (results_dir / "campaign-v1.0.0_muse.json").write_text(
                json.dumps(
                    _mock_adoption_result(release_tag="v1.0.0", provider="muse", outcome="pass")
                ),
                encoding="utf-8",
            )

            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    campaigns_dir=campaigns_dir,
                    resume=True,
                    which_fn=lambda _cmd: None,
                )
            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            self.assertNotEqual(saved["providers"][0]["state"], "complete")


class AmbiguousProviderLaneOverrideTests(unittest.TestCase):
    """One lane spelled twice in code-mower.yml is ambiguous, so it is refused.

    `muse` and `muse_cli` (or `claude` and `claude_code`) name one lane with one
    adapter command. Reading whichever spelling was looked up first meant two
    different `campaign_adapter_argv` values could each win depending on set
    iteration order. There is no correct choice between two adapter commands,
    so the configuration is rejected instead.
    """

    ARGV_BLOCK = (
        "    provider_config:\n"
        "      campaign_adapter_argv:\n"
        "        - \"{command}\"\n"
        "        - qualify\n"
        "        - --output\n"
        "        - \"{output}\"\n"
    )

    @classmethod
    def _config(cls, *lane_keys: str) -> str:
        return "version: 1\nlanes:\n" + "".join(
            f"  {key}:\n{cls.ARGV_BLOCK}" for key in lane_keys
        )

    def _apply_with_config(self, config_text: str, provider: str) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            campaigns_dir = repo_path / ".code-mower" / "campaigns"
            (repo_path / "code-mower.yml").write_text(config_text, encoding="utf-8")
            adapter_mock = mock.MagicMock()

            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    providers=[provider],
                    repo_path=repo_path,
                    campaigns_dir=campaigns_dir,
                    apply=True,
                    which_fn=lambda _cmd: "/bin/provider",
                    adapter_runner=adapter_mock,
                )

            adapter_mock.assert_not_called()
            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            serialized = json.dumps(saved)
            self.assertNotIn(tmp, serialized)
            return saved["providers"][0]

    def test_canonical_lane_id_plus_alias_is_rejected_in_either_order(self) -> None:
        entries = []
        for keys in (("muse_cli", "muse"), ("muse", "muse_cli")):
            with self.subTest(order=keys):
                entry = self._apply_with_config(self._config(*keys), "muse")
                self.assertEqual(entry["state"], "unavailable")
                self.assertEqual(entry["error"], "adapter_configuration_invalid")
                self.assertIn("muse, muse_cli", entry["next_detail"])
                self.assertIn("keep exactly one", entry["next_detail"])
                entries.append(entry)
        # Invariant across insertion order: the same refusal, worded the same.
        self.assertEqual(entries[0]["error"], entries[1]["error"])
        self.assertEqual(entries[0]["next_detail"], entries[1]["next_detail"])
        self.assertEqual(entries[0]["next_action"], entries[1]["next_action"])

    def test_two_aliases_of_one_lane_are_rejected_in_either_order(self) -> None:
        entries = []
        for keys in (("claude", "claude_code"), ("claude_code", "claude")):
            with self.subTest(order=keys):
                entry = self._apply_with_config(self._config(*keys), "claude")
                self.assertEqual(entry["state"], "unavailable")
                self.assertEqual(entry["error"], "adapter_configuration_invalid")
                self.assertIn("claude, claude_code", entry["next_detail"])
                entries.append(entry)
        self.assertEqual(entries[0]["next_detail"], entries[1]["next_detail"])

    def test_a_single_spelling_is_still_honored(self) -> None:
        """Rejection is scoped to genuine ambiguity, not to aliases as such."""
        for key in ("muse", "muse_cli"):
            with self.subTest(key=key):
                with tempfile.TemporaryDirectory() as tmp:
                    repo_path = Path(tmp)
                    campaigns_dir = repo_path / ".code-mower" / "campaigns"
                    (repo_path / "code-mower.yml").write_text(
                        self._config(key), encoding="utf-8"
                    )

                    def fake_adapter_runner(argv, timeout):
                        output_path = Path(argv[argv.index("--output") + 1])
                        with output_path.open("w", encoding="utf-8") as fh:
                            json.dump(
                                _mock_adoption_result(
                                    release_tag="v1.0.0", provider="muse", outcome="pass"
                                ),
                                fh,
                            )
                        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                        io.StringIO()
                    ):
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
                    saved = release_campaigns.load_campaign_by_id(
                        "campaign-v1.0.0", campaigns_dir
                    )
                    assert saved is not None
                    self.assertEqual(saved["providers"][0]["state"], "complete")

    def test_an_unrelated_lane_entry_does_not_collide(self) -> None:
        """Only spellings of the *same* lane collide; `claude_review` is its own lane."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            campaigns_dir = repo_path / ".code-mower" / "campaigns"
            (repo_path / "code-mower.yml").write_text(
                self._config("muse_cli", "claude_review"), encoding="utf-8"
            )

            def fake_adapter_runner(argv, timeout):
                output_path = Path(argv[argv.index("--output") + 1])
                with output_path.open("w", encoding="utf-8") as fh:
                    json.dump(
                        _mock_adoption_result(
                            release_tag="v1.0.0", provider="muse", outcome="pass"
                        ),
                        fh,
                    )
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
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

    def test_override_lane_keys_are_returned_in_a_stable_sorted_order(self) -> None:
        """No consumer may depend on set iteration order for these keys."""
        _, muse_lane = release_campaigns.resolve_provider_lane("muse")
        _, claude_lane = release_campaigns.resolve_provider_lane("claude")
        muse_keys = release_campaigns._campaign_adapter_override_lane_keys(muse_lane)
        claude_keys = release_campaigns._campaign_adapter_override_lane_keys(claude_lane)
        self.assertEqual(muse_keys, ("muse", "muse_cli"))
        self.assertEqual(claude_keys, ("claude", "claude_audit", "claude_code"))
        self.assertEqual(muse_keys, tuple(sorted(muse_keys)))
        self.assertEqual(claude_keys, tuple(sorted(claude_keys)))

    def test_override_loader_reports_the_same_conflict_for_either_key_order(self) -> None:
        """Order invariance at the source, independent of any campaign wiring."""
        _, lane = release_campaigns.resolve_provider_lane("muse")
        results = []
        for keys in (("muse", "muse_cli"), ("muse_cli", "muse")):
            with tempfile.TemporaryDirectory() as tmp:
                repo_path = Path(tmp)
                (repo_path / "code-mower.yml").write_text(
                    self._config(*keys), encoding="utf-8"
                )
                results.append(
                    release_campaigns._load_campaign_adapter_overrides(lane, repo_path)
                )
        self.assertEqual(results[0], results[1])
        overrides, error, detail = results[0]
        self.assertEqual(overrides, {})
        self.assertEqual(error, "adapter_configuration_invalid")
        self.assertIn("muse, muse_cli", detail)


class ExactPackageSpecParsingTests(unittest.TestCase):
    """A campaign's identity and its pinned version come from one parse.

    The identity used to be derived with a parser that accepts the dotted
    distribution names a package index accepts, while the version was read back
    out of the same string with a narrower `[\\w-]+` name grammar. Exact specs
    such as `zope.interface==5.0.0` -- and the documented `code.mower==1.0.0` --
    were therefore refused for a version mismatch they did not have.
    """

    ADAPTER_CONFIG = (
        "version: 1\n"
        "lanes:\n"
        "  muse_cli:\n"
        "    provider_config:\n"
        "      campaign_adapter_argv:\n"
        "        - \"{command}\"\n"
        "        - qualify\n"
        "        - --output\n"
        "        - \"{output}\"\n"
    )

    def test_dotted_and_underscore_names_create_campaigns(self) -> None:
        """Spellings one package index treats as one package all create."""
        cases = (
            ("v5.0.0", "zope.interface==5.0.0", "zope-interface", "5.0.0"),
            ("v1.0.0", "code.mower==1.0.0", "code-mower", "1.0.0"),
            ("v1.0.0", "code_mower==1.0.0", "code-mower", "1.0.0"),
            ("v1.0.0", "Code_Mower==1.0.0", "code-mower", "1.0.0"),
            ("v1.0.0", "code-mower==1.0.0", "code-mower", "1.0.0"),
            ("v5.0.0", "zope.interface_extra==5.0.0", "zope-interface-extra", "5.0.0"),
            ("v1.0.0-rc.1", "zope.interface==1.0.0rc1", "zope-interface", "1.0.0rc1"),
        )
        for tag, spec, identity, version in cases:
            with self.subTest(spec=spec):
                campaign = release_campaigns.initialize_campaign(
                    release_tag=tag,
                    package_spec=spec,
                    providers=["muse"],
                )
                self.assertEqual(campaign.package_identity, identity)
                self.assertEqual(campaign.normalized_version, version)
                # The spec is stored exactly as given; only the identity derived
                # from it is normalized.
                self.assertEqual(campaign.package_spec, spec)
                self.assertEqual(
                    release_campaigns.campaign_package_identity(spec), identity
                )

    def test_a_mismatched_tag_is_still_refused_for_a_dotted_name(self) -> None:
        """The version half is read, not skipped, for names with dots."""
        with self.assertRaises(ValueError) as ctx:
            release_campaigns.initialize_campaign(
                release_tag="v5.0.0",
                package_spec="zope.interface==5.0.1",
                providers=["muse"],
            )
        message = str(ctx.exception)
        self.assertIn("Version mismatch", message)
        self.assertIn("5.0.0", message)
        self.assertIn("zope.interface==5.0.1", message)

    def test_malformed_and_inexact_specs_are_refused_as_inexact(self) -> None:
        """Anything that is not `<name>==<version>` fails as an inexact spec.

        Not as a version mismatch: the spec names no single package and version
        a result could ever be bound to, and saying so is the accurate error.
        """
        for spec in (
            ".",
            "code-mower",
            "code-mower>=1.0.0",
            "code-mower==",
            "==1.0.0",
            "-code-mower==1.0.0",
            "code-mower[extra]==1.0.0",
            'code-mower==1.0.0; python_version<"3"',
            "code-mower==1.0.0 --index-url https://example.invalid/simple",
            "/tmp/code-mower",
            "./code-mower",
            "git+https://example.invalid/x.git",
            "https://example.invalid/x.whl",
        ):
            with self.subTest(spec=spec):
                with self.assertRaises(ValueError) as ctx:
                    release_campaigns.initialize_campaign(
                        release_tag="v1.0.0",
                        package_spec=spec,
                        providers=["muse"],
                    )
                message = str(ctx.exception)
                self.assertIn("Only exact package-index specs supported", message)
                self.assertNotIn("Version mismatch", message)
                self.assertEqual(release_campaigns.campaign_package_identity(spec), "")

    def test_the_cli_refuses_a_malformed_spec_without_a_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            stderr = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                code = release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    package_spec=f"{tmp}/checkout",
                    providers=["muse"],
                    campaigns_dir=campaigns_dir,
                )
            self.assertEqual(code, 1)
            message = stderr.getvalue()
            self.assertIn("Only exact package-index specs supported", message)
            self.assertNotIn("Traceback", message)
            self.assertNotIn(tmp, message)
            self.assertIsNone(
                release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            )

    def test_every_spec_with_an_identity_also_yields_its_version(self) -> None:
        """No spec parses for identity and then fails to parse for version."""
        for spec in (
            "zope.interface==5.0.0",
            "code.mower==1.0.0",
            "Code_Mower==1.0.0",
            "CODE--MOWER==1.0.0",
            "other-widget==1.0.0",
        ):
            with self.subTest(spec=spec):
                identity, version = release_qualify._parse_exact_package_spec(spec)
                self.assertEqual(
                    identity, release_campaigns.campaign_package_identity(spec)
                )
                self.assertEqual(version, spec.split("==", 1)[1])

    def test_a_dotted_campaign_binds_results_to_the_normalized_identity(self) -> None:
        """Result binding compares the normalized identity, not the spelling."""
        for result_identity, expected_state, expected_error in (
            ("code-mower", "complete", ""),
            ("other-widget", "blocked", "adapter_result_invalid"),
        ):
            with self.subTest(result_identity=result_identity):
                with tempfile.TemporaryDirectory() as tmp:
                    repo_path = Path(tmp)
                    campaigns_dir = repo_path / ".code-mower" / "campaigns"
                    (repo_path / "code-mower.yml").write_text(
                        self.ADAPTER_CONFIG, encoding="utf-8"
                    )

                    def fake_adapter_runner(argv, timeout, _identity=result_identity):
                        output_path = Path(argv[argv.index("--output") + 1])
                        adoption_res = _mock_adoption_result(
                            release_tag="v1.0.0", provider="muse", outcome="pass"
                        )
                        adoption_res["package_identity"] = _identity
                        with output_path.open("w", encoding="utf-8") as fh:
                            json.dump(adoption_res, fh)
                        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                        io.StringIO()
                    ):
                        release_campaigns.campaign_command(
                            release_tag="v1.0.0",
                            package_spec="code.mower==1.0.0",
                            providers=["muse"],
                            repo_path=repo_path,
                            campaigns_dir=campaigns_dir,
                            apply=True,
                            which_fn=lambda _cmd: "/bin/muse",
                            adapter_runner=fake_adapter_runner,
                        )

                    saved = release_campaigns.load_campaign_by_id(
                        "campaign-v1.0.0", campaigns_dir
                    )
                    assert saved is not None
                    self.assertEqual(saved["package_spec"], "code.mower==1.0.0")
                    self.assertEqual(saved["package_identity"], "code-mower")
                    self.assertEqual(saved["providers"][0]["state"], expected_state)
                    self.assertEqual(saved["providers"][0]["error"], expected_error)


class HostedDispatchCrashWindowTests(unittest.TestCase):
    """A hosted dispatch interrupted around its external post stays pollable.

    The dangerous window is between the `gh issue comment` call and the save
    that records its outcome: the comment may already exist on GitHub, and
    nothing in this process will ever learn whether it does. A checkpoint that
    recorded only `attempted_at` left the provider `queued`, which an ordinary
    resume neither polls (it is not `running`) nor redispatches (it is already
    attempted) -- so the campaign stalled until someone ran an explicit retry,
    which then posted a second comment for a dispatch that may well have
    succeeded.

    The checkpoint therefore persists a pollable state *before* the post, and
    claims a successful post only once `gh` returns zero.
    """

    CAMPAIGN_ID = "campaign-v1.0.0"
    ISSUE = "42"
    TRUSTED_AUTHOR = "devin-ai-integration[bot]"

    @staticmethod
    def _load(campaigns_dir: Path) -> dict[str, Any]:
        """Reload the campaign from disk -- never from the in-process object."""
        stored = release_campaigns.load_campaign_by_id(
            HostedDispatchCrashWindowTests.CAMPAIGN_ID, campaigns_dir
        )
        assert stored is not None
        return stored

    @staticmethod
    def _no_comments(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], str]:
        return {"comments": []}, ""

    @staticmethod
    def _ok_command_runner(argv, **_kwargs) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    def _crash_during_dispatch(
        self,
        campaigns_dir: Path,
        *,
        lane: ProviderLane | None = None,
    ) -> list[list[str]]:
        """Apply a hosted dispatch whose external post never returns to its caller."""
        posted: list[list[str]] = []

        def interrupted_command_runner(argv, **_kwargs):
            posted.append(list(argv))
            raise _ProcessInterrupted("interrupted after the dispatch comment was posted")

        with mock.patch.object(
            release_campaigns,
            "resolve_provider_lane",
            return_value=("devin", lane or _fake_hosted_bridge_lane()),
        ):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                with self.assertRaises(_ProcessInterrupted):
                    release_campaigns.campaign_command(
                        release_tag="v1.0.0",
                        package_spec="code-mower==1.0.0",
                        providers=["devin"],
                        repo_slug="owner/repo",
                        issue=self.ISSUE,
                        campaigns_dir=campaigns_dir,
                        apply=True,
                        which_fn=lambda _cmd: "/bin/gh",
                        command_runner=interrupted_command_runner,
                        gh_json_runner=self._no_comments,
                        env={"DEVIN_API_KEY": "token"},
                    )

        self.assertEqual(len(posted), 1, posted)
        return posted

    def _dispatch(
        self,
        campaigns_dir: Path,
        *,
        command_runner,
        lane: ProviderLane | None = None,
    ) -> None:
        """Apply a hosted dispatch that runs to completion."""
        with mock.patch.object(
            release_campaigns,
            "resolve_provider_lane",
            return_value=("devin", lane or _fake_hosted_bridge_lane()),
        ):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    providers=["devin"],
                    repo_slug="owner/repo",
                    issue=self.ISSUE,
                    campaigns_dir=campaigns_dir,
                    apply=True,
                    which_fn=lambda _cmd: "/bin/gh",
                    command_runner=command_runner,
                    gh_json_runner=self._no_comments,
                    env={"DEVIN_API_KEY": "token"},
                )

    def _resume(
        self,
        campaigns_dir: Path,
        *,
        lane: ProviderLane | None = None,
        gh_json_runner=None,
        command_runner=None,
        apply: bool = False,
        retry_provider: str = "",
        issue: str = "",
    ):
        """Resume the stored campaign. `--issue` defaults to absent: an ordinary
        resume must poll the issue it stored, not one supplied again."""
        runner = mock.MagicMock() if command_runner is None else command_runner
        with mock.patch.object(
            release_campaigns,
            "resolve_provider_lane",
            return_value=("devin", lane or _fake_hosted_bridge_lane()),
        ):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    campaigns_dir=campaigns_dir,
                    resume=True,
                    apply=apply,
                    retry_provider=retry_provider,
                    issue=issue,
                    repo_slug="owner/repo",
                    which_fn=lambda _cmd: "/bin/gh",
                    command_runner=runner,
                    gh_json_runner=gh_json_runner or self._no_comments,
                    env={"DEVIN_API_KEY": "token"},
                )
        return runner

    def _trusted_result_comments(self, campaigns_dir: Path, *, outcome: str = "pass"):
        """A trusted-author comment carrying a result bound to the stored dispatch."""
        stored = self._load(campaigns_dir)
        wrapper = {
            "schema": release_campaigns.RESULT_MARKER_SCHEMA,
            "campaign_id": stored["campaign_id"],
            "provider": "devin",
            "release_tag": "v1.0.0",
            "idempotency_key": stored["providers"][0]["idempotency_key"],
            "adoption_result": _mock_adoption_result(provider="devin", outcome=outcome),
        }
        marker = f"<!-- CODE_MOWER_ADOPTION_RESULT: {json.dumps(wrapper)} -->"

        def gh_json_runner(*_args: Any, **_kwargs: Any):
            return {
                "comments": [
                    {
                        "author": {"login": self.TRUSTED_AUTHOR},
                        "body": f"Qualification finished.\n\n{marker}",
                    }
                ]
            }, ""

        return gh_json_runner

    def test_a_crash_around_the_external_post_checkpoints_a_pollable_state(self) -> None:
        """Reloaded from disk: attempted, running, and addressed to a known issue."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._crash_during_dispatch(campaigns_dir)

            stored = self._load(campaigns_dir)
            provider_entry = stored["providers"][0]
            self.assertEqual(provider_entry["state"], "running")
            self.assertTrue(provider_entry["attempted_at"])
            self.assertEqual(provider_entry["dispatch_mode"], "applied")
            # The issue identity a later poll needs, in a bounded field.
            self.assertEqual(provider_entry["dispatch_ref"]["issue_number"], self.ISSUE)
            # Nothing claims the post succeeded: this process never saw it return.
            self.assertFalse(provider_entry["dispatch_ref"]["comment_posted"])
            self.assertIsNone(provider_entry["dispatched_at"])
            self.assertIsNone(provider_entry["adoption_result"])
            self.assertEqual(provider_entry["error"], "")
            self.assertEqual(
                provider_entry["next_action"], "poll devin remote progress marker"
            )
            self.assertFalse(stored["dry_run"])

    def test_the_checkpointed_campaign_header_is_accurate_for_the_board(self) -> None:
        """The interrupted run may be the last writer, so its header must be true."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._crash_during_dispatch(campaigns_dir)

            stored = self._load(campaigns_dir)
            self.assertEqual(stored["status"], "running")
            self.assertIn("poll running providers: devin", stored["next_action"])
            # Never the pre-attempt headline for work that is already dispatched.
            self.assertNotIn("run with --apply", stored["next_action"])

            payload = release_campaigns.release_campaigns_board_payload(
                campaigns_dir=campaigns_dir
            )
            entry = payload["campaigns"][0]
            self.assertEqual(entry["status"], "running")
            self.assertFalse(entry["dry_run"])
            self.assertIn("poll running providers: devin", entry["next_action"])
            card = entry["cards"][0]
            self.assertEqual(card["state"], "running")
            self.assertEqual(card["next_action"], "poll devin remote progress marker")

    def test_ordinary_resume_polls_the_original_issue_and_never_reposts(self) -> None:
        """Resume names no issue: it must poll the one the interrupted run stored."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._crash_during_dispatch(campaigns_dir)
            checkpoint = self._load(campaigns_dir)["providers"][0]

            polled: list[list[str]] = []

            def recording_gh_json(args, **_kwargs):
                polled.append(list(args))
                return {"comments": []}, ""

            runner = self._resume(campaigns_dir, gh_json_runner=recording_gh_json)

            runner.assert_not_called()
            self.assertEqual(len(polled), 1, polled)
            self.assertIn(self.ISSUE, polled[0])
            self.assertIn("owner/repo", polled[0])

            stored = self._load(campaigns_dir)
            provider_entry = stored["providers"][0]
            self.assertEqual(provider_entry["state"], "running")
            self.assertEqual(provider_entry["attempted_at"], checkpoint["attempted_at"])
            self.assertIsNone(provider_entry["dispatched_at"])
            self.assertEqual(stored["status"], "running")

    def test_an_applied_resume_in_the_crash_window_also_never_reposts(self) -> None:
        """`--apply` is not a retry: only --retry-provider may dispatch again."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._crash_during_dispatch(campaigns_dir)

            runner = self._resume(campaigns_dir, apply=True)

            runner.assert_not_called()
            stored = self._load(campaigns_dir)
            self.assertEqual(stored["providers"][0]["state"], "running")
            self.assertEqual(stored["status"], "running")

    def test_repeated_no_result_resumes_stay_running_and_post_nothing(self) -> None:
        """If nothing was ever posted, resume waits -- it never infers a dispatch."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._crash_during_dispatch(campaigns_dir)

            for _ in range(3):
                runner = self._resume(campaigns_dir)
                runner.assert_not_called()
                stored = self._load(campaigns_dir)
                self.assertEqual(stored["providers"][0]["state"], "running")
                self.assertIsNone(stored["providers"][0]["dispatched_at"])
                self.assertEqual(stored["status"], "running")

            self.assertEqual(
                sorted(p.name for p in campaigns_dir.glob("*.json")),
                [f"{self.CAMPAIGN_ID}.json"],
            )

    def test_nonresponsive_hosted_provider_expires_without_redispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            lane = _fake_hosted_bridge_lane(response_timeout=60)
            self._dispatch(
                campaigns_dir,
                lane=lane,
                command_runner=self._ok_command_runner,
            )
            stored = self._load(campaigns_dir)
            stored["providers"][0]["response_deadline_at"] = "2000-01-01T00:00:00Z"
            release_campaigns.save_campaign(stored, campaigns_dir)

            runner = self._resume(campaigns_dir, lane=lane)

            runner.assert_not_called()
            expired = self._load(campaigns_dir)["providers"][0]
            self.assertEqual(expired["state"], "unavailable")
            self.assertEqual(expired["error"], "hosted_response_timeout")
            self.assertIn("--retry-provider devin", expired["next_action"])
            board_payload = release_campaigns.release_campaigns_board_payload(
                campaigns_dir=campaigns_dir
            )
            self.assertEqual(
                board_payload["campaigns"][0]["cards"][0]["response_deadline_at"],
                "2000-01-01T00:00:00Z",
            )

    def test_legacy_hosted_provider_gets_fresh_deadline_before_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            lane = _fake_hosted_bridge_lane(response_timeout=60)
            self._dispatch(
                campaigns_dir,
                lane=lane,
                command_runner=self._ok_command_runner,
            )
            stored = self._load(campaigns_dir)
            stored["providers"][0].pop("response_deadline_at", None)
            stored["providers"][0]["attempted_at"] = "2000-01-01T00:00:00Z"
            stored["providers"][0]["dispatched_at"] = "2000-01-01T00:00:00Z"
            release_campaigns.save_campaign(stored, campaigns_dir)

            runner = self._resume(campaigns_dir, lane=lane)

            runner.assert_not_called()
            migrated = self._load(campaigns_dir)["providers"][0]
            self.assertEqual(migrated["state"], "running")
            self.assertEqual(migrated["error"], "")
            self.assertTrue(migrated["response_deadline_at"])

    def test_github_outage_does_not_expire_hosted_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            lane = _fake_hosted_bridge_lane(response_timeout=60)
            self._dispatch(
                campaigns_dir,
                lane=lane,
                command_runner=self._ok_command_runner,
            )
            stored = self._load(campaigns_dir)
            stored["providers"][0]["response_deadline_at"] = "2000-01-01T00:00:00Z"
            release_campaigns.save_campaign(stored, campaigns_dir)

            self._resume(
                campaigns_dir,
                lane=lane,
                gh_json_runner=lambda *_args, **_kwargs: (
                    {},
                    "github_poll_unavailable",
                ),
            )

            waiting = self._load(campaigns_dir)["providers"][0]
            self.assertEqual(waiting["state"], "running")
            self.assertEqual(waiting["error"], "github_poll_unavailable")

    def test_resume_completes_from_a_valid_trusted_bound_result(self) -> None:
        """The comment the interrupted dispatch may have posted is still honored."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            lane = _fake_hosted_bridge_lane(bot_authors=(self.TRUSTED_AUTHOR,))
            self._crash_during_dispatch(campaigns_dir, lane=lane)

            runner = self._resume(
                campaigns_dir,
                lane=lane,
                gh_json_runner=self._trusted_result_comments(campaigns_dir),
            )

            runner.assert_not_called()
            stored = self._load(campaigns_dir)
            provider_entry = stored["providers"][0]
            self.assertEqual(provider_entry["state"], "complete")
            self.assertEqual(provider_entry["next_action"], "none")
            self.assertTrue(provider_entry["completed_at"])
            assert provider_entry["adoption_result"] is not None
            self.assertEqual(provider_entry["adoption_result"]["provider"], "devin")
            self.assertEqual(stored["status"], "complete")

    def test_resume_blocks_from_a_valid_trusted_failing_result(self) -> None:
        """A failing bound result is just as conclusive as a passing one."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            lane = _fake_hosted_bridge_lane(bot_authors=(self.TRUSTED_AUTHOR,))
            self._crash_during_dispatch(campaigns_dir, lane=lane)

            runner = self._resume(
                campaigns_dir,
                lane=lane,
                gh_json_runner=self._trusted_result_comments(campaigns_dir, outcome="fail"),
            )

            runner.assert_not_called()
            stored = self._load(campaigns_dir)
            self.assertEqual(stored["providers"][0]["state"], "blocked")
            self.assertEqual(stored["status"], "blocked")

    def test_an_untrusted_result_never_completes_the_checkpointed_dispatch(self) -> None:
        """Trusted-author binding is unchanged by the checkpoint: the poll waits."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            # Lane with no configured trusted authors: nobody's reply counts.
            self._crash_during_dispatch(campaigns_dir)
            gh_json_runner = self._trusted_result_comments(campaigns_dir)

            runner = self._resume(campaigns_dir, gh_json_runner=gh_json_runner)

            runner.assert_not_called()
            stored = self._load(campaigns_dir)
            self.assertEqual(stored["providers"][0]["state"], "running")
            self.assertIsNone(stored["providers"][0]["adoption_result"])

    def test_an_explicit_retry_is_read_only_without_apply(self) -> None:
        """A checkpointed dispatch is not redispatched by a preview."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._crash_during_dispatch(campaigns_dir)

            runner = self._resume(campaigns_dir, retry_provider="devin")

            runner.assert_not_called()
            stored = self._load(campaigns_dir)
            provider_entry = stored["providers"][0]
            self.assertEqual(provider_entry["state"], "running")
            self.assertEqual(
                provider_entry["next_action"],
                "run with --apply --retry-provider devin to retry devin",
            )

    def test_an_explicit_applied_retry_reposts_exactly_once(self) -> None:
        """The one documented way out of an uncertain dispatch, on operator demand."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._crash_during_dispatch(campaigns_dir)

            posted: list[list[str]] = []

            def counting_command_runner(argv, **_kwargs):
                posted.append(list(argv))
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            self._resume(
                campaigns_dir,
                apply=True,
                retry_provider="devin",
                issue=self.ISSUE,
                command_runner=counting_command_runner,
            )

            self.assertEqual(len(posted), 1, posted)
            stored = self._load(campaigns_dir)
            provider_entry = stored["providers"][0]
            self.assertEqual(provider_entry["state"], "running")
            # The confirmed post replaces the uncertain checkpoint reference.
            self.assertTrue(provider_entry["dispatch_ref"]["comment_posted"])
            self.assertEqual(provider_entry["dispatch_ref"]["issue_number"], self.ISSUE)
            self.assertTrue(provider_entry["dispatched_at"])
            self.assertEqual(stored["status"], "running")

    def test_a_retry_that_cannot_dispatch_keeps_the_dispatch_pollable(self) -> None:
        """A refused retry learns nothing about the outstanding post.

        `--apply --retry-provider devin` without `--issue` cannot dispatch, but
        that refusal costs nothing externally: demoting the provider to
        `unavailable` would stop every later resume from reading the comment
        the interrupted attempt may already have posted.
        """
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._crash_during_dispatch(campaigns_dir)

            runner = self._resume(campaigns_dir, apply=True, retry_provider="devin")

            runner.assert_not_called()
            stored = self._load(campaigns_dir)
            provider_entry = stored["providers"][0]
            self.assertEqual(provider_entry["state"], "running")
            self.assertEqual(provider_entry["dispatch_ref"]["issue_number"], self.ISSUE)
            self.assertIn("--issue", provider_entry["next_action"])
            self.assertEqual(stored["status"], "running")

            # And the dispatch is still pollable to a conclusion afterwards.
            lane = _fake_hosted_bridge_lane(bot_authors=(self.TRUSTED_AUTHOR,))
            self._resume(
                campaigns_dir,
                lane=lane,
                gh_json_runner=self._trusted_result_comments(campaigns_dir),
            )
            self.assertEqual(self._load(campaigns_dir)["providers"][0]["state"], "complete")

    def test_a_failed_external_post_records_the_unavailable_result(self) -> None:
        """An in-process failure is a known outcome, so it replaces the checkpoint."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            posted: list[list[str]] = []

            def failing_command_runner(argv, **_kwargs):
                posted.append(list(argv))
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="gh: boom")

            self._dispatch(campaigns_dir, command_runner=failing_command_runner)

            self.assertEqual(len(posted), 1, posted)
            stored = self._load(campaigns_dir)
            provider_entry = stored["providers"][0]
            self.assertEqual(provider_entry["state"], "unavailable")
            self.assertEqual(provider_entry["error"], "github_dispatch_failed")
            self.assertTrue(provider_entry["attempted_at"])
            self.assertIsNone(provider_entry["dispatched_at"])
            self.assertFalse(provider_entry["dispatch_ref"]["comment_posted"])
            self.assertEqual(
                provider_entry["next_action"],
                "retry devin dispatch when GitHub is available",
            )
            self.assertEqual(stored["status"], "unavailable")

    def test_a_failed_external_post_still_requires_an_explicit_retry(self) -> None:
        """Ordinary resume after a failed post neither reposts nor forgets it."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"

            def failing_command_runner(argv, **_kwargs):
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="gh: boom")

            self._dispatch(campaigns_dir, command_runner=failing_command_runner)

            runner = self._resume(campaigns_dir, apply=True)
            runner.assert_not_called()
            stored = self._load(campaigns_dir)
            self.assertEqual(stored["providers"][0]["state"], "unavailable")
            self.assertEqual(
                stored["providers"][0]["next_action"],
                "use --retry-provider devin to retry devin",
            )

            posted: list[list[str]] = []

            def counting_command_runner(argv, **_kwargs):
                posted.append(list(argv))
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            self._resume(
                campaigns_dir,
                apply=True,
                retry_provider="devin",
                issue=self.ISSUE,
                command_runner=counting_command_runner,
            )

            self.assertEqual(len(posted), 1, posted)
            stored = self._load(campaigns_dir)
            provider_entry = stored["providers"][0]
            self.assertEqual(provider_entry["state"], "running")
            self.assertEqual(provider_entry["error"], "")
            self.assertTrue(provider_entry["dispatch_ref"]["comment_posted"])
            self.assertTrue(provider_entry["dispatched_at"])

    def test_a_successful_dispatch_records_the_returned_metadata(self) -> None:
        """The happy path is unchanged: confirmed post, confirmed reference."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._dispatch(campaigns_dir, command_runner=self._ok_command_runner)

            stored = self._load(campaigns_dir)
            provider_entry = stored["providers"][0]
            self.assertEqual(provider_entry["state"], "running")
            self.assertEqual(
                provider_entry["dispatch_ref"],
                {"issue_number": self.ISSUE, "comment_posted": True},
            )
            self.assertTrue(provider_entry["dispatched_at"])
            self.assertTrue(provider_entry["attempted_at"])
            self.assertEqual(stored["status"], "running")


class CampaignUploadTests(unittest.TestCase):
    """`release campaign upload`: preview-first cloud publication of campaign evidence.

    Everything here is mocked at the HTTP boundary: no test may reach the
    network, read the developer's real token profiles, or write campaign state.
    """

    FAKE_CREDENTIAL = "cmw_live_abcdefghijklmnop"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.campaigns_dir = self.tmp / "campaigns"
        # An empty, explicit token directory: never the developer's real
        # ~/.config/code-mower/tokens.
        self.token_dir = self.tmp / "tokens"
        self.token_dir.mkdir(parents=True)

    @contextlib.contextmanager
    def _cloud_env(self, credential: str = "") -> Any:
        """Run with a hermetic environment carrying only the cloud token, if any."""
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
        }
        if credential:
            env[release_campaigns._load_cloud_client().DEFAULT_TOKEN_ENV] = credential
        with mock.patch.dict(os.environ, env, clear=True):
            yield

    def _seed(
        self,
        *,
        providers: tuple[str, ...] = ("claude", "codex"),
        complete: tuple[str, ...] = ("claude",),
    ) -> dict[str, Any]:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=list(providers),
                campaigns_dir=self.campaigns_dir,
            )
            campaign = release_campaigns.load_campaign_by_id(
                "campaign-v1.0.0", self.campaigns_dir
            )
            assert campaign is not None
            for provider in complete:
                campaign = release_campaigns.record_manual_result(
                    campaign,
                    provider,
                    _mock_adoption_result(provider=provider),
                    campaigns_dir=self.campaigns_dir,
                )
        return campaign

    def _upload(self, **kwargs: Any) -> tuple[int, dict[str, Any] | None, str, str]:
        """Run the upload action, returning (exit code, parsed JSON, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = release_campaigns.campaign_command(
                action="upload",
                release_tag="v1.0.0",
                campaigns_dir=self.campaigns_dir,
                token_dir=self.token_dir,
                emit_json=True,
                **kwargs,
            )
        stdout = out.getvalue()
        parsed = json.loads(stdout) if stdout.strip() else None
        return code, parsed, stdout, err.getvalue()

    @staticmethod
    def _capturing_post(response: dict[str, Any] | None = None) -> Any:
        posted: list[dict[str, Any]] = []

        def _post(*, payload: dict[str, Any], endpoint: str, token: str, timeout: float) -> dict[str, Any]:
            posted.append(payload)
            return {
                "mode": "cloud-upload",
                "endpoint": endpoint,
                "status": 200,
                "response": response or {"ok": True},
            }

        _post.posted = posted  # type: ignore[attr-defined]
        return _post

    def _stored_bytes(self) -> dict[str, bytes]:
        return {
            path.name: path.read_bytes()
            for path in sorted(self.campaigns_dir.glob("*.json"))
        }

    def test_zero_completed_providers_uploads_nothing(self) -> None:
        """No completed provider means no evidence: reported truthfully, never posted."""
        self._seed(complete=())
        post = self._capturing_post()
        with self._cloud_env(self.FAKE_CREDENTIAL), mock.patch.object(
            release_campaigns._load_cloud_client(), "post_upload_payload", post
        ):
            code, result, _, _ = self._upload(yes=True)

        self.assertEqual(code, 0)
        assert result is not None
        self.assertEqual(result["status"], "no_events")
        self.assertEqual(result["counts"]["accepted"], 0)
        self.assertEqual(result["counts"]["complete"], 0)
        self.assertEqual(result["counts"]["skipped"], 2)
        self.assertEqual(result["event_ids"], [])
        self.assertEqual(post.posted, [])
        self.assertIn("complete at least one provider", result["next_action"])

    def test_some_completed_providers_convert_only_completed_results(self) -> None:
        """Completed providers convert; incomplete/unavailable ones are skipped, not faked."""
        self._seed(complete=("claude",))
        with self._cloud_env():
            code, result, _, _ = self._upload()

        self.assertEqual(code, 0)
        assert result is not None
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["accepted_providers"], ["claude"])
        self.assertEqual(result["counts"], {
            "providers": 2,
            "complete": 1,
            "skipped": 1,
            "accepted": 1,
            "rejected": 0,
            "events": 1,
        })
        self.assertEqual(
            [row["provider"] for row in result["skipped_providers"]], ["codex"]
        )
        self.assertEqual(result["upload"]["event_types"], {"adoption_run": 1})
        self.assertFalse(result["upload"]["would_upload"])

    def test_all_completed_providers_convert_in_deterministic_order(self) -> None:
        """Every completed result crosses; the event set is ordered, not incidental."""
        self._seed(complete=("claude", "codex"))
        with self._cloud_env():
            code, result, _, _ = self._upload()

        self.assertEqual(code, 0)
        assert result is not None
        self.assertEqual(result["accepted_providers"], ["claude", "codex"])
        self.assertEqual(result["counts"]["events"], 2)
        self.assertEqual(result["skipped_providers"], [])
        self.assertEqual(len(set(result["event_ids"])), 2)

    def test_dry_run_and_applied_upload_share_one_event_set(self) -> None:
        """What the preview shows is exactly what --yes uploads."""
        self._seed(complete=("claude", "codex"))
        post = self._capturing_post()
        with self._cloud_env(self.FAKE_CREDENTIAL):
            _, preview, _, _ = self._upload()
            with mock.patch.object(
                release_campaigns._load_cloud_client(), "post_upload_payload", post
            ):
                code, applied, _, _ = self._upload(yes=True)

        self.assertEqual(code, 0)
        assert preview is not None and applied is not None
        self.assertEqual(preview["event_ids"], applied["event_ids"])
        self.assertEqual(len(post.posted), 1)
        posted_ids = [event["event_id"] for event in post.posted[0]["events"]]
        self.assertEqual(posted_ids, preview["event_ids"])
        self.assertEqual(post.posted[0]["upload_mode"], "metadata_only")
        self.assertEqual(post.posted[0]["reports"], [])

    def test_repeat_upload_reuses_idempotent_event_ids(self) -> None:
        """Uploading the same evidence twice republishes the same ids, never new ones."""
        self._seed(complete=("claude",))
        post = self._capturing_post()
        with self._cloud_env(self.FAKE_CREDENTIAL), mock.patch.object(
            release_campaigns._load_cloud_client(), "post_upload_payload", post
        ):
            _, first, _, _ = self._upload(yes=True)
            _, second, _, _ = self._upload(yes=True)

        assert first is not None and second is not None
        self.assertEqual(first["event_ids"], second["event_ids"])
        self.assertEqual(len(post.posted), 2)
        self.assertEqual(post.posted[0]["events"], post.posted[1]["events"])

    def test_upload_writes_no_campaign_state(self) -> None:
        """Upload publishes; it never advances, records, or rewrites a campaign."""
        self._seed(complete=("claude",))
        before = self._stored_bytes()
        post = self._capturing_post()
        with self._cloud_env(self.FAKE_CREDENTIAL), mock.patch.object(
            release_campaigns._load_cloud_client(), "post_upload_payload", post
        ):
            self._upload(yes=True)

        self.assertEqual(before, self._stored_bytes())

    def test_malformed_stored_result_is_a_bounded_error(self) -> None:
        """A completed provider with an unusable result stops the upload; nothing partial posts."""
        self._seed(complete=("claude", "codex"))
        path = self.campaigns_dir / "campaign-v1.0.0.json"
        stored = json.loads(path.read_text(encoding="utf-8"))
        for provider in stored["providers"]:
            if provider["provider"] == "claude":
                # Hand-edited/corrupted evidence: still marked complete, but the
                # result no longer satisfies the adoption-result schema.
                provider["adoption_result"].pop("steps")
            if provider["provider"] == "codex":
                provider["adoption_result"] = None
        path.write_text(json.dumps(stored), encoding="utf-8")

        post = self._capturing_post()
        with self._cloud_env(self.FAKE_CREDENTIAL), mock.patch.object(
            release_campaigns._load_cloud_client(), "post_upload_payload", post
        ):
            code, result, stdout, _ = self._upload(yes=True)

        self.assertEqual(code, 1)
        assert result is not None
        self.assertEqual(result["status"], "invalid_results")
        self.assertEqual(post.posted, [])
        self.assertEqual(result["counts"]["rejected"], 2)
        self.assertEqual(result["counts"]["accepted"], 0)
        self.assertEqual(result["event_ids"], [])
        reasons = {row["reason"] for row in result["rejected_providers"]}
        self.assertEqual(reasons, {"adoption_result_invalid", "adoption_result_missing"})
        self.assertTrue(reasons <= release_campaigns.CAMPAIGN_UPLOAD_REJECT_CODES)
        self.assertIn("inspect stored qualification results", result["next_action"])
        # The rejection names providers and a bounded code -- never the
        # validator's message or any part of the stored result.
        self.assertNotIn("steps", stdout)

    def test_malformed_provider_list_is_a_bounded_rejection(self) -> None:
        """A `providers` value that is not a bounded list is refused, never iterated.

        A stored campaign file is untrusted input, so the converter must not
        assume `providers` is the list the tool writes. `null`, a scalar, a
        string, a mapping, or an oversized list each has to produce the
        documented bounded `invalid_results` summary -- not a TypeError, and not
        a per-element walk of an unbounded value.
        """
        self._seed(complete=("claude",))
        path = self.campaigns_dir / "campaign-v1.0.0.json"
        seeded = json.loads(path.read_text(encoding="utf-8"))
        oversized = [{"provider": "claude", "state": "queued"}] * (
            release_campaigns.MAX_CAMPAIGN_UPLOAD_PROVIDERS + 1
        )
        for label, providers in (
            ("null", None),
            ("integer", 7),
            ("string", "claude"),
            ("mapping", {"claude": {"state": "complete"}}),
            ("oversized", oversized),
        ):
            with self.subTest(providers=label):
                stored = dict(seeded)
                stored["providers"] = providers
                path.write_text(json.dumps(stored), encoding="utf-8")

                post = self._capturing_post()
                with self._cloud_env(self.FAKE_CREDENTIAL), mock.patch.object(
                    release_campaigns._load_cloud_client(), "post_upload_payload", post
                ):
                    code, result, _, stderr = self._upload(yes=True)

                self.assertEqual(code, 1, stderr)
                assert result is not None
                self.assertEqual(result["status"], "invalid_results")
                # Nothing left this machine, and no partial event set was built.
                self.assertEqual(post.posted, [])
                self.assertEqual(result["event_ids"], [])
                self.assertEqual(result["accepted_providers"], [])
                self.assertEqual(result["counts"]["events"], 0)
                self.assertEqual(result["counts"]["accepted"], 0)
                self.assertEqual(result["counts"]["rejected"], 1)
                reasons = {row["reason"] for row in result["rejected_providers"]}
                self.assertEqual(reasons, {"provider_list_invalid"})
                self.assertTrue(reasons <= release_campaigns.CAMPAIGN_UPLOAD_REJECT_CODES)

    def test_unhashable_provider_state_is_skipped_and_never_leaks(self) -> None:
        """A list or mapping `state` reads as unavailable, in preview and in apply.

        A stored state is untrusted: a hand-edited but valid JSON campaign can
        carry an unhashable value there, which a bare `state in
        VALID_PROVIDER_STATES` test raises `TypeError` on instead of answering
        `False`. Such a state is unrecognized like any other, so its provider is
        skipped -- while a genuinely complete sibling still uploads -- and the
        raw stored value reaches neither the summary nor the payload.
        """
        marker = "hand-edited-state-marker"
        path = self.campaigns_dir / "campaign-v1.0.0.json"
        for label, state in (
            ("list", ["complete", marker]),
            ("mapping", {"state": "complete", "note": marker}),
        ):
            with self.subTest(state=label):
                self._seed(complete=("claude",))
                stored = json.loads(path.read_text(encoding="utf-8"))
                for provider in stored["providers"]:
                    if provider["provider"] == "codex":
                        provider["state"] = state
                path.write_text(json.dumps(stored), encoding="utf-8")

                preview_post = self._capturing_post()
                with self._cloud_env(self.FAKE_CREDENTIAL), mock.patch.object(
                    release_campaigns._load_cloud_client(), "post_upload_payload", preview_post
                ):
                    preview_code, preview, preview_out, preview_err = self._upload()

                # Preview computes the same event set and posts nothing.
                self.assertEqual(preview_code, 0, preview_err)
                assert preview is not None
                self.assertEqual(preview["status"], "dry_run")
                self.assertEqual(preview_post.posted, [])

                post = self._capturing_post()
                with self._cloud_env(self.FAKE_CREDENTIAL), mock.patch.object(
                    release_campaigns._load_cloud_client(), "post_upload_payload", post
                ):
                    code, result, stdout, stderr = self._upload(yes=True)

                self.assertEqual(code, 0, stderr)
                assert result is not None
                self.assertEqual(result["status"], "uploaded")
                # The unusable state is not an error: the campaign's real
                # evidence still uploads, and only the malformed provider is
                # dropped.
                self.assertEqual(result["accepted_providers"], ["claude"])
                self.assertEqual(result["rejected_providers"], [])
                self.assertEqual(result["counts"]["events"], 1)
                self.assertEqual(preview["event_ids"], result["event_ids"])
                self.assertEqual(len(post.posted), 1)
                self.assertEqual(len(post.posted[0]["events"]), 1)

                skipped = {row["provider"]: row for row in result["skipped_providers"]}
                self.assertEqual(skipped["codex"]["state"], "unavailable")
                self.assertTrue(
                    skipped["codex"]["reason"] in release_campaigns.SAFE_ERROR_CODES
                    or skipped["codex"]["reason"] == ""
                )
                # Nothing derived from the hand-edited value is printed or posted.
                for text in (preview_out, preview_err, stdout, stderr, json.dumps(post.posted)):
                    self.assertNotIn(marker, text)

    def test_missing_token_refuses_the_network_upload(self) -> None:
        """--yes without a resolvable token fails closed with local remediation."""
        self._seed(complete=("claude",))
        post = self._capturing_post()
        with self._cloud_env(), mock.patch.object(
            release_campaigns._load_cloud_client(), "post_upload_payload", post
        ):
            code, result, _, stderr = self._upload(yes=True)

        self.assertEqual(code, 1)
        self.assertIsNone(result)
        self.assertEqual(post.posted, [])
        self.assertIn("CODE_MOWER_CLOUD_TOKEN", stderr)
        self.assertIn("code-mower cloud setup", stderr)

    def test_ambiguous_token_profiles_refuse_the_network_upload(self) -> None:
        """Several stored profiles select none of them, and say how to choose."""
        self._seed(complete=("claude",))
        for name in ("one", "two"):
            (self.token_dir / f"{name}.env").write_text(
                f"CODE_MOWER_CLOUD_TOKEN={self.FAKE_CREDENTIAL}\n", encoding="utf-8"
            )
        post = self._capturing_post()
        with self._cloud_env(), mock.patch.object(
            release_campaigns._load_cloud_client(), "post_upload_payload", post
        ):
            preview_code, preview, _, _ = self._upload()
            code, result, _, stderr = self._upload(yes=True)

        self.assertEqual(preview_code, 0)
        assert preview is not None
        self.assertEqual(preview["token_status"], "ambiguous")
        self.assertIn("--install-id", preview["next_action"])
        self.assertEqual(code, 1)
        self.assertIsNone(result)
        self.assertEqual(post.posted, [])
        self.assertIn("--install-id", stderr)
        self.assertNotIn(self.FAKE_CREDENTIAL, stderr)

    def test_network_failure_is_reported_without_endpoint_response_text(self) -> None:
        """A failed post is bounded: no server body, and a safe retry instruction."""
        self._seed(complete=("claude",))
        cloud = release_campaigns._load_cloud_client()

        def _failing_post(**_: Any) -> dict[str, Any]:
            raise cloud.CloudBundleError(
                "upload failed with HTTP 500: <html>internal-detail-abc123</html>"
            )

        with self._cloud_env(self.FAKE_CREDENTIAL), mock.patch.object(
            cloud, "post_upload_payload", _failing_post
        ):
            code, result, _, stderr = self._upload(yes=True)

        self.assertEqual(code, 1)
        self.assertIsNone(result)
        self.assertNotIn("internal-detail-abc123", stderr)
        self.assertNotIn("<html>", stderr)
        self.assertIn("did not complete", stderr)
        self.assertIn("idempotent", stderr)

    def test_upload_output_carries_no_secrets_or_local_paths(self) -> None:
        """Preview and applied output stay metadata-only and path-free."""
        self._seed(complete=("claude", "codex"))
        cloud = release_campaigns._load_cloud_client()
        post = self._capturing_post()
        with self._cloud_env(self.FAKE_CREDENTIAL), mock.patch.object(
            cloud, "post_upload_payload", post
        ):
            _, preview, preview_out, _ = self._upload()
            _, applied, applied_out, _ = self._upload(yes=True)

        assert preview is not None and applied is not None
        for rendered, payload in ((preview_out, preview), (applied_out, applied)):
            self.assertNotIn(self.FAKE_CREDENTIAL, rendered)
            self.assertNotIn(str(self.tmp), rendered)
            self.assertNotIn(str(Path.home()), rendered)
            # The same metadata-only validator the upload boundary uses.
            cloud.validate_metadata_payload(payload)
        posted = post.posted[0]
        cloud.validate_metadata_payload(posted)
        self.assertNotIn(self.FAKE_CREDENTIAL, json.dumps(posted))
        self.assertNotIn(str(self.tmp), json.dumps(posted))

    def test_upload_requires_an_existing_campaign(self) -> None:
        """Upload never falls through to creating a campaign it could publish."""
        with self._cloud_env():
            code, result, _, stderr = self._upload()

        self.assertEqual(code, 1)
        self.assertIsNone(result)
        self.assertIn("no existing campaign", stderr)
        self.assertEqual(sorted(self.campaigns_dir.glob("*.json")), [])

    def test_upload_refuses_conflicting_campaign_intents(self) -> None:
        """Upload states one intent: it never dispatches, retries, or records."""
        self._seed()
        for kwargs, expected in (
            ({"apply": True}, "--apply"),
            ({"retry_provider": "claude"}, "--retry-provider"),
            ({"record_result": Path("result.json")}, "--record-result"),
            ({"resume": True}, "--resume"),
            ({"status": True}, "read-only"),
        ):
            with self.subTest(kwargs=sorted(kwargs)), self._cloud_env():
                code, result, _, stderr = self._upload(**kwargs)
                self.assertEqual(code, 1)
                self.assertIsNone(result)
                self.assertIn(expected, stderr)

    def test_yes_outside_upload_is_refused_rather_than_ignored(self) -> None:
        """`--yes` authorizes an upload post and means nothing to another action."""
        self._seed()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), self._cloud_env():
            code = release_campaigns.campaign_command(
                action="resume",
                release_tag="v1.0.0",
                campaigns_dir=self.campaigns_dir,
                yes=True,
            )
        self.assertEqual(code, 1)
        self.assertIn("--yes", err.getvalue())
        self.assertIn("upload", err.getvalue())

    def test_cli_upload_action_previews_without_network(self) -> None:
        """End to end through the CLI parser: preview is the default."""
        self._seed(complete=("claude",))
        post = self._capturing_post()
        out = io.StringIO()
        with self._cloud_env(), mock.patch.object(
            release_campaigns._load_cloud_client(), "post_upload_payload", post
        ), contextlib.redirect_stdout(out):
            code = release_qualify.main(
                [
                    "campaign",
                    "upload",
                    "--release-tag",
                    "v1.0.0",
                    "--campaigns-dir",
                    str(self.campaigns_dir),
                    "--token-dir",
                    str(self.token_dir),
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(post.posted, [])
        rendered = out.getvalue()
        self.assertIn("Release Campaign Upload: v1.0.0", rendered)
        self.assertIn("Network: skipped (pass --yes to upload)", rendered)

    def test_cloud_dogfood_adoption_run_event_path_stays_compatible(self) -> None:
        """The existing `cloud dogfood --event adoption_run=...` route is unchanged.

        Both routes convert one adoption result through the same converter, so
        an operator who already uploads results file-by-file and one who uploads
        a whole campaign publish the same idempotent event id for the same
        evidence.
        """
        campaign = self._seed(complete=("claude",))
        cloud = release_campaigns._load_cloud_client()
        result_path = self.tmp / "adoption-result.json"
        result_path.write_text(
            json.dumps(_mock_adoption_result(provider="claude")), encoding="utf-8"
        )

        file_events = cloud.parse_event_args([f"adoption_run={result_path}"])
        with self._cloud_env():
            plan = release_campaigns.build_campaign_upload_events(campaign)

        self.assertEqual(len(file_events), 1)
        self.assertEqual(len(plan["events"]), 1)
        self.assertEqual(file_events[0]["event_id"], plan["events"][0]["event_id"])
        self.assertEqual(
            file_events[0]["dimensions"], plan["events"][0]["dimensions"]
        )


class FakeClock:
    """Monotonic fake clock for deterministic time and sleep control."""

    def __init__(self, start: float = 100.0) -> None:
        self.current = start
        self.sleep_calls: list[float] = []

    def time(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self.current += seconds


class CampaignWatchTests(unittest.TestCase):
    """Tests for bounded release campaign watch operation."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmp_dir.name)
        self.campaigns_dir = self.tmp / ".code-mower" / "campaigns"
        self.campaigns_dir.mkdir(parents=True, exist_ok=True)
        self.clock = FakeClock()

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def _seed_campaign(
        self,
        *,
        campaign_id: str = "campaign-v1.0.0",
        release_tag: str = "v1.0.0",
        status: str = "running",
        providers: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if providers is None:
            providers = [
                {
                    "provider": "claude",
                    "lane_id": "claude_code",
                    "driver": "local_cli",
                    "state": "running",
                    "environment": "local",
                    "elapsed_seconds": 10.0,
                    "idempotency_key": "idemp-claude",
                    "dispatch_mode": "applied",
                    "next_action": "wait for claude to finish",
                    "next_detail": "",
                },
                {
                    "provider": "codex",
                    "lane_id": "codex_cli",
                    "driver": "local_cli",
                    "state": "complete",
                    "environment": "local",
                    "elapsed_seconds": 15.0,
                    "idempotency_key": "idemp-codex",
                    "dispatch_mode": "applied",
                    "next_action": "none",
                    "next_detail": "",
                },
            ]
        c = {
            "schema": release_campaigns.CAMPAIGN_SCHEMA,
            "campaign_id": campaign_id,
            "release_tag": release_tag,
            "package_identity": "code-mower",
            "package_spec": f"code-mower=={release_tag.lstrip('v')}",
            "normalized_version": release_tag.lstrip("v"),
            "qualification_context": "cold_install",
            "starting_version": "",
            "repo_slug": "codemower/code-mower",
            "status": status,
            "dry_run": False,
            "elapsed_seconds": 25.0,
            "created_at": "2026-09-04T12:00:00Z",
            "updated_at": "2026-09-04T12:00:00Z",
            "next_action": "poll running providers",
            "next_detail": "",
            "providers": providers,
        }
        release_campaigns.save_campaign(c, self.campaigns_dir)
        return c

    def test_watch_no_change_suppression(self) -> None:
        """Polls that observe no change suppress output until a real state transition occurs."""
        self._seed_campaign()
        out = io.StringIO()
        err = io.StringIO()
        poll_count = 0

        def custom_sleep(seconds: float) -> None:
            nonlocal poll_count
            poll_count += 1
            self.clock.sleep(seconds)
            if poll_count >= 3:
                c = release_campaigns.load_campaign_by_id("campaign-v1.0.0", self.campaigns_dir)
                assert c is not None
                c["status"] = "complete"
                c["providers"][0]["state"] = "complete"
                c["providers"][0]["next_action"] = "none"
                release_campaigns.save_campaign(c, self.campaigns_dir)

        summary = release_campaigns.campaign_watch(
            campaign_id="campaign-v1.0.0",
            campaigns_dir=self.campaigns_dir,
            interval=10.0,
            timeout=60.0,
            stdout=out,
            stderr=err,
            time_fn=self.clock.time,
            sleep_fn=custom_sleep,
        )

        self.assertEqual(summary["stop_reason"], "complete")
        self.assertEqual(summary["polls"], 3)
        rendered = out.getvalue()
        self.assertEqual(rendered.count("Release Campaign: v1.0.0"), 1)
        self.assertEqual(rendered.count("Transition: claude running -> complete"), 1)
        self.assertEqual(rendered.count("Transition: campaign status running -> complete"), 1)
        self.assertEqual(rendered.count("Final result: complete"), 1)
        lines = [line.strip() for line in rendered.strip().split("\n") if line.strip()]
        transition_lines = [line for line in lines if line.startswith("Transition:")]
        self.assertEqual(len(transition_lines), 2)

    def test_watch_complete_stop(self) -> None:
        """Watch stops with stop_reason='complete' and exit 0 when all providers pass."""
        self._seed_campaign(
            status="complete",
            providers=[
                {
                    "provider": "claude",
                    "state": "complete",
                    "environment": "local",
                    "elapsed_seconds": 10.0,
                },
                {
                    "provider": "codex",
                    "state": "complete",
                    "environment": "local",
                    "elapsed_seconds": 15.0,
                },
            ],
        )
        out = io.StringIO()
        exit_code = release_campaigns.campaign_command(
            action="watch",
            campaign_id="campaign-v1.0.0",
            campaigns_dir=self.campaigns_dir,
            stdout=out,
            time_fn=self.clock.time,
            sleep_fn=self.clock.sleep,
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("Final result: complete", out.getvalue())

    def test_watch_blocked_stop_and_retry_guidance(self) -> None:
        """Watch stops distinctly for blocked status and emits actionable retry guidance."""
        self._seed_campaign()
        out = io.StringIO()

        def fail_on_poll(seconds: float) -> None:
            self.clock.sleep(seconds)
            c = release_campaigns.load_campaign_by_id("campaign-v1.0.0", self.campaigns_dir)
            assert c is not None
            c["status"] = "blocked"
            c["providers"][0]["state"] = "blocked"
            c["providers"][0]["attempted_at"] = "2026-09-04T12:00:00Z"
            c["providers"][0]["error"] = "doctor_failed"
            c["providers"][0]["next_action"] = "inspect claude qualification failures"
            release_campaigns.save_campaign(c, self.campaigns_dir)

        summary = release_campaigns.campaign_watch(
            campaign_id="campaign-v1.0.0",
            campaigns_dir=self.campaigns_dir,
            interval=5.0,
            timeout=30.0,
            stdout=out,
            time_fn=self.clock.time,
            sleep_fn=fail_on_poll,
        )
        self.assertEqual(summary["stop_reason"], "blocked")
        self.assertEqual(summary["status"], "blocked")
        self.assertIn("--retry-provider claude", summary["retry_guidance"])
        self.assertIn("--campaign-id campaign-v1.0.0", summary["retry_guidance"])
        self.assertNotIn("--release-tag", summary["retry_guidance"])
        rendered = out.getvalue()
        self.assertIn("Final result: blocked", rendered)
        self.assertIn("Retry guidance: inspect failures and retry with", rendered)
        self.assertIn("--retry-provider claude", rendered)

    def test_watch_owner_action_stop(self) -> None:
        """Watch stops with stop_reason='owner_action' when no providers are running but campaign is incomplete."""
        self._seed_campaign(
            status="pending",
            providers=[
                {
                    "provider": "claude",
                    "state": "complete",
                    "environment": "local",
                    "elapsed_seconds": 10.0,
                },
                {
                    "provider": "devin",
                    "state": "unavailable",
                    "environment": "hosted",
                    "elapsed_seconds": 0.0,
                    "next_action": "configure DEVIN_AUDIT_LABEL_TOKEN",
                },
            ],
        )
        out = io.StringIO()
        exit_code = release_campaigns.campaign_command(
            action="watch",
            campaign_id="campaign-v1.0.0",
            campaigns_dir=self.campaigns_dir,
            stdout=out,
            time_fn=self.clock.time,
            sleep_fn=self.clock.sleep,
        )
        self.assertEqual(exit_code, 1)
        self.assertIn("Final result: owner_action", out.getvalue())
        self.assertIn("resolve provider prerequisites", out.getvalue())

    def test_watch_timeout_stop(self) -> None:
        """Watch stops with stop_reason='timeout' when duration exceeds bounded timeout."""
        self._seed_campaign()
        out = io.StringIO()
        exit_code = release_campaigns.campaign_command(
            action="watch",
            campaign_id="campaign-v1.0.0",
            campaigns_dir=self.campaigns_dir,
            interval=10.0,
            timeout=35.0,
            stdout=out,
            time_fn=self.clock.time,
            sleep_fn=self.clock.sleep,
        )
        self.assertEqual(exit_code, 1)
        rendered = out.getvalue()
        self.assertIn("Final result: timeout", rendered)
        self.assertIn("Retry guidance: re-run 'code-mower release campaign watch", rendered)
        self.assertIn("--campaign-id campaign-v1.0.0", rendered)
        self.assertNotIn("--release-tag", rendered)
        self.assertEqual(len(self.clock.sleep_calls), 4)

    def test_github_poll_accepts_production_json_shape_and_safe_failure(self) -> None:
        comments, error = release_campaigns._poll_github_comments(
            "codemower/code-mower",
            123,
            gh_json_runner=lambda _args: {
                "comments": [{"body": "complete", "author": {"login": "trusted-bot"}}]
            },
        )
        self.assertEqual(error, "")
        self.assertEqual(comments[0]["body"], "complete")

        def unavailable(_args: Any) -> Any:
            raise release_campaigns.lane_status.LaneStatusUnavailable("private detail")

        comments, error = release_campaigns._poll_github_comments(
            "codemower/code-mower",
            123,
            gh_json_runner=unavailable,
        )
        self.assertEqual(comments, [])
        self.assertEqual(error, "github_poll_unavailable")

    def test_watch_bounds_production_github_calls_by_remaining_time(self) -> None:
        self._seed_campaign(
            providers=[
                {
                    "provider": "devin",
                    "lane_id": "fake_hosted",
                    "driver": "hosted_bridge",
                    "state": "running",
                    "environment": "hosted",
                    "elapsed_seconds": 0.0,
                    "dispatch_ref": {"issue_number": "123"},
                    "idempotency_key": "idemp-devin",
                }
            ]
        )
        with mock.patch.object(
            release_campaigns.lane_status,
            "_run_gh_json",
            return_value={"comments": []},
        ) as run_gh_json:
            summary = release_campaigns.campaign_watch(
                campaign_id="campaign-v1.0.0",
                campaigns_dir=self.campaigns_dir,
                issue="123",
                interval=10.0,
                timeout=20.0,
                emit_json=True,
                time_fn=self.clock.time,
                sleep_fn=self.clock.sleep,
            )

        self.assertEqual(summary["stop_reason"], "timeout")
        self.assertEqual(run_gh_json.call_count, 2)
        self.assertEqual(
            [call.kwargs["timeout"] for call in run_gh_json.call_args_list],
            [20.0, 10.0],
        )

    def test_watch_interrupt_stop(self) -> None:
        """Watch handles KeyboardInterrupt gracefully, stops with stop_reason='interrupt' and exit 130."""
        self._seed_campaign()
        out = io.StringIO()

        def raise_sigint(seconds: float) -> None:
            raise KeyboardInterrupt()

        exit_code = release_campaigns.campaign_command(
            action="watch",
            campaign_id="campaign-v1.0.0",
            campaigns_dir=self.campaigns_dir,
            interval=10.0,
            timeout=60.0,
            stdout=out,
            time_fn=self.clock.time,
            sleep_fn=raise_sigint,
        )
        self.assertEqual(exit_code, 130)
        rendered = out.getvalue()
        self.assertIn("Final result: interrupt", rendered)
        self.assertIn("re-run 'code-mower release campaign watch", rendered)

    def test_watch_slow_initial_poll(self) -> None:
        """A slow initial poll that reaches or exceeds timeout stops immediately without loop polling."""
        self._seed_campaign(
            providers=[
                {
                    "provider": "devin",
                    "lane_id": "fake_hosted",
                    "driver": "hosted_bridge",
                    "state": "running",
                    "environment": "hosted",
                    "elapsed_seconds": 10.0,
                    "dispatch_ref": {"issue_number": "123"},
                    "idempotency_key": "idemp-devin",
                }
            ]
        )
        out = io.StringIO()

        def slow_gh_json(*args: Any, **kwargs: Any) -> tuple[Any, str]:
            # Simulate a slow initial remote poll taking 25.0s (exceeding timeout of 20.0s)
            self.clock.current += 25.0
            return {"comments": []}, ""

        exit_code = release_campaigns.campaign_command(
            action="watch",
            campaign_id="campaign-v1.0.0",
            campaigns_dir=self.campaigns_dir,
            issue="123",
            interval=5.0,
            timeout=20.0,
            gh_json_runner=slow_gh_json,
            stdout=out,
            time_fn=self.clock.time,
            sleep_fn=self.clock.sleep,
        )
        self.assertEqual(exit_code, 1)
        rendered = out.getvalue()
        self.assertIn("Final result: timeout", rendered)
        self.assertIn("Retry guidance: re-run 'code-mower release campaign watch", rendered)
        self.assertEqual(len(self.clock.sleep_calls), 0)

        # Directly verify campaign_watch summary contract
        summary = release_campaigns.campaign_watch(
            campaign_id="campaign-v1.0.0",
            campaigns_dir=self.campaigns_dir,
            issue="123",
            interval=5.0,
            timeout=20.0,
            gh_json_runner=slow_gh_json,
            time_fn=self.clock.time,
            sleep_fn=self.clock.sleep,
        )
        self.assertEqual(summary["stop_reason"], "timeout")
        self.assertEqual(summary["polls"], 0)
        self.assertEqual(summary["elapsed_seconds"], 25.0)

    def test_watch_deadline_after_sleep(self) -> None:
        """Sleeping to the deadline still performs the final bounded poll."""
        self._seed_campaign(
            providers=[
                {
                    "provider": "devin",
                    "lane_id": "fake_hosted",
                    "driver": "hosted_bridge",
                    "state": "running",
                    "environment": "hosted",
                    "elapsed_seconds": 10.0,
                    "dispatch_ref": {"issue_number": "123"},
                    "idempotency_key": "idemp-devin",
                }
            ]
        )
        out = io.StringIO()
        poll_call_count = 0

        def counting_gh_json(*args: Any, **kwargs: Any) -> tuple[Any, str]:
            nonlocal poll_call_count
            poll_call_count += 1
            return {"comments": []}, ""

        exit_code = release_campaigns.campaign_command(
            action="watch",
            campaign_id="campaign-v1.0.0",
            campaigns_dir=self.campaigns_dir,
            issue="123",
            interval=10.0,
            timeout=20.0,
            gh_json_runner=counting_gh_json,
            stdout=out,
            time_fn=self.clock.time,
            sleep_fn=self.clock.sleep,
        )
        self.assertEqual(exit_code, 1)
        rendered = out.getvalue()
        self.assertIn("Final result: timeout", rendered)
        self.assertEqual(self.clock.sleep_calls, [10.0, 10.0])
        # Initial poll at t=0 plus one after each bounded sleep, including the
        # deadline, so completion in the final interval is observable.
        self.assertEqual(poll_call_count, 3)

    def test_watch_bounds_campaign_lock_by_remaining_time(self) -> None:
        self._seed_campaign()
        observed_timeouts: list[float] = []

        @contextlib.contextmanager
        def recording_lock(_campaigns_dir: Path, **kwargs: Any) -> Any:
            observed_timeouts.append(kwargs["timeout_seconds"])
            yield

        with mock.patch.object(
            release_campaigns,
            "locked_campaigns_dir",
            recording_lock,
        ):
            summary = release_campaigns.campaign_watch(
                campaign_id="campaign-v1.0.0",
                campaigns_dir=self.campaigns_dir,
                interval=10.0,
                timeout=20.0,
                emit_json=True,
                time_fn=self.clock.time,
                sleep_fn=self.clock.sleep,
            )

        self.assertEqual(summary["stop_reason"], "timeout")
        self.assertEqual(observed_timeouts, [20.0, 10.0, 0.0])

    def test_watch_lock_timeout_returns_stable_timeout_summary(self) -> None:
        self._seed_campaign()

        @contextlib.contextmanager
        def unavailable_lock(_campaigns_dir: Path, **_kwargs: Any) -> Any:
            raise release_campaigns.FileLockError("private lock detail")
            yield

        with mock.patch.object(
            release_campaigns,
            "locked_campaigns_dir",
            unavailable_lock,
        ):
            summary = release_campaigns.campaign_watch(
                campaign_id="campaign-v1.0.0",
                campaigns_dir=self.campaigns_dir,
                timeout=1.0,
                emit_json=True,
                time_fn=self.clock.time,
                sleep_fn=self.clock.sleep,
            )

        self.assertEqual(summary["stop_reason"], "timeout")
        self.assertNotIn("private lock detail", json.dumps(summary))

    def test_watch_storage_failure_returns_stable_invalid_summary(self) -> None:
        self._seed_campaign()

        @contextlib.contextmanager
        def broken_storage(_campaigns_dir: Path, **_kwargs: Any) -> Any:
            raise OSError("private /tmp/campaign path")
            yield

        with mock.patch.object(
            release_campaigns,
            "locked_campaigns_dir",
            broken_storage,
        ):
            summary = release_campaigns.campaign_watch(
                campaign_id="campaign-v1.0.0",
                campaigns_dir=self.campaigns_dir,
                emit_json=True,
                time_fn=self.clock.time,
                sleep_fn=self.clock.sleep,
            )

        self.assertEqual(summary["stop_reason"], "invalid_campaign")
        self.assertEqual(summary["error"], "campaign storage is unavailable")
        self.assertNotIn("/tmp/campaign", json.dumps(summary))

    def test_watch_interrupt_during_initial_lock_or_poll(self) -> None:
        """KeyboardInterrupt during initial lock or poll produces the same interrupt summary and exit 130."""
        self._seed_campaign(
            providers=[
                {
                    "provider": "devin",
                    "lane_id": "fake_hosted",
                    "driver": "hosted_bridge",
                    "state": "running",
                    "environment": "hosted",
                    "elapsed_seconds": 10.0,
                    "dispatch_ref": {"issue_number": "123"},
                    "idempotency_key": "idemp-devin",
                }
            ]
        )

        # 1. Interrupt during initial remote poll
        out_poll = io.StringIO()

        def interrupting_gh_json(*args: Any, **kwargs: Any) -> tuple[Any, str]:
            raise KeyboardInterrupt()

        exit_code = release_campaigns.campaign_command(
            action="watch",
            campaign_id="campaign-v1.0.0",
            campaigns_dir=self.campaigns_dir,
            issue="123",
            interval=10.0,
            timeout=60.0,
            gh_json_runner=interrupting_gh_json,
            stdout=out_poll,
            time_fn=self.clock.time,
            sleep_fn=self.clock.sleep,
        )
        self.assertEqual(exit_code, 130)
        rendered_poll = out_poll.getvalue()
        self.assertIn("Final result: interrupt", rendered_poll)
        self.assertIn("re-run 'code-mower release campaign watch", rendered_poll)

        summary = release_campaigns.campaign_watch(
            campaign_id="campaign-v1.0.0",
            campaigns_dir=self.campaigns_dir,
            issue="123",
            interval=10.0,
            timeout=60.0,
            gh_json_runner=interrupting_gh_json,
            time_fn=self.clock.time,
            sleep_fn=self.clock.sleep,
        )
        self.assertEqual(summary["stop_reason"], "interrupt")
        self.assertEqual(summary["polls"], 0)
        self.assertEqual(summary["mode"], "release-campaign-watch")
        self.assertEqual(summary["schema"], release_campaigns.CAMPAIGN_WATCH_SCHEMA)
        self.assertIn("re-run 'code-mower release campaign watch", summary["retry_guidance"])

        # 2. Interrupt during initial lock acquisition
        out_lock = io.StringIO()

        @contextlib.contextmanager
        def interrupting_lock(campaigns_dir: Path, **_kwargs: Any) -> Any:
            raise KeyboardInterrupt()
            yield

        with mock.patch.object(release_campaigns, "locked_campaigns_dir", interrupting_lock):
            exit_code_lock = release_campaigns.campaign_command(
                action="watch",
                campaign_id="campaign-v1.0.0",
                campaigns_dir=self.campaigns_dir,
                interval=10.0,
                timeout=60.0,
                stdout=out_lock,
                time_fn=self.clock.time,
                sleep_fn=self.clock.sleep,
            )
        self.assertEqual(exit_code_lock, 130)
        rendered_lock = out_lock.getvalue()
        self.assertIn("Final result: interrupt", rendered_lock)
        self.assertIn("re-run 'code-mower release campaign watch", rendered_lock)

    def test_watch_remote_unavailable_outage(self) -> None:
        """Watch stops distinctly with stop_reason='remote_unavailable' during remote provider outage."""
        self._seed_campaign(
            providers=[
                {
                    "provider": "devin",
                    "lane_id": "fake_hosted",
                    "driver": "hosted_bridge",
                    "state": "running",
                    "environment": "hosted",
                    "elapsed_seconds": 10.0,
                    "dispatch_ref": {"issue_number": "123"},
                    "idempotency_key": "idemp-devin",
                }
            ]
        )
        out = io.StringIO()

        def fail_gh_json(*args: Any, **kwargs: Any) -> tuple[Any, str]:
            return None, "network down"

        exit_code = release_campaigns.campaign_command(
            action="watch",
            campaign_id="campaign-v1.0.0",
            campaigns_dir=self.campaigns_dir,
            issue="123",
            gh_json_runner=fail_gh_json,
            stdout=out,
            time_fn=self.clock.time,
            sleep_fn=self.clock.sleep,
        )
        self.assertEqual(exit_code, 1)
        rendered = out.getvalue()
        self.assertIn("Final result: remote_unavailable", rendered)
        self.assertIn("verify GitHub connectivity", rendered)

    def test_watch_invalid_campaign_missing_and_ambiguous(self) -> None:
        """Watch stops distinctly with stop_reason='invalid_campaign' on missing or ambiguous campaigns."""
        out = io.StringIO()
        err = io.StringIO()
        exit_code = release_campaigns.campaign_command(
            action="watch",
            campaign_id="nonexistent-campaign",
            campaigns_dir=self.campaigns_dir,
            stdout=out,
            stderr=err,
        )
        self.assertEqual(exit_code, 1)
        self.assertIn("no campaign found", err.getvalue())

        self._seed_campaign(campaign_id="campaign-1", release_tag="v2.0.0")
        self._seed_campaign(campaign_id="campaign-2", release_tag="v2.0.0")
        out = io.StringIO()
        err = io.StringIO()
        exit_code = release_campaigns.campaign_command(
            action="watch",
            release_tag="v2.0.0",
            campaigns_dir=self.campaigns_dir,
            stdout=out,
            stderr=err,
        )
        self.assertEqual(exit_code, 1)
        self.assertIn("matches 2 campaigns", err.getvalue())

    def test_watch_invalid_campaign_malformed_id(self) -> None:
        """Watch stops with stop_reason='invalid_campaign' for invalid campaign ID."""
        out = io.StringIO()
        err = io.StringIO()
        exit_code = release_campaigns.campaign_command(
            action="watch",
            campaign_id="invalid/id/with/slashes",
            campaigns_dir=self.campaigns_dir,
            stdout=out,
            stderr=err,
        )
        self.assertEqual(exit_code, 1)
        self.assertIn("campaign_id must", err.getvalue())

    def test_watch_malformed_stored_payloads_emit_stable_json(self) -> None:
        malformed_path = self.campaigns_dir / "campaign-malformed.json"
        payloads = [
            {
                "schema": release_campaigns.CAMPAIGN_SCHEMA,
                "release_tag": "v1.0.0",
                "package_spec": "code-mower==1.0.0",
                "qualification_context": "cold_install",
                "starting_version": "",
                "providers": [],
            },
            {
                "schema": release_campaigns.CAMPAIGN_SCHEMA,
                "campaign_id": "campaign-malformed",
                "release_tag": "v1.0.0",
                "package_spec": "code-mower==1.0.0",
                "qualification_context": "cold_install",
                "starting_version": "",
                "providers": [None],
            },
            {
                "schema": release_campaigns.CAMPAIGN_SCHEMA,
                "campaign_id": "campaign-malformed",
                "release_tag": "v1.0.0",
                "package_spec": "code-mower==1.0.0",
                "qualification_context": "cold_install",
                "starting_version": "",
                "providers": [
                    {
                        "provider": "claude",
                        "state": "running",
                        "elapsed_seconds": "not-a-number",
                    }
                ],
            },
            {
                "schema": release_campaigns.CAMPAIGN_SCHEMA,
                "campaign_id": "campaign-malformed",
                "release_tag": "v1.0.0",
                "package_spec": "code-mower==1.0.0",
                "qualification_context": "cold_install",
                "starting_version": "",
                "providers": [
                    {
                        "provider": "claude",
                        "state": "running",
                        "elapsed_seconds": "5.0",
                    }
                ],
            },
        ]

        for payload in payloads:
            with self.subTest(payload=payload):
                malformed_path.write_text(json.dumps(payload), encoding="utf-8")
                out = io.StringIO()
                exit_code = release_campaigns.campaign_command(
                    action="watch",
                    campaign_id="campaign-malformed",
                    campaigns_dir=self.campaigns_dir,
                    emit_json=True,
                    stdout=out,
                )
                self.assertEqual(exit_code, 1)
                summary = json.loads(out.getvalue())
                self.assertEqual(summary["schema"], release_campaigns.CAMPAIGN_WATCH_SCHEMA)
                self.assertEqual(summary["status"], "invalid")
                self.assertEqual(summary["stop_reason"], "invalid_campaign")
                self.assertIn(
                    summary["error"],
                    {
                        "invalid campaign identity",
                        "invalid campaign provider collection",
                        "invalid campaign provider metrics",
                        "no campaign found for 'campaign-malformed'",
                    },
                )

        direct_summary = release_campaigns.campaign_watch(
            campaign=payloads[0],
            campaigns_dir=self.campaigns_dir,
            emit_json=True,
        )
        self.assertEqual(direct_summary["stop_reason"], "invalid_campaign")
        self.assertEqual(direct_summary["error"], "invalid campaign identity")

    def test_watch_repo_slug_is_effective_and_cannot_change_identity(self) -> None:
        campaign = self._seed_campaign(
            status="running",
            providers=[
                {
                    "provider": "cursor_bugbot",
                    "lane_id": "cursor_bugbot",
                    "driver": "hosted_bridge",
                    "state": "running",
                    "environment": "hosted",
                    "elapsed_seconds": 0.0,
                    "idempotency_key": "cursor-key",
                    "dispatch_mode": "applied",
                    "trigger_posted": False,
                    "dispatch_reconciliation_key": "dispatch-key",
                    "trigger_reconciliation_key": "trigger-key",
                    "dispatch_ref": {"issue_number": "42", "comment_posted": True},
                    "next_action": "poll cursor_bugbot remote progress marker",
                    "next_detail": "",
                }
            ],
        )
        campaign["repo_slug"] = ""
        release_campaigns.save_campaign(campaign, self.campaigns_dir)
        seen_repos: list[str] = []

        wrapper = {
            "schema": release_campaigns.RESULT_MARKER_SCHEMA,
            "campaign_id": campaign["campaign_id"],
            "provider": "cursor_bugbot",
            "release_tag": campaign["release_tag"],
            "idempotency_key": "cursor-key",
            "adoption_result": _mock_adoption_result(
                release_tag="v1.0.0",
                provider="cursor_bugbot",
                outcome="pass",
            ),
        }
        result_marker = (
            "<!-- CODE_MOWER_ADOPTION_RESULT: "
            f"{json.dumps(wrapper, sort_keys=True)} -->"
        )

        def gh_json(args: list[str], **kwargs: Any) -> tuple[dict[str, Any], str]:
            seen_repos.append("owner/repo" if "owner/repo" in args else "")
            return {
                "comments": [
                    {"author": {"login": "cursor[bot]"}, "body": result_marker}
                ]
            }, ""

        out = io.StringIO()
        summary = release_campaigns.campaign_watch(
            campaign_id="campaign-v1.0.0",
            campaigns_dir=self.campaigns_dir,
            repo_slug="owner/repo",
            interval=1.0,
            timeout=1.0,
            stdout=out,
            time_fn=self.clock.time,
            sleep_fn=self.clock.sleep,
            gh_json_runner=gh_json,
            env={"CURSOR_BUGBOT_AUDIT_LABEL_TOKEN": "token"},
        )

        self.assertEqual(summary["stop_reason"], "complete")
        self.assertTrue(
            any(
                transition.get("provider") == "cursor_bugbot"
                and transition.get("from_state") == "running"
                and transition.get("to_state") == "complete"
                for transition in summary["transitions"]
            )
        )
        self.assertIn("Transition: cursor_bugbot running -> complete", out.getvalue())
        self.assertEqual(seen_repos, ["owner/repo"])
        persisted = release_campaigns.load_campaign_by_id(
            "campaign-v1.0.0", self.campaigns_dir
        )
        assert persisted is not None
        self.assertEqual(persisted["repo_slug"], "")
        self.assertEqual(persisted["providers"][0]["state"], "complete")

        campaign["repo_slug"] = "owner/original"
        release_campaigns.save_campaign(campaign, self.campaigns_dir)
        conflict = release_campaigns.campaign_watch(
            campaign_id="campaign-v1.0.0",
            campaigns_dir=self.campaigns_dir,
            repo_slug="owner/different",
            emit_json=True,
        )
        self.assertEqual(conflict["stop_reason"], "invalid_campaign")
        self.assertEqual(
            conflict["error"], "requested repo slug does not match stored campaign"
        )

    def test_watch_does_not_create_reconciliation_state_for_older_campaign(self) -> None:
        campaign = self._seed_campaign(
            status="running",
            providers=[
                {
                    "provider": "cursor_bugbot",
                    "lane_id": "cursor_bugbot",
                    "driver": "hosted_bridge",
                    "state": "running",
                    "environment": "hosted",
                    "elapsed_seconds": 0.0,
                    "idempotency_key": "cursor-key",
                    "dispatch_mode": "applied",
                    "trigger_posted": False,
                    "dispatch_ref": {"issue_number": "42", "comment_posted": True},
                    "error": "",
                    "next_action": "poll cursor_bugbot remote progress marker",
                    "next_detail": "",
                }
            ],
        )
        before = json.loads(json.dumps(campaign))

        summary = release_campaigns.campaign_watch(
            campaign_id="campaign-v1.0.0",
            campaigns_dir=self.campaigns_dir,
            interval=1.0,
            timeout=1.0,
            emit_json=True,
            time_fn=self.clock.time,
            sleep_fn=self.clock.sleep,
            gh_json_runner=lambda _args: {"comments": []},
        )

        self.assertEqual(summary["stop_reason"], "timeout")
        persisted = release_campaigns.load_campaign_by_id(
            "campaign-v1.0.0", self.campaigns_dir
        )
        self.assertEqual(persisted, before)
        self.assertNotIn("dispatch_reconciliation_key", persisted["providers"][0])
        self.assertNotIn("trigger_reconciliation_key", persisted["providers"][0])

    def test_watch_polls_once_at_timeout_boundary(self) -> None:
        self._seed_campaign()

        def finish_during_sleep(seconds: float) -> None:
            self.clock.sleep(seconds)
            campaign = release_campaigns.load_campaign_by_id(
                "campaign-v1.0.0", self.campaigns_dir
            )
            assert campaign is not None
            campaign["status"] = "complete"
            campaign["providers"][0]["state"] = "complete"
            campaign["providers"][0]["next_action"] = "none"
            release_campaigns.save_campaign(campaign, self.campaigns_dir)

        summary = release_campaigns.campaign_watch(
            campaign_id="campaign-v1.0.0",
            campaigns_dir=self.campaigns_dir,
            interval=10.0,
            timeout=10.0,
            stdout=io.StringIO(),
            time_fn=self.clock.time,
            sleep_fn=finish_during_sleep,
        )

        self.assertEqual(summary["polls"], 1)
        self.assertEqual(summary["stop_reason"], "complete")

    def test_watch_positive_interval_and_timeout_validation(self) -> None:
        """Non-positive, non-numeric, or non-finite interval/timeout are rejected."""
        self._seed_campaign()
        for bad_interval in [-5.0, 0.0, float("nan"), float("inf"), True, False]:
            err = io.StringIO()
            summary = release_campaigns.campaign_watch(
                campaign_id="campaign-v1.0.0",
                campaigns_dir=self.campaigns_dir,
                interval=bad_interval,  # type: ignore[arg-type]
                timeout=60.0,
                stderr=err,
            )
            self.assertEqual(summary["stop_reason"], "invalid_campaign")
            self.assertIn("interval must be a positive number of seconds", err.getvalue())

        for bad_timeout in [-10.0, 0.0, float("nan"), float("inf"), True, False]:
            err = io.StringIO()
            summary = release_campaigns.campaign_watch(
                campaign_id="campaign-v1.0.0",
                campaigns_dir=self.campaigns_dir,
                interval=10.0,
                timeout=bad_timeout,  # type: ignore[arg-type]
                stderr=err,
            )
            self.assertEqual(summary["stop_reason"], "invalid_campaign")
            self.assertIn("timeout must be a positive number of seconds", err.getvalue())

    def test_watch_intent_conflicts(self) -> None:
        """Watch rejects conflicting mutating actions, and non-watch rejects --interval."""
        err = io.StringIO()
        res = release_campaigns.campaign_command(action="watch", apply=True, stderr=err)
        self.assertEqual(res, 1)
        self.assertIn("watch polls campaign progress without executing or mutating", err.getvalue())

        err = io.StringIO()
        res = release_campaigns.campaign_command(action="watch", yes=True, stderr=err)
        self.assertEqual(res, 1)
        self.assertIn("applies only to the 'upload' action", err.getvalue())

        err = io.StringIO()
        res = release_campaigns.campaign_command(
            action="watch", retry_provider="claude", stderr=err
        )
        self.assertEqual(res, 1)
        self.assertIn("cannot be combined with --retry-provider", err.getvalue())

        err = io.StringIO()
        res = release_campaigns.campaign_command(
            action="status", interval=5.0, stderr=err
        )
        self.assertEqual(res, 1)
        self.assertIn("--interval applies only to the 'watch' action", err.getvalue())

    def test_watch_json_mode_schema_and_metadata_only(self) -> None:
        """JSON mode emits exactly one metadata-only summary matching code_mower.releaseCampaignWatch.v1."""
        self._seed_campaign(
            status="complete",
            providers=[
                {
                    "provider": "claude",
                    "state": "complete",
                    "environment": "local",
                    "elapsed_seconds": 10.0,
                }
            ],
        )
        out = io.StringIO()
        exit_code = release_campaigns.campaign_command(
            action="watch",
            campaign_id="campaign-v1.0.0",
            campaigns_dir=self.campaigns_dir,
            emit_json=True,
            stdout=out,
            time_fn=self.clock.time,
            sleep_fn=self.clock.sleep,
        )
        self.assertEqual(exit_code, 0)
        parsed = json.loads(out.getvalue())
        self.assertEqual(parsed["schema"], release_campaigns.CAMPAIGN_WATCH_SCHEMA)
        self.assertEqual(parsed["mode"], "release-campaign-watch")
        self.assertEqual(parsed["campaign_id"], "campaign-v1.0.0")
        self.assertEqual(parsed["release_tag"], "v1.0.0")
        self.assertEqual(parsed["status"], "complete")
        self.assertEqual(parsed["stop_reason"], "complete")
        self.assertIsInstance(parsed["transitions"], list)
        self.assertIsInstance(parsed["providers"], list)
        self.assertIn("interval_seconds", parsed)
        self.assertIn("timeout_seconds", parsed)
        self.assertIn("elapsed_seconds", parsed)
        json_text = out.getvalue()
        self.assertNotIn(str(self.campaigns_dir), json_text)
        self.assertNotIn("TOKEN", json_text)

    def test_watch_idempotency_and_no_implicit_execution(self) -> None:
        """Watch preserves idempotency and never calls local adapters or dispatches comments."""
        self._seed_campaign(
            providers=[
                {
                    "provider": "claude",
                    "state": "running",
                    "environment": "local",
                    "elapsed_seconds": 5.0,
                    "attempted_at": "2026-09-04T12:00:00Z",
                    "idempotency_key": "idemp-claude",
                }
            ]
        )
        adapter_calls: list[Any] = []
        cmd_calls: list[Any] = []

        def fake_adapter(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            adapter_calls.append(args)
            return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

        def fake_cmd(*args: Any, **kwargs: Any) -> tuple[int, str, str]:
            cmd_calls.append(args)
            return 0, "", ""

        polls = 0

        def sleep_two(seconds: float) -> None:
            nonlocal polls
            polls += 1
            self.clock.sleep(seconds)
            if polls >= 2:
                c = release_campaigns.load_campaign_by_id("campaign-v1.0.0", self.campaigns_dir)
                assert c is not None
                c["status"] = "complete"
                c["providers"][0]["state"] = "complete"
                release_campaigns.save_campaign(c, self.campaigns_dir)

        summary = release_campaigns.campaign_watch(
            campaign_id="campaign-v1.0.0",
            campaigns_dir=self.campaigns_dir,
            interval=10.0,
            timeout=60.0,
            adapter_runner=fake_adapter,
            command_runner=fake_cmd,
            time_fn=self.clock.time,
            sleep_fn=sleep_two,
            stdout=io.StringIO(),
        )
        self.assertEqual(summary["stop_reason"], "complete")
        self.assertEqual(adapter_calls, [])
        self.assertEqual(cmd_calls, [])
        reloaded = release_campaigns.load_campaign_by_id("campaign-v1.0.0", self.campaigns_dir)
        assert reloaded is not None
        self.assertEqual(reloaded["providers"][0]["attempted_at"], "2026-09-04T12:00:00Z")

    def test_watch_via_release_qualify_main_cli(self) -> None:
        """release_qualify CLI dispatches watch command correctly."""
        self._seed_campaign(
            status="complete",
            providers=[
                {
                    "provider": "claude",
                    "state": "complete",
                    "environment": "local",
                    "elapsed_seconds": 10.0,
                }
            ],
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = release_qualify.main(
                [
                    "campaign",
                    "watch",
                    "--release-tag",
                    "v1.0.0",
                    "--campaigns-dir",
                    str(self.campaigns_dir),
                    "--json",
                ]
            )
        self.assertEqual(code, 0)
        parsed = json.loads(out.getvalue())
        self.assertEqual(parsed["schema"], release_campaigns.CAMPAIGN_WATCH_SCHEMA)
        self.assertEqual(parsed["stop_reason"], "complete")

    def test_watch_in_memory_publication_under_lock_and_concurrency(self) -> None:
        """watch_release_campaign publishes in-memory campaign under directory lock and re-checks existence."""
        campaign_data = {
            "schema": release_campaigns.CAMPAIGN_SCHEMA,
            "campaign_id": "campaign-in-memory",
            "release_tag": "v1.0.0",
            "package_spec": "code-mower==1.0.0",
            "qualification_context": "cold_install",
            "status": "complete",
            "providers": [
                {
                    "provider": "claude",
                    "lane_id": "lane-claude",
                    "state": "complete",
                    "environment": "local",
                    "elapsed_seconds": 5.0,
                }
            ],
        }

        # 1. Lock assertion: verify directory lock is held when save_campaign is invoked
        lock_held_during_save = False
        is_locked = False
        real_lock = release_campaigns.locked_campaigns_dir
        real_save = release_campaigns.save_campaign

        @contextlib.contextmanager
        def tracking_lock(dir_path: Path, **kwargs: Any) -> Any:
            nonlocal is_locked
            with real_lock(dir_path, **kwargs) as lock_file:
                is_locked = True
                try:
                    yield lock_file
                finally:
                    is_locked = False

        def tracking_save(campaign: Any, dir_path: Path) -> Path:
            nonlocal lock_held_during_save
            lock_held_during_save = is_locked
            return real_save(campaign, dir_path)

        target_file = self.campaigns_dir / release_campaigns.campaign_filename("campaign-in-memory")
        self.assertFalse(target_file.is_file())

        with (
            mock.patch.object(release_campaigns, "locked_campaigns_dir", tracking_lock),
            mock.patch.object(release_campaigns, "save_campaign", tracking_save),
        ):
            summary = release_campaigns.campaign_watch(
                campaign=campaign_data,
                campaigns_dir=self.campaigns_dir,
                stdout=io.StringIO(),
            )

        self.assertTrue(lock_held_during_save)
        self.assertTrue(target_file.is_file())
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["stop_reason"], "complete")

        # 2. Concurrency re-check assertion:
        # If another watcher/writer already created the target file before this watcher enters the lock,
        # re-checking inside the lock ensures save_campaign is NOT called again.
        save_calls = 0

        def counting_save(campaign: Any, dir_path: Path) -> Path:
            nonlocal save_calls
            save_calls += 1
            return real_save(campaign, dir_path)

        with (
            mock.patch.object(release_campaigns, "save_campaign", counting_save),
            mock.patch.object(
                release_campaigns,
                "dispatch_or_advance_campaign",
                side_effect=lambda c, **kwargs: dict(c),
            ),
        ):
            self.assertTrue(target_file.is_file())
            summary2 = release_campaigns.campaign_watch(
                campaign=campaign_data,
                campaigns_dir=self.campaigns_dir,
                stdout=io.StringIO(),
            )
            self.assertEqual(save_calls, 0)
            self.assertEqual(summary2["stop_reason"], "complete")

        # 3. Concurrent watcher threads racing with in-memory campaign:
        concurrent_cid = "campaign-concurrent-race"
        concurrent_file = self.campaigns_dir / release_campaigns.campaign_filename(concurrent_cid)
        concurrent_data = dict(campaign_data)
        concurrent_data["campaign_id"] = concurrent_cid
        self.assertFalse(concurrent_file.is_file())

        concurrent_saves = 0
        save_lock = threading.Lock()

        def thread_safe_counting_save(campaign: Any, dir_path: Path) -> Path:
            nonlocal concurrent_saves
            with save_lock:
                concurrent_saves += 1
            return real_save(campaign, dir_path)

        errors: list[Exception] = []
        results: list[dict[str, Any]] = []

        def run_watcher() -> None:
            try:
                res = release_campaigns.campaign_watch(
                    campaign=dict(concurrent_data),
                    campaigns_dir=self.campaigns_dir,
                    stdout=io.StringIO(),
                )
                results.append(res)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=run_watcher) for _ in range(5)]
        with (
            mock.patch.object(release_campaigns, "save_campaign", thread_safe_counting_save),
            mock.patch.object(
                release_campaigns,
                "dispatch_or_advance_campaign",
                side_effect=lambda c, **kwargs: dict(c),
            ),
        ):
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 5)
        self.assertTrue(concurrent_file.is_file())
        # Exactly one watcher published the campaign; all other concurrent watchers observed existence inside the lock
        self.assertEqual(concurrent_saves, 1)
        for r in results:
            self.assertEqual(r["stop_reason"], "complete")

    def test_option_scope_enforcement_and_no_mutation(self) -> None:
        """--interval is valid only for watch; --timeout is valid only for watch and upload.

        Actions where options are out of scope reject them with a bounded non-zero error
        and perform no campaign mutation.
        """
        # Seed an existing campaign
        self._seed_campaign(campaign_id="campaign-v1.0.0", release_tag="v1.0.0", status="running")
        campaign_path = self.campaigns_dir / release_campaigns.campaign_filename("campaign-v1.0.0")
        initial_stat = campaign_path.stat()
        initial_content = campaign_path.read_text(encoding="utf-8")

        # 1. Non-watch actions reject --interval
        for act in ["status", "create", "resume", "dispatch", "upload"]:
            err = io.StringIO()
            res = release_campaigns.campaign_command(
                action=act,
                campaign_id="campaign-v1.0.0",
                release_tag="v1.0.0",
                campaigns_dir=self.campaigns_dir,
                interval=5.0,
                stderr=err,
            )
            self.assertEqual(res, 1)
            self.assertIn("--interval applies only to the 'watch' action", err.getvalue())

        # Action omitted with --interval
        err = io.StringIO()
        res = release_campaigns.campaign_command(
            campaign_id="campaign-v1.0.0",
            campaigns_dir=self.campaigns_dir,
            interval=5.0,
            stderr=err,
        )
        self.assertEqual(res, 1)
        self.assertIn("--interval applies only to the 'watch' action", err.getvalue())

        # Legacy flags with --interval
        for legacy_kwarg in [{"status": True}, {"resume": True}]:
            err = io.StringIO()
            res = release_campaigns.campaign_command(
                campaign_id="campaign-v1.0.0",
                campaigns_dir=self.campaigns_dir,
                interval=5.0,
                stderr=err,
                **legacy_kwarg,
            )
            self.assertEqual(res, 1)
            self.assertIn("--interval applies only to the 'watch' action", err.getvalue())

        # 2. Actions other than watch and upload reject --timeout
        for act in ["status", "create", "resume", "dispatch"]:
            err = io.StringIO()
            res = release_campaigns.campaign_command(
                action=act,
                campaign_id="campaign-v1.0.0",
                release_tag="v1.0.0",
                campaigns_dir=self.campaigns_dir,
                timeout=30.0,
                stderr=err,
            )
            self.assertEqual(res, 1)
            self.assertIn("--timeout applies only to the 'watch' and 'upload' actions", err.getvalue())

        # Action omitted with --timeout
        err = io.StringIO()
        res = release_campaigns.campaign_command(
            campaign_id="campaign-v1.0.0",
            campaigns_dir=self.campaigns_dir,
            timeout=30.0,
            stderr=err,
        )
        self.assertEqual(res, 1)
        self.assertIn("--timeout applies only to the 'watch' and 'upload' actions", err.getvalue())

        # Legacy flags with --timeout
        for legacy_kwarg in [{"status": True}, {"resume": True}]:
            err = io.StringIO()
            res = release_campaigns.campaign_command(
                campaign_id="campaign-v1.0.0",
                campaigns_dir=self.campaigns_dir,
                timeout=30.0,
                stderr=err,
                **legacy_kwarg,
            )
            self.assertEqual(res, 1)
            self.assertIn("--timeout applies only to the 'watch' and 'upload' actions", err.getvalue())

        # 3. Assert NO campaign mutation occurred on disk
        self.assertEqual(campaign_path.read_text(encoding="utf-8"), initial_content)
        current_stat = campaign_path.stat()
        self.assertEqual(current_stat.st_mtime_ns, initial_stat.st_mtime_ns)

        # 4. Valid actions accept options
        # watch accepts both --interval and --timeout
        out = io.StringIO()
        res = release_campaigns.campaign_command(
            action="watch",
            campaign_id="campaign-v1.0.0",
            campaigns_dir=self.campaigns_dir,
            interval=5.0,
            timeout=10.0,
            stdout=out,
            time_fn=self.clock.time,
            sleep_fn=self.clock.sleep,
        )
        self.assertEqual(res, 1)  # stops with timeout because still running
        self.assertIn("Final result: timeout", out.getvalue())

        # upload accepts --timeout (preview mode by default, without --yes)
        self._seed_campaign(
            campaign_id="campaign-upload-test",
            release_tag="v1.0.0",
            status="running",
            providers=[
                {
                    "provider": "claude",
                    "lane_id": "lane-claude",
                    "state": "running",
                    "environment": "local",
                    "elapsed_seconds": 1.0,
                }
            ],
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            res = release_campaigns.campaign_command(
                action="upload",
                campaign_id="campaign-upload-test",
                campaigns_dir=self.campaigns_dir,
                timeout=45.0,
            )
        self.assertEqual(res, 0)
        self.assertIn("Release Campaign Upload", out.getvalue())

    def test_cli_option_scope_enforcement(self) -> None:
        """CLI enforces option scope for --interval and --timeout with bounded errors."""
        self._seed_campaign(campaign_id="campaign-v1.0.0", release_tag="v1.0.0")

        # --interval on status
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = release_qualify.main(
                [
                    "campaign",
                    "status",
                    "--release-tag",
                    "v1.0.0",
                    "--campaigns-dir",
                    str(self.campaigns_dir),
                    "--interval",
                    "5.0",
                ]
            )
        self.assertEqual(code, 1)
        self.assertIn("--interval applies only to the 'watch' action", err.getvalue())

        # --timeout on status
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = release_qualify.main(
                [
                    "campaign",
                    "status",
                    "--release-tag",
                    "v1.0.0",
                    "--campaigns-dir",
                    str(self.campaigns_dir),
                    "--timeout",
                    "30.0",
                ]
            )
        self.assertEqual(code, 1)
        self.assertIn("--timeout applies only to the 'watch' and 'upload' actions", err.getvalue())

        # --timeout on create
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = release_qualify.main(
                [
                    "campaign",
                    "create",
                    "--release-tag",
                    "v2.0.0",
                    "--package-spec",
                    "code-mower==2.0.0",
                    "--campaigns-dir",
                    str(self.campaigns_dir),
                    "--timeout",
                    "30.0",
                ]
            )
        self.assertEqual(code, 1)
        self.assertIn("--timeout applies only to the 'watch' and 'upload' actions", err.getvalue())

        # --interval on upload
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = release_qualify.main(
                [
                    "campaign",
                    "upload",
                    "--release-tag",
                    "v1.0.0",
                    "--campaigns-dir",
                    str(self.campaigns_dir),
                    "--interval",
                    "5.0",
                ]
            )
        self.assertEqual(code, 1)
        self.assertIn("--interval applies only to the 'watch' action", err.getvalue())

        # --timeout on omitted action (implicit create/advance)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = release_qualify.main(
                [
                    "campaign",
                    "--release-tag",
                    "v1.0.0",
                    "--campaigns-dir",
                    str(self.campaigns_dir),
                    "--timeout",
                    "30.0",
                ]
            )
        self.assertEqual(code, 1)
        self.assertIn("--timeout applies only to the 'watch' and 'upload' actions", err.getvalue())

        # --timeout on legacy --status flag
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = release_qualify.main(
                [
                    "campaign",
                    "--status",
                    "--release-tag",
                    "v1.0.0",
                    "--campaigns-dir",
                    str(self.campaigns_dir),
                    "--timeout",
                    "30.0",
                ]
            )
        self.assertEqual(code, 1)
        self.assertIn("--timeout applies only to the 'watch' and 'upload' actions", err.getvalue())


class CheckpointedLocalAdapterBoardProjectionTests(unittest.TestCase):
    """Board projects in-flight checkpointed local_cli adapters as running.

    While an applied local adapter process is actively working, its campaign
    state is checkpointed on disk with attempted_at set, but its persisted state
    remains queued until the adapter process completes. The read-only Board
    projection renders such providers as running, while preserving stored
    campaign state, retry semantics, untouched dry-run previews, and terminal
    outcomes.
    """

    def test_checkpointed_local_cli_adapter_renders_running_on_board(self) -> None:
        """A genuinely fresh local_cli provider checkpointed with attempted_at renders as running."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            campaign = release_campaigns.initialize_campaign(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["codex"],
            ).to_dict()
            now_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            provider_entry = campaign["providers"][0]
            provider_entry["attempted_at"] = now_utc
            provider_entry["dispatch_mode"] = "applied"
            provider_entry["state"] = "queued"
            release_campaigns.save_campaign(campaign, campaigns_dir)

            payload = release_campaigns.release_campaigns_board_payload(
                campaigns_dir=campaigns_dir
            )
            self.assertEqual(payload["card_count"], 1)
            card = payload["campaigns"][0]["cards"][0]
            self.assertEqual(card["provider"], "codex")
            self.assertEqual(card["state"], "running")

            # Persisted state on disk is never mutated by the read-only Board projection
            stored = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert stored is not None
            self.assertEqual(stored["providers"][0]["state"], "queued")
            self.assertEqual(stored["providers"][0]["attempted_at"], now_utc)

    def test_untouched_dry_run_queued_provider_renders_queued_on_board(self) -> None:
        """A dry-run queued provider without attempted_at stays queued on Board."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            campaign = release_campaigns.initialize_campaign(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["codex"],
            ).to_dict()
            release_campaigns.save_campaign(campaign, campaigns_dir)

            payload = release_campaigns.release_campaigns_board_payload(
                campaigns_dir=campaigns_dir
            )
            card = payload["campaigns"][0]["cards"][0]
            self.assertEqual(card["provider"], "codex")
            self.assertEqual(card["state"], "queued")

    def test_attempted_terminal_states_are_not_reinterpreted_as_running(self) -> None:
        """Terminal blocked and unavailable attempts preserve their stored state."""
        for terminal_state in ("blocked", "unavailable"):
            with self.subTest(state=terminal_state):
                with tempfile.TemporaryDirectory() as tmp:
                    campaigns_dir = Path(tmp) / "campaigns"
                    campaign = release_campaigns.initialize_campaign(
                        release_tag="v1.0.0",
                        package_spec="code-mower==1.0.0",
                        providers=["codex"],
                    ).to_dict()
                    provider_entry = campaign["providers"][0]
                    provider_entry["attempted_at"] = "2026-09-04T19:00:00Z"
                    provider_entry["dispatch_mode"] = "applied"
                    provider_entry["state"] = terminal_state
                    release_campaigns.save_campaign(campaign, campaigns_dir)

                    payload = release_campaigns.release_campaigns_board_payload(
                        campaigns_dir=campaigns_dir
                    )
                    card = payload["campaigns"][0]["cards"][0]
                    self.assertEqual(card["state"], terminal_state)

    def test_recorded_completion_evidence_prevents_running_projection(self) -> None:
        """A provider with recorded completion evidence stays queued."""
        # Case A: completed_at set
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            campaign = release_campaigns.initialize_campaign(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["codex"],
            ).to_dict()
            provider_entry = campaign["providers"][0]
            provider_entry["attempted_at"] = "2026-09-04T19:00:00Z"
            provider_entry["completed_at"] = "2026-09-04T19:01:00Z"
            provider_entry["state"] = "queued"
            release_campaigns.save_campaign(campaign, campaigns_dir)

            payload = release_campaigns.release_campaigns_board_payload(
                campaigns_dir=campaigns_dir
            )
            card = payload["campaigns"][0]["cards"][0]
            self.assertEqual(card["state"], "queued")

        # Case B: adoption_result set
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            campaign = release_campaigns.initialize_campaign(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["codex"],
            ).to_dict()
            provider_entry = campaign["providers"][0]
            provider_entry["attempted_at"] = "2026-09-04T19:00:00Z"
            provider_entry["adoption_result"] = {"outcome": "pass"}
            provider_entry["state"] = "queued"
            release_campaigns.save_campaign(campaign, campaigns_dir)

            payload = release_campaigns.release_campaigns_board_payload(
                campaigns_dir=campaigns_dir
            )
            card = payload["campaigns"][0]["cards"][0]
            self.assertEqual(card["state"], "queued")

    def test_unconsumed_result_file_does_not_hide_running_adapter(self) -> None:
        """A result file is not completion evidence until the adapter exits and records it."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            results_dir = campaigns_dir / "results"
            results_dir.mkdir(parents=True, exist_ok=True)
            campaign = release_campaigns.initialize_campaign(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["codex"],
            ).to_dict()
            provider_entry = campaign["providers"][0]
            provider_entry["attempted_at"] = "2026-09-04T19:00:00Z"
            provider_entry["state"] = "queued"
            release_campaigns.save_campaign(campaign, campaigns_dir)

            result_file = results_dir / f"{campaign['campaign_id']}_codex.json"
            result_file.write_text('{"outcome": "pass"}', encoding="utf-8")

            payload = release_campaigns.release_campaigns_board_payload(
                campaigns_dir=campaigns_dir,
                now="2026-09-04T19:05:00Z",
            )
            card = payload["campaigns"][0]["cards"][0]
            self.assertEqual(card["state"], "running")
            self.assertEqual(card["next_action"], "wait for codex local adapter")
            self.assertIn("900s timeout window", card["next_detail"])

    def test_non_local_cli_provider_with_queued_state_is_not_projected_as_running(self) -> None:
        """Providers that are not local_cli (e.g. hosted_bridge) are not affected."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            campaign = release_campaigns.initialize_campaign(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["devin"],
            ).to_dict()
            provider_entry = campaign["providers"][0]
            provider_entry["attempted_at"] = "2026-09-04T19:00:00Z"
            provider_entry["state"] = "queued"
            release_campaigns.save_campaign(campaign, campaigns_dir)

            payload = release_campaigns.release_campaigns_board_payload(
                campaigns_dir=campaigns_dir
            )
            card = payload["campaigns"][0]["cards"][0]
            self.assertEqual(card["provider"], "devin")
            self.assertEqual(card["state"], "queued")

    def test_board_release_campaigns_payload_projects_running_card(self) -> None:
        """board.release_campaigns_payload surfaces the running card projection."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            campaigns_dir = repo_path / ".code-mower" / "campaigns"
            campaign = release_campaigns.initialize_campaign(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["codex"],
            ).to_dict()
            now_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            provider_entry = campaign["providers"][0]
            provider_entry["attempted_at"] = now_utc
            provider_entry["dispatch_mode"] = "applied"
            provider_entry["state"] = "queued"
            release_campaigns.save_campaign(campaign, campaigns_dir)

            cfg = board.BoardConfig(repo="owner/repo", repo_path=str(repo_path))
            payload = board.release_campaigns_payload(cfg)
            card = payload["campaigns"][0]["cards"][0]
            self.assertEqual(card["provider"], "codex")
            self.assertEqual(card["state"], "running")

    def test_fresh_in_flight_adapter_within_timeout_renders_running(self) -> None:
        """A checkpointed in-flight adapter within effective timeout projects as running."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            campaign = release_campaigns.initialize_campaign(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["codex"],
            ).to_dict()
            provider_entry = campaign["providers"][0]
            provider_entry["attempted_at"] = "2026-09-04T19:00:00Z"
            provider_entry["dispatch_mode"] = "applied"
            provider_entry["state"] = "queued"
            provider_entry["next_action"] = "poll codex local process"
            provider_entry["next_detail"] = "codex local adapter is still running"
            release_campaigns.save_campaign(campaign, campaigns_dir)

            # 300 seconds elapsed (within 900s default timeout)
            payload = release_campaigns.release_campaigns_board_payload(
                campaigns_dir=campaigns_dir,
                now="2026-09-04T19:05:00Z",
            )
            card = payload["campaigns"][0]["cards"][0]
            self.assertEqual(card["provider"], "codex")
            self.assertEqual(card["state"], "running")

    def test_stale_checkpointed_adapter_exceeding_timeout_renders_queued_with_retry_guidance(self) -> None:
        """A stale checkpointed adapter exceeding timeout projects as queued with retry guidance."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            campaign = release_campaigns.initialize_campaign(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["codex"],
            ).to_dict()
            provider_entry = campaign["providers"][0]
            provider_entry["attempted_at"] = "2026-09-04T19:00:00Z"
            provider_entry["dispatch_mode"] = "applied"
            provider_entry["state"] = "queued"
            release_campaigns.save_campaign(campaign, campaigns_dir)

            # 901 seconds elapsed (exceeds 900s default timeout)
            payload = release_campaigns.release_campaigns_board_payload(
                campaigns_dir=campaigns_dir,
                now="2026-09-04T19:15:01Z",
            )
            projected_campaign = payload["campaigns"][0]
            card = projected_campaign["cards"][0]
            self.assertEqual(card["provider"], "codex")
            self.assertEqual(card["state"], "queued")
            self.assertEqual(card["next_action"], "use --retry-provider codex to retry codex")
            self.assertIn("900s timeout", card["next_detail"])
            self.assertNotIn("still running", card["next_detail"])
            self.assertEqual(projected_campaign["status"], "queued")
            self.assertEqual(projected_campaign["next_action"], card["next_action"])
            self.assertEqual(projected_campaign["next_detail"], card["next_detail"])
            self.assertEqual(payload["next_action"], card["next_action"])

            # Persisted state on disk remains untouched
            stored = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert stored is not None
            self.assertEqual(stored["providers"][0]["state"], "queued")
            self.assertEqual(stored["providers"][0]["attempted_at"], "2026-09-04T19:00:00Z")

    def test_boundary_projection_at_exact_timeout(self) -> None:
        """At exact timeout the adapter is still plausibly live; at timeout+1 it is stale."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            campaign = release_campaigns.initialize_campaign(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["codex"],
            ).to_dict()
            provider_entry = campaign["providers"][0]
            provider_entry["attempted_at"] = "2026-09-04T19:00:00Z"
            provider_entry["dispatch_mode"] = "applied"
            provider_entry["state"] = "queued"
            release_campaigns.save_campaign(campaign, campaigns_dir)

            # Exactly 900.0s elapsed -> running
            payload_boundary = release_campaigns.release_campaigns_board_payload(
                campaigns_dir=campaigns_dir,
                now="2026-09-04T19:15:00Z",
            )
            card_boundary = payload_boundary["campaigns"][0]["cards"][0]
            self.assertEqual(card_boundary["state"], "running")

            # 901.0s elapsed -> queued with retry guidance
            payload_past = release_campaigns.release_campaigns_board_payload(
                campaigns_dir=campaigns_dir,
                now="2026-09-04T19:15:01Z",
            )
            card_past = payload_past["campaigns"][0]["cards"][0]
            self.assertEqual(card_past["state"], "queued")
            self.assertEqual(card_past["next_action"], "use --retry-provider codex to retry codex")
            self.assertIn("900s timeout", card_past["next_detail"])

    def test_valid_repository_config_override_bounds_projection(self) -> None:
        """A valid repository code-mower.yml timeout override bounds the live window."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            campaigns_dir = repo_path / ".code-mower" / "campaigns"
            config_file = repo_path / "code-mower.yml"
            config_file.write_text(
                "lanes:\n"
                "  codex:\n"
                "    provider_config:\n"
                "      campaign_adapter_timeout_seconds: 120\n",
                encoding="utf-8",
            )

            campaign = release_campaigns.initialize_campaign(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["codex"],
            ).to_dict()
            provider_entry = campaign["providers"][0]
            provider_entry["attempted_at"] = "2026-09-04T19:00:00Z"
            provider_entry["dispatch_mode"] = "applied"
            provider_entry["state"] = "queued"
            release_campaigns.save_campaign(campaign, campaigns_dir)

            # 100 seconds elapsed (< 120s override) -> running
            payload_fresh = release_campaigns.release_campaigns_board_payload(
                repo_path=repo_path,
                campaigns_dir=campaigns_dir,
                now="2026-09-04T19:01:40Z",
            )
            card_fresh = payload_fresh["campaigns"][0]["cards"][0]
            self.assertEqual(card_fresh["state"], "running")

            # 121 seconds elapsed (> 120s override) -> queued with retry guidance
            payload_stale = release_campaigns.release_campaigns_board_payload(
                repo_path=repo_path,
                campaigns_dir=campaigns_dir,
                now="2026-09-04T19:02:01Z",
            )
            card_stale = payload_stale["campaigns"][0]["cards"][0]
            self.assertEqual(card_stale["state"], "queued")
            self.assertEqual(card_stale["next_action"], "use --retry-provider codex to retry codex")
            self.assertIn("120s timeout", card_stale["next_detail"])

    def test_malformed_or_invalid_repository_config_degrades_safely(self) -> None:
        """Malformed or invalid repo configs degrade safely to default timeout without crashing."""
        invalid_configs = [
            # Non-integer string
            "lanes:\n  codex:\n    provider_config:\n      campaign_adapter_timeout_seconds: not_a_number\n",
            # Negative integer
            "lanes:\n  codex:\n    provider_config:\n      campaign_adapter_timeout_seconds: -60\n",
            # Boolean
            "lanes:\n  codex:\n    provider_config:\n      campaign_adapter_timeout_seconds: true\n",
            # Malformed YAML
            "lanes:\n  codex:\n    [unclosed list\n",
        ]
        for cfg_text in invalid_configs:
            with self.subTest(config=cfg_text):
                with tempfile.TemporaryDirectory() as tmp:
                    repo_path = Path(tmp)
                    campaigns_dir = repo_path / ".code-mower" / "campaigns"
                    config_file = repo_path / "code-mower.yml"
                    config_file.write_text(cfg_text, encoding="utf-8")

                    campaign = release_campaigns.initialize_campaign(
                        release_tag="v1.0.0",
                        package_spec="code-mower==1.0.0",
                        providers=["codex"],
                    ).to_dict()
                    provider_entry = campaign["providers"][0]
                    provider_entry["attempted_at"] = "2026-09-04T19:00:00Z"
                    provider_entry["dispatch_mode"] = "applied"
                    provider_entry["state"] = "queued"
                    release_campaigns.save_campaign(campaign, campaigns_dir)

                    # At 100s, still under 900s default timeout -> running
                    payload = release_campaigns.release_campaigns_board_payload(
                        repo_path=repo_path,
                        campaigns_dir=campaigns_dir,
                        now="2026-09-04T19:01:40Z",
                    )
                    card = payload["campaigns"][0]["cards"][0]
                    self.assertEqual(card["state"], "running")

                    # At 901s, exceeds 900s default timeout -> queued with retry guidance
                    payload_stale = release_campaigns.release_campaigns_board_payload(
                        repo_path=repo_path,
                        campaigns_dir=campaigns_dir,
                        now="2026-09-04T19:15:01Z",
                    )
                    card_stale = payload_stale["campaigns"][0]["cards"][0]
                    self.assertEqual(card_stale["state"], "queued")
                    self.assertEqual(card_stale["next_action"], "use --retry-provider codex to retry codex")
                    self.assertIn("900s timeout", card_stale["next_detail"])

    def test_malformed_or_future_attempted_at_timestamp_degrades_safely(self) -> None:
        """Malformed or future timestamps degrade safely to non-running queued without raising."""
        bad_timestamps = [
            ("garbage_string", "timestamp is malformed"),
            ("2026-99-99T99:99:99Z", "timestamp is malformed"),
            ("2099-01-01T00:00:00Z", "timestamp is in the future"),
        ]
        for bad_ts, expected_detail_snippet in bad_timestamps:
            with self.subTest(timestamp=bad_ts):
                with tempfile.TemporaryDirectory() as tmp:
                    campaigns_dir = Path(tmp) / "campaigns"
                    campaign = release_campaigns.initialize_campaign(
                        release_tag="v1.0.0",
                        package_spec="code-mower==1.0.0",
                        providers=["codex"],
                    ).to_dict()
                    provider_entry = campaign["providers"][0]
                    provider_entry["attempted_at"] = bad_ts
                    provider_entry["dispatch_mode"] = "applied"
                    provider_entry["state"] = "queued"
                    release_campaigns.save_campaign(campaign, campaigns_dir)

                    payload = release_campaigns.release_campaigns_board_payload(
                        campaigns_dir=campaigns_dir,
                        now="2026-09-04T19:05:00Z",
                    )
                    card = payload["campaigns"][0]["cards"][0]
                    self.assertEqual(card["state"], "queued")
                    self.assertEqual(card["next_action"], "use --retry-provider codex to retry codex")
                    self.assertIn(expected_detail_snippet, card["next_detail"])


if __name__ == "__main__":
    unittest.main()
