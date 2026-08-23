from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from code_mower.doctor_checks.provider_local_audit_setup import (
    check_local_audit_wrapper_setup,
)


class DoctorProviderLocalAuditSetupTests(unittest.TestCase):
    def test_non_wrapper_lane_returns_no_checks(self) -> None:
        checks = check_local_audit_wrapper_setup(
            "gitar",
            {"driver": "saas_event", "provider": "gitar"},
        )

        self.assertEqual(checks, [])

    def test_missing_direct_wrapper_token_and_repo_paths_warn(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            checks = check_local_audit_wrapper_setup(
                "codex",
                {"driver": "local_cli", "provider": "codex"},
            )

        self.assertEqual([check.status for check in checks], ["warn", "warn"])
        self.assertEqual(checks[0].name, "runtime.local_audit.auth")
        self.assertIn("--read-token-from-stdin", str(checks[0].remediation))
        self.assertIn("CODEX_AUDIT_REPO_PATHS", checks[1].message)
        self.assertIn("docs/local-audit-runner.md", str(checks[1].remediation))

    def test_invalid_repo_paths_env_fails_with_doc_link(self) -> None:
        with patch.dict(
            os.environ,
            {"GITHUB_TOKEN": "token", "CLAUDE_AUDIT_REPO_PATHS": "owner/repo:relative"},
            clear=True,
        ):
            checks = check_local_audit_wrapper_setup(
                "claude_audit",
                {"driver": "local_cli", "provider": "claude"},
            )

        self.assertEqual([check.status for check in checks], ["pass", "fail"])
        self.assertIn("CLAUDE_AUDIT_REPO_PATHS is invalid", checks[1].message)
        self.assertIn("docs/local-audit-runner.md", str(checks[1].remediation))

    def test_current_repo_path_env_fails_as_not_separate_checkout(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            with patch.dict(
                os.environ,
                {
                    "GITHUB_TOKEN": "token",
                    "CODEX_AUDIT_REPO_PATHS": f"owner/repo:{repo}",
                },
                clear=True,
            ):
                checks = check_local_audit_wrapper_setup(
                    "codex",
                    {"driver": "local_cli", "provider": "codex"},
                    repo_root=repo,
                )

        self.assertEqual([check.status for check in checks], ["pass", "fail"])
        self.assertIn("invalid PR-head checkout paths", checks[1].message)
        self.assertIn("separate PR-head checkout", checks[1].detail["invalid"][0]["error"])


if __name__ == "__main__":
    unittest.main()
