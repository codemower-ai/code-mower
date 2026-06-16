from __future__ import annotations

import json
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from code_mower import __version__
from code_mower import audit_progress
from code_mower import calibration as calibration_pkg
from code_mower import cloud as code_mower_cloud
from code_mower import code_mower_calibration
from code_mower import cli as code_mower_cli
from code_mower import cloud_client
from code_mower import doctor
from code_mower import doctor_checks
from code_mower import init as code_mower_init
from code_mower import migration as code_mower_migration
from code_mower import next_steps
from code_mower import package as code_mower_package
from code_mower import release_readiness
from code_mower import secrets as code_mower_secrets
from code_mower import config as code_mower_config
from code_mower import provider_runners
from code_mower.calibration import arms as calibration_arms
from code_mower.calibration import policy as calibration_policy
from scripts import privacy_scan


class ReleaseHygieneTests(unittest.TestCase):
    def test_version_is_v05_alpha_25(self) -> None:
        self.assertEqual(__version__, "0.5.0a43")

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
        self.assertIn("CODE_MOWER_TESTPYPI_PUBLISH", testpypi_job)
        self.assertIn("    environment: testpypi\n", testpypi_job)
        self.assertIn("repository-url: https://test.pypi.org/legacy/", testpypi_job)
        self.assertIn(
            "uses: pypa/gh-action-pypi-publish@cef221092ed1bacb1cc03d23a2d87d1d172e277b",
            testpypi_job,
        )
        production_job = workflow.split("  publish-pypi:\n", 1)[1]
        self.assertIn("inputs.publish_pypi", production_job)
        self.assertIn("CODE_MOWER_PYPI_PUBLISH", production_job)
        self.assertIn("    environment: pypi\n", production_job)
        self.assertNotIn("test.pypi.org", production_job)

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

    def test_cli_command_registry_is_single_source_of_truth(self) -> None:
        self.assertEqual(
            tuple(code_mower_cli.COMMAND_HANDLERS),
            (
                "antigravity-cli",
                "blind-review",
                "bootstrap",
                "builder-experiment",
                "calibration",
                "claude-audit",
                "cloud",
                "config",
                "context-packs",
                "coderabbit-cli",
                "codex-audit",
                "codex-audit-env-preflight",
                "codex-audit-schema-smoke",
                "doctor",
                "gemini-cli",
                "hermes-cli",
                "init",
                "local-llm",
                "migration",
                "merge-plan",
                "next-steps",
                "package",
                "prompts",
                "providers",
                "reviewer-metrics",
                "saas-reviewer-labeler",
                "telemetry",
                "trailer-comment-labeler",
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
                "git+https://github.com/codemower-ai/code-mower.git@v0.5.0-alpha.43 "
                "--json"
            ),
            help_text,
        )
        self.assertIn("  init", help_text)
        self.assertIn("  doctor", help_text)
        self.assertIn("  calibration", help_text)
        self.assertIn("  cloud", help_text)
        self.assertNotIn("trailer-comment-labeler", help_text)
        self.assertNotIn("codex-audit-env-preflight", help_text)
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
        self.assertIn("builder-experiment", help_text)
        self.assertIn("providers", help_text)
        self.assertIn("migration", help_text)

    def test_internal_package_seams_keep_cli_first_surface(self) -> None:
        self.assertIs(doctor.DoctorCheck, doctor_checks.DoctorCheck)
        self.assertIs(doctor.DoctorReport, doctor_checks.DoctorReport)
        self.assertIs(doctor.render_doctor_text, doctor_checks.render_doctor_text)
        rendered_doctor = doctor_checks.render_doctor_text(
            doctor_checks.DoctorReport(
                config_path="code-mower.yml",
                provider_templates_path="code-mower.provider-templates.yml",
                profile="recommended",
                checks=(
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
        self.assertIn(
            "- WARN provider.claude.auth [claude-audit]: runtime probe needs attention",
            rendered_doctor,
        )
        self.assertIn("remediation: run `claude -p ok` and retry doctor", rendered_doctor)
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
            code_mower_calibration.RUN_STATUS_UNKNOWN,
            calibration_pkg.RUN_STATUS_UNKNOWN,
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

    def test_calibration_arm_catalog_is_packaged_and_explicit_lens_aware(self) -> None:
        arm_ids = {arm["arm_id"] for arm in calibration_pkg.default_arms()}

        self.assertIn("topology-baseline", arm_ids)
        self.assertIn("antigravity-doctrine-lens-fanout", arm_ids)
        self.assertEqual(
            calibration_pkg.DEFAULT_CLI_LANES,
            ("gemini_cli", "antigravity_cli", "hermes_cli", "coderabbit_cli"),
        )
        package_targets = {target for _, target, _ in code_mower_package.PACKAGE_FILES}
        self.assertIn("src/code_mower/calibration/arms.py", package_targets)
        self.assertIn("src/code_mower/calibration/planning.py", package_targets)
        self.assertIn("src/code_mower/calibration/results.py", package_targets)
        self.assertIn("src/code_mower/calibration/run_results.py", package_targets)
        self.assertIn("src/code_mower/calibration/overlap.py", package_targets)
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

    def test_mirror_removal_plan_reports_product_support_files(self) -> None:
        from code_mower import migration

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            support_paths = [
                repo / "tools" / "code_mower",
                repo / "tools" / "code_mower_standalone_pin.env",
                repo / "tools" / "code_mower_standalone_shadow.sh",
                repo / "tools" / "run_codex_audit_pr.sh",
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
        self.assertIn("tools/run_codex_audit_pr.sh", payload["product_support_files"])
        self.assertIn("tools/safe_gh_comment.py", payload["product_support_files"])

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
                "tools/safe_gh_comment.py",
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

    def test_package_materializer_includes_product_support_templates(self) -> None:
        packaged_sources = {source for _, source, _ in code_mower_package.PACKAGE_FILES}
        for source in (
            "src/code_mower/templates/product-support/code_mower",
            "src/code_mower/templates/product-support/code_mower_standalone_pin.env",
            "src/code_mower/templates/product-support/code_mower_standalone_shadow.sh",
            "src/code_mower/templates/product-support/run_claude_audit_pr.sh",
            "src/code_mower/templates/product-support/run_codex_audit_pr.sh",
            "src/code_mower/templates/product-support/safe_gh_comment.py",
        ):
            self.assertIn(source, packaged_sources)

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
                'version = "0.5.0a43"',
                (output_dir / "pyproject.toml").read_text(encoding="utf-8"),
            )
            self.assertIn(
                '__version__ = "0.5.0a43"',
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

        fallback_text = (ROOT / "src/code_mower/package.py").read_text(
            encoding="utf-8"
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
        self.assertIn("def _fetch_base_sha_for_diff", text)
        self.assertIn("def _fetch_pr_head_sha_for_diff", text)
        self.assertIn('fetch_refspec = f"+{base_ref}:{local_ref}"', text)
        self.assertIn('f"+pull/{pr_number}/head:{local_ref}"', text)
        self.assertIn('["git", "-C", str(local_repo), "update-ref", "-d", local_ref]', text)
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
            self.assertEqual(result["upload"]["event_count"], 1)
            self.assertEqual(result["export"]["event_count"], 1)
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
            self.assertEqual(steps[0]["upload"]["event_count"], 1)
            self.assertEqual(steps[1]["status"], "no_events")

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
        package_text = (ROOT / "src/code_mower/package.py").read_text(encoding="utf-8")
        migration_text = (ROOT / "src/code_mower/migration.py").read_text(
            encoding="utf-8"
        )
        for text in (smoke_text, package_text, migration_text):
            with self.subTest():
                self.assertIn("lane-policy=lane-policy.json", text)
                self.assertIn("cloud-upload-dry-run.json", text)

    def test_package_install_rehearsal_covers_first_user_artifacts(self) -> None:
        migration_text = (ROOT / "src/code_mower/migration.py").read_text(
            encoding="utf-8"
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
                        "upload_ready": False,
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
                version="code-mower 0.5.0a43",
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
                        "upload_ready": False,
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
                version="code-mower 0.5.0a43",
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
                version="code-mower 0.5.0a43",
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
                code_mower_migration,
                "_run_rehearsal_step",
                return_value=completed,
            ) as run_step:
                result = code_mower_migration._run_rehearsal_step_to_file(
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
                    "git+https://github.com/codemower-ai/code-mower.git@v0.5.0-alpha.43",
                    base_dir=package,
                ),
                "git+https://github.com/codemower-ai/code-mower.git@v0.5.0-alpha.43",
            )
            self.assertEqual(
                code_mower_migration._resolve_install_package_spec(
                    "code-mower==0.5.0a43",
                    base_dir=package,
                ),
                "code-mower==0.5.0a43",
            )

    def test_package_install_rehearsal_supports_index_aware_pip_install(self) -> None:
        self.assertEqual(
            code_mower_migration._pip_install_command(
                Path("/tmp/venv/bin/python"),
                "code-mower==0.5.0a43",
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
                "code-mower==0.5.0a43",
            ],
        )

    def test_release_readiness_reports_package_index_promotion_gate(self) -> None:
        payload = release_readiness.render_release_readiness(ROOT)

        self.assertEqual(payload["status"], "pass")
        self.assertEqual(payload["version"], "0.5.0a43")
        self.assertEqual(payload["alpha_tag"], "v0.5.0-alpha.43")
        self.assertEqual(payload["package_index_spec"], "code-mower==0.5.0a43")
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
        self.assertEqual(check["detail"]["source_version"], "0.5.0a43")
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
            release_readiness._release_tag_for_version("0.5.0a43"),
            "v0.5.0-alpha.43",
        )
        self.assertEqual(
            release_readiness._release_tag_for_version("0.5.0b2"),
            "v0.5.0-beta.2",
        )
        self.assertEqual(
            release_readiness._release_tag_for_version("1.0.0rc1"),
            "v1.0.0-rc.1",
        )
        self.assertEqual(
            release_readiness._release_tag_for_version("1.0.0"),
            "v1.0.0",
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
            "        uses: pypa/gh-action-pypi-publish@cef221092ed1bacb1cc03d23a2d87d1d172e277b\n",
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
        self.assertIn(
            "git+https://github.com/codemower-ai/code-mower.git@v0.5.0-alpha.43",
            package_step["command"],
        )
        self.assertIn("current GitHub tag", package_step["why"])
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
