"""GraphQL boundary tests use independent GitHub fixtures, never provider replies."""
import copy
import json
import pickle
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from code_mower.github_builder_evidence import GitHubBuilderEvidence, MAX_BYTES
from code_mower.remote_session import RemoteError


class GitHubTests(unittest.TestCase):
    def setUp(self):
        self.raw = {
            "number": 42, "state": "OPEN", "repository": {"nameWithOwner": "owner/repo"},
            "author": {"login": "builder[bot]", "databaseId": 123},
            "headRepository": {"nameWithOwner": "owner/repo"}, "headRefName": "devin/907",
            "headRefOid": "a" * 40, "baseRefName": "main",
            "closingIssuesReferences": {"nodes": [
                {"number": 907, "repository": {"nameWithOwner": "owner/repo"}}],
                "pageInfo": {"hasNextPage": False}},
        }
        self.calls = []
        def runner(query, variables, headers):
            self.calls.append((query, variables))
            return {"data": {"repository": {
                "pullRequest": self.raw,
                "pullRequests": {"nodes": [self.raw], "pageInfo": {"hasNextPage": False}},
            }}}
        self.client = GitHubBuilderEvidence("test-key", runner=runner)

    def test_queries_are_bounded_read_only_and_metadata_only(self):
        page = self.client.candidates("owner/repo", "devin/907", limit=2)
        self.assertTrue(page.complete)
        self.assertEqual(page.items[0], self.client.read("owner/repo", 42))
        queries = json.dumps(self.calls)
        for forbidden in ("mutation", "diff", "body", "files", "reviews", "test-key"):
            self.assertNotIn(forbidden, queries)
        self.assertIn("first:2", queries)
        self.assertIn("OPEN,CLOSED,MERGED", queries)
        self.assertEqual(self.calls[0][1], {"owner": "owner", "name": "repo", "branch": "devin/907"})
        with self.assertRaises(TypeError):
            pickle.dumps(self.client)

    def test_malformed_incomplete_and_secret_errors_fail_closed(self):
        for bad in (None, {"errors": [{"message": "PRIVATE_CANARY"}]},
                    {"data": {"repository": None}}, {"secret": "x" * (MAX_BYTES + 1)}):
            with patch.object(self.client, "_runner", return_value=bad):
                with self.assertRaisesRegex(RemoteError, "^github_unavailable$"):
                    self.client.read("owner/repo", 42)
        with patch.object(self.client, "_runner", side_effect=RuntimeError("PRIVATE_CANARY")):
            with self.assertRaisesRegex(RemoteError, "^github_unavailable$"):
                self.client.read("owner/repo", 42)
        original = copy.deepcopy(self.raw)
        for mutation in ({"author": None}, {"headRepository": None},
                         {"closingIssuesReferences": {"nodes": [], "pageInfo": {"hasNextPage": True}}}):
            self.raw = {**original, **mutation}
            with self.assertRaisesRegex(RemoteError, "github_invalid_response"):
                self.client.read("owner/repo", 42)
        with patch.object(self.client, "_runner", return_value={"data": {"repository": {
                "pullRequests": {"nodes": [], "pageInfo": {"hasNextPage": True}}}}}):
            self.assertFalse(self.client.candidates("owner/repo", "devin/907").complete)

    def test_invalid_requests_and_deadline(self):
        for repository in ("owner/repo/other", "owner/repo\\n", None):
            with self.assertRaisesRegex(RemoteError, "invalid_request"):
                self.client.read(repository, 42)
        with self.assertRaises(RemoteError):
            self.client.candidates("owner/repo", "devin/907", limit=100)
        with patch("code_mower.github_builder_evidence.time.monotonic", side_effect=[0, 31]):
            with self.assertRaisesRegex(RemoteError, "github_unavailable"):
                self.client.read("owner/repo", 42)


if __name__ == "__main__":
    unittest.main()
