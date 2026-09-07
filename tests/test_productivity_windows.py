"""Normalized productivity windows for issue #738.

The converter turns metadata-only ``code_mower.productivityWindow.v1``
observations into deterministic ``productivity_summary`` events: separated
elapsed/active/queue/review/green/owner timings, observed-only counts and
defect linkage, explicit coverage, no causal claims, idempotent syncs.
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from code_mower.cloud_client import (
    EVENT_SCHEMA,
    PRODUCTIVITY_BASELINE_TRUST_GUIDANCE,
    PRODUCTIVITY_EVENT_TYPE,
    PRODUCTIVITY_METRICS_SCHEMA,
    PRODUCTIVITY_WINDOW_DIMENSION,
    PRODUCTIVITY_WINDOW_INPUT_SCHEMA,
    CloudBundleError,
    load_event_file,
    load_productivity_window_events,
    normalize_event,
    productivity_window_to_event,
    repo_sync_window_events,
    validate_cloud_event,
    validate_productivity_window_event,
)
from code_mower.cloud_client.operations import build_repo_sync_data_class_summary


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "productivity_window_observations.json"


def _fixture() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


class ProductivityWindowConversionTests(unittest.TestCase):
    def test_converts_to_valid_separated_event(self) -> None:
        observation = _fixture()["repo_window"]
        event = productivity_window_to_event(
            observation, team_id="team", install_id="install", source="unit-test"
        )
        validate_cloud_event(event)

        self.assertEqual(event["schema"], EVENT_SCHEMA)
        self.assertEqual(event["event_type"], PRODUCTIVITY_EVENT_TYPE)
        self.assertEqual(
            event["dimensions"]["productivity_schema"], PRODUCTIVITY_METRICS_SCHEMA
        )
        self.assertEqual(
            event["dimensions"]["productivity_window_schema"],
            PRODUCTIVITY_WINDOW_DIMENSION,
        )
        self.assertEqual(event["dimensions"]["aggregation_subject"], "repo")
        self.assertEqual(event["tool"]["tool_name"], "code-mower")
        self.assertEqual(event["tool"]["role"], "reporter")

        metrics = event["metrics"]
        self.assertEqual(metrics["cycle_time_seconds"], 7 * 24 * 3600)
        self.assertEqual(metrics["active_time_seconds"], 7200)
        self.assertEqual(metrics["queue_wait_seconds"], 1800)
        self.assertEqual(metrics["time_to_first_review_seconds"], 900)
        self.assertEqual(metrics["time_to_green_seconds"], 10800)
        self.assertEqual(metrics["time_to_merge_seconds"], 14400)
        self.assertEqual(metrics["owner_wait_seconds"], 600)
        self.assertNotIn("wait_time_seconds", metrics)
        self.assertEqual(metrics["merged_pr_count"], 4)
        self.assertEqual(metrics["fix_round_count"], 2)
        self.assertNotIn("post_merge_defect_count", metrics)
        self.assertNotIn("reverted_pr_count", metrics)

    def test_carries_coverage_and_no_causal_claim(self) -> None:
        event = productivity_window_to_event(_fixture()["repo_window"], source="unit-test")

        self.assertEqual(event["dimensions"]["active_time_coverage"], "observed")
        self.assertEqual(event["dimensions"]["defect_coverage"], "unavailable")
        self.assertEqual(event["dimensions"]["comparison_basis"], "code_mower_window")
        self.assertEqual(
            event["dimensions"]["timing_provenance"],
            "github_lifecycle_and_local_timing",
        )
        self.assertEqual(event["dimensions"]["causal_claim"], "none")
        serialized = json.dumps(event).lower().replace('"causal_claim"', "")
        self.assertNotIn("causal", serialized)
        self.assertNotIn("caused", serialized)

    def test_release_window_supports_pre_code_mower_comparison(self) -> None:
        observation = _fixture()["release_window"]
        event = productivity_window_to_event(observation, source="unit-test")
        validate_cloud_event(event)

        self.assertEqual(event["dimensions"]["aggregation_subject"], "release")
        self.assertEqual(event["dimensions"]["release"], "v1.0.0")
        self.assertEqual(event["dimensions"]["comparison_basis"], "pre_code_mower")
        self.assertEqual(event["dimensions"]["causal_claim"], "none")
        self.assertEqual(event["dimensions"]["active_time_coverage"], "unavailable")
        self.assertEqual(event["dimensions"]["defect_coverage"], "observed")
        self.assertNotIn("active_time_seconds", event["metrics"])
        self.assertEqual(event["metrics"]["post_merge_defect_count"], 2)
        self.assertEqual(event["metrics"]["reverted_pr_count"], 1)

    def test_missing_values_stay_unavailable_never_zero(self) -> None:
        observation = {
            "schema": PRODUCTIVITY_WINDOW_INPUT_SCHEMA,
            "repo_slug": "owner/repo",
            "window_start": "2026-08-01T00:00:00Z",
            "window_end": "2026-08-02T00:00:00Z",
            "window_granularity": "day",
            "aggregation_subject": "repo",
        }
        event = productivity_window_to_event(observation, source="unit-test")
        validate_cloud_event(event)

        self.assertEqual(event["metrics"], {"cycle_time_seconds": 86400})
        self.assertEqual(event["dimensions"]["active_time_coverage"], "unavailable")
        self.assertEqual(event["dimensions"]["defect_coverage"], "unavailable")


class ProductivityWindowIdempotenceTests(unittest.TestCase):
    def test_repeated_syncs_emit_stable_windows(self) -> None:
        observation = _fixture()["repo_window"]

        first = productivity_window_to_event(
            observation, team_id="t", install_id="i", source="a"
        )
        second = productivity_window_to_event(
            observation, team_id="t", install_id="i", source="a"
        )
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(
            json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True)
        )

        # Envelope context (source/team/install) does not fork the identity,
        # so a re-sync through another route still dedupes to one window.
        other_route = productivity_window_to_event(
            observation, team_id="other", install_id="other", source="b"
        )
        self.assertEqual(other_route["event_id"], first["event_id"])
        self.assertEqual(first["created_at"], "2026-08-08T00:00:00Z")

        changed = copy.deepcopy(observation)
        changed["counts"]["merged_pr_count"] = 5
        self.assertNotEqual(
            productivity_window_to_event(changed)["event_id"], first["event_id"]
        )

    def test_repo_sync_window_events_are_stable_across_syncs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "window.json"
            path.write_text(json.dumps(_fixture()["repo_window"]), encoding="utf-8")
            specs = [f"{PRODUCTIVITY_EVENT_TYPE}={path}"]

            first = repo_sync_window_events(
                specs, repo_slug="owner/repo", team_id="t", install_id="i", source="s"
            )
            second = repo_sync_window_events(
                specs, repo_slug="owner/repo", team_id="t", install_id="i", source="s"
            )
            self.assertEqual(
                [event["event_id"] for event in first],
                [event["event_id"] for event in second],
            )

    def test_repo_sync_rejects_malformed_event_specs(self) -> None:
        with self.assertRaisesRegex(CloudBundleError, "--event entries must use EVENT_TYPE=PATH"):
            repo_sync_window_events(
                ["not-an-event-spec"],
                repo_slug="r",
                team_id="t",
                install_id="i",
                source="s",
            )


class ProductivityWindowEventFileTests(unittest.TestCase):
    def test_event_file_loading_accepts_window_observations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            observation = _fixture()["repo_window"]
            path = Path(tmp) / "window.json"
            path.write_text(json.dumps(observation), encoding="utf-8")

            events = load_event_file(path, PRODUCTIVITY_EVENT_TYPE)
            self.assertEqual(len(events), 1)
            validate_cloud_event(events[0])
            self.assertEqual(
                events[0]["dimensions"]["productivity_window_schema"],
                PRODUCTIVITY_WINDOW_DIMENSION,
            )

            direct = load_productivity_window_events(
                path,
                PRODUCTIVITY_EVENT_TYPE,
                repo_slug="owner/other",
                source="unit-test",
            )
            # The observation carries its own slug, which wins over sync context.
            self.assertEqual(direct[0]["repo_slug"], "owner/repo")

            slugless = copy.deepcopy(observation)
            del slugless["repo_slug"]
            slugless_path = Path(tmp) / "slugless.json"
            slugless_path.write_text(json.dumps(slugless), encoding="utf-8")
            filled = load_productivity_window_events(
                slugless_path,
                PRODUCTIVITY_EVENT_TYPE,
                repo_slug="owner/repo",
                source="unit-test",
            )
            self.assertEqual(filled[0]["repo_slug"], "owner/repo")

    def test_repo_sync_summary_reports_productivity_baseline(self) -> None:
        summary = build_repo_sync_data_class_summary(
            [
                {
                    "steps": [
                        {
                            "mode": "cloud-dogfood",
                            "export": {
                                "event_count": 3,
                                "event_types": {
                                    "dogfood_upload": 1,
                                    "productivity_summary": 2,
                                },
                            },
                        },
                        {"mode": "cloud-reviewer-runs", "event_count": 1},
                    ]
                }
            ]
        )

        baseline = summary["productivity_baseline"]
        self.assertEqual(baseline["steps"], 1)
        self.assertEqual(baseline["events"], 2)
        self.assertEqual(baseline["trust_guidance"], PRODUCTIVITY_BASELINE_TRUST_GUIDANCE)
        self.assertIn("causal", baseline["trust_guidance"]["do_not_use_for"])
        # Existing classes are unchanged.
        self.assertEqual(summary["current_dogfood"]["steps"], 1)
        self.assertEqual(summary["current_dogfood"]["events"], 3)


class ProductivityWindowRejectionTests(unittest.TestCase):
    def test_rejects_unobserved_subjects_and_bad_windows(self) -> None:
        observation = copy.deepcopy(_fixture()["repo_window"])

        pr_scoped = copy.deepcopy(observation)
        pr_scoped["aggregation_subject"] = "pr"
        with self.assertRaisesRegex(CloudBundleError, "repository or release windows only"):
            productivity_window_to_event(pr_scoped)

        missing_release = copy.deepcopy(_fixture()["release_window"])
        del missing_release["release"]
        with self.assertRaisesRegex(CloudBundleError, "require the 'release' field"):
            productivity_window_to_event(missing_release)

        inverted = copy.deepcopy(observation)
        inverted["window_start"], inverted["window_end"] = (
            inverted["window_end"],
            inverted["window_start"],
        )
        with self.assertRaisesRegex(CloudBundleError, "must be after 'window_start'"):
            productivity_window_to_event(inverted)

        bad_slug = copy.deepcopy(observation)
        bad_slug["repo_slug"] = "not-a-slug"
        with self.assertRaisesRegex(CloudBundleError, "OWNER/REPO"):
            productivity_window_to_event(bad_slug)

    def test_rejects_undeclared_fields_and_bad_measurements(self) -> None:
        observation = copy.deepcopy(_fixture()["repo_window"])
        observation["issue_body"] = "untrusted prose must not cross the boundary"
        with self.assertRaisesRegex(CloudBundleError, "unsupported productivity_window field"):
            productivity_window_to_event(observation)

        bad_timing = copy.deepcopy(_fixture()["repo_window"])
        bad_timing["timings"]["active_seconds"] = -5
        with self.assertRaisesRegex(CloudBundleError, "finite and non-negative"):
            productivity_window_to_event(bad_timing)

        fractional = copy.deepcopy(_fixture()["repo_window"])
        fractional["counts"]["merged_pr_count"] = 1.5
        with self.assertRaisesRegex(CloudBundleError, "non-negative integer"):
            productivity_window_to_event(fractional)

        with_path = copy.deepcopy(_fixture()["repo_window"])
        with_path["aggregation_key"] = "/home/owner/repo"
        with self.assertRaisesRegex(CloudBundleError, "local paths"):
            productivity_window_to_event(with_path)

    def test_windowed_event_validation_enforces_coverage_and_causality(self) -> None:
        event = productivity_window_to_event(_fixture()["repo_window"], source="unit-test")

        tampered = copy.deepcopy(event)
        tampered["dimensions"]["causal_claim"] = "code_mower_improved_it"
        with self.assertRaisesRegex(CloudBundleError, "causal_claim"):
            validate_cloud_event(tampered)

        drifted = copy.deepcopy(event)
        drifted["dimensions"]["sneaky_prose"] = "extra channel"
        with self.assertRaisesRegex(CloudBundleError, "unsupported productivity_window dimension"):
            validate_cloud_event(drifted)

        mismatch = copy.deepcopy(event)
        del mismatch["metrics"]["active_time_seconds"]
        with self.assertRaisesRegex(CloudBundleError, "active_time_coverage"):
            validate_cloud_event(mismatch)

    def test_unstamped_events_keep_historical_validation(self) -> None:
        legacy = normalize_event(
            {
                "event_type": PRODUCTIVITY_EVENT_TYPE,
                "repo_slug": "owner/repo",
                "source": "unit-test",
                "status": "observed",
                "metrics": {"merged_pr_count": 1},
                "dimensions": {
                    "productivity_schema": "code_mower.productivityMetrics.v1",
                    "repo_slug": "owner/repo",
                    "window_start": "2026-09-03T00:00:00Z",
                    "window_end": "2026-09-03T01:00:00Z",
                    "window_granularity": "cycle",
                    "aggregation_subject": "repo",
                },
            },
            PRODUCTIVITY_EVENT_TYPE,
        )
        validate_productivity_window_event(legacy)
        validate_cloud_event(legacy)

    def test_fixtures_stay_metadata_only(self) -> None:
        serialized = json.dumps(_fixture()).lower()
        for phrase in (
            "raw_diff",
            "transcript",
            "issue body",
            "source code",
            "auth output",
            "local path",
            "secret",
            "prompt",
        ):
            self.assertNotIn(phrase, serialized)


if __name__ == "__main__":
    unittest.main()
