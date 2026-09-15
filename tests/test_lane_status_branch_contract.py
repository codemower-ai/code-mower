"""Status routes reviewers, so it resolves under the gate's own contract.

Without the configured branch identity this projection called a `codex/`
branch labelled `builder:claude` a sole Claude writer -- and would route a
reviewer on that -- while the gate refused the very same pull request. These
go through the actual status consumer, not a lower helper.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from code_mower import lane_status  # noqa: E402

CONFIG = {
    "enabled": True,
    "labels": {"builder:claude": "claude", "builder:codex": "codex"},
    "authors": {"claude[bot]": "claude", "codex[bot]": "codex",
                "devin-ai-integration[bot]": "devin"},
    "branch_prefixes": {"claude/": "claude", "codex/": "codex",
                        "feature/cx-": "codex"},
    "require_verified_lineage": True,
}
UNCONFIGURED = {
    "enabled": True,
    "labels": {"builder:claude": "claude", "builder:codex": "codex"},
    "authors": {},
}
HEAD = "b" * 40
BEFORE = "a" * 40
AUTHORITY = "owner"


def _handoff_marker(branch: str) -> str:
    from code_mower import builder_lineage

    episode = builder_lineage.ContributionEpisode(
        sequence=1,
        kind=builder_lineage.HANDOFF_KIND,
        repo="owner/repo",
        pr_number=7,
        branch=branch,
        source_lane="devin",
        destination_lane="codex",
        expected_head=BEFORE,
        resulting_head=HEAD,
        writer_state="terminated",
    )
    return "Lineage\n\n" + builder_lineage.lineage_comment_marker((episode,))


class StatusCarriesTheConfiguredBranchContract(unittest.TestCase):
    def _lineage(self, *, branch, labels, config, comments=None,
                 author="a-human"):
        env = {
            "CODE_MOWER_AUTHOR_EXCLUSION_JSON": json.dumps(config),
            "CODE_MOWER_DECISION_AUTHORITIES": AUTHORITY,
            "CODE_MOWER_DECISION_AUTHORITIES_OVERRIDE": "",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            return lane_status.builder_lineage_for(
                "owner/repo",
                pr_number=7,
                branch=branch,
                head_sha=HEAD,
                labels=list(labels),
                author=author,
                raw_comments=[] if comments is None else comments,
            )

    def test_a_matched_ordinary_branch_resolves(self):
        result = self._lineage(
            branch="claude/topic", labels=["builder:claude"], config=CONFIG
        )
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["current_writer"], "claude")

    def test_a_custom_configured_prefix_resolves(self):
        result = self._lineage(
            branch="feature/cx-topic", labels=["builder:codex"], config=CONFIG
        )
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["current_writer"], "codex")

    def test_a_configured_branch_and_label_conflict_refuses(self):
        result = self._lineage(
            branch="codex/topic", labels=["builder:claude"], config=CONFIG
        )
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result["reason"], "conflicting_builder_identity")
        self.assertEqual(
            result["current_writer"], "", "an unresolved lineage names no writer"
        )

    def test_no_configured_branch_contract_keeps_the_old_answer(self):
        result = self._lineage(
            branch="codex/topic", labels=["builder:claude"], config=UNCONFIGURED
        )
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["current_writer"], "claude")

    def test_a_recorded_handoff_resolves_to_its_current_writer(self):
        result = self._lineage(
            branch="devin/topic",
            labels=["builder:codex"],
            config=CONFIG,
            comments=[{"user": {"login": AUTHORITY},
                       "body": _handoff_marker("devin/topic")}],
            author="devin-ai-integration[bot]",
        )
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["current_writer"], "codex")
        self.assertEqual(sorted(result["contributors"]), ["codex", "devin"])

    def test_a_malformed_published_history_is_one_bounded_conflict(self):
        result = self._lineage(
            branch="claude/topic",
            labels=["builder:claude"],
            config=CONFIG,
            comments=[{"user": {"login": AUTHORITY}, "body": 12345}],
        )
        self.assertEqual(result["status"], "conflict")
        self.assertTrue(result["owner_action"])
        self.assertEqual(result["current_writer"], "")

    def test_a_genuinely_empty_history_stays_ordinary(self):
        result = self._lineage(
            branch="claude/topic", labels=["builder:claude"], config=CONFIG,
            comments=[],
        )
        self.assertEqual(result["status"], "resolved")

    def test_the_status_projection_carries_the_same_answer(self):
        """Through `_summarize_pr`, the consumer status actually renders."""

        env = {
            "CODE_MOWER_AUTHOR_EXCLUSION_JSON": json.dumps(CONFIG),
            "CODE_MOWER_DECISION_AUTHORITIES": AUTHORITY,
            "CODE_MOWER_DECISION_AUTHORITIES_OVERRIDE": "",
        }
        pr = {
            "number": 7,
            "headRefName": "codex/topic",
            "headRefOid": HEAD,
            "author": {"login": "a-human"},
            "labels": [{"name": "builder:claude"}],
            "comments": [],
            "isDraft": True,
            "updatedAt": "2026-01-01T00:00:00Z",
        }
        from datetime import UTC, datetime

        with mock.patch.dict(os.environ, env, clear=False):
            summary = lane_status._summarize_pr(
                "owner/repo", pr, datetime.now(UTC), 60
            )
        self.assertEqual(summary["builder_lineage"]["status"], "conflict")
        self.assertEqual(summary["builder_lineage"]["current_writer"], "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
