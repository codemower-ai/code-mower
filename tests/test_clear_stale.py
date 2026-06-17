import unittest

from code_mower.clear_stale import (
    latest_trusted_terminal_comment,
    resolve_stale_clear_decision,
)
from code_mower.lane_configs import load_lane_config


CURRENT_SHA = "a" * 40
OLD_SHA = "b" * 40


def _comment(author: str, body: str, created_at: str = "2026-06-17T00:00:00Z") -> dict:
    return {
        "user": {"login": author},
        "body": body,
        "created_at": created_at,
    }


def _devin_body(label: str, head_sha: str | None = CURRENT_SHA) -> str:
    lines = ["Devin Audit Result: PASS"]
    if head_sha:
        lines.append(f"Head SHA: `{head_sha}`")
    lines.append(f"<!-- DEVIN_AUDIT_STATE: {label} -->")
    return "\n".join(lines)


class ClearStaleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_lane_config("devin")

    def test_no_terminal_labels_returns_no_decision(self) -> None:
        result = resolve_stale_clear_decision(
            issue_number=123,
            current_head_sha=CURRENT_SHA,
            labels=["needs-devin-audit"],
            comments=[],
            config=self.config,
        )

        self.assertIsNone(result.decision)
        self.assertIn("no terminal", result.reason)

    def test_current_trusted_terminal_comment_preserves_labels(self) -> None:
        result = resolve_stale_clear_decision(
            issue_number=123,
            current_head_sha=CURRENT_SHA,
            labels=["devin-audit-done"],
            comments=[
                _comment(
                    "devin-ai-integration[bot]",
                    _devin_body("devin-audit-done", CURRENT_SHA),
                )
            ],
            config=self.config,
        )

        self.assertIsNone(result.decision)
        self.assertIn("terminal label is current", result.reason)

    def test_stale_terminal_comment_requeues_and_clears_terminal_labels(self) -> None:
        result = resolve_stale_clear_decision(
            issue_number=123,
            current_head_sha=CURRENT_SHA,
            labels=["devin-audit-done"],
            comments=[
                _comment(
                    "devin-ai-integration[bot]",
                    _devin_body("devin-audit-done", OLD_SHA),
                )
            ],
            config=self.config,
        )

        self.assertIsNotNone(result.decision)
        assert result.decision is not None
        self.assertEqual(result.decision.add_label, "needs-devin-audit")
        self.assertEqual(
            result.decision.remove_labels,
            ("devin-audit-done", "devin-audit-blocked"),
        )
        self.assertEqual(result.decision.reviewed_sha, OLD_SHA)
        self.assertTrue(result.requeue_added)

    def test_existing_needs_label_avoids_duplicate_requeue_dispatch_signal(self) -> None:
        result = resolve_stale_clear_decision(
            issue_number=123,
            current_head_sha=CURRENT_SHA,
            labels=["needs-devin-audit", "devin-audit-blocked"],
            comments=[
                _comment(
                    "devin-ai-integration[bot]",
                    _devin_body("devin-audit-blocked", OLD_SHA),
                )
            ],
            config=self.config,
        )

        self.assertIsNotNone(result.decision)
        self.assertFalse(result.requeue_added)

    def test_no_sha_fallback_comment_does_not_preserve_terminal_label(self) -> None:
        result = resolve_stale_clear_decision(
            issue_number=123,
            current_head_sha=CURRENT_SHA,
            labels=["devin-audit-done"],
            comments=[
                _comment(
                    "devin-ai-integration[bot]",
                    _devin_body("devin-audit-done", None),
                    created_at="2026-06-17T00:02:00Z",
                )
            ],
            config=self.config,
        )

        self.assertIsNotNone(result.decision)
        assert result.decision is not None
        self.assertEqual(result.decision.add_label, "needs-devin-audit")
        self.assertIsNone(result.decision.reviewed_sha)

    def test_untrusted_comment_does_not_preserve_terminal_label(self) -> None:
        result = resolve_stale_clear_decision(
            issue_number=123,
            current_head_sha=CURRENT_SHA,
            labels=["devin-audit-done"],
            comments=[_comment("random-user", _devin_body("devin-audit-done", CURRENT_SHA))],
            config=self.config,
        )

        self.assertIsNotNone(result.decision)

    def test_latest_trusted_terminal_comment_uses_newest_sha_bound_comment(self) -> None:
        latest = latest_trusted_terminal_comment(
            [
                _comment(
                    "devin-ai-integration[bot]",
                    _devin_body("devin-audit-done", OLD_SHA),
                    created_at="2026-06-17T00:01:00Z",
                ),
                _comment(
                    "devin-ai-integration[bot]",
                    _devin_body("devin-audit-done", CURRENT_SHA),
                    created_at="2026-06-17T00:02:00Z",
                ),
            ],
            config=self.config,
        )

        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest.reviewed_sha, CURRENT_SHA)


if __name__ == "__main__":
    unittest.main()
