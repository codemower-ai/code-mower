from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from code_mower import tracker_contract


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "tracker_work_items.json"


def _fixture() -> dict[str, dict[str, object]]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


class TrackerWorkItemValidationTests(unittest.TestCase):
    def test_github_and_jira_cloud_fixtures_are_valid(self) -> None:
        payload = _fixture()
        self.assertEqual(tracker_contract.validate_tracker_work_item(payload["github"]), ())
        self.assertEqual(tracker_contract.validate_tracker_work_item(payload["jira_cloud"]), ())

    def test_denylisted_field_is_rejected_with_explicit_message(self) -> None:
        item = copy.deepcopy(_fixture()["github"])
        item["description"] = "issue prose must never appear here"

        errors = tracker_contract.validate_tracker_work_item(item)

        self.assertTrue(any("description" in error and "not permitted" in error for error in errors))

    def test_unknown_field_is_rejected(self) -> None:
        item = copy.deepcopy(_fixture()["github"])
        item["unexpected_field"] = "x"

        errors = tracker_contract.validate_tracker_work_item(item)

        self.assertTrue(any("unexpected_field" in error for error in errors))

    def test_invalid_lifecycle_category_is_rejected(self) -> None:
        item = copy.deepcopy(_fixture()["github"])
        item["lifecycle_category"] = "archived"

        errors = tracker_contract.validate_tracker_work_item(item)

        self.assertTrue(any("lifecycle_category" in error for error in errors))

    def test_jira_identity_requires_cloud_project_and_issue_ids(self) -> None:
        item = copy.deepcopy(_fixture()["jira_cloud"])
        item["identity"] = {"issue_key": "ABC-123"}

        errors = tracker_contract.validate_tracker_work_item(item)

        self.assertTrue(any("identity" in error and "missing required" in error for error in errors))

    def test_optional_jira_identity_must_be_bounded_text(self) -> None:
        item = copy.deepcopy(_fixture()["jira_cloud"])
        item["identity"]["issue_key"] = {"description": "not metadata"}

        errors = tracker_contract.validate_tracker_work_item(item)

        self.assertTrue(any(error.startswith("identity.issue_key:") for error in errors))

    def test_github_identity_url_and_timestamps_are_required(self) -> None:
        item = copy.deepcopy(_fixture()["github"])
        item["identity"] = {"repo": "owner/example"}
        item["url"] = ""
        item["created_at"] = None

        errors = tracker_contract.validate_tracker_work_item(item)

        self.assertTrue(any("identity" in error and "missing required" in error for error in errors))
        self.assertTrue(any(error.startswith("url:") for error in errors))
        self.assertTrue(any(error.startswith("created_at:") for error in errors))

    def test_labels_must_use_the_stable_json_list_shape(self) -> None:
        item = copy.deepcopy(_fixture()["github"])
        item["labels"] = ("tier:R",)

        errors = tracker_contract.validate_tracker_work_item(item)

        self.assertTrue(any(error.startswith("labels:") for error in errors))

    def test_url_and_timestamps_are_validated(self) -> None:
        item = copy.deepcopy(_fixture()["github"])
        item["url"] = "not-a-url"
        item["created_at"] = "yesterday"
        item["updated_at"] = "2026-01-06T09:30:00"

        errors = tracker_contract.validate_tracker_work_item(item)

        self.assertTrue(any(error.startswith("url:") for error in errors))
        self.assertTrue(any(error.startswith("created_at:") for error in errors))
        self.assertTrue(any(error.startswith("updated_at:") for error in errors))

    def test_normalizer_preserves_invalid_missing_number_for_validation(self) -> None:
        item = tracker_contract.normalize_github_work_item(
            {
                "state": "OPEN",
                "url": "https://github.com/owner/example/issues/1",
                "createdAt": "2026-01-05T12:00:00Z",
                "updatedAt": "2026-01-06T09:30:00Z",
            },
            repo="owner/example",
        )

        errors = tracker_contract.validate_tracker_work_item(item)

        self.assertTrue(any(error.startswith("identity.number:") for error in errors))

    def test_provider_metadata_rejects_unknown_key_and_multiline_value(self) -> None:
        item = copy.deepcopy(_fixture()["jira_cloud"])
        item["provider_metadata"] = {"status_name": "line one\nline two", "custom_field": "x"}

        errors = tracker_contract.validate_tracker_work_item(item)

        self.assertTrue(any("provider_metadata.status_name" in error for error in errors))
        self.assertTrue(any("provider_metadata.custom_field" in error for error in errors))


class NormalizeGithubWorkItemTests(unittest.TestCase):
    def test_open_issue_normalizes_to_new_and_validates(self) -> None:
        raw = {
            "number": 42,
            "url": "https://github.com/owner/example/issues/42",
            "state": "OPEN",
            "labels": [{"name": "tier:R"}, {"name": "tier:R"}],
            "assignees": [],
            "createdAt": "2026-01-05T12:00:00Z",
            "updatedAt": "2026-01-06T09:30:00Z",
        }

        item = tracker_contract.normalize_github_work_item(raw, repo="owner/example")

        self.assertEqual(tracker_contract.validate_tracker_work_item(item), ())
        self.assertEqual(item["lifecycle_category"], "new")
        self.assertEqual(item["labels"], ["tier:R"])
        self.assertEqual(item["identity"], {"repo": "owner/example", "number": "42"})

    def test_closed_issue_normalizes_to_done(self) -> None:
        raw = {"number": 7, "state": "CLOSED", "assignees": [{"login": "octocat"}]}

        item = tracker_contract.normalize_github_work_item(raw, repo="owner/example")

        self.assertEqual(item["lifecycle_category"], "done")
        self.assertTrue(item["assigned"])


class TrackerCapabilitiesTests(unittest.TestCase):
    def test_github_can_read_but_never_mutates(self) -> None:
        capabilities = tracker_contract.tracker_capabilities("github")

        self.assertTrue(capabilities.can_read)
        self.assertFalse(capabilities.can_plan_mutations)
        self.assertFalse(capabilities.can_apply_mutations)

    def test_jira_cloud_mutation_planning_requires_writes_enabled_and_allowed_operations(
        self,
    ) -> None:
        disabled = tracker_contract.tracker_capabilities(
            "jira_cloud",
            {"mutations": {"writes_enabled": False, "allowed_operations": ["transition"]}},
        )
        no_ops = tracker_contract.tracker_capabilities(
            "jira_cloud", {"mutations": {"writes_enabled": True, "allowed_operations": []}}
        )
        enabled = tracker_contract.tracker_capabilities(
            "jira_cloud",
            {
                "mutations": {
                    "writes_enabled": True,
                    "allowed_operations": ["assign", "transition", "delete"],
                }
            },
        )

        self.assertFalse(disabled.can_plan_mutations)
        self.assertFalse(no_ops.can_plan_mutations)
        self.assertTrue(enabled.can_plan_mutations)
        self.assertEqual(enabled.allowed_mutation_operations, ("assign", "transition"))
        self.assertFalse(enabled.can_apply_mutations)

    def test_string_write_switch_does_not_enable_planning(self) -> None:
        capabilities = tracker_contract.tracker_capabilities(
            "jira_cloud",
            {"mutations": {"writes_enabled": "true", "allowed_operations": ["assign"]}},
        )

        self.assertFalse(capabilities.can_plan_mutations)

    def test_unsupported_kind_raises(self) -> None:
        with self.assertRaises(ValueError):
            tracker_contract.tracker_capabilities("trello")


if __name__ == "__main__":
    unittest.main()
