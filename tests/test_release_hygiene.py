from __future__ import annotations

import json
import copy
import importlib.util
import os
import re
import shlex
import subprocess
import sys
import tempfile
import textwrap
import tomllib
import unittest
import urllib.error
from argparse import Namespace
from contextlib import ExitStack, nullcontext, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from code_mower import __version__
from code_mower import audit_labeler_lib
from code_mower import audit_progress
from code_mower import calibration as calibration_pkg
from code_mower import cloud as code_mower_cloud
from code_mower import code_mower_calibration
from code_mower import codex_audit_pr
from code_mower import cli as code_mower_cli
from code_mower import cloud_client
from code_mower import decisions as code_mower_decisions
from code_mower import doctor
from code_mower import doctor_checks
from code_mower import init as code_mower_init
from code_mower import migration as code_mower_migration
from code_mower import migration_install as code_mower_migration_install
from code_mower import next_steps
from code_mower import package as code_mower_package
from code_mower import package_content as code_mower_package_content
from code_mower import package_paths
from code_mower import release_readiness
from code_mower import secrets as code_mower_secrets
from code_mower import config as code_mower_config
from code_mower import provider_runners
from code_mower import versioning as code_mower_versioning
from code_mower.calibration import arms as calibration_arms
from code_mower.calibration import policy as calibration_policy
from code_mower.provider_registry import REFERENCE_PROVIDERS
from scripts import guard_package_workflows, privacy_scan


class ReleaseHygieneTests(unittest.TestCase):
    def test_version_is_current_v05_prerelease(self) -> None:
        self.assertEqual(__version__, "0.5.0b53")

    def test_release_workflow_verifies_downloaded_distributions_before_publish(self) -> None:
        workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn("  verify-distributions:\n", workflow)
        self.assertIn("    needs: build-distributions\n", workflow)
        self.assertIn(
            "        uses: actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c\n",
            workflow,
        )
        self.assertIn("          python -m twine check dist/*\n", workflow)
        publish_job = workflow.split("  publish-pypi:\n", 1)[1]
        self.assertIn("    needs: verify-distributions\n", publish_job)

    def test_test_extra_matches_contributor_setup_docs(self) -> None:
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

        extras = pyproject["project"]["optional-dependencies"]
        self.assertIn("test", extras)
        self.assertIn("pytest>=8.0", extras["test"])
        self.assertIn("ruff>=0.8", extras["test"])

    def test_ruff_static_rule_stage_is_intentional(self) -> None:
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

        lint = pyproject["tool"]["ruff"]["lint"]
        self.assertEqual(lint["select"], ["E", "F", "W", "B"])
        self.assertIn("E501", lint["ignore"])
        self.assertIn("B019", lint["ignore"])

    def test_release_workflow_has_separate_testpypi_publish_gate(self) -> None:
        workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn("      publish_testpypi:\n", workflow)
        self.assertIn("  publish-testpypi:\n", workflow)
        testpypi_job = workflow.split("  publish-testpypi:\n", 1)[1].split(
            "  publish-pypi:\n",
            1,
        )[0]
        self.assertIn("    needs: verify-distributions\n", testpypi_job)
        self.assertIn("inputs.publish_testpypi", testpypi_job)
        self.assertIn("inputs.publish_testpypi == true", testpypi_job)
        self.assertIn("github.event_name == 'workflow_dispatch'", testpypi_job)
        self.assertIn("github.event_name == 'release'", testpypi_job)
        self.assertIn("CODE_MOWER_TESTPYPI_PUBLISH", testpypi_job)
        self.assertIn("    environment: testpypi\n", testpypi_job)
        self.assertIn("repository-url: https://test.pypi.org/legacy/", testpypi_job)
        self.assertIn(
            "uses: pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33",
            testpypi_job,
        )
        production_job = workflow.split("  publish-pypi:\n", 1)[1]
        self.assertIn("inputs.publish_pypi", production_job)
        self.assertIn("inputs.publish_pypi == true", production_job)
        self.assertIn("github.event_name == 'workflow_dispatch'", production_job)
        self.assertIn("github.event_name == 'release'", production_job)
        self.assertIn("CODE_MOWER_PYPI_PUBLISH", production_job)
        self.assertIn("    environment: pypi\n", production_job)
        self.assertNotIn("test.pypi.org", production_job)

    def test_release_workflow_variables_only_apply_to_release_events(self) -> None:
        workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        testpypi_job = workflow.split("  publish-testpypi:\n", 1)[1].split(
            "  publish-pypi:\n",
            1,
        )[0]
        production_job = workflow.split("  publish-pypi:\n", 1)[1]

        self.assertIn(
            "(github.event_name == 'release' && vars.CODE_MOWER_TESTPYPI_PUBLISH == 'true')",
            testpypi_job,
        )
        self.assertIn(
            "(github.event_name == 'release' && vars.CODE_MOWER_PYPI_PUBLISH == 'true')",
            production_job,
        )
        self.assertNotIn(
            "(github.event_name == 'workflow_dispatch' && vars.CODE_MOWER_TESTPYPI_PUBLISH",
            testpypi_job,
        )
        self.assertNotIn(
            "(github.event_name == 'workflow_dispatch' && vars.CODE_MOWER_PYPI_PUBLISH",
            production_job,
        )

    def test_ci_workflow_runs_release_readiness_gate(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

        self.assertIn("      - name: Release readiness\n", workflow)
        self.assertIn(
            "        run: python -m code_mower.migration release-readiness --json\n",
            workflow,
        )
        self.assertIn("      - name: Package-install first-user rehearsal\n", workflow)
        self.assertIn(
            "          python -m code_mower.migration package-install-rehearsal\n",
            workflow,
        )
        self.assertIn('          --package-spec "$GITHUB_WORKSPACE"\n', workflow)
        self.assertIn(
            '          --work-dir "$RUNNER_TEMP/code-mower-package-install"\n',
            workflow,
        )
        self.assertIn('          --python "$(command -v python)"\n', workflow)
        self.assertIn("          --json\n", workflow)

    def test_pypi_runbook_publishes_newest_beta_as_regular_release(self) -> None:
        runbook = (ROOT / "docs/pypi-release.md").read_text(encoding="utf-8")
        rendered_text = re.sub(r"\s+", " ", runbook)

        self.assertIn("`DEC-427-LATEST` records the 2026-08-23 owner decision", runbook)
        self.assertIn(
            "https://github.com/codemower-ai/code-mower/pull/427#issuecomment-5388373205",
            runbook,
        )
        self.assertIn(
            "beta.52 (and future newest betas until 1.0) are published as "
            "**regular releases** (prerelease flag off)",
            rendered_text,
        )
        self.assertIn("GitHub's `/releases/latest` endpoint resolves", rendered_text)
        self.assertIn(
            "prerelease-flagged release cannot be returned by that endpoint",
            rendered_text,
        )
        self.assertIn(
            "publishing beta tags as regular GitHub releases makes the release line look",
            rendered_text,
        )
        self.assertIn("install docs still pin explicit versions", rendered_text)
        self.assertNotIn("Do not manually mark beta releases", runbook)
        self.assertNotIn("newest beta release should be marked", runbook)
        self.assertNotIn("`/releases/latest` endpoint to return `404`", runbook)

    def test_ci_workflow_actionlints_generated_workflows(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

        self.assertIn("      - name: Install actionlint\n", workflow)
        self.assertIn(
            "go install github.com/rhysd/actionlint/cmd/actionlint@v1.7.12\n",
            workflow,
        )
        self.assertIn("      - name: Actionlint generated workflows\n", workflow)
        self.assertIn(
            "run: python scripts/actionlint_generated_workflows.py --code-mower-bin code-mower\n",
            workflow,
        )

    def test_cli_command_registry_is_single_source_of_truth(self) -> None:
        self.assertEqual(
            tuple(code_mower_cli.COMMAND_HANDLERS),
            (
                "antigravity-cli",
                "blind-review",
                "bootstrap",
                "builder",
                "builder-experiment",
                "calibration",
                "claude-audit",
                "claude-bounce",
                "clear-stale",
                "checks",
                "cloud",
                "config",
                "context",
                "context-packs",
                "coderabbit-cli",
                "codex-audit",
                "codex-audit-env-preflight",
                "codex-audit-schema-smoke",
                "decide",
                "doctor",
                "gemini-cli",
                "gate-health",
                "grok-build",
                "hermes-cli",
                "init",
                "lanes",
                "local-llm",
                "migration",
                "merge-plan",
                "next-steps",
                "observe",
                "package",
                "plan",
                "project-context",
                "prompts",
                "providers",
                "reviewer-metrics",
                "saas-reviewer-labeler",
                "telemetry",
                "trailer-comment-labeler",
                "work-order",
            ),
        )
        self.assertTrue(
            all(callable(handler) for handler in code_mower_cli.COMMAND_HANDLERS.values())
        )

    def test_cli_default_help_is_first_user_focused(self) -> None:
        out = StringIO()
        with redirect_stdout(out):
            exit_code = code_mower_cli.main(["--help"])

        self.assertEqual(exit_code, 0)
        help_text = out.getvalue()
        self.assertIn("First-user commands:", help_text)
        self.assertIn("code-mower --help-all", help_text)
        self.assertIn(
            (
                "code-mower migration package-install-rehearsal --package-spec "
                "code-mower==0.5.0b53 --json"
            ),
            help_text,
        )
        self.assertIn("  init", help_text)
        self.assertIn("  doctor", help_text)
        self.assertIn("  checks", help_text)
        self.assertIn("  calibration", help_text)
        self.assertIn("  cloud", help_text)
        self.assertIn("  lanes", help_text)
        self.assertIn("  project-context", help_text)
        self.assertNotIn("trailer-comment-labeler", help_text)
        self.assertNotIn("codex-audit-env-preflight", help_text)
        self.assertNotIn("claude-bounce", help_text)
        self.assertNotIn("  work-order", help_text)
        self.assertNotIn("  providers", help_text)
        self.assertNotIn("  migration", help_text)

    def test_cli_help_all_shows_operator_commands(self) -> None:
        out = StringIO()
        with redirect_stdout(out):
            exit_code = code_mower_cli.main(["--help-all"])

        self.assertEqual(exit_code, 0)
        help_text = out.getvalue()
        self.assertIn("First-user commands:", help_text)
        self.assertIn("Advanced/provider/operator commands:", help_text)
        self.assertIn("trailer-comment-labeler", help_text)
        self.assertIn("codex-audit-env-preflight", help_text)
        self.assertIn("claude-bounce", help_text)
        self.assertIn("builder-experiment", help_text)
        self.assertIn("context", help_text)
        self.assertIn("observe", help_text)
        self.assertIn("work-order", help_text)
        self.assertIn("providers", help_text)
        self.assertIn("migration", help_text)

    def test_codex_audit_transport_commands_skip_git_repo_check(self) -> None:
        config = codex_audit_pr.AuditConfig(
            github_token="",
            repo_paths={},
            codex_cli_path="codex",
        )

        review_command = codex_audit_pr._codex_exec_command(
            config,
            "--sandbox",
            "read-only",
            "review",
        )
        self.assertNotIn("--skip-git-repo-check", review_command)

        transport_command = codex_audit_pr._codex_exec_command(
            config,
            "--sandbox",
            "read-only",
            "--output-schema",
            "schema.json",
            "-",
            skip_git_repo_check=True,
        )
        self.assertIn("--skip-git-repo-check", transport_command)
        self.assertLess(
            transport_command.index("--skip-git-repo-check"),
            transport_command.index("--sandbox"),
        )

    def test_codex_audit_preflight_requires_skip_git_repo_check_flag(self) -> None:
        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            if command == ["codex", "--version"]:
                return subprocess.CompletedProcess(command, 0, stdout="codex 0.140.0\n", stderr="")
            if command == ["codex", "exec", "--help"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=(
                        "Usage: codex exec [OPTIONS] [PROMPT]\n"
                        "If `-` is used, read from stdin\n"
                        "--output-schema\n--output-last-message\n"
                    ),
                    stderr="",
                )
            if command == ["codex", "exec", "review", "--help"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=(
                        "Usage: codex exec review [OPTIONS] [PROMPT]\n"
                        "If `-` is used, read from stdin\n"
                        "--base\n--output-last-message\n"
                    ),
                    stderr="",
                )
            raise AssertionError(f"unexpected command: {command}")

        config = codex_audit_pr.AuditConfig(
            github_token="",
            repo_paths={},
            codex_cli_path="codex",
        )
        with (
            mock.patch.object(
                codex_audit_pr,
                "_resolve_executable_path",
                return_value="codex",
            ),
            mock.patch.object(codex_audit_pr.subprocess, "run", side_effect=fake_run),
        ):
            with self.assertRaisesRegex(RuntimeError, "--skip-git-repo-check"):
                codex_audit_pr.preflight_codex_cli(config)

    def test_codex_audit_preflight_requires_plain_exec_stdin_prompt(self) -> None:
        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            if command == ["codex", "--version"]:
                return subprocess.CompletedProcess(command, 0, stdout="codex 0.140.0\n", stderr="")
            if command == ["codex", "exec", "--help"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=(
                        "Usage: codex exec [OPTIONS]\n"
                        "--skip-git-repo-check\n"
                        "--output-schema\n"
                        "--output-last-message\n"
                    ),
                    stderr="",
                )
            if command == ["codex", "exec", "review", "--help"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout="--base\n--output-last-message\n",
                    stderr="",
                )
            raise AssertionError(f"unexpected command: {command}")

        config = codex_audit_pr.AuditConfig(
            github_token="",
            repo_paths={},
            codex_cli_path="codex",
        )
        with (
            mock.patch.object(
                codex_audit_pr,
                "_resolve_executable_path",
                return_value="codex",
            ),
            mock.patch.object(codex_audit_pr.subprocess, "run", side_effect=fake_run),
        ):
            with self.assertRaisesRegex(RuntimeError, "codex exec - stdin prompt"):
                codex_audit_pr.preflight_codex_cli(config)

    def test_internal_package_seams_keep_cli_first_surface(self) -> None:
        self.assertIs(doctor.DoctorCheck, doctor_checks.DoctorCheck)
        self.assertIs(doctor.DoctorReport, doctor_checks.DoctorReport)
        self.assertIs(doctor.run_doctor, doctor_checks.run_doctor)
        self.assertIs(doctor.render_doctor_text, doctor_checks.render_doctor_text)
        self.assertIs(
            doctor.resolve_doctor_config_path_for_script,
            doctor_checks.resolve_doctor_config_path_for_script,
        )
        self.assertIs(
            doctor.resolve_doctor_provider_templates_path,
            doctor_checks.resolve_doctor_provider_templates_path,
        )
        self.assertIs(
            doctor._apply_first_run_defaults,
            doctor_checks.apply_first_run_defaults,
        )
        rendered_doctor = doctor_checks.render_doctor_text(
            doctor_report := doctor_checks.DoctorReport(
                config_path="code-mower.yml",
                provider_templates_path="code-mower.provider-templates.yml",
                profile="recommended",
                checks=(
                    doctor_checks.DoctorCheck(
                        name="runtime.python",
                        status=doctor_checks.STATUS_PASS,
                        message="Python is supported",
                    ),
                    doctor_checks.DoctorCheck(
                        name="provider.claude.auth",
                        status=doctor_checks.STATUS_WARN,
                        lane="claude-audit",
                        message="runtime probe needs attention",
                        remediation="run `claude -p ok` and retry doctor",
                    ),
                ),
            )
        )
        self.assertIn("Profile: recommended", rendered_doctor)
        self.assertEqual(
            doctor_report.as_dict()["groups"],
            {
                "runtime": {
                    "label": "Runtime",
                    "checks": 1,
                    "failures": 0,
                    "warnings": 0,
                    "skipped": 0,
                },
                "providers": {
                    "label": "Provider lanes",
                    "checks": 1,
                    "failures": 0,
                    "warnings": 1,
                    "skipped": 0,
                },
            },
        )
        self.assertIn(
            "- WARN provider.claude.auth [claude-audit]: runtime probe needs attention",
            rendered_doctor,
        )
        self.assertIn("remediation: run `claude -p ok` and retry doctor", rendered_doctor)
        easy_config = doctor_checks.resolve_doctor_config_path("code-mower.yml", easy=True)
        self.assertEqual(easy_config.name, "code-mower.example.yml")
        self.assertTrue(easy_config.exists())
        self.assertEqual(
            doctor_checks.default_check_group_ids(),
            ("runtime", "github", "providers", "cloud", "output"),
        )
        self.assertEqual(
            cloud_client.dashboard_url_for_endpoint("https://codemower.com/api/ingest"),
            "https://codemower.com/dashboard",
        )
        self.assertTrue(
            cloud_client.is_bundle_manifest(
                {"schema": cloud_client.BUNDLE_SCHEMA},
            )
        )
        self.assertEqual(
            cloud_client.default_dogfood_reports(ROOT)[0][1],
            "value-report",
        )
        self.assertIs(code_mower_cloud.CloudBundleError, cloud_client.CloudBundleError)
        self.assertIs(code_mower_cloud.build_upload_payload, cloud_client.build_upload_payload)
        self.assertIs(code_mower_cloud.post_upload_payload, cloud_client.post_upload_payload)
        self.assertEqual(code_mower_cloud.UPLOAD_SCHEMA, cloud_client.UPLOAD_SCHEMA)
        self.assertIs(
            code_mower_package.load_provider_templates,
            package_paths.load_provider_templates,
        )
        self.assertIs(
            code_mower_package.resolve_provider_templates_path,
            package_paths.resolve_provider_templates_path,
        )
        preview = cloud_client.build_dogfood_dry_run_preview(
            endpoint="https://codemower.com/api/ingest",
            payload={
                "upload_mode": "metadata_only",
                "reports": [{}],
                "events": [{}, {}],
                "privacy_mode": "metadata_and_reports",
                "excluded_content": ["source_code"],
            },
        )
        self.assertEqual(preview["mode"], "cloud-upload-dry-run")
        self.assertEqual(preview["event_count"], 2)
        self.assertEqual(preview["requires_yes"], True)
        self.assertEqual(
            code_mower_calibration._safe_slug("owner/repo with spaces"),
            "owner-repo-with-spaces",
        )
        self.assertEqual(code_mower_calibration._int("42", field="pr"), 42)
        self.assertIs(code_mower_calibration.default_arms, calibration_pkg.default_arms)
        self.assertIs(calibration_pkg.default_arms, calibration_arms.default_arms)
        self.assertEqual(calibration_pkg.float_or_zero("0.75"), 0.75)
        self.assertIs(
            code_mower_calibration._normalize_run_status_category,
            calibration_pkg.normalize_run_status_category,
        )
        self.assertEqual(
            calibration_pkg.normalize_run_status_category("context-insufficient"),
            calibration_pkg.RUN_STATUS_AUDIT_INPUT_INSUFFICIENT,
        )
        self.assertEqual(
            calibration_pkg.status_from_verdict("unknown", returncode=1),
            calibration_pkg.RUN_STATUS_INFRA_ERROR,
        )
        self.assertEqual(
            calibration_pkg.status_from_verdict("stale", returncode=2),
            calibration_pkg.RUN_STATUS_STALE,
        )
        self.assertEqual(
            code_mower_calibration.RUN_STATUS_UNKNOWN,
            calibration_pkg.RUN_STATUS_UNKNOWN,
        )
        self.assertEqual(
            code_mower_calibration.RUN_STATUS_STALE,
            calibration_pkg.RUN_STATUS_STALE,
        )
        self.assertIs(
            code_mower_calibration.RUN_STATUS_CATEGORY_ALIASES,
            calibration_pkg.RUN_STATUS_CATEGORY_ALIASES,
        )
        self.assertIs(
            code_mower_calibration.build_pilot_plan,
            calibration_pkg.build_pilot_plan,
        )
        self.assertIs(
            code_mower_calibration.load_corpus,
            calibration_pkg.load_corpus,
        )
        self.assertIs(
            code_mower_calibration._normalize_truth,
            calibration_pkg.normalize_truth,
        )
        self.assertIs(
            code_mower_calibration._expected_finding_matches,
            calibration_pkg.expected_finding_matches,
        )
        self.assertEqual(
            calibration_pkg.normalize_truth({"source": "known-clean-control"})[
                "expectation"
            ],
            calibration_pkg.TRUTH_EXPECTATION_KNOWN_CLEAN,
        )
        self.assertIs(
            code_mower_calibration._run_records_from_summary,
            calibration_pkg.run_records_from_summary,
        )
        self.assertIs(
            code_mower_calibration._infra_run_record,
            calibration_pkg.infra_run_record,
        )
        self.assertIs(
            code_mower_calibration._load_run_results,
            calibration_pkg.load_run_results,
        )
        self.assertIs(
            code_mower_calibration.corpus_with_run_results,
            calibration_pkg.corpus_with_run_results,
        )
        self.assertIs(
            code_mower_calibration.build_overlap_report,
            calibration_pkg.build_overlap_report,
        )
        self.assertIs(
            code_mower_calibration.render_overlap_text,
            calibration_pkg.render_overlap_text,
        )
        self.assertIs(
            code_mower_calibration._normalize_disposition,
            calibration_pkg.normalize_disposition,
        )
        self.assertTrue(
            calibration_pkg.audit_input_insufficient_result(
                [
                    {
                        "severity": "P2",
                        "summary": "Diff was truncated before the relevant file.",
                    }
                ]
            )
        )

    def test_doctor_github_config_imports_models_from_canonical_module(self) -> None:
        source = (
            ROOT / "src/code_mower/doctor_checks/github_config.py"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "from .models import DoctorCheck, STATUS_PASS, STATUS_WARN",
            source,
        )
        self.assertNotIn("from .common import DoctorCheck", source)

    def test_calibration_arm_catalog_is_packaged_and_explicit_lens_aware(self) -> None:
        arm_ids = {arm["arm_id"] for arm in calibration_pkg.default_arms()}

        self.assertIn("topology-baseline", arm_ids)
        self.assertIn("antigravity-doctrine-lens-fanout", arm_ids)
        self.assertEqual(
            calibration_pkg.DEFAULT_CLI_LANES,
            (
                "gemini_cli",
                "antigravity_cli",
                "hermes_cli",
                "coderabbit_cli",
                "grok_build",
            ),
        )
        package_targets = {target for _, target, _ in code_mower_package.PACKAGE_FILES}
        self.assertIn("src/code_mower/calibration/arms.py", package_targets)
        self.assertIn("src/code_mower/calibration/planning.py", package_targets)
        self.assertIn("src/code_mower/calibration/results.py", package_targets)
        self.assertIn("src/code_mower/calibration/run_results.py", package_targets)
        self.assertIn("src/code_mower/calibration/overlap.py", package_targets)
        self.assertIn("src/code_mower/calibration/effect_report.py", package_targets)
        self.assertIn("src/code_mower/calibration/run_status.py", package_targets)
        self.assertIn("src/code_mower/calibration/truth.py", package_targets)

    def test_provider_runner_github_token_helper_reads_stdin_and_clears_env(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"GITHUB_TOKEN": "env-token", "GH_TOKEN": "gh-token"},
            clear=False,
        ):
            token = provider_runners.resolve_github_token_from_stdin_or_env(
                True,
                stdin=StringIO("stdin-token\n"),
            )
            self.assertEqual(token, "stdin-token")
            self.assertNotIn("GITHUB_TOKEN", os.environ)
            self.assertNotIn("GH_TOKEN", os.environ)

    def test_provider_runner_github_token_helper_legacy_env_path(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"GITHUB_TOKEN": "env-token", "GH_TOKEN": "gh-token"},
            clear=False,
        ):
            token = provider_runners.resolve_github_token_from_stdin_or_env(False)
            self.assertEqual(token, "env-token")
            self.assertNotIn("GITHUB_TOKEN", os.environ)
            self.assertNotIn("GH_TOKEN", os.environ)

    def test_doctor_preflight_applies_v05_first_run_defaults(self) -> None:
        args = Namespace(
            v05=False,
            preflight=True,
            easy=False,
            profile=None,
            probe_runtime=False,
            github=False,
            cloud=False,
        )

        doctor._apply_first_run_defaults(args)

        self.assertTrue(args.easy)
        self.assertEqual(args.profile, "recommended")
        self.assertTrue(args.probe_runtime)
        self.assertTrue(args.github)
        self.assertTrue(args.cloud)

    def test_cloud_upload_dry_run_does_not_require_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            report = work / "reviewer-value-report.md"
            report.write_text("# Reviewer Value Report\n\nNo findings.\n", encoding="utf-8")
            bundle = work / "bundle"

            export_out = StringIO()
            with redirect_stdout(export_out):
                export_code = code_mower_cloud.main(
                    [
                        "export",
                        "--report",
                        f"value-report={report}",
                        "--repo-slug",
                        "owner/repo",
                        "--output-dir",
                        str(bundle),
                        "--anonymous",
                        "--json",
                    ]
                )
            self.assertEqual(export_code, 0)
            self.assertEqual(
                json.loads(export_out.getvalue())["mode"],
                "cloud-export",
            )

            upload_out = StringIO()
            with mock.patch.dict(os.environ, {"CODE_MOWER_CLOUD_TOKEN": ""}, clear=False):
                with redirect_stdout(upload_out):
                    upload_code = code_mower_cloud.main(
                        [
                            "upload",
                            str(bundle),
                            "--dry-run",
                            "--json",
                        ]
                    )

            self.assertEqual(upload_code, 0)
            payload = json.loads(upload_out.getvalue())
            self.assertEqual(payload["mode"], "cloud-upload-dry-run")
            self.assertFalse(payload["would_upload"])
            self.assertEqual(payload["report_count"], 1)

    def test_cloud_catch_up_dry_run_builds_workflow_events_without_git_refs(self) -> None:
        fake_runs = [
            {
                "databaseId": 123,
                "name": "CI",
                "status": "completed",
                "conclusion": "success",
                "event": "push",
                "headBranch": "secret-feature-name",
                "headSha": "abc123",
                "createdAt": "2026-06-15T00:00:00Z",
                "updatedAt": "2026-06-15T00:01:00Z",
                "url": "https://github.com/owner/repo/actions/runs/123",
            }
        ]

        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "bundle"
            out = StringIO()
            completed = subprocess.CompletedProcess(
                ["gh", "run", "list"],
                0,
                stdout=json.dumps(fake_runs),
                stderr="",
            )
            with mock.patch.object(subprocess, "run", return_value=completed):
                with mock.patch.dict(os.environ, {"CODE_MOWER_CLOUD_TOKEN": ""}, clear=False):
                    with redirect_stdout(out):
                        code = code_mower_cloud.main(
                            [
                                "catch-up",
                                "--repo-slug",
                                "owner/repo",
                                "--output-dir",
                                str(bundle),
                                "--json",
                            ]
                        )
            manifest = json.loads(
                (bundle / "code-mower-cloud-bundle.json").read_text(encoding="utf-8")
            )

        self.assertEqual(code, 0)
        result = json.loads(out.getvalue())
        self.assertEqual(result["mode"], "cloud-catch-up")
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["run_count"], 1)
        self.assertEqual(result["catch_up"]["provenance"], "imported_history")
        self.assertTrue(result["catch_up"]["history_only"])
        self.assertFalse(result["catch_up"]["calibration_evidence"])
        self.assertIn("activity context", result["catch_up"]["trust_guidance"]["use_for"])
        self.assertIn(
            "lane promotion",
            result["catch_up"]["trust_guidance"]["do_not_use_for"],
        )
        self.assertEqual(result["catch_up"]["workflow_counts"], {"CI": 1})
        self.assertEqual(result["catch_up"]["conclusion_counts"], {"success": 1})
        self.assertEqual(result["export"]["event_count"], 1)
        self.assertFalse(result["upload"]["would_upload"])
        event = manifest["events"][0]
        self.assertEqual(event["event_type"], "workflow_run")
        self.assertEqual(event["repo_slug"], "owner/repo")
        self.assertEqual(event["source"], "github-actions-catch-up")
        self.assertEqual(event["status"], "success")
        self.assertNotIn("head_branch", event["dimensions"])
        self.assertNotIn("head_sha", event["dimensions"])

    def test_cloud_catch_up_can_include_git_refs_explicitly(self) -> None:
        fake_runs = [
            {
                "databaseId": 456,
                "name": "Dogfood",
                "status": "completed",
                "conclusion": "success",
                "event": "push",
                "headBranch": "main",
                "headSha": "def456",
                "createdAt": "2026-06-15T00:00:00Z",
                "updatedAt": "2026-06-15T00:01:00Z",
                "url": "https://github.com/owner/repo/actions/runs/456",
            }
        ]

        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "bundle"
            completed = subprocess.CompletedProcess(
                ["gh", "run", "list"],
                0,
                stdout=json.dumps(fake_runs),
                stderr="",
            )
            with mock.patch.object(subprocess, "run", return_value=completed):
                with redirect_stdout(StringIO()):
                    code = code_mower_cloud.main(
                        [
                            "catch-up",
                            "--repo-slug",
                            "owner/repo",
                            "--output-dir",
                            str(bundle),
                            "--include-git-ref",
                            "--json",
                        ]
                    )
            manifest = json.loads(
                (bundle / "code-mower-cloud-bundle.json").read_text(encoding="utf-8")
                )

        self.assertEqual(code, 0)
        event = manifest["events"][0]
        self.assertEqual(event["dimensions"]["head_branch"], "main")
        self.assertEqual(event["dimensions"]["head_sha"], "def456")

    def test_dev_python_wrapper_is_executable(self) -> None:
        wrapper = ROOT / "scripts/dev-python"
        self.assertTrue(wrapper.exists())
        self.assertTrue(os.access(wrapper, os.X_OK))

    def test_dev_python_wrapper_runs_supported_python(self) -> None:
        completed = subprocess.run(
            [
                str(ROOT / "scripts/dev-python"),
                "-c",
                "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_dev_python_wrapper_rejects_unsupported_python(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_python = Path(tmp) / "python-old"
            fake_python.write_text(
                """#!/usr/bin/env bash
if [[ "$1" == "-" ]]; then
  exit 1
fi
exit 1
""",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            completed = subprocess.run(
                [str(ROOT / "scripts/dev-python"), "--version"],
                cwd=tmp,
                env={"CODE_MOWER_PYTHON": str(fake_python), "PATH": os.defpath},
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("Python 3.12+", completed.stderr)

    def test_direct_cli_execution_points_to_package_or_dev_wrapper(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(ROOT / "src/code_mower/cli.py"), "--version"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("pipx install code-mower==0.5.0b53", completed.stderr)
        self.assertIn("scripts/dev-python -m venv .venv", completed.stderr)
        self.assertIn(".venv/bin/code-mower", completed.stderr)
        self.assertNotIn("PYTHONPATH=src", completed.stderr)

    def test_public_source_checkout_guidance_uses_dev_wrapper_not_pythonpath(self) -> None:
        public_docs = [
            ROOT / "README.md",
            ROOT / "docs/quickstart.md",
            ROOT / "docs/architecture.md",
            ROOT / "docs/public-release-checklist.md",
        ]

        for path in public_docs:
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                text = path.read_text(encoding="utf-8")
                self.assertIn("scripts/dev-python", text)
                self.assertNotIn("PYTHONPATH=src", text)

    def test_provider_registry_uses_package_relative_profile_import(self) -> None:
        text = (ROOT / "src/code_mower/provider_registry.py").read_text(
            encoding="utf-8",
        )

        self.assertIn("from . import local_llm_profiles", text)
        self.assertNotIn("from tools import local_llm_profiles", text)
        self.assertNotIn("direct repo execution fallback", text)

    def test_shared_templates_match_packaged_templates(self) -> None:
        shared_templates = [
            "builder-experiment.example.json",
            "calibration-corpus.example.json",
            "calibration-corpus.json",
            "context-packs.example.json",
            "providers.yml",
            "reviewer-value-report.example.md",
            "reviewer-spend.example.json",
        ]
        for relative_path in shared_templates:
            with self.subTest(template=relative_path):
                self.assertEqual(
                    (ROOT / "templates" / relative_path).read_text(encoding="utf-8"),
                    (ROOT / "src/code_mower/templates" / relative_path).read_text(
                        encoding="utf-8"
                    ),
                )
        self.assertEqual(
            (ROOT / "tools/reviewer_value_report.example.md").read_text(
                encoding="utf-8"
            ),
            (ROOT / "templates/reviewer-value-report.example.md").read_text(
                encoding="utf-8"
            ),
        )

    def test_clear_stale_workflow_template_matches_package_generator(self) -> None:
        template = (
            ROOT / "templates/workflows/review-clear-stale.yml.j2"
        ).read_text(encoding="utf-8")
        self.assertEqual(
            template,
            (
                ROOT
                / "src/code_mower/templates/workflows/review-clear-stale.yml.j2"
            ).read_text(encoding="utf-8"),
        )
        self.assertEqual(
            template,
            code_mower_package_content._workflow_template_text(
                "templates/workflows/review-clear-stale.yml.j2"
            ),
        )
        self.assertIn(
            "actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10",
            template,
        )
        self.assertNotIn("actions/checkout@v4", template)
        self.assertIn(
            "{% raw %}${{ github.event.inputs.lane || 'devin' }}{% endraw %}",
            template,
        )
        self.assertIn("code-mower-clear-stale-", template)
        self.assertIn("cancel-in-progress: true", template)
        self.assertIn("No PR/head SHA available for stale-label cleanup", template)
        self.assertIn("{% raw %}${{ secrets.GITHUB_TOKEN }}{% endraw %}", template)
        self.assertIn("pull-requests: write", template)

    def test_reviewer_workflow_templates_are_real_and_packaged(self) -> None:
        workflow_templates = (
            "audit-label-cleanup.yml.j2",
            "builder-provenance.yml.j2",
            "code-mower-agent-pr-labeler.yml.j2",
            "code-mower-fix-round-dispatch.yml.j2",
            "code-mower-gate-health.yml.j2",
            "code-mower-gate.yml.j2",
            "dispatch-lanes.yml.j2",
            "hosted-bridge.yml.j2",
            "lane-mac-runner.yml.j2",
            "local-cli-audit.yml.j2",
            "self-hosted-local-audit.yml.j2",
            "saas-reviewer-labeler.yml.j2",
            "trailer-comment-labeler.yml.j2",
        )
        for filename in workflow_templates:
            with self.subTest(workflow=filename):
                template = (
                    ROOT / "templates/workflows" / filename
                ).read_text(encoding="utf-8")
                packaged_template = (
                    ROOT / "src/code_mower/templates/workflows" / filename
                ).read_text(encoding="utf-8")
                self.assertEqual(template, packaged_template)
                self.assertEqual(
                    template,
                    code_mower_package_content._workflow_template_text(
                        f"templates/workflows/{filename}"
                    ),
                )
                self.assertEqual(
                    template,
                    code_mower_package_content._workflow_template_text(
                        f"src/code_mower/templates/workflows/{filename}"
                    ),
                )
                self.assertNotIn("Replace placeholders", template)
                self.assertNotIn("Install this generated template", template)
                if filename == "builder-provenance.yml.j2":
                    self.assertIn(
                        f"CODE_MOWER_PACKAGE_SPEC: {next_steps.current_alpha_package_spec()}",
                        template,
                    )

        trailer = (
            ROOT / "templates/workflows/trailer-comment-labeler.yml.j2"
        ).read_text(encoding="utf-8")
        self.assertIn("tools/code_mower trailer-comment-labeler", trailer)
        self.assertIn("__TRAILER_LANE__", trailer)
        self.assertIn("__BOT_AUTHORS__", trailer)
        self.assertIn("CODE_MOWER_GITHUB_ACTIONS_WORKFLOWS", trailer)
        self.assertIn("actions: write", trailer)
        self.assertIn("pull-requests: write", trailer)
        self.assertIn("types: [created, edited]", trailer)
        self.assertIn("gh workflow run code-mower-gate.yml", trailer)
        self.assertIn('gh api "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}"', trailer)
        self.assertIn("vars.__AUTHORS_ENV__", trailer)
        self.assertIn(
            "contains(github.event.comment.body, '__TRAILER_PREFIX__')",
            trailer,
        )
        self.assertNotIn("workflow_dispatch:", trailer)
        self.assertNotIn("github.event.inputs.lane", trailer)
        gate_template = (
            ROOT / "templates/workflows/code-mower-gate.yml.j2"
        ).read_text(encoding="utf-8")
        self.assertIn("actions: read", gate_template)
        self.assertIn("contents: read", gate_template)
        self.assertIn("issues: write", gate_template)
        self.assertIn("Check out Code Mower support files", gate_template)
        self.assertIn("CODE_MOWER_AUDIT_RUN", gate_template)
        self.assertIn("comment_id", gate_template)
        self.assertIn("body_sha256", gate_template)
        self.assertIn("github_actions_comment_attested", gate_template)
        self.assertIn("github_actions_workflows", gate_template)
        self.assertIn("Clear stale Code Mower gate override", gate_template)
        self.assertIn("audit in flight", gate_template)
        self.assertIn("workflow_run:", gate_template)
        self.assertIn('workflows: ["Code Mower Local CLI Audits"]', gate_template)
        self.assertIn("github.event.workflow_run.pull_requests[0].number", gate_template)
        self.assertIn("WORKFLOW_RUN_HEAD_SHA", gate_template)
        self.assertIn("commits/${WORKFLOW_RUN_HEAD_SHA}/pulls?per_page=100", gate_template)
        self.assertIn("WORKFLOW_RUN_HEAD_BRANCH", gate_template)
        self.assertIn('--method GET', gate_template)
        self.assertIn('gh api "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}"', gate_template)
        self.assertIn("export HEAD_SHA", gate_template)
        self.assertNotIn("HEAD_SHA: ${{ github.event.pull_request.head.sha || github.event.inputs.head_sha || github.event.workflow_run.head_sha }}", gate_template)
        self.assertIn("CODE_MOWER_HISTORY_REWRITE_WARNING", gate_template)
        self.assertIn(
            "actions/runs?event=pull_request_target&status={status}&per_page=100",
            gate_template,
        )
        hosted = (
            ROOT / "templates/workflows/hosted-bridge.yml.j2"
        ).read_text(encoding="utf-8")
        self.assertIn("gh issue edit", hosted)
        self.assertIn("pull-requests: write", hosted)
        saas = (
            ROOT / "templates/workflows/saas-reviewer-labeler.yml.j2"
        ).read_text(encoding="utf-8")
        self.assertIn("tools/code_mower saas-reviewer-labeler", saas)
        self.assertIn("__BOT_AUTHORS__", saas)
        self.assertIn("pull-requests: write", saas)
        self.assertNotIn("workflow_dispatch:", saas)
        self.assertNotIn("github.event.inputs.adapter", saas)
        cleanup = (
            ROOT / "templates/workflows/audit-label-cleanup.yml.j2"
        ).read_text(encoding="utf-8")
        self.assertIn("pull-requests: write", cleanup)
        local_audit = (
            ROOT / "templates/workflows/self-hosted-local-audit.yml.j2"
        ).read_text(encoding="utf-8")
        self.assertIn("pull-requests: write", local_audit)
        self.assertIn("vars.__LOCAL_AUDIT_RUNNER_ENABLED_VAR__ == 'true'", local_audit)
        self.assertIn("Verify local runner environment", local_audit)
        self.assertIn("for name in USER LOGNAME SHELL LANG", local_audit)
        self.assertIn("CODE_MOWER_REVIEWER_SPEND_PATH", local_audit)
        self.assertNotIn("CODE_MOWER_REVIEWER_SPEND_PATH", local_audit.split("    steps:", 1)[0])
        self.assertIn("Reset pull request checkout path", local_audit)
        self.assertIn("${{ github.workflow }}-${{ github.event.pull_request.number }}", local_audit)
        self.assertIn("${{ matrix.lane.lane }}", local_audit)
        self.assertIn("cancel-in-progress: true", local_audit)
        self.assertIn("id: run_audit", local_audit)
        self.assertIn("audited=false", local_audit)
        self.assertIn("audited=true", local_audit)
        self.assertIn("steps.run_audit.outputs.audited == 'true'", local_audit)
        self.assertIn("Upload Code Mower audit metadata", local_audit)
        self.assertIn("secrets.CODE_MOWER_CLOUD_TOKEN", local_audit)
        self.assertIn("cloud reviewer-runs", local_audit)
        self.assertIn("cloud dogfood", local_audit)
        self.assertIn("--event \"work_order=${event_file}\"", local_audit)
        self.assertIn('${event_args[@]+"${event_args[@]}"}', local_audit)
        self.assertNotIn('"${event_args[@]}" || upload_failed=1', local_audit)
        gate_health = (
            ROOT / "templates/workflows/code-mower-gate-health.yml.j2"
        ).read_text(encoding="utf-8")
        self.assertIn("CODE_MOWER_GATE_HEALTH_LANES_JSON", gate_health)
        self.assertIn("tools/code_mower gate-health", gate_health)
        self.assertIn("--status-issue", gate_health)
        self.assertIn("--runner-label", gate_health)
        self.assertIn("--liveness-minutes", gate_health)
        self.assertIn("CODE_MOWER_GATE_HEALTH_RUNNER_TOKEN", gate_health)

    def test_local_audit_label_expression_uses_exact_label_membership(self) -> None:
        entries = ({"needs_label": "needs-codex-audit"},)

        expression = code_mower_init._local_audit_label_expression(
            entries,
            "pull_request",
        )

        self.assertIn(
            "contains(github.event.pull_request.labels.*.name, 'needs-codex-audit')",
            expression,
        )
        self.assertNotIn("join(", expression)

    def test_repository_local_audit_workflow_uses_source_cli(self) -> None:
        workflow = (ROOT / ".github/workflows/local-cli-audit.yml").read_text(
            encoding="utf-8"
        )
        gate = (ROOT / ".github/workflows/code-mower-gate.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("name: Code Mower Local CLI Audits", workflow)
        self.assertIn("vars.CODE_MOWER_LOCAL_AUDIT_RUNNER_ENABLED == 'true'", workflow)
        self.assertIn("runs-on: [self-hosted, macOS, code-mower-audit]", workflow)
        self.assertIn(
            'export PYTHONPATH="${SUPPORT_PATH}/src${PYTHONPATH:+:${PYTHONPATH}}"',
            workflow,
        )
        self.assertIn("tools/run_codex_audit_pr.sh", workflow)
        self.assertIn("tools/run_claude_audit_pr.sh", workflow)
        self.assertIn(
            '"${SUPPORT_PATH}/scripts/dev-python" -m code_mower.cli cloud reviewer-runs',
            workflow,
        )
        self.assertIn(
            '"${SUPPORT_PATH}/scripts/dev-python" -m code_mower.cli cloud dogfood',
            workflow,
        )
        self.assertNotIn('"${SUPPORT_PATH}/tools/code_mower" cloud', workflow)

        wrappers = {
            "tools/run_codex_audit_pr.sh": "codex-audit",
            "tools/run_claude_audit_pr.sh": "claude-audit",
        }
        for relative_path, command in wrappers.items():
            wrapper = (ROOT / relative_path).read_text(encoding="utf-8")
            self.assertIn("scripts/dev-python", wrapper)
            self.assertIn(f"code_mower.cli {command}", wrapper)
            self.assertIn('export PYTHONPATH="${repo_root}/src${PYTHONPATH:+:${PYTHONPATH}}"', wrapper)
            self.assertNotIn('${script_dir}/code_mower', wrapper)

        self.assertIn('CODE_MOWER_OWNER_LOGIN: "jeffhuber"', gate)
        self.assertIn('CODE_MOWER_DECISION_AUTHORITIES: "jeffhuber"', gate)
        self.assertIn(
            "CODE_MOWER_GATE_AUTOMERGE_TOKEN: ${{ secrets.CODE_MOWER_GATE_AUTOMERGE_TOKEN || secrets.DISPATCH_TOKEN || '' }}",
            gate,
        )
        self.assertIn('GH_TOKEN="${automerge_token}" gh api graphql', gate)
        self.assertNotIn("TODO_OWNER_LOGIN", gate)

        workflow_commands = {
            ".github/workflows/claude-audit-labeler.yml": "trailer-comment-labeler",
            ".github/workflows/codex-audit-labeler.yml": "trailer-comment-labeler",
            ".github/workflows/claude-clear-stale.yml": "clear-stale",
            ".github/workflows/codex-clear-stale.yml": "clear-stale",
        }
        for relative_path, command in workflow_commands.items():
            installed = (ROOT / relative_path).read_text(encoding="utf-8")
            self.assertIn(f"scripts/dev-python -m code_mower.cli {command}", installed)
            self.assertIn("PYTHONPATH: src", installed)
            self.assertNotIn("tools/code_mower", installed)

    def test_package_workflow_guard_rejects_runner_context_in_job_env(self) -> None:
        bad_workflow = """
name: bad
jobs:
  audit:
    runs-on: ubuntu-latest
    env:
      BAD: ${{ runner.temp }}/spend.json
    steps:
      - name: ok
        env:
          OK: ${{ runner.temp }}/step.json
        run: true
"""

        errors = guard_package_workflows._job_env_runner_context_errors(
            "bad.yml",
            bad_workflow,
        )

        self.assertEqual(len(errors), 1)
        self.assertIn("runner context is not valid in job-level env", errors[0])

    def test_gate_health_template_invokes_packaged_command(self) -> None:
        template = (
            ROOT / "src/code_mower/templates/workflows/code-mower-gate-health.yml.j2"
        ).read_text(encoding="utf-8")
        self.assertIn("tools/code_mower gate-health", template)
        self.assertIn("--repo \"{% raw %}${{ github.repository }}{% endraw %}\"", template)
        self.assertIn("--status-issue \"${STATUS_ISSUE}\"", template)
        self.assertIn("--runner-label \"${CODE_MOWER_LOCAL_AUDIT_RUNNER_LABEL}\"", template)
        self.assertIn(
            "--liveness-minutes \"${CODE_MOWER_GATE_HEALTH_LIVENESS_MINUTES}\"",
            template,
        )
        self.assertNotIn("python3 - <<'PY'", template)

    def test_provider_catalog_wires_merge_authority_stale_hygiene(self) -> None:
        for relative_path in (
            "templates/providers.yml",
            "src/code_mower/templates/providers.yml",
        ):
            catalog = code_mower_config.load_config(ROOT / relative_path)
            providers = catalog["provider_templates"]
            self.assertEqual(
                providers["codex"]["review_hygiene"]["workflow"],
                ".github/workflows/codex-clear-stale.yml",
            )
            self.assertEqual(
                providers["claude_audit"]["review_hygiene"]["workflow"],
                ".github/workflows/claude-clear-stale.yml",
            )
            self.assertEqual(providers["acp_bridge"]["review_hygiene"], {})
            self.assertEqual(providers["aider"]["review_hygiene"], {})

    def _run_gate_template_decision(
        self,
        *,
        lanes: list[dict[str, str]],
        labels: set[str],
        comments: list[dict[str, object]] | None = None,
        events: list[dict[str, object]] | None = None,
        event_pages: list[list[dict[str, object]]] | None = None,
        head_sha: str = "a" * 40,
        pr_head_sha: str | None = None,
        pr_number: int = 7,
        actions_runs: dict[str, dict[str, object]] | None = None,
        audit_runs: list[dict[str, object]] | None = None,
        commit_pull_requests: dict[str, list[dict[str, object]]] | None = None,
        author_exclusion: dict[str, object] | None = None,
        owner_login: str = "owner",
        env: dict[str, str] | None = None,
    ) -> dict[str, str]:
        template = (
            ROOT / "src/code_mower/templates/workflows/code-mower-gate.yml.j2"
        ).read_text(encoding="utf-8")
        script = template.split(
            'python3 - "${labels_file}" "${comments_file}" "${events_file}" "${pr_file}" "${audit_runs_file}" <<\'PY\'\n',
            1,
        )[1].split("\n          PY", 1)[0]
        script = textwrap.dedent(script)

        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            json.dump([{"name": label} for label in sorted(labels)], handle)
            labels_path = handle.name
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            json.dump([comments or []], handle)
            comments_path = handle.name
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            json.dump(event_pages if event_pages is not None else [events or []], handle)
            events_path = handle.name
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            json.dump({"number": pr_number, "head": {"sha": pr_head_sha or head_sha}}, handle)
            pr_path = handle.name
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            json.dump(audit_runs or [], handle)
            audit_runs_path = handle.name

        stdout = StringIO()
        action_patch = nullcontext()
        if actions_runs is not None:
            def fake_github_request(
                method: str,
                path: str,
                *,
                tokens: object,
                body: object = None,
                allow_missing: bool = False,
            ) -> object:
                del body, allow_missing, tokens
                if method != "GET":
                    raise AssertionError(f"unexpected GitHub method: {method}")
                run_prefix = "/repos/owner/repo/actions/runs/"
                if path.startswith(run_prefix):
                    run_id = path.removeprefix(run_prefix)
                    return actions_runs.get(run_id) or {}
                commit_prefix = "/repos/owner/repo/commits/"
                if path.startswith(commit_prefix) and path.endswith("/pulls?per_page=100"):
                    head = path.removeprefix(commit_prefix).removesuffix("/pulls?per_page=100")
                    return (commit_pull_requests or {}).get(head) or []
                raise AssertionError(f"unexpected gh api endpoint: {path}")

            action_patch = ExitStack()
            action_patch.enter_context(
                mock.patch.object(
                    audit_labeler_lib,
                    "github_request_with_fallback",
                    fake_github_request,
                )
            )
            try:
                from tools import audit_labeler_lib as tools_audit_labeler_lib
            except ImportError:
                tools_audit_labeler_lib = None
            if tools_audit_labeler_lib is not None:
                action_patch.enter_context(
                    mock.patch.object(
                        tools_audit_labeler_lib,
                        "github_request_with_fallback",
                        fake_github_request,
                    )
                )
        try:
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "CODE_MOWER_GATE_LANES_JSON": json.dumps(lanes),
                        "CODE_MOWER_AUTHOR_EXCLUSION_JSON": json.dumps(
                            author_exclusion or {"enabled": False}
                        ),
                        "CODE_MOWER_OWNER_LABEL": "needs-owner",
                        "CODE_MOWER_OWNER_SITTING_LABEL": "owner-sitting",
                        "CODE_MOWER_OWNER_LOGIN": owner_login,
                        "CODE_MOWER_GATE_OVERRIDE_LABEL": "gate:override",
                        "CODE_MOWER_DECISION_AUTHORITIES": owner_login,
                        "GITHUB_REPOSITORY": "owner/repo",
                        "GH_TOKEN": "test-token",
                        "HEAD_SHA": head_sha,
                        "PR_NUMBER": str(pr_number),
                        **(env or {}),
                    },
                ),
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        "gate-decision",
                        labels_path,
                        comments_path,
                        events_path,
                        pr_path,
                        audit_runs_path,
                    ],
                ),
                action_patch,
                redirect_stdout(stdout),
            ):
                try:
                    exec(compile(script, "code-mower-gate.yml.j2", "exec"), {})
                except SystemExit as exc:
                    if exc.code not in (0, None):
                        raise
        finally:
            Path(labels_path).unlink(missing_ok=True)
            Path(comments_path).unlink(missing_ok=True)
            Path(events_path).unlink(missing_ok=True)
            Path(pr_path).unlink(missing_ok=True)
            Path(audit_runs_path).unlink(missing_ok=True)

        result: dict[str, str] = {}
        for line in stdout.getvalue().splitlines():
            key, value = line.split("=", 1)
            result[key] = shlex.split(value)[0]
        return result

    def _bound_actions_body(self, body: str, *, comment_id: int = 1234) -> str:
        return provider_runners.bind_actions_run_comment_id(body, comment_id)

    def test_gate_decision_rejects_builder_exclusion_with_no_independent_lane(
        self,
    ) -> None:
        result = self._run_gate_template_decision(
            lanes=[
                {
                    "id": "codex",
                    "display_name": "Codex",
                    "done": "codex-audit-done",
                    "blocked": "codex-audit-blocked",
                    "author_lane": "codex",
                    "builder_label": "builder:codex",
                    "bot_authors": "codex-audit-bot,codex-audit-bot[bot]",
                }
            ],
            labels={"builder:codex"},
            author_exclusion={
                "enabled": True,
                "labels": {"builder:codex": "codex"},
                "authors": {},
            },
        )

        self.assertEqual(result["gate_state"], "failure")
        self.assertEqual(
            result["gate_description"],
            "no independent Code Mower audit lane remains",
        )

    def test_gate_decision_allows_author_exclusion_with_peer_pass(self) -> None:
        result = self._run_gate_template_decision(
            lanes=[
                {
                    "id": "codex",
                    "display_name": "Codex",
                    "done": "codex-audit-done",
                    "blocked": "codex-audit-blocked",
                    "author_lane": "codex",
                    "builder_label": "builder:codex",
                    "bot_authors": "codex-audit-bot,codex-audit-bot[bot]",
                },
                {
                    "id": "claude",
                    "display_name": "Claude",
                    "done": "claude-audit-done",
                    "blocked": "claude-audit-blocked",
                    "author_lane": "claude",
                    "builder_label": "builder:claude",
                    "bot_authors": "claude-audit-bot,claude-audit-bot[bot]",
                },
            ],
            labels={"builder:codex", "claude-audit-done"},
            author_exclusion={
                "enabled": True,
                "labels": {"builder:codex": "codex"},
                "authors": {},
            },
            comments=[
                {
                    "body": "Head SHA: `" + ("a" * 40) + "`\n"
                    "<!-- CLAUDE_AUDIT_STATE: claude-audit-done -->",
                    "user": {"login": "claude-audit-bot"},
                }
            ],
        )

        self.assertEqual(result["gate_state"], "success")
        self.assertEqual(result["gate_description"], "Code Mower merge gate passed")

    def test_gate_decision_accepts_configured_lane_author_env(self) -> None:
        result = self._run_gate_template_decision(
            lanes=[
                {
                    "id": "claude",
                    "display_name": "Claude",
                    "done": "claude-audit-done",
                    "blocked": "claude-audit-blocked",
                    "builder_label": "builder:claude",
                    "bot_authors": "claude-audit-bot,claude-audit-bot[bot]",
                    "authors_env": "CLAUDE_AUDIT_BOT_AUTHORS",
                }
            ],
            labels={"claude-audit-done"},
            comments=[
                {
                    "body": "Head SHA: `" + ("a" * 40) + "`\n"
                    "<!-- CLAUDE_AUDIT_STATE: claude-audit-done -->",
                    "user": {"login": "jeffhuber"},
                }
            ],
            env={"CLAUDE_AUDIT_BOT_AUTHORS": "jeffhuber,github-actions[bot]"},
        )

        self.assertEqual(result["gate_state"], "success")
        self.assertEqual(result["gate_description"], "Code Mower merge gate passed")

    def test_gate_decision_requires_current_head_done_trailer(self) -> None:
        result = self._run_gate_template_decision(
            lanes=[
                {
                    "id": "claude",
                    "display_name": "Claude",
                    "done": "claude-audit-done",
                    "blocked": "claude-audit-blocked",
                    "builder_label": "builder:claude",
                    "bot_authors": "claude-audit-bot,claude-audit-bot[bot]",
                }
            ],
            labels={"claude-audit-done"},
            comments=[
                {
                    "body": "Head SHA: `" + ("b" * 40) + "`\n"
                    "<!-- CLAUDE_AUDIT_STATE: claude-audit-done -->",
                    "user": {"login": "claude-audit-bot"},
                }
            ],
        )

        self.assertEqual(result["gate_state"], "pending")
        self.assertEqual(result["gate_description"], "waiting for audit: Claude")

    def test_gate_decision_pending_on_owner_sitting_label(self) -> None:
        result = self._run_gate_template_decision(
            lanes=[
                {
                    "id": "claude",
                    "display_name": "Claude",
                    "done": "claude-audit-done",
                    "blocked": "claude-audit-blocked",
                    "builder_label": "builder:claude",
                    "bot_authors": "claude-audit-bot,claude-audit-bot[bot]",
                }
            ],
            labels={"owner-sitting", "claude-audit-done"},
            comments=[
                {
                    "body": "Head SHA: `" + ("a" * 40) + "`\n"
                    "<!-- CLAUDE_AUDIT_STATE: claude-audit-done -->",
                    "user": {"login": "claude-audit-bot"},
                }
            ],
        )

        self.assertEqual(result["gate_state"], "pending")
        self.assertEqual(result["gate_description"], "owner-sitting: waiting on owner")

    def test_gate_decision_later_blocked_beats_earlier_done_label(self) -> None:
        head_sha = "a" * 40
        result = self._run_gate_template_decision(
            lanes=[
                {
                    "id": "claude",
                    "display_name": "Claude",
                    "done": "claude-audit-done",
                    "blocked": "claude-audit-blocked",
                    "builder_label": "builder:claude",
                    "bot_authors": "claude-audit-bot,claude-audit-bot[bot]",
                }
            ],
            labels={"claude-audit-done"},
            comments=[
                {
                    "body": "Head SHA: `" + head_sha + "`\n"
                    "<!-- CLAUDE_AUDIT_STATE: claude-audit-done -->",
                    "user": {"login": "claude-audit-bot"},
                },
                {
                    "body": "Head SHA: `" + head_sha + "`\n"
                    "<!-- CLAUDE_AUDIT_STATE: claude-audit-blocked -->",
                    "user": {"login": "claude-audit-bot"},
                },
            ],
            head_sha=head_sha,
        )

        self.assertEqual(result["gate_state"], "failure")
        self.assertEqual(result["gate_description"], "blocked audit: Claude")

    def test_gate_decision_treats_decision_covered_blocked_verdict_as_done(self) -> None:
        head_sha = "a" * 40
        result = self._run_gate_template_decision(
            lanes=[
                {
                    "id": "codex",
                    "display_name": "Codex",
                    "done": "codex-audit-done",
                    "blocked": "codex-audit-blocked",
                    "decision_coverage": True,
                    "builder_label": "builder:codex",
                    "bot_authors": "codex-audit-bot,codex-audit-bot[bot]",
                }
            ],
            labels={"codex-audit-done"},
            comments=[
                {
                    "id": 1,
                    "author_association": "MEMBER",
                    "body": (
                        '<!-- CODE_MOWER_DECISION: id=ADR-007 scope=finding '
                        'finding_id="codex:4c3d9d885482c8e63cca" by=owner ref=ADR-007 -->'
                    ),
                    "user": {"login": "owner"},
                },
                {
                    "id": 2,
                    "created_at": "2026-08-18T02:52:00Z",
                    "body": (
                        "Head SHA: `" + head_sha + "`\n"
                        "Findings:\n\n"
                        + code_mower_decisions.render_audit_findings_marker(
                            lane="codex",
                            findings=[
                                {
                                    "severity": "P2",
                                    "title": "HOST_DISPLAY_NAME class-B finding repeats",
                                    "file": "src/app.py",
                                    "line": 1,
                                }
                            ],
                            complete=True,
                        )
                        + "\n\n"
                        "- [P2] HOST_DISPLAY_NAME class-B finding repeats -- `src/app.py:1`\n"
                        "  ADR-007 already accepted this topic.\n\n"
                        "<!-- CODEX_AUDIT_STATE: codex-audit-blocked -->"
                    ),
                    "user": {"login": "codex-audit-bot"},
                },
            ],
            head_sha=head_sha,
        )

        self.assertEqual(result["gate_state"], "success")
        self.assertEqual(result["gate_description"], "Code Mower merge gate passed")

    def test_gate_decision_does_not_promote_free_text_findings(self) -> None:
        head_sha = "a" * 40
        title = "HOST_DISPLAY_NAME class-B finding repeats"
        result = self._run_gate_template_decision(
            lanes=[
                {
                    "id": "codex",
                    "display_name": "Codex",
                    "done": "codex-audit-done",
                    "blocked": "codex-audit-blocked",
                    "decision_coverage": True,
                    "builder_label": "builder:codex",
                    "bot_authors": "codex-audit-bot,codex-audit-bot[bot]",
                }
            ],
            labels={"codex-audit-done"},
            comments=[
                {
                    "id": 1,
                    "body": code_mower_decisions.render_decision_marker(
                        code_mower_decisions.DecisionRecord(
                            id="ADR-007",
                            scope="finding",
                            resolves="",
                            by="owner",
                            finding_id=code_mower_decisions.stable_finding_id(
                                "codex",
                                title,
                                "src/app.py",
                            ),
                            ref="ADR-007",
                        )
                    ),
                    "user": {"login": "owner"},
                },
                {
                    "id": 2,
                    "created_at": "2026-08-18T02:52:00Z",
                    "body": (
                        "Head SHA: `" + head_sha + "`\n"
                        "Findings:\n\n"
                        f"- [P2] {title} -- `src/app.py:1`\n"
                        "  ADR-007 already accepted this topic.\n\n"
                        "<!-- CODEX_AUDIT_STATE: codex-audit-blocked -->"
                    ),
                    "user": {"login": "codex-audit-bot"},
                },
            ],
            head_sha=head_sha,
        )

        self.assertEqual(result["gate_state"], "failure")
        self.assertEqual(result["gate_description"], "blocked audit: Codex")

    def test_gate_decision_does_not_promote_unopted_lane_findings(self) -> None:
        head_sha = "a" * 40
        title = "HOST_DISPLAY_NAME class-B finding repeats"
        result = self._run_gate_template_decision(
            lanes=[
                {
                    "id": "devin",
                    "display_name": "Devin",
                    "done": "devin-audit-done",
                    "blocked": "devin-audit-blocked",
                    "builder_label": "builder:devin",
                    "bot_authors": "devin-ai-integration,devin-ai-integration[bot]",
                }
            ],
            labels={"devin-audit-done"},
            comments=[
                {
                    "id": 1,
                    "body": code_mower_decisions.render_decision_marker(
                        code_mower_decisions.DecisionRecord(
                            id="ADR-007",
                            scope="finding",
                            resolves="",
                            by="owner",
                            finding_id=code_mower_decisions.stable_finding_id(
                                "devin",
                                title,
                                "src/app.py",
                            ),
                            ref="ADR-007",
                        )
                    ),
                    "user": {"login": "owner"},
                },
                {
                    "id": 2,
                    "created_at": "2026-08-18T02:52:00Z",
                    "body": (
                        "Head SHA: `" + head_sha + "`\n"
                        "Findings:\n\n"
                        + code_mower_decisions.render_audit_findings_marker(
                            lane="devin",
                            findings=[
                                {
                                    "severity": "P2",
                                    "title": title,
                                    "file": "src/app.py",
                                    "line": 1,
                                }
                            ],
                            complete=True,
                        )
                        + "\n\n"
                        f"- [P2] {title} -- `src/app.py:1`\n"
                        "  ADR-007 already accepted this topic.\n\n"
                        "<!-- DEVIN_AUDIT_STATE: devin-audit-blocked -->"
                    ),
                    "user": {"login": "devin-ai-integration"},
                },
            ],
            head_sha=head_sha,
        )

        self.assertEqual(result["gate_state"], "failure")
        self.assertEqual(result["gate_description"], "blocked audit: Devin")

    def test_gate_decision_uses_updated_comment_order_for_latest_verdict(self) -> None:
        head_sha = "a" * 40
        result = self._run_gate_template_decision(
            lanes=[
                {
                    "id": "claude",
                    "display_name": "Claude",
                    "done": "claude-audit-done",
                    "blocked": "claude-audit-blocked",
                    "builder_label": "builder:claude",
                    "bot_authors": "claude-audit-bot,claude-audit-bot[bot]",
                }
            ],
            labels={"claude-audit-done"},
            comments=[
                {
                    "id": 10,
                    "created_at": "2026-08-18T02:52:00Z",
                    "updated_at": "2026-08-18T03:02:00Z",
                    "body": "Head SHA: `" + head_sha + "`\n"
                    "<!-- CLAUDE_AUDIT_STATE: claude-audit-done -->",
                    "user": {"login": "claude-audit-bot"},
                },
                {
                    "id": 11,
                    "created_at": "2026-08-18T03:01:00Z",
                    "updated_at": "2026-08-18T03:01:00Z",
                    "body": "Head SHA: `" + head_sha + "`\n"
                    "<!-- CLAUDE_AUDIT_STATE: claude-audit-blocked -->",
                    "user": {"login": "claude-audit-bot"},
                },
            ],
            head_sha=head_sha,
        )

        self.assertEqual(result["gate_state"], "success")
        self.assertEqual(result["gate_description"], "Code Mower merge gate passed")

    def test_gate_decision_pending_when_required_audit_run_in_flight(self) -> None:
        head_sha = "a" * 40
        result = self._run_gate_template_decision(
            lanes=[
                {
                    "id": "claude_audit",
                    "author_lane": "claude",
                    "display_name": "Claude",
                    "done": "claude-audit-done",
                    "blocked": "claude-audit-blocked",
                    "builder_label": "builder:claude",
                    "bot_authors": "claude-audit-bot,claude-audit-bot[bot]",
                    "github_actions_workflows": ".github/workflows/local-cli-audit.yml",
                }
            ],
            labels={"claude-audit-done"},
            comments=[
                {
                    "body": "Head SHA: `" + head_sha + "`\n"
                    "<!-- CLAUDE_AUDIT_STATE: claude-audit-done -->",
                    "user": {"login": "claude-audit-bot"},
                }
            ],
            head_sha=head_sha,
            audit_runs=[
                {
                    "run": {
                        "id": 12345,
                        "status": "in_progress",
                        "path": ".github/workflows/local-cli-audit.yml",
                        "head_sha": head_sha,
                        "created_at": "2026-08-19T16:18:09Z",
                        "pull_requests": [{"number": 7, "head": {"sha": head_sha}}],
                    },
                    "jobs": [
                        {
                            "name": "audit (claude)",
                            "status": "in_progress",
                            "conclusion": None,
                            "started_at": "2026-08-19T16:18:12Z",
                        }
                    ],
                }
            ],
        )

        self.assertEqual(result["gate_state"], "pending")
        self.assertEqual(
            result["gate_description"],
            "audit in flight: Claude (run 12345 job 'audit (claude)' queued since 2026-08-19T16:18:12Z)",
        )

    def test_gate_decision_done_verdict_older_than_in_flight_job_stays_pending(
        self,
    ) -> None:
        head_sha = "a" * 40
        result = self._run_gate_template_decision(
            lanes=[
                {
                    "id": "claude_audit",
                    "author_lane": "claude",
                    "display_name": "Claude",
                    "done": "claude-audit-done",
                    "blocked": "claude-audit-blocked",
                    "builder_label": "builder:claude",
                    "bot_authors": "claude-audit-bot,claude-audit-bot[bot]",
                    "github_actions_workflows": ".github/workflows/local-cli-audit.yml",
                }
            ],
            labels={"claude-audit-done"},
            comments=[
                {
                    "id": 222,
                    "created_at": "2026-08-19T16:25:00Z",
                    "updated_at": "2026-08-19T16:25:00Z",
                    "body": "Head SHA: `" + head_sha + "`\n"
                    "<!-- CLAUDE_AUDIT_STATE: claude-audit-done -->",
                    "user": {"login": "claude-audit-bot"},
                }
            ],
            head_sha=head_sha,
            audit_runs=[
                {
                    "run": {
                        "id": 12345,
                        "status": "in_progress",
                        "path": ".github/workflows/local-cli-audit.yml",
                        "head_sha": head_sha,
                        "created_at": "2026-08-19T16:18:09Z",
                        "pull_requests": [{"number": 7, "head": {"sha": head_sha}}],
                    },
                    "jobs": [
                        {
                            "name": "audit (claude)",
                            "status": "in_progress",
                            "conclusion": None,
                            "started_at": "2026-08-19T16:30:00Z",
                        }
                    ],
                }
            ],
        )

        self.assertEqual(result["gate_state"], "pending")
        self.assertEqual(
            result["gate_description"],
            "audit in flight: Claude (run 12345 job 'audit (claude)' queued since 2026-08-19T16:30:00Z)",
        )

    def test_gate_decision_done_verdict_newer_than_in_flight_run_passes(self) -> None:
        head_sha = "a" * 40
        result = self._run_gate_template_decision(
            lanes=[
                {
                    "id": "claude_audit",
                    "author_lane": "claude",
                    "display_name": "Claude",
                    "done": "claude-audit-done",
                    "blocked": "claude-audit-blocked",
                    "builder_label": "builder:claude",
                    "bot_authors": "claude-audit-bot,claude-audit-bot[bot]",
                    "github_actions_workflows": ".github/workflows/local-cli-audit.yml",
                }
            ],
            labels={"claude-audit-done"},
            comments=[
                {
                    "id": 222,
                    "created_at": "2026-08-19T16:25:00Z",
                    "updated_at": "2026-08-19T16:25:00Z",
                    "body": "Head SHA: `" + head_sha + "`\n"
                    "<!-- CLAUDE_AUDIT_STATE: claude-audit-done -->",
                    "user": {"login": "claude-audit-bot"},
                }
            ],
            head_sha=head_sha,
            audit_runs=[
                {
                    "run": {
                        "id": 12345,
                        "status": "in_progress",
                        "path": ".github/workflows/local-cli-audit.yml",
                        "head_sha": head_sha,
                        "created_at": "2026-08-19T16:18:09Z",
                        "pull_requests": [{"number": 7, "head": {"sha": head_sha}}],
                    },
                    "jobs": [
                        {
                            "name": "audit (claude)",
                            "status": "in_progress",
                            "conclusion": None,
                        }
                    ],
                }
            ],
        )

        self.assertEqual(result["gate_state"], "success")
        self.assertEqual(result["gate_description"], "Code Mower merge gate passed")

    def test_gate_decision_equal_second_newer_verdict_run_passes(self) -> None:
        head_sha = "a" * 40
        body = self._bound_actions_body(
            "Head SHA: `" + head_sha + "`\n"
            "<!-- CODE_MOWER_AUDIT_RUN: run_id=12346 -->\n"
            "<!-- CLAUDE_AUDIT_STATE: claude-audit-done -->",
            comment_id=222,
        )
        result = self._run_gate_template_decision(
            lanes=[
                {
                    "id": "claude_audit",
                    "author_lane": "claude",
                    "display_name": "Claude",
                    "done": "claude-audit-done",
                    "blocked": "claude-audit-blocked",
                    "builder_label": "builder:claude",
                    "bot_authors": "github-actions[bot]",
                    "github_actions_workflows": ".github/workflows/local-cli-audit.yml",
                }
            ],
            labels={"claude-audit-done"},
            comments=[
                {
                    "id": 222,
                    "created_at": "2026-08-19T16:25:00Z",
                    "updated_at": "2026-08-19T16:25:00Z",
                    "body": body,
                    "user": {"login": "github-actions[bot]"},
                }
            ],
            head_sha=head_sha,
            actions_runs={
                "12346": {
                    "path": ".github/workflows/local-cli-audit.yml",
                    "event": "pull_request_target",
                    "head_sha": head_sha,
                    "pull_requests": [{"number": 7, "head": {"sha": head_sha}}],
                },
            },
            audit_runs=[
                {
                    "run": {
                        "id": 12345,
                        "status": "in_progress",
                        "path": ".github/workflows/local-cli-audit.yml",
                        "head_sha": head_sha,
                        "created_at": "2026-08-19T16:25:00Z",
                        "pull_requests": [{"number": 7, "head": {"sha": head_sha}}],
                    },
                    "jobs": [
                        {
                            "name": "audit (claude)",
                            "status": "in_progress",
                            "conclusion": None,
                            "started_at": "2026-08-19T16:25:00Z",
                        }
                    ],
                }
            ],
        )

        self.assertEqual(result["gate_state"], "success")
        self.assertEqual(result["gate_description"], "Code Mower merge gate passed")

    def test_gate_decision_equal_second_older_verdict_run_stays_pending(self) -> None:
        head_sha = "a" * 40
        body = self._bound_actions_body(
            "Head SHA: `" + head_sha + "`\n"
            "<!-- CODE_MOWER_AUDIT_RUN: run_id=12345 -->\n"
            "<!-- CLAUDE_AUDIT_STATE: claude-audit-done -->",
            comment_id=222,
        )
        result = self._run_gate_template_decision(
            lanes=[
                {
                    "id": "claude_audit",
                    "author_lane": "claude",
                    "display_name": "Claude",
                    "done": "claude-audit-done",
                    "blocked": "claude-audit-blocked",
                    "builder_label": "builder:claude",
                    "bot_authors": "github-actions[bot]",
                    "github_actions_workflows": ".github/workflows/local-cli-audit.yml",
                }
            ],
            labels={"claude-audit-done"},
            comments=[
                {
                    "id": 222,
                    "created_at": "2026-08-19T16:25:00Z",
                    "updated_at": "2026-08-19T16:25:00Z",
                    "body": body,
                    "user": {"login": "github-actions[bot]"},
                }
            ],
            head_sha=head_sha,
            actions_runs={
                "12345": {
                    "path": ".github/workflows/local-cli-audit.yml",
                    "event": "pull_request_target",
                    "head_sha": head_sha,
                    "pull_requests": [{"number": 7, "head": {"sha": head_sha}}],
                },
            },
            audit_runs=[
                {
                    "run": {
                        "id": 12346,
                        "status": "in_progress",
                        "path": ".github/workflows/local-cli-audit.yml",
                        "head_sha": head_sha,
                        "created_at": "2026-08-19T16:25:00Z",
                        "pull_requests": [{"number": 7, "head": {"sha": head_sha}}],
                    },
                    "jobs": [
                        {
                            "name": "audit (claude)",
                            "status": "in_progress",
                            "conclusion": None,
                            "started_at": "2026-08-19T16:25:00Z",
                        }
                    ],
                }
            ],
        )

        self.assertEqual(result["gate_state"], "pending")
        self.assertEqual(
            result["gate_description"],
            "audit in flight: Claude (run 12346 job 'audit (claude)' queued since 2026-08-19T16:25:00Z)",
        )

    def test_gate_decision_older_verdict_run_edited_after_in_flight_stays_pending(
        self,
    ) -> None:
        head_sha = "a" * 40
        body = self._bound_actions_body(
            "Head SHA: `" + head_sha + "`\n"
            "<!-- CODE_MOWER_AUDIT_RUN: run_id=12345 -->\n"
            "<!-- CLAUDE_AUDIT_STATE: claude-audit-done -->",
            comment_id=222,
        )
        result = self._run_gate_template_decision(
            lanes=[
                {
                    "id": "claude_audit",
                    "author_lane": "claude",
                    "display_name": "Claude",
                    "done": "claude-audit-done",
                    "blocked": "claude-audit-blocked",
                    "builder_label": "builder:claude",
                    "bot_authors": "github-actions[bot]",
                    "github_actions_workflows": ".github/workflows/local-cli-audit.yml",
                }
            ],
            labels={"claude-audit-done"},
            comments=[
                {
                    "id": 222,
                    "created_at": "2026-08-19T16:20:00Z",
                    "updated_at": "2026-08-19T16:35:00Z",
                    "body": body,
                    "user": {"login": "github-actions[bot]"},
                }
            ],
            head_sha=head_sha,
            actions_runs={
                "12345": {
                    "path": ".github/workflows/local-cli-audit.yml",
                    "event": "pull_request_target",
                    "head_sha": head_sha,
                    "pull_requests": [{"number": 7, "head": {"sha": head_sha}}],
                },
            },
            audit_runs=[
                {
                    "run": {
                        "id": 12346,
                        "status": "in_progress",
                        "path": ".github/workflows/local-cli-audit.yml",
                        "head_sha": head_sha,
                        "created_at": "2026-08-19T16:30:00Z",
                        "pull_requests": [{"number": 7, "head": {"sha": head_sha}}],
                    },
                    "jobs": [
                        {
                            "name": "audit (claude)",
                            "status": "in_progress",
                            "conclusion": None,
                            "started_at": "2026-08-19T16:30:00Z",
                        }
                    ],
                }
            ],
        )

        self.assertEqual(result["gate_state"], "pending")
        self.assertEqual(
            result["gate_description"],
            "audit in flight: Claude (run 12346 job 'audit (claude)' queued since 2026-08-19T16:30:00Z)",
        )

    def test_gate_decision_404_terminal_audit_runs_fixture_passes(self) -> None:
        head_sha = "7821693" + ("a" * 33)
        codex_body = self._bound_actions_body(
            "Head SHA: `" + head_sha + "`\n"
            "<!-- CODE_MOWER_AUDIT_RUN: run_id=31101 -->\n"
            "<!-- CODEX_AUDIT_STATE: codex-audit-done -->",
            comment_id=501,
        )
        claude_body = self._bound_actions_body(
            "Head SHA: `" + head_sha + "`\n"
            "<!-- CODE_MOWER_AUDIT_RUN: run_id=31102 -->\n"
            "<!-- CLAUDE_AUDIT_STATE: claude-audit-done -->",
            comment_id=502,
        )
        result = self._run_gate_template_decision(
            lanes=[
                {
                    "id": "codex",
                    "author_lane": "codex",
                    "display_name": "Codex",
                    "done": "codex-audit-done",
                    "blocked": "codex-audit-blocked",
                    "builder_label": "builder:codex",
                    "bot_authors": "github-actions[bot]",
                    "github_actions_workflows": ".github/workflows/local-cli-audit.yml",
                },
                {
                    "id": "claude_audit",
                    "author_lane": "claude",
                    "display_name": "Claude",
                    "done": "claude-audit-done",
                    "blocked": "claude-audit-blocked",
                    "builder_label": "builder:claude",
                    "bot_authors": "github-actions[bot]",
                    "github_actions_workflows": ".github/workflows/local-cli-audit.yml",
                },
            ],
            labels={"codex-audit-done", "claude-audit-done"},
            comments=[
                {
                    "id": 501,
                    "created_at": "2026-08-19T16:25:00Z",
                    "updated_at": "2026-08-19T16:25:00Z",
                    "body": codex_body,
                    "user": {"login": "github-actions[bot]"},
                },
                {
                    "id": 502,
                    "created_at": "2026-08-19T16:26:00Z",
                    "updated_at": "2026-08-19T16:26:00Z",
                    "body": claude_body,
                    "user": {"login": "github-actions[bot]"},
                },
            ],
            head_sha=head_sha,
            actions_runs={
                "31101": {
                    "path": ".github/workflows/local-cli-audit.yml",
                    "event": "pull_request_target",
                    "head_sha": head_sha,
                    "pull_requests": [{"number": 7, "head": {"sha": head_sha}}],
                },
                "31102": {
                    "path": ".github/workflows/local-cli-audit.yml",
                    "event": "pull_request_target",
                    "head_sha": head_sha,
                    "pull_requests": [{"number": 7, "head": {"sha": head_sha}}],
                },
            },
            audit_runs=[
                {
                    "run": {
                        "id": 31101,
                        "status": "completed",
                        "conclusion": "cancelled",
                        "path": ".github/workflows/local-cli-audit.yml",
                        "head_sha": head_sha,
                        "created_at": "2026-08-19T16:18:09Z",
                        "pull_requests": [{"number": 7, "head": {"sha": head_sha}}],
                    },
                    "jobs": [
                        {
                            "name": "audit (codex)",
                            "status": "in_progress",
                            "conclusion": None,
                        },
                        {
                            "name": "audit (claude)",
                            "status": "queued",
                            "conclusion": None,
                        },
                    ],
                },
                {
                    "run": {
                        "id": 31102,
                        "status": "completed",
                        "conclusion": "success",
                        "path": ".github/workflows/local-cli-audit.yml",
                        "head_sha": head_sha,
                        "created_at": "2026-08-19T16:18:09Z",
                        "pull_requests": [{"number": 7, "head": {"sha": head_sha}}],
                    },
                    "jobs": [
                        {
                            "name": "audit (codex)",
                            "status": "completed",
                            "conclusion": "success",
                        },
                        {
                            "name": "audit (claude)",
                            "status": "completed",
                            "conclusion": "success",
                        },
                    ],
                },
                {
                    "run": {
                        "id": 31103,
                        "status": "completed",
                        "conclusion": "skipped",
                        "path": ".github/workflows/local-cli-audit.yml",
                        "head_sha": head_sha,
                        "created_at": "2026-08-19T16:30:00Z",
                        "pull_requests": [{"number": 7, "head": {"sha": head_sha}}],
                    },
                    "jobs": [],
                },
            ],
        )

        self.assertEqual(result["gate_state"], "success")
        self.assertEqual(result["gate_description"], "Code Mower merge gate passed")

    def test_gate_decision_ignores_in_flight_run_for_different_pr(self) -> None:
        head_sha = "a" * 40
        result = self._run_gate_template_decision(
            lanes=[
                {
                    "id": "claude_audit",
                    "author_lane": "claude",
                    "display_name": "Claude",
                    "done": "claude-audit-done",
                    "blocked": "claude-audit-blocked",
                    "builder_label": "builder:claude",
                    "bot_authors": "claude-audit-bot,claude-audit-bot[bot]",
                    "github_actions_workflows": ".github/workflows/local-cli-audit.yml",
                }
            ],
            labels={"claude-audit-done"},
            comments=[
                {
                    "body": "Head SHA: `" + head_sha + "`\n"
                    "<!-- CLAUDE_AUDIT_STATE: claude-audit-done -->",
                    "user": {"login": "claude-audit-bot"},
                }
            ],
            head_sha=head_sha,
            audit_runs=[
                {
                    "run": {
                        "id": 12345,
                        "status": "in_progress",
                        "path": ".github/workflows/local-cli-audit.yml",
                        "head_sha": head_sha,
                        "pull_requests": [{"number": 99, "head": {"sha": head_sha}}],
                    },
                    "jobs": [
                        {
                            "name": "audit (claude)",
                            "status": "in_progress",
                            "conclusion": None,
                        }
                    ],
                }
            ],
        )

        self.assertEqual(result["gate_state"], "success")
        self.assertEqual(result["gate_description"], "Code Mower merge gate passed")

    def test_gate_decision_reports_attested_non_current_heads(self) -> None:
        head_sha = "a" * 40
        old_head = "b" * 40
        result = self._run_gate_template_decision(
            lanes=[
                {
                    "id": "claude",
                    "display_name": "Claude",
                    "done": "claude-audit-done",
                    "blocked": "claude-audit-blocked",
                    "builder_label": "builder:claude",
                    "bot_authors": "claude-audit-bot,claude-audit-bot[bot]",
                }
            ],
            labels={"claude-audit-done"},
            comments=[
                {
                    "body": "Head SHA: `" + old_head + "`\n"
                    "<!-- CLAUDE_AUDIT_STATE: claude-audit-done -->",
                    "user": {"login": "claude-audit-bot"},
                },
                {
                    "body": "Head SHA: `" + head_sha + "`\n"
                    "<!-- CLAUDE_AUDIT_STATE: claude-audit-done -->",
                    "user": {"login": "claude-audit-bot"},
                },
            ],
            head_sha=head_sha,
        )

        self.assertEqual(json.loads(result["history_warning_heads"]), [old_head])
        self.assertEqual(result["gate_state"], "success")

    def test_gate_decision_rejects_abbreviated_current_head_trailer(self) -> None:
        result = self._run_gate_template_decision(
            lanes=[
                {
                    "id": "claude",
                    "display_name": "Claude",
                    "done": "claude-audit-done",
                    "blocked": "claude-audit-blocked",
                    "builder_label": "builder:claude",
                    "bot_authors": "claude-audit-bot,claude-audit-bot[bot]",
                }
            ],
            labels={"claude-audit-done"},
            comments=[
                {
                    "body": "Head SHA: `" + ("a" * 12) + "`\n"
                    "<!-- CLAUDE_AUDIT_STATE: claude-audit-done -->",
                    "user": {"login": "claude-audit-bot"},
                }
            ],
        )

        self.assertEqual(result["gate_state"], "pending")
        self.assertEqual(result["gate_description"], "waiting for audit: Claude")

    def test_gate_decision_rejects_dispatch_head_mismatch(self) -> None:
        result = self._run_gate_template_decision(
            lanes=[
                {
                    "id": "claude",
                    "display_name": "Claude",
                    "done": "claude-audit-done",
                    "blocked": "claude-audit-blocked",
                    "builder_label": "builder:claude",
                    "bot_authors": "claude-audit-bot,claude-audit-bot[bot]",
                }
            ],
            labels={"claude-audit-done"},
            comments=[
                {
                    "body": "Head SHA: `" + ("a" * 40) + "`\n"
                    "<!-- CLAUDE_AUDIT_STATE: claude-audit-done -->",
                    "user": {"login": "claude-audit-bot"},
                }
            ],
            pr_head_sha="b" * 40,
        )

        self.assertEqual(result["gate_state"], "failure")
        self.assertEqual(
            result["gate_description"],
            "workflow head_sha does not match PR head",
        )

    def test_gate_decision_ignores_untrusted_trailer_comment(self) -> None:
        result = self._run_gate_template_decision(
            lanes=[
                {
                    "id": "claude",
                    "display_name": "Claude",
                    "done": "claude-audit-done",
                    "blocked": "claude-audit-blocked",
                    "builder_label": "builder:claude",
                    "bot_authors": "claude-audit-bot,claude-audit-bot[bot]",
                }
            ],
            labels={"claude-audit-done"},
            comments=[
                {
                    "body": "Head SHA: `" + ("a" * 40) + "`\n"
                    "<!-- CLAUDE_AUDIT_STATE: claude-audit-done -->",
                    "user": {"login": "drive-by-commenter"},
                }
            ],
        )

        self.assertEqual(result["gate_state"], "pending")
        self.assertEqual(result["gate_description"], "waiting for audit: Claude")

    def test_gate_decision_rejects_unattested_github_actions_comment(self) -> None:
        result = self._run_gate_template_decision(
            lanes=[
                {
                    "id": "claude",
                    "display_name": "Claude",
                    "done": "claude-audit-done",
                    "blocked": "claude-audit-blocked",
                    "builder_label": "builder:claude",
                    "bot_authors": "github-actions[bot]",
                    "github_actions_workflows": ".github/workflows/local-cli-audit.yml",
                }
            ],
            labels={"claude-audit-done"},
            comments=[
                {
                    "body": "Head SHA: `" + ("a" * 40) + "`\n"
                    "<!-- CLAUDE_AUDIT_STATE: claude-audit-done -->",
                    "id": 1234,
                    "user": {"login": "github-actions[bot]"},
                }
            ],
            actions_runs={},
        )

        self.assertEqual(result["gate_state"], "pending")
        self.assertEqual(result["gate_description"], "waiting for audit: Claude")

    def test_gate_decision_accepts_attested_github_actions_comment(self) -> None:
        result = self._run_gate_template_decision(
            lanes=[
                {
                    "id": "claude",
                    "display_name": "Claude",
                    "done": "claude-audit-done",
                    "blocked": "claude-audit-blocked",
                    "builder_label": "builder:claude",
                    "bot_authors": "github-actions[bot]",
                    "github_actions_workflows": ".github/workflows/local-cli-audit.yml",
                }
            ],
            labels={"claude-audit-done"},
            comments=[
                {
                    "body": self._bound_actions_body(
                        "Head SHA: `" + ("a" * 40) + "`\n"
                        "<!-- CODE_MOWER_AUDIT_RUN: run_id=12345 -->\n"
                        "<!-- CLAUDE_AUDIT_STATE: claude-audit-done -->"
                    ),
                    "id": 1234,
                    "user": {"login": "github-actions[bot]"},
                }
            ],
            actions_runs={
                "12345": {
                    "path": ".github/workflows/local-cli-audit.yml",
                    "event": "pull_request_target",
                    "pull_requests": [
                        {
                            "number": 7,
                            "head": {"sha": "a" * 40},
                        }
                    ],
                }
            },
        )

        self.assertEqual(result["gate_state"], "success")
        self.assertEqual(result["gate_description"], "Code Mower merge gate passed")

    def test_gate_decision_rejects_replayed_github_actions_comment(self) -> None:
        result = self._run_gate_template_decision(
            lanes=[
                {
                    "id": "claude",
                    "display_name": "Claude",
                    "done": "claude-audit-done",
                    "blocked": "claude-audit-blocked",
                    "builder_label": "builder:claude",
                    "bot_authors": "github-actions[bot]",
                    "github_actions_workflows": ".github/workflows/local-cli-audit.yml",
                }
            ],
            labels={"claude-audit-done"},
            comments=[
                {
                    "body": self._bound_actions_body(
                        "Head SHA: `" + ("a" * 40) + "`\n"
                        "<!-- CODE_MOWER_AUDIT_RUN: run_id=12345 -->\n"
                        "<!-- CLAUDE_AUDIT_STATE: claude-audit-done -->"
                    ),
                    "id": 9876,
                    "user": {"login": "github-actions[bot]"},
                }
            ],
            actions_runs={
                "12345": {
                    "path": ".github/workflows/local-cli-audit.yml",
                    "event": "pull_request_target",
                    "pull_requests": [
                        {
                            "number": 7,
                            "head": {"sha": "a" * 40},
                        }
                    ],
                }
            },
        )

        self.assertEqual(result["gate_state"], "pending")
        self.assertEqual(result["gate_description"], "waiting for audit: Claude")

    def test_gate_decision_rejects_tampered_github_actions_comment_body(self) -> None:
        bound_body = self._bound_actions_body(
            "Head SHA: `" + ("a" * 40) + "`\n"
            "<!-- CODE_MOWER_AUDIT_RUN: run_id=12345 -->\n"
            "<!-- CLAUDE_AUDIT_STATE: claude-audit-blocked -->"
        )
        result = self._run_gate_template_decision(
            lanes=[
                {
                    "id": "claude",
                    "display_name": "Claude",
                    "done": "claude-audit-done",
                    "blocked": "claude-audit-blocked",
                    "builder_label": "builder:claude",
                    "bot_authors": "github-actions[bot]",
                    "github_actions_workflows": ".github/workflows/local-cli-audit.yml",
                }
            ],
            labels={"claude-audit-done"},
            comments=[
                {
                    "body": bound_body.replace(
                        "claude-audit-blocked",
                        "claude-audit-done",
                    ),
                    "id": 1234,
                    "user": {"login": "github-actions[bot]"},
                }
            ],
            actions_runs={
                "12345": {
                    "path": ".github/workflows/local-cli-audit.yml",
                    "event": "pull_request_target",
                    "pull_requests": [
                        {
                            "number": 7,
                            "head": {"sha": "a" * 40},
                        }
                    ],
                }
            },
        )

        self.assertEqual(result["gate_state"], "pending")
        self.assertEqual(result["gate_description"], "waiting for audit: Claude")

    def test_gate_decision_rejects_empty_run_prs_without_commit_fallback(self) -> None:
        head_sha = "a" * 40
        result = self._run_gate_template_decision(
            lanes=[
                {
                    "id": "claude",
                    "display_name": "Claude",
                    "done": "claude-audit-done",
                    "blocked": "claude-audit-blocked",
                    "builder_label": "builder:claude",
                    "bot_authors": "github-actions[bot]",
                    "github_actions_workflows": ".github/workflows/local-cli-audit.yml",
                }
            ],
            labels={"claude-audit-done"},
            comments=[
                {
                    "body": self._bound_actions_body(
                        "Head SHA: `" + head_sha + "`\n"
                        "<!-- CODE_MOWER_AUDIT_RUN: run_id=12345 -->\n"
                        "<!-- CLAUDE_AUDIT_STATE: claude-audit-done -->"
                    ),
                    "id": 1234,
                    "user": {"login": "github-actions[bot]"},
                }
            ],
            head_sha=head_sha,
            actions_runs={
                "12345": {
                    "path": ".github/workflows/local-cli-audit.yml",
                    "event": "pull_request_target",
                    "head_sha": head_sha,
                    "pull_requests": [],
                }
            },
        )

        self.assertEqual(result["gate_state"], "pending")
        self.assertEqual(result["gate_description"], "waiting for audit: Claude")

    def test_gate_decision_accepts_empty_run_prs_with_commit_fallback(self) -> None:
        head_sha = "a" * 40
        result = self._run_gate_template_decision(
            lanes=[
                {
                    "id": "claude",
                    "display_name": "Claude",
                    "done": "claude-audit-done",
                    "blocked": "claude-audit-blocked",
                    "builder_label": "builder:claude",
                    "bot_authors": "github-actions[bot]",
                    "github_actions_workflows": ".github/workflows/local-cli-audit.yml",
                }
            ],
            labels={"claude-audit-done"},
            comments=[
                {
                    "body": self._bound_actions_body(
                        "Head SHA: `" + head_sha + "`\n"
                        "<!-- CODE_MOWER_AUDIT_RUN: run_id=12345 -->\n"
                        "<!-- CLAUDE_AUDIT_STATE: claude-audit-done -->"
                    ),
                    "id": 1234,
                    "user": {"login": "github-actions[bot]"},
                }
            ],
            head_sha=head_sha,
            actions_runs={
                "12345": {
                    "path": ".github/workflows/local-cli-audit.yml",
                    "event": "pull_request_target",
                    "head_sha": head_sha,
                    "pull_requests": [],
                }
            },
            commit_pull_requests={
                head_sha: [
                    {
                        "number": 7,
                        "head": {"sha": head_sha},
                    }
                ]
            },
        )

        self.assertEqual(result["gate_state"], "success")
        self.assertEqual(result["gate_description"], "Code Mower merge gate passed")

    def test_gate_decision_allows_owner_applied_gate_override(self) -> None:
        result = self._run_gate_template_decision(
            lanes=[
                {
                    "id": "claude",
                    "display_name": "Claude",
                    "done": "claude-audit-done",
                    "blocked": "claude-audit-blocked",
                    "builder_label": "builder:claude",
                    "bot_authors": "claude-audit-bot,claude-audit-bot[bot]",
                }
            ],
            labels={"gate:override", "claude-audit-blocked"},
            comments=[
                {
                    "body": "Head SHA: `" + ("a" * 40) + "`\n"
                    "<!-- CLAUDE_AUDIT_STATE: claude-audit-blocked -->",
                    "user": {"login": "claude-audit-bot"},
                }
            ],
            events=[
                {
                    "event": "committed",
                    "sha": "a" * 40,
                },
                {
                    "event": "labeled",
                    "label": {"name": "gate:override"},
                    "actor": {"login": "owner"},
                }
            ],
            owner_login="Owner",
        )

        self.assertEqual(result["gate_state"], "success")
        self.assertEqual(result["gate_description"], "owner gate override")

    def test_gate_decision_allows_owner_override_from_late_timeline_page(self) -> None:
        head_sha = "a" * 40
        event_pages = [
            [
                {
                    "event": "commented",
                    "created_at": f"2026-08-19T00:{page:02d}:{index % 60:02d}Z",
                }
                for index in range(100)
            ]
            for page in range(6)
        ]
        event_pages.append(
            [
                {
                    "event": "committed",
                    "sha": head_sha,
                    "created_at": "2026-08-19T16:00:00Z",
                },
                {
                    "event": "labeled",
                    "label": {"name": "gate:override"},
                    "actor": {"login": "owner"},
                    "created_at": "2026-08-19T16:01:00Z",
                },
            ]
        )

        result = self._run_gate_template_decision(
            lanes=[
                {
                    "id": "claude",
                    "display_name": "Claude",
                    "done": "claude-audit-done",
                    "blocked": "claude-audit-blocked",
                    "builder_label": "builder:claude",
                    "bot_authors": "claude-audit-bot,claude-audit-bot[bot]",
                }
            ],
            labels={"gate:override"},
            event_pages=event_pages,
            head_sha=head_sha,
            owner_login="owner",
        )

        self.assertEqual(result["gate_state"], "success")
        self.assertEqual(result["gate_description"], "owner gate override")

    def test_gate_decision_rejects_stale_owner_gate_override(self) -> None:
        result = self._run_gate_template_decision(
            lanes=[
                {
                    "id": "claude",
                    "display_name": "Claude",
                    "done": "claude-audit-done",
                    "blocked": "claude-audit-blocked",
                    "builder_label": "builder:claude",
                    "bot_authors": "claude-audit-bot,claude-audit-bot[bot]",
                }
            ],
            labels={"gate:override", "claude-audit-blocked"},
            comments=[
                {
                    "body": "Head SHA: `" + ("a" * 40) + "`\n"
                    "<!-- CLAUDE_AUDIT_STATE: claude-audit-blocked -->",
                    "user": {"login": "claude-audit-bot"},
                }
            ],
            events=[
                {
                    "event": "labeled",
                    "label": {"name": "gate:override"},
                    "actor": {"login": "owner"},
                },
                {
                    "event": "head_ref_force_pushed",
                    "after_commit": {"sha": "a" * 40},
                },
            ],
        )

        self.assertEqual(result["gate_state"], "failure")
        self.assertEqual(
            result["gate_description"],
            "gate:override is stale for current head",
        )

    def test_gate_decision_rejects_override_before_current_head_commit(self) -> None:
        result = self._run_gate_template_decision(
            lanes=[
                {
                    "id": "claude",
                    "display_name": "Claude",
                    "done": "claude-audit-done",
                    "blocked": "claude-audit-blocked",
                    "builder_label": "builder:claude",
                    "bot_authors": "claude-audit-bot,claude-audit-bot[bot]",
                }
            ],
            labels={"gate:override", "claude-audit-blocked"},
            comments=[
                {
                    "body": "Head SHA: `" + ("a" * 40) + "`\n"
                    "<!-- CLAUDE_AUDIT_STATE: claude-audit-blocked -->",
                    "user": {"login": "claude-audit-bot"},
                }
            ],
            events=[
                {
                    "event": "labeled",
                    "label": {"name": "gate:override"},
                    "actor": {"login": "owner"},
                },
                {
                    "event": "committed",
                    "sha": "a" * 40,
                },
            ],
        )

        self.assertEqual(result["gate_state"], "failure")
        self.assertEqual(
            result["gate_description"],
            "gate:override is stale for current head",
        )

    def test_gate_decision_fails_when_stale_override_clear_failed(self) -> None:
        result = self._run_gate_template_decision(
            lanes=[
                {
                    "id": "claude",
                    "display_name": "Claude",
                    "done": "claude-audit-done",
                    "blocked": "claude-audit-blocked",
                    "builder_label": "builder:claude",
                    "bot_authors": "claude-audit-bot,claude-audit-bot[bot]",
                }
            ],
            labels={"gate:override"},
            events=[
                {
                    "event": "committed",
                    "sha": "a" * 40,
                },
                {
                    "event": "labeled",
                    "label": {"name": "gate:override"},
                    "actor": {"login": "owner"},
                },
            ],
            env={"CODE_MOWER_GATE_OVERRIDE_CLEAR_FAILED": "true"},
        )

        self.assertEqual(result["gate_state"], "failure")
        self.assertEqual(
            result["gate_description"],
            "gate:override could not be cleared after head change",
        )

    def test_gate_decision_rejects_non_owner_gate_override(self) -> None:
        result = self._run_gate_template_decision(
            lanes=[
                {
                    "id": "claude",
                    "display_name": "Claude",
                    "done": "claude-audit-done",
                    "blocked": "claude-audit-blocked",
                    "builder_label": "builder:claude",
                    "bot_authors": "claude-audit-bot,claude-audit-bot[bot]",
                }
            ],
            labels={"gate:override"},
            events=[
                {
                    "event": "committed",
                    "sha": "a" * 40,
                },
                {
                    "event": "labeled",
                    "label": {"name": "gate:override"},
                    "actor": {"login": "contributor"},
                }
            ],
        )

        self.assertEqual(result["gate_state"], "failure")
        self.assertEqual(result["gate_description"], "gate:override was not owner-applied")

    def test_mirror_removal_plan_reports_product_support_files(self) -> None:
        from code_mower import migration

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            support_paths = [
                repo / "tools" / "code_mower",
                repo / "tools" / "code_mower_standalone_pin.env",
                repo / "tools" / "code_mower_standalone_shadow.sh",
                repo / "tools" / "run_codex_audit_pr.sh",
                repo / "tools" / "audit_labeler_lib.py",
                repo / "tools" / "decisions.py",
                repo / "tools" / "safe_gh_comment.py",
            ]
            for path in support_paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("", encoding="utf-8")

            payload = migration.render_mirror_removal_plan(
                repo_path=repo,
                shadow_cycles=0,
                required_shadow_cycles=1,
                standalone_default_cycles=1,
                required_standalone_default_cycles=1,
            )

        self.assertEqual(payload["status"], "mirrors_removed")
        self.assertEqual(payload["mirrored_file_count"], 0)
        self.assertIn("tools/audit_labeler_lib.py", payload["product_support_files"])
        self.assertIn("tools/decisions.py", payload["product_support_files"])
        self.assertIn("tools/run_codex_audit_pr.sh", payload["product_support_files"])
        self.assertIn("tools/safe_gh_comment.py", payload["product_support_files"])

    def test_init_apply_generates_real_reviewer_workflows(self) -> None:
        config_path = ROOT / "src/code_mower/templates/code-mower.example.yml"
        plan = code_mower_init.render_init_plan(
            code_mower_config.load_config(config_path),
            package_mode=True,
            package_command="code-mower",
        )
        self.assertIn("CODE_MOWER_GATE_HEALTH_RUNNER_TOKEN", plan.data["required_secrets"])
        self.assertIn("DISPATCH_TOKEN", plan.data["required_secrets"])
        self.assertIn("DISPATCH_TOKEN_EXPIRES_AT", plan.data["required_variables"])
        self.assertIn(
            "CODE_MOWER_LOCAL_AUDIT_RUNNER_ENABLED",
            plan.data["required_variables"],
        )
        self.assertEqual(plan.data["human_automation_token"]["secret"], "DISPATCH_TOKEN")
        self.assertEqual(plan.data["audit"]["max_diff_bytes"], 180_000)
        self.assertEqual(plan.data["audit"]["max_diff_hard_limit_bytes"], 1_500_000)
        self.assertEqual(plan.data["audit"]["budget_usd"], "")
        self.assertIn("builder:cursor", plan.data["labels"])
        self.assertNotIn("builder:grok-bot", plan.data["labels"])
        self.assertIn("require `code-mower/gate` from Any source", plan.text)
        self.assertIn("Required setup next steps:", plan.text)
        self.assertIn("create human automation token secret DISPATCH_TOKEN", plan.text)
        self.assertIn("allow_auto_merge=true", plan.text)
        self.assertIn("Audit limits:", plan.text)
        self.assertIn("app_id: 15368", plan.text)

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / ".code-mower.generated"
            result = code_mower_init.apply_init_plan(plan, output_dir)
            placeholder_files = {
                str(Path(path).relative_to(output_dir))
                for path in result["placeholder_files"]
                if Path(path).is_relative_to(output_dir)
            }
            expected = {
                ".github/workflows/audit-label-cleanup.yml",
                ".github/workflows/claude-audit-labeler.yml",
                ".github/workflows/claude-clear-stale.yml",
                ".github/workflows/code-mower-agent-pr-labeler.yml",
                ".github/workflows/code-mower-fix-round-dispatch.yml",
                ".github/workflows/code-mower-gate-health.yml",
                ".github/workflows/code-mower-gate.yml",
                ".github/workflows/codex-audit-labeler.yml",
                ".github/workflows/codex-clear-stale.yml",
                ".github/workflows/gitar-audit-labeler.yml",
                ".github/workflows/local-cli-audit.yml",
            }
            self.assertTrue(expected.isdisjoint(placeholder_files))
            required_variables = output_dir.joinpath("required-variables.txt").read_text(
                encoding="utf-8"
            )
            self.assertIn("DISPATCH_TOKEN_EXPIRES_AT", required_variables)
            self.assertIn("CODE_MOWER_LOCAL_AUDIT_RUNNER_ENABLED", required_variables)
            for rel_path in expected:
                text = output_dir.joinpath(rel_path).read_text(encoding="utf-8")
                self.assertNotIn("__WORKFLOW_NAME__", text)
                self.assertNotIn("{% raw %}", text)
                self.assertNotIn("{% endraw %}", text)
                self.assertNotIn("Install this generated template", text)

            claude = output_dir.joinpath(
                ".github/workflows/claude-audit-labeler.yml"
            ).read_text(encoding="utf-8")
            self.assertIn("tools/code_mower trailer-comment-labeler --lane", claude)
            self.assertIn("TRAILER_LANE: claude", claude)
            self.assertIn("claude-audit-bot[bot]", claude)
            self.assertIn("DISPATCH_TOKEN", claude)
            self.assertIn("CLAUDE_AUDIT_BOT_AUTHORS", claude)
            self.assertIn("actions: write", claude)
            self.assertIn('CODE_MOWER_GITHUB_ACTIONS_WORKFLOWS: ".github/workflows/local-cli-audit.yml"', claude)
            self.assertIn("gh workflow run code-mower-gate.yml", claude)
            self.assertIn("repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}", claude)
            self.assertIn('-f "head_sha=${head_sha}"', claude)
            self.assertNotIn("github.event.inputs.lane", claude)
            self.assertNotIn("github.event.comment.user.type == 'Bot'", claude)

            codex_stale = output_dir.joinpath(
                ".github/workflows/codex-clear-stale.yml"
            ).read_text(encoding="utf-8")
            self.assertIn("name: Code Mower Codex Clear Stale Audits", codex_stale)
            self.assertIn("tools/code_mower clear-stale", codex_stale)
            self.assertIn('default: "codex"', codex_stale)
            self.assertIn("github.event.inputs.lane || 'codex'", codex_stale)
            self.assertIn("code-mower-clear-stale-codex-${{", codex_stale)
            self.assertNotIn("code-mower-clear-stale-${{", codex_stale)

            claude_stale = output_dir.joinpath(
                ".github/workflows/claude-clear-stale.yml"
            ).read_text(encoding="utf-8")
            self.assertIn("name: Code Mower Claude Clear Stale Audits", claude_stale)
            self.assertIn("code-mower-clear-stale-claude-${{", claude_stale)
            self.assertNotIn("code-mower-clear-stale-${{", claude_stale)

            local_cli_audit = output_dir.joinpath(
                ".github/workflows/local-cli-audit.yml"
            ).read_text(encoding="utf-8")
            self.assertIn("pull_request_target:\n    types: [opened, synchronize, labeled]", local_cli_audit)
            self.assertIn("vars.CODE_MOWER_LOCAL_AUDIT_RUNNER_ENABLED == 'true'", local_cli_audit)
            self.assertIn("runs-on: [self-hosted, macOS, code-mower-audit]", local_cli_audit)
            self.assertIn("pull-requests: write", local_cli_audit)
            self.assertIn("Verify local runner environment", local_cli_audit)
            self.assertIn("for name in USER LOGNAME SHELL LANG", local_cli_audit)
            self.assertIn("CODE_MOWER_REVIEWER_SPEND_PATH", local_cli_audit)
            self.assertIn("Reset pull request checkout path", local_cli_audit)
            self.assertIn("rm -rf \"${PR_HEAD_PATH}\"", local_cli_audit)
            self.assertIn("concurrency:\n      group:", local_cli_audit)

            agent_labeler = output_dir.joinpath(
                ".github/workflows/code-mower-agent-pr-labeler.yml"
            ).read_text(encoding="utf-8")
            agent_env = yaml.safe_load(agent_labeler)["env"]
            agent_rules = json.loads(agent_env["CODE_MOWER_AGENT_PR_RULES_JSON"])
            self.assertIn("CODE_MOWER_AGENT_PR_RULES_JSON", agent_labeler)
            self.assertEqual(agent_rules[0]["branch_prefixes"], ["cursor/"])
            self.assertEqual(agent_rules[0]["builder_label"], "builder:cursor")
            self.assertEqual(
                agent_rules[0]["builder_labels"],
                ["builder:cursor", "builder:grok-bot"],
            )
            self.assertIn("secrets.DISPATCH_TOKEN", agent_labeler)
            self.assertIn("--remove-label", agent_labeler)
            self.assertIn("code_mower_backoff_rate_limit", agent_labeler)
            self.assertIn("code_mower_pr_view_labels_retry", agent_labeler)
            self.assertIn("code_mower_pr_edit_retry", agent_labeler)
            self.assertIn('grep -Fxq -- "${label}"', agent_labeler)
            self.assertIn('code_mower_pr_edit_retry "remove label ${label}" --remove-label "${label}"', agent_labeler)
            self.assertIn('code_mower_pr_edit_retry "add requested labels" "${add_args[@]}"', agent_labeler)
            self.assertIn("Retrying ${description} after rate-limit backoff", agent_labeler)
            self.assertNotIn('gh pr edit "${PR_NUMBER}" --repo "${GITHUB_REPOSITORY}" "${remove_args[@]}" >/dev/null 2>&1 || true', agent_labeler)
            self.assertIn("GitHub API rate limit hit", agent_labeler)
            self.assertIn("needs-codex-audit", agent_labeler)
            self.assertIn("needs-claude-audit", agent_labeler)
            self.assertNotIn("__AGENT_PR", agent_labeler)

            fix_round = output_dir.joinpath(
                ".github/workflows/code-mower-fix-round-dispatch.yml"
            ).read_text(encoding="utf-8")
            fix_env = yaml.safe_load(fix_round)["env"]
            fix_rules = json.loads(fix_env["CODE_MOWER_FIX_ROUND_RULES_JSON"])
            self.assertIn("CODE_MOWER_FIX_ROUND_RULES_JSON", fix_round)
            self.assertEqual(fix_rules[0]["mention"], "@cursor")
            self.assertEqual(fix_rules[0]["builder_label"], "builder:cursor")
            self.assertEqual(
                fix_rules[0]["builder_labels"],
                ["builder:cursor", "builder:grok-bot"],
            )
            self.assertIn("CODE_MOWER_FIX_ROUND:", fix_round)
            self.assertIn("secrets.DISPATCH_TOKEN", fix_round)
            self.assertEqual(fix_env["NEEDS_OWNER_LABEL"], "needs-owner")
            self.assertIn("codex-audit-blocked", fix_round)
            self.assertIn("claude-audit-blocked", fix_round)
            self.assertIn("code_mower_handle_rate_limit", fix_round)
            self.assertIn("GitHub API rate limit hit", fix_round)
            self.assertIn("comments_output", fix_round)
            self.assertNotIn("__FIX_ROUND", fix_round)
            self.assertIn("cancel-in-progress: true", local_cli_audit)
            self.assertIn("name: audit (${{ matrix.lane.lane }})", local_cli_audit)
            self.assertIn("strategy:\n      fail-fast: false", local_cli_audit)
            self.assertIn("path: code-mower-support", local_cli_audit)
            self.assertIn("path: pr-head", local_cli_audit)
            self.assertIn("fetch-depth: 0", local_cli_audit)
            self.assertIn("id: verify_pr_head", local_cli_audit)
            self.assertIn('export PATH="${CODE_MOWER_LOCAL_AUDIT_PATH}:${PATH}"', local_cli_audit)
            self.assertIn("if ! current_head=", local_cli_audit)
            self.assertIn('gh api "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}"', local_cli_audit)
            self.assertIn("Unable to verify current PR head via gh api", local_cli_audit)
            self.assertIn('echo "superseded=true" >> "${GITHUB_OUTPUT}"', local_cli_audit)
            self.assertIn(
                "steps.verify_pr_head.outputs.superseded != 'true'",
                local_cli_audit,
            )
            self.assertIn(
                "steps.verify_pr_head.conclusion == 'success'",
                local_cli_audit,
            )
            self.assertIn("github.event.pull_request.head.repo.full_name == github.repository", local_cli_audit)
            self.assertIn("CODE_MOWER_LOCAL_AUDIT_RUNNER_ENABLED", local_cli_audit)
            self.assertIn("github.event.action != 'labeled'", local_cli_audit)
            self.assertIn("needs-codex-audit", local_cli_audit)
            self.assertIn("needs-claude-audit", local_cli_audit)
            self.assertIn("DISPATCH_TOKEN", local_cli_audit)
            self.assertIn("CLAUDE_AUDIT_MAX_BUDGET_USD", local_cli_audit)
            self.assertIn("CLAUDE_AUDIT_MAX_DIFF_BYTES", local_cli_audit)
            self.assertIn("CLAUDE_AUDIT_MAX_DIFF_HARD_LIMIT_BYTES", local_cli_audit)
            self.assertIn("CODEX_AUDIT_MAX_BUDGET_USD", local_cli_audit)
            self.assertIn("CODEX_AUDIT_MAX_DIFF_BYTES", local_cli_audit)
            self.assertIn("CODEX_AUDIT_MAX_DIFF_HARD_LIMIT_BYTES", local_cli_audit)
            self.assertNotIn("CODEX_AUDIT_LABEL_TOKEN", local_cli_audit)
            self.assertNotIn("CLAUDE_AUDIT_LABEL_TOKEN", local_cli_audit)
            self.assertIn("tools/run_codex_audit_pr.sh", local_cli_audit)
            self.assertIn("tools/run_claude_audit_pr.sh", local_cli_audit)
            self.assertIn("--read-token-from-stdin", local_cli_audit)
            self.assertIn("--repo-paths", local_cli_audit)
            self.assertIn("PR_HEAD_PATH was checked out and verified", local_cli_audit)
            self.assertIn("skips the private pull/N/head", local_cli_audit)
            self.assertIn("Upload Code Mower audit metadata", local_cli_audit)
            self.assertIn("always() &&", local_cli_audit)
            self.assertIn("secrets.CODE_MOWER_CLOUD_TOKEN", local_cli_audit)
            self.assertIn("CODE_MOWER_CLOUD_TOKEN is not configured", local_cli_audit)
            self.assertIn("cloud reviewer-runs", local_cli_audit)
            self.assertIn("cloud dogfood", local_cli_audit)
            self.assertIn(
                '"${SUPPORT_PATH}/tools/code_mower" cloud reviewer-runs \\\n'
                '            --repo-path "${PR_HEAD_PATH}"',
                local_cli_audit,
            )
            self.assertIn(
                '--verdicts "${verdict_dir}" \\\n'
                '            --spend "${CODE_MOWER_REVIEWER_SPEND_PATH}"',
                local_cli_audit,
            )
            self.assertIn(
                '"${SUPPORT_PATH}/tools/code_mower" cloud dogfood \\\n'
                '            --repo-path "${SUPPORT_PATH}"',
                local_cli_audit,
            )
            self.assertIn("--spend \"${CODE_MOWER_REVIEWER_SPEND_PATH}\"", local_cli_audit)
            self.assertNotIn('--source local-cli-audit \\\n            --spend', local_cli_audit)
            self.assertIn("--event \"work_order=${event_file}\"", local_cli_audit)
            self.assertNotIn("__LOCAL_AUDIT_", local_cli_audit)
            parsed_local_cli = yaml.safe_load(local_cli_audit)
            audit_job = parsed_local_cli["jobs"]["audit"]
            self.assertNotIn("CODE_MOWER_REVIEWER_SPEND_PATH", audit_job["env"])
            run_step = next(
                step
                for step in audit_job["steps"]
                if step.get("name") == "Run matching local CLI audits"
            )
            upload_step = next(
                step
                for step in audit_job["steps"]
                if step.get("name") == "Upload Code Mower audit metadata"
            )
            self.assertEqual(
                run_step["env"]["CODE_MOWER_REVIEWER_SPEND_PATH"],
                "${{ runner.temp }}/code-mower-reviewer-spend.json",
            )
            self.assertEqual(
                upload_step["env"]["CODE_MOWER_REVIEWER_SPEND_PATH"],
                "${{ runner.temp }}/code-mower-reviewer-spend.json",
            )
            self.assertIn(
                "github.event.pull_request.head.repo.full_name == github.repository",
                audit_job["if"],
            )
            self.assertIn(
                "${{ github.event.pull_request.head.sha }}",
                audit_job["concurrency"]["group"],
            )
            self.assertIn(
                "${{ matrix.lane.lane }}",
                audit_job["concurrency"]["group"],
            )
            self.assertTrue(audit_job["concurrency"]["cancel-in-progress"])
            self.assertIn("lane", audit_job["strategy"]["matrix"])
            first_lane = audit_job["strategy"]["matrix"]["lane"][0]
            self.assertEqual(first_lane["audit_budget_usd"], "")
            self.assertEqual(first_lane["audit_max_diff_bytes"], "180000")
            self.assertEqual(first_lane["audit_max_diff_hard_limit_bytes"], "1500000")
            run_step = next(
                step
                for step in audit_job["steps"]
                if step["name"] == "Run matching local CLI audits"
            )
            self.assertEqual(run_step["id"], "run_audit")
            self.assertEqual(
                run_step["env"]["CLAUDE_AUDIT_MAX_BUDGET_USD"],
                "${{ matrix.lane.audit_budget_usd }}",
            )
            self.assertEqual(
                run_step["env"]["CODEX_AUDIT_MAX_DIFF_HARD_LIMIT_BYTES"],
                "${{ matrix.lane.audit_max_diff_hard_limit_bytes }}",
            )
            self.assertIn("audited=false", run_step["run"])
            self.assertIn("audited=true", run_step["run"])
            self.assertIn(
                "steps.run_audit.outputs.audited == 'true'",
                upload_step["if"],
            )

            gate = output_dir.joinpath(
                ".github/workflows/code-mower-gate.yml"
            ).read_text(encoding="utf-8")
            gate_env = yaml.safe_load(gate)["env"]
            gate_lanes = json.loads(gate_env["CODE_MOWER_GATE_LANES_JSON"])
            self.assertIn("CODE_MOWER_GATE_CONTEXT: code-mower/gate", gate)
            self.assertIn("name: publish Code Mower gate status", gate)
            self.assertNotIn("name: code-mower/gate", gate)
            self.assertIn("Check out Code Mower support files", gate)
            self.assertIn('CODE_MOWER_OWNER_LABEL: "needs-owner"', gate)
            self.assertIn("codex-audit-done", gate)
            self.assertIn("claude-audit-done", gate)
            self.assertIn("builder:codex", gate)
            self.assertIn("builder:claude", gate)
            self.assertIn("enablePullRequestAutoMerge", gate)
            self.assertIn("CODE_MOWER_GATE_AUTOMERGE_TOKEN", gate)
            self.assertIn("secrets.CODE_MOWER_GATE_AUTOMERGE_TOKEN", gate)
            self.assertIn("secrets.DISPATCH_TOKEN", gate)
            self.assertIn('automerge_token="${CODE_MOWER_GATE_AUTOMERGE_TOKEN:-${GH_TOKEN:-}}"', gate)
            self.assertIn('GH_TOKEN="${automerge_token}" gh api graphql', gate)
            self.assertNotIn("enablePullRequestAutoMerge(input:{pullRequestId:$pullRequestId,mergeMethod:SQUASH}){pullRequest{id}}}' >/dev/null || {", gate)
            self.assertIn("gh api -X POST", gate)
            self.assertIn("CODE_MOWER_AUDIT_RUN", gate)
            self.assertIn("comment_id", gate)
            self.assertIn("body_sha256", gate)
            self.assertIn("  pull_request_target:", gate)
            self.assertNotIn("  pull_request:\n", gate)
            self.assertIn("github.event_name == 'pull_request_target'", gate)
            self.assertNotIn("github.event_name == 'pull_request' &&", gate)
            self.assertIn("Clear stale Code Mower gate override", gate)
            self.assertIn("github_actions_comment_attested", gate)
            self.assertIn("github_actions_workflows", gate)
            self.assertIn("CLAUDE_AUDIT_BOT_AUTHORS: ${{ vars.CLAUDE_AUDIT_BOT_AUTHORS || '' }}", gate)
            self.assertIn("CODEX_BOT_AUTHORS: ${{ vars.CODEX_BOT_AUTHORS || '' }}", gate)
            self.assertIn(".github/workflows/local-cli-audit.yml", gate)
            self.assertIn("CODE_MOWER_GATE_OVERRIDE_LABEL", gate)
            self.assertIn("issues/${PR_NUMBER}/timeline?per_page=100", gate)
            self.assertIn("gate:override", gate)
            self.assertIn("was not owner-applied", gate)
            self.assertIn("is stale for current head", gate)
            self.assertIn("owner gate override", gate)
            self.assertIn("audit in flight", gate)
            self.assertIn("workflow_run:", gate)
            self.assertIn("pending/failure verdicts", gate)
            self.assertIn('workflows: ["Code Mower Local CLI Audits"]', gate)
            self.assertIn("github.event.workflow_run.pull_requests[0].number", gate)
            self.assertIn("WORKFLOW_RUN_HEAD_SHA", gate)
            self.assertIn("commits/${WORKFLOW_RUN_HEAD_SHA}/pulls?per_page=100", gate)
            self.assertIn("WORKFLOW_RUN_HEAD_BRANCH", gate)
            self.assertIn("--method GET", gate)
            self.assertIn('gh api "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}"', gate)
            self.assertIn("export HEAD_SHA", gate)
            self.assertNotIn("HEAD_SHA: ${{ github.event.pull_request.head.sha || github.event.inputs.head_sha || github.event.workflow_run.head_sha }}", gate)
            self.assertIn("commits may have been dropped", gate)
            self.assertIn(
                "actions/runs?event=pull_request_target&status={status}&per_page=100",
                gate,
            )
            self.assertTrue(all(lane["decision_coverage"] for lane in gate_lanes))
            self.assertNotIn("__GATE_LANES_JSON__", gate)

            gate_health = output_dir.joinpath(
                ".github/workflows/code-mower-gate-health.yml"
            ).read_text(encoding="utf-8")
            gate_health_env = yaml.safe_load(gate_health)["env"]
            gate_health_lanes = json.loads(
                gate_health_env["CODE_MOWER_GATE_HEALTH_LANES_JSON"]
            )
            self.assertIn("CODE_MOWER_GATE_HEALTH_LANES_JSON", gate_health)
            self.assertIn("CODE_MOWER_GATE_HEALTH_MAX_WAIT_MINUTES", gate_health)
            self.assertIn("CODE_MOWER_GATE_HEALTH_LIVENESS_MINUTES", gate_health)
            self.assertEqual(gate_health_env["NEEDS_OWNER_LABEL"], "needs-owner")
            self.assertIn("needs-codex-audit", gate_health)
            self.assertIn("needs-claude-audit", gate_health)
            codex_health = next(
                lane for lane in gate_health_lanes if lane["author_lane"] == "codex"
            )
            self.assertEqual(
                codex_health["builder_authors"],
                "chatgpt-codex-connector[bot]",
            )
            self.assertEqual(codex_health["builder_label"], "builder:codex")
            self.assertIn("github-actions[bot]", gate_health)
            self.assertIn("CLAUDE_AUDIT_BOT_AUTHORS: ${{ vars.CLAUDE_AUDIT_BOT_AUTHORS || '' }}", gate_health)
            self.assertIn("CODEX_BOT_AUTHORS: ${{ vars.CODEX_BOT_AUTHORS || '' }}", gate_health)
            self.assertIn("tools/code_mower gate-health", gate_health)
            self.assertIn("CODE_MOWER_GATE_HEALTH_RUNNER_TOKEN", gate_health)
            self.assertIn("github_actions_workflows", gate_health)
            self.assertNotIn("__GATE_HEALTH", gate_health)

            gitar = output_dir.joinpath(
                ".github/workflows/gitar-audit-labeler.yml"
            ).read_text(encoding="utf-8")
            self.assertIn("tools/code_mower saas-reviewer-labeler --adapter", gitar)
            self.assertIn("REVIEWER_ADAPTER: gitar", gitar)
            self.assertIn("gitar-bot[bot]", gitar)
            self.assertIn("GITAR_AUDIT_LABEL_TOKEN", gitar)
            self.assertIn("GITAR_BOT_AUTHORS", gitar)
            self.assertNotIn("github.event.inputs.adapter", gitar)
            self.assertNotIn("github.event.comment.user.type == 'Bot'", gitar)
            self.assertTrue(
                any(
                    "github-actions[bot] requires an explicit *_BOT_AUTHORS" in warning
                    for warning in plan.data["warnings"]
                )
            )

    def test_init_warns_for_agent_pr_builder_identity_mismatches(self) -> None:
        config_path = ROOT / "src/code_mower/templates/code-mower.example.yml"
        config = copy.deepcopy(code_mower_config.load_config(config_path))
        config["builder_identity"]["labels"]["builder:grok-alt"] = "grok-bot"
        config["builder_identity"]["branch_prefixes"]["cursr/"] = "grok-boot"
        config["builder_identity"]["fix_round_mentions"]["grok-boot"] = "@cursor"

        plan = code_mower_init.render_init_plan(
            config,
            package_mode=True,
            package_command="code-mower",
        )

        warnings = "\n".join(plan.data["warnings"])
        self.assertIn("maps multiple builder labels", warnings)
        self.assertIn("lane 'grok-boot' has no matching builder label", warnings)

    def test_init_apply_generates_owner_surface_templates(self) -> None:
        config_path = ROOT / "src/code_mower/templates/code-mower.example.yml"
        config = dict(code_mower_config.load_config(config_path))
        config["owner_surface"] = {
            "owner_login": "jeffhuber",
            "needs_owner_label": "needs-jeff",
            "owner_decision_label": "owner-decision",
            "owner_sitting_label": "owner-sitting",
            "gate_override_label": "gate:override",
            "status_issue": "6",
            "weekly_cron": "15 12 * * 1",
            "gate_health_cron": "*/15 * * * *",
            "gate_health_max_wait_minutes": "45",
            "gate_health_liveness_minutes": "60",
            "local_audit_runner_label": "bridge-pro-audit",
            "ready_label": "tier:R",
            "phase_labels": ["phase:0", "phase:1"],
            "reviewer_spend_path": ".code-mower/reviewer-spend.json",
            "reviewer_value_report_path": ".code-mower/reviewer-value-report.md",
        }
        plan = code_mower_init.render_init_plan(
            config,
            package_mode=True,
            package_command="code-mower",
        )

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / ".code-mower.generated"
            result = code_mower_init.apply_init_plan(plan, output_dir)
            generated = {
                ".github/workflows/code-mower-gate-health.yml",
                ".github/workflows/needs-owner-notify.yml",
                ".github/workflows/weekly-status.yml",
                "tools/status_report.py",
            }
            written = {
                str(Path(path).relative_to(output_dir))
                for path in result["written_files"]
                if Path(path).is_relative_to(output_dir)
            }
            placeholder_files = {
                str(Path(path).relative_to(output_dir))
                for path in result["placeholder_files"]
                if Path(path).is_relative_to(output_dir)
            }
            self.assertTrue(generated.issubset(written))
            self.assertTrue(generated.isdisjoint(placeholder_files))
            self.assertIn("needs-jeff", plan.data["labels"])
            self.assertIn("owner-decision", plan.data["labels"])
            self.assertIn("owner-sitting", plan.data["labels"])
            self.assertIn("gate:override", plan.data["labels"])

            notify = output_dir.joinpath(
                ".github/workflows/needs-owner-notify.yml"
            ).read_text(encoding="utf-8")
            parsed_notify = yaml.safe_load(notify)
            self.assertIn('NEEDS_OWNER_LABEL: "needs-jeff"', notify)
            self.assertIn('OWNER_DECISION_LABEL: "owner-decision"', notify)
            self.assertIn('OWNER_SITTING_LABEL: "owner-sitting"', notify)
            self.assertIn('OWNER_LOGIN: "jeffhuber"', notify)
            self.assertEqual(parsed_notify["env"]["NEEDS_OWNER_LABEL"], "needs-jeff")
            self.assertEqual(parsed_notify["env"]["OWNER_DECISION_LABEL"], "owner-decision")
            self.assertEqual(parsed_notify["env"]["OWNER_SITTING_LABEL"], "owner-sitting")
            self.assertEqual(parsed_notify["env"]["GATE_OVERRIDE_LABEL"], "gate:override")
            notify_step = parsed_notify["jobs"]["notify"]["steps"][0]
            self.assertEqual(
                notify_step["if"],
                "github.event.label.name == env.NEEDS_OWNER_LABEL || "
                "github.event.label.name == env.OWNER_DECISION_LABEL || "
                "github.event.label.name == env.OWNER_SITTING_LABEL || "
                "github.event.label.name == env.GATE_OVERRIDE_LABEL",
            )
            self.assertNotIn("github.event.label.name == 'needs-jeff'", notify)
            self.assertNotIn("github.event.label.name == 'gate:override'", notify)
            self.assertIn(
                'gh api -X POST "repos/${REPO}/issues/${NUM}/assignees"',
                notify,
            )
            self.assertIn(
                'gh api -X POST "repos/${REPO}/issues/${NUM}/comments"',
                notify,
            )
            self.assertNotIn('gh api -X PATCH "repos/${REPO}/issues/${NUM}"', notify)
            self.assertNotIn("gh issue comment", notify)
            self.assertIn("Options, Recommendation, and Existing decision touched", notify)
            self.assertIn("orchestration lane may resolve it here", notify)
            self.assertNotIn("__NEEDS_OWNER_LABEL__", notify)
            self.assertNotIn("{% raw %}", notify)

            weekly = output_dir.joinpath(
                ".github/workflows/weekly-status.yml"
            ).read_text(encoding="utf-8")
            self.assertIn('cron: "15 12 * * 1"', weekly)
            self.assertIn('STATUS_ISSUE: "6"', weekly)
            self.assertIn('NEEDS_OWNER_LABEL: "needs-jeff"', weekly)
            self.assertIn('OWNER_DECISION_LABEL: "owner-decision"', weekly)
            self.assertIn('OWNER_SITTING_LABEL: "owner-sitting"', weekly)
            self.assertIn('PHASE_LABELS: "phase:0,phase:1"', weekly)
            self.assertIn("python3 tools/status_report.py", weekly)
            self.assertIn(
                "actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10",
                weekly,
            )
            self.assertIn(
                "actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405",
                weekly,
            )
            self.assertNotIn("actions/checkout@v6", weekly)
            self.assertNotIn("actions/setup-python@v6", weekly)
            self.assertNotIn('STATUS_ISSUE: "TODO_STATUS_ISSUE"', weekly)
            self.assertNotIn("{% raw %}", weekly)

            gate_health = output_dir.joinpath(
                ".github/workflows/code-mower-gate-health.yml"
            ).read_text(encoding="utf-8")
            self.assertIn('NEEDS_OWNER_LABEL: "needs-jeff"', gate_health)
            self.assertIn('OWNER_LOGIN: "jeffhuber"', gate_health)
            self.assertIn('STATUS_ISSUE: "6"', gate_health)
            self.assertIn('cron: "*/15 * * * *"', gate_health)
            self.assertIn('CODE_MOWER_GATE_HEALTH_MAX_WAIT_MINUTES: ${{ github.event.inputs.max_wait_minutes || \'45\' }}', gate_health)
            self.assertIn('CODE_MOWER_GATE_HEALTH_LIVENESS_MINUTES: ${{ github.event.inputs.liveness_minutes || \'60\' }}', gate_health)
            self.assertIn('CODE_MOWER_LOCAL_AUDIT_RUNNER_LABEL: "bridge-pro-audit"', gate_health)
            self.assertIn("tools/code_mower gate-health", gate_health)
            self.assertIn("needs-codex-audit", gate_health)
            self.assertIn("github-actions[bot]", gate_health)
            self.assertIn("CLAUDE_AUDIT_BOT_AUTHORS: ${{ vars.CLAUDE_AUDIT_BOT_AUTHORS || '' }}", gate_health)
            self.assertNotIn("__GATE_HEALTH", gate_health)

            gate = output_dir.joinpath(
                ".github/workflows/code-mower-gate.yml"
            ).read_text(encoding="utf-8")
            self.assertIn('CODE_MOWER_OWNER_LABEL: "needs-jeff"', gate)
            self.assertIn('CODE_MOWER_OWNER_SITTING_LABEL: "owner-sitting"', gate)
            self.assertIn('CODE_MOWER_OWNER_LOGIN: "jeffhuber"', gate)
            self.assertIn('CODE_MOWER_GATE_OVERRIDE_LABEL: "gate:override"', gate)
            self.assertIn('CODE_MOWER_DECISION_AUTHORITIES: "jeffhuber"', gate)
            self.assertNotIn("permission=admin", gate)
            self.assertIn("override_actor != override_owner", gate)
            self.assertIn("Clear stale Code Mower gate override", gate)
            self.assertIn("CODE_MOWER_GATE_OVERRIDE_CLEAR_FAILED=true", gate)
            self.assertNotIn('CODE_MOWER_OWNER_LABEL: "needs-owner"', gate)
            codex_labeler = output_dir.joinpath(
                ".github/workflows/codex-audit-labeler.yml"
            ).read_text(encoding="utf-8")
            self.assertIn(
                'CODE_MOWER_DECISION_AUTHORITIES: "jeffhuber"',
                codex_labeler,
            )

            config["owner_surface"]["needs_owner_label"] = "needs: jeff # owner"
            special_plan = code_mower_init.render_init_plan(
                config,
                package_mode=True,
                package_command="code-mower",
            )
            special_output_dir = Path(tmp) / ".code-mower.generated-special"
            code_mower_init.apply_init_plan(special_plan, special_output_dir)
            special_gate = special_output_dir.joinpath(
                ".github/workflows/code-mower-gate.yml"
            ).read_text(encoding="utf-8")
            parsed_gate = yaml.safe_load(special_gate)
            self.assertEqual(
                parsed_gate["env"]["CODE_MOWER_OWNER_LABEL"],
                "needs: jeff # owner",
            )
            self.assertEqual(
                parsed_gate["env"]["CODE_MOWER_OWNER_SITTING_LABEL"],
                "owner-sitting",
            )
            self.assertEqual(
                parsed_gate["env"]["CODE_MOWER_OWNER_LOGIN"],
                "jeffhuber",
            )
            self.assertEqual(
                parsed_gate["env"]["CODE_MOWER_GATE_OVERRIDE_LABEL"],
                "gate:override",
            )
            special_notify = special_output_dir.joinpath(
                ".github/workflows/needs-owner-notify.yml"
            ).read_text(encoding="utf-8")
            parsed_special_notify = yaml.safe_load(special_notify)
            self.assertEqual(
                parsed_special_notify["env"]["NEEDS_OWNER_LABEL"],
                "needs: jeff # owner",
            )
            self.assertEqual(
                parsed_special_notify["env"]["OWNER_DECISION_LABEL"],
                "owner-decision",
            )
            self.assertEqual(
                parsed_special_notify["env"]["OWNER_SITTING_LABEL"],
                "owner-sitting",
            )
            self.assertEqual(
                parsed_special_notify["jobs"]["notify"]["steps"][0]["if"],
                "github.event.label.name == env.NEEDS_OWNER_LABEL || "
                "github.event.label.name == env.OWNER_DECISION_LABEL || "
                "github.event.label.name == env.OWNER_SITTING_LABEL || "
                "github.event.label.name == env.GATE_OVERRIDE_LABEL",
            )

            config["owner_surface"]["gate_override_label"] = ""
            disabled_override_plan = code_mower_init.render_init_plan(
                config,
                package_mode=True,
                package_command="code-mower",
            )
            self.assertNotIn("gate:override", disabled_override_plan.data["labels"])
            disabled_override_output_dir = (
                Path(tmp) / ".code-mower.generated-disabled-override"
            )
            code_mower_init.apply_init_plan(
                disabled_override_plan,
                disabled_override_output_dir,
            )
            disabled_override_gate = disabled_override_output_dir.joinpath(
                ".github/workflows/code-mower-gate.yml"
            ).read_text(encoding="utf-8")
            parsed_disabled_override_gate = yaml.safe_load(disabled_override_gate)
            self.assertEqual(
                parsed_disabled_override_gate["env"]["CODE_MOWER_GATE_OVERRIDE_LABEL"],
                "",
            )

            disabled_override_notify = disabled_override_output_dir.joinpath(
                ".github/workflows/needs-owner-notify.yml"
            ).read_text(encoding="utf-8")
            parsed_disabled_override_notify = yaml.safe_load(disabled_override_notify)
            self.assertEqual(
                parsed_disabled_override_notify["env"]["GATE_OVERRIDE_LABEL"],
                "",
            )
            self.assertEqual(
                parsed_disabled_override_notify["jobs"]["notify"]["steps"][0]["if"],
                "github.event.label.name == env.NEEDS_OWNER_LABEL || "
                "github.event.label.name == env.OWNER_DECISION_LABEL || "
                "github.event.label.name == env.OWNER_SITTING_LABEL || "
                "github.event.label.name == env.GATE_OVERRIDE_LABEL",
            )

            status_report = output_dir.joinpath("tools/status_report.py")
            self.assertTrue(status_report.stat().st_mode & 0o111)
            self.assertIn(
                "metadata only",
                status_report.read_text(encoding="utf-8"),
            )
            subprocess.run(
                [sys.executable, "-m", "py_compile", str(status_report)],
                check=True,
                text=True,
            )

    def test_init_renders_valid_decision_authorities_in_workflow_env(self) -> None:
        config_path = ROOT / "src/code_mower/templates/code-mower.example.yml"
        config = dict(code_mower_config.load_config(config_path))
        config["owner_surface"] = {"owner_login": "owner"}
        config["decisions"] = {
            "authorities": [
                "valid-maintainer",
                "ci-bot[bot]",
            ],
        }
        plan = code_mower_init.render_init_plan(
            config,
            package_mode=True,
            package_command="code-mower",
        )
        expected = "owner,valid-maintainer,ci-bot[bot]"

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / ".code-mower.generated"
            code_mower_init.apply_init_plan(plan, output_dir)
            gate = output_dir.joinpath(
                ".github/workflows/code-mower-gate.yml"
            ).read_text(encoding="utf-8")
            codex_labeler = output_dir.joinpath(
                ".github/workflows/codex-audit-labeler.yml"
            ).read_text(encoding="utf-8")

        self.assertEqual(
            yaml.safe_load(gate)["env"]["CODE_MOWER_DECISION_AUTHORITIES"],
            expected,
        )
        self.assertEqual(
            yaml.safe_load(codex_labeler)["env"]["CODE_MOWER_DECISION_AUTHORITIES"],
            expected,
        )

    def test_safe_gh_comment_reports_gh_streams_on_failure(self) -> None:
        module_path = ROOT / "src/code_mower/templates/product-support/safe_gh_comment.py"
        spec = importlib.util.spec_from_file_location("safe_gh_comment_template", module_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        exc = subprocess.CalledProcessError(
            1,
            ["gh", "api"],
            output="response body\n",
            stderr="permission denied\n",
        )

        formatted = module._format_error(exc)
        self.assertIn("error: Command '['gh', 'api']' returned non-zero exit status 1.", formatted)
        self.assertIn("stderr:\npermission denied", formatted)
        self.assertIn("stdout:\nresponse body", formatted)

    def test_gate_health_skips_runner_check_without_local_cli_lanes(self) -> None:
        config_path = ROOT / "src/code_mower/templates/code-mower.example.yml"
        config = copy.deepcopy(code_mower_config.load_config(config_path))
        config["profiles"]["hosted-only-audit"] = {
            "description": "Hosted audit lane without a local runner.",
            "lanes": ["gitar"],
        }
        plan = code_mower_init.render_init_plan(
            config,
            profile_id="hosted-only-audit",
            package_mode=True,
            package_command="code-mower",
        )
        self.assertNotIn(
            "CODE_MOWER_GATE_HEALTH_RUNNER_TOKEN",
            plan.data["required_secrets"],
        )
        gate_health_entry = next(
            entry
            for entry in plan.data["generated_files"]
            if entry["path"] == ".github/workflows/code-mower-gate-health.yml"
        )
        self.assertEqual(gate_health_entry["local_audit_runner_label"], "")

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / ".code-mower.generated"
            code_mower_init.apply_init_plan(plan, output_dir)
            gate_health = output_dir.joinpath(
                ".github/workflows/code-mower-gate-health.yml"
            ).read_text(encoding="utf-8")

        self.assertIn('CODE_MOWER_LOCAL_AUDIT_RUNNER_LABEL: ""', gate_health)
        self.assertIn("needs-gitar-audit", gate_health)

    def test_status_report_template_summarizes_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_gh = root / "gh"
            fake_gh.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
cmd="$1 $2"
state=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--state" ]; then state="$2"; fi
  shift || true
done
if [ "$cmd" = "issue list" ] && [ "$state" = "open" ]; then
  printf '%s\\n' '[{"number":1,"title":"Raw needs owner","labels":[{"name":"needs-jeff"},{"name":"phase:0"}],"assignees":[]},{"number":2,"title":"Ready","labels":[{"name":"tier:R"},{"name":"phase:0"}],"assignees":[]},{"number":5,"title":"Owner sitting","labels":[{"name":"owner-sitting"},{"name":"tier:R"},{"name":"phase:0"}],"assignees":[]},{"number":7,"title":"Owner decision","labels":[{"name":"needs-jeff"},{"name":"owner-decision"},{"name":"phase:0"}],"assignees":[]}]'
elif [ "$cmd" = "issue list" ] && [ "$state" = "closed" ]; then
  printf '%s\\n' '[{"number":4,"title":"Done","labels":[{"name":"phase:0"}],"closedAt":"2099-01-01T00:00:00Z","assignees":[]}]'
elif [ "$cmd" = "pr list" ] && [ "$state" = "open" ]; then
  printf '%s\\n' '[{"number":3,"title":"Builder PR","labels":[{"name":"builder:codex"},{"name":"needs-codex-audit"},{"name":"needs-jeff"}],"author":{"login":"builder"},"isDraft":false},{"number":6,"title":"Owner sitting PR","labels":[{"name":"builder:claude"},{"name":"owner-sitting"}],"author":{"login":"builder"},"isDraft":false},{"number":8,"title":"Owner decision PR","labels":[{"name":"builder:claude"},{"name":"needs-jeff"},{"name":"owner-decision"}],"author":{"login":"builder"},"isDraft":false}]'
elif [ "$cmd" = "pr list" ] && [ "$state" = "merged" ]; then
  printf '%s\\n' '[]'
else
  printf '%s\\n' '[]'
fi
""",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)
            spend_dir = root / ".code-mower"
            spend_dir.mkdir()
            (spend_dir / "reviewer-spend.json").write_text(
                json.dumps(
                    {
                        "runs": [
                            {
                                "lane": "codex",
                                "wall_seconds": 12,
                                "tokens": {"total": 34},
                                "cost_usd": 0.056,
                                "verdict": "pass",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "src/code_mower/templates/product-support/status_report.py"),
                ],
                cwd=root,
                env={
                    **os.environ,
                    "PATH": f"{root}{os.pathsep}{os.environ['PATH']}",
                    "REPO": "owner/repo",
                    "NEEDS_OWNER_LABEL": "needs-jeff",
                    "OWNER_DECISION_LABEL": "owner-decision",
                    "OWNER_SITTING_LABEL": "owner-sitting",
                    "READY_LABEL": "tier:R",
                    "PHASE_LABELS": "phase:0",
                },
                text=True,
                capture_output=True,
                check=True,
            )

        self.assertIn("## Needs Owner (needs-jeff)", completed.stdout)
        self.assertIn("`owner-decision` decisions that survived triage", completed.stdout)
        self.assertIn("`owner-sitting` physical-step sittings", completed.stdout)
        owner_section = completed.stdout.split("## Needs Owner", 1)[1].split(
            "## Open PRs",
            1,
        )[0]
        self.assertNotIn("- issue #1 Raw needs owner", owner_section)
        self.assertIn("- issue #5 Owner sitting", owner_section)
        self.assertIn("- issue #7 Owner decision", owner_section)
        self.assertNotIn("- PR #3 Builder PR", owner_section)
        self.assertIn("- PR #6 Owner sitting PR", owner_section)
        self.assertIn("- PR #8 Owner decision PR", owner_section)
        open_pr_section = completed.stdout.split("## Open PRs", 1)[1].split(
            "## Merged In The Last 7 Days",
            1,
        )[0]
        self.assertIn("- #3 Builder PR", open_pr_section)
        ready_section = completed.stdout.split("## Ready Queue", 1)[1].split("## Reviewer Spend", 1)[0]
        self.assertIn("- issue #2 Ready", ready_section)
        self.assertNotIn("Raw needs owner", ready_section)
        self.assertNotIn("Owner decision", ready_section)
        self.assertNotIn("Owner sitting", ready_section)
        self.assertIn("codex:pending", completed.stdout)
        self.assertIn("| codex | 1 | 12 | 34 | 0.0560 | pass |", completed.stdout)
        self.assertNotIn("body", completed.stdout.lower())

    def test_init_add_repo_records_sibling_repo_targets(self) -> None:
        config_path = ROOT / "src/code_mower/templates/code-mower.example.yml"
        config, added_repos = code_mower_init.config_with_added_repositories(
            code_mower_config.load_config(config_path),
            ("owner/sibling-one", "owner/sibling-two"),
        )
        plan = code_mower_init.render_init_plan(
            config,
            package_mode=True,
            package_command="code-mower",
            add_repositories=added_repos,
        )

        self.assertEqual(added_repos, ("owner/sibling-one", "owner/sibling-two"))
        self.assertIn("owner/sibling-one", plan.text)
        self.assertIn("Additional repository targets from --add-repo", plan.text)
        self.assertTrue(
            any(
                "--add-repo owner/sibling-one" in test
                for test in plan.data["smoke_tests"]
            )
        )
        self.assertEqual(
            plan.data["additional_repositories"],
            ["owner/sibling-one", "owner/sibling-two"],
        )
        self.assertIn(
            "owner/sibling-two",
            [repo["slug"] for repo in plan.data["repositories"]],
        )

    def test_init_add_repo_rejects_non_owner_repo_slug(self) -> None:
        with self.assertRaisesRegex(
            code_mower_config.ConfigError,
            "OWNER/REPO",
        ):
            code_mower_init.config_with_added_repositories(
                {"repositories": []},
                ("not/a/slug",),
            )

    def test_init_apply_generates_devin_bridge_labeler_and_stale_workflow(self) -> None:
        devin_config = {
            "version": 1,
            "project": {
                "name": "devin-generated-workflow-test",
                "state_dir": "~/.cache/code-mower",
            },
            "repositories": [{"slug": "owner/repo", "default_branch": "main"}],
            "lanes": {
                "devin": {
                    "type": "audit",
                    "driver": "hosted_bridge",
                    "provider": "devin",
                    "merge_authority": True,
                    "trigger_policy": "manual",
                    "labels": {
                        "needs": "needs-devin-audit",
                        "done": "devin-audit-done",
                        "blocked": "devin-audit-blocked",
                    },
                    "token_env": ["DEVIN_AUDIT_LABEL_TOKEN", "GITHUB_TOKEN"],
                    "review_hygiene": {
                        "workflow": ".github/workflows/devin-clear-stale.yml",
                        "token_env": "GITHUB_TOKEN",
                    },
                }
            },
            "profiles": {
                "recommended": {
                    "description": "Devin generated workflow test",
                    "lanes": ["devin"],
                }
            },
        }
        plan = code_mower_init.render_init_plan(
            devin_config,
            package_mode=True,
            package_command="code-mower",
        )

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / ".code-mower.generated-devin"
            result = code_mower_init.apply_init_plan(plan, output_dir)
            placeholder_files = {
                str(Path(path).relative_to(output_dir))
                for path in result["placeholder_files"]
                if Path(path).is_relative_to(output_dir)
            }
            expected = {
                ".github/workflows/devin-audit-bridge.yml",
                ".github/workflows/devin-audit-labeler.yml",
                ".github/workflows/devin-clear-stale.yml",
            }
            self.assertTrue(expected.isdisjoint(placeholder_files))

            bridge = output_dir.joinpath(
                ".github/workflows/devin-audit-bridge.yml"
            ).read_text(encoding="utf-8")
            self.assertIn("gh issue edit", bridge)
            self.assertIn("needs-devin-audit", bridge)
            self.assertIn("DEVIN_AUDIT_LABEL_TOKEN", bridge)

            labeler = output_dir.joinpath(
                ".github/workflows/devin-audit-labeler.yml"
            ).read_text(encoding="utf-8")
            self.assertIn("tools/code_mower trailer-comment-labeler --lane", labeler)
            self.assertIn("TRAILER_LANE: devin", labeler)
            self.assertIn("devin-ai-integration[bot]", labeler)
            self.assertNotIn("devin-audit-bot[bot]", labeler)
            self.assertIn("DEVIN_AUDIT_LABEL_TOKEN", labeler)
            self.assertIn("DEVIN_BOT_AUTHORS", labeler)
            self.assertIn("CODE_MOWER_GITHUB_ACTIONS_WORKFLOWS", labeler)
            self.assertNotIn("__GITHUB_ACTIONS_WORKFLOWS__", labeler)
            self.assertNotIn("github.event.inputs.lane", labeler)
            self.assertNotIn("github.event.comment.user.type == 'Bot'", labeler)

            stale = output_dir.joinpath(
                ".github/workflows/devin-clear-stale.yml"
            ).read_text(encoding="utf-8")
            self.assertIn("tools/code_mower clear-stale", stale)
            self.assertIn('default: "devin"', stale)

    def test_shipped_saas_event_lanes_have_bot_authors(self) -> None:
        config_path = ROOT / "src/code_mower/templates/code-mower.example.yml"
        cfg = code_mower_config.load_config(config_path)
        for lane_id, lane in cfg["lanes"].items():
            if lane.get("driver") != "saas_event":
                continue
            provider_config = lane.get("provider_config")
            self.assertIsInstance(provider_config, dict, lane_id)
            self.assertTrue(provider_config.get("bot_authors"), lane_id)

    def test_init_apply_generates_product_support_wrappers(self) -> None:
        config_path = ROOT / "src/code_mower/templates/code-mower.example.yml"
        plan = code_mower_init.render_init_plan(
            code_mower_config.load_config(config_path),
            package_mode=True,
            package_command="code-mower",
        )

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / ".code-mower.generated"
            result = code_mower_init.apply_init_plan(plan, output_dir)

            generated = {
                "reviewer-value-report.example.md",
                "tools/code_mower",
                "tools/code_mower_standalone_shadow.sh",
                "tools/code_mower_standalone_pin.env",
                "tools/run_codex_audit_pr.sh",
                "tools/run_claude_audit_pr.sh",
                "tools/audit_labeler_lib.py",
                "tools/decisions.py",
                "tools/safe_gh_comment.py",
                "tools/status_report.py",
            }
            written = {
                str(Path(path).relative_to(output_dir))
                for path in result["written_files"]
                if Path(path).is_relative_to(output_dir)
            }
            self.assertTrue(generated.issubset(written))
            placeholder_files = {
                str(Path(path).relative_to(output_dir))
                for path in result["placeholder_files"]
                if Path(path).is_relative_to(output_dir)
            }
            self.assertTrue(generated.isdisjoint(placeholder_files))
            non_executable_generated = {
                "reviewer-value-report.example.md",
                "tools/audit_labeler_lib.py",
                "tools/decisions.py",
                "tools/code_mower_standalone_pin.env",
            }
            for rel_path in generated - non_executable_generated:
                self.assertTrue(output_dir.joinpath(rel_path).stat().st_mode & 0o111, rel_path)
            self.assertIn(
                "CODE_MOWER_STANDALONE_REF",
                output_dir.joinpath("tools/code_mower_standalone_pin.env").read_text(
                    encoding="utf-8"
                ),
            )
            self.assertIn(
                "Code Mower Reviewer Value Report",
                output_dir.joinpath("reviewer-value-report.example.md").read_text(
                    encoding="utf-8"
                ),
            )
            package_helper = ROOT / "src/code_mower/audit_labeler_lib.py"
            self.assertTrue(package_helper.is_file())
            self.assertEqual(
                output_dir.joinpath("tools/audit_labeler_lib.py").read_text(
                    encoding="utf-8"
                ),
                package_helper.read_text(encoding="utf-8"),
            )
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "py_compile",
                    str(output_dir / "tools/audit_labeler_lib.py"),
                ],
                check=True,
                text=True,
            )

            devin_config = {
                "version": 1,
                "project": {
                    "name": "devin-stale-test",
                    "state_dir": "~/.cache/code-mower",
                },
                "repositories": [{"slug": "owner/repo", "default_branch": "main"}],
                "lanes": {
                    "devin": {
                        "type": "audit",
                        "driver": "hosted_bridge",
                        "provider": "devin",
                        "merge_authority": True,
                        "trigger_policy": "manual",
                        "labels": {
                            "needs": "needs-devin-audit",
                            "done": "devin-audit-done",
                            "blocked": "devin-audit-blocked",
                        },
                        "token_env": ["DEVIN_AUDIT_LABEL_TOKEN", "GITHUB_TOKEN"],
                        "review_hygiene": {
                            "workflow": ".github/workflows/devin-clear-stale.yml",
                            "token_env": "GITHUB_TOKEN",
                        },
                    }
                },
                "profiles": {
                    "recommended": {
                        "description": "Devin stale workflow test",
                        "lanes": ["devin"],
                    }
                },
            }
            stale_plan = code_mower_init.render_init_plan(
                devin_config,
                package_mode=True,
                package_command="code-mower",
            )
            stale_generated = Path(tmp) / ".code-mower.generated-devin-stale"
            stale_apply = code_mower_init.apply_init_plan(stale_plan, stale_generated)
            stale_workflow = stale_generated / ".github/workflows/devin-clear-stale.yml"
            self.assertIn(str(stale_workflow), stale_apply["written_files"])
            self.assertNotIn(str(stale_workflow), stale_apply["placeholder_files"])
            stale_text = stale_workflow.read_text(encoding="utf-8")
            self.assertIn("name: Code Mower Devin Clear Stale Audits", stale_text)
            self.assertIn("tools/code_mower clear-stale", stale_text)
            self.assertIn("--dispatch-workflow", stale_text)
            self.assertIn("github.event.pull_request.head.sha", stale_text)
            self.assertIn("code-mower-clear-stale-devin-${{", stale_text)
            self.assertIn("cancel-in-progress: true", stale_text)
            self.assertIn("No PR/head SHA available for stale-label cleanup", stale_text)
            self.assertIn('default: "devin"', stale_text)
            self.assertIn("github.event.inputs.lane || 'devin'", stale_text)
            self.assertNotIn("{% raw %}", stale_text)
            self.assertNotIn("{% endraw %}", stale_text)

            custom_config = {
                **devin_config,
                "lanes": {
                    "custom": {
                        **devin_config["lanes"]["devin"],
                        "review_hygiene": {
                            "workflow": ".github/workflows/custom-clear-stale.yml",
                            "token_env": "GITHUB_TOKEN",
                        },
                    }
                },
                "profiles": {
                    "recommended": {
                        "description": "Custom stale workflow test",
                        "lanes": ["custom"],
                    }
                },
            }
            custom_plan = code_mower_init.render_init_plan(
                custom_config,
                package_mode=True,
                package_command="code-mower",
            )
            custom_generated = Path(tmp) / ".code-mower.generated-custom-stale"
            code_mower_init.apply_init_plan(custom_plan, custom_generated)
            custom_text = (
                custom_generated / ".github/workflows/custom-clear-stale.yml"
            ).read_text(encoding="utf-8")
            self.assertIn("name: Code Mower Custom Clear Stale Audits", custom_text)
            self.assertIn('default: "custom"', custom_text)
            self.assertIn("github.event.inputs.lane || 'custom'", custom_text)
            self.assertIn("code-mower-clear-stale-custom-${{", custom_text)
            self.assertNotIn("{% raw %}", custom_text)

            disabled_hygiene_config = {
                **devin_config,
                "lanes": {
                    "manual_review": {
                        **devin_config["lanes"]["devin"],
                        "driver": "manual",
                        "provider": "manual-reviewer",
                        "merge_authority": False,
                        "review_hygiene": {},
                    }
                },
                "profiles": {
                    "recommended": {
                        "description": "Disabled stale workflow test",
                        "lanes": ["manual_review"],
                    }
                },
            }
            self.assertEqual(code_mower_config.validate_config(disabled_hygiene_config), [])
            disabled_plan = code_mower_init.render_init_plan(
                disabled_hygiene_config,
                package_mode=True,
                package_command="code-mower",
            )
            disabled_paths = {
                item["path"] for item in disabled_plan.data["generated_files"]
            }
            self.assertNotIn(".github/workflows/devin-clear-stale.yml", disabled_paths)

            result = subprocess.run(
                [str(output_dir / "tools/code_mower"), "--version"],
                cwd=output_dir,
                env={
                    "PATH": os.defpath,
                    "HOME": os.environ.get("HOME", ""),
                },
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("replace the placeholder", result.stderr)
            source_root = Path(tmp) / "stale-product-repo"
            source_root.joinpath("tools").mkdir(parents=True)
            source_root.joinpath("tools/code_mower").write_text("stale local wrapper\n", encoding="utf-8")
            stale_output = Path(tmp) / ".code-mower.generated-stale-source"
            stale_result = code_mower_init.apply_init_plan(
                plan,
                stale_output,
                source_root=source_root,
            )
            self.assertIn(str(stale_output / "tools/code_mower"), stale_result["written_files"])
            self.assertNotEqual(
                stale_output.joinpath("tools/code_mower").read_text(encoding="utf-8"),
                "stale local wrapper\n",
            )
            removed_mirror_completed = subprocess.run(
                [str(output_dir / "tools/code_mower"), "providers", "list"],
                cwd=output_dir,
                env={
                    "PATH": os.environ.get("PATH", os.defpath),
                    "HOME": os.environ.get("HOME", ""),
                    "CODE_MOWER_USE_LOCAL": "1",
                },
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(removed_mirror_completed.returncode, 0)
            self.assertIn(
                "repo-local Code Mower mirror is unavailable",
                removed_mirror_completed.stderr,
            )
            local_bootstrap = output_dir / "tools/code_mower_bootstrap.py"
            local_bootstrap.write_text(
                """#!/usr/bin/env python3
import sys
if "--print-python" in sys.argv:
    print(sys.executable)
else:
    raise SystemExit("unexpected bootstrap args: " + " ".join(sys.argv[1:]))
""",
                encoding="utf-8",
            )
            local_cli = output_dir / "tools/code_mower_cli.py"
            local_cli.write_text(
                """#!/usr/bin/env python3
import sys
print("local:" + " ".join(sys.argv[1:]))
""",
                encoding="utf-8",
            )
            local_completed = subprocess.run(
                [str(output_dir / "tools/code_mower"), "providers", "list"],
                cwd=output_dir,
                env={
                    "PATH": os.environ.get("PATH", os.defpath),
                    "HOME": os.environ.get("HOME", ""),
                    "CODE_MOWER_USE_LOCAL": "1",
                },
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(local_completed.returncode, 0, local_completed.stderr)
            self.assertEqual(local_completed.stdout.strip(), "local:providers list")
            fake_code_mower = output_dir / "tools/code_mower"
            fake_code_mower.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
lane="$1"
stdin_flag="$2"
if [ -n "${GITHUB_TOKEN:-}" ] || [ -n "${GH_TOKEN:-}" ]; then
  echo "token leaked through environment" >&2
  exit 42
fi
read -r token
if [ "${stdin_flag}" != "--read-token-from-stdin" ]; then
  echo "missing stdin flag: ${stdin_flag}" >&2
  exit 43
fi
if [ "${token}" != "secret-token" ]; then
  echo "wrong token: ${token}" >&2
  exit 44
fi
printf '%s\\n' "${lane}"
""",
                encoding="utf-8",
            )
            fake_code_mower.chmod(0o755)
            for wrapper, lane in (
                ("tools/run_codex_audit_pr.sh", "codex-audit"),
                ("tools/run_claude_audit_pr.sh", "claude-audit"),
            ):
                completed = subprocess.run(
                    [str(output_dir / wrapper), "--repo", "owner/repo"],
                    cwd=output_dir,
                    env={
                        "PATH": os.defpath,
                        "HOME": os.environ.get("HOME", ""),
                        "GITHUB_TOKEN": "secret-token",
                    },
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(completed.stdout.strip(), lane)

    def test_privacy_scan_is_clean(self) -> None:
        self.assertEqual(privacy_scan.scan(ROOT), [])

    def test_public_demo_calibration_artifacts_are_parseable_and_sanitized(self) -> None:
        demo_dir = ROOT / "examples/demo-calibration"
        required = {
            "README.md",
            "calibration-corpus.json",
            "lane-policy.json",
            "reviewer-metrics.json",
            "reviewer-value-report.md",
        }
        self.assertTrue(demo_dir.is_dir())
        self.assertTrue(required.issubset({path.name for path in demo_dir.iterdir()}))

        for rel_path in (
            "calibration-corpus.json",
            "lane-policy.json",
            "reviewer-metrics.json",
        ):
            with self.subTest(rel_path=rel_path):
                json.loads(demo_dir.joinpath(rel_path).read_text(encoding="utf-8"))
        metrics = json.loads(
            demo_dir.joinpath("reviewer-metrics.json").read_text(encoding="utf-8")
        )
        policy = json.loads(demo_dir.joinpath("lane-policy.json").read_text(encoding="utf-8"))
        self.assertEqual(metrics["mode"], "reviewer-metrics")
        self.assertIn("codex-audit", metrics["profiles"])
        self.assertEqual(policy["mode"], "code-mower-lane-policy")
        self.assertIn("codex-audit", policy["policies"])

        public_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(demo_dir.iterdir())
            if path.is_file()
        )
        self.assertIn("known-clean", public_text)
        self.assertIn("known-blocked", public_text)
        self.assertIn("metadata", public_text)
        forbidden_fragments = (
            "/" + "Users/",
            "/private" + "/tmp",
            "raw diffs included",
            "raw transcripts included",
        )
        for fragment in forbidden_fragments:
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, public_text)

    def test_public_demo_calibration_generates_expected_value_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "reviewer-value-report.md"
            code = code_mower_calibration.main(
                [
                    "value-report",
                    str(ROOT / "examples/demo-calibration/calibration-corpus.json"),
                    "--output",
                    str(output),
                ]
            )
            self.assertEqual(code, 0)
            report = output.read_text(encoding="utf-8")

        self.assertIn("Corpus: `demo-two-pr-calibration`", report)
        self.assertIn("| `codex-audit` | 2 | 1 | 0 | 1.0 | 1 | 1/0", report)
        self.assertIn("| `claude-audit` | 2 | 1 | 0 | 1.0 | 1 | 1/0", report)
        self.assertIn("| `experimental-lens` | 2 | 0 | 1 | 0.0 | 0 | 0/1", report)

    def test_calibration_policy_seam_classifies_lane_roles(self) -> None:
        report = calibration_policy.build_lane_policy_report(
            {
                "mode": "reviewer-metrics",
                "profiles": {
                    "codex": {
                        "useful_rate": 0.75,
                        "useful_findings": 12,
                        "known_disposition_count": 12,
                        "known_clean_pass_runs": 3,
                        "known_blocked_runs": 2,
                        "known_blocked_caught_runs": 2,
                    },
                    "operability": {
                        "useful_rate": 0.5,
                        "useful_findings": 2,
                        "known_disposition_count": 4,
                        "known_clean_pass_runs": 2,
                        "review_classes": ["general", "ops"],
                        "useful_review_classes": ["ops"],
                    },
                    "noisy": {
                        "useful_rate": 0.2,
                        "useful_findings": 1,
                        "known_disposition_count": 5,
                        "known_clean_pass_runs": 0,
                        "blocking_false_positive_runs": 1,
                    },
                },
            }
        )

        self.assertEqual(report["mode"], "code-mower-lane-policy")
        policies = report["policies"]
        self.assertEqual(policies["codex"]["classification"], "merge_gate_candidate")
        self.assertEqual(policies["codex"]["recommended_role"], "merge_gate_eligible")
        self.assertEqual(
            policies["operability"]["classification"],
            "selective_trigger_candidate",
        )
        self.assertEqual(
            policies["operability"]["automatic_trigger"],
            "matching_review_class_only",
        )
        self.assertEqual(policies["operability"]["suggested_trigger_classes"], ["ops"])
        self.assertEqual(policies["noisy"]["classification"], "informational")
        self.assertIn(
            "has known-clean blocking false positives",
            policies["noisy"]["reasons"],
        )

    def test_schema_ids_are_code_mower_branded(self) -> None:
        schema = json.loads(
            (ROOT / "src/code_mower/codex_audit_verdict.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            schema["properties"]["schema"]["enum"],
            ["codeMower.codexAudit.v1"],
        )
        self.assertIn(
            "codeMower.claudeAudit.v1",
            (ROOT / "src/code_mower/claude_audit_pr.py").read_text(encoding="utf-8"),
        )

    def test_package_manifest_has_no_local_output_path(self) -> None:
        manifest = json.loads(
            (ROOT / "code-mower-package-manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["output_dir"], "<generated-output-dir>")

    def test_package_manifest_excludes_generated_cache_artifacts(self) -> None:
        forbidden_fragments = (
            "__pycache__",
            ".pytest_cache",
            ".ruff_cache",
            ".mypy_cache",
            ".egg-info",
        )
        forbidden_suffixes = (".pyc", ".pyo")

        package_entries = {
            value
            for source, target, _mode in code_mower_package.PACKAGE_FILES
            for value in (source, target)
        }
        bad_package_entries = sorted(
            entry
            for entry in package_entries
            if any(fragment in entry for fragment in forbidden_fragments)
            or entry.endswith(forbidden_suffixes)
        )
        self.assertEqual(bad_package_entries, [])

        tracked_files = subprocess.check_output(
            ["git", "ls-files"],
            cwd=ROOT,
            text=True,
        ).splitlines()
        bad_tracked_files = sorted(
            path
            for path in tracked_files
            if any(fragment in path for fragment in forbidden_fragments)
            or path.endswith(forbidden_suffixes)
        )
        self.assertEqual(bad_tracked_files, [])

        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        self.assertIn("global-exclude *.py[cod]", manifest)
        self.assertIn("global-exclude __pycache__", manifest)

    def test_package_materializer_includes_product_support_templates(self) -> None:
        packaged_sources = {source for _, source, _ in code_mower_package.PACKAGE_FILES}
        packaged_sources_by_target = {
            target: source for source, target, _ in code_mower_package.PACKAGE_FILES
        }
        for source in (
            "src/code_mower/templates/product-support/code_mower",
            "src/code_mower/templates/product-support/code_mower_standalone_pin.env",
            "src/code_mower/templates/product-support/code_mower_standalone_shadow.sh",
            "src/code_mower/templates/product-support/run_claude_audit_pr.sh",
            "src/code_mower/templates/product-support/run_codex_audit_pr.sh",
            "src/code_mower/audit_labeler_lib.py",
            "src/code_mower/decisions.py",
            "src/code_mower/templates/product-support/safe_gh_comment.py",
            "src/code_mower/templates/product-support/status_report.py",
        ):
            self.assertIn(source, packaged_sources)
        self.assertEqual(
            packaged_sources_by_target["src/code_mower/audit_labeler_lib.py"],
            "tools/audit_labeler_lib.py",
        )

        packaged_template_targets = {
            target for _kind, target in code_mower_package.TEMPLATE_FILES
        }
        self.assertIn(
            "src/code_mower/templates/workflows/review-clear-stale.yml.j2",
            packaged_template_targets,
        )
        self.assertIn(
            "src/code_mower/templates/workflows/needs-owner-notify.yml.j2",
            packaged_template_targets,
        )
        self.assertIn(
            "src/code_mower/templates/workflows/weekly-status.yml.j2",
            packaged_template_targets,
        )
        self.assertIn(
            "src/code_mower/templates/workflows/self-hosted-local-audit.yml.j2",
            packaged_template_targets,
        )
        self.assertIn(
            "src/code_mower/templates/workflows/code-mower-gate-health.yml.j2",
            packaged_template_targets,
        )
        self.assertIn(
            "src/code_mower/templates/workflows/code-mower-agent-pr-labeler.yml.j2",
            packaged_template_targets,
        )
        self.assertIn(
            "src/code_mower/templates/workflows/code-mower-fix-round-dispatch.yml.j2",
            packaged_template_targets,
        )
        self.assertIn(
            "src/code_mower/templates/workflows/dispatch-lanes.yml.j2",
            packaged_template_targets,
        )
        self.assertIn(
            "src/code_mower/templates/workflows/lane-mac-runner.yml.j2",
            packaged_template_targets,
        )
        self.assertIn(
            "src/code_mower/templates/lanes/run_mac_lane.sh",
            packaged_template_targets,
        )

    def test_package_manifest_has_unique_targets_and_known_source_reuse(self) -> None:
        from collections import Counter

        sources = [source for source, _target, _mode in code_mower_package.PACKAGE_FILES]
        targets = [target for _source, target, _mode in code_mower_package.PACKAGE_FILES]
        self.assertEqual(len(targets), len(set(targets)))
        expected_source_reuse = {
            "tools/builder_experiment.example.json",
            "tools/calibration_corpus.example.json",
            "tools/context_packs.example.json",
            "tools/reviewer_spend.example.json",
            "tools/reviewer_value_report.example.md",
        }
        repeated_sources = {
            source for source, count in Counter(sources).items() if count > 1
        }
        self.assertEqual(repeated_sources, expected_source_reuse)

    def test_package_materializer_includes_internal_package_seams(self) -> None:
        packaged_targets = {target for _, target, _ in code_mower_package.PACKAGE_FILES}
        expected_targets = {
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / "src/code_mower").glob("*.py")
            if path.name != "__init__.py"
        }
        package_dirs = (
            "src/code_mower/calibration",
            "src/code_mower/cloud_client",
            "src/code_mower/doctor_checks",
            "src/code_mower/provider_runners",
        )
        for package_dir in package_dirs:
            expected_targets.update(
                path.relative_to(ROOT).as_posix()
                for path in (ROOT / package_dir).glob("*.py")
            )

        for target in sorted(expected_targets):
            with self.subTest(target=target):
                self.assertIn(target, packaged_targets)

    def test_package_helper_splits_preserve_legacy_tools_sources(self) -> None:
        packaged_sources_by_target = {
            target: source for source, target, _ in code_mower_package.PACKAGE_FILES
        }
        expected_sources = {
            "src/code_mower/package_content.py": "tools/code_mower_package_content.py",
            "src/code_mower/package_manifest.py": "tools/code_mower_package_manifest.py",
            "src/code_mower/package_rendering.py": "tools/code_mower_package_rendering.py",
            "src/code_mower/package_static.py": "tools/code_mower_package_static.py",
            "src/code_mower/versioning.py": "tools/code_mower_versioning.py",
        }
        for target, source in expected_sources.items():
            with self.subTest(target=target):
                self.assertEqual(packaged_sources_by_target[target], source)

        self.assertEqual(
            (ROOT / "src/code_mower/versioning.py").read_text(encoding="utf-8"),
            (ROOT / "tools/code_mower_versioning.py").read_text(encoding="utf-8"),
        )

    def test_package_content_legacy_tools_import_falls_back_without_tools_shim(self) -> None:
        original_path = list(sys.path)
        saved_modules = {
            key: sys.modules.get(key)
            for key in ("tools", "tools.code_mower_package_content")
            if key in sys.modules
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tools_dir = tmp_path / "tools"
            tools_dir.mkdir()
            tools_dir.joinpath("__init__.py").write_text("", encoding="utf-8")
            tools_dir.joinpath("code_mower_package_content.py").write_text(
                (ROOT / "src/code_mower/package_content.py").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            try:
                sys.path.insert(0, str(tmp_path))
                sys.modules.pop("tools", None)
                sys.modules.pop("tools.code_mower_package_content", None)
                legacy_module = __import__(
                    "tools.code_mower_package_content",
                    fromlist=["current_alpha_package_spec"],
                )
                self.assertEqual(
                    legacy_module.current_alpha_package_spec("0.5.0b53"),
                    "code-mower==0.5.0b53",
                )
            finally:
                sys.path[:] = original_path
                sys.modules.pop("tools", None)
                sys.modules.pop("tools.code_mower_package_content", None)
                for key, module in saved_modules.items():
                    if module is not None:
                        sys.modules[key] = module

    def test_package_content_legacy_tools_import_is_self_contained_with_tools_shim(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tools_dir = tmp_path / "tools"
            tools_dir.mkdir()
            tools_dir.joinpath("__init__.py").write_text("", encoding="utf-8")
            for filename in (
                "code_mower_package_content.py",
                "code_mower_versioning.py",
            ):
                tools_dir.joinpath(filename).write_text(
                    (ROOT / "tools" / filename).read_text(encoding="utf-8")
                    if (ROOT / "tools" / filename).is_file()
                    else (ROOT / "src/code_mower/package_content.py").read_text(
                        encoding="utf-8",
                    ),
                    encoding="utf-8",
                )

            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "from tools import code_mower_package_content as c; "
                        "print(c.current_alpha_package_spec('0.5.0b53'))"
                    ),
                ],
                cwd=tmp_path,
                env={"PYTHONPATH": str(tmp_path), "PATH": os.environ.get("PATH", "")},
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            completed.stdout.strip(),
            "code-mower==0.5.0b53",
        )

    def test_package_rendering_legacy_fallback_stays_valid_yaml_subset(self) -> None:
        payload = {
            "providers": {
                "codex": {"enabled": True, "labels": ["needs-codex-audit"]},
                "local": {"enabled": False, "labels": []},
            }
        }

        rendered = code_mower_package._render_provider_catalog_json_fallback(payload)

        self.assertEqual(json.loads(rendered), payload)
        self.assertTrue(rendered.endswith("\n"))

    def test_package_materializer_prefers_loaded_checkout_before_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                code_mower_package.Path, "cwd", return_value=Path(tmp)
            ):
                roots = code_mower_package._candidate_package_source_roots()

        self.assertEqual(roots[0], ROOT)
        self.assertEqual(roots[-1], Path(tmp).resolve())

    def test_package_materializer_preserves_explicit_default_named_config(self) -> None:
        self.assertEqual(
            code_mower_package.resolve_package_config_path(
                code_mower_package.DEFAULT_PACKAGE_CONFIG,
                explicit=True,
            ),
            Path(code_mower_package.DEFAULT_PACKAGE_CONFIG),
        )
        self.assertIn(
            "src/code_mower/templates/code-mower.example.yml",
            code_mower_package.resolve_package_config_path(
                code_mower_package.DEFAULT_PACKAGE_CONFIG
            ).as_posix(),
        )

    def test_package_materializer_prefers_explicit_repo_root_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_path = root / "src/code_mower/__init__.py"
            init_path.parent.mkdir(parents=True)
            init_path.write_text(
                '"""Code Mower package."""\n\n__version__ = "9.9.9a1"\n',
                encoding="utf-8",
            )

            self.assertEqual(
                code_mower_package._running_code_mower_version(root),
                "9.9.9a1",
            )

    def test_package_materializer_can_run_from_extracted_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stdout = StringIO()
            with redirect_stdout(stdout):
                code = code_mower_package.main(
                    [
                        str(ROOT / "src/code_mower/templates/code-mower.example.yml"),
                        "--output-dir",
                        tmp,
                        "--json",
                    ]
                )

            self.assertEqual(code, 0)
            output_dir = Path(tmp)
            self.assertTrue((output_dir / "pyproject.toml").is_file())
            self.assertTrue(
                (output_dir / "src/code_mower/cloud_client/dogfood.py").is_file()
            )
            self.assertIn(
                'version = "0.5.0b53"',
                (output_dir / "pyproject.toml").read_text(encoding="utf-8"),
            )
            self.assertIn(
                '__version__ = "0.5.0b53"',
                (output_dir / "src/code_mower/__init__.py").read_text(
                    encoding="utf-8"
                ),
            )
            manifest = json.loads(
                (output_dir / "code-mower-package-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            sources = {entry["source"] for entry in manifest["files_written"]}
            self.assertIn("src/code_mower/cloud_client/dogfood.py", sources)
            self.assertIn("src/code_mower/templates/code-mower.example.yml", sources)

    def test_package_materializer_resolves_default_config_from_extracted_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stdout = StringIO()
            with redirect_stdout(stdout):
                code = code_mower_package.main(
                    [
                        "--output-dir",
                        tmp,
                        "--json",
                    ]
                )

            self.assertEqual(code, 0)
            output_dir = Path(tmp)
            self.assertTrue(
                (output_dir / "src/code_mower/templates/code-mower.example.yml").is_file()
            )

    def test_cli_package_resolves_default_config_from_extracted_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stdout = StringIO()
            with redirect_stdout(stdout):
                code = code_mower_cli._package_main(
                    [
                        "--output-dir",
                        tmp,
                        "--json",
                    ]
                )

            self.assertEqual(code, 0)
            output_dir = Path(tmp)
            self.assertTrue(
                (output_dir / "src/code_mower/templates/code-mower.example.yml").is_file()
            )

    def test_standalone_shadow_holds_checkout_lock_through_delegation(self) -> None:
        text = (
            ROOT
            / "src/code_mower/templates/product-support/code_mower_standalone_shadow.sh"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'CODE_MOWER_STANDALONE_CHECKOUT_LOCK_TIMEOUT_SECONDS:-7200',
            text,
        )
        release_index = text.rfind("release_checkout_lock")
        delegate_index = text.rfind('"${script_dir}/code_mower" "$@"')
        self.assertGreaterEqual(release_index, 0)
        self.assertGreater(delegate_index, release_index)
        self.assertNotIn(
            'release_checkout_lock\nset +e\n"${script_dir}/code_mower" "$@"',
            text,
        )

    def test_private_shadow_workflow_uses_authenticated_package_rehearsal(self) -> None:
        for rel_path in (
            "templates/workflows/private-standalone-shadow.yml.j2",
            "src/code_mower/templates/workflows/private-standalone-shadow.yml.j2",
        ):
            with self.subTest(rel_path=rel_path):
                text = (ROOT / rel_path).read_text(encoding="utf-8")
                self.assertIn("CODE_MOWER_STANDALONE_DEPLOY_KEY", text)
                self.assertIn("CODE_MOWER_STANDALONE_REPO_URL", text)
                self.assertIn("CODE_MOWER_STANDALONE_PACKAGE_REPO_URL", text)
                self.assertIn('code_mower_ref="${CODE_MOWER_STANDALONE_REF:-}"', text)
                self.assertIn('if [ -z "${code_mower_ref}" ]; then', text)
                self.assertIn("code_mower_ref=\"$(sed -n", text)
                self.assertIn(
                    'if [ "${code_mower_ref}" = "<pin-a-reviewed-code-mower-commit-or-tag>" ]; then',
                    text,
                )
                self.assertIn(
                    'replace CODE_MOWER_STANDALONE_PACKAGE_REPO_URL',
                    text,
                )
                self.assertIn(
                    'package_spec="${CODE_MOWER_STANDALONE_PACKAGE_REPO_URL}@${code_mower_ref}"',
                    text,
                )
                self.assertIn("tools/code_mower migration package-install-rehearsal", text)
                self.assertIn(".code-mower/package-install-rehearsal.json", text)
                self.assertNotIn("source tools/code_mower_standalone_pin.env", text)

        fallback_text = "\n".join(
            [
                (ROOT / "src/code_mower/package.py").read_text(encoding="utf-8"),
                (ROOT / "src/code_mower/package_content.py").read_text(
                    encoding="utf-8"
                ),
            ]
        )
        self.assertIn("CODE_MOWER_STANDALONE_PACKAGE_REPO_URL", fallback_text)
        self.assertIn('code_mower_ref="${CODE_MOWER_STANDALONE_REF:-}"', fallback_text)
        self.assertIn(
            'package_spec="${CODE_MOWER_STANDALONE_PACKAGE_REPO_URL}@${code_mower_ref}"',
            fallback_text,
        )

    def test_claude_diff_builder_does_not_use_fetch_head_for_pr_head(self) -> None:
        text = (ROOT / "src/code_mower/claude_audit_pr.py").read_text(
            encoding="utf-8"
        )
        helper_text = (
            ROOT / "src/code_mower/provider_runners/pr_worktree.py"
        ).read_text(encoding="utf-8")
        self.assertIn("def _fetch_base_sha_for_diff", text)
        self.assertIn("def _fetch_pr_head_sha_for_diff", text)
        self.assertIn("return _shared_fetch_base_ref_sha", text)
        self.assertIn("return _shared_fetch_pr_head_sha", text)
        self.assertIn('fetch_refspec = f"+{base_ref}:{local_ref}"', helper_text)
        self.assertIn('f"+pull/{pr_number}/head:{local_ref}"', helper_text)
        self.assertIn('["update-ref", "-d", local_ref]', helper_text)
        self.assertIn("fetched_base_ref = _fetch_base_sha_for_diff", text)
        self.assertIn("fetched_head_ref = _fetch_pr_head_sha_for_diff", text)
        self.assertNotIn('["rev-parse", "FETCH_HEAD"]', text)

    def test_standalone_wrapper_reinstalls_into_custom_venv_without_deleting_it(self) -> None:
        config_path = ROOT / "src/code_mower/templates/code-mower.example.yml"
        plan = code_mower_init.render_init_plan(
            code_mower_config.load_config(config_path),
            package_mode=True,
            package_command="code-mower",
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "generated"
            code_mower_init.apply_init_plan(plan, output_dir)

            fake_package = root / "fake-code-mower"
            fake_package.mkdir()
            fake_package.joinpath("pyproject.toml").write_text(
                """[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "fake-code-mower-wrapper-test"
version = "0.0.0"

[project.scripts]
code-mower = "fake_code_mower:main"

[tool.setuptools]
py-modules = ["fake_code_mower"]
""",
                encoding="utf-8",
            )
            fake_package.joinpath("fake_code_mower.py").write_text(
                """import sys


def main():
    print("fake-code-mower " + " ".join(sys.argv[1:]))
    return 0
""",
                encoding="utf-8",
            )
            custom_venv = root / "custom-standalone-venv"
            env = {
                "PATH": os.environ.get("PATH", os.defpath),
                "HOME": os.environ.get("HOME", ""),
                "CODE_MOWER_BOOTSTRAP_PYTHON": sys.executable,
                "CODE_MOWER_STANDALONE_PATH": str(fake_package),
                "CODE_MOWER_STANDALONE_VENV": str(custom_venv),
            }
            first = subprocess.run(
                [str(output_dir / "tools/code_mower"), "--version"],
                cwd=output_dir,
                env=env,
                text=True,
                capture_output=True,
                check=False,
                timeout=120,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn("fake-code-mower --version", first.stdout)
            self.assertTrue(custom_venv.joinpath("bin/python").is_file())

            second_env = dict(env)
            second_env["CODE_MOWER_STANDALONE_REINSTALL"] = "1"
            second = subprocess.run(
                [str(output_dir / "tools/code_mower"), "providers", "list"],
                cwd=output_dir,
                env=second_env,
                text=True,
                capture_output=True,
                check=False,
                timeout=120,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("fake-code-mower providers list", second.stdout)
            self.assertNotIn("refusing to recreate unsafe standalone venv", second.stderr)

    def test_wrapper_ignores_missing_absolute_python_candidates(self) -> None:
        config_path = ROOT / "src/code_mower/templates/code-mower.example.yml"
        plan = code_mower_init.render_init_plan(
            code_mower_config.load_config(config_path),
            package_mode=True,
            package_command="code-mower",
        )

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "generated"
            code_mower_init.apply_init_plan(plan, output_dir)
            missing_python = Path(tmp) / "definitely-missing-python"
            completed = subprocess.run(
                [str(output_dir / "tools/code_mower"), "providers", "list"],
                cwd=output_dir,
                env={
                    "PATH": os.environ.get("PATH", os.defpath),
                    "HOME": os.environ.get("HOME", ""),
                    "CODE_MOWER_BOOTSTRAP_PYTHON_CANDIDATES": (
                        f"{missing_python} {sys.executable}"
                    ),
                    "CODE_MOWER_USE_LOCAL": "1",
                },
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn(
                "repo-local Code Mower mirror is unavailable",
                completed.stderr,
            )

    def test_shadow_default_checkout_directory_is_ref_scoped(self) -> None:
        text = (
            ROOT
            / "src/code_mower/templates/product-support/code_mower_standalone_shadow.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("ref_slug=", text)
        self.assertIn("cut -c1-48", text)
        self.assertIn("hash_ref_name()", text)
        self.assertIn("sha256sum", text)
        self.assertIn("shasum -a 256", text)
        self.assertIn("openssl dgst -sha256", text)
        self.assertIn('ref_hash="$(hash_ref_name "${ref}" | cut -c1-12)"', text)
        self.assertIn('code-mower-${ref_slug}-${ref_hash}', text)
        self.assertIn("CODE_MOWER_STANDALONE_VENV", text)
        self.assertIn("standalone-venvs/code-mower-${ref_slug}-${ref_hash}", text)
        self.assertIn('CODE_MOWER_STANDALONE_SOURCE_DIR:-}', text)
        wrapper_text = (
            ROOT / "src/code_mower/templates/product-support/code_mower"
        ).read_text(encoding="utf-8")
        self.assertIn('allowed_managed_venv_root="${repo_root}/.code-mower/standalone-venvs"', wrapper_text)
        self.assertIn('"${allowed_managed_venv_root}"/*', wrapper_text)

    def test_command_redaction_masks_secret_arguments(self) -> None:
        command = [
            "provider",
            "--api-key",
            "sk-real-secret",
            "token=abc12345678901234567890",
            "--safe",
            "value",
        ]
        self.assertEqual(
            audit_progress.redact_command(command),
            [
                "provider",
                "--api-key",
                "REDACTED",
                "token=REDACTED",
                "--safe",
                "value",
            ],
        )

    def test_secret_file_assignment_parser_accepts_supported_name(self) -> None:
        result = code_mower_secrets.parse_secret_file_text(
            "export GEMINI_API_KEY='abc123'\n",
            supported_env_names=code_mower_secrets.GEMINI_SECRET_ENV_NAMES,
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.value, "abc123")
        self.assertEqual(result.assignment_name, "GEMINI_API_KEY")

    def test_secret_file_assignment_parser_rejects_unsupported_secret_name(self) -> None:
        result = code_mower_secrets.parse_secret_file_text(
            "OTHER_API_KEY='abc123'\n",
            supported_env_names=code_mower_secrets.GEMINI_SECRET_ENV_NAMES,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.rejected_assignment_name, "OTHER_API_KEY")

    def test_doctor_auth_probe_detail_redacts_output_content(self) -> None:
        detail = doctor._auth_probe_output_detail("email@example.com\nscope repo\n")
        self.assertEqual(detail, {"output_redacted": True, "output_line_count": 2})

    def test_doctor_json_probe_classifies_provider_auth_failure(self) -> None:
        payload = json.dumps(
            {
                "type": "result",
                "subtype": "error",
                "is_error": True,
                "api_error_status": 401,
                "result": "Invalid authentication credentials",
            }
        )

        status, message, detail = doctor._evaluate_json_probe(
            {
                "doctor_probe_error_fields": ("is_error", "api_error_status"),
                "doctor_probe_expect_json_field": "result",
                "doctor_probe_expect_json_value": "ok",
            },
            payload,
            returncode=0,
        )

        self.assertEqual(status, doctor.STATUS_WARN)
        self.assertEqual(message, "probe reported provider authentication failure")
        self.assertTrue(detail["auth_error_detected"])
        self.assertEqual(detail["auth_status_code"], "401")
        self.assertEqual(detail["auth_status_field"], "api_error_status")
        self.assertIn("api_error_status", detail["dirty_error_fields"])
        self.assertNotIn("Invalid authentication credentials", json.dumps(detail))

    def test_doctor_json_probe_classifies_nonzero_provider_auth_failure(self) -> None:
        payload = json.dumps(
            {
                "type": "result",
                "subtype": "error",
                "is_error": True,
                "api_error_status": 401,
                "result": "Invalid authentication credentials",
            }
        )

        status, message, detail = doctor._evaluate_json_probe(
            {
                "doctor_probe_error_fields": ("is_error", "api_error_status"),
                "doctor_probe_expect_json_field": "result",
                "doctor_probe_expect_json_value": "ok",
            },
            payload,
            returncode=1,
        )

        self.assertEqual(status, doctor.STATUS_WARN)
        self.assertEqual(message, "probe reported provider authentication failure")
        self.assertTrue(detail["auth_error_detected"])
        self.assertEqual(detail["auth_status_code"], "401")
        self.assertEqual(detail["auth_status_field"], "api_error_status")

    def test_doctor_json_probe_classifies_configured_auth_status_field(self) -> None:
        payload = json.dumps(
            {
                "is_error": True,
                "status_code": 403,
                "result": "Forbidden",
            }
        )

        status, message, detail = doctor._evaluate_json_probe(
            {
                "doctor_probe_error_fields": ("is_error", "status_code"),
                "doctor_probe_auth_status_fields": ("status_code",),
                "doctor_probe_expect_json_field": "result",
                "doctor_probe_expect_json_value": "ok",
            },
            payload,
            returncode=1,
        )

        self.assertEqual(status, doctor.STATUS_WARN)
        self.assertEqual(message, "probe reported provider authentication failure")
        self.assertTrue(detail["auth_error_detected"])
        self.assertEqual(detail["auth_status_code"], "403")
        self.assertEqual(detail["auth_status_field"], "status_code")

    def test_doctor_json_probe_auth_status_field_can_be_status_only(self) -> None:
        payload = json.dumps(
            {
                "is_error": True,
                "status_code": 401,
                "result": "Unauthorized",
            }
        )

        status, message, detail = doctor._evaluate_json_probe(
            {
                "doctor_probe_error_fields": ("is_error",),
                "doctor_probe_auth_status_fields": ("status_code",),
                "doctor_probe_expect_json_field": "result",
                "doctor_probe_expect_json_value": "ok",
            },
            payload,
            returncode=1,
        )

        self.assertEqual(status, doctor.STATUS_WARN)
        self.assertEqual(message, "probe reported provider authentication failure")
        self.assertTrue(detail["auth_error_detected"])
        self.assertEqual(detail["auth_status_code"], "401")
        self.assertEqual(detail["auth_status_field"], "status_code")
        self.assertEqual(detail["dirty_error_fields"], ["is_error"])

    def test_doctor_json_probe_does_not_copy_verbatim_auth_status_text(self) -> None:
        payload = json.dumps(
            {
                "is_error": True,
                "api_error_status": "401 token for person@example.com expired",
                "result": "Invalid authentication credentials",
            }
        )

        status, message, detail = doctor._evaluate_json_probe(
            {
                "doctor_probe_error_fields": ("is_error", "api_error_status"),
                "doctor_probe_expect_json_field": "result",
                "doctor_probe_expect_json_value": "ok",
            },
            payload,
            returncode=1,
        )

        self.assertEqual(status, doctor.STATUS_WARN)
        self.assertEqual(message, "probe reported provider authentication failure")
        self.assertTrue(detail["auth_error_detected"])
        detail_json = json.dumps(detail)
        self.assertNotIn("person@example.com", detail_json)
        self.assertNotIn("expired", detail_json)
        self.assertNotIn("auth_status_code", detail)

    def test_doctor_json_probe_ignores_unconfigured_auth_status_field(self) -> None:
        payload = json.dumps(
            {
                "api_error_status": 401,
                "result": "ok",
            }
        )

        status, message, detail = doctor._evaluate_json_probe(
            {
                "doctor_probe_error_fields": ("is_error",),
                "doctor_probe_auth_status_fields": (),
                "doctor_probe_expect_json_field": "result",
                "doctor_probe_expect_json_value": "ok",
            },
            payload,
            returncode=0,
        )

        self.assertEqual(status, doctor.STATUS_PASS)
        self.assertEqual(message, "auth smoke probe succeeded")
        self.assertNotIn("auth_error_detected", detail)
        self.assertNotIn("auth_status_code", detail)

    def test_doctor_claude_auth_failure_remediation_uses_real_prompt_smoke(self) -> None:
        remediation = doctor._local_cli_probe_remediation(
            "claude",
            ("--print", "--output-format", "json", "Reply with exactly: ok"),
            {"provider": "claude"},
            auth_error_detected=True,
        )

        self.assertIn("claude auth status", remediation)
        self.assertIn('claude -p "Reply with exactly: ok" --output-format json', remediation)
        self.assertIn("returns 401", remediation)

    def test_doctor_v05_preset_sets_early_adopter_checks(self) -> None:
        import argparse

        args = argparse.Namespace(
            v05=True,
            preflight=False,
            easy=False,
            profile=None,
            probe_runtime=False,
            github=False,
            cloud=False,
        )

        doctor._apply_first_run_defaults(args)

        self.assertTrue(args.easy)
        self.assertEqual(args.profile, "recommended")
        self.assertTrue(args.probe_runtime)
        self.assertTrue(args.github)
        self.assertTrue(args.cloud)

    def test_doctor_cloud_token_check_is_optional_and_content_free(self) -> None:
        old_token = os.environ.pop("CODE_MOWER_TEST_CLOUD_TOKEN", None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                missing = doctor._check_cloud_token_surface(
                    token_env="CODE_MOWER_TEST_CLOUD_TOKEN",
                    token_dir=Path(tmp) / "missing",
                )
                self.assertEqual(missing.status, doctor.STATUS_SKIP)
                self.assertNotIn("secret", json.dumps(missing.as_dict()).lower())

                token_dir = Path(tmp) / "tokens"
                token_dir.mkdir()
                token_file = token_dir / "local.env"
                fake_token = "cmw_live_" + "secret_token"
                token_file.write_text(
                    f"export CODE_MOWER_TEST_CLOUD_TOKEN='{fake_token}'\n",
                    encoding="utf-8",
                )
                token_file.chmod(0o600)

                configured = doctor._check_cloud_token_surface(
                    token_env="CODE_MOWER_TEST_CLOUD_TOKEN",
                    token_dir=token_dir,
                )
                payload = configured.as_dict()
                self.assertEqual(configured.status, doctor.STATUS_PASS)
                self.assertEqual(payload["detail"]["token_files"], ["local.env"])
                self.assertNotIn(fake_token, json.dumps(payload))
        finally:
            if old_token is not None:
                os.environ["CODE_MOWER_TEST_CLOUD_TOKEN"] = old_token

    def test_doctor_cloud_token_check_warns_on_broad_permissions(self) -> None:
        old_token = os.environ.pop("CODE_MOWER_TEST_CLOUD_TOKEN", None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                token_dir = Path(tmp) / "tokens"
                token_dir.mkdir()
                token_file = token_dir / "local.env"
                fake_token = "cmw_live_" + "secret_token"
                token_file.write_text(
                    f"CODE_MOWER_TEST_CLOUD_TOKEN={fake_token}\n",
                    encoding="utf-8",
                )
                token_file.chmod(0o644)

                check = doctor._check_cloud_token_surface(
                    token_env="CODE_MOWER_TEST_CLOUD_TOKEN",
                    token_dir=token_dir,
                )
                payload = check.as_dict()
                self.assertEqual(check.status, doctor.STATUS_WARN)
                self.assertEqual(payload["detail"]["insecure_files"], ["local.env"])
                self.assertNotIn(fake_token, json.dumps(payload))
        finally:
            if old_token is not None:
                os.environ["CODE_MOWER_TEST_CLOUD_TOKEN"] = old_token

    def test_cloud_export_accepts_lane_policy_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            metrics = root / "reviewer-metrics.json"
            policy = root / "lane-policy.json"
            value = root / "reviewer-value-report.md"
            for path in (metrics, policy, value):
                path.write_text("{}", encoding="utf-8")

            payload = code_mower_cloud.build_cloud_bundle(
                reports=[
                    (metrics, "reviewer-metrics"),
                    (policy, "lane-policy"),
                    (value, "value-report"),
                ],
                output_dir=root / "bundle",
                anonymous=True,
            )

            self.assertFalse(payload["upload_ready"])
            kinds = {entry["kind"] for entry in payload["included_reports"]}
            self.assertEqual(kinds, {"reviewer-metrics", "lane-policy", "value-report"})

    def test_cloud_upload_payload_is_metadata_only_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            metrics = root / "reviewer-metrics.json"
            metrics.write_text('{"safe": true}', encoding="utf-8")
            code_mower_cloud.build_cloud_bundle(
                reports=[(metrics, "reviewer-metrics")],
                output_dir=root / "bundle",
                anonymous=True,
            )

            payload = code_mower_cloud.build_upload_payload(
                bundle_dir=root / "bundle",
                include_reports=False,
            )

            self.assertEqual(payload["schema"], code_mower_cloud.UPLOAD_SCHEMA)
            self.assertEqual(payload["upload_mode"], "metadata_only")
            self.assertEqual(payload["reports"][0]["kind"], "reviewer-metrics")
            self.assertNotIn("text", payload["reports"][0])

    def test_cloud_upload_payload_includes_structured_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "reviewer-value-report.md"
            report.write_text("# Report\n", encoding="utf-8")
            code_mower_cloud.build_cloud_bundle(
                reports=[(report, "value-report")],
                events=[
                    {
                        "event_type": "reviewer_run",
                        "repo_slug": "owner/repo",
                        "provider": "codex",
                        "lens": "base",
                        "status": "pass",
                        "metrics": {"latency_ms": 1234},
                    }
                ],
                output_dir=root / "bundle",
                repo_slug="owner/repo",
            )

            payload = code_mower_cloud.build_upload_payload(
                bundle_dir=root / "bundle",
                include_reports=False,
            )

            self.assertEqual(payload["events"][0]["schema"], code_mower_cloud.EVENT_SCHEMA)
            self.assertEqual(payload["events"][0]["event_type"], "reviewer_run")
            self.assertEqual(payload["events"][0]["provider"], "codex")
            self.assertEqual(payload["events"][0]["metrics"]["latency_ms"], 1234)

    def test_cloud_upload_payload_allows_token_count_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "reviewer-value-report.md"
            report.write_text("# Report\n", encoding="utf-8")
            code_mower_cloud.build_cloud_bundle(
                reports=[(report, "value-report")],
                events=[
                    {
                        "event_type": "reviewer_run",
                        "provider": "codex",
                        "metrics": {
                            "input_tokens": 100,
                            "output_tokens": 20,
                            "total_tokens": 120,
                        },
                    }
                ],
                output_dir=root / "bundle",
                repo_slug="owner/repo",
            )

            payload = code_mower_cloud.build_upload_payload(
                bundle_dir=root / "bundle",
                include_reports=False,
            )

            self.assertEqual(payload["events"][0]["metrics"]["total_tokens"], 120)

    def test_cloud_export_rejects_unsafe_structured_event_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "reviewer-value-report.md"
            report.write_text("# Report\n", encoding="utf-8")

            with self.assertRaisesRegex(
                code_mower_cloud.CloudBundleError,
                "unsafe field",
            ):
                code_mower_cloud.build_cloud_bundle(
                    reports=[(report, "value-report")],
                    events=[
                        {
                            "event_type": "reviewer_run",
                            "provider": "claude",
                            "dimensions": {
                                "auth_preview": '{"loggedIn": true, "email": "user@example.com"}',
                            },
                        }
                    ],
                    output_dir=root / "bundle",
                    repo_slug="owner/repo",
                )

    def test_cloud_export_rejects_raw_output_structured_event_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "reviewer-value-report.md"
            report.write_text("# Report\n", encoding="utf-8")

            with self.assertRaisesRegex(
                code_mower_cloud.CloudBundleError,
                "unsafe field",
            ):
                code_mower_cloud.build_cloud_bundle(
                    reports=[(report, "value-report")],
                    events=[
                        {
                            "event_type": "reviewer_run",
                            "provider": "codex",
                            "metrics": {
                                "raw_output": "full terminal output",
                            },
                        }
                    ],
                    output_dir=root / "bundle",
                    repo_slug="owner/repo",
                )

    def test_cloud_metadata_validator_rejects_camel_case_unsafe_keys(self) -> None:
        unsafe_keys = (
            "authPreview",
            "rawOutput",
            "apiKey",
            "privateKey",
            "sourceCode",
            "accessToken",
            "auth preview",
            "raw-output",
        )
        for key in unsafe_keys:
            with self.subTest(key=key):
                with self.assertRaisesRegex(
                    code_mower_cloud.CloudBundleError,
                    "unsafe field",
                ):
                    code_mower_cloud.validate_metadata_payload({"metrics": {key: "value"}})

    def test_cloud_export_rejects_malformed_metric_secret_before_scrub(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "reviewer-value-report.md"
            report.write_text("# Report\n", encoding="utf-8")

            with self.assertRaisesRegex(
                code_mower_cloud.CloudBundleError,
                "secret-like value",
            ):
                code_mower_cloud.build_cloud_bundle(
                    reports=[(report, "value-report")],
                    events=[
                        {
                            "event_type": "reviewer_run",
                            "provider": "codex",
                            "metrics": "Authorization: Bearer abcdefghijklmnop",
                        }
                    ],
                    output_dir=root / "bundle",
                    repo_slug="owner/repo",
                )

    def test_cloud_export_rejects_non_object_event_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "reviewer-value-report.md"
            report.write_text("# Report\n", encoding="utf-8")

            with self.assertRaisesRegex(
                code_mower_cloud.CloudBundleError,
                "metrics must be an object",
            ):
                code_mower_cloud.build_cloud_bundle(
                    reports=[(report, "value-report")],
                    events=[
                        {
                            "event_type": "reviewer_run",
                            "provider": "codex",
                            "metrics": "not an object",
                        }
                    ],
                    output_dir=root / "bundle",
                    repo_slug="owner/repo",
                )

    def test_cloud_upload_rejects_tampered_manifest_secret_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "reviewer-value-report.md"
            report.write_text("# Report\n", encoding="utf-8")
            bundle = root / "bundle"
            code_mower_cloud.build_cloud_bundle(
                reports=[(report, "value-report")],
                output_dir=bundle,
                repo_slug="owner/repo",
            )
            manifest_path = bundle / code_mower_cloud.BUNDLE_MANIFEST_FILENAME
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["events"] = [
                {
                    "schema": code_mower_cloud.EVENT_SCHEMA,
                    "event_type": "reviewer_run",
                    "event_id": "evt-test",
                    "created_at": "2026-06-15T00:00:00+00:00",
                    "provider": "codex",
                    "metrics": {
                        "detail": "Authorization: Bearer abcdefghijklmnop",
                    },
                }
            ]
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                code_mower_cloud.CloudBundleError,
                "secret-like value",
            ):
                code_mower_cloud.build_upload_payload(
                    bundle_dir=bundle,
                    include_reports=False,
                )

    def test_cloud_upload_rejects_tampered_manifest_identity_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "reviewer-value-report.md"
            report.write_text("# Report\n", encoding="utf-8")
            bundle = root / "bundle"
            code_mower_cloud.build_cloud_bundle(
                reports=[(report, "value-report")],
                output_dir=bundle,
                repo_slug="owner/repo",
            )
            manifest_path = bundle / code_mower_cloud.BUNDLE_MANIFEST_FILENAME
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["repo_slug"] = "cmw_live_secret_value_should_not_upload"
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                code_mower_cloud.CloudBundleError,
                "secret-like value",
            ):
                code_mower_cloud.build_upload_payload(
                    bundle_dir=bundle,
                    include_reports=False,
                )

    def test_cloud_metadata_validator_rejects_deep_nesting_cleanly(self) -> None:
        value: object = "safe"
        for _ in range(40):
            value = {"nested": value}

        with self.assertRaisesRegex(
            code_mower_cloud.CloudBundleError,
            "too deeply nested",
        ):
            code_mower_cloud.validate_metadata_payload(value)

    def test_cloud_anonymous_bundle_strips_structured_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            event = root / "event.json"
            event.write_text(
                json.dumps({"event_type": "dogfood_upload", "repo_slug": "owner/repo"}),
                encoding="utf-8",
            )
            code_mower_cloud.build_cloud_bundle(
                reports=[],
                events=code_mower_cloud._parse_event_args(
                    [f"dogfood_upload={event}"]
                ),
                output_dir=root / "bundle",
                repo_slug="owner/repo",
                anonymous=True,
            )

            payload = code_mower_cloud.build_upload_payload(
                bundle_dir=root / "bundle",
                include_reports=False,
            )

            self.assertEqual(payload["events"], [])

    def test_cloud_dogfood_dry_run_adds_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test"],
                cwd=root,
                check=True,
            )
            docs = root / "docs"
            docs.mkdir()
            (docs / "reviewer-value-report.md").write_text("# Report\n", encoding="utf-8")
            (root / "README.md").write_text("fixture\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "fixture"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            token_env = "CODE_MOWER_TEST_DOGFOOD_TOKEN"
            old_token = os.environ.get(token_env)
            os.environ[token_env] = "test-token"
            try:
                result = code_mower_cloud._dogfood_upload(
                    repo_path=root,
                    output_dir=root / ".code-mower/cloud-benchmark-bundle",
                    reports=[],
                    events=[],
                    spend_path=None,
                    repo_slug="owner/repo",
                    team_id="team",
                    install_id="install",
                    source="test",
                    endpoint="https://codemower.com/api/ingest",
                    token_env=token_env,
                    include_reports=False,
                    yes=False,
                    timeout=0.1,
                )
            finally:
                if old_token is None:
                    os.environ.pop(token_env, None)
                else:
                    os.environ[token_env] = old_token

            self.assertEqual(result["status"], "dry_run")
            self.assertEqual(
                result["upload"]["event_count"],
                1 + len(REFERENCE_PROVIDERS),
            )
            self.assertEqual(
                result["export"]["event_count"],
                1 + len(REFERENCE_PROVIDERS),
            )
            self.assertEqual(
                result["upload"]["event_types"]["provider_catalog_snapshot"],
                len(REFERENCE_PROVIDERS),
            )
            self.assertTrue(result["upload"]["requires_yes"])

    def test_cloud_dogfood_cli_is_dry_run_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            (root / "README.md").write_text("fixture\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "fixture"], cwd=root, check=True, capture_output=True)

            output = StringIO()
            with redirect_stdout(output):
                code = code_mower_cloud.main(
                    [
                        "dogfood",
                        "--repo-path",
                        str(root),
                        "--output-dir",
                        str(root / ".code-mower/cloud-benchmark-bundle"),
                        "--repo-slug",
                        "owner/repo",
                        "--endpoint",
                        "http://localhost:3000/api/ingest",
                        "--json",
                    ]
                )

            self.assertEqual(code, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["status"], "dry_run")
            self.assertEqual(payload["upload"]["mode"], "cloud-upload-dry-run")
            self.assertFalse(payload["upload"]["would_upload"])

    def test_cloud_repo_sync_cli_dry_runs_default_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test"],
                cwd=root,
                check=True,
            )
            (root / "README.md").write_text("fixture\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "fixture"],
                cwd=root,
                check=True,
                capture_output=True,
            )

            output = StringIO()
            with redirect_stdout(output):
                code = code_mower_cloud.main(
                    [
                        "repo-sync",
                        "--repo",
                        f"owner/repo={root}",
                        "--output-dir",
                        str(root / ".code-mower/cloud-repo-sync"),
                        "--endpoint",
                        "http://localhost:3000/api/ingest",
                        "--json",
                    ]
                )

            self.assertEqual(code, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["status"], "dry_run")
            self.assertEqual(payload["repo_count"], 1)
            self.assertEqual(payload["step_count"], 2)
            self.assertEqual(payload["error_count"], 0)
            self.assertEqual(payload["modes"], ["dogfood", "reviewer-runs"])
            steps = payload["repos"][0]["steps"]
            self.assertEqual(steps[0]["status"], "dry_run")
            self.assertEqual(
                steps[0]["upload"]["event_count"],
                1 + len(REFERENCE_PROVIDERS),
            )
            self.assertEqual(
                steps[0]["upload"]["event_types"]["provider_catalog_snapshot"],
                len(REFERENCE_PROVIDERS),
            )
            self.assertEqual(steps[1]["status"], "no_events")

    def test_cloud_repo_sync_cli_text_summarizes_data_classes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test"],
                cwd=root,
                check=True,
            )
            (root / "README.md").write_text("fixture\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "fixture"],
                cwd=root,
                check=True,
                capture_output=True,
            )

            output = StringIO()
            with redirect_stdout(output):
                code = code_mower_cloud.main(
                    [
                        "repo-sync",
                        "--repo",
                        f"owner/repo={root}",
                        "--output-dir",
                        str(root / ".code-mower/cloud-repo-sync"),
                        "--endpoint",
                        "http://localhost:3000/api/ingest",
                    ]
                )

            self.assertEqual(code, 0)
            text = output.getvalue()
            self.assertIn("Code Mower cloud repo sync", text)
            self.assertIn("Current dogfood: 1 steps, ", text)
            self.assertIn("Imported history: 0 steps, 0 events (not calibration evidence)", text)
            self.assertIn("Reviewer evidence: 1 steps, 0 events", text)

    def test_cloud_repo_sync_reviewer_only_no_events_is_not_uploaded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test"],
                cwd=root,
                check=True,
            )
            (root / "README.md").write_text("fixture\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "fixture"],
                cwd=root,
                check=True,
                capture_output=True,
            )

            output = StringIO()
            with redirect_stdout(output):
                code = code_mower_cloud.main(
                    [
                        "repo-sync",
                        "--repo",
                        f"owner/repo={root}",
                        "--mode",
                        "reviewer-runs",
                        "--output-dir",
                        str(root / ".code-mower/cloud-repo-sync"),
                        "--endpoint",
                        "http://localhost:3000/api/ingest",
                        "--yes",
                        "--json",
                    ]
                )

            self.assertEqual(code, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["status"], "no_events")
            self.assertEqual(payload["step_count"], 1)
            self.assertEqual(payload["repos"][0]["steps"][0]["status"], "no_events")

    def test_cloud_repo_sync_output_names_are_unique_by_index(self) -> None:
        first = code_mower_cloud._repo_sync_output_name(
            "owner/repo", Path("/tmp/a/repo"), 0
        )
        second = code_mower_cloud._repo_sync_output_name(
            "owner/repo", Path("/tmp/b/repo"), 1
        )

        self.assertEqual(first, "owner--repo-1")
        self.assertEqual(second, "owner--repo-2")
        self.assertNotEqual(first, second)

    def test_cloud_dogfood_dry_run_does_not_require_production_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            (root / "README.md").write_text("fixture\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "fixture"], cwd=root, check=True, capture_output=True)
            token_env = "CODE_MOWER_TEST_DOGFOOD_MISSING_TOKEN"
            os.environ.pop(token_env, None)

            output = StringIO()
            with redirect_stdout(output):
                code = code_mower_cloud.main(
                    [
                        "dogfood",
                        "--repo-path",
                        str(root),
                        "--output-dir",
                        str(root / ".code-mower/cloud-benchmark-bundle"),
                        "--repo-slug",
                        "owner/repo",
                        "--endpoint",
                        "https://codemower.com/api/ingest",
                        "--token-env",
                        token_env,
                        "--json",
                    ]
                )

            self.assertEqual(code, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["status"], "dry_run")
            token_check = next(
                check for check in payload["doctor"]["checks"] if check["name"] == "token"
            )
            self.assertEqual(token_check["status"], "warn")
            self.assertFalse(payload["upload"]["would_upload"])

    def test_cloud_setup_cli_dry_run_redacts_token(self) -> None:
        output = StringIO()
        token = "cmw_live_cli_secret_never_print"
        with redirect_stdout(output):
            code = code_mower_cloud.main(
                [
                    "setup",
                    "--token",
                    token,
                    "--team-id",
                    "team",
                    "--install-id",
                    "install",
                    "--dry-run",
                    "--json",
                ]
            )

        self.assertEqual(code, 0)
        encoded = output.getvalue()
        self.assertNotIn(token, encoded)
        self.assertIn("cmw_live_cli...", encoded)

    def test_cloud_setup_writes_private_env_file_without_echoing_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token = "cmw_live_test_secret_token"
            target = root / "tokens" / "codex-code-mower.env"

            result = code_mower_cloud.run_cloud_setup(
                token=token,
                token_file=None,
                token_stdin=False,
                token_env="CODE_MOWER_TEST_CLOUD_TOKEN",
                endpoint="https://codemower.com/api/ingest",
                team_id="jeff-internal",
                install_id="codex-code-mower",
                out=target,
                force=False,
                dry_run=False,
            )

            self.assertEqual(result["status"], "written")
            self.assertEqual(result["path"], str(target))
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            text = target.read_text(encoding="utf-8")
            self.assertIn("export CODE_MOWER_CLOUD_TOKEN=", text)
            self.assertIn("export CODE_MOWER_CLOUD_TEAM_ID=jeff-internal", text)
            self.assertNotIn(token, json.dumps(result))

    def test_cloud_setup_token_prefix_never_echoes_full_short_token(self) -> None:
        for token in ("abc", "short13chars!", "sixteen-char-tok"):
            with self.subTest(token=token):
                prefix = code_mower_cloud._token_prefix(token)
                self.assertNotIn(token, prefix)
                if prefix != "<redacted>":
                    self.assertLess(len(prefix.removesuffix("...")), len(token))

    def test_cloud_setup_refuses_overwrite_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "token.env"
            target.write_text("existing\n", encoding="utf-8")

            with self.assertRaises(code_mower_cloud.CloudBundleError):
                code_mower_cloud.run_cloud_setup(
                    token="cmw_live_test_secret_token",
                    token_file=None,
                    token_stdin=False,
                    token_env="CODE_MOWER_TEST_CLOUD_TOKEN",
                    endpoint="https://codemower.com/api/ingest",
                    team_id="team",
                    install_id="install",
                    out=target,
                    force=False,
                    dry_run=False,
                )

    def test_cloud_setup_token_file_parses_export_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token_file = root / "source.env"
            token_file.write_text(
                "export CODE_MOWER_CLOUD_TOKEN='cmw_live_from_file'\n",
                encoding="utf-8",
            )
            target = root / "out.env"

            result = code_mower_cloud.run_cloud_setup(
                token="",
                token_file=token_file,
                token_stdin=False,
                token_env="CODE_MOWER_TEST_CLOUD_TOKEN",
                endpoint="https://codemower.com/api/ingest",
                team_id="team",
                install_id="install",
                out=target,
                force=False,
                dry_run=False,
            )

            self.assertEqual(result["status"], "written")
            self.assertIn("cmw_live_from_file", target.read_text(encoding="utf-8"))

    def test_cloud_upload_payload_includes_reports_only_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "reviewer-value-report.md"
            report.write_text("# Report\n\nsafe aggregate data\n", encoding="utf-8")
            code_mower_cloud.build_cloud_bundle(
                reports=[(report, "value-report")],
                output_dir=root / "bundle",
                anonymous=True,
            )

            payload = code_mower_cloud.build_upload_payload(
                bundle_dir=root / "bundle",
                include_reports=True,
            )

            self.assertEqual(payload["upload_mode"], "reports_included")
            self.assertIn("safe aggregate data", payload["reports"][0]["text"])

    def test_cloud_upload_rejects_non_https_non_local_endpoint(self) -> None:
        for endpoint in (
            "http://example.com/api/ingest",
            "http://localhost.evil.test/api/ingest",
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(code_mower_cloud.CloudBundleError):
                    code_mower_cloud.post_upload_payload(
                        payload={"schema": code_mower_cloud.UPLOAD_SCHEMA},
                        endpoint=endpoint,
                        timeout=0.1,
                    )

    def test_cloud_doctor_fails_for_missing_production_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "reviewer-value-report.md"
            report.write_text("# Report\n", encoding="utf-8")
            code_mower_cloud.build_cloud_bundle(
                reports=[(report, "value-report")],
                output_dir=root / "bundle",
                anonymous=True,
            )
            token_env = "CODE_MOWER_TEST_MISSING_TOKEN"
            os.environ.pop(token_env, None)

            payload = code_mower_cloud.run_cloud_doctor(
                bundle_dir=root / "bundle",
                endpoint="https://codemower.com/api/ingest",
                token_env=token_env,
            )

            self.assertEqual(payload["status"], "fail")
            self.assertEqual(payload["failures"], 1)
            token_check = next(
                check for check in payload["checks"] if check["name"] == "token"
            )
            self.assertEqual(token_check["status"], "fail")

    def test_cloud_doctor_allows_local_missing_token_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            token_env = "CODE_MOWER_TEST_MISSING_LOCAL_TOKEN"
            os.environ.pop(token_env, None)

            payload = code_mower_cloud.run_cloud_doctor(
                bundle_dir=Path(tmp) / "bundle",
                endpoint="http://localhost:3000/api/ingest",
                token_env=token_env,
            )

            self.assertEqual(payload["status"], "pass")
            token_check = next(
                check for check in payload["checks"] if check["name"] == "token"
            )
            self.assertEqual(token_check["status"], "warn")
            self.assertIn("local configless ingest", token_check["message"])

    def test_cloud_doctor_reports_dashboard_and_next_steps_without_token_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            token_env = "CODE_MOWER_TEST_STATUS_TOKEN"
            os.environ[token_env] = "cmw_live_secret_for_status"
            try:
                payload = code_mower_cloud.run_cloud_doctor(
                    bundle_dir=Path(tmp) / "bundle",
                    endpoint="https://codemower.com/api/ingest",
                    token_env=token_env,
                )
            finally:
                os.environ.pop(token_env, None)

        encoded = json.dumps(payload)
        self.assertEqual(payload["dashboard_url"], "https://codemower.com/dashboard")
        self.assertEqual(payload["health_url"], "https://codemower.com/api/health")
        self.assertIn("next_steps", payload)
        self.assertIn("cloud setup --token-stdin", encoded)
        self.assertNotIn("cmw_live_secret_for_status", encoded)

    def test_cloud_doctor_service_probe_uses_endpoint_health_url(self) -> None:
        captured: dict[str, str] = {}

        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return b'{"ok": true, "app": "codemower.com", "supabaseConfigured": true}'

            def getcode(self) -> int:
                return 200

        def fake_urlopen(request: object, timeout: float = 0) -> FakeResponse:
            captured["url"] = getattr(request, "full_url", "")
            captured["timeout"] = str(timeout)
            return FakeResponse()

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "urllib.request.urlopen", side_effect=fake_urlopen
        ):
            token_env = "CODE_MOWER_TEST_STATUS_TOKEN"
            os.environ[token_env] = "cmw_live_secret_for_status"
            try:
                payload = code_mower_cloud.run_cloud_doctor(
                    bundle_dir=Path(tmp) / "bundle",
                    endpoint="https://codemower.com/api/ingest",
                    token_env=token_env,
                    probe_service=True,
                    timeout=0.25,
                )
            finally:
                os.environ.pop(token_env, None)

        service_check = next(check for check in payload["checks"] if check["name"] == "service")
        self.assertEqual(captured["url"], "https://codemower.com/api/health")
        self.assertEqual(service_check["status"], "pass")
        self.assertEqual(service_check["detail"]["app"], "codemower.com")
        self.assertTrue(service_check["detail"]["supabaseConfigured"])

    def test_cloud_doctor_service_probe_failure_is_a_failed_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("offline"),
        ):
            token_env = "CODE_MOWER_TEST_STATUS_TOKEN"
            os.environ[token_env] = "cmw_live_secret_for_status"
            try:
                payload = code_mower_cloud.run_cloud_doctor(
                    bundle_dir=Path(tmp) / "bundle",
                    endpoint="https://codemower.com/api/ingest",
                    token_env=token_env,
                    probe_service=True,
                    timeout=0.25,
                )
            finally:
                os.environ.pop(token_env, None)

        service_check = next(check for check in payload["checks"] if check["name"] == "service")
        self.assertEqual(payload["status"], "fail")
        self.assertEqual(service_check["status"], "fail")
        self.assertIn("offline", service_check["message"])
        self.assertNotIn("cmw_live_secret_for_status", json.dumps(payload))

    def test_cloud_upload_command_requires_token_for_production(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "reviewer-value-report.md"
            report.write_text("# Report\n", encoding="utf-8")
            code_mower_cloud.build_cloud_bundle(
                reports=[(report, "value-report")],
                output_dir=root / "bundle",
                anonymous=True,
            )
            token_env = "CODE_MOWER_TEST_MISSING_UPLOAD_TOKEN"
            os.environ.pop(token_env, None)

            status = code_mower_cloud.main(
                [
                    "upload",
                    str(root / "bundle"),
                    "--endpoint",
                    "https://codemower.com/api/ingest",
                    "--token-env",
                    token_env,
                    "--yes",
                    "--json",
                ]
            )

            self.assertEqual(status, 1)

    def test_cloud_export_examples_include_lane_policy(self) -> None:
        smoke_text = (ROOT / "scripts/smoke_easy_mode.py").read_text(encoding="utf-8")
        package_text = "\n".join(
            [
                (ROOT / "src/code_mower/package.py").read_text(encoding="utf-8"),
                (ROOT / "src/code_mower/package_static.py").read_text(
                    encoding="utf-8"
                ),
            ]
        )
        migration_rehearsal_text = (
            ROOT / "src/code_mower/migration_rehearsal.py"
        ).read_text(encoding="utf-8")
        for text in (smoke_text, package_text, migration_rehearsal_text):
            with self.subTest():
                self.assertIn("lane-policy=lane-policy.json", text)
                self.assertIn("cloud-upload-dry-run.json", text)

    def test_package_install_rehearsal_covers_first_user_artifacts(self) -> None:
        migration_text = "\n".join(
            [
                (ROOT / "src/code_mower/migration.py").read_text(encoding="utf-8"),
                (ROOT / "src/code_mower/migration_rehearsal.py").read_text(
                    encoding="utf-8"
                ),
                (ROOT / "src/code_mower/migration_readiness.py").read_text(
                    encoding="utf-8"
                ),
            ]
        )

        self.assertIn("first_user_artifacts", migration_text)
        self.assertIn("calibration-plan.json", migration_text)
        self.assertIn("auto-discovery-prs.json", migration_text)
        self.assertIn("draft-calibration-corpus.json", migration_text)
        self.assertIn("draft-reviewer-value-report.md", migration_text)
        self.assertIn("calibration-evidence.json", migration_text)
        self.assertIn("reviewer-metrics.json", migration_text)
        self.assertIn("lane-policy.json", migration_text)
        self.assertIn("reviewer-value-report.md", migration_text)
        self.assertIn("cloud-export.json", migration_text)
        self.assertIn("cloud-upload-dry-run.json", migration_text)
        self.assertIn("cloud-dogfood-dry-run.json", migration_text)
        self.assertIn("package-install-rehearsal", migration_text)
        self.assertIn("auto-discover", migration_text)
        self.assertIn(".code-mower/cloud-dogfood-bundle", migration_text)
        self.assertIn("example/toy-repo", migration_text)
        self.assertIn("http://localhost:3000/api/ingest", migration_text)
        self.assertIn("Value report:", migration_text)
        self.assertIn("Draft value report:", migration_text)
        self.assertIn("first_user_readiness", migration_text)
        self.assertIn("first-user-readiness.json", migration_text)
        self.assertIn("First-user readiness:", migration_text)
        self.assertIn("cloud-upload-dry-run-privacy", migration_text)
        self.assertIn("external_repo_readiness", migration_text)
        self.assertIn("checks_dry_run", migration_text)

    def test_package_install_rehearsal_render_handles_external_repo_readiness(
        self,
    ) -> None:
        text = code_mower_migration.render_package_install_rehearsal_text(
            {
                "status": "pass",
                "package_spec": "code-mower",
                "version": "code-mower 0.5.0b53",
                "work_dir": "/tmp/code-mower-rehearsal",
                "toy_repo": "/tmp/code-mower-rehearsal/toy-repo",
                "step_count": 1,
                "first_user_readiness": {},
                "first_user_artifacts": {},
                "repo_path": "/tmp/external-repo",
                "product_wrapper_rehearsal": None,
                "product_mirror_removal_plan": None,
                "external_repo_readiness": {
                    "status": "pass",
                    "check_count": 3,
                },
            }
        )

        self.assertIn("Product wrapper rehearsal: not-run", text)
        self.assertIn("Product mirror-removal status: not-run", text)
        self.assertIn("External repo readiness: pass (3 native checks detected)", text)

    def test_package_install_rehearsal_artifact_contract_is_structured(self) -> None:
        toy_repo = Path("/tmp/code-mower-example-toy-repo")
        artifacts = code_mower_migration._first_user_artifacts(toy_repo)

        self.assertEqual(
            artifacts,
            {
                "calibration_plan": (
                    "/tmp/code-mower-example-toy-repo/.code-mower/calibration-plan.json"
                ),
                "draft_calibration_corpus": (
                    "/tmp/code-mower-example-toy-repo/.code-mower/draft-calibration-corpus.json"
                ),
                "draft_reviewer_value_report": (
                    "/tmp/code-mower-example-toy-repo/.code-mower/draft-reviewer-value-report.md"
                ),
                "calibration_evidence": (
                    "/tmp/code-mower-example-toy-repo/calibration-evidence.json"
                ),
                "reviewer_metrics": (
                    "/tmp/code-mower-example-toy-repo/reviewer-metrics.json"
                ),
                "lane_policy": "/tmp/code-mower-example-toy-repo/lane-policy.json",
                "reviewer_value_report": (
                    "/tmp/code-mower-example-toy-repo/reviewer-value-report.md"
                ),
                "cloud_export": "/tmp/code-mower-example-toy-repo/cloud-export.json",
                "cloud_upload_dry_run": (
                    "/tmp/code-mower-example-toy-repo/cloud-upload-dry-run.json"
                ),
                "cloud_dogfood_dry_run": (
                    "/tmp/code-mower-example-toy-repo/cloud-dogfood-dry-run.json"
                ),
            },
        )

    def test_first_user_readiness_scorecard_passes_for_complete_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            toy_repo = root / "toy-repo"
            outputs = root / "outputs"
            generated = toy_repo / ".code-mower.generated"
            (generated / "tools").mkdir(parents=True)
            outputs.mkdir()
            for path in (
                generated / "code-mower-init-plan.json",
                generated / "smoke-tests.sh",
                generated / "tools" / "code_mower",
            ):
                path.write_text("ok\n", encoding="utf-8")
            artifacts = code_mower_migration._first_user_artifacts(toy_repo)
            for key in (
                "draft_calibration_corpus",
                "draft_reviewer_value_report",
                "reviewer_value_report",
            ):
                Path(artifacts[key]).parent.mkdir(parents=True, exist_ok=True)
                Path(artifacts[key]).write_text("ok\n", encoding="utf-8")
            excluded_content = sorted(code_mower_migration.PRIVACY_EXCLUDED_CONTENT)
            Path(artifacts["cloud_export"]).write_text(
                json.dumps(
                    {
                        "mode": "cloud-export",
                        "included_reports": [
                            {"kind": "reviewer-metrics"},
                            {"kind": "lane-policy"},
                            {"kind": "value-report"},
                        ],
                        "upload_ready": True,
                    }
                ),
                encoding="utf-8",
            )
            dry_run_upload = {
                "mode": "cloud-upload-dry-run",
                "privacy_mode": "metadata_and_reports",
                "requires_yes": True,
                "would_upload": False,
                "excluded_content": excluded_content,
            }
            Path(artifacts["cloud_upload_dry_run"]).write_text(
                json.dumps(dry_run_upload),
                encoding="utf-8",
            )
            Path(artifacts["cloud_dogfood_dry_run"]).write_text(
                json.dumps({"status": "dry_run", "upload": dry_run_upload}),
                encoding="utf-8",
            )

            scorecard = code_mower_migration._first_user_readiness_scorecard(
                toy_repo=toy_repo,
                outputs=outputs,
                version="code-mower 0.5.0b53",
                steps=[
                    {
                        "command": ["code-mower", "doctor", "--easy", "--json"],
                        "returncode": 0,
                    }
                ],
            )

        self.assertEqual(scorecard["status"], "pass")
        self.assertEqual(scorecard["passed"], scorecard["total"])
        self.assertEqual(scorecard["failed"], 0)
        self.assertEqual(
            {check["id"] for check in scorecard["checks"]},
            {
                "package-installed",
                "easy-init-generated",
                "doctor-ran",
                "draft-calibration-corpus",
                "draft-value-report",
                "starter-value-report",
                "cloud-export-metadata-bundle",
                "cloud-upload-dry-run-privacy",
                "cloud-dogfood-dry-run",
                "cloud-dogfood-upload-privacy",
            },
        )

    def test_first_user_readiness_scorecard_fails_open_on_privacy_regression(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            toy_repo = root / "toy-repo"
            outputs = root / "outputs"
            generated = toy_repo / ".code-mower.generated"
            (generated / "tools").mkdir(parents=True)
            outputs.mkdir()
            for path in (
                generated / "code-mower-init-plan.json",
                generated / "smoke-tests.sh",
                generated / "tools" / "code_mower",
            ):
                path.write_text("ok\n", encoding="utf-8")
            artifacts = code_mower_migration._first_user_artifacts(toy_repo)
            for key in (
                "draft_calibration_corpus",
                "draft_reviewer_value_report",
                "reviewer_value_report",
            ):
                Path(artifacts[key]).parent.mkdir(parents=True, exist_ok=True)
                Path(artifacts[key]).write_text("ok\n", encoding="utf-8")
            Path(artifacts["cloud_export"]).write_text(
                json.dumps(
                    {
                        "mode": "cloud-export",
                        "included_reports": [{}, {}, {}],
                        "upload_ready": True,
                    }
                ),
                encoding="utf-8",
            )
            unsafe_upload = {
                "mode": "cloud-upload-dry-run",
                "privacy_mode": "metadata_and_reports",
                "requires_yes": True,
                "would_upload": True,
                "excluded_content": ["source_code"],
            }
            Path(artifacts["cloud_upload_dry_run"]).write_text(
                json.dumps(unsafe_upload),
                encoding="utf-8",
            )
            Path(artifacts["cloud_dogfood_dry_run"]).write_text(
                json.dumps({"status": "dry_run", "upload": unsafe_upload}),
                encoding="utf-8",
            )

            scorecard = code_mower_migration._first_user_readiness_scorecard(
                toy_repo=toy_repo,
                outputs=outputs,
                version="code-mower 0.5.0b53",
                steps=[
                    {
                        "command": ["code-mower", "doctor", "--easy", "--json"],
                        "returncode": 0,
                    }
                ],
            )

        self.assertEqual(scorecard["status"], "fail")
        failed = {check["id"]: check for check in scorecard["checks"] if check["status"] == "fail"}
        self.assertIn("cloud-upload-dry-run-privacy", failed)
        self.assertIn("cloud-dogfood-upload-privacy", failed)
        self.assertIn("raw_diffs", failed["cloud-upload-dry-run-privacy"]["detail"]["missing_exclusions"])

    def test_first_user_readiness_scorecard_fails_open_on_malformed_cloud_export(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            toy_repo = root / "toy-repo"
            outputs = root / "outputs"
            generated = toy_repo / ".code-mower.generated"
            (generated / "tools").mkdir(parents=True)
            outputs.mkdir()
            for path in (
                generated / "code-mower-init-plan.json",
                generated / "smoke-tests.sh",
                generated / "tools" / "code_mower",
            ):
                path.write_text("ok\n", encoding="utf-8")
            artifacts = code_mower_migration._first_user_artifacts(toy_repo)
            for key in (
                "draft_calibration_corpus",
                "draft_reviewer_value_report",
                "reviewer_value_report",
            ):
                Path(artifacts[key]).parent.mkdir(parents=True, exist_ok=True)
                Path(artifacts[key]).write_text("ok\n", encoding="utf-8")
            Path(artifacts["cloud_export"]).write_text(
                json.dumps([{"mode": "cloud-export"}]),
                encoding="utf-8",
            )
            dry_run_upload = {
                "mode": "cloud-upload-dry-run",
                "privacy_mode": "metadata_and_reports",
                "requires_yes": True,
                "would_upload": False,
                "excluded_content": sorted(code_mower_migration.PRIVACY_EXCLUDED_CONTENT),
            }
            Path(artifacts["cloud_upload_dry_run"]).write_text(
                json.dumps(dry_run_upload),
                encoding="utf-8",
            )
            Path(artifacts["cloud_dogfood_dry_run"]).write_text(
                json.dumps({"status": "dry_run", "upload": dry_run_upload}),
                encoding="utf-8",
            )

            scorecard = code_mower_migration._first_user_readiness_scorecard(
                toy_repo=toy_repo,
                outputs=outputs,
                version="code-mower 0.5.0b53",
                steps=[
                    {
                        "command": ["code-mower", "doctor", "--easy", "--json"],
                        "returncode": 0,
                    }
                ],
            )

        self.assertEqual(scorecard["status"], "fail")
        failed = {check["id"] for check in scorecard["checks"] if check["status"] == "fail"}
        self.assertEqual(failed, {"cloud-export-metadata-bundle"})

    def test_rehearsal_step_to_file_writes_stdout_and_creates_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "nested" / "stdout.json"
            completed = subprocess.CompletedProcess(
                ["code-mower", "example"],
                0,
                stdout='{"status":"pass"}\n',
                stderr="",
            )

            with mock.patch.object(
                code_mower_migration_install,
                "_run_rehearsal_step",
                return_value=completed,
            ) as run_step:
                result = code_mower_migration_install._run_rehearsal_step_to_file(
                    ["code-mower", "example"],
                    cwd=Path(tmp),
                    env={"PATH": os.defpath},
                    steps=[],
                    timeout=12,
                    stdout_path=output,
                )

            self.assertIs(result, completed)
            self.assertEqual(output.read_text(encoding="utf-8"), '{"status":"pass"}\n')
            run_step.assert_called_once_with(
                ["code-mower", "example"],
                cwd=Path(tmp),
                env={"PATH": os.defpath},
                steps=[],
                timeout=12,
            )

    def test_package_install_rehearsal_resolves_local_package_specs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "code-mower-source"
            package.mkdir()
            (package / "pyproject.toml").write_text(
                "[project]\nname = 'x'\n",
                encoding="utf-8",
            )

            self.assertEqual(
                code_mower_migration._resolve_install_package_spec(
                    ".",
                    base_dir=package,
                ),
                str(package.resolve()),
            )
            self.assertEqual(
                code_mower_migration._resolve_install_package_spec(
                    "git+https://github.com/codemower-ai/code-mower.git@v0.5.0-beta.53",
                    base_dir=package,
                ),
                "git+https://github.com/codemower-ai/code-mower.git@v0.5.0-beta.53",
            )
            self.assertEqual(
                code_mower_migration._resolve_install_package_spec(
                    "code-mower==0.5.0b53",
                    base_dir=package,
                ),
                "code-mower==0.5.0b53",
            )

    def test_wrapper_rehearsal_command_parser_preserves_quoted_spaces(self) -> None:
        self.assertEqual(
            code_mower_migration._resolve_command(
                "'/tmp/Code Mower/.venv/bin/code-mower' --example"
            ),
            ("/tmp/Code Mower/.venv/bin/code-mower", "--example"),
        )
        with tempfile.TemporaryDirectory(prefix="Code Mower ") as tmp:
            executable = Path(tmp) / "code-mower"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)

            self.assertEqual(
                code_mower_migration._resolve_command(str(executable)),
                (str(executable.resolve()),),
            )

    def test_package_install_rehearsal_supports_index_aware_pip_install(self) -> None:
        self.assertEqual(
            code_mower_migration._pip_install_command(
                Path("/tmp/venv/bin/python"),
                "code-mower==0.5.0b53",
                pip_index_url="https://test.pypi.org/simple/",
                pip_extra_index_urls=["https://pypi.org/simple/"],
            ),
            [
                "/tmp/venv/bin/python",
                "-m",
                "pip",
                "install",
                "--index-url",
                "https://test.pypi.org/simple/",
                "--extra-index-url",
                "https://pypi.org/simple/",
                "code-mower==0.5.0b53",
            ],
        )

    def test_package_install_rehearsal_resolves_python_command_from_path(self) -> None:
        with mock.patch("shutil.which", return_value="/opt/example/bin/python3.12"):
            self.assertEqual(
                code_mower_migration._resolve_python_executable(Path("python3.12")),
                Path("/opt/example/bin/python3.12"),
            )

    def test_package_install_rehearsal_rejects_missing_python_command(self) -> None:
        with mock.patch("shutil.which", return_value=None):
            with self.assertRaisesRegex(ValueError, "Python executable not found on PATH"):
                code_mower_migration._resolve_python_executable(Path("python3.12"))

    def test_release_readiness_reports_package_index_promotion_gate(self) -> None:
        payload = release_readiness.render_release_readiness(ROOT)

        self.assertEqual(payload["status"], "pass")
        self.assertEqual(payload["version"], "0.5.0b53")
        self.assertEqual(payload["release_tag"], "v0.5.0-beta.53")
        self.assertEqual(payload["alpha_tag"], "v0.5.0-beta.53")
        self.assertEqual(payload["package_index_spec"], "code-mower==0.5.0b53")
        check_ids = {check["id"]: check for check in payload["checks"]}
        self.assertEqual(check_ids["package-version-consistency"]["status"], "pass")
        self.assertEqual(
            check_ids["materialized-package-version-consistency"]["status"],
            "pass",
        )
        self.assertEqual(check_ids["testpypi-gate"]["status"], "pass")
        self.assertEqual(check_ids["pypi-gate"]["status"], "pass")
        self.assertEqual(check_ids["trusted-publishing-runbook"]["status"], "pass")
        self.assertEqual(check_ids["ci-package-install-rehearsal"]["status"], "pass")
        self.assertEqual(check_ids["public-maintainer-docs"]["status"], "pass")
        self.assertEqual(check_ids["public-docs-linked-from-readme"]["status"], "pass")
        self.assertEqual(check_ids["public-support-redaction-guidance"]["status"], "pass")
        commands = {action["id"]: action["command"] for action in payload["next_actions"]}
        urls = {action["id"]: action.get("url", "") for action in payload["next_actions"]}
        self.assertIn("--ref v0.5.0-beta.53", commands["dry-run-release-workflow"])
        self.assertNotIn("--ref main", commands["dry-run-release-workflow"])
        self.assertIn("--ref v0.5.0-beta.53", commands["publish-testpypi-candidate"])
        self.assertNotIn("--ref main", commands["publish-testpypi-candidate"])
        self.assertIn("publish_testpypi=true", commands["publish-testpypi-candidate"])
        self.assertIn("publish_pypi=false", commands["publish-testpypi-candidate"])
        self.assertIn("--pip-index-url https://test.pypi.org/simple/", commands["testpypi-install-rehearsal"])
        self.assertEqual(
            payload["setup_urls"]["github_environments"],
            "https://github.com/codemower-ai/code-mower/settings/environments",
        )
        self.assertEqual(
            payload["setup_urls"]["testpypi_trusted_publishers"],
            "https://test.pypi.org/manage/project/code-mower/settings/publishing/",
        )
        self.assertEqual(
            urls["dry-run-release-workflow"],
            "https://github.com/codemower-ai/code-mower/actions/workflows/release.yml",
        )
        self.assertEqual(
            urls["testpypi-install-rehearsal"],
            "https://test.pypi.org/project/code-mower/",
        )

    def test_release_readiness_fails_on_materialized_package_version_drift(
        self,
    ) -> None:
        with mock.patch.object(
            release_readiness.package_module,
            "_init_py_text",
            return_value='"""Code Mower package."""\n\n__version__ = "0.0.0"\n',
        ):
            payload = release_readiness.render_release_readiness(ROOT)

        check_ids = {check["id"]: check for check in payload["checks"]}
        check = check_ids["materialized-package-version-consistency"]
        self.assertEqual(check["status"], "fail")
        self.assertEqual(check["detail"]["source_version"], "0.5.0b53")
        self.assertEqual(check["detail"]["generated_init_version"], "0.0.0")

    def test_public_support_docs_are_packaged_and_privacy_forward(self) -> None:
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        support = (ROOT / "SUPPORT.md").read_text(encoding="utf-8")
        conduct = (ROOT / "CODE_OF_CONDUCT.md").read_text(encoding="utf-8")

        self.assertIn("include CODE_OF_CONDUCT.md", manifest)
        self.assertIn("include SUPPORT.md", manifest)
        self.assertIn("[Support](SUPPORT.md)", readme)
        self.assertIn("[Security Policy](SECURITY.md)", readme)
        self.assertIn("[Code of Conduct](CODE_OF_CONDUCT.md)", readme)
        for text in (support, conduct):
            lowered = text.lower()
            self.assertIn("private source", lowered)
            self.assertIn("credentials", lowered)
        self.assertIn("raw model transcripts", support.lower())
        self.assertIn("raw model transcripts", conduct.lower())

    def test_public_hygiene_checks_fail_when_docs_or_links_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "README.md").write_text("No public support links yet.\n", encoding="utf-8")

            payload = release_readiness.render_release_readiness(repo)

        check_ids = {check["id"]: check for check in payload["checks"]}
        self.assertEqual(check_ids["public-maintainer-docs"]["status"], "fail")
        self.assertIn(
            "SUPPORT.md",
            check_ids["public-maintainer-docs"]["detail"]["missing_docs"],
        )
        self.assertEqual(check_ids["public-docs-linked-from-readme"]["status"], "fail")
        self.assertEqual(check_ids["public-support-redaction-guidance"]["status"], "fail")

    def test_public_redaction_guidance_requires_support_and_conduct(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "README.md").write_text(
                "\n".join(
                    [
                        "[Support](SUPPORT.md)",
                        "[Security Policy](SECURITY.md)",
                        "[Code of Conduct](CODE_OF_CONDUCT.md)",
                    ]
                ),
                encoding="utf-8",
            )
            for relative_path in release_readiness.PUBLIC_HYGIENE_DOC_PATHS:
                path = repo / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    "placeholder tokens private source raw diffs "
                    "raw model transcripts auth output security.md\n",
                    encoding="utf-8",
                )
            (repo / "CODE_OF_CONDUCT.md").write_text(
                "Redact private source, credentials, and auth output. "
                "Use SECURITY.md for private reports.\n",
                encoding="utf-8",
            )

            payload = release_readiness.render_release_readiness(repo)

        check_ids = {check["id"]: check for check in payload["checks"]}
        self.assertEqual(check_ids["public-maintainer-docs"]["status"], "pass")
        self.assertEqual(check_ids["public-docs-linked-from-readme"]["status"], "pass")
        redaction_check = check_ids["public-support-redaction-guidance"]
        self.assertEqual(redaction_check["status"], "fail")
        conduct_missing = redaction_check["detail"]["missing_terms_by_doc"][
            "CODE_OF_CONDUCT.md"
        ]
        self.assertIn("tokens", conduct_missing)
        self.assertIn("raw diffs", conduct_missing)
        self.assertIn("raw model transcripts", conduct_missing)

    def test_release_readiness_tag_derivation_supports_release_stages(self) -> None:
        self.assertEqual(
            release_readiness._release_tag_for_version("0.5.0b53"),
            "v0.5.0-beta.53",
        )
        self.assertEqual(
            code_mower_versioning.release_tag_for_version("0.5.0b53"),
            "v0.5.0-beta.53",
        )
        self.assertEqual(
            release_readiness._release_tag_for_version("0.5.0b53"),
            "v0.5.0-beta.53",
        )
        self.assertEqual(
            code_mower_versioning.release_tag_for_version("0.5.0b53"),
            "v0.5.0-beta.53",
        )
        self.assertEqual(
            release_readiness._release_tag_for_version("1.0.0rc1"),
            "v1.0.0-rc.1",
        )
        self.assertEqual(
            code_mower_versioning.release_tag_for_version("1.0.0rc1"),
            "v1.0.0-rc.1",
        )
        self.assertEqual(
            release_readiness._release_tag_for_version("1.0.0"),
            "v1.0.0",
        )
        self.assertEqual(
            code_mower_versioning.release_tag_for_version("1.0.0"),
            "v1.0.0",
        )

    def test_public_release_baseline_helpers_derive_announcement_links(self) -> None:
        self.assertEqual(
            code_mower_versioning.public_baseline_sentence(__version__),
            (
                "The prepared public beta baseline is `v0.5.0-beta.53`; it is "
                "to be published and rehearsed as part of the "
                "`v0.5.0-beta.53` release execution, with evidence recorded on "
                "the release after the workflow runs complete. The pinned "
                "package-index install spec is `code-mower==0.5.0b53`."
            ),
        )
        self.assertEqual(
            code_mower_versioning.tagged_doc_url(__version__),
            (
                "https://github.com/codemower-ai/code-mower/blob/"
                "v0.5.0-beta.53/docs/try-in-10-minutes.md"
            ),
        )

    def test_public_announcement_docs_use_current_release_helpers(self) -> None:
        baseline_sentence = code_mower_versioning.public_baseline_sentence(__version__)
        install_command = (
            "pipx install --python python3.12 "
            f"{code_mower_versioning.public_package_spec(__version__)}"
        )
        announcement_url = code_mower_versioning.tagged_doc_url(__version__)

        current_state = (ROOT / "docs/current-state-and-roadmap.md").read_text(
            encoding="utf-8",
        )
        rollout = (ROOT / "docs/friendly-user-rollout-v05.md").read_text(
            encoding="utf-8",
        )
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn(baseline_sentence, current_state)
        self.assertIn(baseline_sentence, rollout)
        self.assertIn(install_command, rollout)
        self.assertIn(announcement_url, readme)

    def test_public_docs_have_no_stale_beta_baselines(self) -> None:
        stale_baseline = re.compile(r"beta\.(39|50|51)")
        docs = sorted((ROOT / "docs").rglob("*.md")) + [ROOT / "README.md"]

        for path in docs:
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                self.assertIsNone(stale_baseline.search(path.read_text(encoding="utf-8")))

    def test_beta53_release_docs_record_package_index_procedure_not_early_proof(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        oss_checklist = (ROOT / "docs" / "oss-v1-checklist.md").read_text(
            encoding="utf-8",
        )
        first_user = (ROOT / "docs" / "first-user-install-rehearsal.md").read_text(
            encoding="utf-8",
        )
        current_state = (
            ROOT / "docs" / "current-state-and-roadmap.md"
        ).read_text(encoding="utf-8")
        rollout = (ROOT / "docs" / "friendly-user-rollout-v05.md").read_text(
            encoding="utf-8",
        )
        public_release = (ROOT / "docs" / "public-release-checklist.md").read_text(
            encoding="utf-8",
        )

        prepared_status = (
            "published and rehearsed as part of the `v0.5.0-beta.53` release "
            "execution, with evidence recorded on the release after the "
            "workflow runs complete"
        )
        for text in (readme, current_state, rollout, public_release):
            self.assertIn(prepared_status, " ".join(text.split()))

        self.assertIn(
            "The latest completed public-package rehearsal\n"
            "remains beta.52: 10/10 first-user readiness for `code-mower==0.5.0b52`.",
            readme,
        )
        self.assertIn(
            "The latest completed package-index proof remains beta.52: 10/10 "
            "first-user\nreadiness from `code-mower==0.5.0b52`",
            rollout,
        )
        self.assertIn(
            "a public PyPI package-install rehearsal from beta.52\n"
            "  `code-mower==0.5.0b52` with a\n"
            "  10/10 first-user readiness score",
            current_state,
        )

        self.assertIn(
            "The prepared public-release baseline is `v0.5.0-beta.53`",
            oss_checklist,
        )
        self.assertIn(
            "the `release.yml` workflow in two separate\n"
            "  runs: TestPyPI first with `publish_testpypi=true`, `publish_pypi=false`,",
            oss_checklist,
        )
        self.assertIn(
            "then production PyPI with `publish_testpypi=false`, `publish_pypi=true`",
            oss_checklist,
        )
        self.assertIn("workflow run\n  links recorded as release evidence", oss_checklist)
        self.assertNotIn(
            "public package installation from PyPI and TestPyPI as",
            oss_checklist,
        )

        self.assertIn("## Beta.53 Package-Index Release Procedure", first_user)
        self.assertIn(
            "Do not record public package proof for `v0.5.0-beta.53` until the release\n"
            "workflow runs and package-install rehearsals exist.",
            first_user,
        )
        self.assertIn("dispatch both package-index publication runs", first_user)
        self.assertIn("never substitute mutable `main`", first_user)
        self.assertEqual(first_user.count("  --ref v0.5.0-beta.53 \\"), 2)
        self.assertNotIn("  --ref main \\", first_user)
        self.assertIn("-f publish_testpypi=true", first_user)
        self.assertIn("-f publish_pypi=false", first_user)
        self.assertIn("-f publish_testpypi=false", first_user)
        self.assertIn("-f publish_pypi=true", first_user)
        self.assertIn(
            "The workflow run links are the publication\n"
            "evidence; the rehearsal JSON is the install-path evidence.",
            first_user,
        )
        self.assertNotIn(
            "The latest public-package rehearsal for `v0.5.0-beta.53` was run",
            first_user,
        )
        self.assertNotIn("TestPyPI is not\npublished for `0.5.0b53`", first_user)
        for text in (readme, current_state, rollout):
            self.assertNotIn(
                "published on PyPI as `code-mower==0.5.0b53`",
                text,
            )
            self.assertNotIn(
                "latest 10/10 public-package readiness proof for "
                "`code-mower==0.5.0b53`",
                text,
            )
            self.assertNotIn(
                "public PyPI package-install rehearsal from "
                "`code-mower==0.5.0b53`",
                text,
            )

    def test_readme_describes_calibration_limits_and_roles(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("## What Calibration Does And Does Not Prove", readme)
        self.assertIn(
            "It does not prove that a reviewer should gate merges.",
            readme,
        )
        self.assertIn("[lane promotion policy](docs/lane-promotion-policy.md)", readme)
        self.assertIn("## Start Here", readme)
        self.assertIn("beta, bring-your-own-agent-loop software", readme)
        self.assertIn("not a drop-in autonomous merge gate", readme)
        self.assertIn("## Roles", readme)
        self.assertIn("Claude Code, Codex, Cursor-style", readme)
        self.assertIn("templates now support that loop end to end", readme)
        self.assertIn("[docs/v06-truth-baseline.md](docs/v06-truth-baseline.md)", readme)

    def test_v06_truth_baseline_records_provider_contract_scope(self) -> None:
        baseline = (ROOT / "docs" / "v06-truth-baseline.md").read_text(
            encoding="utf-8",
        )

        self.assertIn("Last verified: 2026-08-31 PT.", baseline)
        self.assertIn("`v0.5.0-beta.53`", baseline)
        self.assertIn("`code-mower==0.5.0b53`", baseline)
        self.assertIn("`src/code_mower/provider_runners/`", baseline)
        self.assertIn("avoid a flag-day `BaseProviderRunner` rewrite", baseline)
        self.assertIn("| Gemini CLI | `gemini_cli`", baseline)
        self.assertIn("| Antigravity CLI | `antigravity_cli`", baseline)
        self.assertIn("| Antigravity SDK | Future `antigravity_sdk`", baseline)
        self.assertIn("`ANTIGRAVITY_CLI_USE_AMBIENT_HOME=1`", baseline)
        self.assertIn("metadata only", baseline)
        self.assertIn("must not contain source code, raw diffs", baseline)

    def test_start_here_docs_cover_reviewer_gate_and_build_loop_routes(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        try_in_10 = (ROOT / "docs" / "try-in-10-minutes.md").read_text(
            encoding="utf-8",
        )
        build_loop_30 = (ROOT / "docs" / "build-loop-in-30-minutes.md").read_text(
            encoding="utf-8",
        )
        quickstart = (ROOT / "docs" / "quickstart.md").read_text(encoding="utf-8")

        self.assertIn("[Try Code Mower In 10 Minutes](docs/try-in-10-minutes.md)", readme)
        self.assertIn("[Build Loop In 30 Minutes](docs/build-loop-in-30-minutes.md)", readme)
        self.assertEqual(len(re.findall(r"^## 8\.", try_in_10, re.MULTILINE)), 1)
        self.assertTrue(try_in_10.rstrip().endswith("](build-loop-in-30-minutes.md)."))
        self.assertIn("[Quickstart](quickstart.md)", try_in_10)
        self.assertIn("[Quickstart](quickstart.md)", build_loop_30)
        self.assertIn("write a PATCH payload from", build_loop_30)
        self.assertIn("existing source-app bindings", build_loop_30)
        self.assertIn("python3.12 - <<'PY'", build_loop_30)
        self.assertIn('preserved_checks.append({"context": check["context"], "app_id": app_id})', build_loop_30)
        self.assertIn('"checks": preserved_checks + [{"context": "code-mower/gate", "app_id": -1}]', build_loop_30)
        self.assertIn("--input /tmp/required-status-checks-patch.json", build_loop_30)
        self.assertNotIn("-F contexts[]=", build_loop_30)

        docs_map = readme.split("## Docs Map", 1)[1]
        for link in (
            "docs/build-loop-in-30-minutes.md",
            "docs/planning-work-orders.md",
            "docs/builders-grok-cursor.md",
            "docs/local-audit-runner.md",
            "docs/self-hosted-mac-runner.md",
            "docs/build-loop.md",
            "docs/lanes/README.md",
            "docs/lanes/codex.md",
            "docs/lanes/claude.md",
            "docs/lanes/cursor.md",
            "docs/v06-truth-baseline.md",
        ):
            with self.subTest(link=link):
                self.assertIn(link, docs_map)

        self.assertIn("Roles: Claude Code as orchestrator", build_loop_30)
        self.assertIn("builder:codex: codex", build_loop_30)
        self.assertIn("builder:claude: claude", build_loop_30)
        self.assertIn("builder:cursor: cursor", build_loop_30)
        self.assertIn("builder:grok-bot: cursor", build_loop_30)
        self.assertIn("dispatched:codex", build_loop_30)
        self.assertIn("dispatched:claude", build_loop_30)
        self.assertIn("dispatched:cursor", build_loop_30)

        repo_path_truth = (
            "Use `--repo-path /path/to/repo` to validate the installed Code Mower "
            "CLI against a real repository; if `tools/code_mower` exists, the "
            "rehearsal also runs wrapper parity for mirror-removal, otherwise it "
            "detects and dry-runs the repo's native checks."
        )
        self.assertIn(repo_path_truth, " ".join(try_in_10.split()))
        self.assertIn(repo_path_truth, " ".join(quickstart.split()))

    def test_v06_adoption_polish_docs_cover_cold_start_surface(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        quickstart = (ROOT / "docs" / "quickstart.md").read_text(encoding="utf-8")
        troubleshooting = (ROOT / "docs" / "troubleshooting.md").read_text(
            encoding="utf-8",
        )
        release_notes = (ROOT / "docs" / "v06-release-notes.md").read_text(
            encoding="utf-8",
        )

        self.assertIn("Cold adopters should start with the reviewer gate", readme)
        self.assertIn("code-mower builder-experiment run", readme)
        self.assertIn("[v0.6 Release Notes Draft](docs/v06-release-notes.md)", readme)
        self.assertIn("Gemini CLI and Antigravity are distinct lane ids", readme)
        self.assertIn("Claude Code/Codex/Cursor as builders", quickstart)
        self.assertIn("Gitar plus Antigravity as informational", quickstart)
        self.assertIn("CODE_MOWER_GATE_AUTOMERGE_TOKEN", quickstart)
        self.assertIn("Manual Audit Wrapper Fails Before Reviewing", troubleshooting)
        self.assertIn("OWNER/REPO:/absolute/path/to/pr-head-checkout", troubleshooting)
        self.assertIn("structured output", " ".join(troubleshooting.split()))
        self.assertIn("Code Mower v0.6 Release Notes Draft", release_notes)
        self.assertIn("`code_mower.provider_runners`", release_notes)
        self.assertIn("code_mower.authoringRun.v1", release_notes)
        self.assertIn("metadata-only", release_notes)
        self.assertIn("no source code, raw diffs", release_notes)

    def test_package_command_inventory_derives_current_prerelease_spec(self) -> None:
        package_content_text = (ROOT / "src/code_mower/package_content.py").read_text(
            encoding="utf-8",
        )

        self.assertEqual(
            code_mower_package_content.current_alpha_package_spec(__version__),
            next_steps.current_alpha_package_spec(),
        )
        self.assertEqual(
            code_mower_package_content.current_alpha_package_spec("0.5.0b53"),
            "code-mower==0.5.0b53",
        )
        self.assertNotIn(next_steps.current_public_tag(), package_content_text)
        self.assertNotIn("v0.0.0", package_content_text)
        self.assertIn(
            (
                "code-mower migration package-install-rehearsal "
                f"--package-spec {next_steps.current_alpha_package_spec()} "
                "--repo-path /path/to/product-repo --json"
            ),
            code_mower_package_content.cli_commands(__version__),
        )

        plan = code_mower_package.render_package_plan(
            code_mower_package.load_config(
                ROOT / "src/code_mower/templates/code-mower.example.yml",
            ),
            code_mower_package.load_provider_templates(
                ROOT / "src/code_mower/templates/providers.yml",
            ),
        )
        self.assertEqual(plan.data["package"]["version"], __version__)
        self.assertIn(
            next_steps.current_alpha_package_spec(),
            "\n".join(plan.data["cli_commands"]),
        )

    def test_release_readiness_workflow_parsing_is_job_order_independent(self) -> None:
        workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        before, testpypi_and_after = workflow.split("  publish-testpypi:\n", 1)
        testpypi_body, pypi_and_after = testpypi_and_after.split("  publish-pypi:\n", 1)
        reordered = f"{before}  publish-pypi:\n{pypi_and_after}  publish-testpypi:\n{testpypi_body}"
        jobs = release_readiness._workflow_jobs(reordered)
        testpypi_text = release_readiness._job_text(jobs["publish-testpypi"])
        pypi_text = release_readiness._job_text(jobs["publish-pypi"])

        self.assertIn("https://test.pypi.org/legacy/", testpypi_text)
        self.assertNotIn("https://test.pypi.org/legacy/", pypi_text)
        self.assertTrue(release_readiness._needs_job(jobs["publish-testpypi"], "verify-distributions"))
        self.assertTrue(release_readiness._needs_job(jobs["publish-pypi"], "verify-distributions"))
        self.assertTrue(
            release_readiness._job_uses_action(
                jobs["publish-testpypi"],
                "pypa/gh-action-pypi-publish@",
            )
        )
        self.assertTrue(
            release_readiness._job_uses_action(
                jobs["publish-pypi"],
                "pypa/gh-action-pypi-publish@",
            )
        )

    def test_release_readiness_detects_missing_production_publish_step(self) -> None:
        workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        workflow_without_publish = workflow.replace(
            "      - name: Publish to PyPI\n"
            "        uses: pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33\n",
            "      - name: Publish to PyPI\n"
            "        run: echo skipped\n",
        )
        jobs = release_readiness._workflow_jobs(workflow_without_publish)

        self.assertFalse(
            release_readiness._job_uses_action(
                jobs["publish-pypi"],
                "pypa/gh-action-pypi-publish@",
            )
        )

    def test_migration_import_does_not_require_tools_release_readiness(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "tools.migration",
            ROOT / "src/code_mower/migration.py",
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        previous_tools = sys.modules.pop("tools", None)
        previous_tools_release_readiness = sys.modules.pop("tools.release_readiness", None)
        try:
            sys.modules["tools.migration"] = module
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop("tools.migration", None)
            if previous_tools is not None:
                sys.modules["tools"] = previous_tools
            if previous_tools_release_readiness is not None:
                sys.modules["tools.release_readiness"] = previous_tools_release_readiness

        self.assertTrue(hasattr(module, "main"))

    def test_release_readiness_cli_outputs_json(self) -> None:
        out = StringIO()
        with redirect_stdout(out):
            exit_code = code_mower_migration.main(
                ["release-readiness", "--repo-path", str(ROOT), "--json"]
            )

        self.assertEqual(exit_code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["mode"], "code-mower-release-readiness")
        self.assertEqual(payload["status"], "pass")
        self.assertEqual(payload["failed"], 0)
        self.assertIn("setup_urls", payload)

    def test_release_readiness_text_includes_package_index_setup_urls(self) -> None:
        payload = release_readiness.render_release_readiness(ROOT)
        text = release_readiness.render_release_readiness_text(payload)

        self.assertIn("Setup URLs:", text)
        self.assertIn("https://github.com/codemower-ai/code-mower/settings/environments", text)
        self.assertIn("https://test.pypi.org/manage/project/code-mower/settings/publishing/", text)
        self.assertIn("https://github.com/codemower-ai/code-mower/actions/workflows/release.yml", text)

    def test_easy_mode_smoke_covers_dogfood_dry_run(self) -> None:
        smoke_text = (ROOT / "scripts/smoke_easy_mode.py").read_text(encoding="utf-8")
        self.assertIn('"dogfood"', smoke_text)
        self.assertIn('"--json"', smoke_text)
        self.assertIn("cloud-dogfood-dry-run.json", smoke_text)

    def test_fresh_clone_rehearsal_uses_venv_code_mower_for_smoke(self) -> None:
        rehearsal_text = (ROOT / "scripts/fresh_clone_rehearsal.py").read_text(
            encoding="utf-8"
        )

        self.assertIn('"scripts/smoke_easy_mode.py"', rehearsal_text)
        self.assertIn('"--code-mower-bin"', rehearsal_text)
        self.assertIn("str(_venv_code_mower(venv_dir))", rehearsal_text)

    def test_next_steps_prefers_antigravity_for_new_google_cli_calibration(self) -> None:
        templates = next_steps.code_mower_package.load_provider_templates(
            ROOT / "src/code_mower/templates/providers.yml"
        )
        plan = next_steps.build_next_steps(
            templates,
            repo="owner/repo",
            pr="123",
            profile="third_peer",
        )
        calibration = next(
            item for item in plan["steps"] if item["id"] == "calibration-run"
        )
        first_audit = next(
            item for item in plan["steps"] if item["id"] == "first-audit"
        )
        self.assertEqual(first_audit["label"], "needs-antigravity-cli-audit")
        self.assertIn("--lanes antigravity-cli", calibration["command"])
        self.assertNotIn("--lanes gemini-cli", calibration["command"])
        self.assertIn("legacy/API-key compatibility", calibration["why"])
        auto_discover = next(
            item for item in plan["steps"] if item["id"] == "calibration-auto-discover"
        )
        self.assertIn("calibration auto-discover", auto_discover["command"])
        self.assertIn("--repo owner/repo", auto_discover["command"])
        self.assertIn("draft-calibration-corpus.json", auto_discover["command"])
        self.assertIn("Confirm every disposition", auto_discover["why"])

    def test_next_steps_includes_cloud_upload_dry_run_after_export(self) -> None:
        templates = next_steps.code_mower_package.load_provider_templates(
            ROOT / "src/code_mower/templates/providers.yml"
        )
        plan = next_steps.build_next_steps(
            templates,
            profile="recommended",
            repo="codemower-ai/code-mower",
            pr="61",
        )
        ids = [step["id"] for step in plan["steps"]]

        self.assertIn("calibration-auto-discover", ids)
        self.assertLess(ids.index("calibration-run"), ids.index("calibration-auto-discover"))
        self.assertLess(ids.index("calibration-auto-discover"), ids.index("value-report"))
        self.assertIn("cloud-export", ids)
        self.assertIn("cloud-setup", ids)
        self.assertIn("cloud-upload-dry-run", ids)
        doctor_step = next(step for step in plan["steps"] if step["id"] == "doctor-easy")
        package_step = next(
            step for step in plan["steps"] if step["id"] == "package-install-rehearsal"
        )
        self.assertIn("doctor --v05", doctor_step["command"])
        self.assertIn("code-mower==0.5.0b53", package_step["command"])
        self.assertIn("current published PyPI prerelease", package_step["why"])
        self.assertIn("first_user_readiness", package_step["why"])
        self.assertEqual(
            package_step["artifacts"],
            [
                "outputs/package-install-rehearsal.json",
                "outputs/first-user-readiness.json",
            ],
        )
        self.assertLess(ids.index("cloud-export"), ids.index("cloud-setup"))
        self.assertLess(ids.index("cloud-setup"), ids.index("cloud-upload-dry-run"))
        self.assertLess(ids.index("cloud-export"), ids.index("cloud-upload-dry-run"))
        setup = next(step for step in plan["steps"] if step["id"] == "cloud-setup")
        self.assertIn("cloud setup", setup["command"])
        self.assertIn("--token-stdin", setup["command"])
        dry_run = next(step for step in plan["steps"] if step["id"] == "cloud-upload-dry-run")
        self.assertIn("cloud upload", dry_run["command"])
        self.assertIn("--dry-run", dry_run["command"])
        dogfood_dry_run = next(
            step for step in plan["steps"] if step["id"] == "cloud-dogfood-dry-run"
        )
        dogfood_upload = next(
            step for step in plan["steps"] if step["id"] == "cloud-dogfood-upload"
        )
        catch_up_dry_run = next(
            step for step in plan["steps"] if step["id"] == "cloud-catch-up-dry-run"
        )
        catch_up_upload = next(
            step for step in plan["steps"] if step["id"] == "cloud-catch-up-upload"
        )
        self.assertLess(ids.index("cloud-upload-dry-run"), ids.index("cloud-dogfood-dry-run"))
        self.assertLess(ids.index("cloud-dogfood-dry-run"), ids.index("cloud-dogfood-upload"))
        self.assertLess(ids.index("cloud-dogfood-upload"), ids.index("cloud-catch-up-dry-run"))
        self.assertLess(ids.index("cloud-catch-up-dry-run"), ids.index("cloud-catch-up-upload"))
        self.assertIn("cloud dogfood", dogfood_dry_run["command"])
        self.assertNotIn("--dry-run", dogfood_dry_run["command"])
        self.assertIn("source ~/.config/code-mower/tokens", dogfood_dry_run["command"])
        self.assertIn("cloud dogfood", dogfood_upload["command"])
        self.assertIn("--yes", dogfood_upload["command"])
        self.assertIn("cloud catch-up", catch_up_dry_run["command"])
        self.assertIn("--repo-slug codemower-ai/code-mower", catch_up_dry_run["command"])
        self.assertNotIn("OWNER/REPO", catch_up_dry_run["command"])
        self.assertNotIn("--yes", catch_up_dry_run["command"])
        self.assertNotIn("--include-git-ref", catch_up_dry_run["command"])
        self.assertIn("cloud catch-up", catch_up_upload["command"])
        self.assertIn("--repo-slug codemower-ai/code-mower", catch_up_upload["command"])
        self.assertNotIn("OWNER/REPO", catch_up_upload["command"])
        self.assertIn("--yes", catch_up_upload["command"])
        self.assertNotIn("--include-git-ref", catch_up_upload["command"])
        self.assertIn("workflow_run", catch_up_dry_run["why"])
        self.assertIn("commit SHAs", catch_up_upload["why"])

    def test_calibration_arms_include_antigravity_lens_fanout(self) -> None:
        arms = {
            arm["arm_id"]: arm
            for arm in code_mower_calibration.default_arms()
        }
        self.assertIn("antigravity-doctrine-lens-fanout", arms)
        reviewer_ids = {
            reviewer["reviewer_id"]
            for reviewer in arms["antigravity-doctrine-lens-fanout"]["reviewers"]
        }
        self.assertEqual(
            reviewer_ids,
            {
                "antigravity-base-audit",
                "antigravity-generic-programming",
                "antigravity-context-driven-quality",
            },
        )


if __name__ == "__main__":
    unittest.main()
