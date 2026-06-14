from __future__ import annotations

import unittest

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from code_mower import reviewer_metrics


class ReviewerMetricsCoreTests(unittest.TestCase):
    def test_metrics_combine_dispositions_spend_latency_and_events(self) -> None:
        report = {
            "mode": "reviewer-evidence-calibration",
            "profiles": {
                "codex-audit": {
                    "model": "codex",
                    "runs": 2,
                    "duration_seconds_total": 30.0,
                    "known_blocked_runs": 1,
                    "known_blocked_caught_runs": 1,
                    "dispositions": {
                        "true_positive": 1,
                        "false_positive": 1,
                        "useful": 1,
                        "unknown": 2,
                    },
                }
            },
        }
        event_summary = {
            "mode": "telemetry-summary",
            "lanes": {
                "codex-audit": {
                    "events": 4,
                    "finished": 3,
                    "pass": 2,
                    "blocked": 1,
                    "failed": 0,
                    "findings": 2,
                    "observed_pr_count": 2,
                }
            },
        }

        metrics = reviewer_metrics.build_reviewer_metrics(
            [report],
            spend={"profiles": {"codex-audit": {"cost_usd": 1.25}}},
            event_summaries=[event_summary],
        )

        profile = metrics["profiles"]["codex-audit"]
        self.assertEqual(profile["runs"], 2)
        self.assertEqual(profile["useful_findings"], 2)
        self.assertEqual(profile["negative_findings"], 1)
        self.assertEqual(profile["precision"], 0.5)
        self.assertEqual(profile["useful_rate"], 0.6667)
        self.assertEqual(profile["cost_per_useful_finding"], 0.625)
        self.assertEqual(profile["seconds_per_run"], 15.0)
        self.assertEqual(profile["event_log"]["blocked"], 1)
        self.assertEqual(profile["event_log"]["observed_pr_count"], 2)

    def test_unsupported_report_mode_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "reviewer metrics expects local-llm-calibration or reviewer-evidence-calibration",
        ):
            reviewer_metrics.build_reviewer_metrics(
                [{"mode": "old-value-report", "profiles": {}}],
            )


if __name__ == "__main__":
    unittest.main()
