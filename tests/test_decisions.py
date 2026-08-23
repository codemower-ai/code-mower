from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from code_mower import decisions
from code_mower.lane_configs import load_lane_config
from code_mower.trailer_comment_labeler import resolve_label_decision


FIXTURES = Path(__file__).resolve().parent / "fixtures"
HEAD_SHA = "abcdef0123456789abcdef0123456789abcdef01"


def _event(author: str, body: str, *, comment_id: int = 9001) -> dict:
    return {
        "action": "created",
        "issue": {"number": 42, "pull_request": {}},
        "comment": {"id": comment_id, "user": {"login": author}, "body": body},
    }


class DecisionMarkerTests(unittest.TestCase):
    def test_decide_renders_parseable_marker(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            result = decisions.main(
                [
                    "--id",
                    "ADR-007",
                    "--scope",
                    "finding",
                    "--resolves",
                    "HOST_DISPLAY_NAME",
                    "--by",
                    "owner",
                    "--ref",
                    "ADR-007",
                ]
            )

        self.assertEqual(result, 0)
        parsed = decisions.parse_decision_markers(out.getvalue())
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].id, "ADR-007")
        self.assertEqual(parsed[0].scope, "finding")
        self.assertEqual(parsed[0].resolves, "HOST_DISPLAY_NAME")
        self.assertEqual(parsed[0].by, "owner")
        self.assertEqual(parsed[0].ref, "ADR-007")

    def test_decision_registry_uses_only_trusted_comment_authors(self) -> None:
        marker = (
            '<!-- CODE_MOWER_DECISION: id=ADR-007 scope=finding '
            'resolves="HOST_DISPLAY_NAME" by=owner ref=ADR-007 -->'
        )
        records = decisions.collect_decision_records_from_comments(
            [
                {"author_association": "CONTRIBUTOR", "body": marker},
                {
                    "author_association": "MEMBER",
                    "body": marker,
                    "html_url": "https://example.test/c",
                },
            ]
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].source, "https://example.test/c")

    def test_decision_coverage_matches_identifier_and_phrase_forms(self) -> None:
        record = decisions.DecisionRecord(
            id="ADR-007",
            scope="finding",
            resolves="HOST_DISPLAY_NAME",
            by="owner",
            ref="ADR-007",
        )
        body = """
Findings:

- [P2] Host display name remains accepted by policy -- `src/display.py:12`
  The bridge exposes the configured host display name.
"""

        self.assertTrue(decisions.audit_blockers_are_decision_covered(body, (record,)))
        self.assertEqual(
            decisions.decision_covered_blocker_ids(body, (record,)),
            ("ADR-007",),
        )

    def test_bridge_pro_311_fixture_decision_covered_p2_yields_codex_done(self) -> None:
        os.environ.pop("CODEX_BOT_AUTHORS", None)
        config = load_lane_config("codex")
        transcript = json.loads(
            (FIXTURES / "bridge_pro_311_decision_transcript.json").read_text(
                encoding="utf-8"
            )
        )
        audit_comment = transcript[-1]

        decision, reason = resolve_label_decision(
            _event(
                "codex-audit-bot",
                audit_comment["body"],
                comment_id=int(audit_comment["id"]),
            ),
            current_head_sha=HEAD_SHA,
            config=config,
            issue_comments=transcript,
        )

        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(reason, "label done")
        self.assertEqual(decision.add_label, "codex-audit-done")
        self.assertEqual(
            decision.remove_labels,
            ("needs-codex-audit", "codex-audit-blocked"),
        )


if __name__ == "__main__":
    unittest.main()
