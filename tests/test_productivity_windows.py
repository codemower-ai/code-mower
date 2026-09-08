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
from unittest.mock import patch

from code_mower.cloud_client import (
    EVENT_SCHEMA,
    PRODUCTIVITY_BASELINE_TRUST_GUIDANCE,
    PRODUCTIVITY_EVENT_TYPE,
    PRODUCTIVITY_METRICS_SCHEMA,
    PRODUCTIVITY_WINDOW_DIMENSION,
    PRODUCTIVITY_WINDOW_INPUT_SCHEMA,
    CloudBundleError,
    build_cloud_bundle,
    is_normalized_productivity_window_event,
    load_event_file,
    load_productivity_window_events,
    normalize_event,
    parse_event_args,
    productivity_window_to_event,
    repo_sync_window_events,
    validate_cloud_event,
    validate_productivity_window_event,
)
from code_mower.cloud_client.operations import (
    build_repo_sync_data_class_summary,
    dogfood_upload,
    repo_sync_upload,
)


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

    def test_posture_and_source_fork_the_event_id(self) -> None:
        observation = _fixture()["repo_window"]
        base = productivity_window_to_event(observation, source="unit-test")

        # Repeat content through another route stays stable.
        repeat = productivity_window_to_event(
            copy.deepcopy(observation), team_id="other", install_id="other", source="b"
        )
        self.assertEqual(repeat["event_id"], base["event_id"])

        postured = copy.deepcopy(observation)
        postured["pilot_posture"] = "supervised"
        postured_event = productivity_window_to_event(postured, source="unit-test")
        validate_cloud_event(postured_event)
        self.assertEqual(postured_event["dimensions"]["pilot_posture"], "supervised")
        self.assertNotEqual(postured_event["event_id"], base["event_id"])
        self.assertEqual(
            productivity_window_to_event(
                copy.deepcopy(postured), source="other-route"
            )["event_id"],
            postured_event["event_id"],
        )

        resourced = copy.deepcopy(observation)
        resourced["event_source"] = "dogfood"
        resourced_event = productivity_window_to_event(resourced, source="unit-test")
        validate_cloud_event(resourced_event)
        self.assertEqual(resourced_event["dimensions"]["event_source"], "dogfood")
        self.assertNotEqual(resourced_event["event_id"], base["event_id"])


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
                                "event_count": 4,
                                "event_types": {
                                    "dogfood_upload": 1,
                                    # Legacy and normalized windows share the
                                    # event type; only the marker counts.
                                    "productivity_summary": 3,
                                },
                                "productivity_window_event_count": 2,
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
        self.assertEqual(summary["current_dogfood"]["events"], 4)

    def test_repo_sync_summary_ignores_legacy_productivity_summaries(self) -> None:
        summary = build_repo_sync_data_class_summary(
            [
                {
                    "steps": [
                        {
                            "mode": "cloud-dogfood",
                            "export": {
                                "event_count": 2,
                                "event_types": {
                                    "dogfood_upload": 1,
                                    "productivity_summary": 1,
                                },
                                "productivity_window_event_count": 0,
                            },
                        }
                    ]
                }
            ]
        )

        baseline = summary["productivity_baseline"]
        self.assertEqual(baseline["steps"], 0)
        self.assertEqual(baseline["events"], 0)
        self.assertEqual(summary["current_dogfood"]["events"], 2)


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

    def test_rejects_silent_elapsed_seconds_input(self) -> None:
        observation = copy.deepcopy(_fixture()["repo_window"])
        observation["timings"] = dict(observation["timings"])
        observation["timings"]["elapsed_seconds"] = 604800
        with self.assertRaisesRegex(
            CloudBundleError, "unsupported productivity_window timing"
        ):
            productivity_window_to_event(observation)

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

    def test_defect_coverage_equivalence_accepts_observed_combinations(self) -> None:
        release = productivity_window_to_event(
            _fixture()["release_window"], source="unit-test"
        )
        validate_cloud_event(release)
        self.assertEqual(release["dimensions"]["defect_coverage"], "observed")

        # Each defect/revert signal alone sustains observed coverage.
        for counts in (
            {"post_merge_defect_count": 1},
            {"reverted_pr_count": 1},
        ):
            event = copy.deepcopy(release)
            event["metrics"] = {
                "cycle_time_seconds": event["metrics"]["cycle_time_seconds"],
                **counts,
            }
            validate_cloud_event(event)

        # Absence of both signals sustains unavailable coverage.
        repo = productivity_window_to_event(
            _fixture()["repo_window"], source="unit-test"
        )
        validate_cloud_event(repo)
        self.assertEqual(repo["dimensions"]["defect_coverage"], "unavailable")

    def test_defect_coverage_equivalence_rejects_mismatches(self) -> None:
        repo = productivity_window_to_event(
            _fixture()["repo_window"], source="unit-test"
        )
        claimed = copy.deepcopy(repo)
        claimed["dimensions"]["defect_coverage"] = "observed"
        with self.assertRaisesRegex(CloudBundleError, "defect_coverage"):
            validate_cloud_event(claimed)

        release = productivity_window_to_event(
            _fixture()["release_window"], source="unit-test"
        )
        cycle_only = copy.deepcopy(release)
        cycle_only["metrics"] = {
            "cycle_time_seconds": release["metrics"]["cycle_time_seconds"]
        }
        cycle_only["dimensions"]["defect_coverage"] = "unavailable"
        validate_cloud_event(cycle_only)

        unrelated_counts = copy.deepcopy(release)
        unrelated_counts["metrics"] = {
            "cycle_time_seconds": release["metrics"]["cycle_time_seconds"],
            "merged_pr_count": 6,
        }
        unrelated_counts["dimensions"]["defect_coverage"] = "unavailable"
        validate_cloud_event(unrelated_counts)

        hidden = copy.deepcopy(release)
        hidden["dimensions"]["defect_coverage"] = "unavailable"
        with self.assertRaisesRegex(CloudBundleError, "defect_coverage"):
            validate_cloud_event(hidden)

        hidden_revert = copy.deepcopy(release)
        hidden_revert["metrics"] = {
            "cycle_time_seconds": release["metrics"]["cycle_time_seconds"],
            "reverted_pr_count": 1,
        }
        hidden_revert["dimensions"]["defect_coverage"] = "unavailable"
        with self.assertRaisesRegex(CloudBundleError, "defect_coverage"):
            validate_cloud_event(hidden_revert)

    def test_normalized_window_span_must_match_cycle_time(self) -> None:
        event = productivity_window_to_event(
            _fixture()["repo_window"], source="unit-test"
        )
        validate_cloud_event(event)

        drifted = copy.deepcopy(event)
        drifted["metrics"]["cycle_time_seconds"] += 1
        with self.assertRaisesRegex(CloudBundleError, "must match"):
            validate_cloud_event(drifted)

        reversed_window = copy.deepcopy(event)
        reversed_window["dimensions"]["window_start"] = event["dimensions"]["window_end"]
        reversed_window["dimensions"]["window_end"] = event["dimensions"]["window_start"]
        with self.assertRaisesRegex(CloudBundleError, "must be after"):
            validate_cloud_event(reversed_window)

        invalid = copy.deepcopy(event)
        invalid["dimensions"]["window_start"] = "not-a-timestamp"
        with self.assertRaisesRegex(CloudBundleError, "ISO 8601"):
            validate_cloud_event(invalid)

        naive = copy.deepcopy(event)
        naive["dimensions"]["window_start"] = "2026-08-01T00:00:00"
        with self.assertRaisesRegex(CloudBundleError, "UTC offset"):
            validate_cloud_event(naive)

        for bad in (float("inf"), float("nan")):
            nonfinite = copy.deepcopy(event)
            nonfinite["metrics"]["cycle_time_seconds"] = bad
            with self.assertRaisesRegex(CloudBundleError, "finite"):
                validate_productivity_window_event(nonfinite)

    def test_shared_jsonl_parsing_stays_in_sync(self) -> None:
        from code_mower.cloud_client import parse_event_file_candidates
        from code_mower.cloud_client.productivity_windows import (
            _parsed_window_candidates,
        )

        source = Path("window.json")
        self.assertEqual(parse_event_file_candidates("", source), [])
        self.assertEqual(_parsed_window_candidates("", source), [])
        single = parse_event_file_candidates('{"a": 1}', source)
        self.assertEqual(single, [{"a": 1}])
        self.assertEqual(_parsed_window_candidates('{"a": 1}', source), single)
        array = parse_event_file_candidates('[{"a": 1}, {"b": 2}]', source)
        self.assertEqual(array, [{"a": 1}, {"b": 2}])
        self.assertEqual(
            _parsed_window_candidates('[{"a": 1}, {"b": 2}]', source), array
        )
        jsonl = parse_event_file_candidates('{"a": 1}\n{"b": 2}\n', source)
        self.assertEqual(jsonl, [{"a": 1}, {"b": 2}])
        self.assertEqual(
            _parsed_window_candidates('{"a": 1}\n{"b": 2}\n', source), jsonl
        )
        with self.assertRaisesRegex(CloudBundleError, "is not JSON"):
            parse_event_file_candidates('{"a": 1}\nnope\n', source)
        with self.assertRaisesRegex(CloudBundleError, "is not JSON"):
            _parsed_window_candidates('{"a": 1}\nnope\n', source)
        with self.assertRaisesRegex(CloudBundleError, "object, array, or JSONL"):
            parse_event_file_candidates("42", source)

        with tempfile.TemporaryDirectory() as tmp:
            observation = _fixture()["repo_window"]
            path = Path(tmp) / "windows.jsonl"
            path.write_text(
                json.dumps(observation) + "\n" + json.dumps(observation) + "\n",
                encoding="utf-8",
            )
            events = load_productivity_window_events(
                path, PRODUCTIVITY_EVENT_TYPE, source="unit-test"
            )
            self.assertEqual(len(events), 2)
            for loaded in events:
                validate_cloud_event(loaded)

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

class ProductivityWindowRepoSyncTests(unittest.TestCase):
    def test_repo_sync_accepts_same_slug_with_differing_case(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "window.json"
            path.write_text(json.dumps(_fixture()["repo_window"]), encoding="utf-8")
            events = repo_sync_window_events(
                [f"{PRODUCTIVITY_EVENT_TYPE}={path}"],
                repo_slug="Owner/Repo",
                team_id="t",
                install_id="i",
                source="s",
            )
            self.assertEqual(events[0]["repo_slug"], "owner/repo")

    def test_repo_sync_rejects_truly_different_slug(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "window.json"
            path.write_text(json.dumps(_fixture()["repo_window"]), encoding="utf-8")
            with self.assertRaisesRegex(
                CloudBundleError, "does not match repo-sync target"
            ):
                repo_sync_window_events(
                    [f"{PRODUCTIVITY_EVENT_TYPE}={path}"],
                    repo_slug="owner/different",
                    team_id="t",
                    install_id="i",
                    source="s",
                )

    def test_repo_sync_rejects_mismatched_repo_slug(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "window.json"
            path.write_text(json.dumps(_fixture()["repo_window"]), encoding="utf-8")
            with self.assertRaisesRegex(
                CloudBundleError, "does not match repo-sync target"
            ):
                repo_sync_window_events(
                    [f"{PRODUCTIVITY_EVENT_TYPE}={path}"],
                    repo_slug="owner/other",
                    team_id="t",
                    install_id="i",
                    source="s",
                )

    def test_repo_sync_accepts_matching_and_slugless_observations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            matching = Path(tmp) / "window.json"
            matching.write_text(
                json.dumps(_fixture()["repo_window"]), encoding="utf-8"
            )
            events = repo_sync_window_events(
                [f"{PRODUCTIVITY_EVENT_TYPE}={matching}"],
                repo_slug="owner/repo",
                team_id="t",
                install_id="i",
                source="s",
            )
            self.assertEqual(events[0]["repo_slug"], "owner/repo")

            slugless = copy.deepcopy(_fixture()["repo_window"])
            del slugless["repo_slug"]
            slugless_path = Path(tmp) / "slugless.json"
            slugless_path.write_text(json.dumps(slugless), encoding="utf-8")
            filled = repo_sync_window_events(
                [f"{PRODUCTIVITY_EVENT_TYPE}={slugless_path}"],
                repo_slug="owner/repo",
                team_id="t",
                install_id="i",
                source="s",
            )
            self.assertEqual(filled[0]["repo_slug"], "owner/repo")

    def test_repo_sync_resolves_path_slug_before_loading_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            slugless = copy.deepcopy(_fixture()["repo_window"])
            del slugless["repo_slug"]
            window_path = Path(tmp) / "window.json"
            window_path.write_text(json.dumps(slugless), encoding="utf-8")
            with patch(
                "code_mower.cloud_client.operations.detect_repo_slug",
                return_value="owner/repo",
            ):
                result = repo_sync_upload(
                    repo_specs=[str(repo)],
                    output_dir=Path(tmp) / "out",
                    modes=["dogfood"],
                    team_id="t",
                    install_id="i",
                    source_prefix="unit-test",
                    limit=5,
                    endpoint="https://codemower.com/api/ingest",
                    token_env="CODE_MOWER_TEST_EMPTY_TOKEN",
                    include_reports=False,
                    include_git_ref=False,
                    yes=False,
                    timeout=0.1,
                    events=[f"{PRODUCTIVITY_EVENT_TYPE}={window_path}"],
                )
            self.assertEqual(result["status"], "dry_run")
            dogfood_step = result["repos"][0]["steps"][0]
            self.assertEqual(dogfood_step["status"], "dry_run")
            self.assertEqual(
                dogfood_step["export"]["productivity_window_event_count"], 1
            )
            baseline = result["data_class_summary"]["productivity_baseline"]
            self.assertEqual((baseline["steps"], baseline["events"]), (1, 1))

    def test_repo_sync_enforces_global_event_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            observations = []
            for day in (1, 2):
                observation = copy.deepcopy(_fixture()["repo_window"])
                del observation["repo_slug"]
                observation["window_start"] = f"2026-08-0{day}T00:00:00Z"
                observation["window_end"] = f"2026-08-0{day + 1}T00:00:00Z"
                observations.append(observation)
            window_path = Path(tmp) / "windows.json"
            window_path.write_text(json.dumps(observations), encoding="utf-8")
            repo_a = Path(tmp) / "a"
            repo_a.mkdir()
            repo_b = Path(tmp) / "b"
            repo_b.mkdir()
            # Each repo loads 2 events (under a per-repo cap of 3) but the
            # run total of 4 exceeds the same cap, so the run must fail.
            with patch(
                "code_mower.cloud_client.operations.MAX_EVENT_COUNT", 3
            ):
                result = repo_sync_upload(
                    repo_specs=[f"owner/a={repo_a}", f"owner/b={repo_b}"],
                    output_dir=Path(tmp) / "out",
                    modes=["dogfood"],
                    team_id="t",
                    install_id="i",
                    source_prefix="unit-test",
                    limit=5,
                    endpoint="https://codemower.com/api/ingest",
                    token_env="CODE_MOWER_TEST_EMPTY_TOKEN",
                    include_reports=False,
                    include_git_ref=False,
                    yes=False,
                    timeout=0.1,
                    events=[f"{PRODUCTIVITY_EVENT_TYPE}={window_path}"],
                )
            self.assertEqual(result["repos"][0]["steps"][0]["status"], "dry_run")
            second = result["repos"][1]["steps"][0]
            self.assertEqual(second["status"], "error")
            self.assertIn("too many events", second["error"])
            self.assertEqual(result["status"], "partial")

    def test_baseline_counts_only_normalized_windows_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            window_path = Path(tmp) / "window.json"
            window_path.write_text(
                json.dumps(_fixture()["repo_window"]), encoding="utf-8"
            )
            legacy = {
                "event_type": PRODUCTIVITY_EVENT_TYPE,
                "repo_slug": "owner/repo",
                "source": "unit-test",
                "status": "observed",
                "metrics": {"merged_pr_count": 1},
                "dimensions": {
                    "productivity_schema": PRODUCTIVITY_METRICS_SCHEMA,
                    "repo_slug": "owner/repo",
                    "window_start": "2026-09-03T00:00:00Z",
                    "window_end": "2026-09-03T01:00:00Z",
                    "window_granularity": "cycle",
                    "aggregation_subject": "repo",
                },
            }
            legacy_path = Path(tmp) / "legacy.json"
            legacy_path.write_text(json.dumps(legacy), encoding="utf-8")

            window_events = repo_sync_window_events(
                [f"{PRODUCTIVITY_EVENT_TYPE}={window_path}"],
                repo_slug="owner/repo",
                team_id="t",
                install_id="i",
                source="s",
            )
            legacy_events = load_productivity_window_events(
                legacy_path,
                PRODUCTIVITY_EVENT_TYPE,
                repo_slug="owner/repo",
                team_id="t",
                install_id="i",
                source="s",
            )
            self.assertTrue(
                is_normalized_productivity_window_event(window_events[0])
            )
            self.assertFalse(
                is_normalized_productivity_window_event(legacy_events[0])
            )

            def _dogfood(extra_events: list[dict[str, object]], name: str) -> dict[str, object]:
                return dogfood_upload(  # type: ignore[return-value]
                    repo_path=root,
                    output_dir=Path(tmp) / name,
                    reports=[],
                    events=extra_events,  # type: ignore[arg-type]
                    spend_path=None,
                    repo_slug="owner/repo",
                    team_id="t",
                    install_id="i",
                    source="unit-test",
                    endpoint="https://codemower.com/api/ingest",
                    token_env="CODE_MOWER_TEST_EMPTY_TOKEN",
                    include_reports=False,
                    yes=False,
                    timeout=0.1,
                )

            window_step = _dogfood(window_events, "bundle-window")
            legacy_step = _dogfood(legacy_events, "bundle-legacy")
            self.assertEqual(
                window_step["export"]["productivity_window_event_count"], 1  # type: ignore[index]
            )
            self.assertEqual(
                legacy_step["export"]["productivity_window_event_count"], 0  # type: ignore[index]
            )

            summary = build_repo_sync_data_class_summary(
                [{"steps": [window_step, legacy_step]}]  # type: ignore[list-item]
            )
            baseline = summary["productivity_baseline"]
            self.assertEqual((baseline["steps"], baseline["events"]), (1, 1))
            # The legacy summary still lands in current dogfood history.
            self.assertGreaterEqual(summary["current_dogfood"]["events"], 2)


class ProductivityWindowArtifactFallbackTests(unittest.TestCase):
    def _builder_run_artifact(self) -> dict[str, object]:
        return {
            "schema": "code_mower.authoringRun.v1",
            "run_id": "run-1",
            "experiment_id": "experiment-1",
            "repo": "owner/repo",
            "task_id": "task-1",
            "task_class": "general",
            "builder": {"provider": "codex", "tool": "codex", "model": "m1"},
            "started_at": "2026-08-01T00:00:00Z",
            "ended_at": "2026-08-01T00:01:00Z",
            "elapsed_seconds": 60.0,
            "status": "completed",
            "branch": "branch-1",
            "pull_request": "",
            "executor": {
                "type": "subprocess",
                "dry_run": False,
                "exit_code": 0,
                "command": {"arg_count": 1, "argv_sha256": "abc"},
            },
            "privacy": {},
        }

    def _adoption_result_artifact(self) -> dict[str, object]:
        return {
            "schema": "code_mower.adoptionResult.v1",
            "timestamp_utc": "2026-09-04T01:00:00Z",
            "release_tag": "v1.0.4",
            "package_identity": "code-mower",
            "normalized_version": "1.0.4",
            "qualification_context": "cold_install",
            "starting_version": "",
            "ending_version": "1.0.4",
            "provider": "local_cli",
            "executor": "release_qualify",
            "host_class": "local",
            "runtime_class": "python_3.12",
            "execution_state": "executed",
            "elapsed_seconds": 12.5,
            "outcome": "pass",
            "steps": [
                {
                    "id": "doctor",
                    "status": "pass",
                    "elapsed_seconds": 1.0,
                    "warning_count": 0,
                    "owner_action_count": 0,
                },
                {
                    "id": "package_install",
                    "status": "pass",
                    "elapsed_seconds": 11.5,
                    "warning_count": 0,
                    "owner_action_count": 0,
                },
            ],
        }

    def test_repo_sync_event_loading_accepts_builder_run_artifact(self) -> None:
        from code_mower.cloud_client import parse_event_args

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "builder-run.json"
            path.write_text(json.dumps(self._builder_run_artifact()), encoding="utf-8")

            dogfood_events = parse_event_args([f"builder_run={path}"])
            self.assertEqual(dogfood_events[0]["event_type"], "builder_run")

            direct = load_productivity_window_events(
                path, "builder_run", repo_slug="owner/repo", source="unit-test"
            )
            self.assertEqual(direct[0]["event_type"], "builder_run")
            self.assertEqual(direct[0]["event_id"], dogfood_events[0]["event_id"])
            validate_cloud_event(direct[0])

            synced = repo_sync_window_events(
                [f"builder_run={path}"],
                repo_slug="owner/repo",
                team_id="t",
                install_id="i",
                source="s",
            )
            self.assertEqual(synced[0]["event_type"], "builder_run")
            self.assertEqual(synced[0]["event_id"], dogfood_events[0]["event_id"])
            validate_cloud_event(synced[0])

    def test_repo_sync_event_loading_accepts_adoption_result_artifact(self) -> None:
        from code_mower.cloud_client import parse_event_args

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "adoption-result.json"
            path.write_text(
                json.dumps(self._adoption_result_artifact()), encoding="utf-8"
            )

            dogfood_events = parse_event_args([f"adoption_run={path}"])
            self.assertEqual(dogfood_events[0]["event_type"], "adoption_run")

            direct = load_productivity_window_events(
                path, "adoption_run", repo_slug="owner/repo", source="unit-test"
            )
            self.assertEqual(direct[0]["event_type"], "adoption_run")
            self.assertEqual(direct[0]["event_id"], dogfood_events[0]["event_id"])
            validate_cloud_event(direct[0])

            synced = repo_sync_window_events(
                [f"adoption_run={path}"],
                repo_slug="owner/repo",
                team_id="t",
                install_id="i",
                source="s",
            )
            self.assertEqual(synced[0]["event_type"], "adoption_run")
            self.assertEqual(synced[0]["event_id"], dogfood_events[0]["event_id"])
            validate_cloud_event(synced[0])


class NormalizedBoundaryPrivacyTests(unittest.TestCase):
    """Already-normalized window events must face the text privacy boundary.

    These events bypass ``normalize_window_observation`` (both the window
    loader and the generic loader pass them straight to ``normalize_event``),
    so ``validate_productivity_window_event`` re-applies the single-line and
    local-path checks to every text dimension.
    """

    def _valid_event(self) -> dict[str, object]:
        return productivity_window_to_event(  # type: ignore[return-value]
            _fixture()["release_window"], source="unit-test"
        )

    def test_rejects_multiline_release_on_normalized_event(self) -> None:
        event = self._valid_event()
        event["dimensions"]["release"] = "v1.0.0\nsecond output line"  # type: ignore[index]
        with self.assertRaisesRegex(CloudBundleError, "single-line"):
            validate_cloud_event(event)

    def test_rejects_raw_output_like_text_on_normalized_event(self) -> None:
        event = self._valid_event()
        event["dimensions"]["aggregation_key"] = (  # type: ignore[index]
            "week-32\nTraceback: command failed with exit 1"
        )
        with self.assertRaisesRegex(CloudBundleError, "single-line"):
            validate_cloud_event(event)

    def test_rejects_local_path_in_release_dimension(self) -> None:
        for bad in ("/opt/company/private-repo", "C:/work/private-repo"):
            event = self._valid_event()
            event["dimensions"]["release"] = bad  # type: ignore[index]
            with self.assertRaisesRegex(CloudBundleError, "local paths"):
                validate_cloud_event(event)

    def test_rejects_leading_whitespace_local_path_on_normalized_event(self) -> None:
        for bad in (" /opt/company/private-repo", "  C:/work/private-repo"):
            event = self._valid_event()
            event["dimensions"]["release"] = bad  # type: ignore[index]
            with self.assertRaisesRegex(CloudBundleError, "local paths"):
                validate_cloud_event(event)

    def test_trailing_newline_still_rejected_on_normalized_event(self) -> None:
        event = self._valid_event()
        event["dimensions"]["release"] = "v1.0.0\n"  # type: ignore[index]
        with self.assertRaisesRegex(CloudBundleError, "single-line"):
            validate_cloud_event(event)

    def test_rejects_tainted_normalized_event_through_file_loaders(self) -> None:
        event = self._valid_event()
        event["dimensions"]["release"] = "/opt/company/private-repo"  # type: ignore[index]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tainted.json"
            path.write_text(json.dumps(event), encoding="utf-8")
            with self.assertRaisesRegex(CloudBundleError, "local paths"):
                load_productivity_window_events(path, PRODUCTIVITY_EVENT_TYPE)
            with self.assertRaisesRegex(CloudBundleError, "local paths"):
                load_event_file(path, PRODUCTIVITY_EVENT_TYPE)

    def test_ordinary_labels_still_validate(self) -> None:
        event = self._valid_event()
        validate_cloud_event(event)
        self.assertEqual(event["dimensions"]["release"], "v1.0.0")  # type: ignore[index]


class AbsolutePathDetectionTests(unittest.TestCase):
    def test_rejects_general_absolute_unix_paths(self) -> None:
        observation = copy.deepcopy(_fixture()["repo_window"])
        observation["aggregation_key"] = "/opt/company/private-repo"
        with self.assertRaisesRegex(CloudBundleError, "local paths"):
            productivity_window_to_event(observation)

    def test_rejects_forward_slash_windows_paths(self) -> None:
        observation = copy.deepcopy(_fixture()["release_window"])
        observation["release"] = "C:/work/private-repo"
        with self.assertRaisesRegex(CloudBundleError, "local paths"):
            productivity_window_to_event(observation)

    def test_accepts_ordinary_release_labels(self) -> None:
        observation = copy.deepcopy(_fixture()["release_window"])
        observation["release"] = "v1.0.0"
        event = productivity_window_to_event(observation, source="unit-test")
        validate_cloud_event(event)
        self.assertEqual(event["dimensions"]["release"], "v1.0.0")


class AnonymousBundleWindowCountTests(unittest.TestCase):
    def _window_event(self) -> dict[str, object]:
        return productivity_window_to_event(  # type: ignore[return-value]
            _fixture()["repo_window"], source="unit-test"
        )

    def test_anonymous_bundle_reports_zero_window_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = build_cloud_bundle(
                reports=[],
                events=[self._window_event()],  # type: ignore[list-item]
                output_dir=Path(tmp) / "bundle",
                repo_slug="owner/repo",
                team_id="t",
                install_id="i",
                anonymous=True,
            )
            self.assertEqual(result["event_count"], 0)
            self.assertEqual(result["productivity_window_event_count"], 0)
            manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["events"], [])
            self.assertEqual(manifest["privacy_mode"], "anonymous")

    def test_non_anonymous_bundle_still_counts_windows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = build_cloud_bundle(
                reports=[],
                events=[self._window_event()],  # type: ignore[list-item]
                output_dir=Path(tmp) / "bundle",
                repo_slug="owner/repo",
                team_id="t",
                install_id="i",
                anonymous=False,
            )
            self.assertEqual(result["event_count"], 1)
            self.assertEqual(result["productivity_window_event_count"], 1)

    def test_repo_sync_summary_cannot_claim_excluded_baseline_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            anonymous = build_cloud_bundle(
                reports=[],
                events=[self._window_event()],  # type: ignore[list-item]
                output_dir=Path(tmp) / "bundle",
                repo_slug="owner/repo",
                team_id="t",
                install_id="i",
                anonymous=True,
            )
            summary = build_repo_sync_data_class_summary(
                [
                    {
                        "steps": [
                            {
                                "mode": "cloud-dogfood",
                                "export": {
                                    "event_count": anonymous["event_count"],
                                    "productivity_window_event_count": anonymous[
                                        "productivity_window_event_count"
                                    ],
                                },
                            }
                        ]
                    }
                ]
            )
            baseline = summary["productivity_baseline"]
            self.assertEqual((baseline["steps"], baseline["events"]), (0, 0))


class GenericExportSlugFillTests(unittest.TestCase):
    """Generic export/dogfood fills slugless observations like repo-sync."""

    def _slugless_path(self, tmp: str) -> Path:
        observation = copy.deepcopy(_fixture()["repo_window"])
        del observation["repo_slug"]
        path = Path(tmp) / "slugless.json"
        path.write_text(json.dumps(observation), encoding="utf-8")
        return path

    def test_parse_event_args_fills_slugless_observation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._slugless_path(tmp)
            events = parse_event_args(
                [f"{PRODUCTIVITY_EVENT_TYPE}={path}"],
                repo_slug="owner/repo",
                team_id="t",
                install_id="i",
                source="unit-test",
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["repo_slug"], "owner/repo")
            validate_cloud_event(events[0])

    def test_load_event_file_fills_slugless_observation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._slugless_path(tmp)
            events = load_event_file(
                path, PRODUCTIVITY_EVENT_TYPE, repo_slug="owner/repo"
            )
            self.assertEqual(events[0]["repo_slug"], "owner/repo")
            validate_cloud_event(events[0])

    def test_slugless_observation_without_context_still_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._slugless_path(tmp)
            with self.assertRaisesRegex(CloudBundleError, "repo_slug"):
                parse_event_args([f"{PRODUCTIVITY_EVENT_TYPE}={path}"])


class DogfoodTildeRepoPathTests(unittest.TestCase):
    def test_dogfood_expands_tilde_repo_path_for_slug_detection(self) -> None:
        import os
        import subprocess
        from contextlib import redirect_stdout
        from io import StringIO

        from code_mower import cloud as cloud_cli

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            repo = home / "repo"
            repo.mkdir(parents=True)
            subprocess.run(
                ["git", "init"], cwd=repo, check=True, capture_output=True
            )
            subprocess.run(
                [
                    "git",
                    "remote",
                    "add",
                    "origin",
                    "https://github.com/owner/repo.git",
                ],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            observation = copy.deepcopy(_fixture()["repo_window"])
            del observation["repo_slug"]
            window_path = Path(tmp) / "window.json"
            window_path.write_text(json.dumps(observation), encoding="utf-8")
            old_home = os.environ.get("HOME")
            os.environ["HOME"] = str(home)
            try:
                stdout = StringIO()
                with redirect_stdout(stdout):
                    code = cloud_cli.main(
                        [
                            "dogfood",
                            "--repo-path",
                            "~/repo",
                            "--output-dir",
                            str(Path(tmp) / "out"),
                            "--event",
                            f"{PRODUCTIVITY_EVENT_TYPE}={window_path}",
                            "--json",
                        ]
                    )
            finally:
                if old_home is None:
                    del os.environ["HOME"]
                else:
                    os.environ["HOME"] = old_home
            self.assertEqual(code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(
                payload["export"]["productivity_window_event_count"], 1
            )


class ProductivityWindowFractionalDurationTests(unittest.TestCase):
    def test_fractional_duration_window_preserved(self) -> None:
        observation = {
            "schema": PRODUCTIVITY_WINDOW_INPUT_SCHEMA,
            "repo_slug": "owner/repo",
            "window_start": "2026-08-01T00:00:00.250Z",
            "window_end": "2026-08-01T00:00:01.750Z",
            "window_granularity": "custom",
            "aggregation_subject": "repo",
        }
        event = productivity_window_to_event(observation, source="unit-test")
        self.assertEqual(event["metrics"]["cycle_time_seconds"], 1.5)
        validate_cloud_event(event)

    def test_sub_second_fractional_duration_not_truncated_to_zero(self) -> None:
        observation = {
            "schema": PRODUCTIVITY_WINDOW_INPUT_SCHEMA,
            "repo_slug": "owner/repo",
            "window_start": "2026-08-01T00:00:00Z",
            "window_end": "2026-08-01T00:00:00.500Z",
            "window_granularity": "custom",
            "aggregation_subject": "repo",
        }
        event = productivity_window_to_event(observation, source="unit-test")
        self.assertEqual(event["metrics"]["cycle_time_seconds"], 0.5)
        validate_cloud_event(event)


if __name__ == "__main__":
    unittest.main()
