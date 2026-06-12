from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from code_mower import __version__
from code_mower import audit_progress
from code_mower import code_mower_calibration
from code_mower import doctor
from code_mower import next_steps
from code_mower import secrets as code_mower_secrets
from scripts import privacy_scan


class ReleaseHygieneTests(unittest.TestCase):
    def test_version_is_alpha_15(self) -> None:
        self.assertEqual(__version__, "0.1.0a15")

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
