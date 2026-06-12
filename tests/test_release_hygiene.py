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
    def test_version_is_alpha_19(self) -> None:
        self.assertEqual(__version__, "0.1.0a19")

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

    def test_standalone_shadow_releases_checkout_lock_before_delegation(self) -> None:
        text = (
            ROOT
            / "src/code_mower/templates/product-support/code_mower_standalone_shadow.sh"
        ).read_text(encoding="utf-8")
        release_index = text.rfind("release_checkout_lock")
        delegate_index = text.rfind('"${script_dir}/code_mower" "$@"')
        self.assertGreaterEqual(release_index, 0)
        self.assertGreater(delegate_index, release_index)

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
