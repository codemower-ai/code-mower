import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from code_mower import doctor as code_mower_doctor
from code_mower.doctor_checks import (
    build_doctor_run_plan,
    check_adoption_setup,
    check_lane_runtime,
    config_with_repository_target,
    detect_repo_slug,
    default_check_group_ids,
    normalize_repo_slug,
    repo_slug_from_remote,
    DoctorReport,
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

    def test_run_plan_records_adoption_posture(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            report = run_doctor(
                config_path=root_path / "missing-code-mower.yml",
                provider_templates_path=root_path / "missing-providers.yml",
                profile="recommended",
                adoption=True,
                adoption_posture="hosted-builders",
                github=True,
                cloud=True,
            )

        plan_check = next(check for check in report.checks if check.name == "doctor.plan")
        self.assertEqual(plan_check.detail["adoption_posture"], "hosted-builders")

    def test_hosted_builder_posture_skips_local_cli_runtime_checks(self) -> None:
        checks = check_lane_runtime(
            "codex",
            {
                "driver": "local_cli",
                "provider": "codex",
                "token_env": ["MISSING_CODEX_TOKEN"],
                "provider_config": {"command": "definitely-missing-code-mower"},
            },
            probe_runtime=True,
            http_timeout=1,
            adoption_posture="hosted-builders",
        )

        self.assertFalse(
            any(check.name in {"env.tokens", "env.required"} for check in checks)
        )
        local_checks = {
            check.name: check
            for check in checks
            if check.name in {"runtime.local_audit", "runtime.local_cli", "runtime.local_cli.probe"}
        }
        self.assertEqual(
            {name: check.status for name, check in local_checks.items()},
            {
                "runtime.local_audit": "skip",
                "runtime.local_cli": "skip",
                "runtime.local_cli.probe": "skip",
            },
        )
        self.assertIn("hosted-builders", local_checks["runtime.local_cli"].message)

    def test_default_adoption_posture_checks_local_cli_runtime(self) -> None:
        checks = check_lane_runtime(
            "codex",
            {
                "driver": "local_cli",
                "provider": "codex",
                "token_env": ["MISSING_CODEX_TOKEN"],
                "provider_config": {"command": "definitely-missing-code-mower"},
            },
            probe_runtime=True,
            http_timeout=1,
        )

        token_check = next(check for check in checks if check.name == "env.tokens")
        local_cli = next(check for check in checks if check.name == "runtime.local_cli")
        local_probe = next(check for check in checks if check.name == "runtime.local_cli.probe")
        self.assertEqual(token_check.status, "warn")
        self.assertEqual(local_cli.status, "warn")
        self.assertEqual(local_probe.status, "warn")

    def test_doctor_cli_aliases_set_adoption_posture(self) -> None:
        cases = (
            (["--adoption", "--repo", "owner/repo"], "reviewer-gate"),
            (["--adoption", "--hosted-builders", "--repo", "owner/repo"], "hosted-builders"),
            (
                ["--adoption", "--orchestrator-only", "--repo", "owner/repo"],
                "orchestrator-only",
            ),
        )
        for argv, expected in cases:
            with self.subTest(expected=expected):
                captured: dict[str, object] = {}

                def fake_run_doctor(
                    *,
                    _captured: dict[str, object] = captured,
                    **kwargs: object,
                ) -> DoctorReport:
                    _captured.update(kwargs)
                    return DoctorReport(
                        config_path="code-mower.yml",
                        provider_templates_path="providers.yml",
                        profile=str(kwargs.get("profile") or ""),
                        checks=(),
                    )

                with (
                    mock.patch.object(
                        code_mower_doctor,
                        "resolve_doctor_config_path",
                        return_value=ROOT / "code-mower.yml",
                    ),
                    mock.patch.object(
                        code_mower_doctor,
                        "resolve_doctor_provider_templates_path",
                        return_value=ROOT / "src/code_mower/templates/providers.yml",
                    ),
                    mock.patch.object(code_mower_doctor, "run_doctor", fake_run_doctor),
                ):
                    with redirect_stdout(StringIO()):
                        self.assertEqual(code_mower_doctor.main(argv), 0)

                self.assertEqual(captured["adoption_posture"], expected)

    def test_config_source_label_distinguishes_starter_and_explicit_paths(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            starter_path = root_path / "src" / "code_mower" / "templates" / "code-mower.example.yml"
            starter_path.parent.mkdir(parents=True)
            starter_path.write_text("version: 1\n", encoding="utf-8")
            (root_path / "pyproject.toml").write_text("[project]\nname = 'code-mower'\n", encoding="utf-8")

            self.assertEqual(
                code_mower_doctor._doctor_config_source_label(
                    config_arg="code-mower.yml",
                    config_path=starter_path,
                    easy=True,
                    cwd=root_path,
                ),
                "source_tree_starter",
            )
            self.assertEqual(
                code_mower_doctor._doctor_config_source_label(
                    config_arg="custom-code-mower.yml",
                    config_path=root_path / "custom-code-mower.yml",
                    easy=False,
                    cwd=root_path,
                ),
                "explicit_config",
            )
            self.assertEqual(
                code_mower_doctor._doctor_config_source_label(
                    config_arg="code-mower.yml",
                    config_path=root_path / "code-mower.yml",
                    easy=False,
                    cwd=root_path,
                ),
                "repository_config",
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
        self.assertEqual(source_check.detail["config_source"], "packaged_starter")
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
        self.assertEqual(source_check.detail["config_source"], "packaged_starter")
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
