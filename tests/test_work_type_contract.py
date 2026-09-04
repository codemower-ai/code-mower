from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from code_mower.cloud_client import (
    CloudBundleError,
    WORK_TYPE_SCHEMA,
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

        for event in payload["conflicting_events"]:
            with self.assertRaisesRegex(CloudBundleError, "independent review"):
                validate_cloud_event(event)

    def test_author_lane_self_review_allowed_when_excluded(self) -> None:
        event = copy.deepcopy(_fixture()["conflicting_events"][0])
        event["dimensions"]["work_type_attribution"] = "excluded_self_review"
        validated = validate_cloud_event(event)
        self.assertEqual(validated["dimensions"]["work_type_attribution"], "excluded_self_review")

    def test_rejects_conflicting_role_and_attribution(self) -> None:
        cases = (
            ({"work_type_attribution": "reviewer_credit"}, "explicit_events", 0),
            ({"work_type": "quantum-computing"}, "explicit_events", 0),
        )
        for overrides, key, index in cases:
            with self.subTest(overrides=overrides):
                event = copy.deepcopy(_fixture()[key][index])
                event["dimensions"].update(overrides)
                with self.assertRaises(CloudBundleError):
                    validate_cloud_event(event)

    def test_missing_required_dimension_when_schema_present(self) -> None:
        event = copy.deepcopy(_fixture()["explicit_events"][0])
        del event["dimensions"]["work_type_role"]
        with self.assertRaisesRegex(CloudBundleError, "work_type_role"):
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
