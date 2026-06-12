from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from code_mower import __version__
from code_mower import audit_progress
from code_mower import code_mower_calibration
from code_mower import doctor
from code_mower import init as code_mower_init
from code_mower import next_steps
from code_mower import package as code_mower_package
from code_mower import secrets as code_mower_secrets
from code_mower import config as code_mower_config
from scripts import privacy_scan


class ReleaseHygieneTests(unittest.TestCase):
    def test_version_is_alpha_17(self) -> None:
        self.assertEqual(__version__, "0.1.0a17")

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
            for rel_path in generated - {"tools/code_mower_standalone_pin.env"}:
                self.assertTrue(output_dir.joinpath(rel_path).stat().st_mode & 0o111, rel_path)
            self.assertIn(
                "CODE_MOWER_STANDALONE_REF",
                output_dir.joinpath("tools/code_mower_standalone_pin.env").read_text(
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
