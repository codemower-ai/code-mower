"""Focused contract tests for the metadata-only adoption_run cloud event."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from code_mower import cloud as cloud_cli
from code_mower.cloud_client import (
    ADOPTION_PROVIDER_POSTURES,
    ADOPTION_RESULT_SCHEMA,
    ADOPTION_RUN_EVENT_TYPE,
    ADOPTION_RUN_SCHEMA,
    SAFE_EVENT_TYPES,
    CloudBundleError,
    adoption_result_to_event,
    build_cloud_bundle,
    build_upload_payload,
    load_event_file,
    normalize_event,
    parse_event_args,
    validate_cloud_event,
)


def _result(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "schema": ADOPTION_RESULT_SCHEMA,
        "timestamp_utc": "2026-09-04T01:00:00Z",
        "release_tag": "v1.0.4",
        "package_identity": "code-mower",
        "normalized_version": "1.0.4",
        "qualification_context": "cold_install",
        "starting_version": "",
        "ending_version": "1.0.4",
        "provider": "local_cli",
        "executor": "release_qualify",
        "host_class": "local",
        "runtime_class": "python_3.12",
        "execution_state": "executed",
        "elapsed_seconds": 12.5,
        "outcome": "pass",
        "steps": [
            {
                "id": "doctor",
                "status": "pass",
                "elapsed_seconds": 1.0,
                "warning_count": 0,
                "owner_action_count": 0,
            },
            {
                "id": "package_install",
                "status": "pass",
                "elapsed_seconds": 11.5,
                "warning_count": 0,
                "owner_action_count": 0,
            },
        ],
    }
    result.update(overrides)
    return result


class AdoptionRunContractTests(unittest.TestCase):
    def test_adoption_run_is_supported_event_type(self) -> None:
        self.assertIn(ADOPTION_RUN_EVENT_TYPE, SAFE_EVENT_TYPES)

    def test_converter_maps_local_result_to_closed_event(self) -> None:
        event = adoption_result_to_event(
            _result(),
            repo_slug="owner/repo",
            team_id="team",
            install_id="install",
            source="unit-test",
        )

        self.assertEqual(event["event_type"], ADOPTION_RUN_EVENT_TYPE)
        self.assertEqual(event["repo_slug"], "owner/repo")
        self.assertEqual(event["status"], "pass")
        self.assertEqual(
            event["dimensions"],
            {
                "adoption_run_schema": ADOPTION_RUN_SCHEMA,
                "release_tag": "v1.0.4",
                "package_identity": "code-mower",
                "normalized_version": "1.0.4",
                "qualification_context": "cold_install",
                "starting_version": "",
                "ending_version": "1.0.4",
                "provider": "local_cli",
                "executor": "release_qualify",
                "host_class": "local",
                "runtime_class": "python_3.12",
                "execution_state": "executed",
                "outcome": "pass",
                "result_timestamp": "2026-09-04T01:00:00Z",
                "provenance_coverage": "complete",
            },
        )
        self.assertEqual(
            event["metrics"],
            {
                "adoption_run_count": 1,
                "elapsed_seconds": 12.5,
                "step_count": 2,
                "step_pass_count": 2,
                "step_warn_count": 0,
                "step_fail_count": 0,
                "step_unavailable_count": 0,
                "step_planned_count": 0,
                "warning_count": 0,
                "owner_action_count": 0,
            },
        )
        self.assertEqual(event["tool"]["role"], "reporter")
        self.assertEqual(event["tool"]["tool_name"], "code-mower")
        self.assertEqual(event["tool"]["model_source"], "not_applicable")
        validate_cloud_event(event)

    def test_converter_aggregates_warnings_and_owner_actions(self) -> None:
        event = adoption_result_to_event(
            _result(
                outcome="pass_with_warnings",
                steps=[
                    {
                        "id": "doctor",
                        "status": "warn",
                        "elapsed_seconds": 1.0,
                        "warning_count": 2,
                        "owner_action_count": 1,
                    },
                    {
                        "id": "lanes_status",
                        "status": "unavailable",
                        "elapsed_seconds": 0.5,
                        "warning_count": 1,
                        "owner_action_count": 0,
                    },
                    {
                        "id": "package_install",
                        "status": "pass",
                        "elapsed_seconds": 10.0,
                        "warning_count": 0,
                        "owner_action_count": 0,
                    },
                ],
            )
        )

        self.assertEqual(event["metrics"]["step_count"], 3)
        self.assertEqual(event["metrics"]["step_warn_count"], 1)
        self.assertEqual(event["metrics"]["step_unavailable_count"], 1)
        self.assertEqual(event["metrics"]["step_pass_count"], 1)
        self.assertEqual(event["metrics"]["step_planned_count"], 0)
        self.assertEqual(event["metrics"]["warning_count"], 3)
        self.assertEqual(event["metrics"]["owner_action_count"], 1)
        validate_cloud_event(event)

    def test_converter_supports_upgrade_and_planned_runs(self) -> None:
        upgrade = adoption_result_to_event(
            _result(
                qualification_context="upgrade",
                starting_version="1.0.3",
                ending_version="1.0.4",
            )
        )
        self.assertEqual(upgrade["dimensions"]["starting_version"], "1.0.3")
        validate_cloud_event(upgrade)

        planned = adoption_result_to_event(
            _result(
                execution_state="planned",
                outcome="incomplete",
                ending_version="",
                elapsed_seconds=0.0,
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
        )
        self.assertEqual(planned["metrics"]["step_planned_count"], 1)
        validate_cloud_event(planned)

    def test_missing_model_token_cost_stay_omitted_never_zero_filled(self) -> None:
        event = adoption_result_to_event(_result())

        for key in event["metrics"]:
            self.assertNotIn("cost", key)
            self.assertNotIn("token", key)
        self.assertNotIn("reported_cost_usd", event["metrics"])
        self.assertNotIn("cost_usd", event["metrics"])
        self.assertEqual(event["tool"]["model"], "")
        self.assertNotIn("report_text", event)
        self.assertNotIn("reports", event)

    def test_normalize_event_defaults_reporter_provenance(self) -> None:
        event = normalize_event(
            {
                "repo_slug": "owner/repo",
                "source": "unit-test",
                "status": "pass",
                "metrics": {
                    "adoption_run_count": 1,
                    "elapsed_seconds": 3.0,
                    "step_count": 0,
                    "step_pass_count": 0,
                    "step_warn_count": 0,
                    "step_fail_count": 0,
                    "step_unavailable_count": 0,
                    "step_planned_count": 0,
                    "warning_count": 0,
                    "owner_action_count": 0,
                },
                "dimensions": {
                    "adoption_run_schema": ADOPTION_RUN_SCHEMA,
                    "release_tag": "v1.0.4",
                    "package_identity": "code-mower",
                    "normalized_version": "1.0.4",
                    "qualification_context": "unknown",
                    "starting_version": "",
                    "ending_version": "",
                    "provider": "local_cli",
                    "executor": "release_qualify",
                    "host_class": "unknown",
                    "runtime_class": "unknown",
                    "execution_state": "planned",
                    "outcome": "incomplete",
                    "result_timestamp": "2026-09-04T01:00:00Z",
                    "provenance_coverage": "unknown",
                },
            },
            ADOPTION_RUN_EVENT_TYPE,
        )

        self.assertEqual(event["tool"]["role"], "reporter")
        self.assertEqual(event["tool"]["tool_name"], "code-mower")

    def test_rejects_undeclared_prose_path_and_output_channels(self) -> None:
        base = adoption_result_to_event(_result())

        prose = copy.deepcopy(base)
        prose["dimensions"]["summary"] = "untrusted prose"
        with self.assertRaisesRegex(CloudBundleError, "unsupported adoption_run dimension"):
            validate_cloud_event(prose)

        metric = copy.deepcopy(base)
        metric["metrics"]["cost_usd"] = 0.0
        with self.assertRaisesRegex(CloudBundleError, "unsupported adoption_run metric"):
            validate_cloud_event(metric)

        output_key = copy.deepcopy(base)
        output_key["raw_stdout"] = "captured output"
        with self.assertRaisesRegex(CloudBundleError, "unsafe field"):
            validate_cloud_event(output_key)

    def test_rejects_path_bearing_multiline_and_secret_like_data(self) -> None:
        base = adoption_result_to_event(_result())

        pathed = copy.deepcopy(base)
        pathed["dimensions"]["executor"] = "run /home/tester/work"
        with self.assertRaisesRegex(CloudBundleError, "local paths"):
            validate_cloud_event(pathed)

        multiline = copy.deepcopy(base)
        multiline["dimensions"]["executor"] = "first line\nsecond line"
        with self.assertRaisesRegex(CloudBundleError, "single-line"):
            validate_cloud_event(multiline)

        secreted = copy.deepcopy(base)
        secreted["tool"]["model"] = "ghp_" + "x" * 24
        with self.assertRaisesRegex(CloudBundleError, "secret-like value"):
            validate_cloud_event(secreted)

        with self.assertRaisesRegex(CloudBundleError, "secret-like value"):
            adoption_result_to_event(_result(executor="cmw_live_abcdefgh"))

    def test_rejects_malformed_identity_state_and_counts(self) -> None:
        cases = {
            "bad tag": {"release_tag": "1.0.4"},
            "tag version mismatch": {"normalized_version": "1.0.5"},
            "upgrade without starting": {"qualification_context": "upgrade"},
            "starting outside upgrade": {"starting_version": "1.0.3"},
            "bad provider": {"provider": "Local CLI"},
            "bad host": {"host_class": "workstation"},
            "bad runtime": {"runtime_class": "node_22"},
            "executed incomplete": {"outcome": "incomplete"},
            "negative elapsed": {"elapsed_seconds": -1.0},
            "non-finite elapsed": {"elapsed_seconds": float("inf")},
            "bad step status": {
                "steps": [
                    {
                        "id": "doctor",
                        "status": "skipped",
                        "elapsed_seconds": 1.0,
                        "warning_count": 0,
                        "owner_action_count": 0,
                    }
                ]
            },
        }
        for name, overrides in cases.items():
            with self.subTest(case=name):
                with self.assertRaises((CloudBundleError, ValueError)):
                    adoption_result_to_event(_result(**overrides))

    def test_rejects_inconsistent_step_counts_and_coverage(self) -> None:
        base = adoption_result_to_event(_result())

        summed = copy.deepcopy(base)
        summed["metrics"]["step_warn_count"] = 2
        with self.assertRaisesRegex(CloudBundleError, "must sum to step_count"):
            validate_cloud_event(summed)

        counted = copy.deepcopy(base)
        counted["metrics"]["adoption_run_count"] = 2
        with self.assertRaisesRegex(CloudBundleError, "must equal 1"):
            validate_cloud_event(counted)

        owner_action_pass = copy.deepcopy(base)
        owner_action_pass["metrics"]["owner_action_count"] = 1
        with self.assertRaisesRegex(CloudBundleError, "zero owner_action_count"):
            validate_cloud_event(owner_action_pass)

        planned_pass = copy.deepcopy(base)
        planned_pass["dimensions"]["execution_state"] = "planned"
        planned_pass["dimensions"]["outcome"] = "pass"
        with self.assertRaisesRegex(CloudBundleError, "planned runs"):
            validate_cloud_event(planned_pass)

        with self.assertRaisesRegex(CloudBundleError, "must be lower"):
            adoption_result_to_event(
                _result(qualification_context="upgrade", starting_version="1.0.4")
            )

        overstated = copy.deepcopy(base)
        overstated["dimensions"]["executor"] = "unknown"
        overstated["dimensions"]["provenance_coverage"] = "complete"
        with self.assertRaisesRegex(CloudBundleError, "provenance_coverage"):
            validate_cloud_event(overstated)

    def test_rejects_failed_step_claiming_pass(self) -> None:
        failed_pass = _result(
            outcome="pass",
            steps=[
                {
                    "id": "doctor",
                    "status": "fail",
                    "elapsed_seconds": 1.0,
                    "warning_count": 0,
                    "owner_action_count": 0,
                },
                {
                    "id": "package_install",
                    "status": "pass",
                    "elapsed_seconds": 11.5,
                    "warning_count": 0,
                    "owner_action_count": 0,
                },
            ],
        )
        with self.assertRaisesRegex(CloudBundleError, "disagrees with step statuses"):
            adoption_result_to_event(failed_pass)

        base = adoption_result_to_event(_result())
        tampered = copy.deepcopy(base)
        tampered["metrics"]["step_fail_count"] = 1
        tampered["metrics"]["step_pass_count"] = 1
        with self.assertRaisesRegex(CloudBundleError, "disagrees with step statuses"):
            validate_cloud_event(tampered)

    def test_rejects_planned_executed_outcome_mismatches(self) -> None:
        executed_planned_step = _result(
            execution_state="executed",
            outcome="pass_with_warnings",
            elapsed_seconds=1.0,
            steps=[
                {
                    "id": "doctor",
                    "status": "warn",
                    "elapsed_seconds": 1.0,
                    "warning_count": 1,
                    "owner_action_count": 0,
                },
                {
                    "id": "package_install",
                    "status": "planned",
                    "elapsed_seconds": 0.0,
                    "warning_count": 0,
                    "owner_action_count": 0,
                },
            ],
        )
        with self.assertRaisesRegex(CloudBundleError, "disagrees with step statuses"):
            adoption_result_to_event(executed_planned_step)

        planned_claiming_pass = _result(
            execution_state="planned",
            outcome="pass",
            ending_version="",
            elapsed_seconds=0.0,
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
        with self.assertRaisesRegex(CloudBundleError, "disagrees with step statuses"):
            adoption_result_to_event(planned_claiming_pass)

        planned_fail_without_failure = _result(
            execution_state="planned",
            outcome="fail",
            ending_version="",
            elapsed_seconds=0.0,
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
        with self.assertRaisesRegex(CloudBundleError, "disagrees with step statuses"):
            adoption_result_to_event(planned_fail_without_failure)

    def test_adoption_result_with_provider_posture(self) -> None:
        for posture in ADOPTION_PROVIDER_POSTURES:
            with self.subTest(posture=posture):
                event = adoption_result_to_event(_result(), provider_posture=posture)
                self.assertEqual(event["dimensions"]["provider_posture"], posture)
                self.assertNotIn("posture", event)
                validate_cloud_event(event)

    def test_adoption_result_with_invalid_provider_posture_rejected(self) -> None:
        for bad_posture in ("", "invalid", "optional", "Required"):
            with self.subTest(bad_posture=bad_posture):
                with self.assertRaisesRegex(CloudBundleError, "unsupported adoption_run provider_posture"):
                    adoption_result_to_event(_result(), provider_posture=bad_posture)

    def test_omitted_provider_posture_preserves_legacy_event_shape(self) -> None:
        event = adoption_result_to_event(_result())
        self.assertNotIn("provider_posture", event["dimensions"])
        self.assertNotIn("posture", event)
        validate_cloud_event(event)

    def test_validation_rejects_invalid_provider_posture_dimension(self) -> None:
        base = adoption_result_to_event(_result())
        bad_dim = copy.deepcopy(base)
        bad_dim["dimensions"]["provider_posture"] = "custom"
        with self.assertRaisesRegex(CloudBundleError, "unsupported adoption_run provider_posture"):
            validate_cloud_event(bad_dim)

    def test_serialized_event_stays_metadata_only(self) -> None:
        serialized = json.dumps(adoption_result_to_event(_result())).lower()
        for phrase in (
            "report text",
            "raw_diff",
            "transcript",
            "issue body",
            "source code",
            "auth output",
            "local path",
            "secret",
        ):
            self.assertNotIn(phrase, serialized)


class AdoptionRunExportTests(unittest.TestCase):
    def test_same_result_retries_share_event_id(self) -> None:
        first = adoption_result_to_event(_result())
        second = adoption_result_to_event(_result())
        self.assertEqual(first["event_id"], second["event_id"])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_bundle = build_cloud_bundle(
                reports=[],
                events=[first],
                output_dir=root / "bundle-one",
                repo_slug="owner/repo",
            )
            second_bundle = build_cloud_bundle(
                reports=[],
                events=[second],
                output_dir=root / "bundle-two",
                repo_slug="owner/repo",
            )
            self.assertEqual(
                first_bundle["event_types"], {ADOPTION_RUN_EVENT_TYPE: 1}
            )
            first_upload = build_upload_payload(
                bundle_dir=Path(first_bundle["output_dir"])
            )
            second_upload = build_upload_payload(
                bundle_dir=Path(second_bundle["output_dir"])
            )
        self.assertEqual(
            first_upload["events"][0]["event_id"],
            second_upload["events"][0]["event_id"],
        )

    def test_provider_posture_deterministic_event_id_collision_prevention(self) -> None:
        omitted = adoption_result_to_event(_result())
        req = adoption_result_to_event(_result(), provider_posture="required")
        info = adoption_result_to_event(_result(), provider_posture="informational")

        # Distinct postures produce distinct event ids for identical underlying result
        self.assertNotEqual(omitted["event_id"], req["event_id"])
        self.assertNotEqual(omitted["event_id"], info["event_id"])
        self.assertNotEqual(req["event_id"], info["event_id"])

        # Retries with same posture produce identical event ids
        req_retry = adoption_result_to_event(_result(), provider_posture="required")
        info_retry = adoption_result_to_event(_result(), provider_posture="informational")
        self.assertEqual(req["event_id"], req_retry["event_id"])
        self.assertEqual(info["event_id"], info_retry["event_id"])

    def test_export_upload_round_trip_stays_metadata_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result_path = root / "adoption-result.json"
            result_path.write_text(json.dumps(_result()), encoding="utf-8")

            events = parse_event_args([f"{ADOPTION_RUN_EVENT_TYPE}={result_path}"])
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event_type"], ADOPTION_RUN_EVENT_TYPE)

            exported = build_cloud_bundle(
                reports=[],
                events=events,
                output_dir=root / "bundle",
                repo_slug="owner/repo",
                team_id="team",
                install_id="install",
            )
            self.assertTrue(exported["upload_ready"])
            self.assertEqual(exported["upload_status"], "ready_for_dry_run")
            self.assertEqual(exported["event_types"], {ADOPTION_RUN_EVENT_TYPE: 1})

            upload = build_upload_payload(bundle_dir=root / "bundle")
            self.assertEqual(upload["upload_mode"], "metadata_only")
            self.assertEqual(upload["reports"], [])
            self.assertEqual(len(upload["events"]), 1)
            serialized_events = json.dumps(upload["events"]).lower()
            for phrase in (
                "report text",
                "raw_diff",
                "transcript",
                "issue body",
                "source code",
                "auth output",
                "stdout",
                "secret",
                "local path",
            ):
                self.assertNotIn(phrase, serialized_events)
            serialized_upload = json.dumps(upload).lower()
            for phrase in ("report text", "issue body"):
                self.assertNotIn(phrase, serialized_upload)

    def test_bundle_propagates_cli_identity_into_converted_adoption_event(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result_path = root / "adoption-result.json"
            result_path.write_text(json.dumps(_result()), encoding="utf-8")

            # Real export path: the file converter leaves envelope identity
            # empty; the bundle call carries the caller-supplied identity.
            events = parse_event_args([f"{ADOPTION_RUN_EVENT_TYPE}={result_path}"])
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["repo_slug"], "")
            self.assertEqual(events[0]["team_id"], "")
            self.assertEqual(events[0]["install_id"], "")

            exported = build_cloud_bundle(
                reports=[],
                events=events,
                output_dir=root / "bundle",
                repo_slug="owner/repo",
                team_id="team",
                install_id="install",
            )
            self.assertEqual(exported["event_types"], {ADOPTION_RUN_EVENT_TYPE: 1})

            manifest_path = root / "bundle" / "code-mower-cloud-bundle.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["events"]), 1)
            converted = manifest["events"][0]
            self.assertEqual(converted["repo_slug"], "owner/repo")
            self.assertEqual(converted["team_id"], "team")
            self.assertEqual(converted["install_id"], "install")
            validate_cloud_event(converted)

            upload = build_upload_payload(bundle_dir=root / "bundle")
            self.assertEqual(len(upload["events"]), 1)
            self.assertEqual(upload["events"][0]["repo_slug"], "owner/repo")
            self.assertEqual(upload["events"][0]["team_id"], "team")
            self.assertEqual(upload["events"][0]["install_id"], "install")

    def test_load_event_file_converts_adoption_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result_path = Path(tmp) / "adoption-result.json"
            result_path.write_text(json.dumps(_result()), encoding="utf-8")

            events = load_event_file(result_path, ADOPTION_RUN_EVENT_TYPE)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], ADOPTION_RUN_EVENT_TYPE)
        self.assertEqual(
            events[0]["dimensions"]["adoption_run_schema"], ADOPTION_RUN_SCHEMA
        )
        validate_cloud_event(events[0])

    def test_cli_upload_dry_run_previews_adoption_event_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result_path = root / "adoption-result.json"
            result_path.write_text(json.dumps(_result()), encoding="utf-8")
            events = load_event_file(result_path, ADOPTION_RUN_EVENT_TYPE)
            build_cloud_bundle(
                reports=[],
                events=events,
                output_dir=root / "bundle",
                repo_slug="owner/repo",
            )

            stdout = StringIO()
            with mock.patch.dict(
                os.environ,
                {
                    "HOME": str(root),
                    "CODE_MOWER_CLOUD_ENDPOINT": "http://127.0.0.1:9/api/ingest",
                },
                clear=False,
            ):
                with redirect_stdout(stdout):
                    code = cloud_cli.main(
                        [
                            "upload",
                            str(root / "bundle"),
                            "--dry-run",
                            "--token-env",
                            "CODE_MOWER_TEST_ADOPTION_TOKEN",
                            "--json",
                        ]
                    )

            self.assertEqual(code, 0)
            preview = json.loads(stdout.getvalue())
            self.assertEqual(preview["mode"], "cloud-upload-dry-run")
            self.assertFalse(preview["would_upload"])
            self.assertEqual(preview["event_count"], 1)
            self.assertEqual(preview["upload_mode"], "metadata_only")

    def test_backward_compatible_with_bundles_that_omit_adoption_run(self) -> None:
        legacy = {
            "schema": "code_mower.benchmarkEvent.v1",
            "event_id": "legacy-1",
            "event_type": "reviewer_run",
            "created_at": "2026-09-03T12:00:00Z",
            "repo_slug": "owner/repo",
            "team_id": "",
            "install_id": "",
            "source": "unit-test",
            "provider": "codex",
            "lens": "base",
            "status": "pass",
            "tool": {
                "role": "reviewer",
                "tool_name": "codex",
                "provider": "codex",
                "integration": "cli",
            },
            "metrics": {},
            "dimensions": {},
        }
        validate_cloud_event(legacy)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exported = build_cloud_bundle(
                reports=[],
                events=[legacy],
                output_dir=root / "bundle",
                repo_slug="owner/repo",
            )
            self.assertNotIn(ADOPTION_RUN_EVENT_TYPE, exported["event_types"])
            upload = build_upload_payload(bundle_dir=root / "bundle")
        self.assertEqual(len(upload["events"]), 1)
        self.assertEqual(upload["events"][0]["event_type"], "reviewer_run")


if __name__ == "__main__":
    unittest.main()
