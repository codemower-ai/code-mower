import unittest
from datetime import UTC, datetime
from unittest import mock

from code_mower.doctor_checks.audit_limits import check_recent_pr_diff_median
from code_mower.doctor_checks.github_actions_permissions import check_actions_permissions
from code_mower.doctor_checks.github_actions_failure_scan import (
    _check_run_id_from_actions_job,
    inspect_recent_actions_failures,
)
from code_mower.doctor_checks.github_actions_failures import (
    _check_recent_actions_billing_blocks,
)
from code_mower.doctor_checks.github_branch import check_branch_protection
from code_mower.doctor_checks.github_config import (
    check_repository_posture,
    configured_repositories,
    selected_saas_or_hosted_lanes,
)
from code_mower.doctor_checks.github_human_token import (
    check_human_automation_token,
    human_automation_token_required,
)
from code_mower.doctor_checks.github_provider import check_private_repo_provider_surface
from code_mower.doctor_checks.github_repo import (
    check_repo_auto_merge,
    check_repo_permissions,
)
from code_mower.doctor_checks.github_trusted_authors import (
    trusted_author_variable_statuses,
)


class GitHubDoctorCheckTests(unittest.TestCase):
    def _human_token_check(
        self,
        api_responses: list[tuple[object, dict[str, object]]],
        *,
        adoption_posture: str = "reviewer-gate",
    ):
        with mock.patch(
            "code_mower.doctor_checks.github_human_token._github_api_json",
            side_effect=api_responses,
        ):
            return check_human_automation_token(
                gh_path="/usr/bin/gh",
                slug="owner/repo",
                config={"owner_surface": {"dispatch_token_env": "DISPATCH_TOKEN"}},
                lanes=[("codex", {"token_env": ["DISPATCH_TOKEN", "GITHUB_TOKEN"]})],
                http_timeout=1,
                adoption_posture=adoption_posture,
                now=datetime(2026, 8, 18, tzinfo=UTC),
            )

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

    def test_repository_posture_reports_multi_repo_config(self) -> None:
        check = check_repository_posture(
            {
                "repositories": [
                    {"slug": "owner/base", "default_branch": "main"},
                    {
                        "slug": "owner/sibling",
                        "default_branch": "main",
                        "local_path_env": "SIBLING_REPO_PATH",
                    },
                ]
            }
        )

        self.assertEqual(check.status, "pass")
        self.assertIn("multi-repo posture", check.message)
        self.assertEqual(check.detail["repo_count"], 2)
        self.assertEqual(check.detail["repositories"], ["owner/base", "owner/sibling"])
        self.assertEqual(check.detail["local_path_env_count"], 1)

    def test_human_automation_token_required_for_dispatch_config(self) -> None:
        required = human_automation_token_required(
            {
                "owner_surface": {"dispatch_token_env": "DISPATCH_TOKEN"},
                "builder_identity": {"branch_prefixes": {"cursor/": "cursor"}},
            },
            [("codex", {"type": "audit", "merge_authority": True})],
        )

        self.assertTrue(required)

    def test_human_automation_token_required_for_agent_lanes_without_merge_authority(
        self,
    ) -> None:
        required = human_automation_token_required(
            {
                "owner_surface": {"dispatch_token_env": "DISPATCH_TOKEN"},
                "builder_identity": {
                    "fix_round_mentions": {
                        "codex": {"mention": "@cursor"},
                    }
                },
            },
            [("gitar", {"type": "audit", "informational": True})],
        )

        self.assertTrue(required)

    def test_human_automation_token_required_for_label_triggered_audit_lanes(
        self,
    ) -> None:
        required = human_automation_token_required(
            {"owner_surface": {"dispatch_token_env": "DISPATCH_TOKEN"}},
            [
                (
                    "codex",
                    {
                        "type": "audit",
                        "driver": "local_cli",
                        "trigger_policy": "label",
                        "token_env": ["GITHUB_TOKEN"],
                    },
                )
            ],
        )

        self.assertTrue(required)

    def test_trusted_author_variable_statuses_do_not_expose_values(self) -> None:
        with mock.patch(
            "code_mower.doctor_checks.github_trusted_authors._github_api_json",
            side_effect=[
                (
                    {
                        "name": "CLAUDE_AUDIT_BOT_AUTHORS",
                        "value": "owner-login-that-must-not-print",
                    },
                    {},
                ),
                (
                    {
                        "name": "CODEX_BOT_AUTHORS",
                        "value": "codex-login-that-must-not-print",
                    },
                    {},
                ),
                (
                    {"name": "EMPTY_BOT_AUTHORS", "value": ""},
                    {},
                ),
                (
                    None,
                    {"returncode": 1, "output_summary": "not found"},
                ),
            ],
        ):
            statuses = trusted_author_variable_statuses(
                gh_path="/usr/bin/gh",
                slug="owner/repo",
                variables=[
                    "CLAUDE_AUDIT_BOT_AUTHORS",
                    "CODEX_BOT_AUTHORS",
                    "EMPTY_BOT_AUTHORS",
                    "MISSING_BOT_AUTHORS",
                ],
                http_timeout=1,
            )

        self.assertEqual(
            statuses,
            {
                "CLAUDE_AUDIT_BOT_AUTHORS": "present",
                "CODEX_BOT_AUTHORS": "present",
                "EMPTY_BOT_AUTHORS": "empty",
                "MISSING_BOT_AUTHORS": "missing",
            },
        )
        self.assertNotIn("owner-login-that-must-not-print", str(statuses))
        self.assertNotIn("codex-login-that-must-not-print", str(statuses))

    def test_human_automation_token_check_passes_with_future_expiry(self) -> None:
        check = self._human_token_check(
            [
                (
                    {
                        "name": "DISPATCH_TOKEN",
                        "created_at": "2026-08-01T00:00:00Z",
                        "updated_at": "2026-08-02T00:00:00Z",
                    },
                    {},
                ),
                (
                    {
                        "name": "DISPATCH_TOKEN_EXPIRES_AT",
                        "value": "2026-09-15",
                    },
                    {},
                ),
            ]
        )

        self.assertEqual(check.status, "pass")
        self.assertIn("expires in 28 day(s)", check.message)
        self.assertEqual(check.detail["days_remaining"], 28)
        self.assertEqual(check.detail["secret"], "DISPATCH_TOKEN")

    def test_human_automation_token_check_passes_with_non_expiring_value(self) -> None:
        check = self._human_token_check(
            [
                (
                    {
                        "name": "DISPATCH_TOKEN",
                        "created_at": "2026-08-01T00:00:00Z",
                        "updated_at": "2026-08-02T00:00:00Z",
                        "value": "secret-value-that-must-not-print",
                    },
                    {},
                ),
                ({"name": "DISPATCH_TOKEN_EXPIRES_AT", "value": "never"}, {}),
            ]
        )

        self.assertEqual(check.status, "pass")
        self.assertIn("non-expiring", check.message)
        self.assertTrue(check.detail["non_expiring"])
        self.assertEqual(check.detail["expires_at"], "never")
        self.assertNotIn("secret-value-that-must-not-print", str(check.as_dict()))

    def test_human_automation_token_check_warns_on_placeholder_expiry(self) -> None:
        check = self._human_token_check(
            [
                ({"name": "DISPATCH_TOKEN"}, {}),
                ({"name": "DISPATCH_TOKEN_EXPIRES_AT", "value": "YYYY-MM-DD"}, {}),
            ]
        )

        self.assertEqual(check.status, "warn")
        self.assertIn("placeholder DISPATCH_TOKEN_EXPIRES_AT", check.message)
        self.assertIn("`never`", str(check.remediation))

    def test_human_automation_token_check_fails_when_secret_missing(self) -> None:
        check = self._human_token_check(
            [(None, {"returncode": 1, "output_summary": "not found"})]
        )

        self.assertEqual(check.status, "fail")
        self.assertIn("missing the DISPATCH_TOKEN", check.message)
        self.assertIn("fine-grained PAT", str(check.remediation))
        self.assertEqual(check.detail["adoption_posture"], "reviewer-gate")

    def test_human_automation_token_check_warns_for_hosted_builder_missing_secret(
        self,
    ) -> None:
        check = self._human_token_check(
            [(None, {"returncode": 1, "output_summary": "not found"})],
            adoption_posture="hosted-builders",
        )

        self.assertEqual(check.status, "warn")
        self.assertIn("hosted-builder observer posture", check.message)
        self.assertEqual(check.detail["adoption_posture"], "hosted-builders")

    def test_human_automation_token_check_warns_when_expiry_metadata_missing(self) -> None:
        check = self._human_token_check(
            [
                ({"name": "DISPATCH_TOKEN"}, {}),
                (None, {"returncode": 1, "output_summary": "not found"}),
            ]
        )

        self.assertEqual(check.status, "warn")
        self.assertIn("missing the DISPATCH_TOKEN_EXPIRES_AT", check.message)
        self.assertIn("--body never", str(check.remediation))

    def test_human_automation_token_check_warns_when_expired(self) -> None:
        check = self._human_token_check(
            [
                ({"name": "DISPATCH_TOKEN"}, {}),
                ({"name": "DISPATCH_TOKEN_EXPIRES_AT", "value": "2026-08-17"}, {}),
            ]
        )

        self.assertEqual(check.status, "warn")
        self.assertIn("expired", check.message)

    def test_human_automation_token_check_warns_for_orchestrator_expired_secret(
        self,
    ) -> None:
        check = self._human_token_check(
            [
                ({"name": "DISPATCH_TOKEN"}, {}),
                ({"name": "DISPATCH_TOKEN_EXPIRES_AT", "value": "2026-08-17"}, {}),
            ],
            adoption_posture="orchestrator-only",
        )

        self.assertEqual(check.status, "warn")
        self.assertIn("expired", check.message)

    def test_human_automation_token_check_warns_on_timestamp_expiry(self) -> None:
        check = self._human_token_check(
            [
                ({"name": "DISPATCH_TOKEN"}, {}),
                (
                    {
                        "name": "DISPATCH_TOKEN_EXPIRES_AT",
                        "value": "2026-09-15T23:59:59Z",
                    },
                    {},
                ),
            ]
        )

        self.assertEqual(check.status, "warn")
        self.assertIn("invalid DISPATCH_TOKEN_EXPIRES_AT", check.message)
        self.assertIn("`never`", str(check.remediation))

    def test_human_automation_token_check_skips_when_not_required(self) -> None:
        check = check_human_automation_token(
            gh_path="/usr/bin/gh",
            slug="owner/repo",
            config={"owner_surface": {"dispatch_token_env": "DISPATCH_TOKEN"}},
            lanes=[("manual", {"driver": "manual", "type": "review"})],
            http_timeout=1,
            now=datetime(2026, 8, 18, tzinfo=UTC),
        )

        self.assertEqual(check.status, "skip")

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

    def test_actions_failure_scan_detects_billing_block_annotation(self) -> None:
        with (
            mock.patch(
                "code_mower.doctor_checks.github_actions_failure_scan._github_api_json",
                side_effect=[
                    (
                        {
                            "workflow_runs": [
                                {
                                    "id": 100,
                                    "name": "Code Mower CI",
                                    "conclusion": "failure",
                                    "head_sha": "abc123",
                                }
                            ]
                        },
                        {},
                    ),
                    (
                        {
                            "jobs": [
                                {
                                    "id": 200,
                                    "name": "package",
                                    "conclusion": "failure",
                                    "check_run_url": "https://api.github.com/repos/o/r/check-runs/300",
                                }
                            ]
                        },
                        {},
                    ),
                ],
            ),
            mock.patch(
                "code_mower.doctor_checks.github_actions_failure_scan._github_api_list",
                return_value=(
                    [{"message": "Recent account payments have failed."}],
                    {},
                ),
            ),
        ):
            inspection = inspect_recent_actions_failures(
                gh_path="/usr/bin/gh",
                slug="owner/repo",
                http_timeout=1,
            )

        self.assertTrue(inspection.has_billing_blocks)
        self.assertEqual(inspection.inspected_failed_runs, 1)
        self.assertEqual(inspection.inspected_failed_jobs, 1)
        self.assertEqual(inspection.billing_blocks[0].check_run_id, "300")

    def test_actions_failure_doctor_warns_when_annotations_cannot_be_inspected(
        self,
    ) -> None:
        with mock.patch(
            "code_mower.doctor_checks.github_actions_failures.inspect_recent_actions_failures",
            return_value=mock.Mock(
                unavailable_detail=None,
                missing_workflow_runs=False,
                has_billing_blocks=False,
                incomplete_inspections=(
                    {
                        "run_id": 100,
                        "workflow": "Code Mower CI",
                        "stage": "annotations",
                        "reason": "missing_check_run_id",
                    },
                ),
                incomplete_inspection_count=1,
                inspected_failed_runs=1,
                inspected_failed_jobs=0,
            ),
        ):
            check = _check_recent_actions_billing_blocks(
                gh_path="/usr/bin/gh",
                slug="owner/repo",
                http_timeout=1,
            )

        self.assertEqual(check.status, "warn")
        self.assertIn("could not fully inspect", check.message)
        self.assertEqual(check.detail["incomplete_inspection_count"], 1)

    def test_actions_failure_doctor_warns_when_failed_run_has_no_jobs(self) -> None:
        with mock.patch(
            "code_mower.doctor_checks.github_actions_failures.inspect_recent_actions_failures",
            return_value=mock.Mock(
                unavailable_detail=None,
                missing_workflow_runs=False,
                has_billing_blocks=False,
                incomplete_inspections=(
                    {
                        "run_id": 100,
                        "workflow": ".github/workflows/local-cli-audit.yml",
                        "stage": "jobs",
                        "reason": "no_jobs",
                    },
                ),
                inspected_failed_runs=1,
                inspected_failed_jobs=0,
            ),
        ):
            check = _check_recent_actions_billing_blocks(
                gh_path="/usr/bin/gh",
                slug="owner/repo",
                http_timeout=1,
            )

        self.assertEqual(check.status, "warn")
        self.assertIn("workflow file may be invalid", check.message)
        self.assertEqual(check.detail["jobless_run_count"], 1)
        self.assertIn("actionlint", str(check.remediation))

    def test_actions_job_check_run_id_parser(self) -> None:
        self.assertEqual(
            _check_run_id_from_actions_job(
                {"check_run_url": "https://api.github.com/repos/o/r/check-runs/12345"}
            ),
            "12345",
        )
        self.assertIsNone(_check_run_id_from_actions_job({"check_run_url": ""}))

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

    def test_branch_protection_warns_when_gate_status_is_not_required(self) -> None:
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
                required_status_context="code-mower/gate",
            )

        self.assertEqual(check.status, "warn")
        self.assertIn("code-mower/gate", check.message)
        self.assertEqual(check.detail["required_status_context"], "code-mower/gate")

    def test_branch_protection_accepts_any_source_gate_status_binding(self) -> None:
        with mock.patch(
            "code_mower.doctor_checks.github_branch._github_api_json",
            return_value=(
                {
                    "required_status_checks": {
                        "contexts": ["ci"],
                        "checks": [{"context": "code-mower/gate", "app_id": None}],
                    }
                },
                {},
            ),
        ):
            check = check_branch_protection(
                gh_path="/usr/bin/gh",
                slug="owner/repo",
                default_branch="main",
                http_timeout=1,
                required_status_context="code-mower/gate",
            )

        self.assertEqual(check.status, "pass")
        self.assertIn("code-mower/gate", check.detail["required_status_contexts"])
        self.assertEqual(
            check.detail["required_status_check_bindings"],
            [{"context": "code-mower/gate", "app_id": None}],
        )

    def test_recent_pr_diff_median_warns_above_hard_limit(self) -> None:
        with (
            mock.patch(
                "code_mower.doctor_checks.audit_limits._github_api_list",
                return_value=(
                    [{"number": 1}, {"number": 2}, {"number": 3}],
                    {},
                ),
            ),
            mock.patch(
                "code_mower.doctor_checks.audit_limits._pull_request_diff_bytes",
                side_effect=[
                    (100_000, {}),
                    (1_700_000, {}),
                    (1_900_000, {}),
                ],
            ),
        ):
            check = check_recent_pr_diff_median(
                gh_path="/usr/bin/gh",
                slug="owner/repo",
                hard_limit_bytes=1_500_000,
                http_timeout=1,
            )

        self.assertEqual(check.status, "warn")
        self.assertEqual(check.detail["median_diff_bytes"], 1_700_000)
        self.assertIn("above audit hard limit", check.message)

    def test_branch_protection_fails_when_gate_status_bound_to_actions(self) -> None:
        with mock.patch(
            "code_mower.doctor_checks.github_branch._github_api_json",
            return_value=(
                {
                    "required_status_checks": {
                        "contexts": ["code-mower/gate"],
                        "checks": [{"context": "code-mower/gate", "app_id": 15368}],
                    }
                },
                {},
            ),
        ):
            check = check_branch_protection(
                gh_path="/usr/bin/gh",
                slug="owner/repo",
                default_branch="main",
                http_timeout=1,
                required_status_context="code-mower/gate",
            )

        self.assertEqual(check.status, "fail")
        self.assertIn("GitHub Actions instead of Any source", check.message)
        self.assertIn("app_id: null", check.remediation)
        self.assertEqual(
            check.detail["required_status_check_bindings"],
            [{"context": "code-mower/gate", "app_id": 15368}],
        )

    def test_branch_protection_fails_when_gate_status_bound_to_any_app(self) -> None:
        with mock.patch(
            "code_mower.doctor_checks.github_branch._github_api_json",
            return_value=(
                {
                    "required_status_checks": {
                        "checks": [{"context": "code-mower/gate", "app_id": 12345}],
                    }
                },
                {},
            ),
        ):
            check = check_branch_protection(
                gh_path="/usr/bin/gh",
                slug="owner/repo",
                default_branch="main",
                http_timeout=1,
                required_status_context="code-mower/gate",
            )

        self.assertEqual(check.status, "fail")
        self.assertIn("specific GitHub App instead of Any source", check.message)
        self.assertIn("app_id: null", check.remediation)

    def test_repo_auto_merge_fails_when_disabled(self) -> None:
        check = check_repo_auto_merge(
            slug="owner/repo",
            repo_payload={"allow_auto_merge": False},
        )

        self.assertEqual(check.status, "fail")
        self.assertIn("allow_auto_merge=true", check.remediation)
        self.assertIn("docs/lane-promotion-policy.md", check.remediation)


if __name__ == "__main__":
    unittest.main()
