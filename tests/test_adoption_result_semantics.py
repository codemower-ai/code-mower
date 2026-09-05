#!/usr/bin/env python3
"""Focused semantic tests for code_mower.adoptionResult.v1 validation (#700).

Both validators -- release_qualify.validate_adoption_result_payload (local
adapters, manual records, trusted hosted results) and
cloud_client.adoption_result_to_event (cloud conversion) -- enforce the same
semantics: executed-result timestamp bounds, the built-in step taxonomy with
an explicit namespaced provider-extension form, tolerant step/total timing
consistency, and owner-action counts consistent with the outcome. Errors stay
bounded and never echo payload content.
"""

from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from code_mower import release_qualify
from code_mower.cloud_client import CloudBundleError, adoption_result_to_event


def _result(**overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": "code_mower.adoptionResult.v1",
        "timestamp_utc": "2026-09-04T08:00:00Z",
        "release_tag": "v1.0.0",
        "package_identity": "code-mower",
        "normalized_version": "1.0.0",
        "qualification_context": "cold_install",
        "starting_version": "",
        "ending_version": "1.0.0",
        "provider": "codex",
        "executor": "codex",
        "host_class": "local",
        "runtime_class": "python_3.12",
        "execution_state": "executed",
        "elapsed_seconds": 12.34,
        "outcome": "pass",
        "steps": [
            {
                "id": "doctor",
                "status": "pass",
                "elapsed_seconds": 1.2,
                "warning_count": 0,
                "owner_action_count": 0,
            },
            {
                "id": "package_install",
                "status": "pass",
                "elapsed_seconds": 11.14,
                "warning_count": 0,
                "owner_action_count": 0,
            },
        ],
    }
    result.update(overrides)
    return result


class AdoptionResultSemanticsTests(unittest.TestCase):
    def assertAcceptsBoth(self, result: dict[str, Any]) -> None:
        release_qualify.validate_adoption_result_payload(result)
        adoption_result_to_event(result)

    def assertRejectsBoth(self, result: dict[str, Any], fragment: str) -> None:
        with self.assertRaises(ValueError) as ctx:
            release_qualify.validate_adoption_result_payload(result)
        self.assertIn(fragment, str(ctx.exception))
        with self.assertRaises(CloudBundleError) as cloud_ctx:
            adoption_result_to_event(result)
        self.assertIn(fragment, str(cloud_ctx.exception))

    def test_builtin_output_shape_stays_valid(self) -> None:
        self.assertAcceptsBoth(_result())
        self.assertAcceptsBoth(
            _result(
                outcome="pass_with_warnings",
                elapsed_seconds=12.5,
                steps=[
                    {
                        "id": "doctor",
                        "status": "pass",
                        "elapsed_seconds": 1.0,
                        "warning_count": 0,
                        "owner_action_count": 0,
                    },
                    {
                        "id": "codex__smoke",
                        "status": "warn",
                        "elapsed_seconds": 2.0,
                        "warning_count": 1,
                        "owner_action_count": 1,
                    },
                    {
                        "id": "package_install",
                        "status": "pass",
                        "elapsed_seconds": 9.0,
                        "warning_count": 0,
                        "owner_action_count": 0,
                    },
                ],
            )
        )

    def test_executed_timestamp_bounds(self) -> None:
        self.assertRejectsBoth(
            _result(timestamp_utc="2999-06-06T06:06:06Z"),
            "newer than the trusted executed-result bound",
        )
        self.assertRejectsBoth(
            _result(timestamp_utc="1999-12-31T23:59:59Z"),
            "older than the trusted executed-result bound",
        )
        # Within the deterministic clock-skew tolerance: still trusted.
        soon = (
            dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=60)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertAcceptsBoth(_result(timestamp_utc=soon))

    def test_planned_future_timestamp_exempt(self) -> None:
        planned = _result(
            execution_state="planned",
            outcome="incomplete",
            ending_version="",
            timestamp_utc="2999-06-06T06:06:06Z",
            elapsed_seconds=0.5,
            steps=[
                {
                    "id": "package_install",
                    "status": "planned",
                    "elapsed_seconds": 0.0,
                    "warning_count": 0,
                    "owner_action_count": 0,
                }
            ],
        )
        self.assertAcceptsBoth(planned)

    def test_unnamespaced_unknown_step_id_rejected(self) -> None:
        bad = _result(
            steps=[
                {
                    "id": "notabuiltinid",
                    "status": "pass",
                    "elapsed_seconds": 12.34,
                    "warning_count": 0,
                    "owner_action_count": 0,
                }
            ]
        )
        with self.assertRaises(ValueError) as ctx:
            release_qualify.validate_adoption_result_payload(bad)
        self.assertIn("namespaced", str(ctx.exception))
        self.assertNotIn("notabuiltinid", str(ctx.exception))
        with self.assertRaises(CloudBundleError) as cloud_ctx:
            adoption_result_to_event(bad)
        self.assertNotIn("notabuiltinid", str(cloud_ctx.exception))

    def test_step_total_beyond_tolerance_rejected(self) -> None:
        # Step sum (12.34) exceeds the total: the inverse direction.
        self.assertRejectsBoth(_result(elapsed_seconds=1.0), "beyond tolerance")

    def test_total_larger_than_step_sum_beyond_tolerance_rejected(self) -> None:
        # Total (100.0) dwarfs the step sum (12.34): same bound, other direction.
        self.assertRejectsBoth(_result(elapsed_seconds=100.0), "beyond tolerance")

    def test_step_total_within_tolerance_accepted(self) -> None:
        self.assertAcceptsBoth(_result(elapsed_seconds=12.0))

    def test_non_finite_and_negative_timings_rejected(self) -> None:
        for elapsed in (float("nan"), float("inf"), -1.0):
            with self.subTest(elapsed=elapsed):
                self.assertRejectsBoth(
                    _result(elapsed_seconds=elapsed), "elapsed_seconds"
                )

    def test_pass_step_with_owner_actions_rejected(self) -> None:
        bad = _result(
            outcome="pass_with_warnings",
            elapsed_seconds=12.5,
            steps=[
                {
                    "id": "doctor",
                    "status": "pass",
                    "elapsed_seconds": 1.0,
                    "warning_count": 0,
                    "owner_action_count": 1,
                },
                {
                    "id": "package_install",
                    "status": "unavailable",
                    "elapsed_seconds": 0.5,
                    "warning_count": 0,
                    "owner_action_count": 0,
                },
            ],
        )
        self.assertRejectsBoth(bad, "nonzero owner_action_count")

    def test_errors_never_echo_payload_content(self) -> None:
        sentinel = "2999-06-06T06:06:06Z"
        with self.assertRaises(ValueError) as ctx:
            release_qualify.validate_adoption_result_payload(
                _result(timestamp_utc=sentinel)
            )
        self.assertNotIn(sentinel, str(ctx.exception))
        with self.assertRaises(CloudBundleError) as cloud_ctx:
            adoption_result_to_event(_result(timestamp_utc=sentinel))
        self.assertNotIn(sentinel, str(cloud_ctx.exception))


if __name__ == "__main__":
    unittest.main()
