"""Authenticated `gh` as campaign credentials, and durable campaign issue binding.

Two defects from the v1.1.0 release campaign, kept together because they hit the
same operator in the same run: Cursor previewed `unavailable` for "missing
credentials" on a machine where `gh` was already authenticated and the transport
*is* `gh issue comment`, and the `--issue` that made a later dispatch work was
never durable campaign identity, so every subsequent invocation had to re-supply
it or fall back to "missing issue number".

Everything here is hermetic: the `gh` credential probe is an injected
zero-argument callable, so no test consults the machine's real `gh` state.
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from code_mower import release_campaigns
from code_mower.provider_registry import REFERENCE_PROVIDERS

CURSOR_TOKEN_ENV = "CURSOR_CLOUD_AGENT_AUDIT_LABEL_TOKEN"
CURSOR_TRANSPORT_ENV = "CODE_MOWER_CURSOR_CLOUD_AGENT_CAMPAIGN_TRANSPORT_READY"
FAKE_GH_AUTH_TOKEN_VALUE = "ghp_s3cret-probe-token-value"


def _verified_cursor_env(*, token: bool = True) -> dict[str, str]:
    env = {CURSOR_TRANSPORT_ENV: "1"}
    if token:
        env[CURSOR_TOKEN_ENV] = "token"
    return env


class _RecordingProbe:
    """A `gh auth token` probe that records how often it was consulted."""

    def __init__(self, authenticated: bool) -> None:
        self.authenticated = authenticated
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        return self.authenticated


def _commented_issue_numbers(argvs: list[list[str]]) -> list[str]:
    """The issue number every posted `gh issue comment` was addressed to.

    Cursor Cloud Agent posts two comments per dispatch -- the machine-readable
    dispatch marker and the `@cursor` builder trigger -- so tests assert on the
    *set of issues addressed*, not on a call count.
    """
    return [argv[3] for argv in argvs if argv[:3] == ["gh", "issue", "comment"]]


def _dispatch_runner(argvs: list[list[str]], *, returncode: int = 0):
    """A `gh` command runner that records the exact argv it was asked to run."""

    def _run(args, **kwargs):
        argvs.append([str(a) for a in args])

        class MockCompleted:
            pass

        completed = MockCompleted()
        completed.returncode = returncode
        completed.stdout = ""
        completed.stderr = ""
        return completed

    return _run


def _empty_gh_json_runner(calls: list[tuple[str, ...]] | None = None):
    """A `gh ... --json comments` runner that answers with no comments."""

    def _run(args, **kwargs):
        argv = tuple(str(a) for a in args)
        if calls is not None:
            calls.append(argv)
        return {"comments": []}

    return _run


class GhAuthCredentialFallbackTests(unittest.TestCase):
    """An authenticated `gh` is the credential a GitHub-comment transport needs."""

    def test_token_environment_keeps_precedence_and_skips_the_probe(self) -> None:
        """A configured token variable answers outright; `gh` is never consulted."""
        probe = _RecordingProbe(authenticated=False)
        lane = REFERENCE_PROVIDERS["cursor_cloud_agent"]

        has_creds, missing = release_campaigns._check_credentials(
            lane, env={CURSOR_TOKEN_ENV: "token"}, auth_probe=probe
        )

        self.assertTrue(has_creds)
        self.assertEqual(missing, "")
        self.assertEqual(probe.calls, 0)

    def test_fallback_environment_variable_also_keeps_precedence(self) -> None:
        """Any configured variable counts, not just the lane's first one."""
        probe = _RecordingProbe(authenticated=False)
        lane = REFERENCE_PROVIDERS["cursor_cloud_agent"]

        has_creds, _missing = release_campaigns._check_credentials(
            lane, env={"GITHUB_TOKEN": "token"}, auth_probe=probe
        )

        self.assertTrue(has_creds)
        self.assertEqual(probe.calls, 0)

    def test_authenticated_gh_supplies_credentials_when_no_variable_is_set(self) -> None:
        probe = _RecordingProbe(authenticated=True)
        lane = REFERENCE_PROVIDERS["cursor_cloud_agent"]

        has_creds, missing = release_campaigns._check_credentials(
            lane, env={}, auth_probe=probe
        )

        self.assertTrue(has_creds)
        self.assertEqual(missing, "")
        self.assertEqual(probe.calls, 1)

    def test_unauthenticated_gh_still_reports_the_missing_variable(self) -> None:
        probe = _RecordingProbe(authenticated=False)
        lane = REFERENCE_PROVIDERS["cursor_cloud_agent"]

        has_creds, missing = release_campaigns._check_credentials(
            lane, env={}, auth_probe=probe
        )

        self.assertFalse(has_creds)
        self.assertEqual(missing, CURSOR_TOKEN_ENV)
        self.assertEqual(probe.calls, 1)

    def test_omitted_probe_leaves_the_environment_only_verdict_unchanged(self) -> None:
        """No probe supplied means no ambient state is consulted at all."""
        lane = REFERENCE_PROVIDERS["cursor_cloud_agent"]

        has_creds, missing = release_campaigns._check_credentials(lane, env={})

        self.assertFalse(has_creds)
        self.assertEqual(missing, CURSOR_TOKEN_ENV)

    def test_local_cli_lane_never_falls_back_to_gh(self) -> None:
        """The fallback is for GitHub-comment transports, not every lane."""
        probe = _RecordingProbe(authenticated=True)
        lane = REFERENCE_PROVIDERS["codex"]

        has_creds, missing = release_campaigns._check_credentials(
            lane, env={}, auth_probe=probe
        )

        self.assertFalse(has_creds)
        self.assertEqual(missing, "DISPATCH_TOKEN")
        self.assertEqual(probe.calls, 0)

    def test_devin_api_credential_resolution_is_unchanged(self) -> None:
        """Devin's v3 transport needs Devin credentials; `gh` says nothing about them."""
        probe = _RecordingProbe(authenticated=True)
        lane = REFERENCE_PROVIDERS["devin"]

        with tempfile.TemporaryDirectory() as tmp:
            # An isolated, empty config_dir keeps this hermetic: with no
            # override, Devin credential resolution falls back to discovering
            # profiles under the real `~/.config/code-mower`, which is real
            # machine state this test must not depend on.
            has_creds, missing = release_campaigns._check_credentials(
                lane, env={}, auth_probe=probe, config_dir=Path(tmp)
            )

        self.assertFalse(has_creds)
        self.assertTrue(missing)
        self.assertEqual(probe.calls, 0)

    def test_dispatch_profile_auth_check_reports_the_gh_credential(self) -> None:
        probe = _RecordingProbe(authenticated=True)
        lane = REFERENCE_PROVIDERS["cursor_cloud_agent"]

        profile = release_campaigns.hosted_dispatch_profile(
            lane,
            env={CURSOR_TRANSPORT_ENV: "1"},
            repo_slug="owner/repo",
            auth_probe=probe,
        )

        self.assertTrue(profile["auth"]["ready"])
        self.assertEqual(release_campaigns.hosted_dispatch_blockers(profile), [])
        self.assertIn("gh", profile["auth"]["detail"])
        self.assertNotIn(FAKE_GH_AUTH_TOKEN_VALUE, json.dumps(profile))

    def test_dispatch_profile_remediation_names_both_ways_to_authenticate(self) -> None:
        probe = _RecordingProbe(authenticated=False)
        lane = REFERENCE_PROVIDERS["cursor_cloud_agent"]

        profile = release_campaigns.hosted_dispatch_profile(
            lane,
            env={CURSOR_TRANSPORT_ENV: "1"},
            repo_slug="owner/repo",
            auth_probe=probe,
        )

        self.assertFalse(profile["auth"]["ready"])
        self.assertEqual(release_campaigns.hosted_dispatch_blockers(profile), ["auth"])
        self.assertIn(CURSOR_TOKEN_ENV, profile["auth"]["remediation"])
        self.assertIn("gh", profile["auth"]["remediation"])


class GhAuthProbeSecrecyTests(unittest.TestCase):
    """`gh auth token` prints the token; the probe must return only a verdict."""

    def _completed(self, *, returncode: int, stdout: str) -> Any:
        return subprocess.CompletedProcess(
            args=["gh", "auth", "token"], returncode=returncode, stdout=stdout, stderr=""
        )

    def test_probe_returns_a_bare_bool_and_never_the_token(self) -> None:
        with mock.patch(
            "code_mower.release_campaigns.subprocess.run",
            return_value=self._completed(returncode=0, stdout=f"{FAKE_GH_AUTH_TOKEN_VALUE}\n"),
        ) as run:
            result = release_campaigns.run_gh_auth_probe()

        self.assertIs(result, True)
        argv = list(run.call_args.args[0])
        self.assertEqual(argv, ["gh", "auth", "token"])
        # Bounded by construction: the probe can never stall a campaign command.
        self.assertEqual(
            run.call_args.kwargs["timeout"],
            release_campaigns.GH_AUTH_PROBE_TIMEOUT_SECONDS,
        )

    def test_probe_fails_closed_on_nonzero_exit(self) -> None:
        with mock.patch(
            "code_mower.release_campaigns.subprocess.run",
            return_value=self._completed(returncode=1, stdout=""),
        ):
            self.assertIs(release_campaigns.run_gh_auth_probe(), False)

    def test_probe_fails_closed_on_empty_output(self) -> None:
        """A zero exit with nothing on stdout is not a usable credential."""
        with mock.patch(
            "code_mower.release_campaigns.subprocess.run",
            return_value=self._completed(returncode=0, stdout="  \n"),
        ):
            self.assertIs(release_campaigns.run_gh_auth_probe(), False)

    def test_probe_fails_closed_when_gh_is_missing_or_times_out(self) -> None:
        for exc in (
            FileNotFoundError("gh"),
            subprocess.TimeoutExpired(cmd=["gh", "auth", "token"], timeout=10),
        ):
            with self.subTest(error=type(exc).__name__):
                with mock.patch(
                    "code_mower.release_campaigns.subprocess.run", side_effect=exc
                ):
                    self.assertIs(release_campaigns.run_gh_auth_probe(), False)

    def test_no_probe_output_reaches_campaign_state_or_rendered_text(self) -> None:
        """A campaign dispatched on a `gh` credential stores no trace of it."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            argvs: list[list[str]] = []
            with mock.patch(
                "code_mower.release_campaigns.subprocess.run",
                return_value=self._completed(returncode=0, stdout=f"{FAKE_GH_AUTH_TOKEN_VALUE}\n"),
            ):
                release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    providers=["cursor_cloud_agent"],
                    campaigns_dir=campaigns_dir,
                    repo_path=Path(tmp),
                    repo_slug="owner/repo",
                    issue="817",
                    apply=True,
                    command_runner=_dispatch_runner(argvs),
                    gh_json_runner=_empty_gh_json_runner(),
                    env={CURSOR_TRANSPORT_ENV: "1"},
                    auth_probe=release_campaigns.run_gh_auth_probe,
                )

            stored = release_campaigns.load_campaign_by_id(
                "campaign-v1.0.0", campaigns_dir
            )
            assert stored is not None
            self.assertEqual(stored["providers"][0]["state"], "running")
            serialized = json.dumps(stored)
            self.assertNotIn(FAKE_GH_AUTH_TOKEN_VALUE, serialized)
            self.assertNotIn("ghp_", serialized)
            rendered = release_campaigns.render_campaign_text(stored)
            self.assertNotIn(FAKE_GH_AUTH_TOKEN_VALUE, rendered)
            self.assertNotIn(FAKE_GH_AUTH_TOKEN_VALUE, json.dumps(argvs))


class GhAuthFallbackReachesTheCampaignTests(unittest.TestCase):
    """The regression itself: an authenticated `gh` dispatches instead of blocking."""

    def _campaign_kwargs(self, tmp: Path, **overrides: Any) -> dict[str, Any]:
        kwargs: dict[str, Any] = dict(
            release_tag="v1.0.0",
            package_spec="code-mower==1.0.0",
            providers=["cursor_cloud_agent"],
            campaigns_dir=tmp / "campaigns",
            repo_path=tmp,
            repo_slug="owner/repo",
            issue="817",
            env=_verified_cursor_env(token=False),
            gh_json_runner=_empty_gh_json_runner(),
        )
        kwargs.update(overrides)
        return kwargs

    def test_dry_run_previews_queued_when_gh_is_authenticated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            release_campaigns.campaign_command(
                **self._campaign_kwargs(root, auth_probe=_RecordingProbe(True))
            )
            stored = release_campaigns.load_campaign_by_id(
                "campaign-v1.0.0", root / "campaigns"
            )
            assert stored is not None
            self.assertEqual(stored["providers"][0]["state"], "queued")

    def test_dry_run_previews_unavailable_when_gh_is_not_authenticated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            release_campaigns.campaign_command(
                **self._campaign_kwargs(root, auth_probe=_RecordingProbe(False))
            )
            stored = release_campaigns.load_campaign_by_id(
                "campaign-v1.0.0", root / "campaigns"
            )
            assert stored is not None
            entry = stored["providers"][0]
            self.assertEqual(entry["state"], "unavailable")
            self.assertIn(CURSOR_TOKEN_ENV, entry["next_action"])

    def test_applied_dispatch_posts_the_comment_on_a_gh_credential(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            argvs: list[list[str]] = []
            release_campaigns.campaign_command(
                **self._campaign_kwargs(
                    root,
                    apply=True,
                    command_runner=_dispatch_runner(argvs),
                    auth_probe=_RecordingProbe(True),
                )
            )

            posted = _commented_issue_numbers(argvs)
            self.assertTrue(posted)
            self.assertEqual(set(posted), {"817"})


class CampaignIssueBindingTests(unittest.TestCase):
    """`--issue` is durable campaign identity, not a per-invocation argument."""

    def test_validate_issue_number_accepts_only_positive_numbers(self) -> None:
        self.assertEqual(release_campaigns.validate_issue_number(""), "")
        self.assertEqual(release_campaigns.validate_issue_number(None), "")
        self.assertEqual(release_campaigns.validate_issue_number(" 817 "), "817")
        self.assertEqual(release_campaigns.validate_issue_number(817), "817")
        for bad in (
            "0",
            "-3",
            "12a",
            "https://github.com/o/r/issues/817",
            "817 --repo other/repo",
            True,
        ):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    release_campaigns.validate_issue_number(bad)

    def test_stored_issue_number_fails_closed_on_hand_edited_values(self) -> None:
        for stored in ({"issue_number": "; rm -rf /"}, {"issue_number": ["817"]}, {}):
            with self.subTest(stored=stored):
                self.assertEqual(release_campaigns.stored_issue_number(stored), "")
        self.assertEqual(
            release_campaigns.stored_issue_number({"issue_number": "817"}), "817"
        )

    def test_creation_with_an_issue_stores_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            release_campaigns.campaign_command(
                action="create",
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["cursor_cloud_agent"],
                campaigns_dir=root / "campaigns",
                repo_path=root,
                repo_slug="owner/repo",
                issue="817",
                env=_verified_cursor_env(),
                gh_json_runner=_empty_gh_json_runner(),
            )
            stored = release_campaigns.load_campaign_by_id(
                "campaign-v1.0.0", root / "campaigns"
            )
            assert stored is not None
            self.assertEqual(stored["issue_number"], "817")

    def test_malformed_issue_is_refused_before_any_campaign_is_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            err = io.StringIO()
            command_runner = mock.MagicMock()

            code = release_campaigns.campaign_command(
                action="create",
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["cursor_cloud_agent"],
                campaigns_dir=root / "campaigns",
                repo_path=root,
                repo_slug="owner/repo",
                issue="817 --repo attacker/repo",
                env=_verified_cursor_env(),
                command_runner=command_runner,
                stderr=err,
            )

            self.assertEqual(code, 1)
            self.assertIn("positive GitHub issue number", err.getvalue())
            command_runner.assert_not_called()
            self.assertIsNone(
                release_campaigns.load_campaign_by_id(
                    "campaign-v1.0.0", root / "campaigns"
                )
            )

    def test_dispatch_reuses_the_stored_issue_without_the_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            campaigns_dir = root / "campaigns"
            release_campaigns.campaign_command(
                action="create",
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["cursor_cloud_agent"],
                campaigns_dir=campaigns_dir,
                repo_path=root,
                repo_slug="owner/repo",
                issue="817",
                env=_verified_cursor_env(),
                gh_json_runner=_empty_gh_json_runner(),
            )

            argvs: list[list[str]] = []
            release_campaigns.campaign_command(
                action="dispatch",
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                repo_path=root,
                apply=True,
                command_runner=_dispatch_runner(argvs),
                gh_json_runner=_empty_gh_json_runner(),
                env=_verified_cursor_env(),
            )

            posted = _commented_issue_numbers(argvs)
            self.assertTrue(posted)
            self.assertEqual(set(posted), {"817"})

    def test_resume_and_result_discovery_poll_the_stored_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            campaigns_dir = root / "campaigns"
            campaign = release_campaigns.initialize_campaign(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["cursor_cloud_agent"],
                repo_slug="owner/repo",
                issue_number="817",
            )
            provider = campaign.providers[0]
            campaign.status = "running"
            provider["state"] = "running"
            provider["attempted_at"] = "2024-01-01T00:00:00Z"
            provider["dispatch_ref"] = {"issue_number": "817", "comment_posted": True}
            release_campaigns.save_campaign(campaign.to_dict(), campaigns_dir)

            calls: list[tuple[str, ...]] = []
            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                repo_path=root,
                resume=True,
                gh_json_runner=_empty_gh_json_runner(calls),
                env=_verified_cursor_env(),
            )

            self.assertTrue(calls)
            self.assertEqual(calls[0][:3], ("issue", "view", "817"))

    def test_retry_without_the_flag_redispatches_on_the_stored_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            campaigns_dir = root / "campaigns"
            release_campaigns.campaign_command(
                action="create",
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["cursor_cloud_agent"],
                campaigns_dir=campaigns_dir,
                repo_path=root,
                repo_slug="owner/repo",
                issue="817",
                apply=True,
                command_runner=_dispatch_runner([]),
                gh_json_runner=_empty_gh_json_runner(),
                env=_verified_cursor_env(),
            )

            argvs: list[list[str]] = []
            release_campaigns.campaign_command(
                action="dispatch",
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                repo_path=root,
                apply=True,
                retry_provider="cursor_cloud_agent",
                command_runner=_dispatch_runner(argvs),
                gh_json_runner=_empty_gh_json_runner(),
                env=_verified_cursor_env(),
            )

            posted = _commented_issue_numbers(argvs)
            self.assertTrue(posted)
            self.assertEqual(set(posted), {"817"})

    def test_watch_polls_the_stored_issue_without_the_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            campaigns_dir = root / "campaigns"
            campaign = release_campaigns.initialize_campaign(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["cursor_cloud_agent"],
                repo_slug="owner/repo",
                issue_number="817",
            )
            provider = campaign.providers[0]
            campaign.status = "running"
            provider["state"] = "running"
            provider["attempted_at"] = "2024-01-01T00:00:00Z"
            provider["dispatch_ref"] = {"issue_number": "817", "comment_posted": True}
            release_campaigns.save_campaign(campaign.to_dict(), campaigns_dir)

            calls: list[tuple[str, ...]] = []
            release_campaigns.campaign_watch(
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                repo_path=root,
                interval=0.01,
                timeout=0.02,
                emit_json=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                sleep_fn=lambda _s: None,
                gh_json_runner=_empty_gh_json_runner(calls),
                env=_verified_cursor_env(),
            )

            self.assertTrue(calls)
            self.assertEqual(calls[0][:3], ("issue", "view", "817"))


class CampaignIssueConflictTests(unittest.TestCase):
    """A later `--issue` naming a different issue is refused, not honored."""

    def _bound_campaign(self, campaigns_dir: Path) -> None:
        campaign = release_campaigns.initialize_campaign(
            release_tag="v1.0.0",
            package_spec="code-mower==1.0.0",
            providers=["cursor_cloud_agent"],
            repo_slug="owner/repo",
            issue_number="817",
        )
        release_campaigns.save_campaign(campaign.to_dict(), campaigns_dir)

    def test_conflicting_issue_is_rejected_before_any_gh_call_or_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            campaigns_dir = root / "campaigns"
            self._bound_campaign(campaigns_dir)
            before = json.dumps(
                release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir),
                sort_keys=True,
            )
            command_runner = mock.MagicMock()
            gh_json_runner = mock.MagicMock()
            err = io.StringIO()

            with contextlib.redirect_stderr(err):
                code = release_campaigns.campaign_command(
                    action="dispatch",
                    release_tag="v1.0.0",
                    campaigns_dir=campaigns_dir,
                    repo_path=root,
                    issue="999",
                    apply=True,
                    command_runner=command_runner,
                    gh_json_runner=gh_json_runner,
                    env=_verified_cursor_env(),
                )

            self.assertEqual(code, 1)
            self.assertIn("--issue '999'", err.getvalue())
            self.assertIn("817", err.getvalue())
            command_runner.assert_not_called()
            gh_json_runner.assert_not_called()
            after = json.dumps(
                release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir),
                sort_keys=True,
            )
            self.assertEqual(before, after)

    def test_repeating_the_bound_issue_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            campaigns_dir = root / "campaigns"
            self._bound_campaign(campaigns_dir)

            code = release_campaigns.campaign_command(
                action="resume",
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                repo_path=root,
                issue="817",
                gh_json_runner=_empty_gh_json_runner(),
                env=_verified_cursor_env(),
            )

            self.assertEqual(code, 0)

    def test_watch_refuses_a_conflicting_issue_before_polling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            campaigns_dir = root / "campaigns"
            self._bound_campaign(campaigns_dir)
            gh_json_runner = mock.MagicMock()

            summary = release_campaigns.campaign_watch(
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                repo_path=root,
                issue="999",
                interval=0.01,
                timeout=0.02,
                emit_json=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                sleep_fn=lambda _s: None,
                gh_json_runner=gh_json_runner,
                env=_verified_cursor_env(),
            )

            self.assertEqual(summary["stop_reason"], "invalid_campaign")
            gh_json_runner.assert_not_called()


class PreBindingCampaignCompatibilityTests(unittest.TestCase):
    """Campaigns stored before issue binding existed stay usable, and bind once."""

    def _legacy_campaign(self, campaigns_dir: Path) -> dict[str, Any]:
        campaign = release_campaigns.initialize_campaign(
            release_tag="v1.0.0",
            package_spec="code-mower==1.0.0",
            providers=["cursor_cloud_agent"],
            repo_slug="owner/repo",
        ).to_dict()
        # Exactly the on-disk shape of a campaign written before the field
        # existed: the key is absent, not empty.
        campaign.pop("issue_number")
        release_campaigns.save_campaign(campaign, campaigns_dir)
        return campaign

    def test_campaign_without_the_field_reads_as_unbound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            legacy = self._legacy_campaign(campaigns_dir)
            self.assertNotIn("issue_number", legacy)
            stored = release_campaigns.load_campaign_by_id(
                "campaign-v1.0.0", campaigns_dir
            )
            assert stored is not None
            self.assertEqual(release_campaigns.stored_issue_number(stored), "")

    def test_legacy_campaign_is_completed_once_with_an_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            campaigns_dir = root / "campaigns"
            self._legacy_campaign(campaigns_dir)

            argvs: list[list[str]] = []
            release_campaigns.campaign_command(
                action="dispatch",
                release_tag="v1.0.0",
                campaigns_dir=campaigns_dir,
                repo_path=root,
                issue="817",
                apply=True,
                command_runner=_dispatch_runner(argvs),
                gh_json_runner=_empty_gh_json_runner(),
                env=_verified_cursor_env(),
            )

            stored = release_campaigns.load_campaign_by_id(
                "campaign-v1.0.0", campaigns_dir
            )
            assert stored is not None
            self.assertEqual(stored["issue_number"], "817")
            self.assertEqual(set(_commented_issue_numbers(argvs)), {"817"})

            # Bound once: a different issue is now refused.
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = release_campaigns.campaign_command(
                    action="resume",
                    release_tag="v1.0.0",
                    campaigns_dir=campaigns_dir,
                    repo_path=root,
                    issue="999",
                    gh_json_runner=_empty_gh_json_runner(),
                    env=_verified_cursor_env(),
                )
            self.assertEqual(code, 1)
            self.assertIn("fixed once set", err.getvalue())


class WatchIssueBindingTests(unittest.TestCase):
    """`watch --issue` binds the campaign issue durably, and binds it once.

    Watching is the long-running command an operator leaves attached to a
    campaign, so it is frequently the first place a pre-migration campaign is
    told which issue it belongs to. Polling that issue without recording it
    would make every later invocation re-supply `--issue` -- the exact defect
    the mutating route already fixes -- so the binding is completed under the
    campaigns lock, before the first poll, on the same fill-once terms.
    """

    def _running_campaign(self, campaigns_dir: Path, *, bound: bool) -> None:
        """Store a dispatched campaign, with or without a bound issue.

        The unbound variant carries no `issue_number` key at all: the on-disk
        shape of a campaign written before the field existed.
        """
        campaign = release_campaigns.initialize_campaign(
            release_tag="v1.0.0",
            package_spec="code-mower==1.0.0",
            providers=["cursor_cloud_agent"],
            repo_slug="owner/repo",
        ).to_dict()
        provider = campaign["providers"][0]
        campaign["status"] = "running"
        provider["state"] = "running"
        provider["attempted_at"] = "2024-01-01T00:00:00Z"
        provider["dispatch_ref"] = {"issue_number": "817", "comment_posted": True}
        if bound:
            campaign["issue_number"] = "817"
        else:
            campaign.pop("issue_number")
        release_campaigns.save_campaign(campaign, campaigns_dir)

    def _watch(
        self,
        campaigns_dir: Path,
        repo_path: Path,
        *,
        issue: str = "",
        gh_json_runner: Any = None,
    ) -> dict[str, Any]:
        return release_campaigns.campaign_watch(
            release_tag="v1.0.0",
            campaigns_dir=campaigns_dir,
            repo_path=repo_path,
            issue=issue,
            interval=0.01,
            timeout=0.02,
            emit_json=True,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            sleep_fn=lambda _s: None,
            gh_json_runner=gh_json_runner or _empty_gh_json_runner(),
            env=_verified_cursor_env(),
        )

    def _stored(self, campaigns_dir: Path) -> dict[str, Any]:
        stored = release_campaigns.load_campaign_by_id(
            "campaign-v1.0.0", campaigns_dir
        )
        assert stored is not None
        return stored

    def test_watch_binds_the_first_explicit_issue(self) -> None:
        """The issue is persisted, and this same run already polls it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            campaigns_dir = root / "campaigns"
            self._running_campaign(campaigns_dir, bound=False)

            calls: list[tuple[str, ...]] = []
            self._watch(
                campaigns_dir,
                root,
                issue="817",
                gh_json_runner=_empty_gh_json_runner(calls),
            )

            self.assertEqual(self._stored(campaigns_dir)["issue_number"], "817")
            self.assertTrue(calls)
            self.assertEqual(calls[0][:3], ("issue", "view", "817"))

    def test_later_watch_without_the_flag_reuses_the_bound_issue(self) -> None:
        """Having bound it once, the operator never names the issue again."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            campaigns_dir = root / "campaigns"
            self._running_campaign(campaigns_dir, bound=False)
            self._watch(campaigns_dir, root, issue="817")

            calls: list[tuple[str, ...]] = []
            self._watch(
                campaigns_dir, root, gh_json_runner=_empty_gh_json_runner(calls)
            )

            self.assertTrue(calls)
            self.assertEqual(calls[0][:3], ("issue", "view", "817"))
            self.assertEqual(self._stored(campaigns_dir)["issue_number"], "817")

    def test_later_different_issue_is_refused_before_polling_or_mutation(self) -> None:
        """The fill-once guarantee survives: the binding is never repointed."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            campaigns_dir = root / "campaigns"
            self._running_campaign(campaigns_dir, bound=False)
            self._watch(campaigns_dir, root, issue="817")
            before = json.dumps(self._stored(campaigns_dir), sort_keys=True)

            gh_json_runner = mock.MagicMock()
            summary = self._watch(
                campaigns_dir, root, issue="999", gh_json_runner=gh_json_runner
            )

            self.assertEqual(summary["stop_reason"], "invalid_campaign")
            self.assertIn("fixed once set", summary["error"])
            gh_json_runner.assert_not_called()
            self.assertEqual(before, json.dumps(self._stored(campaigns_dir), sort_keys=True))

    def test_binding_is_rechecked_against_the_campaign_loaded_under_the_lock(self) -> None:
        """A binding completed after the pre-lock read still wins.

        The conflict check that precedes the lock sees a stale record, so a
        campaign bound in between must be re-checked against the copy actually
        loaded under the lock -- otherwise the watch would overwrite a binding
        the fill-once rule promises is permanent.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            campaigns_dir = root / "campaigns"
            self._running_campaign(campaigns_dir, bound=False)
            stale = self._stored(campaigns_dir)
            # Another invocation binds 817 between the caller's read and the lock.
            self._watch(campaigns_dir, root, issue="817")

            gh_json_runner = mock.MagicMock()
            summary = release_campaigns.campaign_watch(
                stale,
                campaigns_dir=campaigns_dir,
                repo_path=root,
                issue="999",
                interval=0.01,
                timeout=0.02,
                emit_json=True,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                sleep_fn=lambda _s: None,
                gh_json_runner=gh_json_runner,
                env=_verified_cursor_env(),
            )

            self.assertEqual(summary["stop_reason"], "invalid_campaign")
            gh_json_runner.assert_not_called()
            self.assertEqual(self._stored(campaigns_dir)["issue_number"], "817")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
