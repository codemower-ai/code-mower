#!/usr/bin/env python3
"""Focused tests for issue #711: retry chronology and terminal failure evidence.

Retry of provider A must not alter provider B's attempt, dispatch, or
completion timestamps; the retried provider keeps a bounded metadata-only
attempt summary; and upload converts every schema-valid terminal result
(`pass`, `pass_with_warnings`, `fail`, and schema-valid `incomplete`) while
still skipping no-result providers and rejecting invalid stored results.
"""

from __future__ import annotations

import contextlib
import io
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

from code_mower import release_campaigns
from code_mower.provider_registry import LaneLabels, ProviderLane


OLD_TS = "2026-09-04T00:00:00Z"
OLD_DONE_TS = "2026-09-04T00:01:00Z"


def _mock_result(provider: str, outcome: str, *, execution_state: str = "executed") -> dict[str, Any]:
    fail_step = outcome == "fail"
    return {
        "schema": "code_mower.adoptionResult.v1",
        "timestamp_utc": "2026-09-04T08:00:00Z",
        "release_tag": "v1.0.0",
        "package_identity": "code-mower",
        "normalized_version": "1.0.0",
        "qualification_context": "cold_install",
        "starting_version": "",
        "ending_version": "1.0.0",
        "provider": provider,
        "executor": provider,
        "host_class": "local",
        "runtime_class": "python_3.12",
        "execution_state": execution_state,
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
                "status": "fail" if fail_step else "pass",
                "elapsed_seconds": 11.14,
                "warning_count": 0,
                "owner_action_count": 0,
            },
        ],
    }


def _fake_lane(provider: str) -> ProviderLane:
    return ProviderLane(
        lane_id=f"fake_{provider}",
        lane_type="audit",
        driver="local_cli",
        provider=provider,
        labels=LaneLabels(needs="needs-fake", done="fake-done", blocked="fake-blocked"),
        trigger_policy="manual",
        provider_config={
            "command": "fake-provider-cli",
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


def _writing_runner(
    outcome: str, invocations: list[list[str]], provider: str = "codex"
):
    def _run(argv: Any, timeout: int) -> subprocess.CompletedProcess[str]:
        invocations.append(list(argv))
        argv_list = list(argv)
        output_path = Path(argv_list[argv_list.index("--output") + 1])
        output_path.write_text(json.dumps(_mock_result(provider, outcome)), encoding="utf-8")
        return subprocess.CompletedProcess(argv_list, 0, stdout="", stderr="")

    return _run


class RetryChronologyTests(unittest.TestCase):
    """Retrying A leaves B's attempt/dispatch/completion timestamps alone."""

    def _seed_two_provider_campaign(self, campaigns_dir: Path) -> None:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["codex", "claude"],
                campaigns_dir=campaigns_dir,
            )
            campaign = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert campaign is not None
            release_campaigns.record_manual_result(
                campaign, "codex", _mock_result("codex", "fail"), campaigns_dir=campaigns_dir
            )
            campaign = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert campaign is not None
            release_campaigns.record_manual_result(
                campaign, "claude", _mock_result("claude", "pass"), campaigns_dir=campaigns_dir
            )
        path = campaigns_dir / "campaign-v1.0.0.json"
        stored = json.loads(path.read_text(encoding="utf-8"))
        for entry in stored["providers"]:
            entry["attempted_at"] = OLD_TS
            entry["dispatched_at"] = OLD_TS
            entry["completed_at"] = OLD_DONE_TS
        path.write_text(json.dumps(stored), encoding="utf-8")

    def _resolve(self, name: str) -> tuple[str, ProviderLane]:
        lane = _fake_lane("codex" if name == "codex" else "claude")
        return lane.provider, lane

    def test_retry_advances_only_the_retried_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._seed_two_provider_campaign(campaigns_dir)
            invocations: list[list[str]] = []

            with mock.patch.object(
                release_campaigns, "resolve_provider_lane", side_effect=self._resolve
            ):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                    io.StringIO()
                ):
                    release_campaigns.campaign_command(
                        release_tag="v1.0.0",
                        campaigns_dir=campaigns_dir,
                        resume=True,
                        apply=True,
                        retry_provider="codex",
                        which_fn=lambda _cmd: "/bin/fake-provider-cli",
                        adapter_runner=_writing_runner("pass", invocations),
                    )

            # Only the retried provider's adapter ran.
            self.assertEqual(len(invocations), 1)
            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            by_name = {p["provider"]: p for p in saved["providers"]}

            # The retried provider advanced to the new passing evidence and
            # kept a bounded summary of the superseded failing attempt.
            codex = by_name["codex"]
            self.assertEqual(codex["state"], "complete")
            self.assertEqual(codex["adoption_result"]["outcome"], "pass")
            self.assertNotEqual(codex["completed_at"], OLD_DONE_TS)
            history = codex.get("attempt_history")
            assert isinstance(history, list) and len(history) == 1
            self.assertEqual(history[0]["outcome"], "fail")
            self.assertEqual(history[0]["completed_at"], OLD_DONE_TS)
            self.assertEqual(history[0]["state"], "blocked")

            # The unrelated terminal provider is byte-identical in chronology
            # and evidence.
            claude = by_name["claude"]
            self.assertEqual(claude["attempted_at"], OLD_TS)
            self.assertEqual(claude["dispatched_at"], OLD_TS)
            self.assertEqual(claude["completed_at"], OLD_DONE_TS)
            self.assertEqual(claude["state"], "complete")
            self.assertEqual(claude["adoption_result"]["outcome"], "pass")
            self.assertEqual(claude["next_action"], "none")

    def test_retry_leaves_queued_providers_undispatched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    providers=["codex", "claude"],
                    campaigns_dir=campaigns_dir,
                )
                campaign = release_campaigns.load_campaign_by_id(
                    "campaign-v1.0.0", campaigns_dir
                )
                assert campaign is not None
                release_campaigns.record_manual_result(
                    campaign, "codex", _mock_result("codex", "fail"), campaigns_dir=campaigns_dir
                )
            path = campaigns_dir / "campaign-v1.0.0.json"
            stored = json.loads(path.read_text(encoding="utf-8"))
            for entry in stored["providers"]:
                if entry["provider"] == "codex":
                    entry["attempted_at"] = OLD_TS
                elif entry["provider"] == "claude":
                    entry["state"] = "queued"
                    entry["attempted_at"] = None
                    entry["dispatched_at"] = None
                    entry["completed_at"] = None
            path.write_text(json.dumps(stored), encoding="utf-8")
            invocations: list[list[str]] = []

            with mock.patch.object(
                release_campaigns, "resolve_provider_lane", side_effect=self._resolve
            ):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                    io.StringIO()
                ):
                    release_campaigns.campaign_command(
                        release_tag="v1.0.0",
                        campaigns_dir=campaigns_dir,
                        resume=True,
                        apply=True,
                        retry_provider="codex",
                        which_fn=lambda _cmd: "/bin/fake-provider-cli",
                        adapter_runner=_writing_runner("fail", invocations),
                    )

            self.assertEqual(len(invocations), 1)
            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            claude = next(p for p in saved["providers"] if p["provider"] == "claude")
            self.assertEqual(claude["state"], "queued")
            self.assertIsNone(claude.get("attempted_at"))
            self.assertIsNone(claude.get("dispatched_at"))
            self.assertIsNone(claude.get("completed_at"))

    def test_identical_evidence_re_poll_does_not_move_completion(self) -> None:
        """An ordinary resume that re-reads unchanged evidence is chronology-stable."""
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._seed_two_provider_campaign(campaigns_dir)
            invocations: list[list[str]] = []

            def _forbidden(argv: Any, timeout: int) -> subprocess.CompletedProcess[str]:
                raise AssertionError("resume must not re-invoke an attempted adapter")

            with mock.patch.object(
                release_campaigns, "resolve_provider_lane", side_effect=self._resolve
            ):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                    io.StringIO()
                ):
                    release_campaigns.campaign_command(
                        release_tag="v1.0.0",
                        campaigns_dir=campaigns_dir,
                        resume=True,
                        apply=True,
                        which_fn=lambda _cmd: "/bin/fake-provider-cli",
                        adapter_runner=_forbidden,
                    )

            self.assertEqual(invocations, [])
            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            for entry in saved["providers"]:
                self.assertEqual(entry["completed_at"], OLD_DONE_TS)
                self.assertEqual(entry.get("attempt_history", []), [])


class AttemptHistoryTests(unittest.TestCase):
    """Prior attempts stay auditable: bounded, metadata-only, no raw output."""

    def test_history_is_bounded_and_metadata_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    providers=["codex"],
                    campaigns_dir=campaigns_dir,
                )
            invocations: list[list[str]] = []
            fake_lane = _fake_lane("codex")

            with mock.patch.object(
                release_campaigns, "resolve_provider_lane", return_value=("codex", fake_lane)
            ):
                for _ in range(7):
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                        io.StringIO()
                    ):
                        release_campaigns.campaign_command(
                            release_tag="v1.0.0",
                            campaigns_dir=campaigns_dir,
                            resume=True,
                            apply=True,
                            retry_provider="codex",
                            which_fn=lambda _cmd: "/bin/fake-provider-cli",
                            adapter_runner=_writing_runner("fail", invocations),
                        )

            self.assertEqual(len(invocations), 7)
            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            history = saved["providers"][0].get("attempt_history")
            assert isinstance(history, list)
            self.assertEqual(len(history), release_campaigns.MAX_ATTEMPT_HISTORY_ENTRIES)
            for entry in history:
                self.assertEqual(
                    set(entry),
                    {
                        "attempted_at",
                        "dispatched_at",
                        "completed_at",
                        "state",
                        "outcome",
                        "error",
                        "elapsed_seconds",
                    },
                )
                self.assertEqual(entry["outcome"], "fail")
            # History carries summaries only: no step detail, raw output, or paths.
            history_blob = json.dumps(history)
            for marker in ("package_install", "steps", "stdout", "stderr", "/", ".json"):
                self.assertNotIn(marker, history_blob)


class TerminalUploadTests(unittest.TestCase):
    """Fail/incomplete terminal evidence uploads; no-result skips; invalid rejects."""

    def _seed(self, campaigns_dir: Path) -> None:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            release_campaigns.campaign_command(
                release_tag="v1.0.0",
                package_spec="code-mower==1.0.0",
                providers=["codex", "claude"],
                campaigns_dir=campaigns_dir,
            )
            campaign = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert campaign is not None
            release_campaigns.record_manual_result(
                campaign, "codex", _mock_result("codex", "fail"), campaigns_dir=campaigns_dir
            )
            campaign = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert campaign is not None
            release_campaigns.record_manual_result(
                campaign, "claude", _mock_result("claude", "pass"), campaigns_dir=campaigns_dir
            )

    def test_blocked_fail_result_converts_to_adoption_run_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._seed(campaigns_dir)
            campaign = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert campaign is not None
            plan = release_campaigns.build_campaign_upload_events(campaign)
            self.assertEqual(plan["accepted_providers"], ["claude", "codex"])
            self.assertEqual(plan["rejected_providers"], [])
            self.assertEqual(plan["skipped_providers"], [])
            self.assertEqual(len(plan["events"]), 2)
            by_provider = {e["dimensions"]["provider"]: e for e in plan["events"]}
            self.assertEqual(by_provider["codex"]["dimensions"]["outcome"], "fail")
            self.assertEqual(by_provider["claude"]["dimensions"]["outcome"], "pass")
            for event in plan["events"]:
                # Counts cross (step_pass_count); the raw steps array never does.
                self.assertNotIn('"steps":', json.dumps(event))

    def test_planned_incomplete_result_converts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    providers=["codex"],
                    campaigns_dir=campaigns_dir,
                )
                campaign = release_campaigns.load_campaign_by_id(
                    "campaign-v1.0.0", campaigns_dir
                )
                assert campaign is not None
                release_campaigns.record_manual_result(
                    campaign,
                    "codex",
                    _mock_result("codex", "incomplete", execution_state="planned"),
                    campaigns_dir=campaigns_dir,
                )
            campaign = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert campaign is not None
            self.assertEqual(campaign["providers"][0]["state"], "blocked")
            plan = release_campaigns.build_campaign_upload_events(campaign)
            self.assertEqual(plan["accepted_providers"], ["codex"])
            self.assertEqual(plan["events"][0]["dimensions"]["outcome"], "incomplete")

    def test_blocked_without_result_is_skipped_not_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._seed(campaigns_dir)
            path = campaigns_dir / "campaign-v1.0.0.json"
            stored = json.loads(path.read_text(encoding="utf-8"))
            for entry in stored["providers"]:
                if entry["provider"] == "codex":
                    entry["state"] = "blocked"
                    entry["adoption_result"] = None
                    entry["error"] = "adapter_produced_no_result"
            path.write_text(json.dumps(stored), encoding="utf-8")
            campaign = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert campaign is not None
            plan = release_campaigns.build_campaign_upload_events(campaign)
            self.assertEqual(plan["accepted_providers"], ["claude"])
            self.assertEqual(plan["rejected_providers"], [])
            self.assertEqual([r["provider"] for r in plan["skipped_providers"]], ["codex"])

    def test_blocked_with_invalid_result_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            self._seed(campaigns_dir)
            path = campaigns_dir / "campaign-v1.0.0.json"
            stored = json.loads(path.read_text(encoding="utf-8"))
            for entry in stored["providers"]:
                if entry["provider"] == "codex":
                    entry["adoption_result"]["outcome"] = "bogus"
            path.write_text(json.dumps(stored), encoding="utf-8")
            campaign = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert campaign is not None
            plan = release_campaigns.build_campaign_upload_events(campaign)
            self.assertEqual(plan["accepted_providers"], ["claude"])
            self.assertEqual(len(plan["rejected_providers"]), 1)
            rejected = plan["rejected_providers"][0]
            self.assertEqual(rejected["provider"], "codex")
            self.assertEqual(rejected["reason"], "adoption_result_invalid")


class RetryEvidenceClearingTests(unittest.TestCase):
    """A result-less failed retry must not upload superseded evidence."""

    def test_failed_retry_clears_superseded_evidence_and_upload_skips_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    providers=["codex", "claude"],
                    campaigns_dir=campaigns_dir,
                )
                campaign = release_campaigns.load_campaign_by_id(
                    "campaign-v1.0.0", campaigns_dir
                )
                assert campaign is not None
                release_campaigns.record_manual_result(
                    campaign, "codex", _mock_result("codex", "fail"),
                    campaigns_dir=campaigns_dir,
                )
                campaign = release_campaigns.load_campaign_by_id(
                    "campaign-v1.0.0", campaigns_dir
                )
                assert campaign is not None
                release_campaigns.record_manual_result(
                    campaign, "claude", _mock_result("claude", "pass"),
                    campaigns_dir=campaigns_dir,
                )

            def _resolve(name: str) -> tuple[str, ProviderLane]:
                return name, _fake_lane(name)

            invocations: list[list[str]] = []

            def _failing_runner(argv: Any, timeout: int) -> subprocess.CompletedProcess[str]:
                invocations.append(list(argv))
                return subprocess.CompletedProcess(list(argv), 1, stdout="", stderr="boom")

            with mock.patch.object(
                release_campaigns, "resolve_provider_lane", side_effect=_resolve
            ):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                    io.StringIO()
                ):
                    release_campaigns.campaign_command(
                        release_tag="v1.0.0",
                        campaigns_dir=campaigns_dir,
                        resume=True,
                        apply=True,
                        retry_provider="codex",
                        which_fn=lambda _cmd: "/bin/fake-provider-cli",
                        adapter_runner=_failing_runner,
                    )

            self.assertEqual(len(invocations), 1)
            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            by_name = {p["provider"]: p for p in saved["providers"]}
            codex = by_name["codex"]
            # Superseded evidence is cleared before the new attempt, so the
            # result-less retry leaves nothing uploadable behind.
            self.assertIsNone(codex.get("adoption_result"))
            history = codex.get("attempt_history")
            assert isinstance(history, list) and len(history) == 1
            self.assertEqual(history[0]["outcome"], "fail")
            plan = release_campaigns.build_campaign_upload_events(saved)
            self.assertEqual(plan["accepted_providers"], ["claude"])
            self.assertEqual(plan["rejected_providers"], [])
            self.assertEqual(
                [r["provider"] for r in plan["skipped_providers"]], ["codex"]
            )


class RetrySupersededTimestampTests(unittest.TestCase):
    """A failed/interrupted retry keeps no superseded evidence or timestamps.

    Covers the Codex chronology invariant in both retry paths: the local
    adapter path and the hosted redispatch path must archive the bounded
    prior attempt, then clear adoption_result, completed_at, and
    dispatched_at before checkpointing the new attempt -- so an adapter
    failure, a failed dispatch post, or an interruption cannot retain
    superseded timestamps while attempt_history preserves the prior
    bounded summary.
    """

    def test_failed_local_retry_clears_superseded_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    providers=["codex"],
                    campaigns_dir=campaigns_dir,
                )
                campaign = release_campaigns.load_campaign_by_id(
                    "campaign-v1.0.0", campaigns_dir
                )
                assert campaign is not None
                release_campaigns.record_manual_result(
                    campaign, "codex", _mock_result("codex", "fail"),
                    campaigns_dir=campaigns_dir,
                )
            # Simulate a prior adapter-completed attempt with full chronology.
            path = campaigns_dir / "campaign-v1.0.0.json"
            stored = json.loads(path.read_text(encoding="utf-8"))
            stored["providers"][0]["attempted_at"] = OLD_TS
            stored["providers"][0]["dispatched_at"] = OLD_TS
            stored["providers"][0]["completed_at"] = OLD_DONE_TS
            stored["providers"][0].pop("attempt_history", None)
            path.write_text(json.dumps(stored), encoding="utf-8")

            def _resolve(name: str) -> tuple[str, ProviderLane]:
                return name, _fake_lane(name)

            def _failing_runner(argv: Any, timeout: int) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(list(argv), 1, stdout="", stderr="boom")

            with mock.patch.object(
                release_campaigns, "resolve_provider_lane", side_effect=_resolve
            ):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                    io.StringIO()
                ):
                    release_campaigns.campaign_command(
                        release_tag="v1.0.0",
                        campaigns_dir=campaigns_dir,
                        resume=True,
                        apply=True,
                        retry_provider="codex",
                        which_fn=lambda _cmd: "/bin/fake-provider-cli",
                        adapter_runner=_failing_runner,
                    )

            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            codex = saved["providers"][0]
            # No superseded evidence or timestamps survive the failed retry.
            self.assertIsNone(codex.get("adoption_result"))
            self.assertIsNone(codex.get("completed_at"))
            self.assertIsNone(codex.get("dispatched_at"))
            # The new attempt itself is still recorded.
            self.assertTrue(codex.get("attempted_at"))
            self.assertNotEqual(codex.get("attempted_at"), OLD_TS)
            # The prior attempt survives only as a bounded metadata-only summary.
            history = codex.get("attempt_history")
            assert isinstance(history, list) and len(history) == 1
            self.assertEqual(
                set(history[0]),
                {
                    "attempted_at",
                    "dispatched_at",
                    "completed_at",
                    "state",
                    "outcome",
                    "error",
                    "elapsed_seconds",
                },
            )
            self.assertEqual(history[0]["outcome"], "fail")
            self.assertEqual(history[0]["attempted_at"], OLD_TS)
            self.assertEqual(history[0]["dispatched_at"], OLD_TS)
            self.assertEqual(history[0]["completed_at"], OLD_DONE_TS)
            plan = release_campaigns.build_campaign_upload_events(saved)
            self.assertEqual(
                [r["provider"] for r in plan["skipped_providers"]], ["codex"]
            )

    def test_failed_hosted_redispatch_clears_superseded_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    providers=["devin"],
                    campaigns_dir=campaigns_dir,
                    repo_slug="owner/repo",
                    issue="42",
                )
                campaign = release_campaigns.load_campaign_by_id(
                    "campaign-v1.0.0", campaigns_dir
                )
                assert campaign is not None
                release_campaigns.record_manual_result(
                    campaign, "devin", _mock_result("devin", "fail"),
                    campaigns_dir=campaigns_dir,
                )
            # Simulate a prior hosted attempt with full chronology.
            path = campaigns_dir / "campaign-v1.0.0.json"
            stored = json.loads(path.read_text(encoding="utf-8"))
            stored["providers"][0]["attempted_at"] = OLD_TS
            stored["providers"][0]["dispatched_at"] = OLD_TS
            stored["providers"][0]["completed_at"] = OLD_DONE_TS
            stored["providers"][0]["dispatch_ref"] = {
                "issue_number": "42",
                "comment_posted": True,
            }
            stored["providers"][0].pop("attempt_history", None)
            path.write_text(json.dumps(stored), encoding="utf-8")

            hosted_lane = ProviderLane(
                lane_id="fake_devin_hosted",
                lane_type="audit",
                driver="hosted_bridge",
                provider="devin",
                labels=LaneLabels(
                    needs="needs-fake", done="fake-done", blocked="fake-blocked"
                ),
                trigger_policy="manual",
                provider_config={},
            )

            def failing_command_runner(argv: Any, **_kwargs: Any) -> Any:
                return subprocess.CompletedProcess(list(argv), 1, stdout="", stderr="boom")

            def _no_comments(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], str]:
                return {"comments": []}, ""

            with mock.patch.object(
                release_campaigns,
                "resolve_provider_lane",
                return_value=("devin", hosted_lane),
            ):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                    io.StringIO()
                ):
                    release_campaigns.campaign_command(
                        release_tag="v1.0.0",
                        campaigns_dir=campaigns_dir,
                        resume=True,
                        apply=True,
                        retry_provider="devin",
                        repo_slug="owner/repo",
                        issue="42",
                        which_fn=lambda _cmd: "/bin/gh",
                        command_runner=failing_command_runner,
                        gh_json_runner=_no_comments,
                        env={"DEVIN_API_KEY": "token"},
                    )

            saved = release_campaigns.load_campaign_by_id("campaign-v1.0.0", campaigns_dir)
            assert saved is not None
            devin = saved["providers"][0]
            # The failed redispatch records its own outcome with no
            # superseded evidence or timestamps.
            self.assertEqual(devin["state"], "unavailable")
            self.assertIsNone(devin.get("adoption_result"))
            self.assertIsNone(devin.get("completed_at"))
            self.assertIsNone(devin.get("dispatched_at"))
            self.assertTrue(devin.get("attempted_at"))
            self.assertNotEqual(devin.get("attempted_at"), OLD_TS)
            # The prior attempt survives only as a bounded metadata-only summary.
            history = devin.get("attempt_history")
            assert isinstance(history, list) and len(history) == 1
            self.assertEqual(
                set(history[0]),
                {
                    "attempted_at",
                    "dispatched_at",
                    "completed_at",
                    "state",
                    "outcome",
                    "error",
                    "elapsed_seconds",
                },
            )
            self.assertEqual(history[0]["outcome"], "fail")
            self.assertEqual(history[0]["attempted_at"], OLD_TS)
            self.assertEqual(history[0]["dispatched_at"], OLD_TS)
            self.assertEqual(history[0]["completed_at"], OLD_DONE_TS)
            plan = release_campaigns.build_campaign_upload_events(saved)
            self.assertEqual(
                [r["provider"] for r in plan["skipped_providers"]], ["devin"]
            )


class RetryFreezeHostedTests(unittest.TestCase):
    """An unrelated running hosted provider is byte-stable during another retry."""

    def test_retry_freezes_running_hosted_provider_with_expired_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    providers=["codex", "claude"],
                    campaigns_dir=campaigns_dir,
                    repo_slug="owner/repo",
                )
                campaign = release_campaigns.load_campaign_by_id(
                    "campaign-v1.0.0", campaigns_dir
                )
                assert campaign is not None
                release_campaigns.record_manual_result(
                    campaign, "codex", _mock_result("codex", "fail"),
                    campaigns_dir=campaigns_dir,
                )
            path = campaigns_dir / "campaign-v1.0.0.json"
            stored = json.loads(path.read_text(encoding="utf-8"))
            for entry in stored["providers"]:
                if entry["provider"] == "claude":
                    entry["state"] = "running"
                    entry["attempted_at"] = OLD_TS
                    entry["dispatched_at"] = OLD_TS
                    entry["completed_at"] = None
                    entry["adoption_result"] = None
                    entry["error"] = ""
                    entry["trigger_posted"] = True
                    entry["response_deadline_at"] = "2000-01-01T00:00:00Z"
                    entry["dispatch_ref"] = {
                        "issue_number": "99",
                        "comment_posted": True,
                    }
                    entry["next_action"] = "wait for hosted provider result"
                    entry["next_detail"] = "polling"
                    entry.pop("attempt_history", None)
            path.write_text(json.dumps(stored), encoding="utf-8")
            before = release_campaigns.load_campaign_by_id(
                "campaign-v1.0.0", campaigns_dir
            )
            assert before is not None
            frozen_before = json.loads(
                json.dumps(
                    next(
                        p for p in before["providers"] if p["provider"] == "claude"
                    )
                )
            )

            hosted_lane = ProviderLane(
                lane_id="fake_claude_hosted",
                lane_type="audit",
                driver="hosted_bridge",
                provider="claude",
                labels=LaneLabels(
                    needs="needs-fake", done="fake-done", blocked="fake-blocked"
                ),
                trigger_policy="manual",
                provider_config={},
            )

            def _resolve(name: str) -> tuple[str, ProviderLane]:
                if name == "claude":
                    return "claude", hosted_lane
                return "codex", _fake_lane("codex")

            def _forbidden_gh(*args: Any, **kwargs: Any) -> Any:
                raise AssertionError("frozen provider must not be polled")

            invocations: list[list[str]] = []
            with mock.patch.object(
                release_campaigns, "resolve_provider_lane", side_effect=_resolve
            ):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                    io.StringIO()
                ):
                    release_campaigns.campaign_command(
                        release_tag="v1.0.0",
                        campaigns_dir=campaigns_dir,
                        resume=True,
                        apply=True,
                        retry_provider="codex",
                        which_fn=lambda _cmd: "/bin/fake-provider-cli",
                        adapter_runner=_writing_runner("pass", invocations),
                        gh_json_runner=_forbidden_gh,
                    )

            self.assertEqual(len(invocations), 1)
            after = release_campaigns.load_campaign_by_id(
                "campaign-v1.0.0", campaigns_dir
            )
            assert after is not None
            frozen_after = next(
                p for p in after["providers"] if p["provider"] == "claude"
            )
            self.assertEqual(frozen_after, frozen_before)
            self.assertEqual(frozen_after["state"], "running")


class FirstCompletionHistoryTests(unittest.TestCase):
    """First completions stamp completion without consuming history slots."""

    def test_first_dropin_completion_records_no_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    providers=["codex"],
                    campaigns_dir=campaigns_dir,
                )
            path = campaigns_dir / "campaign-v1.0.0.json"
            stored = json.loads(path.read_text(encoding="utf-8"))
            stored["providers"][0]["state"] = "running"
            stored["providers"][0]["attempted_at"] = OLD_TS
            stored["providers"][0]["dispatched_at"] = OLD_TS
            stored["providers"][0]["completed_at"] = None
            stored["providers"][0]["adoption_result"] = None
            stored["providers"][0].pop("attempt_history", None)
            path.write_text(json.dumps(stored), encoding="utf-8")
            results_dir = campaigns_dir / "results"
            results_dir.mkdir(parents=True, exist_ok=True)
            (results_dir / "campaign-v1.0.0_codex.json").write_text(
                json.dumps(_mock_result("codex", "pass")), encoding="utf-8"
            )
            fake_lane = _fake_lane("codex")
            with mock.patch.object(
                release_campaigns, "resolve_provider_lane",
                return_value=("codex", fake_lane),
            ):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                    io.StringIO()
                ):
                    release_campaigns.campaign_command(
                        release_tag="v1.0.0",
                        campaigns_dir=campaigns_dir,
                        resume=True,
                        apply=True,
                        which_fn=lambda _cmd: "/bin/fake-provider-cli",
                    )
            saved = release_campaigns.load_campaign_by_id(
                "campaign-v1.0.0", campaigns_dir
            )
            assert saved is not None
            entry = saved["providers"][0]
            self.assertEqual(entry["state"], "complete")
            self.assertEqual(entry["adoption_result"]["outcome"], "pass")
            self.assertIsNotNone(entry.get("completed_at"))
            self.assertEqual(entry.get("attempt_history", []), [])

    def test_first_hosted_poll_completion_records_no_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    providers=["devin"],
                    campaigns_dir=campaigns_dir,
                    repo_slug="owner/repo",
                )
            path = campaigns_dir / "campaign-v1.0.0.json"
            stored = json.loads(path.read_text(encoding="utf-8"))
            stored["providers"][0]["state"] = "running"
            stored["providers"][0]["attempted_at"] = OLD_TS
            stored["providers"][0]["dispatched_at"] = OLD_TS
            stored["providers"][0]["completed_at"] = None
            stored["providers"][0]["adoption_result"] = None
            stored["providers"][0]["dispatch_ref"] = {"issue_number": "99"}
            stored["providers"][0]["trigger_posted"] = True
            stored["providers"][0]["response_deadline_at"] = "2099-01-01T00:00:00Z"
            stored["providers"][0].pop("attempt_history", None)
            path.write_text(json.dumps(stored), encoding="utf-8")
            campaign = release_campaigns.load_campaign_by_id(
                "campaign-v1.0.0", campaigns_dir
            )
            assert campaign is not None
            idempotency_key = campaign["providers"][0]["idempotency_key"]
            adoption_res = _mock_result("devin", "pass")
            wrapper = {
                "schema": release_campaigns.RESULT_MARKER_SCHEMA,
                "campaign_id": "campaign-v1.0.0",
                "provider": "devin",
                "release_tag": "v1.0.0",
                "idempotency_key": idempotency_key,
                "adoption_result": adoption_res,
            }
            marker = f"<!-- CODE_MOWER_ADOPTION_RESULT: {json.dumps(wrapper)} -->"

            def mock_gh_json(args: Any, **kwargs: Any) -> Any:
                return {
                    "comments": [
                        {
                            "author": {"login": "devin-ai-integration[bot]"},
                            "body": marker,
                        }
                    ]
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
            entry = saved["providers"][0]
            self.assertEqual(entry["state"], "complete")
            self.assertEqual(entry["adoption_result"]["outcome"], "pass")
            self.assertIsNotNone(entry.get("completed_at"))
            self.assertEqual(entry.get("attempt_history", []), [])

    def test_second_completion_supersedes_first_with_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    providers=["codex"],
                    campaigns_dir=campaigns_dir,
                )
                campaign = release_campaigns.load_campaign_by_id(
                    "campaign-v1.0.0", campaigns_dir
                )
                assert campaign is not None
                release_campaigns.record_manual_result(
                    campaign, "codex", _mock_result("codex", "pass"),
                    campaigns_dir=campaigns_dir,
                )
            path = campaigns_dir / "campaign-v1.0.0.json"
            stored = json.loads(path.read_text(encoding="utf-8"))
            stored["providers"][0]["completed_at"] = OLD_DONE_TS
            stored["providers"][0].pop("attempt_history", None)
            path.write_text(json.dumps(stored), encoding="utf-8")
            campaign = release_campaigns.load_campaign_by_id(
                "campaign-v1.0.0", campaigns_dir
            )
            assert campaign is not None
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                release_campaigns.record_manual_result(
                    campaign, "codex", _mock_result("codex", "fail"),
                    campaigns_dir=campaigns_dir,
                )
            saved = release_campaigns.load_campaign_by_id(
                "campaign-v1.0.0", campaigns_dir
            )
            assert saved is not None
            entry = saved["providers"][0]
            self.assertEqual(entry["adoption_result"]["outcome"], "fail")
            history = entry.get("attempt_history")
            assert isinstance(history, list) and len(history) == 1
            self.assertEqual(history[0]["outcome"], "pass")


class ManualResultIdempotencyTests(unittest.TestCase):
    """Byte-identical re-records add no history and move no timestamps."""

    def test_identical_manual_result_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaigns_dir = Path(tmp) / "campaigns"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                release_campaigns.campaign_command(
                    release_tag="v1.0.0",
                    package_spec="code-mower==1.0.0",
                    providers=["codex"],
                    campaigns_dir=campaigns_dir,
                )
                campaign = release_campaigns.load_campaign_by_id(
                    "campaign-v1.0.0", campaigns_dir
                )
                assert campaign is not None
                release_campaigns.record_manual_result(
                    campaign, "codex", _mock_result("codex", "pass"),
                    campaigns_dir=campaigns_dir,
                )
            path = campaigns_dir / "campaign-v1.0.0.json"
            stored = json.loads(path.read_text(encoding="utf-8"))
            stored["providers"][0]["completed_at"] = OLD_DONE_TS
            stored["providers"][0].pop("attempt_history", None)
            path.write_text(json.dumps(stored), encoding="utf-8")
            campaign = release_campaigns.load_campaign_by_id(
                "campaign-v1.0.0", campaigns_dir
            )
            assert campaign is not None
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                release_campaigns.record_manual_result(
                    campaign, "codex", _mock_result("codex", "pass"),
                    campaigns_dir=campaigns_dir,
                )
            saved = release_campaigns.load_campaign_by_id(
                "campaign-v1.0.0", campaigns_dir
            )
            assert saved is not None
            entry = saved["providers"][0]
            self.assertEqual(entry.get("attempt_history", []), [])
            self.assertEqual(entry.get("completed_at"), OLD_DONE_TS)


class UploadTextTests(unittest.TestCase):
    """Upload summaries describe terminal (complete+blocked) evidence."""

    def test_upload_text_uses_terminal_wording(self) -> None:
        text = release_campaigns.render_campaign_upload_text(
            {
                "release_tag": "v1.0.0",
                "campaign_id": "campaign-v1.0.0",
                "status": "dry_run",
                "endpoint": "https://example.invalid",
                "counts": {
                    "events": 2,
                    "accepted": 2,
                    "skipped": 1,
                    "rejected": 1,
                },
                "next_action": "inspect",
                "next_detail": "",
            }
        )
        self.assertIn("terminal provider(s)", text)
        self.assertIn("without terminal evidence", text)
        self.assertNotIn("completed evidence", text)
        self.assertNotIn("completed provider", text)

    def test_no_events_detail_uses_terminal_wording(self) -> None:
        _, detail = release_campaigns._campaign_upload_next_action(
            status="no_events",
            event_count=0,
            rejected=[],
            skipped=[{"provider": "codex"}],
            token_status="ok",
        )
        self.assertIn("terminal", detail)
        self.assertNotIn("completed", detail)


if __name__ == "__main__":
    unittest.main()
