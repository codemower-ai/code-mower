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

    def test_doctor_output_group_keeps_lane_checks_with_providers(self) -> None:
        check = DoctorCheck(
            name="runtime.local_cli.probe",
            status=STATUS_WARN,
            lane="codex",
            message="probe needs attention",
        )

        self.assertEqual(output.doctor_output_group(check), "providers")


if __name__ == "__main__":
    unittest.main()
