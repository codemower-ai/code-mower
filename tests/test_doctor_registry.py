import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from code_mower.doctor_checks import (
    build_doctor_run_plan,
    check_adoption_setup,
    config_with_repository_target,
    detect_repo_slug,
    default_check_group_ids,
    normalize_repo_slug,
    repo_slug_from_remote,
    run_doctor,
)

ROOT = Path(__file__).resolve().parents[1]


class DoctorRegistryTests(unittest.TestCase):
    def test_default_check_groups_stay_stable(self) -> None:
        self.assertEqual(
            default_check_group_ids(),
            ("runtime", "setup", "github", "providers", "cloud", "output"),
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

        runner_plan = build_doctor_run_plan(runner=True)
        self.assertEqual(
            tuple(stage.id for stage in runner_plan),
            ("load-inputs", "select-profile", "runtime", "providers", "runner"),
        )
        self.assertEqual(
            {stage.id for stage in runner_plan if stage.optional},
            {"runner"},
        )

        adoption_plan = build_doctor_run_plan(adoption=True)
        self.assertEqual(
            tuple(stage.id for stage in adoption_plan),
            ("load-inputs", "select-profile", "runtime", "providers", "adoption"),
        )
        self.assertEqual(
            {stage.id for stage in adoption_plan if stage.optional},
            {"adoption"},
        )

    def test_repo_slug_helpers_support_adoption_targeting(self) -> None:
        self.assertEqual(
            repo_slug_from_remote("git@github.com:codemower-ai/code-mower.git"),
            "codemower-ai/code-mower",
        )
        self.assertEqual(
            repo_slug_from_remote("ssh://git@github.com/codemower-ai/code-mower.git"),
            "codemower-ai/code-mower",
        )
        self.assertEqual(
            normalize_repo_slug(" codemower-ai/code-mower "),
            "codemower-ai/code-mower",
        )
        with self.assertRaisesRegex(ValueError, "OWNER/REPO"):
            normalize_repo_slug("codemower-ai")

    def test_repo_override_preserves_matching_repo_metadata(self) -> None:
        config = {
            "repositories": [
                {
                    "slug": "owner/first",
                    "default_branch": "main",
                    "local_path_env": "FIRST_REPO_PATH",
                },
                {
                    "slug": "owner/second",
                    "default_branch": "develop",
                    "local_path_env": "SECOND_REPO_PATH",
                },
            ],
        }

        targeted = config_with_repository_target(config, "owner/second")

        self.assertEqual(
            targeted["repositories"],
            [
                {
                    "slug": "owner/second",
                    "default_branch": "develop",
                    "local_path_env": "SECOND_REPO_PATH",
                }
            ],
        )

    def test_adoption_warns_when_inferred_repo_disagrees_with_config(self) -> None:
        checks = check_adoption_setup(
            config={"repositories": [{"slug": "owner/configured"}]},
            config_path=ROOT / "code-mower.yml",
            adoption=True,
            repo_slug="owner/from-remote",
            repo_source="git_remote",
            using_packaged_example=False,
        )

        mismatch = next(
            check for check in checks if check.name == "doctor.adoption.repo_mismatch"
        )
        self.assertEqual(mismatch.status, "warn")
        self.assertEqual(mismatch.detail["inferred_repository"], "owner/from-remote")
        self.assertEqual(mismatch.detail["configured_repositories"], ["owner/configured"])

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

    def test_adoption_repo_overrides_packaged_example_repository(self) -> None:
        report = run_doctor(
            config_path=ROOT / "src/code_mower/templates/code-mower.example.yml",
            provider_templates_path=ROOT / "src/code_mower/templates/providers.yml",
            profile="recommended",
            repo_slug="codemower-ai/example-adopter",
            repo_source="explicit",
            adoption=True,
        )

        repo_check = next(check for check in report.checks if check.name == "doctor.repo")
        self.assertEqual(repo_check.status, "pass")
        self.assertEqual(repo_check.detail["repo"], "codemower-ai/example-adopter")

        source_check = next(
            check for check in report.checks if check.name == "doctor.adoption.config_source"
        )
        self.assertEqual(source_check.status, "warn")
        self.assertEqual(source_check.detail["configured_repositories"], ["owner/example"])
        self.assertEqual(
            source_check.detail["effective_repository"],
            "codemower-ai/example-adopter",
        )

        posture_check = next(
            check for check in report.checks if check.name == "config.repositories"
        )
        self.assertEqual(
            posture_check.detail["repositories"],
            ["codemower-ai/example-adopter"],
        )

    def test_packaged_example_config_source_is_explicit_without_adoption_mode(self) -> None:
        report = run_doctor(
            config_path=ROOT / "src/code_mower/templates/code-mower.example.yml",
            provider_templates_path=ROOT / "src/code_mower/templates/providers.yml",
            profile="recommended",
        )

        source_check = next(
            check for check in report.checks if check.name == "doctor.adoption.config_source"
        )
        self.assertEqual(source_check.status, "warn")
        self.assertEqual(source_check.detail["configured_repositories"], ["owner/example"])

        self.assertFalse(
            any(check.name == "doctor.adoption.trusted_authors" for check in report.checks)
        )

    def test_adoption_infers_repo_from_origin_remote(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            subprocess.run(["git", "init", "-q"], cwd=root_path, check=True)
            subprocess.run(
                [
                    "git",
                    "remote",
                    "add",
                    "origin",
                    "https://github.com/codemower-ai/adoption-target.git",
                ],
                cwd=root_path,
                check=True,
            )

            self.assertEqual(
                detect_repo_slug(root_path),
                "codemower-ai/adoption-target",
            )

    def test_packaged_example_config_does_not_require_installed_stale_workflow(self) -> None:
        report = run_doctor(
            config_path=ROOT / "src/code_mower/templates/code-mower.example.yml",
            provider_templates_path=ROOT / "src/code_mower/templates/providers.yml",
            profile="recommended",
        )

        limits_check = next(
            check for check in report.checks if check.name == "config.audit_limits"
        )
        self.assertEqual(limits_check.status, "pass")
        self.assertEqual(limits_check.detail["max_diff_hard_limit_bytes"], 1_500_000)
        self.assertIn("size-aware default", limits_check.message)

        hygiene_checks = [
            check for check in report.checks if check.name == "provider.review_hygiene"
        ]
        self.assertTrue(hygiene_checks)
        self.assertFalse(
            any(
                check.status == "fail"
                and "workflow is configured but missing" in check.message
                for check in hygiene_checks
            )
        )

    def test_runner_doctor_uses_repository_template_source_root(self) -> None:
        from code_mower.doctor_checks import runner as doctor_runner

        with mock.patch.object(
            doctor_runner,
            "check_self_hosted_runner",
            return_value=(),
        ) as runner_checks:
            doctor_runner.run_doctor(
                config_path=ROOT / "src/code_mower/templates/code-mower.example.yml",
                provider_templates_path=ROOT / "src/code_mower/templates/providers.yml",
                profile="recommended",
                runner=True,
            )

        self.assertEqual(runner_checks.call_args.kwargs["provider_templates_root"], ROOT)

    def test_real_config_requires_configured_stale_workflow_file(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            config_path = root_path / "code-mower.yml"
            config_path.write_text(
                "\n".join(
                    [
                        "version: 1",
                        "project:",
                        "  name: test",
                        "  state_dir: .code-mower",
                        "repositories:",
                        "  - slug: owner/repo",
                        "    default_branch: main",
                        "lanes:",
                        "  codex:",
                        "    type: audit",
                        "    provider: codex",
                        "    merge_authority: true",
                        "    driver: manual",
                        "    labels:",
                        "      needs: needs-codex-audit",
                        "      done: codex-audit-done",
                        "      blocked: codex-audit-blocked",
                        "    review_hygiene:",
                        "      workflow: .github/workflows/codex-clear-stale.yml",
                        "      token_env: GITHUB_TOKEN",
                        "profiles:",
                        "  recommended:",
                        "    description: recommended lanes",
                        "    lanes: [codex]",
                    ]
                ),
                encoding="utf-8",
            )

            report = run_doctor(
                config_path=config_path,
                provider_templates_path=ROOT / "src/code_mower/templates/providers.yml",
                profile="recommended",
            )

        hygiene_check = next(
            check
            for check in report.checks
            if check.name == "provider.review_hygiene" and check.lane == "codex"
        )
        self.assertEqual(hygiene_check.status, "fail")
        self.assertEqual(hygiene_check.detail["workflow_exists"], False)
        self.assertIn("configured but missing", hygiene_check.message)


if __name__ == "__main__":
    unittest.main()
