from __future__ import annotations

import unittest

from code_mower.doctor_checks.provider_review_hygiene import check_review_hygiene


class DoctorProviderReviewHygieneTests(unittest.TestCase):
    def test_merge_authority_lane_requires_stale_guard(self) -> None:
        check = check_review_hygiene(
            "devin",
            {
                "merge_authority": True,
                "review_hygiene": {},
            },
        )

        self.assertEqual(check.status, "fail")
        self.assertEqual(check.name, "provider.review_hygiene")
        self.assertEqual(check.lane, "devin")
        self.assertIn("workflow", check.detail["missing"])
        self.assertIn("token_env", check.detail["missing"])
        self.assertIn("code-mower init --easy --apply", check.remediation)

    def test_template_defaults_do_not_satisfy_source_hygiene_requirement(self) -> None:
        check = check_review_hygiene(
            "codex",
            {},
            effective_lane={
                "merge_authority": True,
                "review_hygiene": {
                    "workflow": ".github/workflows/codex-clear-stale.yml",
                    "token_env": "GITHUB_TOKEN",
                },
            },
        )

        self.assertEqual(check.status, "fail")
        self.assertEqual(check.detail["missing"], ["workflow", "token_env"])
        self.assertIn("missing stale terminal-label guard config", check.message)

    def test_merge_authority_lane_passes_with_stale_guard(self) -> None:
        check = check_review_hygiene(
            "codex",
            {
                "merge_authority": True,
                "review_hygiene": {
                    "workflow": ".github/workflows/codex-clear-stale.yml",
                    "token_env": "GITHUB_TOKEN",
                    "dispatch_workflow": "codex-audit-labeler.yml",
                    "trusted_authors": ["codex[bot]"],
                },
            },
        )

        self.assertEqual(check.status, "pass")
        self.assertEqual(
            check.message,
            "stale terminal-label guard configured via .github/workflows/codex-clear-stale.yml",
        )
        self.assertEqual(check.detail["workflow"], ".github/workflows/codex-clear-stale.yml")
        self.assertEqual(check.detail["token_env"], "GITHUB_TOKEN")
        self.assertEqual(check.detail["dispatch_workflow"], "codex-audit-labeler.yml")
        self.assertEqual(check.detail["trusted_authors"], ["codex[bot]"])

    def test_merge_authority_lane_rejects_custom_hygiene_token_until_template_supports_it(self) -> None:
        check = check_review_hygiene(
            "devin",
            {
                "merge_authority": True,
                "review_hygiene": {
                    "workflow": ".github/workflows/devin-clear-stale.yml",
                    "token_env": "DEVIN_AUDIT_LABEL_TOKEN",
                },
            },
        )

        self.assertEqual(check.status, "fail")
        self.assertEqual(check.detail["token_env"], "DEVIN_AUDIT_LABEL_TOKEN")
        self.assertEqual(check.detail["supported_token_env"], "GITHUB_TOKEN")
        self.assertIn("unsupported", check.message)

    def test_informational_lane_skips_stale_guard_requirement(self) -> None:
        check = check_review_hygiene(
            "gitar",
            {
                "merge_authority": False,
                "review_hygiene": {},
            },
        )

        self.assertEqual(check.status, "skip")
        self.assertEqual(check.lane, "gitar")
        self.assertIn("optional", check.message)


if __name__ == "__main__":
    unittest.main()
