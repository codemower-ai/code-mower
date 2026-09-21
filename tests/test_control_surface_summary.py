from __future__ import annotations

import hashlib
import importlib.resources
import json
from pathlib import Path

import pytest

from code_mower.cloud_client.errors import CloudBundleError
from code_mower.cloud_client.events import validate_cloud_event
from code_mower.control_surface_summary import (
    CAPABILITY_VERSION,
    EVENT_TYPE,
    SUMMARY_SCHEMA,
    validate_control_surface_summary,
)
from code_mower.package_manifest import PACKAGE_FILES


ROOT = Path(__file__).resolve().parents[1]
RESOURCE_ROOT = ROOT / "src" / "code_mower"
PREFIX = "control_surface_session_summary"


def _fixture(name: str) -> dict:
    return json.loads((RESOURCE_ROOT / f"{PREFIX}.{name}.json").read_text())


def test_all_canonical_accepted_events_satisfy_specialized_and_cloud_boundaries() -> None:
    rows = _fixture("accepted")["events"]

    assert len(rows) == 10
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


def test_all_canonical_rejected_events_fail_closed() -> None:
    rows = _fixture("rejected")["events"]

    assert len(rows) == 16
    for row in rows:
        with pytest.raises(CloudBundleError):
            validate_control_surface_summary(row["event"])
        with pytest.raises(CloudBundleError):
            validate_cloud_event(row["event"])


def test_fixture_manifest_binds_exact_source_and_installed_resource_bytes() -> None:
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


def test_contract_resources_are_part_of_materialized_packages() -> None:
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


def test_schema_and_expectations_pin_capability_and_hosted_data_controls() -> None:
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

