from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from code_mower.cloud_client import (
    CloudBundleError,
    WORK_TYPE_SCHEMA,
    WORK_TYPE_SUPPORTED_EVENT_TYPES,
    WORK_TYPE_VALUES,
    resolve_work_type_classification,
    validate_cloud_event,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "work_type_events.json"


def _fixture() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


class WorkTypeContractTests(unittest.TestCase):
    def test_fixtures_cover_classification_categories(self) -> None:
        payload = _fixture()
        for event in payload["legacy_events"]:
            validated = validate_cloud_event(event)
            self.assertNotIn("work_type_schema", validated["dimensions"])

        for event in payload["explicit_events"]:
            validated = validate_cloud_event(event)
            self.assertEqual(validated["dimensions"]["work_type_source"], "explicit_user")
            self.assertIn(validated["dimensions"]["work_type"], WORK_TYPE_VALUES)

        sources = {validate_cloud_event(e)["dimensions"]["work_type_source"] for e in payload["inferred_events"]}
        self.assertEqual(sources, {"repository_metadata", "file_category_metadata"})

        for event in payload["unknown_events"]:
            validated = validate_cloud_event(event)
            self.assertEqual(validated["dimensions"]["work_type"], "unknown")
            self.assertEqual(validated["dimensions"]["work_type_source"], "unknown")

    def test_author_lane_cannot_count_as_independent_review(self) -> None:
        for event in _fixture()["self_review_conflict_events"]:
            with self.assertRaisesRegex(CloudBundleError, "independent review"):
                validate_cloud_event(event)

    def test_author_lane_self_review_allowed_when_excluded(self) -> None:
        event = copy.deepcopy(_fixture()["self_review_conflict_events"][0])
        event["dimensions"]["work_type_attribution"] = "excluded_self_review"
        validated = validate_cloud_event(event)
        self.assertEqual(validated["dimensions"]["work_type_attribution"], "excluded_self_review")

    def test_unsupported_event_type_rejected(self) -> None:
        for event in _fixture()["unsupported_event_type_events"]:
            with self.subTest(event_type=event["event_type"]):
                with self.assertRaisesRegex(CloudBundleError, "not supported on event_type"):
                    validate_cloud_event(event)
        # Sanity: the fixture actually targets event types outside the allowlist.
        for event in _fixture()["unsupported_event_type_events"]:
            self.assertNotIn(event["event_type"], WORK_TYPE_SUPPORTED_EVENT_TYPES)

    def test_builder_reviewer_role_swaps_rejected(self) -> None:
        for event in _fixture()["role_mismatch_events"]:
            with self.subTest(event_type=event["event_type"]):
                with self.assertRaisesRegex(CloudBundleError, "requires work_type_role"):
                    validate_cloud_event(event)

    def test_attribution_without_role_rejected(self) -> None:
        for event in _fixture()["attribution_without_role_events"]:
            with self.assertRaisesRegex(CloudBundleError, "requires work_type_role"):
                validate_cloud_event(event)

    def test_provider_and_model_mismatch_rejected(self) -> None:
        events = _fixture()["identity_mismatch_events"]
        with self.assertRaisesRegex(CloudBundleError, "work_type_provider"):
            validate_cloud_event(events[0])
        with self.assertRaisesRegex(CloudBundleError, "work_type_model"):
            validate_cloud_event(events[1])

    def test_matching_identities_accepted(self) -> None:
        event = _fixture()["inferred_events"][0]
        validated = validate_cloud_event(event)
        self.assertEqual(validated["dimensions"]["work_type_provider"], validated["tool"]["provider"])
        self.assertEqual(validated["dimensions"]["work_type_model"], validated["tool"]["model"])

    def test_role_optional_for_work_order_and_productivity_without_guessing(self) -> None:
        for event in _fixture()["optional_role_events"]:
            with self.subTest(event_type=event["event_type"]):
                validated = validate_cloud_event(event)
                self.assertNotIn("work_type_role", validated["dimensions"])

    def test_missing_required_dimension_when_schema_present(self) -> None:
        event = copy.deepcopy(_fixture()["explicit_events"][0])
        del event["dimensions"]["work_type"]
        with self.assertRaisesRegex(CloudBundleError, "work_type"):
            validate_cloud_event(event)

    def test_rejects_unsupported_work_type_value(self) -> None:
        event = copy.deepcopy(_fixture()["explicit_events"][0])
        event["dimensions"]["work_type"] = "quantum-computing"
        with self.assertRaisesRegex(CloudBundleError, "unsupported work_type"):
            validate_cloud_event(event)

    def test_fixture_stays_metadata_only(self) -> None:
        serialized = json.dumps(_fixture()).lower()
        for phrase in (
            "raw_diff", "transcript", "issue body", "source code",
            "auth output", "local path", "secret", ".swift", ".py",
        ):
            self.assertNotIn(phrase, serialized)


class WorkTypeClassificationPrecedenceTests(unittest.TestCase):
    def test_precedence_order(self) -> None:
        cases = (
            (
                {"explicit": "documentation", "repository_language": "swift", "file_category": "android-app"},
                ("documentation", "explicit_user"),
            ),
            (
                {"repository_language": "kotlin", "file_category": "web-frontend"},
                ("android", "repository_metadata"),
            ),
            (
                {"repository_language": "cobol", "file_category": "infra-config"},
                ("infrastructure", "file_category_metadata"),
            ),
            ({}, ("unknown", "unknown")),
        )
        for kwargs, expected in cases:
            with self.subTest(kwargs=kwargs):
                self.assertEqual(resolve_work_type_classification(**kwargs), expected)

    def test_rejects_unsupported_explicit_value(self) -> None:
        with self.assertRaisesRegex(CloudBundleError, "unsupported explicit work_type"):
            resolve_work_type_classification(explicit="quantum-computing")

    def test_schema_constant_is_versioned(self) -> None:
        self.assertTrue(WORK_TYPE_SCHEMA.endswith(".v1"))


if __name__ == "__main__":
    unittest.main()
