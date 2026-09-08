#!/usr/bin/env python3
"""Offline tests for the Jira PR sync surface (issue #802).

Every test is deterministic and performs no live network call: Jira HTTP
goes through a small stateful fake honoring Jira's 201-created versus
200-replaced property contract, so "no duplicate comment/link/transition"
is proven from fake state rather than asserted from a report field alone.
Fixtures use synthetic ids and example.atlassian.net only.
"""

from __future__ import annotations

import json
import re
import unittest
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Mapping

from code_mower import jira_mutations, jira_pr_sync


CLOUD_ID = "11111111-2222-3333-4444-555555555555"
SITE_URL = "https://example.atlassian.net"
PROJECT_ID = "10001"
EMAIL = "qa-bot@example.com"
TOKEN = "tok-1"
ISSUE_ID = "10101"
ISSUE_KEY = "ABC-123"
PR_URL = "https://github.com/owner/repo/pull/12"
GLOBAL_ID = "code-mower:github:owner/repo/pull/12"
#: Prose the fake issue carries that must never reach a sync report.
PROSE = "private issue prose must never leave Jira"


def sync_config(
    *,
    transitions: Mapping[str, str] | None = None,
    allowed_operations: tuple[str, ...] = ("transition", "comment", "link"),
) -> dict[str, Any]:
    resolved = (
        {"in_progress": "31", "blocked": "71", "done": "91"}
        if transitions is None
        else dict(transitions)
    )
    return {
        "tracker": {
            "kind": "jira_cloud",
            "jira_cloud": {
                "site_url": SITE_URL,
                "cloud_id": CLOUD_ID,
                "project_id": PROJECT_ID,
                "project_key": "ABC",
                "sync": {"trusted_pr_authors": ["trusted-builder"]},
                "status_category_map": {
                    "in_progress": ["3"],
                    "blocked": ["7"],
                    "done": ["9"],
                },
                "mutations": {
                    "writes_enabled": True,
                    "allowed_operations": list(allowed_operations),
                    "transitions": resolved,
                },
            },
        },
    }


def ok(payload: Any, status: int = 200):
    return (status, {}, json.dumps(payload).encode("utf-8"))


def empty(status: int = 204):
    return (status, {}, b"")


def error(status: int):
    return (status, {}, b"{}")


class FakeJira:
    """Minimal stateful Jira for the endpoints the sync apply touches."""

    def __init__(
        self,
        *,
        status_id: str = "1",
        project_id: str = PROJECT_ID,
        transitions: Any = (
            {"id": "31", "to": {"id": "3"}},
            {"id": "71", "to": {"id": "7"}},
            {"id": "91", "to": {"id": "9"}},
        ),
        faults: Mapping[tuple[str, str], Any] | None = None,
        status_changes_on_issue_reads: Mapping[int, str] | None = None,
        links_on_issue_reads: Mapping[int, list[str]] | None = None,
    ) -> None:
        self.status_id = status_id
        self.project_id = project_id
        self.transitions = [dict(item) for item in transitions]
        self.faults = {key: list(value) for key, value in dict(faults or {}).items()}
        self.status_changes_on_issue_reads = dict(status_changes_on_issue_reads or {})
        self.links_on_issue_reads = dict(links_on_issue_reads or {})
        self.issue_reads = 0
        self.properties: dict[str, Any] = {}
        self.global_ids: list[str] = []
        self.comments: list[Any] = []
        self.calls: list[dict[str, Any]] = []

    def _fault(self, method: str, label: str) -> Any:
        queue = self.faults.get((method, label))
        if not queue:
            return None
        return queue.pop(0) if len(queue) > 1 else queue[0]

    @staticmethod
    def _raise_or_return(item: Any):
        if isinstance(item, BaseException):
            raise item
        return item

    def __call__(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None):
        parts = urllib.parse.urlsplit(url)
        path = "/rest/api/3/" + parts.path.split("/rest/api/3/", 1)[-1]
        parsed_body = json.loads(body.decode("utf-8")) if body else None
        self.calls.append({"method": method, "path": path, "body": parsed_body})
        return self._handle(method, path, parsed_body)

    def _handle(self, method: str, path: str, body: Any):
        match = re.fullmatch(r"^/rest/api/3/issue/(?P<ref>[^/]+)(?P<suffix>/.*)?$", path)
        if match is None:
            raise AssertionError(f"unrouted request: {method} {path}")
        ref = urllib.parse.unquote(match.group("ref"))
        if ref != ISSUE_ID and ref.upper() != ISSUE_KEY.upper():
            return error(404)
        suffix = match.group("suffix") or ""
        if suffix.startswith("/properties/"):
            key = urllib.parse.unquote(suffix[len("/properties/"):])
            fault = self._fault(method, "comment_claim" if key.startswith(
                jira_mutations.COMMENT_CLAIM_PREFIX) else f"properties/{key}")
            if fault is not None:
                return self._raise_or_return(fault)
            if method == "GET":
                if key not in self.properties:
                    return error(404)
                return ok({"key": key, "value": self.properties[key]})
            if method == "PUT":
                created = key not in self.properties
                self.properties[key] = body
                return empty(status=201 if created else 200)
            raise AssertionError(f"unrouted property request: {method} {key}")
        label = suffix.lstrip("/") or "issue"
        fault = self._fault(method, label)
        if fault is not None:
            return self._raise_or_return(fault)
        if suffix == "" and method == "GET":
            self.issue_reads += 1
            self.status_id = self.status_changes_on_issue_reads.get(
                self.issue_reads, self.status_id
            )
            if self.issue_reads in self.links_on_issue_reads:
                self.global_ids = list(self.links_on_issue_reads[self.issue_reads])
            return ok({
                "id": ISSUE_ID,
                "key": ISSUE_KEY,
                "fields": {
                    "summary": PROSE,
                    "description": PROSE,
                    "project": {"id": self.project_id, "key": "ABC"},
                    "status": {"id": self.status_id, "name": "Status"},
                    "issuetype": {"id": "10001", "name": "Task"},
                    "assignee": None,
                },
            })
        if suffix == "/transitions" and method == "GET":
            return ok({"transitions": self.transitions})
        if suffix == "/transitions" and method == "POST":
            chosen = next(
                (item for item in self.transitions
                 if item["id"] == body["transition"]["id"]), None)
            assert chosen is not None, "transition posted without being offered"
            self.status_id = str(chosen.get("to", {}).get("id") or self.status_id)
            return empty()
        if suffix == "/comment" and method == "POST":
            self.comments.append(body)
            return ok({"id": f"2000{len(self.comments)}"}, status=201)
        if suffix == "/remotelink" and method == "GET":
            return ok([{"id": 9, "globalId": gid} for gid in self.global_ids])
        if suffix == "/remotelink" and method == "POST":
            if body["globalId"] not in self.global_ids:
                self.global_ids.append(body["globalId"])
            return ok({"id": 9}, status=201)
        raise AssertionError(f"unrouted request: {method} {path}")

    def comment_posts(self) -> list[dict[str, Any]]:
        return [call for call in self.calls
                if call["method"] == "POST" and call["path"].endswith("/comment")]

    def transition_posts(self) -> list[dict[str, Any]]:
        return [call for call in self.calls
                if call["method"] == "POST" and call["path"].endswith("/transitions")]


def make_client(runner: Any) -> jira_mutations.JiraMutationClient:
    return jira_mutations.JiraMutationClient(
        cloud_id=CLOUD_ID,
        email=EMAIL,
        token=TOKEN,
        site_url=SITE_URL,
        http_runner=runner,
        sleep_fn=lambda _: None,
        random_fn=lambda: 0.0,
        cancelled_fn=lambda: False,
        max_attempts=4,
    )


def plan(config: Mapping[str, Any], milestone: str, **kwargs: Any) -> dict[str, Any]:
    return jira_pr_sync.build_sync_plan(
        config,
        milestone=milestone,
        pr_url=kwargs.pop("pr_url", PR_URL),
        branch=kwargs.pop("branch", "feature/ABC-123-work"),
        pr_author=kwargs.pop("pr_author", "trusted-builder"),
        **kwargs,
    )


def apply_plan(report: Mapping[str, Any], runner: FakeJira) -> dict[str, Any]:
    assert isinstance(report.get("mutation_plan"), Mapping)
    assert report["mutation_plan"].get("mode") == "apply"
    return jira_pr_sync.apply_sync_plan(report, make_client(runner))


def plan_and_apply(config: Mapping[str, Any], milestone: str, runner: FakeJira,
                   **kwargs: Any) -> dict[str, Any]:
    report = plan(config, milestone, apply_requested=True, **kwargs)
    return apply_plan(report, runner)


class MarkerParsingTest(unittest.TestCase):
    def test_branch_marker_is_authoritative(self) -> None:
        parsed = jira_pr_sync.parse_jira_marker(branch="feature/ABC-123-work")
        self.assertEqual(parsed, {"status": "ok", "issue_key": "ABC-123", "reason": "ok"})

    def test_matching_title_prefix_agrees(self) -> None:
        parsed = jira_pr_sync.parse_jira_marker(
            branch="feature/ABC-123-work", pr_title="ABC-123: add widgets")
        self.assertEqual(parsed["status"], "ok")

    def test_title_body_is_never_searched(self) -> None:
        parsed = jira_pr_sync.parse_jira_marker(
            branch="feature/no-marker-here",
            pr_title="Some free text mentioning ABC-123 deep inside",
        )
        self.assertEqual(parsed["status"], "missing")

    def test_two_branch_markers_are_ambiguous(self) -> None:
        parsed = jira_pr_sync.parse_jira_marker(branch="ABC-1-x-DEF-2-y")
        self.assertEqual(parsed["reason"], "ambiguous_jira_identity")

    def test_embedded_branch_marker_is_rejected(self) -> None:
        parsed = jira_pr_sync.parse_jira_marker(branch="feature/notABC-123x-work")
        self.assertEqual(parsed["reason"], "missing_jira_identity")

    def test_oversized_branch_cannot_be_discarded_for_title_marker(self) -> None:
        branch = "feature/ABC-456/" + ("x" * 260)
        parsed = jira_pr_sync.parse_jira_marker(
            branch=branch, pr_title="ABC-123: conflicting marker"
        )
        self.assertEqual(parsed, {"status": "invalid", "reason": "invalid_request"})

    def test_branch_title_disagreement_is_ambiguous(self) -> None:
        parsed = jira_pr_sync.parse_jira_marker(
            branch="feature/ABC-123-work", pr_title="DEF-456: other work")
        self.assertEqual(parsed["reason"], "ambiguous_jira_identity")

    def test_explicit_issue_must_match_marker(self) -> None:
        identity = jira_pr_sync.resolve_sync_identity(
            sync_config(), branch="feature/ABC-123-work", issue_ref="DEF-456")
        self.assertEqual(identity, {"status": "mismatch",
                                    "reason": "jira_identity_mismatch"})

    def test_explicit_issue_does_not_replace_missing_marker(self) -> None:
        identity = jira_pr_sync.resolve_sync_identity(
            sync_config(), branch="feature/no-marker", issue_ref="ABC-123")
        self.assertEqual(identity, {"status": "missing",
                                    "reason": "missing_jira_identity"})

    def test_explicit_issue_does_not_disambiguate_conflicting_markers(self) -> None:
        identity = jira_pr_sync.resolve_sync_identity(
            sync_config(), branch="ABC-123-x-DEF-456", issue_ref="ABC-123")
        self.assertEqual(identity, {"status": "ambiguous",
                                    "reason": "ambiguous_jira_identity"})

    def test_wrong_project_prefix_is_mismatch(self) -> None:
        identity = jira_pr_sync.resolve_sync_identity(
            sync_config(), branch="feature/XYZ-9-work")
        self.assertEqual(identity["reason"], "jira_identity_mismatch")

    def test_untrusted_pr_author_is_refused(self) -> None:
        identity = jira_pr_sync.resolve_sync_identity(
            sync_config(),
            branch="feature/ABC-123-work",
            pr_author="outside-contributor",
        )
        self.assertEqual(identity, {"status": "untrusted",
                                    "reason": "untrusted_pr_author"})


class MilestonePlansTest(unittest.TestCase):
    def test_opened_links_comments_and_transitions(self) -> None:
        report = plan(sync_config(), "opened")
        self.assertEqual(report["status"], "planned")
        self.assertEqual(report["transition_category"], "in_progress")
        self.assertEqual(report["comment_template"], "pr_opened")
        operations = [(item["operation"], item["status"])
                      for item in report["mutation_plan"]["operations"]]
        self.assertEqual(operations, [("transition", "planned"),
                                      ("link", "planned"), ("comment", "planned")])
        self.assertEqual(report["gate_authority"], "github")
        self.assertEqual(report["gate_impact"], "none")

    def test_updated_is_link_only(self) -> None:
        report = plan(sync_config(), "updated")
        operations = [(item["operation"], item["status"])
                      for item in report["mutation_plan"]["operations"]]
        self.assertEqual(operations, [("link", "planned")])
        self.assertEqual(report["comment_template"], "")

    def test_blocked_posts_pr_blocked(self) -> None:
        report = plan(sync_config(), "blocked")
        self.assertEqual(report["transition_category"], "blocked")
        self.assertEqual(report["comment_template"], "pr_blocked")
        self.assertNotIn(
            "No Jira state was changed",
            jira_mutations.COMMENT_TEMPLATES["pr_blocked"],
        )
        self.assertIn(
            "Review and merge decisions remain on GitHub",
            jira_mutations.COMMENT_TEMPLATES["pr_blocked"],
        )

    def test_green_is_silent_link_verify(self) -> None:
        report = plan(sync_config(), "green")
        operations = [(item["operation"], item["status"])
                      for item in report["mutation_plan"]["operations"]]
        self.assertEqual(operations, [("link", "planned")])
        self.assertEqual(report["comment_template"], "")
        self.assertEqual(report["transition_category"], "")

    def test_merged_moves_done_and_comments(self) -> None:
        report = plan(sync_config(), "merged")
        self.assertEqual(report["transition_category"], "done")
        self.assertEqual(report["comment_template"], "pr_merged")

    def test_closed_unmerged_is_link_only(self) -> None:
        report = plan(sync_config(), "closed_unmerged")
        operations = [(item["operation"], item["status"])
                      for item in report["mutation_plan"]["operations"]]
        self.assertEqual(operations, [("link", "planned")])

    def test_unconfigured_transition_is_skipped_not_refused(self) -> None:
        config = sync_config(transitions={"in_progress": "31", "done": "91"})
        report = plan(config, "blocked")
        self.assertEqual(report["transition_category"], "")
        operations = [(item["operation"], item["status"])
                      for item in report["mutation_plan"]["operations"]]
        self.assertEqual(operations, [("link", "planned"), ("comment", "planned")])

    def test_missing_later_status_mapping_refuses_backward_capable_plan(self) -> None:
        config = sync_config()
        del config["tracker"]["jira_cloud"]["status_category_map"]["done"]

        report = plan(config, "opened")

        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["reason"], "stale_guard_unconfigured")
        self.assertIsNone(report["mutation_plan"])
        self.assertEqual(report["write_request_count"], 0)


class FailClosedTest(unittest.TestCase):
    def test_missing_identity_is_owner_action_with_zero_calls(self) -> None:
        report = plan(sync_config(), "opened", branch="feature/no-marker")
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["reason"], "missing_jira_identity")
        self.assertIsNone(report["mutation_plan"])
        self.assertEqual(report["write_request_count"], 0)
        self.assertIn("owner", report["next_action"].lower())

    def test_ambiguous_identity_fails_closed(self) -> None:
        report = plan(sync_config(), "opened", branch="ABC-1-x-DEF-2-y")
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["reason"], "ambiguous_jira_identity")
        self.assertIsNone(report["mutation_plan"])

    def test_mismatched_identity_fails_closed(self) -> None:
        report = plan(sync_config(), "opened", branch="feature/XYZ-9-work")
        self.assertEqual(report["reason"], "jira_identity_mismatch")
        self.assertIsNone(report["mutation_plan"])

    def test_untrusted_pr_author_fails_closed(self) -> None:
        report = plan(sync_config(), "opened", pr_author="outside-contributor")
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["reason"], "untrusted_pr_author")
        self.assertIsNone(report["mutation_plan"])
        self.assertEqual(report["write_request_count"], 0)


class ApplyIdempotencyTest(unittest.TestCase):
    def test_dry_run_apply_helper_performs_no_jira_call(self) -> None:
        runner = FakeJira()
        report = plan(sync_config(), "opened", apply_requested=False)

        returned = jira_pr_sync.apply_sync_plan(report, make_client(runner))

        self.assertEqual(returned, report)
        self.assertEqual(runner.calls, [])

    def test_whole_plan_refusal_performs_no_jira_call(self) -> None:
        config = sync_config(allowed_operations=("link", "comment"))
        report = plan(config, "opened", apply_requested=True)
        runner = FakeJira()

        refused = jira_pr_sync.apply_sync_plan(report, make_client(runner))

        self.assertEqual(refused["status"], "refused")
        self.assertEqual(refused["write_request_count"], 0)
        self.assertEqual(runner.calls, [])

    def test_opened_retry_reports_already_applied_once(self) -> None:
        config, runner = sync_config(), FakeJira()
        first = plan_and_apply(config, "opened", runner)
        self.assertEqual(first["status"], "applied")
        second = plan_and_apply(config, "opened", runner)
        self.assertEqual(second["status"], "already_applied")
        self.assertEqual(len(runner.comment_posts()), 1)
        self.assertEqual(runner.global_ids, [GLOBAL_ID])
        self.assertEqual(len(runner.transition_posts()), 1)

    def test_merged_retry_never_duplicates(self) -> None:
        config, runner = sync_config(), FakeJira()
        plan_and_apply(config, "merged", runner)
        replay = plan_and_apply(config, "merged", runner)
        self.assertEqual(replay["status"], "already_applied")
        self.assertEqual(len(runner.comment_posts()), 1)
        self.assertEqual(len(runner.transition_posts()), 1)
        self.assertEqual(runner.global_ids, [GLOBAL_ID])

    def test_second_pr_cannot_take_over_existing_issue_association(self) -> None:
        config, runner = sync_config(), FakeJira()
        plan_and_apply(config, "opened", runner)
        calls_before = len(runner.calls)
        comments_before = len(runner.comment_posts())
        transitions_before = len(runner.transition_posts())

        conflicting = plan_and_apply(
            config,
            "merged",
            runner,
            pr_url="https://github.com/owner/repo/pull/19",
        )

        self.assertEqual(conflicting["status"], "blocked")
        self.assertEqual(conflicting["reason"], "already_linked_elsewhere")
        self.assertEqual(conflicting["write_request_count"], 0)
        self.assertEqual(runner.status_id, "3")
        self.assertEqual(len(runner.comment_posts()), comments_before)
        self.assertEqual(len(runner.transition_posts()), transitions_before)
        self.assertEqual(runner.global_ids, [GLOBAL_ID])
        new_calls = runner.calls[calls_before:]
        self.assertEqual([call["method"] for call in new_calls], ["GET"])

    def test_unrelated_remote_link_does_not_block_pr_association(self) -> None:
        config, runner = sync_config(), FakeJira()
        runner.global_ids.append("external-system:unrelated")

        report = plan_and_apply(config, "opened", runner)

        self.assertEqual(report["status"], "applied")
        self.assertEqual(runner.global_ids, ["external-system:unrelated", GLOBAL_ID])

    def test_transport_rechecks_association_before_first_write(self) -> None:
        config = sync_config()
        competing = "code-mower:github:owner/repo/pull/19"
        runner = FakeJira(links_on_issue_reads={3: [competing]})

        report = plan_and_apply(config, "opened", runner)

        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["reason"], "already_linked_elsewhere")
        self.assertEqual(report["write_request_count"], 0)
        self.assertEqual(runner.comment_posts(), [])
        self.assertEqual(runner.transition_posts(), [])
        self.assertEqual(runner.global_ids, [competing])

    def test_failed_sync_clears_association_guard_on_reused_client(self) -> None:
        from code_mower import jira_cloud

        config = sync_config()
        runner = FakeJira(
            faults={
                ("GET", "issue"): [jira_cloud.JiraApiError("jira_forbidden"), None]
            }
        )
        client = make_client(runner)
        failed = jira_pr_sync.apply_sync_plan(
            plan(config, "opened", apply_requested=True), client
        )
        self.assertEqual(failed["status"], "blocked")

        runner.global_ids = ["code-mower:github:owner/repo/pull/19"]
        ordinary = jira_mutations.build_mutation_plan(
            config,
            jira_mutations.MutationRequest(
                issue_ref=ISSUE_KEY, transition_category="in_progress"
            ),
            apply_requested=True,
        )
        applied = jira_mutations.apply_mutation_plan(ordinary, client)

        self.assertEqual(applied["status"], "applied")
        self.assertEqual(len(runner.transition_posts()), 1)

    def test_blocked_then_merged_posts_each_template_once(self) -> None:
        config, runner = sync_config(), FakeJira()
        plan_and_apply(config, "blocked", runner)
        replay = plan_and_apply(config, "blocked", runner)
        self.assertEqual(replay["status"], "already_applied")
        self.assertEqual(len(runner.comment_posts()), 1)
        plan_and_apply(config, "merged", runner)
        self.assertEqual(len(runner.comment_posts()), 2)
        self.assertEqual(len(runner.transition_posts()), 2)

    def test_delayed_opened_event_cannot_regress_merged_issue(self) -> None:
        config, runner = sync_config(), FakeJira()
        plan_and_apply(config, "opened", runner)
        plan_and_apply(config, "merged", runner)

        replay = plan_and_apply(config, "opened", runner)

        self.assertEqual(replay["status"], "already_applied")
        self.assertEqual(runner.status_id, "9")
        self.assertEqual(len(runner.comment_posts()), 2)
        self.assertEqual(len(runner.transition_posts()), 2)
        stale = [
            item
            for item in replay["mutation_plan"]["operations"]
            if item["reason"] == "stale_milestone"
        ]
        self.assertEqual(
            [item["operation"] for item in stale], ["transition", "comment"]
        )

    def test_transport_guard_closes_status_change_race(self) -> None:
        config = sync_config()
        runner = FakeJira(status_changes_on_issue_reads={3: "9"})

        report = plan_and_apply(config, "opened", runner)

        self.assertEqual(report["status"], "blocked")
        transition = report["mutation_plan"]["operations"][0]
        self.assertEqual(transition["reason"], "stale_milestone")
        self.assertEqual(report["write_request_count"], 0)
        self.assertEqual(runner.status_id, "9")
        self.assertEqual(runner.comment_posts(), [])
        self.assertEqual(runner.transition_posts(), [])

    def test_jira_outage_never_marks_gate(self) -> None:
        from code_mower import jira_cloud

        config = sync_config()
        runner = FakeJira(faults={("GET", "issue"): [jira_cloud.JiraApiError("jira_unavailable")]})
        report = plan_and_apply(config, "merged", runner)
        self.assertIn(report["status"], ("failed", "blocked"))
        self.assertEqual(report["reason"], "unavailable")
        self.assertEqual(report["gate_authority"], "github")
        self.assertEqual(report["gate_impact"], "none")
        self.assertEqual(len(runner.comment_posts()), 0)
        self.assertEqual(runner.global_ids, [])

    def test_ambiguous_comment_post_is_unverified_not_reposted(self) -> None:
        from code_mower import jira_cloud

        config = sync_config()
        faults = {("POST", "comment"): [jira_cloud.JiraApiError("jira_unavailable")]}
        runner = FakeJira(faults=faults)
        report = plan_and_apply(config, "merged", runner)
        self.assertEqual(report["status"], "unverified")
        replay = plan_and_apply(config, "merged", runner)
        self.assertEqual(replay["status"], "unverified")
        self.assertEqual(len(runner.comment_posts()), 1)


class RecoveryTest(unittest.TestCase):
    def test_reconcile_dedupes_duplicate_webhooks(self) -> None:
        config, runner = sync_config(), FakeJira()
        applied: list[dict[str, Any]] = []

        def apply_fn(report: dict[str, Any]) -> dict[str, Any]:
            result = apply_plan(report, runner)
            applied.append(result)
            return result

        events = [
            {"milestone": "opened", "pr_url": PR_URL,
             "branch": "feature/ABC-123-work", "pr_author": "trusted-builder"},
            {"milestone": "opened", "pr_url": PR_URL,
             "branch": "feature/ABC-123-work", "pr_author": "trusted-builder"},
            {"milestone": "merged", "pr_url": PR_URL,
             "branch": "feature/ABC-123-work", "pr_author": "trusted-builder"},
            {"milestone": "merged", "pr_url": PR_URL,
             "branch": "feature/ABC-123-work", "pr_author": "trusted-builder"},
        ]
        summary = jira_pr_sync.reconcile_missed_events(
            events, config, apply_requested=True, apply_fn=apply_fn)
        self.assertEqual(summary["events_received"], 4)
        self.assertEqual(summary["events_replayed"], 2)
        self.assertEqual(len(applied), 2)
        self.assertEqual(len(runner.comment_posts()), 2)
        self.assertEqual(runner.global_ids, [GLOBAL_ID])
        self.assertEqual(summary["gate_authority"], "github")
        self.assertEqual(summary["gate_impact"], "none")

    def test_reconcile_without_apply_fn_is_dry_run(self) -> None:
        config = sync_config()
        summary = jira_pr_sync.reconcile_missed_events(
            [{"milestone": "green", "pr_url": PR_URL,
              "branch": "feature/ABC-123-work", "pr_author": "trusted-builder"}],
            config,
            apply_requested=False,
        )
        self.assertEqual(summary["events_replayed"], 1)
        self.assertEqual(summary["results"][0]["status"], "planned")
        self.assertEqual(summary["results"][0]["write_request_count"], 0)

    def test_reconcile_rejects_one_pr_mapped_to_multiple_issues(self) -> None:
        config, runner = sync_config(), FakeJira()
        applied: list[dict[str, Any]] = []

        def apply_fn(report: dict[str, Any]) -> dict[str, Any]:
            applied.append(report)
            return apply_plan(report, runner)

        summary = jira_pr_sync.reconcile_missed_events(
            [
                {"milestone": "opened", "pr_url": PR_URL,
                 "branch": "feature/ABC-123-work", "pr_author": "trusted-builder"},
                {"milestone": "merged", "pr_url": PR_URL,
                 "branch": "feature/ABC-456-other", "pr_author": "trusted-builder"},
            ],
            config,
            apply_requested=True,
            apply_fn=apply_fn,
        )

        self.assertEqual(summary["events_replayed"], 0)
        self.assertEqual(summary["events_blocked"], 2)
        self.assertEqual(
            [item["reason"] for item in summary["results"]],
            ["ambiguous_jira_identity", "ambiguous_jira_identity"],
        )
        self.assertEqual(applied, [])
        self.assertEqual(runner.calls, [])

    def test_reconcile_sanitizes_invalid_identity_and_milestone(self) -> None:
        private_input = "private arbitrary payload"
        summary = jira_pr_sync.reconcile_missed_events(
            [
                {"milestone": "opened", "pr_url": PR_URL,
                 "branch": "feature/ABC-123-work", "issue_ref": private_input,
                 "pr_author": "trusted-builder"},
                {"milestone": private_input, "pr_url": PR_URL,
                 "branch": "feature/ABC-123-work", "pr_author": "trusted-builder"},
            ],
            sync_config(),
        )

        self.assertEqual(summary["events_replayed"], 0)
        self.assertEqual(summary["events_blocked"], 2)
        self.assertEqual(
            [item["reason"] for item in summary["results"]],
            ["jira_identity_mismatch", "invalid_milestone"],
        )
        self.assertEqual(summary["results"][1]["milestone"], "")
        self.assertNotIn(private_input, json.dumps(summary))

    def test_reconcile_counts_invalid_pr_url_as_blocked(self) -> None:
        summary = jira_pr_sync.reconcile_missed_events(
            [{"milestone": "opened", "pr_url": "not a pull request",
              "branch": "feature/ABC-123-work", "pr_author": "trusted-builder"}],
            sync_config(),
        )

        self.assertEqual(summary["events_replayed"], 0)
        self.assertEqual(summary["events_blocked"], 1)
        self.assertEqual(summary["results"][0]["reason"], "invalid_pr_url")
        self.assertEqual(summary["results"][0]["issue_key"], "")


class DiscoverLinksTest(unittest.TestCase):
    def test_branch_markers_join_to_explicit_references(self) -> None:
        result = jira_pr_sync.discover_links(
            [
                {"number": 12, "branch": "feature/ABC-123-work", "title": "whatever"},
                {"number": 13, "branch": "feature/no-marker", "title": "plain title"},
                {"number": 14, "branch": "ABC-1-x-DEF-2-y", "title": ""},
                {"number": 15, "branch": "feature/no-marker",
                 "title": "", "body": "mentions ABC-123 in prose"},
            ],
            cloud_id=CLOUD_ID,
            project_id=PROJECT_ID,
            key_to_issue_id={"ABC-123": ISSUE_ID},
        )
        self.assertEqual(result["links"], {(CLOUD_ID, PROJECT_ID, ISSUE_ID): 12})
        reasons = {item["pr_number"]: item["reason"] for item in result["rejected"]}
        # Bodies, descriptions, and comments are never searched: PR 15 stays
        # unlinked even though its body names the key.
        self.assertEqual(reasons["15"], "missing_jira_identity")
        self.assertEqual(reasons["13"], "missing_jira_identity")
        self.assertEqual(reasons["14"], "ambiguous_jira_identity")

    def test_second_pr_for_one_issue_is_rejected(self) -> None:
        result = jira_pr_sync.discover_links(
            [
                {"number": 12, "branch": "feature/ABC-123-work"},
                {"number": 19, "branch": "hotfix/ABC-123-again"},
            ],
            cloud_id=CLOUD_ID,
            project_id=PROJECT_ID,
            key_to_issue_id={"ABC-123": ISSUE_ID},
        )
        self.assertEqual(result["links"], {(CLOUD_ID, PROJECT_ID, ISSUE_ID): 12})
        self.assertEqual(result["rejected"],
                         [{"pr_number": "19", "reason": "already_linked_elsewhere"}])


class MetadataBoundaryTest(unittest.TestCase):
    DENIED = ("description", "transcript", "diff", "stdout", "stderr",
              '"token":', "secret", PROSE, EMAIL, "/tmp/")

    def test_reports_carry_metadata_only(self) -> None:
        config, runner = sync_config(), FakeJira()
        reports = [plan(config, milestone) for milestone in jira_pr_sync.PR_MILESTONES]
        reports.append(plan_and_apply(config, "merged", runner))
        reports.append(plan(config, "opened", branch="feature/no-marker"))
        blob = json.dumps(reports, sort_keys=True)
        for denied in self.DENIED:
            self.assertNotIn(denied, blob)


class CliTest(unittest.TestCase):
    def test_pr_sync_plan_mode_reports_without_network(self) -> None:
        from code_mower import config as code_mower_config

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "code-mower.yml"
            path.write_text(
                "tracker:\n"
                "  kind: jira_cloud\n"
                "  jira_cloud:\n"
                f'    site_url: "{SITE_URL}"\n'
                f'    cloud_id: "{CLOUD_ID}"\n'
                f'    project_id: "{PROJECT_ID}"\n'
                '    project_key: "ABC"\n'
                "    sync:\n"
                "      trusted_pr_authors:\n"
                "        - trusted-builder\n"
                "    status_category_map:\n"
                "      in_progress:\n"
                '        - "3"\n'
                "      blocked:\n"
                '        - "7"\n'
                "      done:\n"
                '        - "9"\n'
                "    mutations:\n"
                "      writes_enabled: true\n"
                "      allowed_operations:\n"
                "        - transition\n"
                "        - comment\n"
                "        - link\n"
                "      transitions:\n"
                '        in_progress: "31"\n',
                encoding="utf-8",
            )
            code_mower_config.load_config(path)  # fail loudly on bad fixture
            profiles = Path(tmp) / "profiles"
            profiles.mkdir()
            out, err = StringIO(), StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = jira_mutations.main(
                    ["pr-sync", str(path), "--milestone", "opened",
                     "--pr-url", PR_URL, "--branch", "feature/ABC-123-work",
                     "--pr-author", "trusted-builder",
                     "--provider-config-dir", str(profiles), "--json"],
                    client_factory=lambda **kwargs: make_client(FakeJira()),
                    env={"JIRA_API_EMAIL": EMAIL, "JIRA_API_TOKEN": TOKEN},
                )
        self.assertEqual(code, 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report["milestone"], "opened")
        self.assertEqual(report["issue_key"], "ABC-123")
        self.assertEqual(report["gate_authority"], "github")


if __name__ == "__main__":
    unittest.main()
