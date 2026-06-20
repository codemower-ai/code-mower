from __future__ import annotations

import unittest

from code_mower.calibration.effect_report import (
    build_effect_report,
    reviewer_dimensions,
)


class CalibrationEffectReportTests(unittest.TestCase):
    def test_reviewer_dimensions_infer_provider_and_lens(self) -> None:
        self.assertEqual(
            reviewer_dimensions("gemini-context-driven-quality"),
            {"provider": "gemini", "lens": "context-driven-quality"},
        )
        self.assertEqual(
            reviewer_dimensions("antigravity-operability"),
            {"provider": "antigravity", "lens": "operability"},
        )
        self.assertEqual(
            reviewer_dimensions("claude-audit"),
            {"provider": "claude", "lens": "base-audit"},
        )

    def test_effect_report_compares_lens_lift_against_provider_spread(self) -> None:
        blocked_one = {
            "repo": "owner/repo",
            "pr_number": 1,
            "head_sha": "1" * 40,
            "truth": {"expectation": "known_blocked"},
            "reviewer_runs": [
                {"reviewer": "claude-base-audit", "status": "pass", "finding_count": 0},
                {
                    "reviewer": "claude-generic-programming",
                    "status": "blocked",
                    "finding_count": 1,
                    "expected_blocker_caught": True,
                },
                {"reviewer": "gemini-base-audit", "status": "pass", "finding_count": 0},
                {
                    "reviewer": "gemini-generic-programming",
                    "status": "blocked",
                    "finding_count": 1,
                    "expected_blocker_caught": True,
                },
            ],
        }
        blocked_two = {
            "repo": "owner/repo",
            "pr_number": 2,
            "head_sha": "2" * 40,
            "truth": {"expectation": "known_blocked"},
            "reviewer_runs": [
                {"reviewer": "claude-base-audit", "status": "pass", "finding_count": 0},
                {
                    "reviewer": "claude-generic-programming",
                    "status": "blocked",
                    "finding_count": 1,
                    "expected_blocker_caught": True,
                },
                {"reviewer": "gemini-base-audit", "status": "pass", "finding_count": 0},
                {
                    "reviewer": "gemini-generic-programming",
                    "status": "blocked",
                    "finding_count": 1,
                    "expected_blocker_caught": True,
                },
            ],
        }
        clean = {
            "repo": "owner/repo",
            "pr_number": 3,
            "head_sha": "3" * 40,
            "truth": {"expectation": "known_clean"},
            "reviewer_runs": [
                {"reviewer": "claude-base-audit", "status": "pass", "finding_count": 0},
                {"reviewer": "claude-generic-programming", "status": "pass", "finding_count": 0},
                {"reviewer": "gemini-base-audit", "status": "pass", "finding_count": 0},
                {"reviewer": "gemini-generic-programming", "status": "pass", "finding_count": 0},
            ],
        }

        report = build_effect_report(
            {
                "version": 1,
                "name": "effect-test",
                "corpus": [blocked_one, blocked_two, clean],
            }
        )

        self.assertEqual(report["summary"]["comparison"], "lens_effect_larger")
        self.assertEqual(report["summary"]["mean_absolute_lens_catch_delta"], 1.0)
        self.assertEqual(report["summary"]["mean_provider_catch_spread"], 0.0)
        self.assertEqual(len(report["lens_lifts"]), 2)
        self.assertEqual(len(report["provider_spreads"]), 2)

    def test_effective_provider_spread_counts_input_gaps(self) -> None:
        corpus = {
            "version": 1,
            "name": "effective-spread-test",
            "corpus": [
                {
                    "repo": "owner/repo",
                    "pr_number": 1,
                    "head_sha": "1" * 40,
                    "truth": {"expectation": "known_blocked"},
                    "reviewer_runs": [
                        {
                            "reviewer": "claude-context-driven-quality",
                            "status": "blocked",
                            "finding_count": 1,
                            "expected_blocker_caught": True,
                        },
                        {
                            "reviewer": "hermes-context-driven-quality",
                            "status": "audit_input_insufficient",
                            "finding_count": 0,
                        },
                    ],
                }
            ],
        }

        report = build_effect_report(corpus)
        spread = report["provider_spreads"][0]

        self.assertEqual(spread["lens"], "context-driven-quality")
        self.assertEqual(spread["effective_catch_rate_spread"], 1.0)
        self.assertIsNone(spread["catch_rate_spread"])


if __name__ == "__main__":
    unittest.main()
