from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from code_mower import code_mower_calibration


class CalibrationTruthTests(unittest.TestCase):
    def _load_corpus(self, payload: dict) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "corpus.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            return code_mower_calibration.load_corpus(path)

    def test_starter_value_report_matches_packaged_example(self) -> None:
        corpus = code_mower_calibration.load_corpus(
            ROOT / "src/code_mower/templates/calibration-corpus.json"
        )
        report = code_mower_calibration.build_value_report(corpus)
        self.assertEqual(
            code_mower_calibration.render_value_report_text(report),
            (ROOT / "src/code_mower/templates/reviewer-value-report.example.md").read_text(
                encoding="utf-8"
            ),
        )

    def test_explicit_truth_marks_known_clean_without_source_prefix(self) -> None:
        corpus = self._load_corpus(
            {
                "version": 1,
                "name": "truth-clean",
                "corpus": [
                    {
                        "repo": "owner/repo",
                        "pr_number": 1,
                        "head_sha": "a" * 40,
                        "source": "merged-control",
                        "truth": {"expectation": "known_clean"},
                        "reviewer_runs": [
                            {
                                "reviewer": "codex-audit",
                                "status": "pass",
                                "finding_count": 0,
                            }
                        ],
                    }
                ],
            }
        )
        report = code_mower_calibration.build_value_report(corpus)
        stats = report["metrics"]["profiles"]["codex-audit"]
        self.assertEqual(stats["known_clean_pass_runs"], 1)
        self.assertEqual(stats["blocking_false_positive_runs"], 0)

    def test_run_disposition_credits_known_blocked_catch(self) -> None:
        corpus = self._load_corpus(
            {
                "version": 1,
                "name": "truth-blocked",
                "corpus": [
                    {
                        "repo": "owner/repo",
                        "pr_number": 2,
                        "head_sha": "b" * 40,
                        "source": "historical-control",
                        "truth": {
                            "expectation": "known_blocked",
                            "expected_findings": [
                                {
                                    "path": "src/expected.py",
                                    "summary": "Expected target text",
                                }
                            ],
                        },
                        "reviewer_runs": [
                            {
                                "reviewer": "gemini-base-audit",
                                "status": "blocked",
                                "finding_count": 1,
                                "expected_finding_matches": 0,
                                "disposition": "true_positive",
                            }
                        ],
                    }
                ],
            }
        )
        report = code_mower_calibration.build_value_report(corpus)
        stats = report["metrics"]["profiles"]["gemini-base-audit"]
        self.assertEqual(stats["known_blocked_runs"], 1)
        self.assertEqual(stats["known_blocked_caught_runs"], 1)
        self.assertEqual(stats["known_blocked_missed_runs"], 0)
        self.assertEqual(stats["useful_findings"], 1)
        self.assertEqual(report["evidence"]["run_disposition_count"], 1)

    def test_disposition_rules_apply_to_folded_run_results(self) -> None:
        corpus = self._load_corpus(
            {
                "version": 1,
                "name": "truth-run-results",
                "corpus": [
                    {
                        "repo": "owner/repo",
                        "pr_number": 20,
                        "head_sha": "f" * 40,
                        "truth": {"expectation": "known_blocked"},
                        "reviewer_run_dispositions": [
                            {
                                "reviewer": "gemini-base-audit",
                                "status": "blocked",
                                "min_finding_count": 1,
                                "disposition": "true_positive",
                                "expected_blocker_caught": True,
                            }
                        ],
                    }
                ],
            }
        )
        run_results = [
            {
                "mode": code_mower_calibration.CALIBRATION_RUN_RESULTS_MODE,
                "run_results_id": "manifest-1",
                "reviewer_runs": [
                    {
                        "reviewer": "gemini-base-audit",
                        "repo": "owner/repo",
                        "pr_number": 20,
                        "head_sha": "f" * 40,
                        "status": "blocked",
                        "finding_count": 1,
                        "expected_finding_matches": 0,
                    }
                ],
            }
        ]
        report = code_mower_calibration.build_value_report(
            corpus,
            run_results=run_results,
        )
        stats = report["metrics"]["profiles"]["gemini-base-audit"]
        self.assertEqual(stats["known_blocked_caught_runs"], 1)
        self.assertEqual(stats["known_blocked_missed_runs"], 0)
        self.assertEqual(stats["useful_findings"], 1)
        self.assertEqual(report["evidence"]["run_disposition_count"], 1)

    def test_expected_finding_match_counts_as_true_positive_evidence(self) -> None:
        corpus = self._load_corpus(
            {
                "version": 1,
                "name": "truth-expected-match",
                "corpus": [
                    {
                        "repo": "owner/repo",
                        "pr_number": 21,
                        "head_sha": "1" * 40,
                        "truth": {"expectation": "known_blocked"},
                        "reviewer_runs": [
                            {
                                "reviewer": "gemini-context-driven-quality",
                                "status": "blocked",
                                "finding_count": 1,
                                "expected_finding_matches": 1,
                            }
                        ],
                    }
                ],
            }
        )
        report = code_mower_calibration.build_value_report(corpus)
        stats = report["metrics"]["profiles"]["gemini-context-driven-quality"]
        self.assertEqual(stats["known_blocked_caught_runs"], 1)
        self.assertEqual(stats["useful_findings"], 1)
        self.assertEqual(stats["dispositions"], {"true_positive": 1})
        self.assertEqual(report["evidence"]["run_disposition_count"], 1)

    def test_audit_input_insufficient_is_not_a_known_blocked_miss(self) -> None:
        corpus = self._load_corpus(
            {
                "version": 1,
                "name": "truth-context",
                "corpus": [
                    {
                        "repo": "owner/repo",
                        "pr_number": 3,
                        "head_sha": "c" * 40,
                        "source": "historical-control",
                        "truth": {"expectation": "known_blocked"},
                        "reviewer_runs": [
                            {
                                "reviewer": "gemini-base-audit",
                                "status": "audit_input_insufficient",
                                "finding_count": 1,
                            }
                        ],
                    }
                ],
            }
        )
        report = code_mower_calibration.build_value_report(corpus)
        stats = report["metrics"]["profiles"]["gemini-base-audit"]
        self.assertEqual(stats["known_blocked_runs"], 1)
        self.assertEqual(stats["known_blocked_caught_runs"], 0)
        self.assertEqual(stats["known_blocked_missed_runs"], 0)
        self.assertEqual(stats["audit_input_insufficient_runs"], 1)

    def test_legacy_source_prefixes_still_define_truth(self) -> None:
        corpus = self._load_corpus(
            {
                "version": 1,
                "name": "legacy-source",
                "corpus": [
                    {
                        "repo": "owner/repo",
                        "pr_number": 4,
                        "head_sha": "d" * 40,
                        "source": "known-clean-merged-control",
                        "reviewer_runs": [
                            {
                                "reviewer": "claude-audit",
                                "status": "pass",
                                "finding_count": 0,
                            }
                        ],
                    },
                    {
                        "repo": "owner/repo",
                        "pr_number": 5,
                        "head_sha": "e" * 40,
                        "source": "seeded-bug-runtime",
                        "reviewer_runs": [
                            {
                                "reviewer": "claude-audit",
                                "status": "blocked",
                                "finding_count": 1,
                                "expected_finding_matches": 1,
                            }
                        ],
                    },
                ],
            }
        )
        report = code_mower_calibration.build_value_report(corpus)
        stats = report["metrics"]["profiles"]["claude-audit"]
        self.assertEqual(stats["known_clean_pass_runs"], 1)
        self.assertEqual(stats["known_blocked_caught_runs"], 1)


if __name__ == "__main__":
    unittest.main()
