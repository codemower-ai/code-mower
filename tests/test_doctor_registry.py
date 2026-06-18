import tempfile
import unittest
from pathlib import Path

from code_mower.doctor_checks import (
    build_doctor_run_plan,
    default_check_group_ids,
    run_doctor,
)


class DoctorRegistryTests(unittest.TestCase):
    def test_default_check_groups_stay_stable(self) -> None:
        self.assertEqual(
            default_check_group_ids(),
            ("runtime", "github", "providers", "cloud", "output"),
        )

    def test_run_plan_enables_optional_stages_explicitly(self) -> None:
        base_plan = build_doctor_run_plan()
        self.assertEqual(
            tuple(stage.id for stage in base_plan),
            ("load-inputs", "select-profile", "runtime", "providers"),
        )
        self.assertTrue(all(not stage.optional for stage in base_plan))

        full_plan = build_doctor_run_plan(github=True, cloud=True)
        self.assertEqual(
            tuple(stage.id for stage in full_plan),
            ("load-inputs", "select-profile", "runtime", "providers", "github", "cloud"),
        )
        self.assertEqual(
            {stage.id for stage in full_plan if stage.optional},
            {"github", "cloud"},
        )

    def test_runner_emits_sanitized_run_plan_check_even_when_inputs_fail(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            report = run_doctor(
                config_path=root_path / "missing-code-mower.yml",
                provider_templates_path=root_path / "missing-providers.yml",
                profile="recommended",
                github=True,
                cloud=True,
            )

        plan_check = next(check for check in report.checks if check.name == "doctor.plan")
        self.assertEqual(plan_check.status, "pass")
        self.assertIn("github", plan_check.message)
        self.assertIn("cloud", plan_check.message)
        self.assertEqual(plan_check.detail["probe_runtime"], False)
        self.assertEqual(
            tuple(stage["id"] for stage in plan_check.detail["stages"]),
            ("load-inputs", "select-profile", "runtime", "providers", "github", "cloud"),
        )
        self.assertEqual(
            tuple(stage["id"] for stage in report.run_plan),
            ("load-inputs", "select-profile", "runtime", "providers", "github", "cloud"),
        )
        self.assertEqual(
            tuple(stage["id"] for stage in report.as_dict()["run_plan"]),
            ("load-inputs", "select-profile", "runtime", "providers", "github", "cloud"),
        )


if __name__ == "__main__":
    unittest.main()
