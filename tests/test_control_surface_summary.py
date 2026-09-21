from __future__ import annotations

import hashlib
import importlib.resources
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest import TestCase, mock

from code_mower.cloud_client.errors import CloudBundleError
from code_mower.cloud_client.events import validate_cloud_event
from code_mower.cloud_client.endpoints import probe_cloud_service
from code_mower.board_local_observation import WorkBinding
from code_mower.control_surface_summary import (
    CAPABILITY_SCHEMA,
    CAPABILITY_VERSION,
    EVENT_TYPE,
    LIFECYCLE_POLICY,
    OUTCOME_BY_STATE,
    SUMMARY_SCHEMA,
    build_control_surface_summary,
    capability_accepts_summary,
    capability_from_health,
    fixture_manifest_digest,
    gated_control_surface_summary,
    gated_control_surface_transition,
    opaque_session,
    slack_board_run,
    validate_control_surface_summary,
)
from code_mower.remote_session import FakeProvider, RemoteSessions, _key
from code_mower.package_manifest import PACKAGE_FILES


ROOT = Path(__file__).resolve().parents[1]
RESOURCE_ROOT = ROOT / "src" / "code_mower"
PREFIX = "control_surface_session_summary"


def _fixture(name: str) -> dict:
    return json.loads((RESOURCE_ROOT / f"{PREFIX}.{name}.json").read_text())


class ControlSurfaceSummaryTests(TestCase):
    def test_all_canonical_accepted_events_satisfy_specialized_and_cloud_boundaries(self) -> None:
        rows = _fixture("accepted")["events"]

        assert len(rows) == 21
        assert {row["event"]["dimensions"]["state"] for row in rows} == {
            "archived",
            "complete",
            "failed",
            "pending",
            "running",
            "suspended",
            "terminated",
            "uncertain",
            "waiting_for_approval",
            "waiting_for_user",
        }
        for row in rows:
            validate_control_surface_summary(row["event"])
            assert validate_cloud_event(row["event"]) == row["event"]


    def test_all_canonical_rejected_events_fail_closed(self) -> None:
        rows = _fixture("rejected")["events"]

        assert len(rows) == 19
        for row in rows:
            with self.assertRaises(CloudBundleError):
                validate_control_surface_summary(row["event"])
            with self.assertRaises(CloudBundleError):
                validate_cloud_event(row["event"])


    def test_fixture_manifest_binds_exact_source_and_installed_resource_bytes(self) -> None:
        manifest_path = RESOURCE_ROOT / f"{PREFIX}.fixture-manifest.json"
        manifest = json.loads(manifest_path.read_text())

        assert manifest["contract_schema"] == SUMMARY_SCHEMA
        assert manifest["digest_scope"] == "exact_file_bytes"
        assert [row["path"] for row in manifest["files"]] == sorted(
            row["path"] for row in manifest["files"]
        )
        package_root = importlib.resources.files("code_mower")
        for row in manifest["files"]:
            source = (RESOURCE_ROOT / row["path"]).read_bytes()
            installed = package_root.joinpath(row["path"]).read_bytes()
            assert installed == source
            assert len(source) == row["bytes"]
            assert hashlib.sha256(source).hexdigest() == row["sha256"]


    def test_contract_resources_are_part_of_materialized_packages(self) -> None:
        targets = {target for _source, target, _kind in PACKAGE_FILES}
        required = {
            "src/code_mower/control_surface_summary.py",
            *{
                f"src/code_mower/{PREFIX}.{suffix}.json"
                for suffix in (
                    "schema",
                    "accepted",
                    "rejected",
                    "expectations",
                    "fixture-manifest",
                )
            },
        }

        assert required <= targets


    def test_schema_and_expectations_pin_capability_and_hosted_data_controls(self) -> None:
        schema = _fixture("schema")
        expectations = _fixture("expectations")

        assert schema["$id"] == SUMMARY_SCHEMA
        assert schema["properties"]["event_type"]["const"] == EVENT_TYPE
        assert (
            schema["$defs"]["dimensions"]["properties"]["capability_version"]["const"]
            == CAPABILITY_VERSION
        )
        names = {row["name"] for row in expectations["expectations"]}
        assert {
            "capability_absent",
            "capability_mismatch",
            "rollback",
            "tenant_isolation",
            "export",
            "deletion",
            "retention",
            "token_revocation",
            "aggregate_reconciliation",
        } <= names


    def test_capability_gate_requires_exact_installed_contract_identity(self) -> None:
        capability = {
            "schema": CAPABILITY_SCHEMA,
            "summary_schema": SUMMARY_SCHEMA,
            "capability_version": CAPABILITY_VERSION,
            "fixture_manifest_sha256": fixture_manifest_digest(),
            "accepting": True,
        }

        assert capability_accepts_summary(capability)
        assert capability_from_health(
            {"capabilities": {EVENT_TYPE: capability}}
        ) == capability
        for changed in (
            {**capability, "accepting": False},
            {**capability, "capability_version": 2},
            {**capability, "fixture_manifest_sha256": "0" * 64},
            {**capability, "unknown": "field"},
            None,
        ):
            assert not capability_accepts_summary(changed)
            assert capability_from_health({"capabilities": {EVENT_TYPE: changed}}) is None


    def test_summary_builder_is_retry_stable_and_capability_gated(self) -> None:
        lifecycle = {
            "schema": "code_mower.remote_session.v1",
            "state": "complete",
            "reason": "none",
            "next_action": "none",
            "counts": {"dispatch": 1, "message": 1, "cancel": 0, "collect": 1},
        }
        values = {
            "logical_session": "private-slack-session-reference",
            "repo_slug": "example/project",
            "provider": "devin",
            "lifecycle": lifecycle,
            "observed_at": datetime(2026, 9, 21, 7, 0, tzinfo=UTC),
            "pr_number": 921,
            "head_sha": "a" * 40,
            "pr_state": "open",
            "elapsed_seconds": 60.0,
            "usage_acu": 0.25,
        }
        first = build_control_surface_summary(**values)
        second = build_control_surface_summary(**values)

        assert first == second
        assert first["dimensions"]["session"] == opaque_session(values["logical_session"])
        assert values["logical_session"] not in json.dumps(first)
        assert gated_control_surface_summary(None, **values) is None
        capability = {
            "schema": CAPABILITY_SCHEMA,
            "summary_schema": SUMMARY_SCHEMA,
            "capability_version": CAPABILITY_VERSION,
            "fixture_manifest_sha256": fixture_manifest_digest(),
            "accepting": True,
        }
        assert gated_control_surface_summary(capability, **values) == first

        later = {**values, "observed_at": datetime(2026, 9, 21, 7, 1, tzinfo=UTC)}
        assert gated_control_surface_transition(capability, first, **later) is None
        changed = {
            **later,
            "lifecycle": {
                **lifecycle,
                "counts": {**lifecycle["counts"], "message": 2},
            },
        }
        assert gated_control_surface_transition(capability, first, **changed) is not None
        assert gated_control_surface_transition(capability, {"invalid": True}, **later) is not None


    def test_archived_result_not_ready_is_a_valid_provider_owner_action(self) -> None:
        lifecycle = {
            "schema": "code_mower.remote_session.v1",
            "state": "archived",
            "reason": "result_not_ready",
            "next_action": "inspect_provider",
            "counts": {"dispatch": 1, "message": 0, "cancel": 0, "collect": 0},
        }

        event = build_control_surface_summary(
            logical_session="archived-session",
            repo_slug="example/project",
            provider="devin",
            lifecycle=lifecycle,
            observed_at=datetime(2026, 9, 21, 7, 0, tzinfo=UTC),
        )

        assert event["dimensions"]["state"] == "archived"
        assert event["dimensions"]["lifecycle_reason"] == "result_not_ready"
        assert event["dimensions"]["owner_action"] == "inspect_provider"
        validate_control_surface_summary(event)
        assert validate_cloud_event(event) == event


    def test_real_collect_lifecycles_cover_every_result_availability_state(self) -> None:
        cases = (
            ("pending", "", "pending", "result_not_ready"),
            ("running", "", "running", "result_not_ready"),
            ("owner_action", "waiting_for_owner", "waiting_for_user", "result_not_ready"),
            ("owner_action", "approval_required", "waiting_for_approval", "result_not_ready"),
            ("complete", "", "complete", "result_unavailable"),
            ("failed", "", "failed", "result_not_ready"),
            ("suspended", "", "suspended", "result_not_ready"),
            ("terminated", "", "terminated", "result_not_ready"),
            ("archived", "", "archived", "result_not_ready"),
        )
        for raw_state, raw_reason, state, reason in cases:
            with self.subTest(raw_state=raw_state, raw_reason=raw_reason), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                provider = FakeProvider(root / "provider")
                sessions = RemoteSessions(root / "sessions", provider)
                logical_session = "summary-matrix"
                sessions.run(
                    "dispatch",
                    logical_session,
                    prose="metadata-only lifecycle test",
                    repo="example/project",
                    apply=True,
                )
                record = sessions.store.read_only(_key(logical_session))
                provider.set_state(record["binding"], raw_state, reason=raw_reason)

                lifecycle = sessions.run("collect", logical_session, apply=True)
                event = build_control_surface_summary(
                    logical_session=logical_session,
                    repo_slug="example/project",
                    provider="codex",
                    lifecycle=lifecycle,
                    observed_at=datetime(2026, 9, 21, 7, 0, tzinfo=UTC),
                )

                assert (lifecycle["state"], lifecycle["reason"]) == (state, reason)
                assert event["dimensions"]["lifecycle_reason"] == reason
                assert event["dimensions"]["owner_action"] == "inspect_provider"
                assert validate_cloud_event(event) == event


    def test_schema_and_semantic_validators_enforce_the_exact_lifecycle_policy_matrix(self) -> None:
        schema = _fixture("schema")
        schema_policy = {
            (
                row["properties"]["state"]["const"],
                row["properties"]["lifecycle_reason"]["const"],
                row["properties"]["owner_action"]["const"],
            )
            for row in schema["$defs"]["dimensions"]["anyOf"]
        }
        semantic_policy = {
            (state, reason, action)
            for state, reasons in LIFECYCLE_POLICY.items()
            for reason, action in reasons.items()
        }
        assert schema_policy == semantic_policy
        base = next(
            row["event"]
            for row in _fixture("accepted")["events"]
            if row["event"]["provider"] == "codex"
        )
        reasons = sorted({reason for policy in LIFECYCLE_POLICY.values() for reason in policy})
        actions = (
            "none",
            "answer_question",
            "respond_to_approval",
            "inspect_provider",
            "inspect_failure",
        )
        for state in sorted(LIFECYCLE_POLICY):
            for reason in reasons:
                for action in actions:
                    with self.subTest(state=state, reason=reason, action=action):
                        event = json.loads(json.dumps(base))
                        event["status"] = state
                        event["dimensions"].update(
                            state=state,
                            lifecycle_reason=reason,
                            outcome=OUTCOME_BY_STATE.get(state, "unknown"),
                            owner_action=action,
                        )
                        expected = LIFECYCLE_POLICY[state].get(reason) == action
                        if expected:
                            validate_control_surface_summary(event)
                            assert validate_cloud_event(event) == event
                        else:
                            with self.assertRaises(CloudBundleError):
                                validate_control_surface_summary(event)
                            with self.assertRaises(CloudBundleError):
                                validate_cloud_event(event)


    def test_tool_provenance_is_closed_and_rejects_forbidden_nested_content(self) -> None:
        event = _fixture("accepted")["events"][0]["event"]
        for tool in (
            {**event["tool"], "message": "private Slack message"},
            {**event["tool"], "details": {"prompt": "private prompt"}},
            {**event["tool"], "runtime_environment": "developer-laptop"},
        ):
            changed = {**event, "tool": tool}
            with self.assertRaises(CloudBundleError):
                validate_control_surface_summary(changed)
            with self.assertRaises(CloudBundleError):
                validate_cloud_event(changed)


    def test_health_probe_exposes_only_the_exact_summary_capability(self) -> None:
        capability = {
            "schema": CAPABILITY_SCHEMA,
            "summary_schema": SUMMARY_SCHEMA,
            "capability_version": CAPABILITY_VERSION,
            "fixture_manifest_sha256": fixture_manifest_digest(),
            "accepting": True,
        }

        class FakeResponse:
            def __init__(self, body: dict[str, object]) -> None:
                self.body = body

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps(self.body).encode()

            def getcode(self) -> int:
                return 200

        accepted = {"capabilities": {EVENT_TYPE: capability}}
        with mock.patch(
            "urllib.request.urlopen", return_value=FakeResponse(accepted)
        ):
            check = probe_cloud_service("https://codemower.com/api/ingest", timeout=1)
        assert check["detail"][EVENT_TYPE] == capability

        mismatched = {
            "capabilities": {
                EVENT_TYPE: {**capability, "fixture_manifest_sha256": "0" * 64}
            }
        }
        with mock.patch(
            "urllib.request.urlopen", return_value=FakeResponse(mismatched)
        ):
            check = probe_cloud_service("https://codemower.com/api/ingest", timeout=1)
        assert EVENT_TYPE not in check["detail"]


    def test_slack_lifecycle_uses_existing_local_board_adapter(self) -> None:
        binding = WorkBinding(
            session_id="a" * 32,
            work_id="work1",
            repository="example/project",
            worktree_id="sha256:" + "b" * 64,
        )
        lifecycle = {
            "schema": "code_mower.remote_session.v1",
            "state": "waiting_for_user",
            "reason": "user_input_required",
            "next_action": "none",
            "counts": {"dispatch": 1, "message": 0, "cancel": 0, "collect": 0},
        }

        run = slack_board_run(
            logical_session="private-slack-session-reference",
            binding=binding,
            provider="codex",
            observed_at=datetime(2026, 9, 21, 7, 0, tzinfo=UTC),
            lifecycle=lifecycle,
        )

        assert run.binding == binding
        assert run.phase == "waiting_for_user"
        assert run.source_kind == "remote_session"
        assert run.lifecycle == lifecycle
