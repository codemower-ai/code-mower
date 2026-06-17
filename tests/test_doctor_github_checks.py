import unittest
from unittest import mock

from code_mower.doctor_checks.github_actions_permissions import check_actions_permissions
from code_mower.doctor_checks.github_branch import check_branch_protection
from code_mower.doctor_checks.github_config import (
    configured_repositories,
    selected_saas_or_hosted_lanes,
)
from code_mower.doctor_checks.github_provider import check_private_repo_provider_surface
from code_mower.doctor_checks.github_repo import check_repo_permissions


class GitHubDoctorCheckTests(unittest.TestCase):
    def test_config_helpers_filter_repositories_and_hosted_lanes(self) -> None:
        repos = configured_repositories(
            {
                "repositories": [
                    {"slug": "owner/repo"},
                    {"name": "missing-slug"},
                    "not-a-mapping",
                ]
            }
        )
        self.assertEqual(tuple(repo["slug"] for repo in repos), ("owner/repo",))

        lanes = selected_saas_or_hosted_lanes(
            [
                ("codex", {"driver": "local_cli"}),
                ("gitar", {"driver": "saas_event"}),
                ("devin", {"driver": "hosted_bridge"}),
            ]
        )
        self.assertEqual(lanes, ["gitar", "devin"])

    def test_private_repo_provider_check_warns_for_hosted_lanes(self) -> None:
        check = check_private_repo_provider_surface(
            private_repos=["owner/private"],
            unknown_visibility_repos=[],
            selected_saas_or_hosted=["gitar"],
        )

        self.assertIsNotNone(check)
        assert check is not None
        self.assertEqual(check.status, "warn")
        self.assertIn("private repos selected", check.message)

    def test_repo_permissions_warn_for_read_only_metadata(self) -> None:
        check = check_repo_permissions(
            slug="owner/repo",
            repo_payload={"permissions": {"pull": True, "push": False}},
        )

        self.assertEqual(check.status, "warn")
        self.assertIn("read-only", check.message)

    def test_actions_permissions_reports_disabled_actions(self) -> None:
        with mock.patch(
            "code_mower.doctor_checks.github_actions_permissions._github_api_json",
            return_value=({"enabled": False, "allowed_actions": "all"}, {}),
        ):
            check = check_actions_permissions(
                gh_path="/usr/bin/gh",
                slug="owner/repo",
                http_timeout=1,
            )

        self.assertEqual(check.status, "warn")
        self.assertEqual(check.detail["enabled"], False)

    def test_branch_protection_counts_required_contexts(self) -> None:
        with mock.patch(
            "code_mower.doctor_checks.github_branch._github_api_json",
            return_value=(
                {"required_status_checks": {"contexts": ["ci", "package"]}},
                {},
            ),
        ):
            check = check_branch_protection(
                gh_path="/usr/bin/gh",
                slug="owner/repo",
                default_branch="main",
                http_timeout=1,
            )

        self.assertEqual(check.status, "pass")
        self.assertEqual(check.detail["required_status_check_count"], 2)


if __name__ == "__main__":
    unittest.main()
