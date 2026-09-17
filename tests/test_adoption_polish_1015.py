"""Adoption polish #1015: drift operands, Board startup evidence, prompt split.

Three presentation gaps from the v1.4.1 adoption feedback:

- `setup-drift` counted paths without naming what was compared or which side
  owned each file;
- a doctor snapshot taken while a Board was still binding its port warned that
  no Board was running just before `board list` succeeded;
- the optional Devin setup prompt crowded the universal orchestrator prompt.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from code_mower import lane_status, migration
from code_mower.doctor_checks import STATUS_PASS, STATUS_WARN
from code_mower.doctor_checks.adoption import check_adoption_campaign_readiness
from code_mower.doctor_checks.supervised_pilot import (
    check_supervised_pilot_board_visibility,
)

ROOT = Path(__file__).resolve().parents[1]


class FakeClock:
    """A deterministic monotonic clock that only advances when someone sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def grace(self, budget: float = 1.0, poll: float = 0.25) -> lane_status.StartupGrace:
        return lane_status.StartupGrace(
            budget_seconds=budget,
            poll_interval_seconds=poll,
            sleep=self.sleep,
            monotonic=self.monotonic,
        )


def _boards(*ports: int) -> dict[str, Any]:
    return {
        "available": True,
        "boards": [
            {"pid": 1000 + port, "port": port, "process": "python3", "confidence": "high"}
            for port in ports
        ],
        "message": "",
    }


_NO_BOARDS: dict[str, Any] = {"available": True, "boards": [], "message": "no local TCP listeners"}
_UNAVAILABLE: dict[str, Any] = {
    "available": False,
    "boards": [],
    "message": "local listener inventory unavailable",
}


def _git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


class SetupDriftComparisonMetadataTests(unittest.TestCase):
    """The report states its operands and defines each classification on them."""

    def _report(self, *, explicit_config: bool) -> dict[str, Any]:
        starter = ROOT / "src" / "code_mower" / "templates" / "code-mower.example.yml"
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp) / "target"
            repo_path.mkdir()
            if explicit_config:
                (repo_path / "code-mower.yml").write_text(
                    starter.read_text(encoding="utf-8"), encoding="utf-8"
                )
            _git_repo(repo_path)
            return migration.render_setup_drift_report(repo_path=repo_path)

    def test_json_names_both_operands(self) -> None:
        payload = self._report(explicit_config=True)
        comparison = payload["comparison"]

        self.assertEqual(comparison["source"]["id"], migration.SETUP_DRIFT_SOURCE_ID)
        self.assertEqual(comparison["source"]["label"], migration.SETUP_DRIFT_SOURCE_LABEL)
        self.assertEqual(comparison["source"]["config_source"], "explicit_repository_config")
        self.assertEqual(comparison["source"]["profile"], payload["profile"])
        self.assertEqual(comparison["target"]["id"], migration.SETUP_DRIFT_TARGET_ID)
        self.assertEqual(comparison["target"]["label"], migration.SETUP_DRIFT_TARGET_LABEL)
        self.assertEqual(comparison["target"]["repo_path"], payload["repo_path"])
        self.assertEqual(comparison["target"]["tracked_source"], payload["tracked_source"])
        self.assertEqual(comparison["basis"], "bytes")

    def test_every_classification_is_defined_with_an_owning_side(self) -> None:
        comparison = self._report(explicit_config=True)["comparison"]

        for name in migration.SETUP_DRIFT_CLASSIFICATIONS:
            with self.subTest(classification=name):
                entry = comparison["classifications"][name]
                self.assertTrue(entry["definition"].strip())
                self.assertIn(
                    entry["side"],
                    {"both", "source_only", "target_only", "source_unreadable"},
                )

    def test_no_definition_claims_a_newer_side(self) -> None:
        comparison = self._report(explicit_config=True)["comparison"]
        rendered = " ".join(
            str(entry["definition"]) for entry in comparison["classifications"].values()
        )

        self.assertNotIn("newer", rendered.replace("neither side is proven newer", ""))
        self.assertNotIn("newer", comparison["note"].split("does not say")[0])
        self.assertIn("bytes only", comparison["note"])

    def test_source_version_identifies_the_installed_package(self) -> None:
        from code_mower import __version__ as running_version

        source = self._report(explicit_config=True)["comparison"]["source"]

        self.assertEqual(source["code_mower_version"], running_version)
        # Empty is legitimate in a source checkout; a value must be a version,
        # never an install path.
        self.assertNotIn("/", source["installed_distribution_version"])

    def test_packaged_starter_source_publishes_the_name_not_the_install_path(self) -> None:
        source = self._report(explicit_config=False)["comparison"]["source"]

        self.assertEqual(source["config_source"], "packaged_starter")
        self.assertEqual(source["config"], "code-mower.example.yml")
        self.assertNotIn("/", source["config"])

    def test_file_entries_carry_the_owning_side(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            (repo_path / ".github" / "workflows").mkdir(parents=True)
            (repo_path / "same.txt").write_text("same\n", encoding="utf-8")
            (repo_path / "differs.txt").write_text("old\n", encoding="utf-8")
            (repo_path / ".github" / "workflows" / "old-code-mower.yml").write_text(
                "legacy\n", encoding="utf-8"
            )

            files = migration._classify_setup_drift(
                repo_path=repo_path,
                generated_files={
                    ".github/workflows/new-code-mower.yml": "new\n",
                    "differs.txt": "new\n",
                    "missing.yml": None,
                    "same.txt": "same\n",
                },
                tracked_files={
                    ".github/workflows/old-code-mower.yml",
                    "differs.txt",
                    "same.txt",
                },
            )

        sides = {item["path"]: item["side"] for item in files}
        self.assertEqual(sides["same.txt"], "both")
        self.assertEqual(sides["differs.txt"], "both")
        self.assertEqual(sides[".github/workflows/new-code-mower.yml"], "source_only")
        self.assertEqual(sides[".github/workflows/old-code-mower.yml"], "target_only")
        self.assertEqual(sides["missing.yml"], "source_unreadable")


class SetupDriftTextTests(unittest.TestCase):
    def _payload(self, **overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": "warn",
            "repo_path": "/tmp/repo",
            "profile": "recommended",
            "counts": {
                "same": 1,
                "differs": 1,
                "new": 0,
                "repo-only": 0,
                "missing-from-output": 0,
            },
            "next_action": "review drift",
            "comparison": migration._setup_drift_comparison(
                repo_path=Path("/tmp/repo"),
                config="code-mower.yml",
                config_source="explicit_repository_config",
                profile="recommended",
                tracked_available=True,
            ),
            "files": [
                {
                    "path": "code-mower.yml",
                    "classification": "differs",
                    "side": "both",
                    "repo_bytes": 24,
                    "generated_bytes": 28,
                }
            ],
        }
        payload.update(overrides)
        return payload

    def test_text_states_the_operands_and_the_legend(self) -> None:
        text = migration.render_setup_drift_text(self._payload())

        self.assertIn("Compared: ", text)
        self.assertIn(migration.SETUP_DRIFT_SOURCE_LABEL, text)
        self.assertIn(migration.SETUP_DRIFT_TARGET_LABEL, text)
        self.assertIn("(source)", text)
        self.assertIn("(target)", text)
        self.assertIn("Classification legend:", text)
        for name in migration.SETUP_DRIFT_CLASSIFICATIONS:
            with self.subTest(classification=name):
                self.assertIn(f"- {name}: ", text)
        self.assertIn("Note: ", text)
        self.assertIn("does not say", text)

    def test_changed_paths_name_the_side_without_leaking_contents(self) -> None:
        text = migration.render_setup_drift_text(self._payload())

        self.assertIn("DIFFERS code-mower.yml", text)
        self.assertIn("side=both", text)
        self.assertIn("repo=24b", text)
        self.assertIn("generated=28b", text)

    def test_text_keeps_the_counts_and_next_action_contract(self) -> None:
        text = migration.render_setup_drift_text(self._payload())

        self.assertIn("Counts: same=1, differs=1, new=0, repo-only=0, missing-from-output=0", text)
        self.assertIn("Next: review drift", text)

    def test_legacy_payload_without_comparison_still_renders(self) -> None:
        # JSON compatibility: a payload written by an older Code Mower has no
        # `comparison` block, and the renderer must not require one.
        payload = self._payload()
        payload.pop("comparison")

        text = migration.render_setup_drift_text(payload)

        self.assertNotIn("Compared: ", text)
        self.assertIn("Counts: ", text)
        self.assertIn("DIFFERS code-mower.yml", text)


class SetupDriftCompatibilityTests(unittest.TestCase):
    def test_report_keeps_every_published_key_and_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp) / "target"
            repo_path.mkdir()
            _git_repo(repo_path)
            payload = migration.render_setup_drift_report(repo_path=repo_path)

        self.assertEqual(payload["schema"], "code_mower.setupDrift.v1")
        for key in (
            "mode",
            "status",
            "repo_path",
            "config",
            "config_source",
            "profile",
            "builders",
            "additional_repositories",
            "tracked_source",
            "counts",
            "file_count",
            "changed_count",
            "repo_path_hint",
            "standalone_pin",
            "superseded_bridge",
            "builder_hint",
            "files",
            "next_action",
        ):
            with self.subTest(key=key):
                self.assertIn(key, payload)
        self.assertEqual(set(payload["counts"]), set(migration.SETUP_DRIFT_CLASSIFICATIONS))


class BoardStartupGraceTests(unittest.TestCase):
    """The grace re-observes one case and never invents or hides a Board."""

    def test_visible_board_is_reported_with_no_wait(self) -> None:
        clock = FakeClock()
        with mock.patch.object(
            lane_status, "collect_local_boards", return_value=_boards(5332)
        ):
            observation = lane_status.observe_local_boards(grace=clock.grace())

        self.assertTrue(observation.visible)
        self.assertEqual(clock.slept, [])
        self.assertFalse(observation.grace["applied"])
        self.assertEqual(
            observation.grace["reason"], lane_status.BOARD_GRACE_VISIBLE_IMMEDIATELY
        )
        self.assertEqual(observation.grace["waited_seconds"], 0.0)

    def test_unavailable_inventory_is_not_a_startup_race(self) -> None:
        clock = FakeClock()
        with mock.patch.object(
            lane_status, "collect_local_boards", return_value=_UNAVAILABLE
        ):
            observation = lane_status.observe_local_boards(grace=clock.grace())

        self.assertEqual(clock.slept, [])
        self.assertFalse(observation.grace["applied"])
        self.assertEqual(
            observation.grace["reason"], lane_status.BOARD_GRACE_INVENTORY_UNAVAILABLE
        )

    def test_board_that_becomes_visible_during_the_grace_is_reported(self) -> None:
        clock = FakeClock()
        with mock.patch.object(
            lane_status,
            "collect_local_boards",
            side_effect=[_NO_BOARDS, _NO_BOARDS, _boards(5332)],
        ):
            observation = lane_status.observe_local_boards(grace=clock.grace())

        self.assertTrue(observation.visible)
        self.assertEqual(clock.slept, [0.25, 0.25])
        self.assertEqual(observation.grace["attempts"], 3)
        self.assertEqual(observation.grace["waited_seconds"], 0.5)
        self.assertEqual(
            observation.grace["reason"], lane_status.BOARD_GRACE_VISIBLE_AFTER_GRACE
        )

    def test_a_board_that_never_appears_stays_not_visible_within_the_budget(self) -> None:
        clock = FakeClock()
        with mock.patch.object(
            lane_status, "collect_local_boards", return_value=_NO_BOARDS
        ):
            observation = lane_status.observe_local_boards(grace=clock.grace())

        self.assertFalse(observation.visible)
        self.assertEqual(clock.slept, [0.25, 0.25, 0.25, 0.25])
        self.assertEqual(observation.grace["attempts"], 5)
        self.assertLessEqual(observation.grace["waited_seconds"], 1.0)
        self.assertEqual(
            observation.grace["reason"], lane_status.BOARD_GRACE_NOT_VISIBLE_AFTER_GRACE
        )

    def test_a_zero_budget_disables_the_grace(self) -> None:
        clock = FakeClock()
        with mock.patch.object(
            lane_status, "collect_local_boards", return_value=_NO_BOARDS
        ) as collector:
            observation = lane_status.observe_local_boards(grace=clock.grace(budget=0.0))

        self.assertEqual(collector.call_count, 1)
        self.assertEqual(clock.slept, [])
        self.assertEqual(observation.grace["reason"], lane_status.BOARD_GRACE_DISABLED)

    def test_waiting_is_opt_in_so_library_callers_observe_once(self) -> None:
        # No `grace` means exactly one observation, like `collect_local_boards`.
        # Only the doctor snapshot opts into waiting.
        with mock.patch.object(
            lane_status, "collect_local_boards", return_value=_NO_BOARDS
        ) as collector:
            observation = lane_status.observe_local_boards()

        self.assertEqual(collector.call_count, 1)
        self.assertFalse(observation.grace["applied"])
        self.assertEqual(observation.grace["reason"], lane_status.BOARD_GRACE_DISABLED)

    def test_the_doctor_snapshot_opts_into_the_bounded_grace(self) -> None:
        from code_mower import doctor

        grace = doctor.board_startup_grace()

        self.assertIsInstance(grace, lane_status.StartupGrace)
        # Budget unset means the environment or the short default decides it.
        self.assertIsNone(grace.budget_seconds)
        self.assertEqual(
            lane_status.resolve_board_grace_seconds(grace.budget_seconds, env={}),
            lane_status.BOARD_STARTUP_GRACE_SECONDS,
        )

    def test_the_reported_boards_are_always_the_final_observation(self) -> None:
        # The grace re-runs the same read-only observation, so it can only
        # report what the last poll returned. A Board that answers late is
        # reported exactly as observed, with no synthesized entry.
        clock = FakeClock()
        with mock.patch.object(
            lane_status,
            "collect_local_boards",
            side_effect=[_NO_BOARDS, _boards(5332, 5333)],
        ):
            observation = lane_status.observe_local_boards(grace=clock.grace())

        self.assertEqual([board["port"] for board in observation.boards], [5332, 5333])


class BoardStartupGraceBudgetTests(unittest.TestCase):
    def test_explicit_budget_wins_over_the_environment(self) -> None:
        self.assertEqual(
            lane_status.resolve_board_grace_seconds(0.5, env={lane_status.BOARD_STARTUP_GRACE_ENV: "9"}),
            0.5,
        )

    def test_environment_override_is_read_and_clamped(self) -> None:
        self.assertEqual(
            lane_status.resolve_board_grace_seconds(None, env={lane_status.BOARD_STARTUP_GRACE_ENV: "1.5"}),
            1.5,
        )
        self.assertEqual(
            lane_status.resolve_board_grace_seconds(None, env={lane_status.BOARD_STARTUP_GRACE_ENV: "0"}),
            0.0,
        )
        self.assertEqual(
            lane_status.resolve_board_grace_seconds(
                None, env={lane_status.BOARD_STARTUP_GRACE_ENV: "600"}
            ),
            lane_status.BOARD_STARTUP_MAX_GRACE_SECONDS,
        )

    def test_unusable_environment_values_fall_back_to_the_default(self) -> None:
        for raw in ("", "   ", "soon", "-1"):
            with self.subTest(raw=raw):
                self.assertEqual(
                    lane_status.resolve_board_grace_seconds(
                        None, env={lane_status.BOARD_STARTUP_GRACE_ENV: raw}
                    ),
                    lane_status.BOARD_STARTUP_GRACE_SECONDS,
                )

    def test_the_default_budget_stays_short(self) -> None:
        self.assertLessEqual(lane_status.BOARD_STARTUP_GRACE_SECONDS, 5.0)
        self.assertLessEqual(
            lane_status.BOARD_STARTUP_GRACE_SECONDS, lane_status.BOARD_STARTUP_MAX_GRACE_SECONDS
        )


class SupervisedPilotBoardVisibilityTests(unittest.TestCase):
    def test_starting_board_resolves_to_pass_with_grace_evidence(self) -> None:
        clock = FakeClock()
        with mock.patch.object(
            lane_status,
            "collect_local_boards",
            side_effect=[_NO_BOARDS, _boards(5332)],
        ):
            check = check_supervised_pilot_board_visibility(board_startup_grace=clock.grace())

        self.assertEqual(check.status, STATUS_PASS)
        self.assertTrue(check.detail["board_visible"])
        self.assertEqual(
            check.detail["startup_grace"]["reason"],
            lane_status.BOARD_GRACE_VISIBLE_AFTER_GRACE,
        )
        self.assertTrue(check.detail["local_paths_redacted"])

    def test_no_board_still_warns_and_records_the_bounded_wait(self) -> None:
        clock = FakeClock()
        with mock.patch.object(
            lane_status, "collect_local_boards", return_value=_NO_BOARDS
        ):
            check = check_supervised_pilot_board_visibility(board_startup_grace=clock.grace())

        self.assertEqual(check.status, STATUS_WARN)
        self.assertFalse(check.detail["board_visible"])
        self.assertEqual(check.detail["board_count"], 0)
        self.assertTrue(check.detail["startup_grace"]["applied"])
        self.assertLessEqual(check.detail["startup_grace"]["waited_seconds"], 1.0)
        self.assertIsNotNone(check.remediation)

    def test_a_visible_board_is_never_delayed_or_masked(self) -> None:
        # A stopped, wrong-repository, stale-version, or unhealthy Board is
        # still a visible listener. The grace must not run for it, so nothing
        # about it is deferred or softened.
        clock = FakeClock()
        stale = {
            "available": True,
            "boards": [
                {
                    "pid": 4242,
                    "port": 5332,
                    "process": "python3",
                    "confidence": "medium",
                    "repo": "other/repo",
                    "cwd": "/secret/path",
                }
            ],
            "message": "",
        }
        with mock.patch.object(
            lane_status, "collect_local_boards", return_value=stale
        ):
            check = check_supervised_pilot_board_visibility(board_startup_grace=clock.grace())

        self.assertEqual(clock.slept, [])
        self.assertEqual(check.detail["board_count"], 1)
        self.assertEqual(check.detail["boards"][0]["port"], 5332)
        self.assertEqual(check.detail["boards"][0]["confidence"], "medium")
        self.assertNotIn("/secret/path", str(check.detail))
        self.assertFalse(check.detail["startup_grace"]["applied"])

    def test_privacy_grace_detail_carries_only_timing(self) -> None:
        clock = FakeClock()
        with mock.patch.object(
            lane_status, "collect_local_boards", return_value=_NO_BOARDS
        ):
            check = check_supervised_pilot_board_visibility(board_startup_grace=clock.grace())

        self.assertEqual(
            set(check.detail["startup_grace"]),
            {
                "applied",
                "reason",
                "attempts",
                "budget_seconds",
                "waited_seconds",
                "poll_interval_seconds",
            },
        )


class AdoptionStartupSequenceRegressionTests(unittest.TestCase):
    """The reported sequence: `board serve`, then doctor, then `board list`.

    The first listener inventory is taken while the Board is still binding its
    port. Before #1015 that snapshot reported "Code Mower Board is not running
    locally" moments before `board list` listed it.
    """

    def _runner(self, visible_from_attempt: int) -> Any:
        state = {"inventories": 0}

        def fake_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            if cmd[0] == "lsof" and "-iTCP" in cmd:
                state["inventories"] += 1
                if state["inventories"] >= visible_from_attempt:
                    return subprocess.CompletedProcess(
                        args=cmd, returncode=0, stdout="p1234\nn*:8000\n", stderr=""
                    )
                # Answered, and nothing is listening yet.
                return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="")
            if cmd[0] == "lsof":
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout="p1234\nn/private/checkout\n", stderr=""
                )
            if cmd[0] == "ps":
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout="python -m code_mower.board", stderr=""
                )
            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="")

        return fake_runner

    def _board_check(self, runner: Any, grace: lane_status.StartupGrace) -> Any:
        with tempfile.TemporaryDirectory() as tmp:
            checks = check_adoption_campaign_readiness(
                config={},
                repo_root=Path(tmp),
                command_runner=runner,
                providers=[],
                board_startup_grace=grace,
            )
        return next(c for c in checks if c.name == "doctor.campaign.board_visibility")

    def test_a_board_that_binds_late_is_reported_as_visible(self) -> None:
        clock = FakeClock()
        check = self._board_check(self._runner(visible_from_attempt=2), clock.grace())

        self.assertEqual(check.status, STATUS_PASS)
        self.assertEqual(check.detail["board_count"], 1)
        self.assertEqual(
            check.detail["startup_grace"]["reason"],
            lane_status.BOARD_GRACE_VISIBLE_AFTER_GRACE,
        )
        self.assertNotIn("/private/checkout", str(check.detail))

    def test_a_board_that_never_binds_still_warns(self) -> None:
        clock = FakeClock()
        check = self._board_check(self._runner(visible_from_attempt=99), clock.grace())

        self.assertEqual(check.status, STATUS_WARN)
        self.assertEqual(check.detail["board_count"], 0)
        self.assertTrue(check.detail["optional"])
        self.assertEqual(
            check.detail["startup_grace"]["reason"],
            lane_status.BOARD_GRACE_NOT_VISIBLE_AFTER_GRACE,
        )
        self.assertLessEqual(check.detail["startup_grace"]["waited_seconds"], 1.0)


class DoctorSnapshotWiringTests(unittest.TestCase):
    def test_run_doctor_forwards_the_board_startup_grace(self) -> None:
        from code_mower.doctor_checks import runner as doctor_runner

        grace = FakeClock().grace()
        captured: dict[str, Any] = {}

        def fake_campaign(**kwargs: Any) -> tuple[Any, ...]:
            captured["campaign"] = kwargs.get("board_startup_grace")
            return ()

        def fake_pilot(checks: Any, **kwargs: Any) -> tuple[Any, ...]:
            captured["pilot"] = kwargs.get("board_startup_grace")
            return ()

        with mock.patch.object(
            doctor_runner, "check_adoption_campaign_readiness", fake_campaign
        ), mock.patch.object(doctor_runner, "check_supervised_pilot", fake_pilot):
            doctor_runner.run_doctor(
                config_path=ROOT / "src/code_mower/templates/code-mower.example.yml",
                provider_templates_path=ROOT / "src/code_mower/templates/providers.yml",
                profile="recommended",
                adoption=True,
                supervised_pilot=True,
                repo_slug="owner/repo",
                board_startup_grace=grace,
            )

        self.assertIs(captured["campaign"], grace)
        self.assertIs(captured["pilot"], grace)


class PromptPackSplitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pack = (ROOT / "docs" / "orchestrator-prompt-pack.md").read_text(encoding="utf-8")
        self.companion = (ROOT / "docs" / "devin-setup-prompt.md").read_text(encoding="utf-8")

    def test_the_pack_keeps_a_pointer_not_the_detailed_prompt(self) -> None:
        self.assertIn("devin-setup-prompt.md", self.pack)
        self.assertNotIn("--set-transport devin=devin_api_v3", self.pack)
        self.assertNotIn("devin-audit-bridge.yml", self.pack)

    def test_the_pack_keeps_the_authority_and_lease_guardrails(self) -> None:
        self.assertIn("docs/participant-qualification.md", self.pack)
        self.assertIn("grants no review or merge authority", self.pack)
        self.assertIn("do not start paid sessions", self.pack)

    def test_the_companion_carries_the_full_opt_in_prompt(self) -> None:
        self.assertIn("--set-transport devin=devin_api_v3 --dry-run", self.companion)
        self.assertIn(".code-mower.generated", self.companion)
        self.assertIn("devin-audit-bridge.yml", self.companion)
        self.assertIn("devin-audit-labeler.yml", self.companion)
        self.assertIn("orchestrator-prompt-pack.md", self.companion)

    def test_the_universal_prompt_is_smaller_than_the_detailed_devin_prompt(self) -> None:
        # The point of the move: provider-specific detail no longer occupies the
        # universal prompt pack.
        self.assertLess(self.pack.count("--devin"), 3)


class VerificationGuidanceTests(unittest.TestCase):
    def test_install_and_upgrade_lead_with_the_concise_doctor(self) -> None:
        install = (ROOT / "docs" / "install.md").read_text(encoding="utf-8")
        upgrade = (ROOT / "docs" / "upgrade-existing-repo.md").read_text(encoding="utf-8")

        for text in (install, upgrade):
            self.assertIn("code-mower doctor --adoption --repo OWNER/REPO --concise", text)
            self.assertIn("code-mower doctor --adoption --repo OWNER/REPO --json", text)
            self.assertIn("--advanced", text)
            self.assertIn("every check still runs", text)

    def test_docs_define_drift_classifications_on_the_named_operands(self) -> None:
        install = (ROOT / "docs" / "install.md").read_text(encoding="utf-8")
        upgrade = (ROOT / "docs" / "upgrade-existing-repo.md").read_text(encoding="utf-8")

        for text in (install, upgrade):
            self.assertIn("generated setup from the installed Code Mower package", text)
            self.assertIn("tracked repository files", text)
            for name in migration.SETUP_DRIFT_CLASSIFICATIONS:
                with self.subTest(classification=name):
                    self.assertIn(f"`{name}`", text)
        self.assertIn("does not prove which side is newer", upgrade)
        self.assertIn("does not say which", install)

    def test_install_documents_the_bounded_board_startup_grace(self) -> None:
        install = (ROOT / "docs" / "install.md").read_text(encoding="utf-8")

        self.assertIn(lane_status.BOARD_STARTUP_GRACE_ENV, install)
        self.assertIn("binding its port", install)
        self.assertIn("never hidden", install)


if __name__ == "__main__":
    unittest.main()
