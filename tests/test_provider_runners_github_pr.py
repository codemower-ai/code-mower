import unittest
from unittest import mock

from code_mower.provider_runners import github_pr


class GitHubPrHelperTests(unittest.TestCase):
    def test_fetch_pull_request_diff_uses_diff_accept(self) -> None:
        with mock.patch.object(github_pr, "_gh_request", return_value="diff --git a/x b/x") as request:
            diff = github_pr.fetch_pull_request_diff("owner/repo", 12, token="ghs_token")

        self.assertEqual(diff, "diff --git a/x b/x")
        request.assert_called_once_with(
            "GET",
            "/repos/owner/repo/pulls/12",
            token="ghs_token",
            accept="application/vnd.github.v3.diff",
        )

    def test_fetch_pull_request_keeps_json_accept_default(self) -> None:
        with mock.patch.object(github_pr, "_gh_request", return_value={"number": 12}) as request:
            payload = github_pr.fetch_pull_request("owner/repo", 12, token="ghs_token")

        self.assertEqual(payload, {"number": 12})
        request.assert_called_once_with(
            "GET",
            "/repos/owner/repo/pulls/12",
            token="ghs_token",
        )

    def test_fetch_pull_request_files_paginates_until_short_page(self) -> None:
        page_one = [{"filename": f"file_{index}.py"} for index in range(100)]
        page_two = [{"filename": "last.py"}]
        with mock.patch.object(
            github_pr,
            "_gh_request",
            side_effect=[page_one, page_two],
        ) as request:
            files = github_pr.fetch_pull_request_files("owner/repo", 12, token="ghs_token")

        self.assertEqual(files, [*page_one, *page_two])
        self.assertEqual(request.call_count, 2)
        request.assert_has_calls(
            [
                mock.call(
                    "GET",
                    "/repos/owner/repo/pulls/12/files?per_page=100&page=1",
                    token="ghs_token",
                ),
                mock.call(
                    "GET",
                    "/repos/owner/repo/pulls/12/files?per_page=100&page=2",
                    token="ghs_token",
                ),
            ]
        )

    def test_fetch_pull_request_files_rejects_non_list_payload(self) -> None:
        with mock.patch.object(github_pr, "_gh_request", return_value={"message": "bad"}):
            with self.assertRaisesRegex(ValueError, "files response was not a list"):
                github_pr.fetch_pull_request_files("owner/repo", 12, token="ghs_token")


if __name__ == "__main__":
    unittest.main()
