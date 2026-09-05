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

import dataclasses
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

    def test_result_return_accepts_numeric_timeout_string(self) -> None:
        base = REFERENCE_PROVIDERS["devin"]
        config = dict(base.provider_config)
        config["campaign_response_timeout_seconds"] = "3600"
        lane = dataclasses.replace(base, provider_config=config)
        profile = release_campaigns.hosted_dispatch_profile(
            lane,
            env={
                "DEVIN_AUDIT_LABEL_TOKEN": "token",
                "CODE_MOWER_DEVIN_CAMPAIGN_TRANSPORT_READY": "1",
            },
        )
        self.assertTrue(profile["result_return"]["ready"])
        self.assertEqual(release_campaigns._hosted_response_timeout(lane), 3600)

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
            self.assertEqual(ret, 0)
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


def _devin_lane_without(
    *,
    trigger_comments: bool = True,
    trusted_responders: bool = True,
):
    """Return the devin lane minus trigger text and/or the responder allowlist."""
    base = REFERENCE_PROVIDERS["devin"]
    config = dict(base.provider_config)
    if not trigger_comments:
        config["trigger_comments"] = ()
    if not trusted_responders:
        config["bot_authors"] = ()
        config.pop("bot_authors_env", None)
    return dataclasses.replace(base, provider_config=config)


def _verified_devin_env() -> dict[str, str]:
    return {
        "DEVIN_AUDIT_LABEL_TOKEN": "s3cret-token-value",
        "CODE_MOWER_DEVIN_CAMPAIGN_TRANSPORT_READY": "1",
    }


class HostedDispatchBlockerTests(unittest.TestCase):
    """Trigger and trusted-responder blockers fail closed in dry-run and doctor."""

    def _dry_run_devin(self, lane, env) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            command_runner = mock.MagicMock()
            gh_json_runner = mock.MagicMock()
            with mock.patch.dict(release_campaigns.REFERENCE_PROVIDERS, {"devin": lane}):
                ret = release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    providers=["devin"],
                    campaigns_dir=campaigns_dir,
                    repo_slug="owner/repo",
                    issue="42",
                    apply=False,
                    command_runner=command_runner,
                    gh_json_runner=gh_json_runner,
                    env=env,
                )
            self.assertEqual(ret, 0)
            saved = release_campaigns.load_campaign_by_id(
                "campaign-v1.0.0", campaigns_dir
            )
            assert saved is not None
            command_runner.assert_not_called()
            gh_json_runner.assert_not_called()
            return saved

    def _assert_blocked_dry_run(
        self, saved: dict[str, Any], *, blocker: str, remediation_marker: str
    ) -> None:
        entry = saved["providers"][0]
        self.assertEqual(entry["state"], "unavailable")
        self.assertEqual(entry["error"], "hosted_transport_unverified")
        self.assertIn(entry["error"], release_campaigns.SAFE_ERROR_CODES)
        self.assertIn(blocker, entry["next_detail"])
        self.assertIn(remediation_marker, entry["next_action"])
        self.assertIn(remediation_marker, entry["next_detail"])
        self.assertNotIn("--apply", entry["next_action"])
        self.assertNotIn("--apply", entry["next_detail"])
        self.assertEqual(saved["status"], "unavailable")
        # Bounded metadata only: no secret values, paths, or output.
        self.assertNotIn("s3cret-token-value", json.dumps(saved))

    def test_dry_run_trigger_blocker_is_unavailable(self) -> None:
        lane = _devin_lane_without(trigger_comments=False)
        profile = release_campaigns.hosted_dispatch_profile(
            lane, env=_verified_devin_env()
        )
        self.assertEqual(release_campaigns.hosted_dispatch_blockers(profile), ["trigger"])
        saved = self._dry_run_devin(lane, _verified_devin_env())
        self._assert_blocked_dry_run(
            saved, blocker="trigger", remediation_marker="builder trigger"
        )

    def test_dry_run_trusted_responder_blocker_is_unavailable(self) -> None:
        lane = _devin_lane_without(trusted_responders=False)
        profile = release_campaigns.hosted_dispatch_profile(
            lane, env=_verified_devin_env()
        )
        self.assertEqual(
            release_campaigns.hosted_dispatch_blockers(profile), ["trusted_responder"]
        )
        saved = self._dry_run_devin(lane, _verified_devin_env())
        self._assert_blocked_dry_run(
            saved, blocker="trusted_responder", remediation_marker="bot_authors"
        )

    def test_dry_run_reports_every_blocker_remediation(self) -> None:
        lane = _devin_lane_without(
            trigger_comments=False,
            trusted_responders=False,
        )
        saved = self._dry_run_devin(lane, _verified_devin_env())
        entry = saved["providers"][0]
        for blocker in ("trigger", "trusted_responder"):
            self.assertIn(blocker, entry["next_detail"])
        for remediation_marker in ("builder trigger", "bot_authors"):
            self.assertIn(remediation_marker, entry["next_action"])
            self.assertIn(remediation_marker, entry["next_detail"])

    def _doctor_checks(self, lane) -> tuple:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(release_campaigns.REFERENCE_PROVIDERS, {"devin": lane}):
                return check_adoption_campaign_readiness(
                    config={},
                    repo_root=Path(tmp),
                    repo_slug="owner/repo",
                    env=_verified_devin_env(),
                    providers=["devin"],
                )

    def _assert_blocked_doctor(
        self, checks: tuple, *, blocker: str, remediation_marker: str
    ) -> None:
        transport = [c for c in checks if c.name == "doctor.campaign.transport"]
        self.assertEqual(len(transport), 1)
        check = transport[0]
        self.assertEqual(check.status, "warn")
        self.assertIn(blocker, list(check.detail.get("dispatch_blockers") or []))
        self.assertIn(
            remediation_marker,
            json.dumps(check.detail.get("blocker_remediations") or {}),
        )
        self.assertIn(remediation_marker, check.remediation)
        # No PASS for the blocked lane, and the aggregate never calls it ready.
        passes = [
            c
            for c in checks
            if c.name == "doctor.campaign.credentials" and c.lane == "devin"
        ]
        self.assertEqual(passes, [])
        readiness = next(c for c in checks if c.name == "doctor.campaign.readiness")
        self.assertNotIn("devin", readiness.detail.get("ready_providers", []))
        self.assertNotIn("s3cret-token-value", json.dumps(checks, default=str))

    def test_doctor_trigger_blocker_warns_without_pass(self) -> None:
        checks = self._doctor_checks(_devin_lane_without(trigger_comments=False))
        self._assert_blocked_doctor(
            checks, blocker="trigger", remediation_marker="builder trigger"
        )

    def test_doctor_trusted_responder_blocker_warns_without_pass(self) -> None:
        checks = self._doctor_checks(_devin_lane_without(trusted_responders=False))
        self._assert_blocked_doctor(
            checks, blocker="trusted_responder", remediation_marker="bot_authors"
        )


if __name__ == "__main__":
    unittest.main()
