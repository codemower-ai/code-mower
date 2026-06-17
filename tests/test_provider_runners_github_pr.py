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


if __name__ == "__main__":
    unittest.main()
