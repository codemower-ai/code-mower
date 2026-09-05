#!/usr/bin/env python3
"""Focused tests for package-install failure reason taxonomy (issue #727).

Covers the closed failure-reason taxonomy (network, package_index, runtime,
sandbox_permission, unknown), metadata-only persisted results (never raw
output, paths, auth, or secrets), existing result compatibility, and the
v1.0.8 failure shape (valid qualification results with package_install
failures classified by reason).
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from code_mower import migration_install
from code_mower import release_qualify


class FailureReasonClassificationTests(unittest.TestCase):
    """Every reason code is accurately classified from error messages."""

    def test_network_connection_refused(self) -> None:
        exc = RuntimeError("connection refused to pypi.org")
        steps = [{"stderr_preview": "error: connection timed out"}]
        reason = migration_install.classify_package_install_failure(
            exception=exc, steps=steps
        )
        self.assertEqual(reason, "network")

    def test_network_dns_failure(self) -> None:
        exc = RuntimeError("temporary failure in name resolution")
        steps = [{"stderr_preview": "Could not resolve host: test.pypi.org"}]
        reason = migration_install.classify_package_install_failure(
            exception=exc, steps=steps
        )
        self.assertEqual(reason, "network")

    def test_network_ssl_error(self) -> None:
        exc = RuntimeError("SSL certificate verify failed")
        steps = [{"stderr_preview": "ssl: certificate validation failed"}]
        reason = migration_install.classify_package_install_failure(
            exception=exc, steps=steps
        )
        self.assertEqual(reason, "network")

    def test_network_timeout(self) -> None:
        exc = subprocess.TimeoutExpired(["pip", "install"], 180)
        steps = [{"stderr_preview": "timeout expired"}]
        reason = migration_install.classify_package_install_failure(
            exception=exc, steps=steps
        )
        self.assertEqual(reason, "network")

    def test_package_index_404(self) -> None:
        exc = RuntimeError("HTTP Error 404: Not Found")
        steps = [{"stderr_preview": "ERROR: Could not find a version"}]
        reason = migration_install.classify_package_install_failure(
            exception=exc, steps=steps
        )
        self.assertEqual(reason, "package_index")

    def test_package_index_no_matching_distribution(self) -> None:
        exc = RuntimeError("No matching distribution found")
        steps = [{"stderr_preview": "ERROR: No matching distribution found"}]
        reason = migration_install.classify_package_install_failure(
            exception=exc, steps=steps
        )
        self.assertEqual(reason, "package_index")

    def test_runtime_python_version(self) -> None:
        exc = RuntimeError("Requires Python >=3.13")
        steps = [{"stderr_preview": "requires python_version >= '3.13'"}]
        reason = migration_install.classify_package_install_failure(
            exception=exc, steps=steps
        )
        self.assertEqual(reason, "runtime")

    def test_runtime_import_error(self) -> None:
        exc = RuntimeError("ModuleNotFoundError: No module named 'distutils'")
        steps = [{"stderr_preview": "ModuleNotFoundError: No module named"}]
        reason = migration_install.classify_package_install_failure(
            exception=exc, steps=steps
        )
        self.assertEqual(reason, "runtime")

    def test_runtime_dependency_conflict(self) -> None:
        exc = RuntimeError("incompatible version for dependency")
        steps = [{"stderr_preview": "requires a different version of"}]
        reason = migration_install.classify_package_install_failure(
            exception=exc, steps=steps
        )
        self.assertEqual(reason, "runtime")

    def test_sandbox_permission_denied(self) -> None:
        exc = RuntimeError("Permission denied")
        steps = [{"stderr_preview": "[Errno 13] Permission denied"}]
        reason = migration_install.classify_package_install_failure(
            exception=exc, steps=steps
        )
        self.assertEqual(reason, "sandbox_permission")

    def test_sandbox_disk_full(self) -> None:
        exc = RuntimeError("No space left on device")
        steps = [{"stderr_preview": "[Errno 28] No space left"}]
        reason = migration_install.classify_package_install_failure(
            exception=exc, steps=steps
        )
        self.assertEqual(reason, "sandbox_permission")

    def test_sandbox_read_only_filesystem(self) -> None:
        exc = RuntimeError("Read-only file system")
        steps = [{"stderr_preview": "[Errno 30] Read-only file system"}]
        reason = migration_install.classify_package_install_failure(
            exception=exc, steps=steps
        )
        self.assertEqual(reason, "sandbox_permission")

    def test_unknown_unclassifiable_error(self) -> None:
        exc = RuntimeError("unexpected exotic failure mode")
        steps = [{"stderr_preview": "some unrecognized error message"}]
        reason = migration_install.classify_package_install_failure(
            exception=exc, steps=steps
        )
        self.assertEqual(reason, "unknown")

    def test_classification_prioritizes_sandbox_over_network(self) -> None:
        # Permission errors should be classified as sandbox even if network
        # indicators are also present
        exc = RuntimeError("connection refused")
        steps = [{"stderr_preview": "Permission denied and connection refused"}]
        reason = migration_install.classify_package_install_failure(
            exception=exc, steps=steps
        )
        self.assertEqual(reason, "sandbox_permission")


class FailureReasonSchemaTests(unittest.TestCase):
    """The adoption result schema accepts and validates failure_reason."""

    def _mock_result(
        self, outcome: str, step_status: str, failure_reason: str | None = None
    ) -> dict[str, Any]:
        step: dict[str, Any] = {
            "id": "package_install",
            "status": step_status,
            "elapsed_seconds": 12.0,
            "warning_count": 0,
            "owner_action_count": 0,
        }
        if failure_reason is not None:
            step["failure_reason"] = failure_reason

        return {
            "schema": "code_mower.adoptionResult.v1",
            "timestamp_utc": "2026-09-04T08:00:00Z",
            "release_tag": "v1.0.8",
            "package_identity": "code-mower",
            "normalized_version": "1.0.8",
            "qualification_context": "cold_install",
            "starting_version": "",
            "ending_version": "1.0.8" if outcome == "pass" else "",
            "provider": "cursor",
            "executor": "cursor",
            "host_class": "local",
            "runtime_class": "python_3.12",
            "execution_state": "executed",
            "elapsed_seconds": 12.0,
            "outcome": outcome,
            "steps": [step],
        }

    def test_fail_with_network_reason_validates(self) -> None:
        result = self._mock_result("fail", "fail", "network")
        release_qualify.validate_adoption_result_payload(
            result, expected_package_identity="code-mower"
        )

    def test_fail_with_package_index_reason_validates(self) -> None:
        result = self._mock_result("fail", "fail", "package_index")
        release_qualify.validate_adoption_result_payload(
            result, expected_package_identity="code-mower"
        )

    def test_fail_with_runtime_reason_validates(self) -> None:
        result = self._mock_result("fail", "fail", "runtime")
        release_qualify.validate_adoption_result_payload(
            result, expected_package_identity="code-mower"
        )

    def test_fail_with_sandbox_permission_reason_validates(self) -> None:
        result = self._mock_result("fail", "fail", "sandbox_permission")
        release_qualify.validate_adoption_result_payload(
            result, expected_package_identity="code-mower"
        )

    def test_fail_with_unknown_reason_validates(self) -> None:
        result = self._mock_result("fail", "fail", "unknown")
        release_qualify.validate_adoption_result_payload(
            result, expected_package_identity="code-mower"
        )

    def test_fail_without_reason_validates(self) -> None:
        # Existing results without failure_reason remain valid
        result = self._mock_result("fail", "fail", None)
        release_qualify.validate_adoption_result_payload(
            result, expected_package_identity="code-mower"
        )

    def test_pass_without_reason_validates(self) -> None:
        result = self._mock_result("pass", "pass", None)
        release_qualify.validate_adoption_result_payload(
            result, expected_package_identity="code-mower"
        )

    def test_pass_with_reason_rejected(self) -> None:
        # failure_reason is only valid for failed steps
        result = self._mock_result("pass", "pass", "network")
        with self.assertRaisesRegex(
            ValueError, "failure_reason is only valid when status is fail"
        ):
            release_qualify.validate_adoption_result_payload(
                result, expected_package_identity="code-mower"
            )

    def test_invalid_reason_rejected(self) -> None:
        result = self._mock_result("fail", "fail", "bogus_reason")
        with self.assertRaisesRegex(
            ValueError, "failure_reason must be one of"
        ):
            release_qualify.validate_adoption_result_payload(
                result, expected_package_identity="code-mower"
            )

    def test_non_string_reason_rejected(self) -> None:
        result = self._mock_result("fail", "fail", None)
        result["steps"][0]["failure_reason"] = 123
        with self.assertRaisesRegex(
            ValueError, "failure_reason must be a string"
        ):
            release_qualify.validate_adoption_result_payload(
                result, expected_package_identity="code-mower"
            )


class NoSecretPersistenceTests(unittest.TestCase):
    """Failure reasons never carry raw output, paths, auth, or secrets."""

    def test_classification_never_returns_raw_output(self) -> None:
        exc = RuntimeError("supersecret auth token ABC123XYZ")
        steps = [
            {
                "stderr_preview": (
                    "ERROR: HTTP 404 Not Found\n"
                    "Authentication failed with token ABC123XYZ\n"
                    "Path: /home/user/.pip/auth.json"
                )
            }
        ]
        reason = migration_install.classify_package_install_failure(
            exception=exc, steps=steps
        )
        # Reason is a closed code, never contains the secret or path
        self.assertIn(reason, migration_install.PACKAGE_INSTALL_FAILURE_REASONS)
        self.assertNotIn("ABC123XYZ", reason)
        self.assertNotIn("/home/user", reason)
        self.assertNotIn("auth.json", reason)

    def test_result_with_failure_reason_contains_no_raw_output(self) -> None:
        result = {
            "schema": "code_mower.adoptionResult.v1",
            "timestamp_utc": "2026-09-04T08:00:00Z",
            "release_tag": "v1.0.8",
            "package_identity": "code-mower",
            "normalized_version": "1.0.8",
            "qualification_context": "cold_install",
            "starting_version": "",
            "ending_version": "",
            "provider": "cursor",
            "executor": "cursor",
            "host_class": "local",
            "runtime_class": "python_3.12",
            "execution_state": "executed",
            "elapsed_seconds": 54.78,
            "outcome": "fail",
            "steps": [
                {
                    "id": "package_install",
                    "status": "fail",
                    "elapsed_seconds": 54.78,
                    "warning_count": 0,
                    "owner_action_count": 0,
                    "failure_reason": "network",
                }
            ],
        }
        # Serialize and verify no secrets/paths/output in the result
        serialized = json.dumps(result)
        # Only the closed reason code is present
        self.assertIn('"network"', serialized)
        # No raw error messages, paths, auth output
        self.assertNotIn("stderr", serialized.lower())
        self.assertNotIn("stdout", serialized.lower())
        self.assertNotIn("/tmp/", serialized)
        self.assertNotIn("/home/", serialized)
        self.assertNotIn("token", serialized.lower())
        self.assertNotIn("secret", serialized.lower())

    def test_closed_taxonomy_is_stable(self) -> None:
        # The taxonomy is a frozen set of known codes
        self.assertEqual(
            migration_install.PACKAGE_INSTALL_FAILURE_REASONS,
            frozenset({
                "network",
                "package_index",
                "runtime",
                "sandbox_permission",
                "unknown",
            }),
        )


class ResultCompatibilityTests(unittest.TestCase):
    """Existing v1 results without failure_reason remain readable."""

    def test_v1_result_without_failure_reason_validates(self) -> None:
        # A v1.0.8-era result with no failure_reason field
        result = {
            "schema": "code_mower.adoptionResult.v1",
            "timestamp_utc": "2026-09-04T08:00:00Z",
            "release_tag": "v1.0.8",
            "package_identity": "code-mower",
            "normalized_version": "1.0.8",
            "qualification_context": "cold_install",
            "starting_version": "",
            "ending_version": "",
            "provider": "claude",
            "executor": "claude",
            "host_class": "local",
            "runtime_class": "python_3.12",
            "execution_state": "executed",
            "elapsed_seconds": 85.0,
            "outcome": "fail",
            "steps": [
                {
                    "id": "doctor",
                    "status": "pass",
                    "elapsed_seconds": 5.0,
                    "warning_count": 0,
                    "owner_action_count": 0,
                },
                {
                    "id": "package_install",
                    "status": "fail",
                    "elapsed_seconds": 80.0,
                    "warning_count": 0,
                    "owner_action_count": 0,
                },
            ],
        }
        # This validates without error
        release_qualify.validate_adoption_result_payload(
            result, expected_package_identity="code-mower"
        )

    def test_v1_result_upgrade_without_failure_reason_validates(self) -> None:
        # An upgrade result without failure_reason (Cursor's v1.0.7 to 1.0.8)
        result = {
            "schema": "code_mower.adoptionResult.v1",
            "timestamp_utc": "2026-09-04T08:00:00Z",
            "release_tag": "v1.0.8",
            "package_identity": "code-mower",
            "normalized_version": "1.0.8",
            "qualification_context": "upgrade",
            "starting_version": "1.0.7",
            "ending_version": "",
            "provider": "cursor",
            "executor": "cursor",
            "host_class": "github_actions",
            "runtime_class": "python_3.12",
            "execution_state": "executed",
            "elapsed_seconds": 120.0,
            "outcome": "fail",
            "steps": [
                {
                    "id": "doctor",
                    "status": "pass",
                    "elapsed_seconds": 10.0,
                    "warning_count": 0,
                    "owner_action_count": 0,
                },
                {
                    "id": "package_install",
                    "status": "fail",
                    "elapsed_seconds": 110.0,
                    "warning_count": 0,
                    "owner_action_count": 0,
                },
            ],
        }
        release_qualify.validate_adoption_result_payload(
            result, expected_package_identity="code-mower"
        )


class V108FailureShapeTests(unittest.TestCase):
    """Reproduce the v1.0.8 campaign failure shape with classified reasons."""

    def test_claude_cold_install_failure_with_network_reason(self) -> None:
        # Claude attempt 1: 54.78 seconds, valid result, package_install failed
        result = {
            "schema": "code_mower.adoptionResult.v1",
            "timestamp_utc": "2026-09-04T08:00:00Z",
            "release_tag": "v1.0.8",
            "package_identity": "code-mower",
            "normalized_version": "1.0.8",
            "qualification_context": "cold_install",
            "starting_version": "",
            "ending_version": "",
            "provider": "claude",
            "executor": "claude",
            "host_class": "local",
            "runtime_class": "python_3.12",
            "execution_state": "executed",
            "elapsed_seconds": 54.78,
            "outcome": "fail",
            "steps": [
                {
                    "id": "doctor",
                    "status": "pass",
                    "elapsed_seconds": 4.0,
                    "warning_count": 0,
                    "owner_action_count": 0,
                },
                {
                    "id": "package_install",
                    "status": "fail",
                    "elapsed_seconds": 50.78,
                    "warning_count": 0,
                    "owner_action_count": 0,
                    "failure_reason": "network",
                },
            ],
        }
        # Valid qualification result with classified network failure
        release_qualify.validate_adoption_result_payload(
            result, expected_package_identity="code-mower"
        )
        self.assertEqual(result["outcome"], "fail")
        self.assertEqual(result["ending_version"], "")
        self.assertEqual(
            result["steps"][1]["failure_reason"], "network"
        )

    def test_cursor_upgrade_failure_with_package_index_reason(self) -> None:
        # Cursor: 1.0.7 to 1.0.8 upgrade, clean-environment verification failed
        result = {
            "schema": "code_mower.adoptionResult.v1",
            "timestamp_utc": "2026-09-04T08:00:00Z",
            "release_tag": "v1.0.8",
            "package_identity": "code-mower",
            "normalized_version": "1.0.8",
            "qualification_context": "upgrade",
            "starting_version": "1.0.7",
            "ending_version": "",
            "provider": "cursor",
            "executor": "cursor",
            "host_class": "github_actions",
            "runtime_class": "python_3.12",
            "execution_state": "executed",
            "elapsed_seconds": 120.0,
            "outcome": "fail",
            "steps": [
                {
                    "id": "doctor",
                    "status": "pass",
                    "elapsed_seconds": 5.0,
                    "warning_count": 0,
                    "owner_action_count": 0,
                },
                {
                    "id": "package_install",
                    "status": "fail",
                    "elapsed_seconds": 115.0,
                    "warning_count": 0,
                    "owner_action_count": 0,
                    "failure_reason": "package_index",
                },
            ],
        }
        release_qualify.validate_adoption_result_payload(
            result, expected_package_identity="code-mower"
        )
        self.assertEqual(result["qualification_context"], "upgrade")
        self.assertEqual(result["starting_version"], "1.0.7")
        self.assertEqual(
            result["steps"][1]["failure_reason"], "package_index"
        )


if __name__ == "__main__":
    unittest.main()
