from __future__ import annotations

import unittest

from code_mower import config as code_mower_config
from code_mower import init as code_mower_init


def _base_config() -> dict[str, object]:
    return dict(
        code_mower_config.load_config(
            code_mower_init._resolve_config_path("code-mower.example.yml")
        )
    )


class TrackerConfigTests(unittest.TestCase):
    def test_config_without_tracker_block_still_validates(self) -> None:
        cfg = _base_config()
        self.assertNotIn("tracker", cfg)

        issues = code_mower_config.validate_config(cfg)

        self.assertFalse(any(issue.path.startswith("tracker") for issue in issues))

    def test_invalid_tracker_kind_is_rejected(self) -> None:
        cfg = _base_config()
        cfg["tracker"] = {"kind": "trello"}

        issues = code_mower_config.validate_config(cfg)

        self.assertTrue(any(issue.path == "tracker.kind" for issue in issues))

    def test_jira_cloud_kind_requires_jira_cloud_block(self) -> None:
        cfg = _base_config()
        cfg["tracker"] = {"kind": "jira_cloud"}

        issues = code_mower_config.validate_config(cfg)

        self.assertTrue(any(issue.path == "tracker.jira_cloud" for issue in issues))

    def test_jira_cloud_identity_requires_cloud_id_and_project_id(self) -> None:
        cfg = _base_config()
        cfg["tracker"] = {"kind": "jira_cloud", "jira_cloud": {}}

        issues = code_mower_config.validate_config(cfg)

        self.assertTrue(any(issue.path == "tracker.jira_cloud.cloud_id" for issue in issues))
        self.assertTrue(any(issue.path == "tracker.jira_cloud.project_id" for issue in issues))
        self.assertTrue(any(issue.path == "tracker.jira_cloud.site_url" for issue in issues))

    def test_jira_site_and_jql_are_bounded(self) -> None:
        cfg = _base_config()
        cfg["tracker"] = {
            "kind": "jira_cloud",
            "jira_cloud": {
                "site_url": "http://example.atlassian.net",
                "cloud_id": "11111111-2222-3333-4444-555555555555",
                "project_id": "10001",
                "jql": "project = 10001\nORDER BY updated",
            },
        }

        issues = code_mower_config.validate_config(cfg)

        self.assertTrue(any(issue.path == "tracker.jira_cloud.site_url" for issue in issues))
        self.assertTrue(any(issue.path == "tracker.jira_cloud.jql" for issue in issues))

    def test_unsafe_field_mapping_target_is_rejected(self) -> None:
        cfg = _base_config()
        cfg["tracker"] = {
            "kind": "jira_cloud",
            "jira_cloud": {
                "cloud_id": "11111111-2222-3333-4444-555555555555",
                "project_id": "10001",
                "field_mappings": {"description": "customfield_10099"},
            },
        }

        issues = code_mower_config.validate_config(cfg)

        self.assertTrue(
            any(issue.path == "tracker.jira_cloud.field_mappings.description" for issue in issues)
        )

    def test_unsupported_mutation_operation_is_rejected(self) -> None:
        cfg = _base_config()
        cfg["tracker"] = {
            "kind": "jira_cloud",
            "jira_cloud": {
                "cloud_id": "11111111-2222-3333-4444-555555555555",
                "project_id": "10001",
                "mutations": {"writes_enabled": True, "allowed_operations": ["delete"]},
            },
        }

        issues = code_mower_config.validate_config(cfg)

        self.assertTrue(
            any(
                issue.path == "tracker.jira_cloud.mutations.allowed_operations[0]"
                for issue in issues
            )
        )

    def test_jira_cloud_block_without_jira_cloud_kind_is_rejected(self) -> None:
        cfg = _base_config()
        cfg["tracker"] = {
            "kind": "github",
            "jira_cloud": {"cloud_id": "x", "project_id": "1"},
        }

        issues = code_mower_config.validate_config(cfg)

        self.assertTrue(any(issue.path == "tracker.jira_cloud" for issue in issues))

    def test_unknown_tracker_key_is_rejected(self) -> None:
        cfg = _base_config()
        cfg["tracker"] = {"kind": "github", "unexpected": True}

        issues = code_mower_config.validate_config(cfg)

        self.assertTrue(any(issue.path == "tracker.unexpected" for issue in issues))

    def test_full_valid_jira_cloud_tracker_block_passes(self) -> None:
        cfg = _base_config()
        cfg["tracker"] = {
            "kind": "jira_cloud",
            "jira_cloud": {
                "site_url": "https://example.atlassian.net",
                "cloud_id": "11111111-2222-3333-4444-555555555555",
                "project_id": "10001",
                "project_key": "ABC",
                "issue_type_id": "10001",
                "jql": "project = 10001 ORDER BY updated DESC",
                "status_category_map": {
                    "new": ["10000"],
                    "in_progress": ["10001"],
                    "blocked": ["10005"],
                    "done": ["10002"],
                },
                "field_mappings": {"lifecycle_category": "status"},
                "mutations": {"writes_enabled": False, "allowed_operations": []},
            },
        }

        issues = code_mower_config.validate_config(cfg)

        self.assertFalse(any(issue.path.startswith("tracker") for issue in issues))


if __name__ == "__main__":
    unittest.main()
