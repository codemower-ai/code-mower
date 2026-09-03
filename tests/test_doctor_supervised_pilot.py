import unittest
from unittest import mock

from code_mower.doctor_checks.github_human_token import (
    check_gate_automerge_token,
    check_human_automation_token,
)
from code_mower.doctor_checks.models import DoctorCheck
from code_mower.doctor_checks.supervised_pilot import (
    check_supervised_pilot,
    check_supervised_pilot_board_visibility,
    check_supervised_pilot_readiness,
    check_supervised_pilot_runner_posture,
)


class DoctorSupervisedPilotTests(unittest.TestCase):
    def _base_checks(self) -> tuple[DoctorCheck, ...]:
        return (
            DoctorCheck(
                name="config.validate",
                status="pass",
                message="config validates",
            ),
            DoctorCheck(
                name="doctor.adoption.owner_login",
                status="pass",
                message="owner decision login configured: owner",
            ),
            DoctorCheck(
                name="doctor.adoption.trusted_authors",
                status="pass",
                message="trusted audit-comment author variables are configured",
            ),
            DoctorCheck(
                name="github.branch_protection",
                status="pass",
                message="owner/repo@main requires code-mower/gate",
                detail={"required_status_context": "code-mower/gate"},
            ),
            DoctorCheck(
                name="github.repo.auto_merge",
                status="pass",
                message="owner/repo has auto-merge enabled",
                detail={"allow_auto_merge": True},
            ),
            DoctorCheck(
                name="github.human_automation_token",
                status="pass",
                message="owner/repo DISPATCH_TOKEN is recorded as non-expiring",
                detail={"non_expiring": True},
            ),
            DoctorCheck(
                name="github.gate_automerge_token",
                status="pass",
                message="owner/repo has a dedicated CODE_MOWER_GATE_AUTOMERGE_TOKEN secret",
            ),
            DoctorCheck(
                name="cloud.token",
                status="pass",
                message="Code Mower Cloud token file is configured",
            ),
        )

    def test_board_visibility_uses_safe_listener_metadata(self) -> None:
        with mock.patch(
            "code_mower.doctor_checks.supervised_pilot.lane_status.collect_local_boards",
            return_value={
                "available": True,
                "boards": [
                    {
                        "pid": 123,
                        "port": 5332,
                        "process": "python3",
                        "cwd": "/secret/path",
                        "confidence": "high",
                    }
                ],
            },
        ):
            check = check_supervised_pilot_board_visibility()

        self.assertEqual(check.status, "pass")
        self.assertTrue(check.detail["board_visible"])
        self.assertEqual(check.detail["boards"], [{"port": 5332, "process": "python3", "confidence": "high"}])
        self.assertNotIn("/secret/path", str(check.detail))

    def test_hosted_builder_posture_does_not_require_local_cli(self) -> None:
        check = check_supervised_pilot_runner_posture(
            (
                DoctorCheck(
                    name="runtime.local_cli",
                    status="warn",
                    lane="codex",
                    message="codex is not installed",
                ),
            ),
            adoption_posture="hosted-builders",
        )

        self.assertEqual(check.status, "pass")
        self.assertFalse(check.detail["local_runner_required_here"])
        self.assertEqual(check.detail["local_cli_blockers"], ["runtime.local_cli:codex"])

    def test_orchestrator_only_posture_does_not_require_local_cli(self) -> None:
        check = check_supervised_pilot_runner_posture(
            (
                DoctorCheck(
                    name="runtime.local_cli.probe",
                    status="warn",
                    lane="claude_audit",
                    message="claude auth probe failed",
                ),
            ),
            adoption_posture="orchestrator-only",
        )

        self.assertEqual(check.status, "pass")
        self.assertFalse(check.detail["local_runner_required_here"])

    def test_local_mac_runner_posture_passes_when_runner_checks_pass(self) -> None:
        check = check_supervised_pilot_runner_posture(
            (
                DoctorCheck(
                    name="runtime.runner_launchagent",
                    status="pass",
                    message="runner listener is visible",
                ),
                DoctorCheck(
                    name="runtime.runner_workflow_labels",
                    status="pass",
                    message="runner workflow labels are ready",
                ),
                DoctorCheck(
                    name="runtime.local_cli",
                    status="pass",
                    lane="codex",
                    message="codex found",
                ),
            ),
            adoption_posture="reviewer-gate",
        )

        self.assertEqual(check.status, "pass")
        self.assertTrue(check.detail["local_runner_required_here"])
        self.assertTrue(check.detail["local_runner_ready"])

    def test_manual_pilot_warns_but_keeps_promotion_todos_separate(self) -> None:
        checks = (
            *self._base_checks(),
            DoctorCheck(
                name="github.repo.auto_merge",
                status="warn",
                message="owner/repo does not allow auto-merge",
                detail={
                    "promotion_todo": True,
                    "promotion_todo_kind": "repo_auto_merge",
                },
            ),
        )

        readiness = check_supervised_pilot_readiness(
            checks,
            repo_slug="owner/repo",
            pilot_mode="manual",
            adoption_posture="reviewer-gate",
        )

        self.assertEqual(readiness.status, "warn")
        self.assertTrue(readiness.detail["manual_pilot_ready"])
        self.assertFalse(readiness.detail["promoted_pilot_ready"])
        self.assertEqual(
            readiness.detail["promotion_todos"][0]["kind"],
            "repo_auto_merge",
        )
        self.assertIn(
            "finish_promotion_todos_before_auto_merge",
            readiness.detail["next_steps"],
        )

    def test_promoted_pilot_fails_when_merge_credential_is_missing(self) -> None:
        checks = tuple(
            check
            for check in self._base_checks()
            if check.name != "github.gate_automerge_token"
        ) + (
            DoctorCheck(
                name="github.gate_automerge_token",
                status="warn",
                message="owner/repo has no merge-capable gate credential configured",
                detail={
                    "promotion_todo": True,
                    "promotion_todo_kind": "merge_credential_missing",
                },
            ),
        )

        readiness = check_supervised_pilot_readiness(
            checks,
            repo_slug="owner/repo",
            pilot_mode="promoted",
            adoption_posture="reviewer-gate",
        )

        self.assertEqual(readiness.status, "fail")
        self.assertFalse(readiness.detail["promoted_pilot_ready"])
        self.assertEqual(readiness.detail["promotion_todos"][0]["kind"], "merge_credential_missing")

    def test_promoted_pilot_accepts_required_contexts_shape_from_github(self) -> None:
        checks = tuple(
            DoctorCheck(
                name="github.branch_protection",
                status="pass",
                message="owner/repo@main requires code-mower/gate",
                detail={"required_status_contexts": ["package", "code-mower/gate"]},
            )
            if check.name == "github.branch_protection"
            else check
            for check in self._base_checks()
        )

        readiness = check_supervised_pilot_readiness(
            checks,
            repo_slug="owner/repo",
            pilot_mode="promoted",
            adoption_posture="reviewer-gate",
        )

        self.assertEqual(readiness.status, "pass")
        self.assertTrue(readiness.detail["required_gate_ready"])
        self.assertTrue(readiness.detail["promoted_pilot_ready"])

    def test_promoted_pilot_treats_null_required_contexts_as_not_ready(self) -> None:
        checks = tuple(
            DoctorCheck(
                name="github.branch_protection",
                status="pass",
                message="owner/repo@main has branch protection",
                detail={"required_status_contexts": None},
            )
            if check.name == "github.branch_protection"
            else check
            for check in self._base_checks()
        )

        readiness = check_supervised_pilot_readiness(
            checks,
            repo_slug="owner/repo",
            pilot_mode="promoted",
            adoption_posture="reviewer-gate",
        )

        self.assertEqual(readiness.status, "fail")
        self.assertFalse(readiness.detail["required_gate_ready"])

    def test_configless_repo_is_a_blocker(self) -> None:
        readiness = check_supervised_pilot_readiness(
            (
                DoctorCheck(
                    name="config.load",
                    status="fail",
                    message="cannot load config",
                ),
            ),
            repo_slug="owner/repo",
            pilot_mode="manual",
            adoption_posture="reviewer-gate",
        )

        self.assertEqual(readiness.status, "fail")
        self.assertFalse(readiness.detail["manual_pilot_ready"])
        self.assertEqual(readiness.detail["blockers"][0]["id"], "config.load")

    def test_never_expiring_dispatch_token_counts_as_ready(self) -> None:
        with mock.patch(
            "code_mower.doctor_checks.github_human_token._github_api_json",
            side_effect=[
                ({"name": "DISPATCH_TOKEN", "created_at": "2026-09-01T00:00:00Z"}, {}),
                ({"name": "DISPATCH_TOKEN_EXPIRES_AT", "value": "never"}, {}),
            ],
        ):
            check = check_human_automation_token(
                gh_path="/usr/bin/gh",
                slug="owner/repo",
                config={"owner_surface": {"dispatch_token_env": "DISPATCH_TOKEN"}},
                lanes=[("codex", {"token_env": ["DISPATCH_TOKEN"]})],
                http_timeout=1,
                adoption=True,
            )

        self.assertEqual(check.status, "pass")
        self.assertTrue(check.detail["non_expiring"])
        self.assertNotIn("secret-value-that-must-not-print", str(check.detail))

    def test_gate_automerge_token_accepts_dispatch_fallback_without_secret_values(self) -> None:
        with mock.patch(
            "code_mower.doctor_checks.github_human_token._github_api_json",
            side_effect=[
                (None, {"returncode": 1, "output_redacted": True}),
                ({"name": "DISPATCH_TOKEN", "created_at": "2026-09-01T00:00:00Z"}, {}),
            ],
        ):
            check = check_gate_automerge_token(
                gh_path="/usr/bin/gh",
                slug="owner/repo",
                config={"owner_surface": {"dispatch_token_env": "DISPATCH_TOKEN"}},
                http_timeout=1,
            )

        self.assertEqual(check.status, "pass")
        self.assertEqual(check.detail["credential_source"], "dispatch_token_fallback")
        self.assertFalse(check.detail["capability_verified"])
        self.assertNotIn("ghp_", str(check.detail))

    def test_combined_supervised_pilot_checks_include_mode_and_board(self) -> None:
        with mock.patch(
            "code_mower.doctor_checks.supervised_pilot.lane_status.collect_local_boards",
            return_value={"available": False, "boards": [], "message": "none"},
        ):
            checks = check_supervised_pilot(
                self._base_checks(),
                repo_slug="owner/repo",
                pilot_mode="manual",
                adoption_posture="orchestrator-only",
            )

        self.assertEqual([check.name for check in checks], [
            "supervised_pilot.mode",
            "supervised_pilot.runner_posture",
            "supervised_pilot.board_visibility",
            "supervised_pilot.readiness",
        ])
        self.assertEqual(checks[0].detail["pilot_mode"], "manual")
        self.assertEqual(checks[2].status, "warn")


if __name__ == "__main__":
    unittest.main()
