#!/usr/bin/env python3
"""Bounded offline fixtures for hosted release-qualification dispatch.

Covers the closed hosted dispatch profile (auth, installation, trigger,
trusted responder, result return) for the opt-in paid hosted providers
(Cursor Cloud Agent, Devin) without any network access: verified readiness,
unverified transport, response timeout, spoofed responder, and successful
result return. Every scenario is metadata-only -- no issue bodies, provider
output, auth output, paths, or secrets leave the test process.
"""

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

from code_mower import release_campaigns
from code_mower.doctor_checks.adoption import check_adoption_campaign_readiness
from code_mower.provider_registry import REFERENCE_PROVIDERS

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "hosted_dispatch"


def load_fixture(name: str) -> dict[str, Any]:
    with (FIXTURES_DIR / f"{name}.json").open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    assert isinstance(data, dict)
    return data


def _mock_adoption_result(
    *,
    release_tag: str = "v1.0.0",
    provider: str = "cursor_cloud_agent",
    qualification_context: str = "cold_install",
    starting_version: str = "",
    outcome: str = "pass",
) -> dict[str, Any]:
    return {
        "schema": "code_mower.adoptionResult.v1",
        "timestamp_utc": "2026-09-04T08:00:00Z",
        "release_tag": release_tag,
        "package_identity": "code-mower",
        "normalized_version": "1.0.0",
        "qualification_context": qualification_context,
        "starting_version": starting_version,
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
                "status": "pass",
                "elapsed_seconds": 11.14,
                "warning_count": 0,
                "owner_action_count": 0,
            },
        ],
    }


def _fixture_env(fixture: dict[str, Any]) -> dict[str, str]:
    env: dict[str, str] = {}
    if fixture.get("token_present"):
        env[str(fixture["token_env"])] = "token"
    if fixture.get("transport_acknowledged"):
        env[str(fixture["transport_env"])] = "1"
    return env


class HostedDispatchProfileTests(unittest.TestCase):
    """The five profile checks are judged independently from fixture inputs."""

    def test_verified_fixture_reports_all_checks_ready(self) -> None:
        fixture = load_fixture("verified")
        lane = REFERENCE_PROVIDERS["cursor_cloud_agent"]
        profile = release_campaigns.hosted_dispatch_profile(
            lane, env=_fixture_env(fixture)
        )
        self.assertEqual(
            {name: entry["ready"] for name, entry in profile.items()},
            fixture["expected_profile"],
        )
        self.assertEqual(release_campaigns.hosted_dispatch_blockers(profile), [])

    def test_unavailable_fixture_blocks_only_installation(self) -> None:
        fixture = load_fixture("unavailable")
        lane = REFERENCE_PROVIDERS["devin"]
        profile = release_campaigns.hosted_dispatch_profile(
            lane, env=_fixture_env(fixture)
        )
        self.assertEqual(
            {name: entry["ready"] for name, entry in profile.items()},
            fixture["expected_profile"],
        )
        self.assertEqual(release_campaigns.hosted_dispatch_blockers(profile), ["installation"])
        remediation = profile["installation"]["remediation"]
        self.assertIn(fixture["transport_env"], remediation)
        # Bounded metadata only: no secret values, paths, or output.
        self.assertNotIn("token", remediation)

    def test_result_return_rejects_non_positive_timeout(self) -> None:
        from code_mower.provider_registry import LaneLabels, ProviderLane

        lane = ProviderLane(
            lane_id="fake_hosted",
            lane_type="audit",
            driver="hosted_bridge",
            provider="fake_hosted",
            labels=LaneLabels(needs="needs-fake", done="fake-done", blocked="fake-blocked"),
            trigger_policy="manual",
            provider_config={
                "bot_authors": ("fake-bot[bot]",),
                "trigger_comments": ("@fake run",),
                "campaign_response_timeout_seconds": 0,
            },
        )
        profile = release_campaigns.hosted_dispatch_profile(
            lane, env={"GITHUB_TOKEN": "token"}
        )
        self.assertTrue(profile["auth"]["ready"])
        self.assertTrue(profile["installation"]["ready"])
        self.assertTrue(profile["trigger"]["ready"])
        self.assertTrue(profile["trusted_responder"]["ready"])
        self.assertFalse(profile["result_return"]["ready"])
        self.assertIn("campaign_response_timeout_seconds", profile["result_return"]["remediation"])

    def test_cursor_builder_trigger_is_the_real_mention_contract(self) -> None:
        """Cursor Cloud Agent uses `@cursor`, never BugBot/reviewer trigger text."""
        lane = REFERENCE_PROVIDERS["cursor_cloud_agent"]
        trigger_comments = tuple(lane.provider_config.get("trigger_comments") or ())
        self.assertEqual(trigger_comments, ("@cursor",))
        for trigger in trigger_comments:
            lowered = trigger.lower()
            self.assertNotIn("bugbot", lowered)
            self.assertNotIn("review", lowered)
        # The shipped templates agree with the registry.
        for relative in (
            "templates/providers/cursor_cloud_agent.yml",
            "templates/providers.yml",
            "src/code_mower/templates/providers.yml",
        ):
            text = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn('"@cursor"', text)
            self.assertNotIn('"@cursor run"', text)
            self.assertNotIn('"cursor run"', text)


class HostedDispatchFixtureCampaignTests(unittest.TestCase):
    """Fixture-driven dry-run, timeout, spoof, and result-return behavior."""

    def test_verified_fixture_previews_queued_without_dispatch(self) -> None:
        fixture = load_fixture("verified")
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            command_runner = mock.MagicMock()
            gh_json_runner = mock.MagicMock()
            ret = release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=[str(fixture["provider"])],
                campaigns_dir=campaigns_dir,
                repo_slug=str(fixture["repo_slug"]),
                issue="42",
                apply=False,
                command_runner=command_runner,
                gh_json_runner=gh_json_runner,
                env=_fixture_env(fixture),
            )
            self.assertEqual(ret, 0)
            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            entry = saved["providers"][0]
            self.assertEqual(entry["state"], fixture["expected_state"])
            self.assertEqual(entry["error"], fixture["expected_error"])
            self.assertEqual(entry["transport_verified"], fixture["expected_transport_verified"])
            self.assertEqual(command_runner.call_count, fixture["expected_dispatch_calls"])
            gh_json_runner.assert_not_called()

    def test_unavailable_fixture_reports_exact_remediation_before_dispatch(self) -> None:
        fixture = load_fixture("unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            command_runner = mock.MagicMock()
            gh_json_runner = mock.MagicMock()
            ret = release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=[str(fixture["provider"])],
                campaigns_dir=campaigns_dir,
                repo_slug=str(fixture["repo_slug"]),
                issue="42",
                apply=False,
                command_runner=command_runner,
                gh_json_runner=gh_json_runner,
                env=_fixture_env(fixture),
            )
            self.assertEqual(ret, 0)
            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            entry = saved["providers"][0]
            self.assertEqual(entry["state"], fixture["expected_state"])
            self.assertEqual(entry["error"], fixture["expected_error"])
            self.assertIn(entry["error"], release_campaigns.SAFE_ERROR_CODES)
            self.assertIn(str(fixture["expected_remediation_contains"]), entry["next_action"])
            self.assertIn(str(fixture["expected_remediation_contains"]), entry["next_detail"])
            self.assertNotIn("--apply", entry["next_action"])
            self.assertEqual(saved["status"], "unavailable")
            self.assertEqual(command_runner.call_count, fixture["expected_dispatch_calls"])
            gh_json_runner.assert_not_called()

    def _running_campaign(self, campaigns_dir: Path, fixture: dict[str, Any]) -> dict[str, Any]:
        campaign = release_campaigns.initialize_campaign(
            release_tag="v1.0.0",
            package_spec="code-mower==1.0.0",
            providers=[str(fixture["provider"])],
            repo_slug="owner/repo",
        )
        provider = campaign.providers[0]
        campaign.status = "running"
        provider["state"] = "running"
        provider["attempted_at"] = "2026-09-04T08:00:00Z"
        provider["dispatched_at"] = "2026-09-04T08:00:00Z"
        provider["trigger_posted"] = True
        provider["response_deadline_at"] = "2030-01-01T00:00:00Z"
        provider["dispatch_ref"] = {"issue_number": "99", "comment_posted": True}
        release_campaigns.save_campaign(campaign, campaigns_dir)
        return provider

    def _bound_marker_body(
        self,
        *,
        campaign_id: str,
        provider: str,
        release_tag: str,
        idempotency_key: str,
        author_note: str = "Qualification complete.",
    ) -> str:
        adoption_res = _mock_adoption_result(
            release_tag=release_tag,
            provider=provider,
        )
        wrapper = {
            "schema": release_campaigns.RESULT_MARKER_SCHEMA,
            "campaign_id": campaign_id,
            "provider": provider,
            "release_tag": release_tag,
            "idempotency_key": idempotency_key,
            "adoption_result": adoption_res,
        }
        return f"{author_note}\n\n<!-- CODE_MOWER_ADOPTION_RESULT: {json.dumps(wrapper)} -->"

    def test_timeout_fixture_records_evidence_and_never_redispatches(self) -> None:
        fixture = load_fixture("timeout")
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            provider = self._running_campaign(campaigns_dir, fixture)
            provider["response_deadline_at"] = str(fixture["response_deadline_at"])
            campaign = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert campaign is not None
            campaign["providers"][0]["response_deadline_at"] = str(
                fixture["response_deadline_at"]
            )
            release_campaigns.save_campaign(campaign, campaigns_dir)

            command_runner = mock.MagicMock()

            def mock_gh_json(args, **kwargs):
                return {"comments": list(fixture["comments"])}, ""

            ret = release_campaigns.campaign_command(
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                resume=True,
                repo_slug="owner/repo",
                command_runner=command_runner,
                gh_json_runner=mock_gh_json,
                env=_fixture_env(fixture),
            )
            self.assertEqual(ret, 0)
            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            entry = saved["providers"][0]
            self.assertEqual(entry["state"], fixture["expected_state"])
            self.assertEqual(entry["error"], fixture["expected_error"])
            self.assertIn(entry["error"], release_campaigns.SAFE_ERROR_CODES)
            self.assertIn("--retry-provider", entry["next_action"])
            # An ordinary resume records the timeout; it never redispatches
            # paid work, and repeating it still dispatches nothing.
            self.assertEqual(
                command_runner.call_count, fixture["expected_resume_dispatch_calls"]
            )
            ret = release_campaigns.campaign_command(
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                resume=True,
                repo_slug="owner/repo",
                command_runner=command_runner,
                gh_json_runner=mock_gh_json,
                env=_fixture_env(fixture),
            )
            self.assertEqual(ret, 1)
            self.assertEqual(
                command_runner.call_count, fixture["expected_resume_dispatch_calls"]
            )

    def test_spoofed_responder_marker_is_ignored(self) -> None:
        fixture = load_fixture("spoofed_responder")
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            provider = self._running_campaign(campaigns_dir, fixture)
            body = self._bound_marker_body(
                campaign_id="campaign-v1.0.0",
                provider=str(fixture["provider"]),
                release_tag="v1.0.0",
                idempotency_key=str(provider["idempotency_key"]),
            )

            def mock_gh_json(args, **kwargs):
                return {
                    "comments": [
                        {"author": {"login": str(fixture["comment_author"])}, "body": body}
                    ]
                }, ""

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                resume=True,
                repo_slug="owner/repo",
                command_runner=mock.MagicMock(),
                gh_json_runner=mock_gh_json,
                env=_fixture_env(fixture),
            )
            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            entry = saved["providers"][0]
            self.assertEqual(entry["state"], fixture["expected_state"])
            self.assertIsNone(entry["adoption_result"])

    def test_successful_return_completes_from_trusted_responder(self) -> None:
        fixture = load_fixture("successful_return")
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            provider = self._running_campaign(campaigns_dir, fixture)
            body = self._bound_marker_body(
                campaign_id="campaign-v1.0.0",
                provider=str(fixture["provider"]),
                release_tag="v1.0.0",
                idempotency_key=str(provider["idempotency_key"]),
            )

            def mock_gh_json(args, **kwargs):
                return {
                    "comments": [
                        {"author": {"login": str(fixture["comment_author"])}, "body": body}
                    ]
                }, ""

            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                resume=True,
                repo_slug="owner/repo",
                command_runner=mock.MagicMock(),
                gh_json_runner=mock_gh_json,
                env=_fixture_env(fixture),
            )
            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            entry = saved["providers"][0]
            self.assertEqual(entry["state"], fixture["expected_state"])
            assert entry["adoption_result"] is not None
            self.assertEqual(
                entry["adoption_result"]["outcome"], fixture["expected_outcome"]
            )


class HostedDoctorProfileTests(unittest.TestCase):
    """Doctor reports the closed profile and stays metadata-only."""

    def test_doctor_transport_warn_carries_dispatch_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checks = check_adoption_campaign_readiness(
                config={},
                repo_root=Path(tmp),
                repo_slug="owner/repo",
                env={"DEVIN_AUDIT_LABEL_TOKEN": "token"},
                providers=["devin"],
            )
            transport = [c for c in checks if c.name == "doctor.campaign.transport"]
            self.assertEqual(len(transport), 1)
            check = transport[0]
            profile = check.detail.get("dispatch_profile")
            self.assertIsInstance(profile, dict)
            self.assertFalse(profile["installation"])
            self.assertTrue(profile["auth"])
            self.assertIn("installation", list(check.detail.get("dispatch_blockers") or []))
            self.assertNotIn("token", json.dumps(check.detail))

    def test_doctor_verified_pass_carries_full_dispatch_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checks = check_adoption_campaign_readiness(
                config={},
                repo_root=Path(tmp),
                repo_slug="owner/repo",
                env={
                    "DEVIN_AUDIT_LABEL_TOKEN": "token",
                    "CODE_MOWER_DEVIN_CAMPAIGN_TRANSPORT_READY": "1",
                },
                providers=["devin"],
            )
            creds = [c for c in checks if c.name == "doctor.campaign.credentials"]
            self.assertEqual(len(creds), 1)
            profile = creds[0].detail.get("dispatch_profile")
            self.assertIsInstance(profile, dict)
            self.assertTrue(all(profile.values()))
            self.assertNotIn("token", json.dumps(creds[0].detail))


if __name__ == "__main__":
    unittest.main()
