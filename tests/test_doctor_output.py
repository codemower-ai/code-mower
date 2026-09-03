import unittest

from code_mower.doctor_checks import output
from code_mower.doctor_checks.models import (
    STATUS_PASS,
    STATUS_SKIP,
    STATUS_WARN,
    DoctorCheck,
    DoctorReport,
)


class DoctorOutputTests(unittest.TestCase):
    def test_output_groups_checks_without_changing_check_lines(self) -> None:
        report = DoctorReport(
            config_path="code-mower.yml",
            provider_templates_path="providers.yml",
            profile="recommended",
            checks=(
                DoctorCheck(
                    name="config.validate",
                    status=STATUS_PASS,
                    message="config validates",
                ),
                DoctorCheck(
                    name="doctor.plan",
                    status=STATUS_PASS,
                    message="doctor run plan: load-inputs, select-profile, runtime, providers",
                    detail={
                        "stages": [
                            {"id": "load-inputs", "group": "runtime", "optional": False},
                            {"id": "select-profile", "group": "runtime", "optional": False},
                            {"id": "runtime", "group": "runtime", "optional": False},
                            {"id": "providers", "group": "providers", "optional": False},
                        ]
                    },
                ),
                DoctorCheck(
                    name="runtime.python",
                    status=STATUS_PASS,
                    message="Python 3.12 is available",
                ),
                DoctorCheck(
                    name="env.tokens",
                    status=STATUS_WARN,
                    lane="claude-audit",
                    message="missing token env vars: GITHUB_TOKEN",
                    remediation="set GITHUB_TOKEN before enabling this lane.",
                ),
                DoctorCheck(
                    name="github.repo.metadata",
                    status=STATUS_WARN,
                    message="could not read GitHub repository metadata",
                ),
                DoctorCheck(
                    name="cloud.token",
                    status=STATUS_PASS,
                    message="Code Mower Cloud token file is configured",
                ),
                DoctorCheck(
                    name="output.json",
                    status=STATUS_SKIP,
                    message="JSON output was not requested",
                ),
            ),
        )

        rendered = output.render_doctor_text(report)

        self.assertIn(
            "Run plan: load-inputs (runtime), select-profile (runtime), "
            "runtime (runtime), providers (providers)",
            rendered,
        )
        self.assertIn("Checks: 7 total, 2 warnings, 1 skipped", rendered)
        self.assertLess(rendered.index("Setup"), rendered.index("Runtime"))
        self.assertLess(rendered.index("Runtime"), rendered.index("Provider lanes"))
        self.assertLess(rendered.index("Provider lanes"), rendered.index("GitHub"))
        self.assertLess(rendered.index("GitHub"), rendered.index("Code Mower Cloud"))
        self.assertLess(rendered.index("Code Mower Cloud"), rendered.index("Output"))
        self.assertIn(
            "- WARN env.tokens [claude-audit]: missing token env vars: GITHUB_TOKEN",
            rendered,
        )
        self.assertIn(
            "  remediation: set GITHUB_TOKEN before enabling this lane.",
            rendered,
        )
        self.assertIn(
            "- PASS doctor.plan: doctor run plan: load-inputs, select-profile, runtime, providers",
            rendered,
        )

    def test_empty_report_is_explicit(self) -> None:
        report = DoctorReport(
            config_path="code-mower.yml",
            provider_templates_path="providers.yml",
            profile=None,
            checks=(),
        )

        rendered = output.render_doctor_text(report)

        self.assertIn("Checks: 0 total, all passing", rendered)
        self.assertIn("No checks ran.", rendered)

    def test_owner_actions_are_rendered_separately_from_warnings(self) -> None:
        report = DoctorReport(
            config_path="code-mower.yml",
            provider_templates_path="providers.yml",
            profile="recommended",
            checks=(
                DoctorCheck(
                    name="github.human_automation_token",
                    status=STATUS_WARN,
                    message="owner/repo is missing DISPATCH_TOKEN",
                    detail={
                        "owner_action": True,
                        "owner_action_kind": "human_automation_token",
                    },
                ),
                DoctorCheck(
                    name="runtime.pytest",
                    status=STATUS_WARN,
                    message="pytest is not installed",
                ),
            ),
        )

        rendered = output.render_doctor_text(report)

        self.assertIn("Checks: 2 total, 1 owner actions, 1 warnings", rendered)
        self.assertIn("GitHub (1 owner actions)", rendered)
        self.assertIn(
            "- OWNER-ACTION github.human_automation_token: owner/repo is missing DISPATCH_TOKEN",
            rendered,
        )
        self.assertIn("Runtime (1 warnings)", rendered)
        self.assertEqual(report.as_dict()["summary"]["owner_actions"], 1)
        self.assertEqual(report.as_dict()["summary"]["warnings"], 1)
        self.assertEqual(report.as_dict()["groups"]["github"]["owner_actions"], 1)
        self.assertEqual(report.as_dict()["groups"]["github"]["warnings"], 0)
        self.assertEqual(report.as_dict()["groups"]["runtime"]["warnings"], 1)
        owner_check = report.as_dict()["checks"][0]
        self.assertEqual(owner_check["id"], "github.human_automation_token")
        self.assertTrue(owner_check["owner_action"])
        self.assertEqual(owner_check["owner_action_kind"], "human_automation_token")

    def test_promotion_todos_are_rendered_separately_from_warnings(self) -> None:
        report = DoctorReport(
            config_path="code-mower.yml",
            provider_templates_path="providers.yml",
            profile="recommended",
            checks=(
                DoctorCheck(
                    name="github.repo.auto_merge",
                    status=STATUS_WARN,
                    message="owner/repo does not allow auto-merge",
                    detail={
                        "promotion_todo": True,
                        "promotion_todo_kind": "repo_auto_merge",
                    },
                ),
                DoctorCheck(
                    name="runtime.pytest",
                    status=STATUS_WARN,
                    message="pytest is not installed",
                ),
            ),
        )

        rendered = output.render_doctor_text(report)

        self.assertIn("Checks: 2 total, 1 promotion todos, 1 warnings", rendered)
        self.assertIn("GitHub (1 promotion todos)", rendered)
        self.assertIn(
            "- PROMOTION-TODO github.repo.auto_merge: owner/repo does not allow auto-merge",
            rendered,
        )
        self.assertEqual(report.as_dict()["summary"]["promotion_todos"], 1)
        self.assertEqual(report.as_dict()["summary"]["warnings"], 1)
        self.assertEqual(report.as_dict()["groups"]["github"]["promotion_todos"], 1)
        promotion_check = report.as_dict()["checks"][0]
        self.assertTrue(promotion_check["promotion_todo"])
        self.assertEqual(promotion_check["promotion_todo_kind"], "repo_auto_merge")

    def test_doctor_output_group_keeps_lane_checks_with_providers(self) -> None:
        check = DoctorCheck(
            name="runtime.local_cli.probe",
            status=STATUS_WARN,
            lane="codex",
            message="probe needs attention",
        )

        self.assertEqual(output.doctor_output_group(check), "providers")

    def test_doctor_output_group_keeps_supervised_checks_together(self) -> None:
        check = DoctorCheck(
            name="supervised_pilot.readiness",
            status=STATUS_PASS,
            message="ready",
        )

        self.assertEqual(output.doctor_output_group(check), "supervised_pilot")


if __name__ == "__main__":
    unittest.main()
