import unittest
from unittest import mock
import io
import json

from code_mower.provider_runners import github_pr


class GitHubPrHelperTests(unittest.TestCase):
    def test_github_json_decoder_accepts_comment_payload_over_private_state_limit(self) -> None:
        comments = [
            {"id": index, "body": "x" * 5_000, "user": {"login": "fixture"}}
            for index in range(1, 65)
        ]
        raw = json.dumps(comments).encode()
        self.assertGreater(len(raw), 256 * 1024)
        with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(raw)):
            self.assertEqual(
                github_pr._gh_request("GET", "/fixture", token="ghs_token"), comments
            )

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

    def test_fetch_pull_request_files_returns_accumulated_files_on_empty_page(self) -> None:
        page_one = [{"filename": f"file_{index}.py"} for index in range(100)]
        with mock.patch.object(
            github_pr,
            "_gh_request",
            side_effect=[page_one, []],
        ) as request:
            files = github_pr.fetch_pull_request_files("owner/repo", 12, token="ghs_token")

        self.assertEqual(files, page_one)
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

    def test_fetch_issue_comments_paginates_until_short_page(self) -> None:
        page_one = [{"id": index} for index in range(1, 101)]
        page_two = [{"id": 101}]
        with mock.patch.object(
            github_pr,
            "_gh_request",
            side_effect=[page_one, page_two, page_one, page_two],
        ) as request:
            comments = github_pr.fetch_issue_comments("owner/repo", 12, token="ghs_token")

        self.assertEqual(comments, [*page_one, *page_two])
        self.assertEqual(request.call_count, 4)
        request.assert_has_calls(
            [
                mock.call(
                    "GET",
                    "/repos/owner/repo/issues/12/comments?per_page=100&page=1",
                    token="ghs_token",
                    maximum_bytes=8388608,
                    include_response_bytes=True,
                ),
                mock.call(
                    "GET",
                    "/repos/owner/repo/issues/12/comments?per_page=100&page=2",
                    token="ghs_token",
                    maximum_bytes=8388608,
                    include_response_bytes=True,
                ),
            ]
        )

    def test_fetch_issue_comments_returns_empty_when_first_page_empty(self) -> None:
        with mock.patch.object(github_pr, "_gh_request", return_value=[]) as request:
            comments = github_pr.fetch_issue_comments("owner/repo", 12, token="ghs_token")

        self.assertEqual(comments, [])
        self.assertEqual(request.call_count, 2)
        request.assert_called_with(
            "GET",
            "/repos/owner/repo/issues/12/comments?per_page=100&page=1",
            token="ghs_token",
            maximum_bytes=8388608,
            include_response_bytes=True,
        )

    def test_fetch_issue_comments_returns_accumulated_comments_on_empty_page(self) -> None:
        page_one = [{"id": index} for index in range(1, 101)]
        with mock.patch.object(
            github_pr,
            "_gh_request",
            side_effect=[page_one, [], page_one, []],
        ) as request:
            comments = github_pr.fetch_issue_comments("owner/repo", 12, token="ghs_token")

        self.assertEqual(comments, page_one)
        self.assertEqual(request.call_count, 4)
        request.assert_has_calls(
            [
                mock.call(
                    "GET",
                    "/repos/owner/repo/issues/12/comments?per_page=100&page=1",
                    token="ghs_token",
                    maximum_bytes=8388608,
                    include_response_bytes=True,
                ),
                mock.call(
                    "GET",
                    "/repos/owner/repo/issues/12/comments?per_page=100&page=2",
                    token="ghs_token",
                    maximum_bytes=8388608,
                    include_response_bytes=True,
                ),
            ]
        )

    def test_fetch_issue_comments_rejects_full_page_cap(self) -> None:
        page = [{"id": index} for index in range(1, 101)]
        overflow = [{"id": 101}]
        with mock.patch.object(github_pr, "_gh_request", side_effect=[page, overflow]):
            with self.assertRaisesRegex(RuntimeError, "pagination cap"):
                github_pr.fetch_issue_comments(
                    "owner/repo",
                    12,
                    token="ghs_token",
                    page_cap=1,
                )


if __name__ == "__main__":
    unittest.main()
