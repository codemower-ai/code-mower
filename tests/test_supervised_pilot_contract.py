from __future__ import annotations

import json
from pathlib import Path

from unittest import TestCase

from code_mower.cloud_client import SAFE_EVENT_TYPES, validate_cloud_event
from code_mower.cloud_client.errors import CloudBundleError
from code_mower.cloud_client.events import normalize_event


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
        assert "orchestrator_provider" not in event["dimensions"]
        assert event["dimensions"]["supervised_pilot_schema"] == SUPERVISED_SCHEMA
        assert event["dimensions"]["next_action"]
        assert event["tool"]["role"] == "controller"


class ControllerOrchestratorContractTests(TestCase):
    def test_controller_events_accept_optional_orchestrator_identity(self) -> None:
        providers = [
            "codex", "claude", "cursor", "devin", "grok-bot", "antigravity", "muse",
            "custom:pilot-agent", "custom:a", "custom:" + "a" * 64,
        ]
        for provider in providers:
            for event in _fixture()["supervised_pilot_events"]:
                with self.subTest(provider=provider, event_type=event["event_type"]):
                    event["dimensions"]["orchestrator_provider"] = provider
                    self.assertEqual(validate_cloud_event(event)["dimensions"]["orchestrator_provider"], provider)
                    self.assertEqual(normalize_event(event, event["event_type"])["dimensions"]["orchestrator_provider"], provider)

    def test_controller_events_reject_invalid_orchestrator_identity(self) -> None:
        providers = [
            None, False, 42, [], {}, "", " ", "Codex", "unregistered-host",
            "gitar", "cursor-bugbot", "qodo", "greptile", "custom:", "custom:1host",
            "custom:" + "a" * 65, "custom:two words", "custom:/tmp/host",
            "custom:host@example.test", "custom:host\n", "custom:sk-" + "a" * 24,
        ]
        for provider in providers:
            for event in _fixture()["supervised_pilot_events"]:
                with self.subTest(provider=provider, event_type=event["event_type"]):
                    event["dimensions"]["orchestrator_provider"] = provider
                    with self.assertRaisesRegex(CloudBundleError, "orchestrator_provider"):
                        validate_cloud_event(event)
                    with self.assertRaisesRegex(CloudBundleError, "orchestrator_provider"):
                        normalize_event(event, event["event_type"])

    def test_legacy_events_still_validate_without_identity(self) -> None:
        payload = _fixture()
        for event in [*payload["supervised_pilot_events"], *payload["legacy_v09_events"]]:
            with self.subTest(event_type=event["event_type"]):
                self.assertNotIn("orchestrator_provider", event["dimensions"])
                validate_cloud_event(event)
                normalize_event(event, event["event_type"])


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
