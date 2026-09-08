from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import code_mower.cloud as cloud_module
import code_mower.cloud_client.operations as cloud_operations
from code_mower.cloud_client.operations import _builder_run_events
from code_mower import reviewer_spend
from code_mower.file_locks import FileLockError, exclusive_file_lock
from code_mower.cloud_client import (
    PR_OUTCOME_EVENT_TYPE,
    PR_OUTCOME_SCHEMA,
    CloudBundleError,
    build_pr_outcome_event,
    load_pr_outcome_observations,
    max_source_freshness,
    pr_outcome_observation_key,
    pr_outcome_observation_record,
    pr_outcomes_upload,
    run_gh_pr_list,
    save_pr_outcome_observations,
    validate_cloud_event,
    validate_pr_outcome_payload,
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

    def test_stale_unchanged_retry_cannot_regress_source_watermark(self) -> None:
        run_events = [
            _builder_run_event(
                "b1", "70", 0.15, created_at="2027-01-02T11:00:00Z"
            )
        ]
        original = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="70",
            outcome="open",
            opened_at="2027-01-02T10:00:00Z",
            run_events=run_events,
            created_at="2027-01-10T00:00:00Z",
        )
        prior = pr_outcome_observation_record(original)
        self.assertEqual(prior["source_freshness"], "2027-01-10T00:00:00Z")

        # An unchanged retry built from an older (January 5) source snapshot
        # is rejected before the idempotent early return, so its lower
        # source_freshness can never replace the recorded watermark.
        with self.assertRaises(CloudBundleError):
            build_pr_outcome_event(
                repo_slug="owner/repo",
                pr_number="70",
                outcome="open",
                opened_at="2027-01-02T10:00:00Z",
                run_events=run_events,
                created_at="2027-01-05T00:00:00Z",
                prior_observation=prior,
            )

        # The January 10 watermark is preserved, so a stale January 6 state
        # transition cannot supersede the newer observation.
        with self.assertRaises(CloudBundleError):
            build_pr_outcome_event(
                repo_slug="owner/repo",
                pr_number="70",
                outcome="closed_unmerged",
                opened_at="2027-01-02T10:00:00Z",
                closed_at="2027-01-06T12:00:00Z",
                run_events=run_events,
                created_at="2027-01-06T00:00:00Z",
                prior_observation=prior,
            )

        # A non-stale unchanged retry stays byte-for-byte idempotent.
        retry = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="70",
            outcome="open",
            opened_at="2027-01-02T10:00:00Z",
            run_events=run_events,
            created_at="2027-01-10T00:00:00Z",
            prior_observation=prior,
        )
        self.assertEqual(retry, original)
        validate_cloud_event(retry)

    def test_source_freshness_watermark_never_regresses(self) -> None:
        self.assertEqual(
            max_source_freshness("2027-01-10T00:00:00Z", "2027-01-05T00:00:00Z"),
            "2027-01-10T00:00:00Z",
        )
        self.assertEqual(
            max_source_freshness("2027-01-05T00:00:00Z", "2027-01-10T00:00:00Z"),
            "2027-01-10T00:00:00Z",
        )
        self.assertEqual(
            max_source_freshness(
                "2027-01-10T03:00:00+03:00", "2027-01-10T00:30:00Z"
            ),
            "2027-01-10T00:30:00Z",
        )

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


class PrOutcomeFailClosedTests(unittest.TestCase):
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
        return code, out.getvalue()

    def _merged_pr(self, number: str) -> dict[str, object]:
        return {
            "number": number,
            "state": "MERGED",
            "createdAt": "2026-09-03T10:00:00Z",
            "mergedAt": "2026-09-03T12:00:00Z",
            "updatedAt": "2026-09-03T13:00:00Z",
        }

    def _emitted_events(self, result: dict[str, object]) -> dict[str, dict]:
        manifest = json.loads(
            Path(result["export"]["manifest"]).read_text(encoding="utf-8")
        )
        return {
            event["dimensions"]["pr_number"]: event
            for event in manifest["events"]
        }

    def test_unreadable_builder_evidence_blocks_complete_and_isolates_prs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            builder_dir = repo_path / ".code-mower" / "builder-runs"
            builder_dir.mkdir(parents=True)
            (builder_dir / "devin-local-pr-1-aa11.cloud-event.json").write_text(
                '{"event_id": "b1", "event_type": "builder_',
                encoding="utf-8",
            )
            (builder_dir / "devin-local-pr-2-bb22.cloud-event.json").write_text(
                json.dumps(_builder_run_event("b2", "2", 0.10)),
                encoding="utf-8",
            )

            code, raw = self._run_upload(
                repo_path,
                repo_path / "bundle",
                [self._merged_pr("1"), self._merged_pr("2")],
            )
            self.assertEqual(code, 0, raw)
            result = json.loads(raw)
            self.assertEqual(result["status"], "dry_run")
            self.assertEqual(result["event_count"], 2)

            events = self._emitted_events(result)
            pr1 = events["1"]
            self.assertEqual(
                pr1["dimensions"]["cost_coverage"], "unknown"
            )
            self.assertEqual(pr1["metrics"]["cost_expected_run_count"], 1)
            self.assertEqual(pr1["metrics"]["cost_reported_run_count"], 0)
            self.assertEqual(
                pr1["dimensions"]["missing_cost_sources"],
                ["unreadable-evidence"],
            )
            self.assertNotIn("reported_cost_usd", pr1["metrics"])
            validate_cloud_event(pr1)

            # Per-PR isolation: the healthy PR still emits complete coverage.
            pr2 = events["2"]
            self.assertEqual(pr2["dimensions"]["cost_coverage"], "complete")
            self.assertEqual(pr2["metrics"]["cost_covered_pr_count"], 1)
            validate_cloud_event(pr2)

            errors = result["errors"]
            self.assertEqual(len(errors), 1)
            self.assertIn("PR 1", errors[0])
            self.assertNotIn(str(repo_path), " ".join(errors))
            self.assertNotIn("builder_", " ".join(errors))

    def test_unattributed_unreadable_evidence_suppresses_complete_coverage(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            builder_dir = repo_path / ".code-mower" / "builder-runs"
            builder_dir.mkdir(parents=True)
            (builder_dir / "corrupt.cloud-event.json").write_text(
                "not json at all {", encoding="utf-8"
            )
            (builder_dir / "devin-local-pr-2-bb22.cloud-event.json").write_text(
                json.dumps(_builder_run_event("b2", "2", 0.10)),
                encoding="utf-8",
            )

            code, raw = self._run_upload(
                repo_path, repo_path / "bundle", [self._merged_pr("2")]
            )
            self.assertEqual(code, 0, raw)
            result = json.loads(raw)
            self.assertEqual(result["status"], "dry_run")

            events = self._emitted_events(result)
            pr2 = events["2"]
            self.assertEqual(pr2["dimensions"]["cost_coverage"], "partial")
            self.assertEqual(pr2["metrics"]["cost_reported_run_count"], 1)
            self.assertEqual(pr2["metrics"]["cost_expected_run_count"], 2)
            self.assertIn(
                "unreadable-evidence",
                pr2["dimensions"]["missing_cost_sources"],
            )
            validate_cloud_event(pr2)

            errors = result["errors"]
            self.assertEqual(len(errors), 1)
            self.assertIn("not attributable", errors[0])
            self.assertNotIn(str(repo_path), " ".join(errors))

    def test_malformed_non_builder_payload_counts_as_evidence_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            builder_dir = repo_path / ".code-mower" / "builder-runs"
            builder_dir.mkdir(parents=True)
            (builder_dir / "devin-local-pr-3-cc33.cloud-event.json").write_text(
                json.dumps({"event_id": "x", "event_type": "other"}),
                encoding="utf-8",
            )

            code, raw = self._run_upload(
                repo_path, repo_path / "bundle", [self._merged_pr("3")]
            )
            self.assertEqual(code, 0, raw)
            result = json.loads(raw)
            events = self._emitted_events(result)
            self.assertEqual(
                events["3"]["dimensions"]["cost_coverage"], "unknown"
            )
            self.assertEqual(
                events["3"]["metrics"]["cost_expected_run_count"], 1
            )
            self.assertTrue(any("PR 3" in e for e in result["errors"]))

    def test_unwritable_observation_state_aborts_before_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            # ``.code-mower`` exists as a regular file, so the observation
            # state location can never be created or written.
            (repo_path / ".code-mower").write_text("blocked", encoding="utf-8")

            code, raw = self._run_upload(
                repo_path, repo_path / "bundle", [self._merged_pr("1")]
            )
            self.assertEqual(code, 1)
            self.assertEqual(raw.strip(), "")

    def test_unwritable_observation_state_raises_bounded_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            (repo_path / ".code-mower").write_text("blocked", encoding="utf-8")
            with mock.patch(
                "code_mower.cloud_client.operations.run_gh_pr_list",
                return_value=[self._merged_pr("1")],
            ), mock.patch(
                "code_mower.cloud_client.operations.build_cloud_bundle"
            ) as bundle:
                with self.assertRaises(CloudBundleError) as ctx:
                    pr_outcomes_upload(
                        repo_path=repo_path,
                        output_dir=repo_path / "bundle",
                        repo_slug="owner/repo",
                        team_id="",
                        install_id="",
                        source="unit-test",
                        limit=10,
                        endpoint="https://codemower.example.com/api/upload",
                        token_env="CODE_MOWER_TEST_TOKEN",
                        yes=False,
                        timeout=1.0,
                    )
            bundle.assert_not_called()
            message = str(ctx.exception)
            self.assertIn("observation state", message)
            self.assertNotIn(str(repo_path), message)

    def _upload_with_spend(
        self,
        repo_path: Path,
        spend_path: Path | None,
    ) -> dict[str, object]:
        with mock.patch(
            "code_mower.cloud_client.operations.run_gh_pr_list",
            return_value=[self._merged_pr("1")],
        ):
            return pr_outcomes_upload(
                repo_path=repo_path,
                output_dir=repo_path / "bundle",
                repo_slug="owner/repo",
                team_id="",
                install_id="",
                source="unit-test",
                limit=10,
                endpoint="https://codemower.example.com/api/upload",
                token_env="CODE_MOWER_TEST_TOKEN",
                yes=False,
                timeout=1.0,
                spend_path=spend_path,
            )

    def _assert_invalid_spend_aborts(
        self, repo_path: Path, spend_path: Path
    ) -> None:
        with mock.patch(
            "code_mower.cloud_client.operations.run_gh_pr_list",
            return_value=[self._merged_pr("1")],
        ), mock.patch(
            "code_mower.cloud_client.operations.build_cloud_bundle"
        ) as bundle:
            with self.assertRaises(CloudBundleError) as ctx:
                pr_outcomes_upload(
                    repo_path=repo_path,
                    output_dir=repo_path / "bundle",
                    repo_slug="owner/repo",
                    team_id="",
                    install_id="",
                    source="unit-test",
                    limit=10,
                    endpoint="https://codemower.example.com/api/upload",
                    token_env="CODE_MOWER_TEST_TOKEN",
                    yes=False,
                    timeout=1.0,
                    spend_path=spend_path,
                )
        bundle.assert_not_called()
        message = str(ctx.exception)
        self.assertIn("spend ledger", message)
        self.assertNotIn(str(repo_path), message)
        self.assertNotIn(str(spend_path), message)

    def test_explicit_missing_spend_ledger_aborts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            self._assert_invalid_spend_aborts(
                repo_path, repo_path / "missing-spend.json"
            )

    def test_explicit_directory_spend_ledger_aborts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            spend_dir = repo_path / "spend-dir"
            spend_dir.mkdir()
            self._assert_invalid_spend_aborts(repo_path, spend_dir)

    def test_explicit_symlink_spend_ledger_aborts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            target = repo_path / "real-spend.json"
            target.write_text(
                json.dumps({"schema": reviewer_spend.SPEND_SCHEMA, "runs": []}),
                encoding="utf-8",
            )
            link = repo_path / "spend-link.json"
            link.symlink_to(target)
            self._assert_invalid_spend_aborts(repo_path, link)

    def test_explicit_malformed_spend_ledger_aborts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            spend_path = repo_path / "bad-spend.json"
            spend_path.write_text("{not json", encoding="utf-8")
            self._assert_invalid_spend_aborts(repo_path, spend_path)

    def test_explicit_non_object_spend_ledger_aborts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            spend_path = repo_path / "list-spend.json"
            spend_path.write_text('["not", "an", "object"]', encoding="utf-8")
            self._assert_invalid_spend_aborts(repo_path, spend_path)

    def test_cli_explicit_missing_spend_ledger_exits_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            spend_path = repo_path / "missing-spend.json"
            with mock.patch(
                "code_mower.cloud_client.operations.run_gh_pr_list",
                return_value=[self._merged_pr("1")],
            ):
                out = StringIO()
                err = StringIO()
                with redirect_stdout(out), redirect_stderr(err):
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
                            "--spend",
                            str(spend_path),
                            "--json",
                        ]
                    )
            self.assertEqual(code, 1)
            self.assertEqual(out.getvalue().strip(), "")
            self.assertIn("spend ledger", err.getvalue())
            self.assertNotIn(str(spend_path), err.getvalue())
            self.assertNotIn(str(repo_path), err.getvalue())

    def test_omitted_spend_ledger_uses_absent_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            builder_dir = repo_path / ".code-mower" / "builder-runs"
            builder_dir.mkdir(parents=True)
            (builder_dir / "devin-local-pr-1-aa11.cloud-event.json").write_text(
                json.dumps(_builder_run_event("b1", "1", 0.10)),
                encoding="utf-8",
            )

            result = self._upload_with_spend(repo_path, None)
            self.assertEqual(result["status"], "dry_run")
            self.assertEqual(result["errors"], [])
            events = self._emitted_events(result)
            self.assertEqual(
                events["1"]["dimensions"]["cost_coverage"], "complete"
            )

    def test_explicit_valid_spend_ledger_is_included(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            spend_path = repo_path / "custom-spend.json"
            spend_path.write_text(
                json.dumps(
                    {
                        "schema": reviewer_spend.SPEND_SCHEMA,
                        "runs": [
                            {
                                "run_id": "run-1",
                                "lane": "claude-audit",
                                "repo": "owner/repo",
                                "pr_number": 1,
                                "head_sha": "abc123",
                                "created_at": "2026-09-03T11:00:00Z",
                                "cost_usd": 0.05,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = self._upload_with_spend(repo_path, spend_path)
            self.assertEqual(result["status"], "dry_run")
            self.assertEqual(result["errors"], [])
            events = self._emitted_events(result)
            self.assertEqual(
                events["1"]["dimensions"]["cost_coverage"], "complete"
            )

    def _assert_invalid_default_spend_aborts(
        self, repo_path: Path
    ) -> None:
        with mock.patch(
            "code_mower.cloud_client.operations.run_gh_pr_list",
            return_value=[self._merged_pr("1")],
        ), mock.patch(
            "code_mower.cloud_client.operations.build_cloud_bundle"
        ) as bundle:
            with self.assertRaises(CloudBundleError) as ctx:
                self._upload_with_spend(repo_path, None)
        bundle.assert_not_called()
        message = str(ctx.exception)
        self.assertIn("spend ledger", message)
        self.assertNotIn(str(repo_path), message)

    def test_default_missing_spend_ledger_allows_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            builder_dir = repo_path / ".code-mower" / "builder-runs"
            builder_dir.mkdir(parents=True)
            (
                builder_dir / "devin-local-pr-1-aa11.cloud-event.json"
            ).write_text(
                json.dumps(_builder_run_event("b1", "1", 0.10)),
                encoding="utf-8",
            )

            result = self._upload_with_spend(repo_path, None)
            self.assertEqual(result["status"], "dry_run")
            self.assertEqual(result["errors"], [])
            events = self._emitted_events(result)
            self.assertEqual(
                events["1"]["dimensions"]["cost_coverage"], "complete"
            )

    def test_default_directory_spend_ledger_aborts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            spend_dir = repo_path / ".code-mower" / "reviewer-spend.json"
            spend_dir.parent.mkdir(parents=True, exist_ok=True)
            spend_dir.mkdir()
            self._assert_invalid_default_spend_aborts(repo_path)

    def test_default_dangling_symlink_spend_ledger_aborts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            spend_path = repo_path / ".code-mower" / "reviewer-spend.json"
            spend_path.parent.mkdir(parents=True, exist_ok=True)
            spend_path.symlink_to("nonexistent-target")
            self._assert_invalid_default_spend_aborts(repo_path)

    def test_default_empty_runs_ledger_allows_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            spend_path = repo_path / ".code-mower" / "reviewer-spend.json"
            spend_path.parent.mkdir(parents=True, exist_ok=True)
            spend_path.write_text(
                json.dumps({"schema": reviewer_spend.SPEND_SCHEMA, "runs": []}),
                encoding="utf-8",
            )
            builder_dir = repo_path / ".code-mower" / "builder-runs"
            builder_dir.mkdir(parents=True)
            (
                builder_dir / "devin-local-pr-1-aa11.cloud-event.json"
            ).write_text(
                json.dumps(_builder_run_event("b1", "1", 0.10)),
                encoding="utf-8",
            )

            result = self._upload_with_spend(repo_path, None)
            self.assertEqual(result["status"], "dry_run")
            self.assertEqual(result["errors"], [])
            events = self._emitted_events(result)
            self.assertEqual(
                events["1"]["dimensions"]["cost_coverage"], "complete"
            )

    def test_default_malformed_runs_ledger_aborts(self) -> None:
        for contents in ("{}", '{"runs": null}'):
            with self.subTest(contents=contents):
                with tempfile.TemporaryDirectory() as tmp:
                    repo_path = Path(tmp)
                    spend_path = (
                        repo_path / ".code-mower" / "reviewer-spend.json"
                    )
                    spend_path.parent.mkdir(parents=True, exist_ok=True)
                    spend_path.write_text(contents, encoding="utf-8")
                    self._assert_invalid_default_spend_aborts(repo_path)


class PrOutcomeIdentityP2Tests(unittest.TestCase):
    def test_repaired_evidence_changes_fingerprint_and_ordering(self) -> None:
        run_events = [
            _builder_run_event("b1", "80", 0.15),
        ]
        incomplete = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="80",
            outcome="merged",
            opened_at="2026-09-03T10:00:00Z",
            merged_at="2026-09-03T12:00:00Z",
            run_events=run_events,
            created_at="2026-09-03T13:00:00Z",
            evidence_incomplete=True,
        )

        self.assertEqual(incomplete["dimensions"]["cost_coverage"], "partial")
        self.assertEqual(incomplete["metrics"]["cost_expected_run_count"], 2)
        self.assertEqual(
            incomplete["dimensions"]["missing_cost_sources"],
            ["unreadable-evidence"],
        )
        self.assertTrue(incomplete["dimensions"].get("evidence_incomplete"))

        prior = pr_outcome_observation_record(incomplete)
        repaired = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="80",
            outcome="merged",
            opened_at="2026-09-03T10:00:00Z",
            merged_at="2026-09-03T12:00:00Z",
            run_events=run_events,
            created_at="2026-09-03T13:00:00Z",
            evidence_incomplete=False,
            prior_observation=prior,
        )

        self.assertEqual(repaired["dimensions"]["cost_coverage"], "complete")
        self.assertEqual(repaired["metrics"]["cost_expected_run_count"], 1)
        self.assertNotIn("missing_cost_sources", repaired["dimensions"])
        self.assertNotEqual(incomplete["event_id"], repaired["event_id"])
        self.assertGreater(repaired["created_at"], incomplete["created_at"])

        # An unchanged retry of the repaired observation stays idempotent.
        retry_prior = pr_outcome_observation_record(repaired)
        retry = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="80",
            outcome="merged",
            opened_at="2026-09-03T10:00:00Z",
            merged_at="2026-09-03T12:00:00Z",
            run_events=run_events,
            created_at="2026-09-03T13:00:00Z",
            evidence_incomplete=False,
            prior_observation=retry_prior,
        )
        self.assertEqual(repaired["event_id"], retry["event_id"])
        self.assertEqual(repaired["created_at"], retry["created_at"])


class PrOutcomeFailClosedP2Tests(unittest.TestCase):
    def _run_upload(
        self,
        repo_path: Path,
        output_dir: Path,
        pr_records: list[dict[str, object]],
    ) -> tuple[int, str]:
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
        return code, out.getvalue()

    def _merged_pr(self, number: str) -> dict[str, object]:
        return {
            "number": number,
            "state": "MERGED",
            "createdAt": "2026-09-03T10:00:00Z",
            "mergedAt": "2026-09-03T12:00:00Z",
            "updatedAt": "2026-09-03T13:00:00Z",
        }

    def _emitted_events(self, result: dict[str, object]) -> dict[str, dict]:
        manifest = json.loads(
            Path(result["export"]["manifest"]).read_text(encoding="utf-8")
        )
        return {
            event["dimensions"]["pr_number"]: event
            for event in manifest["events"]
        }

    def test_unenumerable_builder_dir_records_unattributable_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            builder_dir = repo_path / ".code-mower" / "builder-runs"
            builder_dir.mkdir(parents=True)
            try:
                builder_dir.chmod(0o000)
                code, raw = self._run_upload(
                    repo_path,
                    repo_path / "bundle",
                    [self._merged_pr("2")],
                )
            finally:
                builder_dir.chmod(0o755)
            self.assertEqual(code, 0, raw)
            result = json.loads(raw)
            self.assertEqual(result["status"], "dry_run")
            events = self._emitted_events(result)
            pr2 = events["2"]
            self.assertEqual(pr2["dimensions"]["cost_coverage"], "unknown")
            self.assertEqual(pr2["metrics"]["cost_expected_run_count"], 1)
            self.assertIn(
                "unreadable-evidence",
                pr2["dimensions"]["missing_cost_sources"],
            )
            self.assertTrue(pr2["dimensions"].get("evidence_incomplete"))
            self.assertEqual(len(result["errors"]), 1)
            self.assertIn("not attributable", result["errors"][0])

    def test_unusable_builder_record_routes_to_filename_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            builder_dir = repo_path / ".code-mower" / "builder-runs"
            builder_dir.mkdir(parents=True)
            (builder_dir / "devin-local-pr-5-aa55.cloud-event.json").write_text(
                json.dumps({"event_type": "builder_run"}),
                encoding="utf-8",
            )
            (builder_dir / "devin-local-pr-6-bb66.cloud-event.json").write_text(
                json.dumps(_builder_run_event("b6", "6", 0.10)),
                encoding="utf-8",
            )

            code, raw = self._run_upload(
                repo_path,
                repo_path / "bundle",
                [self._merged_pr("5"), self._merged_pr("6")],
            )
            self.assertEqual(code, 0, raw)
            result = json.loads(raw)
            events = self._emitted_events(result)
            self.assertEqual(
                events["5"]["dimensions"]["cost_coverage"], "unknown"
            )
            self.assertEqual(
                events["5"]["metrics"]["cost_expected_run_count"], 1
            )
            self.assertEqual(
                events["5"]["dimensions"]["missing_cost_sources"],
                ["unreadable-evidence"],
            )
            self.assertEqual(
                events["6"]["dimensions"]["cost_coverage"], "complete"
            )
            self.assertEqual(
                events["6"]["metrics"]["cost_expected_run_count"], 1
            )
            self.assertTrue(any("PR 5" in e for e in result["errors"]))
            self.assertNotIn(str(repo_path), " ".join(result["errors"]))


class PrOutcomeStateFailClosedTests(unittest.TestCase):
    def _run_upload(
        self,
        repo_path: Path,
        output_dir: Path,
        pr_records: list[dict[str, object]],
    ) -> tuple[int, str]:
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
        return code, out.getvalue()

    def _merged_pr(self, number: str) -> dict[str, object]:
        return {
            "number": number,
            "state": "MERGED",
            "createdAt": "2026-09-03T10:00:00Z",
            "mergedAt": "2026-09-03T12:00:00Z",
            "updatedAt": "2026-09-03T13:00:00Z",
        }

    def _upload_kwargs(self, repo_path: Path) -> dict[str, object]:
        return {
            "repo_path": repo_path,
            "output_dir": repo_path / "bundle",
            "repo_slug": "owner/repo",
            "team_id": "",
            "install_id": "",
            "source": "unit-test",
            "limit": 10,
            "endpoint": "https://codemower.example.com/api/upload",
            "token_env": "CODE_MOWER_TEST_TOKEN",
            "yes": False,
            "timeout": 1.0,
        }

    def _state_path(self, repo_path: Path) -> Path:
        return repo_path / ".code-mower" / "pr-outcome-observations.json"

    def test_missing_state_file_is_valid_initial_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / ".code-mower" / "pr-outcome-observations.json"
            self.assertEqual(load_pr_outcome_observations(missing), {})

    def test_corrupt_state_file_aborts_before_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            state_path = self._state_path(repo_path)
            state_path.parent.mkdir(parents=True)
            state_path.write_text("{not json", encoding="utf-8")
            with mock.patch(
                "code_mower.cloud_client.operations.run_gh_pr_list",
                return_value=[self._merged_pr("1")],
            ), mock.patch(
                "code_mower.cloud_client.operations.build_cloud_bundle"
            ) as bundle:
                with self.assertRaises(CloudBundleError) as ctx:
                    pr_outcomes_upload(**self._upload_kwargs(repo_path))
            bundle.assert_not_called()
            message = str(ctx.exception)
            self.assertIn("observation state", message)
            self.assertIn("malformed", message)
            self.assertNotIn(str(repo_path), message)
            self.assertNotIn(str(state_path), message)

    def test_malformed_state_structure_aborts_before_export(self) -> None:
        for payload in (
            '"just a string"',
            '{"observations": []}',
            '{"observations": {"owner/repo#1": "oops"}}',
            '{"observations": {"owner/repo#1": {"fingerprint": "abc"}}}',
        ):
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as tmp:
                    repo_path = Path(tmp)
                    state_path = self._state_path(repo_path)
                    state_path.parent.mkdir(parents=True)
                    state_path.write_text(payload, encoding="utf-8")
                    with mock.patch(
                        "code_mower.cloud_client.operations.run_gh_pr_list",
                        return_value=[self._merged_pr("1")],
                    ), mock.patch(
                        "code_mower.cloud_client.operations.build_cloud_bundle"
                    ) as bundle:
                        with self.assertRaises(CloudBundleError) as ctx:
                            pr_outcomes_upload(**self._upload_kwargs(repo_path))
                    bundle.assert_not_called()
                    self.assertIn("observation state", str(ctx.exception))
                    self.assertNotIn(str(repo_path), str(ctx.exception))

    def test_unreadable_state_file_aborts_before_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            state_path = self._state_path(repo_path)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                '{"schema": "x", "observations": {}}', encoding="utf-8"
            )
            try:
                state_path.chmod(0o000)
                with mock.patch(
                    "code_mower.cloud_client.operations.run_gh_pr_list",
                    return_value=[self._merged_pr("1")],
                ), mock.patch(
                    "code_mower.cloud_client.operations.build_cloud_bundle"
                ) as bundle:
                    with self.assertRaises(CloudBundleError) as ctx:
                        pr_outcomes_upload(**self._upload_kwargs(repo_path))
            finally:
                state_path.chmod(0o644)
            bundle.assert_not_called()
            message = str(ctx.exception)
            self.assertIn("observation state", message)
            self.assertNotIn(str(repo_path), message)
            self.assertNotIn(str(state_path), message)

    def test_corrupt_state_aborts_via_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            state_path = self._state_path(repo_path)
            state_path.parent.mkdir(parents=True)
            state_path.write_text("not json at all", encoding="utf-8")
            code, raw = self._run_upload(
                repo_path, repo_path / "bundle", [self._merged_pr("1")]
            )
            self.assertEqual(code, 1)
            self.assertEqual(raw.strip(), "")

    def test_state_directory_where_file_expected_aborts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            self._state_path(repo_path).mkdir(parents=True)
            with mock.patch(
                "code_mower.cloud_client.operations.run_gh_pr_list",
                return_value=[self._merged_pr("1")],
            ), mock.patch(
                "code_mower.cloud_client.operations.build_cloud_bundle"
            ) as bundle:
                with self.assertRaises(CloudBundleError) as ctx:
                    pr_outcomes_upload(**self._upload_kwargs(repo_path))
            bundle.assert_not_called()
            self.assertIn("observation state", str(ctx.exception))
            self.assertNotIn(str(repo_path), str(ctx.exception))

    def test_dangling_symlink_state_aborts_and_is_never_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            state_path = self._state_path(repo_path)
            state_path.parent.mkdir(parents=True)
            state_path.symlink_to("nonexistent-target")
            with self.assertRaises(CloudBundleError) as ctx:
                load_pr_outcome_observations(state_path)
            message = str(ctx.exception)
            self.assertIn("observation state", message)
            self.assertIn("malformed", message)
            self.assertNotIn(str(repo_path), message)
            self.assertNotIn(str(state_path), message)
            self.assertNotIn(tmp, message)
            with self.assertRaises(CloudBundleError):
                save_pr_outcome_observations(state_path, {})
            self.assertTrue(state_path.is_symlink())
            self.assertFalse(state_path.exists())

    def test_symlink_to_valid_state_file_aborts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            target = repo_path / "real-state.json"
            target.write_text(
                json.dumps(
                    {
                        "schema": "code_mower.prOutcomeObservations.v1",
                        "observations": {},
                    }
                ),
                encoding="utf-8",
            )
            state_path = self._state_path(repo_path)
            state_path.parent.mkdir(parents=True)
            state_path.symlink_to(target)
            with self.assertRaises(CloudBundleError) as ctx:
                load_pr_outcome_observations(state_path)
            message = str(ctx.exception)
            self.assertIn("observation state", message)
            self.assertNotIn(str(repo_path), message)
            self.assertNotIn(str(state_path), message)

    def test_non_regular_state_entries_abort_on_load_and_save(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            state_path = self._state_path(repo_path)
            state_path.mkdir(parents=True)
            for op in (
                lambda: load_pr_outcome_observations(state_path),
                lambda: save_pr_outcome_observations(state_path, {}),
            ):
                with self.assertRaises(CloudBundleError) as ctx:
                    op()
                self.assertIn("observation state", str(ctx.exception))
                self.assertNotIn(str(repo_path), str(ctx.exception))
            self.assertTrue(state_path.is_dir())

    def test_valid_regular_state_file_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            state_path = self._state_path(repo_path)
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema": "code_mower.prOutcomeObservations.v1",
                        "observations": {
                            "owner/repo#1": {
                                "fingerprint": "abc123",
                                "created_at": "2026-09-03T10:00:00Z",
                                "source_freshness": "2026-09-03T13:00:00Z",
                                "outcome": "merged",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            loaded = load_pr_outcome_observations(state_path)
            self.assertEqual(
                loaded,
                {
                    "owner/repo#1": {
                        "fingerprint": "abc123",
                        "created_at": "2026-09-03T10:00:00Z",
                        "source_freshness": "2026-09-03T13:00:00Z",
                        "outcome": "merged",
                    }
                },
            )


class PrOutcomeNonRegularEvidenceTests(unittest.TestCase):
    def _run_upload(
        self,
        repo_path: Path,
        output_dir: Path,
        pr_records: list[dict[str, object]],
    ) -> tuple[int, str]:
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
        return code, out.getvalue()

    def _merged_pr(self, number: str) -> dict[str, object]:
        return {
            "number": number,
            "state": "MERGED",
            "createdAt": "2026-09-03T10:00:00Z",
            "mergedAt": "2026-09-03T12:00:00Z",
            "updatedAt": "2026-09-03T13:00:00Z",
        }

    def _emitted_events(self, result: dict[str, object]) -> dict[str, dict]:
        manifest = json.loads(
            Path(result["export"]["manifest"]).read_text(encoding="utf-8")
        )
        return {
            event["dimensions"]["pr_number"]: event
            for event in manifest["events"]
        }

    def test_directory_named_like_evidence_counts_as_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            builder_dir = repo_path / ".code-mower" / "builder-runs"
            (builder_dir / "devin-local-pr-8-aa88.cloud-event.json").mkdir(
                parents=True
            )
            (builder_dir / "devin-local-pr-9-bb99.cloud-event.json").write_text(
                json.dumps(_builder_run_event("b9", "9", 0.10)),
                encoding="utf-8",
            )

            code, raw = self._run_upload(
                repo_path,
                repo_path / "bundle",
                [self._merged_pr("8"), self._merged_pr("9")],
            )
            self.assertEqual(code, 0, raw)
            result = json.loads(raw)
            events = self._emitted_events(result)
            self.assertEqual(
                events["8"]["dimensions"]["cost_coverage"], "unknown"
            )
            self.assertEqual(
                events["8"]["dimensions"]["missing_cost_sources"],
                ["unreadable-evidence"],
            )
            validate_cloud_event(events["8"])
            self.assertEqual(
                events["9"]["dimensions"]["cost_coverage"], "complete"
            )
            self.assertTrue(any("PR 8" in e for e in result["errors"]))
            self.assertNotIn(str(repo_path), " ".join(result["errors"]))

    def test_symlinked_evidence_file_counts_as_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            builder_dir = repo_path / ".code-mower" / "builder-runs"
            builder_dir.mkdir(parents=True)
            target = repo_path / "real-event.json"
            target.write_text(
                json.dumps(_builder_run_event("b10", "10", 0.10)),
                encoding="utf-8",
            )
            (builder_dir / "devin-local-pr-10-cc10.cloud-event.json").symlink_to(
                target
            )

            code, raw = self._run_upload(
                repo_path, repo_path / "bundle", [self._merged_pr("10")]
            )
            self.assertEqual(code, 0, raw)
            result = json.loads(raw)
            events = self._emitted_events(result)
            self.assertEqual(
                events["10"]["dimensions"]["cost_coverage"], "unknown"
            )
            self.assertEqual(
                events["10"]["metrics"]["cost_expected_run_count"], 1
            )
            self.assertEqual(
                events["10"]["dimensions"]["missing_cost_sources"],
                ["unreadable-evidence"],
            )
            validate_cloud_event(events["10"])
            self.assertTrue(any("PR 10" in e for e in result["errors"]))
            self.assertNotIn(str(repo_path), " ".join(result["errors"]))
            self.assertNotIn(str(target), " ".join(result["errors"]))

    def test_unattributable_non_regular_entry_suppresses_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            builder_dir = repo_path / ".code-mower" / "builder-runs"
            (builder_dir / "odd.cloud-event.json").mkdir(parents=True)
            (builder_dir / "devin-local-pr-2-bb22.cloud-event.json").write_text(
                json.dumps(_builder_run_event("b2", "2", 0.10)),
                encoding="utf-8",
            )

            code, raw = self._run_upload(
                repo_path, repo_path / "bundle", [self._merged_pr("2")]
            )
            self.assertEqual(code, 0, raw)
            result = json.loads(raw)
            events = self._emitted_events(result)
            self.assertEqual(
                events["2"]["dimensions"]["cost_coverage"], "partial"
            )
            self.assertIn(
                "unreadable-evidence",
                events["2"]["dimensions"]["missing_cost_sources"],
            )
            self.assertTrue(events["2"]["dimensions"]["evidence_incomplete"])
            self.assertTrue(
                any("not attributable" in e for e in result["errors"])
            )


class PrOutcomeInvalidPrNumberTests(unittest.TestCase):
    def _run_upload(
        self,
        repo_path: Path,
        output_dir: Path,
        pr_records: list[dict[str, object]],
    ) -> tuple[int, str]:
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
        return code, out.getvalue()

    def _merged_pr(self, number: str) -> dict[str, object]:
        return {
            "number": number,
            "state": "MERGED",
            "createdAt": "2026-09-03T10:00:00Z",
            "mergedAt": "2026-09-03T12:00:00Z",
            "updatedAt": "2026-09-03T13:00:00Z",
        }

    def _emitted_events(self, result: dict[str, object]) -> dict[str, dict]:
        manifest = json.loads(
            Path(result["export"]["manifest"]).read_text(encoding="utf-8")
        )
        return {
            event["dimensions"]["pr_number"]: event
            for event in manifest["events"]
        }

    def test_invalid_pr_number_shapes_route_to_fail_closed(self) -> None:
        for bad_number in ("unknown", "0", "-2", "1.5", "", "1e3"):
            with self.subTest(pr_number=bad_number):
                with tempfile.TemporaryDirectory() as tmp:
                    repo_path = Path(tmp)
                    builder_dir = repo_path / ".code-mower" / "builder-runs"
                    builder_dir.mkdir(parents=True)
                    event = _builder_run_event("b11", "11", 0.10)
                    event["dimensions"]["pr_number"] = bad_number
                    (
                        builder_dir / "devin-local-pr-11-dd11.cloud-event.json"
                    ).write_text(json.dumps(event), encoding="utf-8")

                    code, raw = self._run_upload(
                        repo_path,
                        repo_path / "bundle",
                        [self._merged_pr("11")],
                    )
                    self.assertEqual(code, 0, raw)
                    result = json.loads(raw)
                    events = self._emitted_events(result)
                    pr11 = events["11"]
                    self.assertEqual(
                        pr11["dimensions"]["cost_coverage"], "unknown"
                    )
                    self.assertEqual(
                        pr11["metrics"]["cost_expected_run_count"], 1
                    )
                    self.assertEqual(
                        pr11["dimensions"]["missing_cost_sources"],
                        ["unreadable-evidence"],
                    )
                    validate_cloud_event(pr11)
                    self.assertTrue(
                        any("PR 11" in e for e in result["errors"])
                    )
                    self.assertNotIn(
                        str(repo_path), " ".join(result["errors"])
                    )

    def test_non_string_pr_number_shapes_route_to_fail_closed(self) -> None:
        for bad_number in (0, -1, 1.5, True):
            with self.subTest(pr_number=bad_number):
                with tempfile.TemporaryDirectory() as tmp:
                    repo_path = Path(tmp)
                    builder_dir = repo_path / ".code-mower" / "builder-runs"
                    builder_dir.mkdir(parents=True)
                    event = _builder_run_event("b12", "12", 0.10)
                    event["dimensions"]["pr_number"] = bad_number
                    (
                        builder_dir / "devin-local-pr-12-ee12.cloud-event.json"
                    ).write_text(json.dumps(event), encoding="utf-8")

                    code, raw = self._run_upload(
                        repo_path,
                        repo_path / "bundle",
                        [self._merged_pr("12")],
                    )
                    self.assertEqual(code, 0, raw)
                    result = json.loads(raw)
                    events = self._emitted_events(result)
                    self.assertEqual(
                        events["12"]["dimensions"]["cost_coverage"], "unknown"
                    )
                    self.assertTrue(
                        any("PR 12" in e for e in result["errors"])
                    )

    def test_invalid_pr_number_without_filename_attribution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            builder_dir = repo_path / ".code-mower" / "builder-runs"
            builder_dir.mkdir(parents=True)
            event = _builder_run_event("b13", "unknown", 0.10)
            (builder_dir / "devin-run-ff13.cloud-event.json").write_text(
                json.dumps(event), encoding="utf-8"
            )
            (builder_dir / "devin-local-pr-13-ff13.cloud-event.json").write_text(
                json.dumps(_builder_run_event("b13b", "13", 0.10)),
                encoding="utf-8",
            )

            code, raw = self._run_upload(
                repo_path, repo_path / "bundle", [self._merged_pr("13")]
            )
            self.assertEqual(code, 0, raw)
            result = json.loads(raw)
            events = self._emitted_events(result)
            self.assertEqual(
                events["13"]["dimensions"]["cost_coverage"], "partial"
            )
            self.assertIn(
                "unreadable-evidence",
                events["13"]["dimensions"]["missing_cost_sources"],
            )
            self.assertTrue(events["13"]["dimensions"]["evidence_incomplete"])
            self.assertTrue(
                any("not attributable" in e for e in result["errors"])
            )


class PrOutcomeObservationLockTests(unittest.TestCase):
    def _merged_pr(self, number: str) -> dict[str, object]:
        return {
            "number": number,
            "state": "MERGED",
            "createdAt": "2026-09-03T10:00:00Z",
            "mergedAt": "2026-09-03T12:00:00Z",
            "updatedAt": "2026-09-03T13:00:00Z",
        }

    def _upload_kwargs(self, repo_path: Path) -> dict[str, object]:
        return {
            "repo_path": repo_path,
            "output_dir": repo_path / "bundle",
            "repo_slug": "owner/repo",
            "team_id": "",
            "install_id": "",
            "source": "unit-test",
            "limit": 10,
            "endpoint": "https://codemower.example.com/api/upload",
            "token_env": "CODE_MOWER_TEST_TOKEN",
            "yes": False,
            "timeout": 1.0,
        }

    def test_observation_state_is_read_and_written_under_exclusive_lock(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            reacquired: list[bool] = []

            real_lock = exclusive_file_lock

            def recording_lock(lock_path, **kwargs):
                ctx = real_lock(lock_path, **kwargs)

                class _Recording:
                    def __enter__(self):
                        handle = ctx.__enter__()
                        # flock is per open-file-description: a second open
                        # must contend while the command holds the lock.
                        try:
                            with real_lock(lock_path, timeout_seconds=0):
                                reacquired.append(True)
                        except FileLockError:
                            reacquired.append(False)
                        return handle

                    def __exit__(self, *exc_info):
                        return ctx.__exit__(*exc_info)

                return _Recording()

            with mock.patch(
                "code_mower.cloud_client.operations.run_gh_pr_list",
                return_value=[self._merged_pr("1")],
            ), mock.patch(
                "code_mower.cloud_client.operations.exclusive_file_lock",
                side_effect=recording_lock,
            ):
                result = pr_outcomes_upload(**self._upload_kwargs(repo_path))

            self.assertEqual(result["status"], "dry_run")
            self.assertEqual(result["event_count"], 1)
            # The lock was held for the whole read-modify-write: a concurrent
            # acquirer could not take it even once.
            self.assertEqual(reacquired, [False])
            self.assertFalse(reacquired[0])

    def test_lock_contention_aborts_with_bounded_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)

            def contended(*args, **kwargs):
                raise FileLockError(
                    "timed out waiting for an exclusive lock"
                )

            with mock.patch(
                "code_mower.cloud_client.operations.run_gh_pr_list",
                return_value=[self._merged_pr("1")],
            ), mock.patch(
                "code_mower.cloud_client.operations.exclusive_file_lock",
                side_effect=contended,
            ), mock.patch(
                "code_mower.cloud_client.operations.build_cloud_bundle"
            ) as bundle:
                with self.assertRaises(CloudBundleError) as ctx:
                    pr_outcomes_upload(**self._upload_kwargs(repo_path))

            bundle.assert_not_called()
            message = str(ctx.exception)
            self.assertIn("observation state", message)
            self.assertNotIn(str(repo_path), message)

    def test_lock_oserror_aborts_with_bounded_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            (repo_path / ".code-mower").write_text("blocked", encoding="utf-8")
            with mock.patch(
                "code_mower.cloud_client.operations.run_gh_pr_list",
                return_value=[self._merged_pr("1")],
            ), mock.patch(
                "code_mower.cloud_client.operations.build_cloud_bundle"
            ) as bundle:
                with self.assertRaises(CloudBundleError) as ctx:
                    pr_outcomes_upload(**self._upload_kwargs(repo_path))
            bundle.assert_not_called()
            message = str(ctx.exception)
            self.assertIn("observation state", message)
            self.assertNotIn(str(repo_path), message)


class PrOutcomeStaleSnapshotTests(unittest.TestCase):
    def test_old_open_snapshot_cannot_supersede_newer_merged(self) -> None:
        merged = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="90",
            outcome="merged",
            opened_at="2026-09-03T10:00:00Z",
            merged_at="2026-09-03T12:00:00Z",
            run_events=[_builder_run_event("b1", "90", 0.15)],
            created_at="2026-09-03T13:00:00Z",
        )
        prior = pr_outcome_observation_record(merged)
        with self.assertRaises(CloudBundleError):
            build_pr_outcome_event(
                repo_slug="owner/repo",
                pr_number="90",
                outcome="open",
                opened_at="2026-09-03T10:00:00Z",
                run_events=[],
                created_at="2026-09-03T12:00:00Z",
                prior_observation=prior,
            )

    def test_old_open_snapshot_with_late_run_cannot_supersede_merged(self) -> None:
        merged = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="91",
            outcome="merged",
            opened_at="2026-09-03T10:00:00Z",
            merged_at="2026-09-03T12:00:00Z",
            run_events=[_builder_run_event("b1", "91", 0.15)],
            created_at="2026-09-03T13:00:00Z",
        )
        prior = pr_outcome_observation_record(merged)
        with self.assertRaises(CloudBundleError):
            build_pr_outcome_event(
                repo_slug="owner/repo",
                pr_number="91",
                outcome="open",
                opened_at="2026-09-03T10:00:00Z",
                run_events=[
                    _reviewer_run_event(
                        "r1",
                        "91",
                        0.05,
                        created_at="2026-09-03T14:00:00Z",
                    )
                ],
                created_at="2026-09-03T12:00:00Z",
                prior_observation=prior,
            )


class PrOutcomeStateTimestampTests(unittest.TestCase):
    def test_naive_observation_created_at_aborts_before_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pr-outcome-observations.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": "code_mower.prOutcomeObservations.v1",
                        "observations": {
                            "owner/repo#1": {
                                "fingerprint": "abc",
                                "created_at": "2026-09-03T10:00:00",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(CloudBundleError) as ctx:
                load_pr_outcome_observations(path)
            self.assertIn("malformed", str(ctx.exception).lower())

    def test_malformed_observation_created_at_aborts_before_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pr-outcome-observations.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": "code_mower.prOutcomeObservations.v1",
                        "observations": {
                            "owner/repo#1": {
                                "fingerprint": "abc",
                                "created_at": "not-a-timestamp",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(CloudBundleError) as ctx:
                load_pr_outcome_observations(path)
            self.assertIn("malformed", str(ctx.exception).lower())


class PrOutcomeAuditP2Tests(unittest.TestCase):
    def _run_upload(
        self,
        repo_path: Path,
        output_dir: Path,
        pr_records: list[dict[str, object]],
        *,
        max_event_count: int | None = None,
        limit: int = 10,
    ) -> tuple[int, str]:
        with mock.patch(
            "code_mower.cloud_client.operations.run_gh_pr_list",
            return_value=pr_records,
        ):
            out = StringIO()
            with redirect_stdout(out):
                if max_event_count is not None:
                    with mock.patch(
                        "code_mower.cloud_client.operations.MAX_EVENT_COUNT",
                        max_event_count,
                    ):
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
                                "--limit",
                                str(limit),
                                "--json",
                            ]
                        )
                else:
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
                            "--limit",
                            str(limit),
                            "--json",
                        ]
                    )
            return code, out.getvalue()

    def _merged_pr(
        self, number: str, updated_at: str = "2026-09-03T13:00:00Z"
    ) -> dict[str, object]:
        return {
            "number": number,
            "state": "MERGED",
            "createdAt": "2026-09-03T10:00:00Z",
            "mergedAt": "2026-09-03T12:00:00Z",
            "updatedAt": updated_at,
        }

    def _emitted_events(self, result: dict[str, object]) -> dict[str, dict]:
        manifest = json.loads(
            Path(result["export"]["manifest"]).read_text(encoding="utf-8")
        )
        return {
            event["dimensions"]["pr_number"]: event
            for event in manifest["events"]
        }

    def test_unattributable_reviewer_spend_suppresses_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            spend_path = repo_path / ".code-mower" / "reviewer-spend.json"
            spend_path.parent.mkdir(parents=True)
            spend_path.write_text(
                json.dumps(
                    {
                        "schema": reviewer_spend.SPEND_SCHEMA,
                        "runs": [
                            {
                                "lane": "claude-audit",
                                "repo": "owner/repo",
                                "pr_number": 2,
                                "head_sha": "abc123",
                                "model": "sonnet",
                                "wall_seconds": 1.0,
                                "verdict": "PASS",
                                "created_at": "2026-09-03T11:00:00Z",
                                "cost_usd": 0.05,
                            },
                            {
                                "lane": "claude-audit",
                                "repo": "owner/repo",
                                "pr_number": "",
                                "head_sha": "def456",
                                "model": "sonnet",
                                "wall_seconds": 1.0,
                                "verdict": "PASS",
                                "created_at": "2026-09-03T11:00:00Z",
                                "cost_usd": 0.05,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            builder_dir = repo_path / ".code-mower" / "builder-runs"
            builder_dir.mkdir(parents=True)
            (
                builder_dir / "devin-local-pr-2-bb22.cloud-event.json"
            ).write_text(
                json.dumps(_builder_run_event("b2", "2", 0.10)),
                encoding="utf-8",
            )

            code, raw = self._run_upload(
                repo_path, repo_path / "bundle", [self._merged_pr("2")]
            )
            self.assertEqual(code, 0, raw)
            result = json.loads(raw)
            events = self._emitted_events(result)
            pr2 = events["2"]
            self.assertEqual(pr2["dimensions"]["cost_coverage"], "partial")
            self.assertEqual(pr2["metrics"]["cost_expected_run_count"], 3)
            self.assertIn(
                "unreadable-evidence",
                pr2["dimensions"]["missing_cost_sources"],
            )
            self.assertTrue(pr2["dimensions"]["evidence_incomplete"])
            self.assertTrue(
                any("parsed attempt(s)" in e for e in result["errors"])
            )
            self.assertNotIn(str(repo_path), " ".join(result["errors"]))

    def test_mismatched_repo_slug_suppresses_complete_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            builder_dir = repo_path / ".code-mower" / "builder-runs"
            builder_dir.mkdir(parents=True)
            (
                builder_dir / "devin-local-pr-2-aa22.cloud-event.json"
            ).write_text(
                json.dumps(
                    {
                        **_builder_run_event("b2a", "2", 0.10),
                        "repo_slug": "other/repo",
                    }
                ),
                encoding="utf-8",
            )
            (
                builder_dir / "devin-local-pr-2-bb22.cloud-event.json"
            ).write_text(
                json.dumps(_builder_run_event("b2b", "2", 0.10)),
                encoding="utf-8",
            )

            code, raw = self._run_upload(
                repo_path, repo_path / "bundle", [self._merged_pr("2")]
            )
            self.assertEqual(code, 0, raw)
            result = json.loads(raw)
            events = self._emitted_events(result)
            pr2 = events["2"]
            self.assertEqual(pr2["dimensions"]["cost_coverage"], "partial")
            self.assertEqual(pr2["metrics"]["cost_expected_run_count"], 2)
            self.assertIn(
                "unreadable-evidence",
                pr2["dimensions"]["missing_cost_sources"],
            )
            self.assertTrue(pr2["dimensions"]["evidence_incomplete"])
            self.assertTrue(
                any("parsed attempt(s)" in e for e in result["errors"])
            )
            self.assertNotIn(str(repo_path), " ".join(result["errors"]))

    def test_truncated_pr_can_retry_and_emit_later(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            state_path = (
                repo_path / ".code-mower" / "pr-outcome-observations.json"
            )
            pr_records = [
                self._merged_pr("1", "2026-09-03T10:00:00Z"),
                self._merged_pr("2", "2026-09-03T11:00:00Z"),
                self._merged_pr("3", "2026-09-03T12:00:00Z"),
            ]

            code, raw = self._run_upload(
                repo_path,
                repo_path / "bundle-1",
                pr_records,
                max_event_count=2,
                limit=2,
            )
            self.assertEqual(code, 0, raw)
            first = json.loads(raw)
            self.assertEqual(first["event_count"], 2)
            self.assertEqual(len(load_pr_outcome_observations(state_path)), 2)

            code, raw = self._run_upload(
                repo_path, repo_path / "bundle-2", pr_records
            )
            self.assertEqual(code, 0, raw)
            second = json.loads(raw)
            self.assertEqual(second["event_count"], 3)
            self.assertEqual(len(load_pr_outcome_observations(state_path)), 3)

    def test_unchanged_source_cost_sequence_emits_monotonic(self) -> None:
        base = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="95",
            outcome="merged",
            opened_at="2026-09-03T10:00:00Z",
            merged_at="2026-09-03T12:00:00Z",
            run_events=[_builder_run_event("b1", "95", 1.0)],
            created_at="2026-09-03T13:00:00Z",
        )
        prior = pr_outcome_observation_record(base)
        second = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="95",
            outcome="merged",
            opened_at="2026-09-03T10:00:00Z",
            merged_at="2026-09-03T12:00:00Z",
            run_events=[_builder_run_event("b1", "95", 2.0)],
            created_at="2026-09-03T13:00:00Z",
            prior_observation=prior,
        )
        self.assertGreater(second["created_at"], base["created_at"])
        validate_cloud_event(second)

        prior2 = pr_outcome_observation_record(second)
        third = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="95",
            outcome="merged",
            opened_at="2026-09-03T10:00:00Z",
            merged_at="2026-09-03T12:00:00Z",
            run_events=[_builder_run_event("b1", "95", 3.0)],
            created_at="2026-09-03T13:00:00Z",
            prior_observation=prior2,
        )
        self.assertGreater(third["created_at"], second["created_at"])
        validate_cloud_event(third)

    def test_newer_reopen_supersedes_closed_unmerged(self) -> None:
        closed = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="96",
            outcome="closed_unmerged",
            opened_at="2026-09-03T10:00:00Z",
            closed_at="2026-09-03T12:00:00Z",
            run_events=[],
            created_at="2026-09-03T13:00:00Z",
        )
        prior = pr_outcome_observation_record(closed)
        reopened = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="96",
            outcome="open",
            opened_at="2026-09-03T10:00:00Z",
            run_events=[],
            created_at="2026-09-03T14:00:00Z",
            prior_observation=prior,
        )
        self.assertEqual(reopened["dimensions"]["outcome"], "open")
        self.assertGreater(reopened["created_at"], closed["created_at"])
        validate_cloud_event(reopened)


class PrOutcomeInvalidBuilderDirP2Tests(unittest.TestCase):
    def _merged_pr(self, number: str) -> dict[str, object]:
        return {
            "number": number,
            "state": "MERGED",
            "createdAt": "2026-09-03T10:00:00Z",
            "mergedAt": "2026-09-03T12:00:00Z",
            "updatedAt": "2026-09-03T13:00:00Z",
        }

    def test_regular_file_builder_runs_cannot_report_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            builder_path = repo_path / ".code-mower" / "builder-runs"
            builder_path.parent.mkdir(parents=True)
            builder_path.write_text("not a directory", encoding="utf-8")

            with mock.patch(
                "code_mower.cloud_client.operations.run_gh_pr_list",
                return_value=[self._merged_pr("1")],
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
            self.assertEqual(code, 0, out.getvalue())
            result = json.loads(out.getvalue())
            manifest = json.loads(
                Path(result["export"]["manifest"]).read_text(encoding="utf-8")
            )
            pr = manifest["events"][0]
            self.assertNotEqual(pr["dimensions"]["cost_coverage"], "complete")
            self.assertIn(
                "unreadable-evidence",
                pr["dimensions"]["missing_cost_sources"],
            )
            self.assertTrue(pr["dimensions"]["evidence_incomplete"])


class PrOutcomePrivacyP2Tests(unittest.TestCase):
    def test_paths_and_secrets_are_canonicalized_before_upload(self) -> None:
        home_path = "/home/user/.code-mower/builder-runs"
        secret = "ghp_0123456789abcdefghijklmnopqrstuvwxyz/"
        event = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="97",
            outcome="merged",
            opened_at="2026-09-03T10:00:00Z",
            merged_at="2026-09-03T12:00:00Z",
            run_events=[
                {
                    "event_id": "b1",
                    "event_type": "builder_run",
                    "created_at": "2026-09-03T11:00:00Z",
                    "repo_slug": "owner/repo",
                    "provider": secret,
                    "lens": "implementation",
                    "status": "pr-opened",
                    "dimensions": {
                        "builder_provider": home_path,
                        "pr_number": "97",
                    },
                },
                {
                    "event_id": "r1",
                    "event_type": "reviewer_run",
                    "created_at": "2026-09-03T11:00:00Z",
                    "repo_slug": "owner/repo",
                    "provider": "claude",
                    "lens": "claude-audit",
                    "status": "pass",
                    "dimensions": {
                        "lane": secret,
                        "pr_number": "97",
                    },
                },
            ],
            created_at="2026-09-03T13:00:00Z",
        )

        self.assertEqual(event["dimensions"]["cost_coverage"], "unknown")
        self.assertEqual(
            event["dimensions"]["missing_cost_sources"],
            ["unknown-source"],
        )
        rendered = json.dumps(event)
        self.assertNotIn(home_path, rendered)
        self.assertNotIn(secret, rendered)
        validate_cloud_event(event)


class PrOutcomeConcurrencyP2Tests(unittest.TestCase):
    def test_local_evidence_is_loaded_under_observation_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            state = {"locked": False}

            class TrackingLock:
                def __init__(self, path: Path) -> None:
                    self.path = path

                def __enter__(self) -> "TrackingLock":
                    state["locked"] = True
                    return self

                def __exit__(self, *exc: object) -> bool:
                    state["locked"] = False
                    return False

            def assert_builder_locked(*args: object, **kwargs: object) -> tuple[list, list]:
                self.assertTrue(state["locked"])
                return [], []

            def assert_spend_locked(*args: object, **kwargs: object) -> list:
                self.assertTrue(state["locked"])
                return []

            with mock.patch(
                "code_mower.cloud_client.operations.exclusive_file_lock",
                TrackingLock,
            ), mock.patch(
                "code_mower.cloud_client.operations._builder_run_events",
                side_effect=assert_builder_locked,
            ) as builder_mock, mock.patch(
                "code_mower.cloud_client.operations._reviewer_spend_events",
                side_effect=assert_spend_locked,
            ) as spend_mock, mock.patch(
                "code_mower.cloud_client.operations.run_gh_pr_list",
                return_value=[
                    {
                        "number": "1",
                        "state": "MERGED",
                        "createdAt": "2026-09-03T10:00:00Z",
                        "mergedAt": "2026-09-03T12:00:00Z",
                        "updatedAt": "2026-09-03T13:00:00Z",
                    }
                ],
            ), mock.patch(
                "code_mower.cloud_client.operations.build_cloud_bundle"
            ) as bundle_mock, mock.patch(
                "code_mower.cloud_client.operations.run_cloud_doctor",
                return_value={"failures": []},
            ), mock.patch(
                "code_mower.cloud_client.operations.build_upload_payload",
                return_value={},
            ), mock.patch(
                "code_mower.cloud_client.operations.build_dogfood_dry_run_preview",
                return_value={},
            ):
                bundle_mock.return_value = {
                    "manifest": str(repo_path / "bundle" / "manifest.json")
                }
                pr_outcomes_upload(
                    repo_path=repo_path,
                    output_dir=repo_path / "bundle",
                    repo_slug="owner/repo",
                    team_id="",
                    install_id="",
                    source="unit-test",
                    limit=10,
                    endpoint="https://codemower.example.com/api/upload",
                    token_env="CODE_MOWER_TEST_TOKEN",
                    yes=False,
                    timeout=1.0,
                )

            self.assertTrue(builder_mock.called)
            self.assertTrue(spend_mock.called)


class PrOutcomeBuilderRunsP2Tests(unittest.TestCase):
    def test_absent_builder_runs_is_not_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            events, unreadable = _builder_run_events(repo_path)
            self.assertEqual(events, [])
            self.assertEqual(unreadable, [])

    def test_dangling_builder_runs_symlink_counts_as_unattributable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            builder_dir = repo_path / ".code-mower" / "builder-runs"
            builder_dir.parent.mkdir(parents=True)
            builder_dir.symlink_to("nowhere-missing")

            events, unreadable = _builder_run_events(repo_path)
            self.assertEqual(events, [])
            self.assertEqual(unreadable, [""])
            # No path leakage: entries are PR numbers or the empty marker.
            self.assertNotIn(str(builder_dir), " ".join(unreadable))
            self.assertNotIn(str(repo_path), " ".join(unreadable))

    def test_valid_builder_runs_directory_returns_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            builder_dir = repo_path / ".code-mower" / "builder-runs"
            builder_dir.mkdir(parents=True)
            (builder_dir / "devin-local-pr-1-aa11.cloud-event.json").write_text(
                json.dumps(_builder_run_event("b1", "1", 0.10)),
                encoding="utf-8",
            )

            events, unreadable = _builder_run_events(repo_path)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event_id"], "b1")
            self.assertEqual(unreadable, [])


class PrOutcomeIdentifierP2Tests(unittest.TestCase):
    def test_secret_like_lane_identifiers_become_unknown_source(self) -> None:
        # fmt: off
        cases = [
            # (raw identifier, kind, is_secret)
            ("ghp_0123456789abcdefghijklmnopqrstuvwxyz", "github_token", True),
            ("github_pat_0123456789abcdefghijklmnopqrstuvwxyz", "github_pat", True),
            ("sk-abcdefghijklmnopqrstuvwxyz123456789", "sk_key", True),
            ("Bearer abcdef0123456789abcdefghij", "bearer", True),
            ("bearer abcdef0123456789abcdefghij", "bearer_lowercase", True),
            ("cat /etc/passwd", "command", True),
            ("/home/user/.code-mower/builder-runs", "path", True),
            ("claude-audit", "valid_lane", False),
            ("codex-audit", "valid_lane", False),
        ]
        # fmt: on
        for raw, _kind, is_secret in cases:
            for event_kind in ("builder", "reviewer"):
                with self.subTest(identifier=raw, kind=event_kind):
                    if event_kind == "builder":
                        run_events = [
                            _builder_run_event("b1", "99", None, provider=raw)
                        ]
                    else:
                        run_events = [
                            _reviewer_run_event("r1", "99", None, lane=raw)
                        ]

                    event = build_pr_outcome_event(
                        repo_slug="owner/repo",
                        pr_number="99",
                        outcome="merged",
                        opened_at="2026-09-03T10:00:00Z",
                        merged_at="2026-09-03T12:00:00Z",
                        run_events=run_events,
                        created_at="2026-09-03T13:00:00Z",
                    )

                    # Invalid identifier input must not drop the whole event.
                    validate_cloud_event(event)
                    self.assertEqual(event["dimensions"]["cost_coverage"], "unknown")
                    if is_secret:
                        self.assertEqual(
                            event["dimensions"]["missing_cost_sources"],
                            ["unknown-source"],
                        )
                        self.assertNotIn(raw, json.dumps(event))
                    else:
                        self.assertEqual(
                            event["dimensions"]["missing_cost_sources"],
                            [raw],
                        )


class PrNumberCanonicalizationTests(unittest.TestCase):
    def _pr_record(self, number: object) -> dict[str, object]:
        return {
            "number": number,
            "state": "MERGED",
            "createdAt": "2026-09-03T10:00:00Z",
            "mergedAt": "2026-09-03T12:00:00Z",
            "updatedAt": "2026-09-03T13:00:00Z",
        }

    def _run_upload(
        self,
        repo_path: Path,
        pr_records: list[dict[str, object]],
    ) -> dict[str, object]:
        with mock.patch(
            "code_mower.cloud_client.operations.run_gh_pr_list",
            return_value=pr_records,
        ):
            return pr_outcomes_upload(
                repo_path=repo_path,
                output_dir=repo_path / "bundle",
                repo_slug="owner/repo",
                team_id="",
                install_id="",
                source="unit-test",
                limit=10,
                endpoint="https://codemower.example.com/api/upload",
                token_env="CODE_MOWER_TEST_TOKEN",
                yes=False,
                timeout=1.0,
            )

    def _emitted_events(self, result: dict[str, object]) -> dict[str, dict]:
        manifest = json.loads(
            Path(result["export"]["manifest"]).read_text(encoding="utf-8")
        )
        return {
            event["dimensions"]["pr_number"]: event
            for event in manifest["events"]
        }

    def test_builder_record_with_leading_zeros_joins_github_pr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            builder_dir = repo_path / ".code-mower" / "builder-runs"
            builder_dir.mkdir(parents=True)
            (builder_dir / "devin-local-pr-042-aa11.cloud-event.json").write_text(
                json.dumps(_builder_run_event("b1", "042", 0.15)),
                encoding="utf-8",
            )

            result = self._run_upload(repo_path, [self._pr_record(42)])

            self.assertEqual(result["status"], "dry_run")
            self.assertEqual(result["event_count"], 1)
            self.assertEqual(result["errors"], [])
            events = self._emitted_events(result)
            self.assertIn("42", events)
            self.assertEqual(events["42"]["dimensions"]["pr_number"], "42")
            self.assertEqual(events["42"]["dimensions"]["cost_coverage"], "complete")

    def test_reviewer_evidence_leading_zeros_joins_pr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            builder_dir = repo_path / ".code-mower" / "builder-runs"
            builder_dir.mkdir(parents=True)
            (builder_dir / "devin-local-pr-42-aa11.cloud-event.json").write_text(
                json.dumps(_builder_run_event("b1", "42", 0.15)),
                encoding="utf-8",
            )
            spend_path = repo_path / ".code-mower" / "reviewer-spend.json"
            spend_path.parent.mkdir(parents=True, exist_ok=True)
            spend_path.write_text(
                json.dumps({
                    "schema": reviewer_spend.SPEND_SCHEMA,
                    "runs": [{
                        "run_id": "r1",
                        "created_at": "2026-09-03T11:00:00Z",
                        "lane": "claude-audit",
                        "repo": "owner/repo",
                        "pr_number": "042",
                        "head_sha": "abcd1234",
                        "model": "claude",
                        "wall_seconds": 1.0,
                        "verdict": "pass",
                        "cost_usd": 0.10,
                    }],
                }),
                encoding="utf-8",
            )

            result = self._run_upload(repo_path, [self._pr_record(42)])

            self.assertEqual(result["status"], "dry_run")
            self.assertEqual(result["event_count"], 1)
            self.assertEqual(result["errors"], [])
            events = self._emitted_events(result)
            self.assertIn("42", events)
            self.assertEqual(events["42"]["dimensions"]["pr_number"], "42")
            self.assertEqual(events["42"]["dimensions"]["cost_coverage"], "complete")
            self.assertEqual(events["42"]["metrics"]["cost_reported_run_count"], 2)

    def test_filename_attributed_failure_with_leading_zeros_suppresses_complete(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            builder_dir = repo_path / ".code-mower" / "builder-runs"
            builder_dir.mkdir(parents=True)
            (builder_dir / "devin-local-pr-42-good.cloud-event.json").write_text(
                json.dumps(_builder_run_event("b1", "42", 0.15)),
                encoding="utf-8",
            )
            (builder_dir / "devin-local-pr-042-bad.cloud-event.json").write_text(
                "not valid json {",
                encoding="utf-8",
            )

            result = self._run_upload(repo_path, [self._pr_record(42)])

            self.assertEqual(result["status"], "dry_run")
            self.assertEqual(result["event_count"], 1)
            events = self._emitted_events(result)
            self.assertIn("42", events)
            pr42 = events["42"]
            self.assertEqual(pr42["dimensions"]["pr_number"], "42")
            self.assertEqual(pr42["dimensions"]["cost_coverage"], "partial")
            self.assertEqual(pr42["metrics"]["cost_reported_run_count"], 1)
            self.assertEqual(pr42["metrics"]["cost_expected_run_count"], 2)
            self.assertIn(
                "unreadable-evidence",
                pr42["dimensions"].get("missing_cost_sources", []),
            )
            self.assertTrue(any("PR 42" in e for e in result["errors"]))

    def test_event_and_state_identity_stable_under_equivalent_representations(
        self,
    ) -> None:
        run_events = [_builder_run_event("b1", "42", 0.15)]
        first = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="42",
            outcome="merged",
            opened_at="2026-09-03T10:00:00Z",
            merged_at="2026-09-03T12:00:00Z",
            run_events=run_events,
            created_at="2026-09-03T13:00:00Z",
        )
        second = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="042",
            outcome="merged",
            opened_at="2026-09-03T10:00:00Z",
            merged_at="2026-09-03T12:00:00Z",
            run_events=run_events,
            created_at="2026-09-03T13:00:00Z",
        )

        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(first["created_at"], second["created_at"])
        self.assertEqual(
            first["dimensions"]["pr_number"],
            second["dimensions"]["pr_number"],
        )
        self.assertEqual(
            pr_outcome_observation_key("owner/repo", "042"),
            pr_outcome_observation_key("owner/repo", "42"),
        )
        self.assertEqual(
            pr_outcome_observation_key("owner/repo", 42),
            "owner/repo#42",
        )

        # A state file keyed with leading zeros loads under the canonical key.
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "observations.json"
            record = pr_outcome_observation_record(first)
            state_path.write_text(
                json.dumps({
                    "schema": "code_mower.prOutcomeObservations.v1",
                    "observations": {
                        "owner/repo#042": record,
                    },
                }),
                encoding="utf-8",
            )
            loaded = load_pr_outcome_observations(state_path)
            self.assertIn("owner/repo#42", loaded)
            self.assertEqual(loaded["owner/repo#42"]["fingerprint"], record["fingerprint"])


class PrOutcomeEvidenceIncompleteContractTests(unittest.TestCase):
    """``evidence_incomplete`` can never accompany full cost coverage."""

    def _complete_event(self) -> dict:
        return build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="42",
            outcome="merged",
            opened_at="2026-09-03T10:00:00Z",
            merged_at="2026-09-03T12:00:00Z",
            run_events=[_builder_run_event("b1", "42", 0.15)],
            created_at="2026-09-03T13:00:00Z",
        )

    def test_historical_v1_event_without_new_dimensions_validates(self) -> None:
        event = self._complete_event()
        # Historical valid v1 events predate the optional dimensions added
        # later; they must keep passing the validator unchanged.
        event["dimensions"].pop("pr_outcome_observation_version", None)
        self.assertNotIn("evidence_incomplete", event["dimensions"])
        self.assertEqual(event["dimensions"]["cost_coverage"], "complete")
        validate_pr_outcome_payload(event)
        validate_cloud_event(event)

    def test_evidence_incomplete_with_complete_coverage_rejected(self) -> None:
        event = self._complete_event()
        event["dimensions"]["evidence_incomplete"] = True
        with self.assertRaises(CloudBundleError):
            validate_pr_outcome_payload(event)
        with self.assertRaises(CloudBundleError):
            validate_cloud_event(event)

    def test_evidence_incomplete_false_with_complete_coverage_validates(
        self,
    ) -> None:
        event = self._complete_event()
        event["dimensions"]["evidence_incomplete"] = False
        validate_pr_outcome_payload(event)
        validate_cloud_event(event)

    def test_evidence_incomplete_event_with_partial_coverage_validates(
        self,
    ) -> None:
        event = build_pr_outcome_event(
            repo_slug="owner/repo",
            pr_number="42",
            outcome="merged",
            opened_at="2026-09-03T10:00:00Z",
            merged_at="2026-09-03T12:00:00Z",
            run_events=[_builder_run_event("b1", "42", 0.15)],
            created_at="2026-09-03T13:00:00Z",
            evidence_incomplete=True,
        )
        self.assertTrue(event["dimensions"]["evidence_incomplete"])
        self.assertEqual(event["dimensions"]["cost_coverage"], "partial")
        self.assertEqual(event["metrics"]["cost_covered_pr_count"], 0)
        validate_pr_outcome_payload(event)
        validate_cloud_event(event)


class PrOutcomeExportSerializationTests(unittest.TestCase):
    """The observation lock must cover bundle creation and payload loading."""

    def _merged_pr(self, number: str) -> dict[str, object]:
        return {
            "number": number,
            "state": "MERGED",
            "createdAt": "2026-09-03T10:00:00Z",
            "mergedAt": "2026-09-03T12:00:00Z",
            "updatedAt": "2026-09-03T13:00:00Z",
        }

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
            return pr_outcomes_upload(
                repo_path=repo_path,
                output_dir=output_dir,
                repo_slug="owner/repo",
                team_id="",
                install_id="",
                source="unit-test",
                limit=10,
                endpoint="https://codemower.example.com/api/upload",
                token_env="CODE_MOWER_TEST_TOKEN",
                yes=False,
                timeout=1.0,
            )

    def _observation_lock_path(self, repo_path: Path) -> Path:
        state_path = (
            repo_path / ".code-mower" / "pr-outcome-observations.json"
        )
        return state_path.with_name(f"{state_path.name}.lock")

    def _assert_lock_held(self, lock_path: Path) -> None:
        # A non-blocking acquire on a second descriptor must fail while the
        # invoking critical section still holds the lock.
        with self.assertRaises(FileLockError):
            with exclusive_file_lock(lock_path, timeout_seconds=0.0):
                pass

    def test_observation_lock_held_during_export_and_payload_load(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            output_dir = repo_path / "bundle"
            lock_path = self._observation_lock_path(repo_path)

            real_build_bundle = cloud_operations.build_cloud_bundle
            real_build_payload = cloud_operations.build_upload_payload
            seen: list[str] = []

            def build_bundle(**kwargs):
                seen.append("bundle")
                self._assert_lock_held(lock_path)
                return real_build_bundle(**kwargs)

            def build_payload(**kwargs):
                seen.append("payload")
                self._assert_lock_held(lock_path)
                return real_build_payload(**kwargs)

            with mock.patch.object(
                cloud_operations,
                "build_cloud_bundle",
                side_effect=build_bundle,
            ), mock.patch.object(
                cloud_operations,
                "build_upload_payload",
                side_effect=build_payload,
            ):
                result = self._run_upload(
                    repo_path, output_dir, [self._merged_pr("7")]
                )

            self.assertEqual(result["status"], "dry_run")
            self.assertEqual(seen, ["bundle", "payload"])

    def test_overlapping_invocation_cannot_contaminate_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            output_dir = repo_path / "bundle"
            real_build_bundle = cloud_operations.build_cloud_bundle
            real_build_payload = cloud_operations.build_upload_payload
            real_lock = cloud_operations.exclusive_file_lock
            nested_attempt: dict[str, object] = {}
            payloads: list[dict] = []
            invoked: list[bool] = []

            def quick_lock(path, **kwargs):
                # Bound the nested invocation's wait so the test cannot hang
                # on the production retry schedule.
                kwargs.setdefault("timeout_seconds", 2.0)
                return real_lock(path, **kwargs)

            def build_bundle(**kwargs):
                result = real_build_bundle(**kwargs)
                if not invoked:
                    invoked.append(True)
                    # Simulate an overlapping second invocation on the same
                    # repo and output directory while this export's bundle is
                    # still on disk.  Under the fixed critical section it must
                    # be blocked by the observation lock; before the fix it
                    # ran to completion and rewrote the manifest out from
                    # under the first invocation.
                    try:
                        nested_attempt["result"] = self._run_upload(
                            repo_path, output_dir, [self._merged_pr("8")]
                        )
                    except CloudBundleError as exc:
                        nested_attempt["error"] = str(exc)
                return result

            def build_payload(**kwargs):
                payload = real_build_payload(**kwargs)
                payloads.append(payload)
                return payload

            with mock.patch.object(
                cloud_operations,
                "exclusive_file_lock",
                side_effect=quick_lock,
            ), mock.patch.object(
                cloud_operations,
                "build_cloud_bundle",
                side_effect=build_bundle,
            ), mock.patch.object(
                cloud_operations,
                "build_upload_payload",
                side_effect=build_payload,
            ):
                result = self._run_upload(
                    repo_path, output_dir, [self._merged_pr("7")]
                )

            self.assertEqual(result["status"], "dry_run")
            # The overlapping invocation was serialized out by the lock
            # rather than allowed to rewrite the shared bundle directory.
            self.assertNotIn("result", nested_attempt)
            self.assertIn("unable to lock", str(nested_attempt.get("error")))
            # The payload this invocation assembled is its own observation,
            # not the overlapping invocation's.
            self.assertEqual(len(payloads), 1)
            pr_numbers = {
                event["dimensions"]["pr_number"]
                for event in payloads[0]["events"]
            }
            self.assertEqual(pr_numbers, {"7"})


if __name__ == "__main__":
    unittest.main()
