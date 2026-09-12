"""Offline contract and adversarial tests for the Board read model."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from code_mower import remote_session
from code_mower.board_observation import (
    ACTIONS,
    ACTORS,
    MAX_BYTES,
    REASONS,
    SCHEMA,
    STAGES,
    BoardObservationError,
    decode,
    derive_primary,
    ordered_reasons,
    schema,
    validate,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "board_observations.json"


def fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def apply_fixture_values(record: dict, values: dict[str, object]) -> dict:
    result = copy.deepcopy(record)
    for pointer, value in values.items():
        if pointer == "":
            result = copy.deepcopy(value)
            continue
        parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer.split("/")[1:]]
        target: object = result
        for part in parts[:-1]:
            target = target[int(part)] if isinstance(target, list) else target[part]
        if isinstance(target, list):
            target[int(parts[-1])] = copy.deepcopy(value)
        else:
            target[parts[-1]] = copy.deepcopy(value)
    return result


def case_record(case: dict) -> dict:
    template = fixture()["templates"][case["template"]]
    return apply_fixture_values(template, case["set"])


def named_record(name: str) -> dict:
    case = next(item for item in fixture()["valid"] if item["name"] == name)
    return case_record(case)


def test_valid_fixtures_cover_frozen_truth_states() -> None:
    expected = {
        "assigned",
        "dispatched",
        "observed_running",
        "provider_reported_progress",
        "waiting_for_user",
        "waiting_for_approval",
        "implementation_complete",
        "failed",
        "cancelled",
        "reviewed",
        "ready",
        "publisher_pass_gate_pending",
        "merged",
        "stale",
        "source_unavailable_preserves_last_observation",
        "unlinked",
        "no_work",
    }
    cases = fixture()["valid"]

    assert {case["name"] for case in cases} == expected
    for case in cases:
        record = case_record(case)
        assert validate(record) == record


def test_adversarial_fixtures_fail_closed_with_bounded_diagnostics() -> None:
    for case in fixture()["adversarial"]:
        record = apply_fixture_values(named_record(case["base"]), case["set"])
        with pytest.raises(BoardObservationError, match=f"^{case['error']}$"):
            validate(record)


def test_multiple_reasons_have_one_canonical_primary_route() -> None:
    reasons = ["review_requested", "ci_failed", "approval_required"]

    assert ordered_reasons(reasons) == ["approval_required", "ci_failed", "review_requested"]
    assert derive_primary(reasons) == {"actor": "owner", "action": "respond_to_approval"}

    record = copy.deepcopy(named_record("observed_running"))
    record["work"]["reasons"] = ordered_reasons(reasons)
    record["work"]["primary"] = derive_primary(reasons)
    assert validate(record)["work"]["primary"]["actor"] == "owner"


def test_reason_duplicates_and_noncanonical_order_fail() -> None:
    with pytest.raises(BoardObservationError, match="invalid_reason"):
        derive_primary(["ci_pending", "ci_pending"])

    record = copy.deepcopy(named_record("observed_running"))
    record["work"]["reasons"] = ["review_requested", "approval_required"]
    record["work"]["primary"] = derive_primary(record["work"]["reasons"])
    with pytest.raises(BoardObservationError, match="invalid_route"):
        validate(record)


def test_provider_lifecycle_is_exactly_the_existing_public_projection() -> None:
    record = named_record("waiting_for_approval")
    lifecycle = record["work"]["runs"][0]["lifecycle"]

    assert remote_session.public_projection(lifecycle) == lifecycle
    lifecycle_schema = schema()["$defs"]["lifecycle"]["properties"]
    assert set(lifecycle_schema["state"]["enum"]) == remote_session.STATES
    assert set(lifecycle_schema["reason"]["enum"]) == remote_session.REASONS
    assert set(lifecycle_schema["next_action"]["enum"]) == remote_session.ACTIONS


def test_request_label_lease_and_gate_publisher_do_not_claim_execution_or_gate() -> None:
    assigned = validate(named_record("assigned"))
    assert assigned["work"]["evidence"]["lease"]["state"] == "held"
    assert assigned["work"]["runs"][0]["phase"] == "assigned"
    assert assigned["work"]["runs"][0]["basis"] == "configured"

    lease_only = copy.deepcopy(assigned)
    lease_only["work"]["runs"] = []
    assert validate(lease_only)["work"]["runs"] == []

    requested = copy.deepcopy(named_record("implementation_complete"))
    requested["work"]["evidence"]["review_request"] = {
        "state": "requested",
        "source_id": "githubobs",
    }
    assert validate(requested)["work"]["evidence"]["review"]["state"] == "unknown"

    published = validate(named_record("publisher_pass_gate_pending"))
    assert published["work"]["evidence"]["gate_publisher"]["state"] == "pass"
    assert published["work"]["evidence"]["gate"]["state"] == "pending"


def test_unavailable_source_preserves_last_observation_without_live_claim() -> None:
    record = validate(named_record("source_unavailable_preserves_last_observation"))
    unavailable = next(source for source in record["sources"] if source["id"] == "providerobs")

    assert unavailable["freshness"] == "unavailable"
    assert unavailable["coverage"] == "unavailable"
    assert unavailable["observed_at"] is not None
    assert record["work"]["runs"] == []
    assert record["work"]["primary"] == {
        "actor": "orchestrator",
        "action": "restore_source",
    }


def test_no_work_requires_fresh_complete_session_queue_and_run_coverage() -> None:
    record = copy.deepcopy(named_record("no_work"))
    assert validate(record)["kind"] == "no_work"

    record["sources"][2]["coverage"] = "partial"
    with pytest.raises(BoardObservationError, match="insufficient_coverage"):
        validate(record)


def test_unknown_measurement_is_null_not_zero_or_success() -> None:
    measurements = validate(named_record("observed_running"))["work"]["measurements"]
    for measurement in measurements.values():
        assert measurement == {
            "value": None,
            "coverage": "unavailable",
            "observed": 0,
            "total": None,
        }


def test_authorized_session_label_is_explicit_and_bounded() -> None:
    record = copy.deepcopy(named_record("no_work"))
    assert validate(record)["display"]["session_label"] == "Board contract delivery"

    record["display"]["session_label"] = "/Users/example/private"
    with pytest.raises(BoardObservationError, match="privacy_violation"):
        validate(record)


def test_decode_is_bounded_duplicate_safe_and_finite() -> None:
    record = named_record("observed_running")
    assert decode(json.dumps(record).encode()) == record
    with pytest.raises(BoardObservationError, match="invalid_contract"):
        decode(b'{"schema":"a","schema":"b"}')
    with pytest.raises(BoardObservationError, match="invalid_contract"):
        decode(b"x" * (MAX_BYTES + 1))

    nonfinite = copy.deepcopy(record)
    nonfinite["work"]["measurements"]["cost_usd"] = {
        "value": float("nan"),
        "coverage": "complete",
        "observed": 1,
        "total": 1,
    }
    with pytest.raises(BoardObservationError, match="invalid_contract"):
        validate(nonfinite)


def test_validation_returns_a_detached_copy() -> None:
    record = named_record("observed_running")
    result = validate(record)
    result["sources"][0]["freshness"] = "stale"
    assert record["sources"][0]["freshness"] == "fresh"


def test_schema_is_closed_and_constants_do_not_drift() -> None:
    contract = schema()
    assert contract["$id"] == SCHEMA
    assert contract["additionalProperties"] is False
    work = contract["$defs"]["work"]["properties"]
    assert set(work["stage"]["enum"]) == STAGES
    assert set(work["reasons"]["items"]["enum"]) == REASONS
    primary = contract["$defs"]["primary"]["properties"]
    assert set(primary["actor"]["enum"]) == ACTORS
    assert set(primary["action"]["enum"]) == ACTIONS


def test_fixture_is_metadata_only() -> None:
    payload = fixture()
    forbidden_keys = {
        "body",
        "description",
        "diff",
        "prompt",
        "transcript",
        "context",
        "graph",
        "credentials",
        "raw_output",
        "path",
        "pid",
        "secret",
        "token",
    }

    def visit(value: object) -> None:
        if isinstance(value, dict):
            assert not (set(value) & forbidden_keys)
            if "message" in value:
                assert type(value["message"]) is int
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, str):
            assert not value.startswith(("/Users/", "/home/"))
            assert "github_pat_" not in value

    visit(payload)


def test_packaged_contract_files_are_declared() -> None:
    from code_mower.package_manifest import PACKAGE_FILES

    targets = {target for _source, target, _mode in PACKAGE_FILES}
    assert "src/code_mower/board_observation.py" in targets
    assert "src/code_mower/board_observation.schema.json" in targets
