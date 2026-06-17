import unittest
from unittest import mock

from code_mower import local_llm_audit_pr


class LocalLlmGitHubHelperTests(unittest.TestCase):
    def test_fetch_pull_request_delegates_to_shared_helper(self) -> None:
        with mock.patch.object(
            local_llm_audit_pr,
            "_fetch_pull_request",
            return_value={"number": 12},
        ) as fetch:
            payload = local_llm_audit_pr.fetch_pull_request("owner/repo", 12, token="ghs_token")

        self.assertEqual(payload, {"number": 12})
        fetch.assert_called_once_with("owner/repo", 12, token="ghs_token")

    def test_fetch_pull_request_rejects_non_object_payload(self) -> None:
        with mock.patch.object(local_llm_audit_pr, "_fetch_pull_request", return_value=[]):
            with self.assertRaisesRegex(ValueError, "pull request response was not an object"):
                local_llm_audit_pr.fetch_pull_request("owner/repo", 12, token="ghs_token")

    def test_fetch_pr_files_preserves_legacy_cap(self) -> None:
        with mock.patch.object(
            local_llm_audit_pr,
            "_fetch_pull_request_files",
            return_value=[{"filename": "x.py"}],
        ) as fetch_files:
            files = local_llm_audit_pr.fetch_pr_files("owner/repo", 12, token="ghs_token")

        self.assertEqual(files, [{"filename": "x.py"}])
        fetch_files.assert_called_once_with(
            "owner/repo",
            12,
            token="ghs_token",
            max_pages=5,
            per_page=100,
        )


if __name__ == "__main__":
    unittest.main()
