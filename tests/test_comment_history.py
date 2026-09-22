import json
from pathlib import Path
import tempfile
import unittest

from code_mower.audit_labeler_lib import (
    CommentHistoryError,
    GitHubCommentPage,
    lineage_history,
)


def page_payload(items):
    raw = json.dumps(items, ensure_ascii=False, separators=(",", ":")).encode()
    return GitHubCommentPage(items, len(raw))


class CommentHistoryTests(unittest.TestCase):
    def comments(self, count=64, body_bytes=5_000):
        return [
            {"id": index, "body": "x" * body_bytes, "user": {"login": "fixture"}}
            for index in range(1, count + 1)
        ]

    def fetcher(self, comments, calls):
        def fetch(page, size):
            calls.append((page, size))
            start = (page - 1) * size
            return page_payload(comments[start:start + size])
        return fetch

    def test_history_larger_than_private_context_limit_is_complete_and_stable(self):
        comments = self.comments()
        calls = []
        restored = lineage_history(self.fetcher(comments, calls), return_raw=True)
        self.assertGreater(len(json.dumps(comments).encode()), 256 * 1024)
        self.assertEqual(len(restored), 64)
        self.assertEqual(calls, [(1, 100), (1, 100)])

    def test_oversized_pages_halve_and_restart_without_omission_or_duplication(self):
        comments = self.comments()
        calls = []
        restored = lineage_history(
            self.fetcher(comments, calls),
            max_response_bytes=50_000,
            max_total_bytes=2_000_000,
            return_raw=True,
        )
        self.assertEqual([item["id"] for item in restored], list(range(1, 65)))
        self.assertEqual(calls[:5], [(1, 100), (1, 50), (1, 25), (1, 12), (1, 6)])
        self.assertEqual(len({item["id"] for item in restored}), len(restored))

    def test_individual_response_total_item_and_duplicate_budgets_are_distinct(self):
        one = self.comments(1, body_bytes=1_000)
        with self.assertRaisesRegex(CommentHistoryError, "One GitHub comment"):
            lineage_history(
                self.fetcher(one, []), max_response_bytes=500,
                max_total_bytes=20_000, return_raw=True,
            )
        with self.assertRaisesRegex(CommentHistoryError, "total-byte"):
            lineage_history(
                self.fetcher(self.comments(), []), max_response_bytes=100_000,
                max_total_bytes=100_000, return_raw=True,
            )
        with self.assertRaisesRegex(CommentHistoryError, "item budget"):
            lineage_history(
                self.fetcher(self.comments(3, body_bytes=1), []), page_size=2,
                max_pages=1, return_raw=True,
            )
        duplicate = self.comments(2, body_bytes=1)
        duplicate[1]["id"] = duplicate[0]["id"]
        with self.assertRaisesRegex(CommentHistoryError, "duplicate ids"):
            lineage_history(self.fetcher(duplicate, []), return_raw=True)

    def test_mutation_between_complete_reads_fails_closed(self):
        comments = self.comments(2, body_bytes=1)
        calls = 0

        def fetch(page, size):
            nonlocal calls
            calls += 1
            current = json.loads(json.dumps(comments))
            if calls > 1:
                current[0]["body"] = "changed"
            return page_payload(current)

        with self.assertRaisesRegex(CommentHistoryError, "changed"):
            lineage_history(fetch, return_raw=True)

    def test_long_history_reaches_both_independent_local_audit_boundaries(self):
        from lineage_consumer_fixtures import REPO, complete_pr, policy, wrapper_boundary

        history = self.comments()
        for lane in ("codex", "claude"):
            with self.subTest(lane=lane), tempfile.TemporaryDirectory() as directory:
                pr = complete_pr(branch="human/topic", labels=[])
                pr["head"]["repo"] = {"full_name": REPO}
                self.assertTrue(
                    wrapper_boundary(Path(directory) / "repo", lane, policy({}), pr, history)
                )


if __name__ == "__main__":
    unittest.main()
