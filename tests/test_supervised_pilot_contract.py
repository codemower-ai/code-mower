from __future__ import annotations

import json
from pathlib import Path

from code_mower.cloud_client import SAFE_EVENT_TYPES, validate_cloud_event


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "supervised_pilot_events.json"
SUPERVISED_SCHEMA = "code_mower.supervisedPilot.v1"


def _fixture() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_supervised_pilot_event_types_are_supported() -> None:
    assert {
        "controller_decision",
        "merge_decision",
        "owner_intervention",
        "queue_state_snapshot",
    }.issubset(SAFE_EVENT_TYPES)


def test_supervised_pilot_fixtures_validate_as_cloud_events() -> None:
    payload = _fixture()
    events = payload["supervised_pilot_events"]

    assert len(events) == 5
    for event in events:
        validate_cloud_event(event)
        assert event["dimensions"]["supervised_pilot_schema"] == SUPERVISED_SCHEMA
        assert event["dimensions"]["next_action"]
        assert event["tool"]["role"] == "controller"


def test_legacy_v09_fixture_still_validates() -> None:
    payload = _fixture()

    for event in payload["legacy_v09_events"]:
        validate_cloud_event(event)
        assert event["event_type"] == "reviewer_run"


def test_supervised_pilot_fixtures_stay_metadata_only() -> None:
    serialized = json.dumps(_fixture()).lower()

    forbidden = (
        "raw_diff",
        "raw stdout",
        "raw_stderr",
        "transcript",
        "issue body",
        "source code",
        "auth output",
        "local path",
        "secret",
    )
    for phrase in forbidden:
        assert phrase not in serialized
