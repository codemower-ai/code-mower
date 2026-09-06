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
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from code_mower import migration_install
from code_mower import release_qualify


def _mock_result(
    outcome: str,
    step_status: str,
    failure_reason: str | None = None,
    step_id: str = "package_install",
    context: str = "cold_install",
    starting_version: str = "",
    ending_version: str = "",
) -> dict[str, Any]:
    """Shared helper for building test adoption results."""
    step: dict[str, Any] = {
        "id": step_id,
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
        "qualification_context": context,
        "starting_version": starting_version,
        "ending_version": ending_version,
        "provider": "cursor",
        "executor": "cursor",
        "host_class": "local",
        "runtime_class": "python_3.12",
        "execution_state": "executed",
        "elapsed_seconds": 12.0,
        "outcome": outcome,
        "steps": [step],
    }


class FailureReasonClassificationTests(unittest.TestCase):
    """Every reason code is accurately classified from error messages."""

    def test_classification_table_driven(self) -> None:
        """Table-driven test covering all reason codes."""
        cases = [
            # (error_text, stderr_preview, expected_reason)
            ("connection refused", "connection timed out", "network"),
            ("temporary failure in name resolution", "Could not resolve host", "network"),
            ("SSL certificate verify failed", "ssl: validation failed", "network"),
            ("timeout expired", "connection refused", "network"),  # timeout + network evidence
            ("HTTP Error 404", "Could not find a version", "package_index"),
            ("No matching distribution found", "", "package_index"),
            ("Requires Python >=3.13", "requires python_version", "runtime"),
            ("ModuleNotFoundError", "No module named", "runtime"),
            ("incompatible version", "requires a different version", "runtime"),
            ("Permission denied", "permission denied", "sandbox_permission"),
            ("No space left on device", "no space left", "sandbox_permission"),
            ("Read-only file system", "read-only file system", "sandbox_permission"),
            ("unexpected exotic failure", "unrecognized error", "unknown"),
            # HTTP 5xx range coverage (codex audit P2 finding)
            ("HTTP Error 500", "Internal Server Error", "network"),
            ("HTTP Error 510", "Not Extended", "network"),
            ("HTTP Error 511", "Network Authentication Required", "network"),
            ("HTTP Error 599", "Network Connect Timeout Error", "network"),
        ]

        for error_text, stderr_preview, expected_reason in cases:
            with self.subTest(expected_reason=expected_reason):
                exc = RuntimeError(error_text)
                steps = [{"stderr_preview": stderr_preview}]
                reason = migration_install.classify_package_install_failure(
                    exception=exc, steps=steps
                )
                self.assertEqual(reason, expected_reason)

    def test_priority_sandbox_over_network(self) -> None:
        exc = RuntimeError("connection refused")
        steps = [{"stderr_preview": "Permission denied and connection refused"}]
        reason = migration_install.classify_package_install_failure(exception=exc, steps=steps)
        self.assertEqual(reason, "sandbox_permission")

    def test_errno_111_classified_as_network(self) -> None:
        """Verify [Errno 111] ECONNREFUSED is classified as network, not sandbox."""
        exc = RuntimeError("Connection refused")
        steps = [{"stderr_preview": "[Errno 111] Connection refused"}]
        reason = migration_install.classify_package_install_failure(exception=exc, steps=steps)
        self.assertEqual(reason, "network")

    def test_classification_preserves_decisive_tail_output(self) -> None:
        exc = RuntimeError("package install failed")
        steps = [
            {
                "stderr_preview": (
                    "x" * 3000 + " No matching distribution found"
                )
            }
        ]
        reason = migration_install.classify_package_install_failure(
            exception=exc,
            steps=steps,
        )
        self.assertEqual(reason, "package_index")

    def test_timeout_with_network_evidence_classified_as_network(self) -> None:
        """Timeout + network evidence = network."""
        exc = RuntimeError("timeout expired")
        steps = [{"stderr_preview": "connection timed out to pypi.org"}]
        reason = migration_install.classify_package_install_failure(exception=exc, steps=steps)
        self.assertEqual(reason, "network")

    def test_timeout_without_network_evidence_classified_as_unknown(self) -> None:
        """Timeout without network evidence = unknown."""
        exc = RuntimeError("timeout expired")
        steps = [{"stderr_preview": "building wheel for complex-package"}]
        reason = migration_install.classify_package_install_failure(exception=exc, steps=steps)
        self.assertEqual(reason, "unknown")

    def test_timeout_with_runtime_evidence_classified_as_runtime(self) -> None:
        """Timeout plus runtime evidence retains the runtime classification."""
        exc = RuntimeError("timeout expired")
        steps = [{"stderr_preview": "Requires Python >=3.13"}]
        reason = migration_install.classify_package_install_failure(exception=exc, steps=steps)
        self.assertEqual(reason, "runtime")

    def test_network_error_without_timeout_classified_as_network(self) -> None:
        """Network error without timeout still classifies as network."""
        exc = RuntimeError("Connection refused")
        steps = [{"stderr_preview": "connection refused"}]
        reason = migration_install.classify_package_install_failure(exception=exc, steps=steps)
        self.assertEqual(reason, "network")

    def test_separate_retry_groups_isolated(self) -> None:
        """Verify earlier pip retry groups don't contaminate current classification."""
        # Shared steps list with earlier pip-upgrade attempt that failed with SSL
        steps: list[dict[str, Any]] = [
            {
                "pip_install_attempt": 1,
                "stderr_preview": "SSL certificate verify failed",
                "stdout_preview": "",
            }
        ]

        # Mock _run_rehearsal_step to simulate HTTP 404 failure
        def mock_rehearsal_step(command, *, cwd, env, steps, timeout):
            # Append step with 404 error
            steps.append(
                {
                    "stderr_preview": "HTTP Error 404: Not Found",
                    "stdout_preview": "",
                    "returncode": 1,
                }
            )
            raise migration_install.RehearsalError("HTTP Error 404", steps)

        with patch.object(
            migration_install, "_run_rehearsal_step", side_effect=mock_rehearsal_step
        ):
            with self.assertRaises(migration_install.RehearsalError) as ctx:
                migration_install._run_pip_install_with_retries(
                    command=["pip", "install", "test-package"],
                    cwd=Path("/tmp"),
                    env=None,
                    steps=steps,
                    timeout=60,
                    attempts=1,
                    retry_delay_seconds=0,
                    package_index=True,
                )

        # Assert failure_reason is package_index (404) not network (SSL from earlier)
        self.assertEqual(ctx.exception.failure_reason, "package_index")


class FailureReasonSchemaTests(unittest.TestCase):
    """The adoption result schema accepts and validates failure_reason."""

    def test_all_reasons_validate_table_driven(self) -> None:
        """Table-driven test for all valid failure reasons."""
        for reason in migration_install.PACKAGE_INSTALL_FAILURE_REASONS:
            with self.subTest(reason=reason):
                result = _mock_result("fail", "fail", reason)
                release_qualify.validate_adoption_result_payload(
                    result, expected_package_identity="code-mower"
                )

    def test_fail_without_reason_validates(self) -> None:
        result = _mock_result("fail", "fail", None)
        release_qualify.validate_adoption_result_payload(
            result, expected_package_identity="code-mower"
        )

    def test_pass_without_reason_validates(self) -> None:
        result = _mock_result("pass", "pass", None, ending_version="1.0.8")
        release_qualify.validate_adoption_result_payload(
            result, expected_package_identity="code-mower"
        )

    def test_pass_with_reason_rejected(self) -> None:
        result = _mock_result("pass", "pass", "network", ending_version="1.0.8")
        with self.assertRaisesRegex(ValueError, "failure_reason is only valid when status is fail"):
            release_qualify.validate_adoption_result_payload(
                result, expected_package_identity="code-mower"
            )

    def test_non_package_install_step_with_reason_rejected(self) -> None:
        """failure_reason is only valid for package_install step."""
        result = _mock_result("fail", "fail", "network", step_id="doctor")
        with self.assertRaisesRegex(
            ValueError, "failure_reason is only valid for package_install step"
        ):
            release_qualify.validate_adoption_result_payload(
                result, expected_package_identity="code-mower"
            )

    def test_invalid_reason_rejected(self) -> None:
        result = _mock_result("fail", "fail", "bogus_reason")
        with self.assertRaisesRegex(ValueError, "failure_reason must be one of"):
            release_qualify.validate_adoption_result_payload(
                result, expected_package_identity="code-mower"
            )

    def test_qualification_boundary_classifies_final_failed_step(self) -> None:
        error = migration_install.RehearsalError(
            "package verification failed",
            [
                {"stderr_preview": "SSL certificate verify failed"},
                {"stderr_preview": "Requires Python >=3.13"},
            ],
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(
                    release_qualify,
                    "_run_doctor_check",
                    return_value=release_qualify.StepResult(
                        id="doctor",
                        status="pass",
                        elapsed_seconds=0.0,
                        warning_count=0,
                        owner_action_count=0,
                    ),
                ),
                patch.object(
                    release_qualify,
                    "run_package_install_rehearsal",
                    side_effect=error,
                ),
            ):
                result = release_qualify.run_release_qualification(
                    release_tag="v1.0.8",
                    package_spec="code-mower==1.0.8",
                    output_path=Path(tmpdir) / "result.json",
                    repo_path=Path(tmpdir),
                    dry_run=False,
                )
        install_step = next(
            step for step in result["steps"] if step["id"] == "package_install"
        )
        self.assertEqual(install_step["failure_reason"], "runtime")

    def test_qualification_boundary_preserves_existing_reason(self) -> None:
        error = migration_install.RehearsalError(
            "package install failed",
            [{"stderr_preview": "unrelated"}],
            failure_reason="package_index",
        )
        self.assertEqual(
            release_qualify._package_install_failure_reason(error),
            "package_index",
        )


class NoSecretPersistenceTests(unittest.TestCase):
    """Failure reasons never carry raw output, paths, auth, or secrets."""

    def test_classification_returns_closed_code_only(self) -> None:
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
        reason = migration_install.classify_package_install_failure(exception=exc, steps=steps)
        self.assertIn(reason, migration_install.PACKAGE_INSTALL_FAILURE_REASONS)
        self.assertNotIn("ABC123XYZ", reason)
        self.assertNotIn("/home/user", reason)

    def test_serialized_result_contains_no_secrets(self) -> None:
        result = _mock_result("fail", "fail", "network")
        serialized = json.dumps(result)
        for forbidden in ["stderr", "stdout", "/tmp/", "token", "secret"]:
            self.assertNotIn(forbidden, serialized.lower())

    def test_taxonomy_is_stable_frozenset(self) -> None:
        self.assertEqual(
            migration_install.PACKAGE_INSTALL_FAILURE_REASONS,
            frozenset(
                {
                    "network",
                    "package_index",
                    "runtime",
                    "sandbox_permission",
                    "unknown",
                }
            ),
        )


class ResultCompatibilityTests(unittest.TestCase):
    """Existing v1 results without failure_reason remain readable."""

    def test_v108_failures_table_driven(self) -> None:
        """Table-driven test for v1.0.8 failure shapes."""
        cases = [
            # (provider, context, starting_version, elapsed, reason)
            ("claude", "cold_install", "", 54.78, "network"),
            ("claude", "cold_install", "", 85.0, None),  # No reason
            ("cursor", "upgrade", "1.0.7", 120.0, "package_index"),
        ]

        for provider, context, starting, elapsed, reason in cases:
            with self.subTest(provider=provider, reason=reason):
                result = _mock_result(
                    "fail",
                    "fail",
                    reason,
                    context=context,
                    starting_version=starting,
                )
                result["provider"] = provider
                result["executor"] = provider
                result["elapsed_seconds"] = elapsed
                result["steps"][0]["elapsed_seconds"] = elapsed
                release_qualify.validate_adoption_result_payload(
                    result, expected_package_identity="code-mower"
                )


if __name__ == "__main__":
    unittest.main()
