from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import code_mower.cloud as cloud_module
from code_mower import reviewer_spend
from code_mower.cloud_client import (
    PR_OUTCOME_EVENT_TYPE,
    PR_OUTCOME_SCHEMA,
    CloudBundleError,
    build_pr_outcome_event,
    load_pr_outcome_observations,
    pr_outcome_observation_key,
    pr_outcome_observation_record,
    pr_outcomes_upload,
    run_gh_pr_list,
    validate_cloud_event,
)


def _builder_run_event(
    event_id: str,
    pr_number: str,
    cost_usd: float | None,
    provider: str = "devin",
    created_at: str = "2026-09-03T10:00:00Z",
) -> dict[str, object]:
    return {
        "schema": "code_mower.benchmarkEvent.v1",
        "event_id": event_id,
        "event_type": "builder_run",
        "created_at": created_at,
        "repo_slug": "owner/repo",
        "team_id": "team",
        "install_id": "install",
        "source": "unit-test",
        "provider": provider,
        "lens": "implementation",
        "status": "pr-opened",
        "tool": {
            "role": "builder",
            "tool_name": provider,
            "provider": provider,
        },
        "metrics": ({"cost_usd": cost_usd} if cost_usd is not None else {}),
        "dimensions": {
            "builder_provider": provider,
            "pr_number": pr_number,
        },
    }


def _reviewer_run_event(
    event_id: str,
    pr_number: str,
    cost_usd: float | None,
    lane: str = "claude-audit",
    created_at: str = "2026-09-03T11:00:00Z",
) -> dict[str, object]:
    return {
        "schema": "code_mower.benchmarkEvent.v1",
        "event_id": event_id,
        "event_type": "reviewer_run",
        "created_at": created_at,
        "repo_slug": "owner/repo",
        "team_id": "team",
        "install_id": "install",
        "source": "unit-test",
        "provider": "claude",
        "lens": lane,
        "status": "pass",
        "tool": {
            "role": "reviewer",
            "tool_name": "claude",
            "provider": "claude",
        },
        "metrics": ({"cost_usd": cost_usd} if cost_usd is not None else {}),
        "dimensions": {
            "lane": lane,
            "pr_number": pr_number,
        },
    }


class PrOutcomeCoverageTests(unittest.TestCase):
    def test_complete_coverage_when_all_attempts_report_cost(self) -> None:
        event = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="42",
            outcome="merged",
            opened_at="2026-09-03T10:00:00Z",
            merged_at="2026-09-03T12:00:00Z",
            run_events=[
                _builder_run_event("b1", "42", 0.15),
                _reviewer_run_event("r1", "42", 0.10),
            ],
            created_at="2026-09-03T13:00:00Z",
        )

        self.assertEqual(event["event_type"], PR_OUTCOME_EVENT_TYPE)
        self.assertEqual(event["dimensions"]["pr_outcome_schema"], PR_OUTCOME_SCHEMA)
        self.assertEqual(event["dimensions"]["cost_coverage"], "complete")
        self.assertEqual(event["metrics"]["cost_reported_run_count"], 2)
        self.assertEqual(event["metrics"]["cost_expected_run_count"], 2)
        self.assertEqual(event["metrics"]["cost_covered_pr_count"], 1)
        self.assertAlmostEqual(event["metrics"]["reported_cost_usd"], 0.25)
        self.assertNotIn("missing_cost_sources", event["dimensions"])
        validate_cloud_event(event)

    def test_partial_coverage_when_some_attempts_miss_cost(self) -> None:
        event = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="43",
            outcome="open",
            opened_at="2026-09-03T10:00:00Z",
            run_events=[
                _builder_run_event("b1", "43", 0.15),
                _reviewer_run_event("r1", "43", None),
            ],
            created_at="2026-09-03T13:00:00Z",
        )

        self.assertEqual(event["dimensions"]["cost_coverage"], "partial")
        self.assertEqual(event["metrics"]["cost_reported_run_count"], 1)
        self.assertEqual(event["metrics"]["cost_expected_run_count"], 2)
        self.assertEqual(event["metrics"]["cost_covered_pr_count"], 0)
        self.assertAlmostEqual(event["metrics"]["reported_cost_usd"], 0.15)
        self.assertEqual(
            event["dimensions"]["missing_cost_sources"],
            ["claude-audit"],
        )
        validate_cloud_event(event)

    def test_unknown_coverage_when_no_attempts_report_cost(self) -> None:
        event = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="44",
            outcome="merged",
            opened_at="2026-09-03T10:00:00Z",
            merged_at="2026-09-03T12:00:00Z",
            run_events=[
                _builder_run_event("b1", "44", None),
                _reviewer_run_event("r1", "44", None),
            ],
            created_at="2026-09-03T13:00:00Z",
        )

        self.assertEqual(event["dimensions"]["cost_coverage"], "unknown")
        self.assertEqual(event["metrics"]["cost_reported_run_count"], 0)
        self.assertEqual(event["metrics"]["cost_expected_run_count"], 2)
        self.assertEqual(event["metrics"]["cost_covered_pr_count"], 0)
        self.assertNotIn("reported_cost_usd", event["metrics"])
        self.assertEqual(
            set(event["dimensions"]["missing_cost_sources"]),
            {"devin", "claude-audit"},
        )
        validate_cloud_event(event)

    def test_no_run_events_yields_unknown_with_zero_counts(self) -> None:
        event = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="45",
            outcome="open",
            opened_at="2026-09-03T10:00:00Z",
            run_events=[],
            created_at="2026-09-03T13:00:00Z",
        )

        self.assertEqual(event["dimensions"]["cost_coverage"], "unknown")
        self.assertEqual(event["metrics"]["cost_reported_run_count"], 0)
        self.assertEqual(event["metrics"]["cost_expected_run_count"], 0)
        self.assertEqual(event["metrics"]["cost_covered_pr_count"], 0)
        self.assertNotIn("reported_cost_usd", event["metrics"])
        validate_cloud_event(event)

    def test_duplicate_event_ids_are_counted_conservatively(self) -> None:
        event = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="46",
            outcome="merged",
            opened_at="2026-09-03T10:00:00Z",
            merged_at="2026-09-03T12:00:00Z",
            run_events=[
                _builder_run_event("dup", "46", 0.15),
                _builder_run_event("dup", "46", 0.15),
            ],
            created_at="2026-09-03T13:00:00Z",
        )

        self.assertEqual(event["dimensions"]["cost_coverage"], "partial")
        self.assertEqual(event["metrics"]["cost_reported_run_count"], 1)
        self.assertEqual(event["metrics"]["cost_expected_run_count"], 2)
        self.assertEqual(event["metrics"]["cost_covered_pr_count"], 0)
        self.assertAlmostEqual(event["metrics"]["reported_cost_usd"], 0.15)
        self.assertEqual(event["dimensions"]["missing_cost_sources"], ["devin"])
        validate_cloud_event(event)

    def test_missing_event_id_counts_as_expected_unknown_cost(self) -> None:
        event = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="49",
            outcome="merged",
            opened_at="2026-09-03T10:00:00Z",
            merged_at="2026-09-03T12:00:00Z",
            run_events=[
                _builder_run_event("b1", "49", 0.15),
                {
                    "event_id": "",
                    "event_type": "builder_run",
                    "repo_slug": "owner/repo",
                    "dimensions": {"builder_provider": "devin", "pr_number": "49"},
                },
            ],
            created_at="2026-09-03T13:00:00Z",
        )

        self.assertEqual(event["dimensions"]["cost_coverage"], "partial")
        self.assertEqual(event["metrics"]["cost_reported_run_count"], 1)
        self.assertEqual(event["metrics"]["cost_expected_run_count"], 2)
        self.assertEqual(event["metrics"]["cost_covered_pr_count"], 0)
        self.assertAlmostEqual(event["metrics"]["reported_cost_usd"], 0.15)
        self.assertEqual(event["dimensions"]["missing_cost_sources"], ["devin"])
        validate_cloud_event(event)

    def test_non_build_or_review_events_ignored(self) -> None:
        event = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="47",
            outcome="merged",
            opened_at="2026-09-03T10:00:00Z",
            merged_at="2026-09-03T12:00:00Z",
            run_events=[
                _builder_run_event("b1", "47", 0.15),
                {
                    "schema": "code_mower.benchmarkEvent.v1",
                    "event_id": "other",
                    "event_type": "dogfood_upload",
                    "repo_slug": "owner/repo",
                },
            ],
            created_at="2026-09-03T13:00:00Z",
        )

        self.assertEqual(event["dimensions"]["cost_coverage"], "complete")
        self.assertEqual(event["metrics"]["cost_expected_run_count"], 1)
        validate_cloud_event(event)

    def test_rejects_negative_cost(self) -> None:
        with self.assertRaises(CloudBundleError):
            build_pr_outcome_event(
                repo_slug="owner/repo",
                pr_number="48",
                outcome="merged",
                opened_at="2026-09-03T10:00:00Z",
                merged_at="2026-09-03T12:00:00Z",
                run_events=[
                    _builder_run_event("b1", "48", -0.01),
                ],
                created_at="2026-09-03T13:00:00Z",
            )

    def test_rejects_non_finite_cost(self) -> None:
        for cost in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(cost=cost):
                with self.assertRaises(CloudBundleError):
                    build_pr_outcome_event(
                        repo_slug="owner/repo",
                        pr_number="48",
                        outcome="merged",
                        opened_at="2026-09-03T10:00:00Z",
                        merged_at="2026-09-03T12:00:00Z",
                        run_events=[
                            _builder_run_event("b1", "48", cost),
                        ],
                        created_at="2026-09-03T13:00:00Z",
                    )

    def test_rejects_json_parsed_non_finite_cost(self) -> None:
        parsed = json.loads('{"nan": NaN, "inf": Infinity, "neg_inf": -Infinity}')
        for field, cost in parsed.items():
            with self.subTest(field=field):
                with self.assertRaises(CloudBundleError):
                    build_pr_outcome_event(
                        repo_slug="owner/repo",
                        pr_number="48",
                        outcome="merged",
                        opened_at="2026-09-03T10:00:00Z",
                        merged_at="2026-09-03T12:00:00Z",
                        run_events=[
                            _builder_run_event("b1", "48", cost),
                        ],
                        created_at="2026-09-03T13:00:00Z",
                    )


class GhPrListTests(unittest.TestCase):
    def test_run_gh_pr_list_parses_json_array(self) -> None:
        payload = [
            {
                "number": 101,
                "state": "MERGED",
                "createdAt": "2026-09-01T10:00:00Z",
                "closedAt": "2026-09-02T10:00:00Z",
                "mergedAt": "2026-09-02T09:00:00Z",
                "updatedAt": "2026-09-02T10:00:00Z",
                "url": "https://github.com/owner/repo/pull/101",
                "headRefName": "feature",
            }
        ]
        completed = mock.MagicMock()
        completed.returncode = 0
        completed.stdout = json.dumps(payload)
        completed.stderr = ""

        with mock.patch("subprocess.run", return_value=completed) as run:
            result = run_gh_pr_list(
                repo_slug="owner/repo",
                limit=10,
                repo_path=__import__("pathlib").Path("."),
            )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["number"], 101)
        run.assert_called_once()
        args = run.call_args[0][0]
        self.assertIn("pr", args)
        self.assertIn("--state", args)
        self.assertIn("all", args)

    def test_run_gh_pr_list_raises_on_failure(self) -> None:
        completed = mock.MagicMock()
        completed.returncode = 1
        completed.stdout = ""
        completed.stderr = "auth failed"

        with mock.patch("subprocess.run", return_value=completed):
            with self.assertRaises(CloudBundleError):
                run_gh_pr_list(
                    repo_slug="owner/repo",
                    limit=10,
                    repo_path=__import__("pathlib").Path("."),
                )


class PackagePathRegressionTests(unittest.TestCase):
    def test_cloud_module_imports_pr_outcomes_upload_in_package_path(self) -> None:
        self.assertIs(
            cloud_module._pr_outcomes_upload,
            pr_outcomes_upload,
        )

    def test_pr_outcomes_command_routes_to_upload_helper(self) -> None:
        with mock.patch.object(
            cloud_module, "_pr_outcomes_upload", return_value={
                "mode": "cloud-pr-outcomes",
                "status": "no_events",
                "repo_slug": "owner/repo",
                "event_count": 0,
                "pr_count": 0,
                "errors": [],
            }
        ) as upload:
            out = StringIO()
            with redirect_stdout(out):
                code = cloud_module.main(
                    ["pr-outcomes", "--repo-slug", "owner/repo", "--json"]
                )
        self.assertEqual(code, 0)
        upload.assert_called_once()

    def test_non_finite_local_cost_is_recorded_per_pr_without_aborting_others(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            builder_dir = repo_path / ".code-mower" / "builder-runs"
            builder_dir.mkdir(parents=True)
            (builder_dir / "pr-1.cloud-event.json").write_text(
                json.dumps({
                    "event_id": "bad-cost",
                    "event_type": "builder_run",
                    "repo_slug": "owner/repo",
                    "metrics": {"cost_usd": float("nan")},
                    "dimensions": {"builder_provider": "devin", "pr_number": "1"},
                }),
                encoding="utf-8",
            )
            (builder_dir / "pr-2.cloud-event.json").write_text(
                json.dumps({
                    "event_id": "good-cost",
                    "event_type": "builder_run",
                    "repo_slug": "owner/repo",
                    "metrics": {"cost_usd": 0.10},
                    "dimensions": {"builder_provider": "devin", "pr_number": "2"},
                }),
                encoding="utf-8",
            )

            pr_records = [
                {
                    "number": "1",
                    "state": "MERGED",
                    "createdAt": "2026-09-03T10:00:00Z",
                    "mergedAt": "2026-09-03T12:00:00Z",
                    "updatedAt": "2026-09-03T13:00:00Z",
                },
                {
                    "number": "2",
                    "state": "MERGED",
                    "createdAt": "2026-09-03T10:00:00Z",
                    "mergedAt": "2026-09-03T12:00:00Z",
                    "updatedAt": "2026-09-03T13:00:00Z",
                },
            ]
            with mock.patch(
                "code_mower.cloud_client.operations.run_gh_pr_list",
                return_value=pr_records,
            ):
                out = StringIO()
                with redirect_stdout(out):
                    code = cloud_module.main(
                        [
                            "pr-outcomes",
                            "--repo-path",
                            str(repo_path),
                            "--repo-slug",
                            "owner/repo",
                            "--output-dir",
                            str(repo_path / "bundle"),
                            "--endpoint",
                            "https://codemower.example.com/api/upload",
                            "--json",
                        ]
                    )

        self.assertEqual(code, 0)
        result = json.loads(out.getvalue())
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["pr_count"], 2)
        self.assertEqual(result["event_count"], 1)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("PR 1", result["errors"][0])


class PrOutcomeIdentityTests(unittest.TestCase):
    def test_unchanged_retry_is_idempotent(self) -> None:
        run_events = [
            _builder_run_event("b1", "60", 0.15),
            _reviewer_run_event("r1", "60", 0.10),
        ]
        event1 = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="60",
            outcome="merged",
            opened_at="2026-09-03T10:00:00Z",
            merged_at="2026-09-03T12:00:00Z",
            run_events=run_events,
            created_at="2026-09-03T13:00:00Z",
        )
        event2 = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="60",
            outcome="merged",
            opened_at="2026-09-03T10:00:00Z",
            merged_at="2026-09-03T12:00:00Z",
            run_events=run_events,
            created_at="2026-09-03T13:00:00Z",
        )

        self.assertEqual(event1["event_id"], event2["event_id"])
        self.assertEqual(event1["created_at"], event2["created_at"])
        self.assertEqual(
            event1["dimensions"]["pr_outcome_observation_version"],
            event2["dimensions"]["pr_outcome_observation_version"],
        )
        validate_cloud_event(event1)
        validate_cloud_event(event2)

    def test_late_arriving_builder_spend_changes_identity_and_ordering(self) -> None:
        reviewer = _reviewer_run_event("r1", "61", 0.10)
        original = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="61",
            outcome="merged",
            opened_at="2026-09-03T10:00:00Z",
            merged_at="2026-09-03T12:00:00Z",
            run_events=[reviewer],
            created_at="2026-09-03T13:00:00Z",
        )
        updated = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="61",
            outcome="merged",
            opened_at="2026-09-03T10:00:00Z",
            merged_at="2026-09-03T12:00:00Z",
            run_events=[
                reviewer,
                _builder_run_event(
                    "b1",
                    "61",
                    0.15,
                    created_at="2026-09-03T14:00:00Z",
                ),
            ],
            created_at="2026-09-03T13:00:00Z",
        )

        self.assertNotEqual(original["event_id"], updated["event_id"])
        self.assertNotEqual(
            original["dimensions"]["pr_outcome_observation_version"],
            updated["dimensions"]["pr_outcome_observation_version"],
        )
        self.assertGreater(updated["created_at"], original["created_at"])
        self.assertEqual(updated["dimensions"]["cost_coverage"], "complete")
        validate_cloud_event(updated)

    def test_late_arriving_reviewer_spend_changes_identity_and_ordering(self) -> None:
        builder = _builder_run_event("b1", "62", 0.15)
        original = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="62",
            outcome="merged",
            opened_at="2026-09-03T10:00:00Z",
            merged_at="2026-09-03T12:00:00Z",
            run_events=[builder],
            created_at="2026-09-03T13:00:00Z",
        )
        updated = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="62",
            outcome="merged",
            opened_at="2026-09-03T10:00:00Z",
            merged_at="2026-09-03T12:00:00Z",
            run_events=[
                builder,
                _reviewer_run_event(
                    "r1",
                    "62",
                    0.10,
                    created_at="2026-09-03T14:00:00Z",
                ),
            ],
            created_at="2026-09-03T13:00:00Z",
        )

        self.assertNotEqual(original["event_id"], updated["event_id"])
        self.assertNotEqual(
            original["dimensions"]["pr_outcome_observation_version"],
            updated["dimensions"]["pr_outcome_observation_version"],
        )
        self.assertGreater(updated["created_at"], original["created_at"])
        self.assertEqual(updated["dimensions"]["cost_coverage"], "complete")
        validate_cloud_event(updated)

    def test_corrected_cost_changes_identity_and_versioning(self) -> None:
        run_events = [_builder_run_event("b1", "63", 0.15)]
        original = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="63",
            outcome="merged",
            opened_at="2026-09-03T10:00:00Z",
            merged_at="2026-09-03T12:00:00Z",
            run_events=run_events,
            created_at="2026-09-03T13:00:00Z",
        )
        corrected = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="63",
            outcome="merged",
            opened_at="2026-09-03T10:00:00Z",
            merged_at="2026-09-03T12:00:00Z",
            run_events=[_builder_run_event("b1", "63", 0.25)],
            created_at="2026-09-03T13:00:00Z",
        )

        self.assertNotEqual(original["event_id"], corrected["event_id"])
        self.assertNotEqual(
            original["dimensions"]["pr_outcome_observation_version"],
            corrected["dimensions"]["pr_outcome_observation_version"],
        )
        self.assertAlmostEqual(corrected["metrics"]["reported_cost_usd"], 0.25)
        self.assertEqual(original["created_at"], corrected["created_at"])
        validate_cloud_event(corrected)

    def test_prior_state_unchanged_retry_is_idempotent(self) -> None:
        run_events = [
            _builder_run_event("b1", "66", 0.15),
            _reviewer_run_event("r1", "66", 0.10),
        ]
        original = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="66",
            outcome="merged",
            opened_at="2026-09-03T10:00:00Z",
            merged_at="2026-09-03T12:00:00Z",
            run_events=run_events,
            created_at="2026-09-03T13:00:00Z",
        )
        prior = pr_outcome_observation_record(original)
        retry = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="66",
            outcome="merged",
            opened_at="2026-09-03T10:00:00Z",
            merged_at="2026-09-03T12:00:00Z",
            run_events=run_events,
            created_at="2026-09-03T13:00:00Z",
            prior_observation=prior,
        )

        self.assertEqual(original["event_id"], retry["event_id"])
        self.assertEqual(original["created_at"], retry["created_at"])
        validate_cloud_event(retry)

    def test_prior_state_corrected_evidence_is_chronologically_newer(self) -> None:
        original = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="67",
            outcome="merged",
            opened_at="2026-09-03T10:00:00Z",
            merged_at="2026-09-03T12:00:00Z",
            run_events=[_builder_run_event("b1", "67", 0.15)],
            created_at="2026-09-03T13:00:00Z",
        )
        prior = pr_outcome_observation_record(original)
        corrected = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="67",
            outcome="merged",
            opened_at="2026-09-03T10:00:00Z",
            merged_at="2026-09-03T12:00:00Z",
            run_events=[_builder_run_event("b1", "67", 0.25)],
            created_at="2026-09-03T13:00:00Z",
            prior_observation=prior,
        )

        self.assertNotEqual(original["event_id"], corrected["event_id"])
        self.assertNotEqual(
            original["dimensions"]["pr_outcome_observation_version"],
            corrected["dimensions"]["pr_outcome_observation_version"],
        )
        self.assertGreater(corrected["created_at"], original["created_at"])
        self.assertAlmostEqual(corrected["metrics"]["reported_cost_usd"], 0.25)
        validate_cloud_event(corrected)

    def test_nonzero_offset_run_timestamp_converts_to_utc(self) -> None:
        event = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="68",
            outcome="merged",
            opened_at="2026-09-03T10:00:00Z",
            merged_at="2026-09-03T12:00:00Z",
            run_events=[
                _builder_run_event(
                    "b1",
                    "68",
                    0.15,
                    created_at="2026-09-03T10:00:00-07:00",
                )
            ],
            created_at="2026-09-03T13:00:00Z",
        )

        self.assertEqual(event["created_at"], "2026-09-03T17:00:00Z")
        validate_cloud_event(event)


class PrOutcomeUnidentifiedReviewerTests(unittest.TestCase):
    def test_missing_reviewer_run_id_does_not_report_cost(self) -> None:
        event = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="64",
            outcome="merged",
            opened_at="2026-09-03T10:00:00Z",
            merged_at="2026-09-03T12:00:00Z",
            run_events=[
                _builder_run_event("b1", "64", 0.15),
                _reviewer_run_event("", "64", 0.10),
            ],
            created_at="2026-09-03T13:00:00Z",
        )

        self.assertEqual(event["dimensions"]["cost_coverage"], "partial")
        self.assertEqual(event["metrics"]["cost_reported_run_count"], 1)
        self.assertEqual(event["metrics"]["cost_expected_run_count"], 2)
        self.assertAlmostEqual(event["metrics"]["reported_cost_usd"], 0.15)
        self.assertEqual(
            event["dimensions"]["missing_cost_sources"],
            ["claude-audit"],
        )
        validate_cloud_event(event)

    def test_duplicate_unidentified_rows_do_not_inflate_spend_or_completeness(self) -> None:
        run_events: list[dict[str, object]] = [_builder_run_event("b1", "65", 0.15)]
        run_events.extend(_reviewer_run_event("", "65", 0.10) for _ in range(2))
        event = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="65",
            outcome="merged",
            opened_at="2026-09-03T10:00:00Z",
            merged_at="2026-09-03T12:00:00Z",
            run_events=run_events,
            created_at="2026-09-03T13:00:00Z",
        )

        self.assertEqual(event["dimensions"]["cost_coverage"], "partial")
        self.assertEqual(event["metrics"]["cost_reported_run_count"], 1)
        self.assertEqual(event["metrics"]["cost_expected_run_count"], 3)
        self.assertAlmostEqual(event["metrics"]["reported_cost_usd"], 0.15)
        self.assertEqual(
            set(event["dimensions"]["missing_cost_sources"]),
            {"claude-audit"},
        )
        validate_cloud_event(event)

    def test_missing_spend_run_id_overrides_generated_event_id(self) -> None:
        spend_like = _reviewer_run_event("generated-uuid", "69", 0.10)
        spend_like["dimensions"]["spend_run_id"] = ""
        event = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="69",
            outcome="merged",
            opened_at="2026-09-03T10:00:00Z",
            merged_at="2026-09-03T12:00:00Z",
            run_events=[
                _builder_run_event("b1", "69", 0.15),
                spend_like,
            ],
            created_at="2026-09-03T13:00:00Z",
        )

        self.assertEqual(event["dimensions"]["cost_coverage"], "partial")
        self.assertEqual(event["metrics"]["cost_reported_run_count"], 1)
        self.assertEqual(event["metrics"]["cost_expected_run_count"], 2)
        self.assertAlmostEqual(event["metrics"]["reported_cost_usd"], 0.15)
        self.assertEqual(
            event["dimensions"]["missing_cost_sources"],
            ["claude-audit"],
        )
        validate_cloud_event(event)

    def test_duplicate_spend_run_ids_do_not_inflate_completeness(self) -> None:
        first = _reviewer_run_event("evt-1", "70", 0.10)
        first["dimensions"]["spend_run_id"] = "run-1"
        second = _reviewer_run_event("evt-2", "70", 0.20)
        second["dimensions"]["spend_run_id"] = "run-1"
        event = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="70",
            outcome="merged",
            opened_at="2026-09-03T10:00:00Z",
            merged_at="2026-09-03T12:00:00Z",
            run_events=[_builder_run_event("b1", "70", 0.15), first, second],
            created_at="2026-09-03T13:00:00Z",
        )

        self.assertEqual(event["dimensions"]["cost_coverage"], "partial")
        self.assertEqual(event["metrics"]["cost_reported_run_count"], 2)
        self.assertEqual(event["metrics"]["cost_expected_run_count"], 3)
        self.assertAlmostEqual(event["metrics"]["reported_cost_usd"], 0.25)
        self.assertEqual(
            event["dimensions"]["missing_cost_sources"],
            ["claude-audit"],
        )
        validate_cloud_event(event)

    def test_conflicting_duplicate_rows_resolve_independent_of_order(self) -> None:
        cheaper = _reviewer_run_event("evt-1", "72", 0.10)
        cheaper["dimensions"]["spend_run_id"] = "run-1"
        pricier = _reviewer_run_event("evt-2", "72", 0.20)
        pricier["dimensions"]["spend_run_id"] = "run-1"

        def _build(rows: list[dict[str, object]]) -> dict[str, object]:
            return build_pr_outcome_event(
                repo_slug="owner/repo",
                pr_number="72",
                outcome="merged",
                opened_at="2026-09-03T10:00:00Z",
                merged_at="2026-09-03T12:00:00Z",
                run_events=[_builder_run_event("b1", "72", 0.15), *rows],
                created_at="2026-09-03T13:00:00Z",
            )

        forward = _build([cheaper, pricier])
        reversed_event = _build([pricier, cheaper])

        self.assertEqual(forward["metrics"], reversed_event["metrics"])
        self.assertEqual(
            forward["dimensions"]["cost_coverage"],
            reversed_event["dimensions"]["cost_coverage"],
        )
        self.assertEqual(
            forward["dimensions"]["missing_cost_sources"],
            reversed_event["dimensions"]["missing_cost_sources"],
        )
        self.assertEqual(
            forward["dimensions"]["pr_outcome_observation_version"],
            reversed_event["dimensions"]["pr_outcome_observation_version"],
        )
        self.assertEqual(forward["event_id"], reversed_event["event_id"])
        self.assertEqual(forward["created_at"], reversed_event["created_at"])

        self.assertEqual(forward["dimensions"]["cost_coverage"], "partial")
        self.assertEqual(forward["metrics"]["cost_reported_run_count"], 2)
        self.assertEqual(forward["metrics"]["cost_expected_run_count"], 3)
        self.assertAlmostEqual(
            forward["metrics"]["reported_cost_usd"], 0.25
        )
        validate_cloud_event(forward)
        validate_cloud_event(reversed_event)

    def test_non_finite_cost_on_missing_id_is_bundle_error(self) -> None:
        bad = _reviewer_run_event("", "71", float("nan"))
        with self.assertRaises(CloudBundleError):
            build_pr_outcome_event(
                repo_slug="owner/repo",
                pr_number="71",
                outcome="merged",
                opened_at="2026-09-03T10:00:00Z",
                merged_at="2026-09-03T12:00:00Z",
                run_events=[_builder_run_event("b1", "71", 0.15), bad],
                created_at="2026-09-03T13:00:00Z",
            )


class PrOutcomeObservationStateTests(unittest.TestCase):
    def _run_upload(
        self,
        repo_path: Path,
        output_dir: Path,
        pr_records: list[dict[str, object]],
    ) -> dict[str, object]:
        with mock.patch(
            "code_mower.cloud_client.operations.run_gh_pr_list",
            return_value=pr_records,
        ):
            out = StringIO()
            with redirect_stdout(out):
                code = cloud_module.main(
                    [
                        "pr-outcomes",
                        "--repo-path",
                        str(repo_path),
                        "--repo-slug",
                        "owner/repo",
                        "--output-dir",
                        str(output_dir),
                        "--endpoint",
                        "https://codemower.example.com/api/upload",
                        "--json",
                    ]
                )
        self.assertEqual(code, 0)
        return json.loads(out.getvalue())

    def test_bundle_path_missing_run_id_and_corrected_ordering(self) -> None:
        pr_records = [
            {
                "number": "1",
                "state": "MERGED",
                "createdAt": "2026-09-03T10:00:00Z",
                "mergedAt": "2026-09-03T12:00:00Z",
                "updatedAt": "2026-09-03T13:00:00Z",
            }
        ]
        spend_run = {
            "lane": "claude-audit",
            "repo": "owner/repo",
            "pr_number": 1,
            "head_sha": "abc123",
            "model": "sonnet",
            "wall_seconds": 1.0,
            "verdict": "PASS",
            "created_at": "2026-09-03T11:00:00Z",
            "cost_usd": 0.05,
        }
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            spend_path = repo_path / ".code-mower" / "reviewer-spend.json"
            spend_path.parent.mkdir(parents=True)
            spend_path.write_text(
                json.dumps({"schema": reviewer_spend.SPEND_SCHEMA, "runs": [spend_run]}),
                encoding="utf-8",
            )

            first = self._run_upload(repo_path, repo_path / "bundle-1", pr_records)
            self.assertEqual(first["status"], "dry_run")
            self.assertEqual(first["event_count"], 1)
            self.assertEqual(first["errors"], [])

            manifest_path = Path(first["export"]["manifest"])
            first_event = json.loads(manifest_path.read_text(encoding="utf-8"))[
                "events"
            ][0]
            self.assertEqual(
                first_event["dimensions"]["cost_coverage"], "unknown"
            )
            self.assertEqual(
                first_event["dimensions"]["missing_cost_sources"],
                ["claude-audit"],
            )
            self.assertNotIn("reported_cost_usd", first_event["metrics"])

            state_path = repo_path / ".code-mower" / "pr-outcome-observations.json"
            observations = load_pr_outcome_observations(state_path)
            key = pr_outcome_observation_key("owner/repo", "1")
            self.assertIn(key, observations)
            self.assertEqual(
                observations[key]["created_at"], first_event["created_at"]
            )

            spend_run["cost_usd"] = 0.07
            spend_path.write_text(
                json.dumps({"schema": reviewer_spend.SPEND_SCHEMA, "runs": [spend_run]}),
                encoding="utf-8",
            )
            second = self._run_upload(repo_path, repo_path / "bundle-2", pr_records)
            self.assertEqual(second["status"], "dry_run")
            self.assertEqual(second["errors"], [])
            second_event = json.loads(
                Path(second["export"]["manifest"]).read_text(encoding="utf-8")
            )["events"][0]

            self.assertNotEqual(
                first_event["event_id"], second_event["event_id"]
            )
            self.assertGreater(
                second_event["created_at"], first_event["created_at"]
            )

            third = self._run_upload(repo_path, repo_path / "bundle-3", pr_records)
            third_event = json.loads(
                Path(third["export"]["manifest"]).read_text(encoding="utf-8")
            )["events"][0]
            self.assertEqual(
                second_event["event_id"], third_event["event_id"]
            )
            self.assertEqual(
                second_event["created_at"], third_event["created_at"]
            )


if __name__ == "__main__":
    unittest.main()
