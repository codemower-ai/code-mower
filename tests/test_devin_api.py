#!/usr/bin/env python3
"""Focused offline tests for the Devin Sessions API v3 campaign transport."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from code_mower import devin_api, release_campaigns


def _adoption_result(*, outcome: str = "pass") -> dict[str, object]:
    return {
        "schema": "code_mower.adoptionResult.v1",
        "timestamp_utc": "2026-09-04T08:00:00Z",
        "release_tag": "v1.0.0",
        "package_identity": "code-mower",
        "normalized_version": "1.0.0",
        "qualification_context": "cold_install",
        "starting_version": "",
        "ending_version": "1.0.0",
        "provider": "devin",
        "executor": "devin",
        "host_class": "local",
        "runtime_class": "python_3.12",
        "execution_state": "executed",
        "elapsed_seconds": 12.34,
        "outcome": outcome,
        "steps": [
            {
                "id": "package_install",
                "status": "pass",
                "elapsed_seconds": 12.34,
                "warning_count": 0,
                "owner_action_count": 0,
            }
        ],
    }


class _FakeApiRunner:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, object, dict[str, str]]] = []

    def __call__(
        self,
        method: str,
        url: str,
        body: object,
        headers: dict[str, str],
    ) -> object:
        self.calls.append((method, url, body, headers))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class DevinApiModuleTests(unittest.TestCase):
    def test_credentials_require_opaque_org_id(self) -> None:
        self.assertEqual(
            devin_api.credentials_from_env({"DEVIN_API_KEY": "secret"}),
            ("", "", "DEVIN_ORG_ID"),
        )
        self.assertEqual(
            devin_api.credentials_from_env(
                {"DEVIN_API_KEY": "secret", "DEVIN_ORG_ID": "github-owner"}
            ),
            ("", "", "DEVIN_ORG_ID"),
        )

    def test_repository_scope_requires_exact_owner_and_repo(self) -> None:
        env = {"CODE_MOWER_DEVIN_REPOSITORIES": "personal-owner/code-mower,other/repo"}
        self.assertFalse(
            devin_api.repository_scope_acknowledged("codemower-ai/code-mower", env=env)
        )
        env["CODE_MOWER_DEVIN_REPOSITORIES"] += ",codemower-ai/code-mower"
        self.assertTrue(
            devin_api.repository_scope_acknowledged("codemower-ai/code-mower", env=env)
        )

    def test_request_uses_bearer_token_and_get_has_no_body(self) -> None:
        runner = _FakeApiRunner([{"status": "running"}])
        devin_api.make_api_request(
            "GET",
            "/v3/organizations/org-test/sessions/devin-test",
            "api-key",
            api_runner=runner,
        )
        method, url, body, headers = runner.calls[0]
        self.assertEqual(method, "GET")
        self.assertIn("/v3/organizations/org-test/sessions/devin-test", url)
        self.assertIsNone(body)
        self.assertEqual(headers["Authorization"], "Bearer api-key")

    def test_payload_uses_exact_repo_and_current_structured_output_fields(self) -> None:
        payload = devin_api.build_devin_session_payload(
            campaign_id="campaign-v1.0.0",
            release_tag="v1.0.0",
            package_spec="code-mower==1.0.0",
            package_identity="code-mower",
            normalized_version="1.0.0",
            qualification_context="cold_install",
            starting_version="",
            repo_slug="codemower-ai/code-mower",
        )
        self.assertEqual(payload["repos"], ["codemower-ai/code-mower"])
        self.assertIs(payload["structured_output_required"], True)
        self.assertEqual(
            payload["structured_output_schema"]["title"],
            "code_mower.adoptionResult.v1",
        )
        self.assertNotIn("structured_output", payload)
        self.assertIn("Qualify exactly one release", payload["prompt"])

    def test_create_and_one_tick_poll_use_v3_session_identity(self) -> None:
        runner = _FakeApiRunner(
            [
                {"session_id": "devin-123"},
                {
                    "status": "exit",
                    "status_detail": "finished",
                    "structured_output": _adoption_result(),
                },
            ]
        )
        session_id, error = devin_api.create_devin_session(
            "org-test", {"prompt": "test"}, "key", api_runner=runner
        )
        self.assertEqual((session_id, error), ("devin-123", ""))
        state, result, error = devin_api.poll_devin_session(
            "org-test", session_id, "key", api_runner=runner
        )
        self.assertEqual((state, error), ("complete", ""))
        self.assertEqual(result, _adoption_result())
        self.assertEqual([call[0] for call in runner.calls], ["POST", "GET"])

    def test_session_id_is_opaque_bounded_and_not_prefix_specific(self) -> None:
        runner = _FakeApiRunner([{"session_id": "01K4Z_session.v3~candidate"}])
        self.assertEqual(
            devin_api.create_devin_session(
                "org-test", {"prompt": "test"}, "key", api_runner=runner
            ),
            ("01K4Z_session.v3~candidate", ""),
        )

    def test_poll_maps_owner_wait_and_terminal_failure_to_closed_codes(self) -> None:
        waiting = _FakeApiRunner(
            [{"status": "running", "status_detail": "waiting_for_approval"}]
        )
        self.assertEqual(
            devin_api.poll_devin_session(
                "org-test", "devin-1", "key", api_runner=waiting
            ),
            ("owner_action", None, "devin_waiting_for_owner"),
        )
        failed = _FakeApiRunner(
            [{"status": "error", "status_detail": "out_of_credits"}]
        )
        self.assertEqual(
            devin_api.poll_devin_session(
                "org-test", "devin-2", "key", api_runner=failed
            ),
            ("failed", None, "devin_session_failed"),
        )

    def test_poll_accepts_structured_result_while_devin_waits_for_user(self) -> None:
        result = _adoption_result()
        runner = _FakeApiRunner(
            [
                {
                    "status": "running",
                    "status_detail": "waiting_for_user",
                    "structured_output": result,
                }
            ]
        )

        self.assertEqual(
            devin_api.poll_devin_session(
                "org-test", "devin-1", "key", api_runner=runner
            ),
            ("complete", result, ""),
        )

    def test_poll_keeps_terminal_failure_ahead_of_structured_output(self) -> None:
        runner = _FakeApiRunner(
            [
                {
                    "status": "error",
                    "status_detail": "out_of_credits",
                    "structured_output": _adoption_result(),
                }
            ]
        )

        self.assertEqual(
            devin_api.poll_devin_session(
                "org-test", "devin-1", "key", api_runner=runner
            ),
            ("failed", None, "devin_session_failed"),
        )


class DevinCampaignApiTests(unittest.TestCase):
    def _campaign(
        self, *, repo_slug: str = "codemower-ai/code-mower", informational: bool = False
    ) -> dict[str, object]:
        providers = ("codex", "devin") if informational else ("devin",)
        required = ("codex",) if informational else None
        return release_campaigns.initialize_campaign(
            release_tag="v1.0.0",
            package_spec="code-mower==1.0.0",
            providers=providers,
            required_providers=required,
            repo_slug=repo_slug,
            campaign_id="campaign-v1.0.0",
        ).to_dict()

    @staticmethod
    def _entry(campaign: dict[str, object], provider: str = "devin") -> dict[str, object]:
        return next(p for p in campaign["providers"] if p["provider"] == provider)

    @staticmethod
    def _env(scope: str = "codemower-ai/code-mower") -> dict[str, str]:
        return {
            "DEVIN_API_KEY": "api-key",
            "DEVIN_ORG_ID": "org-test",
            "CODE_MOWER_DEVIN_REPOSITORIES": scope,
        }

    @staticmethod
    def _command_runner(calls: list[list[str]] | None = None):
        def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
            if calls is not None:
                calls.append(argv)
            return subprocess.CompletedProcess(args=argv, returncode=0)

        return runner

    def test_dispatch_without_issue_checkpoints_then_creates_exact_repo_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            api = _FakeApiRunner([{"session_id": "devin-abc"}])
            updated = release_campaigns.dispatch_or_advance_campaign(
                self._campaign(),
                apply=True,
                repo_path=Path(tmp),
                campaigns_dir=Path(tmp) / "campaigns",
                env=self._env(),
                command_runner=self._command_runner(),
                api_runner=api,
            )
            entry = self._entry(updated)
            self.assertEqual(entry["state"], "running")
            self.assertEqual(entry["dispatch_ref"]["session_id"], "devin-abc")
            self.assertFalse(entry["dispatch_ref"]["issue_marker_posted"])
            self.assertEqual(api.calls[0][2]["repos"], ["codemower-ai/code-mower"])

    def test_optional_issue_marker_is_audit_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gh_calls: list[list[str]] = []
            order: list[str] = []

            def api_runner(method, url, body, headers):
                order.append("api")
                return {"session_id": "devin-marker"}

            def command_runner(
                argv: list[str], **_kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                order.append("github")
                gh_calls.append(argv)
                return subprocess.CompletedProcess(args=argv, returncode=0)

            updated = release_campaigns.dispatch_or_advance_campaign(
                self._campaign(),
                apply=True,
                issue_number="42",
                repo_path=Path(tmp),
                campaigns_dir=Path(tmp) / "campaigns",
                env=self._env(),
                command_runner=command_runner,
                api_runner=api_runner,
            )
            ref = self._entry(updated)["dispatch_ref"]
            self.assertEqual(ref["session_id"], "devin-marker")
            self.assertEqual(ref["issue_number"], "42")
            self.assertTrue(ref["issue_marker_posted"])
            self.assertEqual(len(gh_calls), 1)
            self.assertEqual(order, ["api", "github"])

    def test_same_name_fork_ack_does_not_authorize_target_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            api = _FakeApiRunner([])
            updated = release_campaigns.dispatch_or_advance_campaign(
                self._campaign(),
                apply=True,
                repo_path=Path(tmp),
                campaigns_dir=Path(tmp) / "campaigns",
                env=self._env("personal-owner/code-mower"),
                api_runner=api,
            )
            entry = self._entry(updated)
            self.assertEqual(entry["state"], "unavailable")
            self.assertEqual(entry["error"], "hosted_transport_unverified")
            self.assertEqual(api.calls, [])

    def test_checkpoint_prevents_redispatch_when_create_outcome_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            api = _FakeApiRunner([KeyboardInterrupt()])
            with self.assertRaises(KeyboardInterrupt):
                release_campaigns.dispatch_or_advance_campaign(
                    self._campaign(),
                    apply=True,
                    repo_path=Path(tmp),
                    campaigns_dir=campaigns_dir,
                    env=self._env(),
                    api_runner=api,
                )
            saved = release_campaigns.load_campaign_by_id(
                "campaign-v1.0.0", campaigns_dir
            )
            self.assertIsNotNone(saved)
            entry = self._entry(saved)
            self.assertEqual(entry["state"], "running")
            self.assertTrue(entry["attempted_at"])
            self.assertEqual(entry["dispatch_ref"]["session_id"], "")

            api.calls.clear()
            resumed = release_campaigns.dispatch_or_advance_campaign(
                saved,
                apply=True,
                repo_path=Path(tmp),
                campaigns_dir=campaigns_dir,
                env=self._env(),
                api_runner=api,
            )
            self.assertEqual(self._entry(resumed)["state"], "running")
            self.assertEqual(api.calls, [])

    def test_resume_performs_one_get_and_binds_structured_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaign = self._campaign()
            entry = self._entry(campaign)
            entry.update(
                {
                    "state": "running",
                    "attempted_at": "2026-09-04T08:00:00Z",
                    "dispatch_ref": {
                        "session_id": "devin-result",
                        "transport_kind": "devin_api_v3",
                        "repo_slug": "codemower-ai/code-mower",
                    },
                }
            )
            api = _FakeApiRunner(
                [
                    {
                        "status": "exit",
                        "status_detail": "finished",
                        "structured_output": _adoption_result(),
                    }
                ]
            )
            updated = release_campaigns.dispatch_or_advance_campaign(
                campaign,
                poll_only=True,
                repo_path=Path(tmp),
                campaigns_dir=Path(tmp) / "campaigns",
                env=self._env(),
                api_runner=api,
            )
            entry = self._entry(updated)
            self.assertEqual(entry["state"], "complete")
            self.assertEqual(entry["adoption_result"]["provider"], "devin")
            self.assertEqual([call[0] for call in api.calls], ["GET"])

    def test_explicit_retry_does_not_duplicate_an_active_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaign = self._campaign()
            entry = self._entry(campaign)
            entry.update(
                {
                    "state": "running",
                    "attempted_at": "2026-09-04T08:00:00Z",
                    "response_deadline_at": "2099-01-01T00:00:00Z",
                    "dispatch_ref": {"session_id": "active-session"},
                }
            )
            api = _FakeApiRunner([{"status": "running", "status_detail": "working"}])
            updated = release_campaigns.dispatch_or_advance_campaign(
                campaign,
                apply=True,
                retry_provider="devin",
                repo_path=Path(tmp),
                campaigns_dir=Path(tmp) / "campaigns",
                env=self._env(),
                api_runner=api,
            )
            entry = self._entry(updated)
            self.assertEqual(entry["state"], "running")
            self.assertIn("retry refused", entry["next_detail"])
            self.assertEqual([call[0] for call in api.calls], ["GET"])

    def test_explicit_retry_does_not_duplicate_an_owner_blocked_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaign = self._campaign()
            entry = self._entry(campaign)
            entry.update(
                {
                    "state": "blocked",
                    "error": "devin_waiting_for_owner",
                    "attempted_at": "2026-09-04T08:00:00Z",
                    "response_deadline_at": "2099-01-01T00:00:00Z",
                    "dispatch_ref": {"session_id": "owner-blocked-session"},
                }
            )
            api = _FakeApiRunner(
                [{"status": "running", "status_detail": "waiting_for_approval"}]
            )
            updated = release_campaigns.dispatch_or_advance_campaign(
                campaign,
                apply=True,
                retry_provider="devin",
                repo_path=Path(tmp),
                campaigns_dir=Path(tmp) / "campaigns",
                env=self._env(),
                api_runner=api,
            )
            entry = self._entry(updated)
            self.assertEqual(entry["state"], "blocked")
            self.assertIn("retry refused", entry["next_detail"])
            self.assertEqual([call[0] for call in api.calls], ["GET"])

    def test_explicit_retry_can_redispatch_after_response_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaign = self._campaign()
            entry = self._entry(campaign)
            entry.update(
                {
                    "state": "running",
                    "attempted_at": "2026-09-04T08:00:00Z",
                    "response_deadline_at": "2020-01-01T00:00:00Z",
                    "dispatch_ref": {"session_id": "expired-session"},
                }
            )
            api = _FakeApiRunner(
                [
                    {"status": "running", "status_detail": "working"},
                    {"session_id": "replacement-session"},
                ]
            )
            updated = release_campaigns.dispatch_or_advance_campaign(
                campaign,
                apply=True,
                retry_provider="devin",
                repo_path=Path(tmp),
                campaigns_dir=Path(tmp) / "campaigns",
                env=self._env(),
                api_runner=api,
            )
            entry = self._entry(updated)
            self.assertEqual(entry["dispatch_ref"]["session_id"], "replacement-session")
            self.assertEqual([call[0] for call in api.calls], ["GET", "POST"])

    def test_api_error_is_bounded_and_never_persists_secret_or_raw_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            api = _FakeApiRunner([OSError("api-key /private/path")])
            gh_calls: list[list[str]] = []
            updated = release_campaigns.dispatch_or_advance_campaign(
                self._campaign(),
                apply=True,
                issue_number="42",
                repo_path=Path(tmp),
                campaigns_dir=Path(tmp) / "campaigns",
                env=self._env(),
                command_runner=self._command_runner(gh_calls),
                api_runner=api,
            )
            rendered = json.dumps(updated)
            self.assertNotIn("api-key", rendered)
            self.assertNotIn("/private/path", rendered)
            self.assertEqual(self._entry(updated)["error"], "devin_api_unavailable")
            self.assertEqual(gh_calls, [])

    def test_operator_can_dispose_only_running_informational_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            campaign = self._campaign(informational=True)
            entry = self._entry(campaign)
            self._entry(campaign, "codex")["state"] = "complete"
            entry.update(
                {
                    "state": "running",
                    "attempted_at": "2026-09-04T08:00:00Z",
                    "dispatch_ref": {"session_id": "devin-stop"},
                }
            )
            result = release_campaigns.dispose_informational_provider(
                campaign,
                "devin",
                "operator_cancelled",
                campaigns_dir=campaigns_dir,
            )
            entry = self._entry(result)
            self.assertEqual(entry["state"], "unavailable")
            self.assertEqual(entry["error"], "operator_cancelled")
            self.assertIsNone(entry["adoption_result"])
            self.assertEqual(entry["dispatch_ref"]["session_id"], "devin-stop")
            self.assertEqual(result["status"], "complete")
            board = release_campaigns.release_campaigns_board_payload(
                campaigns_dir=campaigns_dir
            )
            devin_card = next(
                card
                for card in board["campaigns"][0]["cards"]
                if card["provider"] == "devin"
            )
            self.assertEqual(devin_card["state"], "unavailable")
            self.assertEqual(devin_card["next_action"], "none")
            self.assertEqual(
                devin_card["next_detail"], "operator disposition: operator_cancelled"
            )

            required = self._campaign()
            self._entry(required).update(
                {"state": "running", "attempted_at": "2026-09-04T08:00:00Z"}
            )
            with self.assertRaisesRegex(ValueError, "informational"):
                release_campaigns.dispose_informational_provider(
                    required,
                    "devin",
                    "operator_cancelled",
                    campaigns_dir=campaigns_dir,
                )

    def test_campaign_dispose_requires_apply_and_uses_closed_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            campaign = self._campaign(informational=True)
            self._entry(campaign, "codex")["state"] = "complete"
            self._entry(campaign).update(
                {
                    "state": "running",
                    "attempted_at": "2026-09-04T08:00:00Z",
                    "dispatch_ref": {"session_id": "devin-stop"},
                }
            )
            release_campaigns.save_campaign(campaign, campaigns_dir)

            self.assertEqual(
                release_campaigns.campaign_command(
                    action="dispose",
                    campaign_id="campaign-v1.0.0",
                    campaigns_dir=campaigns_dir,
                    dispose_provider="devin",
                    unavailable_reason="provider_transport_unavailable",
                ),
                1,
            )
            self.assertEqual(self._entry(campaign)["state"], "running")

            self.assertEqual(
                release_campaigns.campaign_command(
                    action="dispose",
                    campaign_id="campaign-v1.0.0",
                    campaigns_dir=campaigns_dir,
                    dispose_provider="devin",
                    unavailable_reason="provider_transport_unavailable",
                    apply=True,
                ),
                0,
            )
            saved = release_campaigns.load_campaign_by_id(
                "campaign-v1.0.0", campaigns_dir
            )
            self.assertEqual(self._entry(saved)["state"], "unavailable")
            self.assertEqual(
                self._entry(saved)["error"], "provider_transport_unavailable"
            )


if __name__ == "__main__":
    unittest.main()
