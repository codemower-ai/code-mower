"""Offline provider-neutral Board, privacy, restart and exact-head regressions."""
from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from code_mower import board
from code_mower.board_local_observation import (
    LocalEvidenceObservation, LocalWorkObservation, WorkBinding, observe_local_work,
    worktree_identity,
)
from code_mower.board_observation import BoardObservationError, validate
from code_mower.board_remote_observation import (
    RemoteEvidence, RemoteRun, hosted_work_input, remote_work_input,
)
from code_mower.context_contract import ContextError
from code_mower.context_store import ContextStore
from code_mower.controller import CONTROLLER_REPORT_SCHEMA
from code_mower.devin_sessions import DevinClient, Session
from code_mower.remote_session import (
    DevinProvider, FakeProvider, RemoteError, RemoteObservation, RemoteSessions, _key,
    public_projection,
)
from test_devin_work_orders import WorkOrderCase


NOW = datetime(2026, 9, 17, 2, 0, tzinfo=timezone.utc)
HEAD = "a" * 40
OTHER = "b" * 40
CANARY = "PRIVATE_PROMPT_QUESTION_ANSWER_RESULT_PATH_PROVIDER_REFERENCE"


def tree(root):
    return {str(p.relative_to(root)): (p.stat().st_mtime_ns, p.read_bytes())
            for p in root.rglob("*") if p.is_file()}


def resolver(*_args, **_kwargs):
    return {"state": "active", "current": True, "lease": {"state": "active"},
            "session": {"id": "a" * 32, "repo": "owner/repo"}}


def work(root, *, pr=42, head=HEAD):
    return LocalWorkObservation(
        WorkBinding("a" * 32, "work950", "owner/repo", worktree_identity(root), pr, head),
        "issue-907", NOW, assigned_provider="fake",
    )


def render(root, snapshot, now=NOW):
    record = observe_local_work(repository="owner/repo", start=root, snapshot=snapshot,
                                now=now, current_session_resolver=resolver)
    assert record is not None
    return validate(record)


def observation(state="running", *, now=NOW, available=True, reason="none"):
    return RemoteObservation("opaque-generation", "another_provider", public_projection({
        "state": state, "reason": reason, "counts": {"dispatch": 1},
        "messages": CANARY, "structured_output": CANARY, "provider_id": CANARY,
        "prompt": CANARY, "path": CANARY, "cost_usd": 999,
    }), now, now, available)


class BoardRemoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / ".git").mkdir()
        self.work = work(self.root)

    def test_all_provider_neutral_fixtures_validate_and_preserve_facts(self):
        fixtures = json.loads((Path(__file__).parent / "fixtures/board_remote_observations.json").read_text())
        for fixture in fixtures:
            with self.subTest(fixture=fixture["name"]):
                observed = NOW - timedelta(seconds=fixture.get("age_seconds", 0))
                remote = observation(fixture["state"], now=observed,
                                     available=fixture.get("available", True), reason=fixture["reason"])
                remote = replace(remote, checked_at=NOW)
                snapshot = remote_work_input(self.work, round_number=0, runs=(RemoteRun(
                    self.work.binding, 0, remote, reported_stage=fixture.get("reported_stage"),
                ),), now=NOW)
                record = render(self.root, snapshot)
                run = record["work"]["runs"][0]
                self.assertEqual(run["phase"], fixture["phase"])
                self.assertEqual(run["lifecycle"]["state"], fixture["state"])
                self.assertEqual(record["work"]["evidence"]["assignment"]["state"], "assigned")
                self.assertEqual(record["work"]["evidence"]["review"]["state"], "unknown")
                self.assertEqual(record["work"]["evidence"]["merge"]["state"], "unknown")
                if fixture["name"] == "suspended":
                    self.assertIn("provider_suspended", record["work"]["reasons"])
                    self.assertNotIn("provider_failed", record["work"]["reasons"])
                if fixture["name"] in {"stale", "unavailable"}:
                    source = next(s for s in record["sources"] if s["id"] == run["source_id"])
                    self.assertEqual(source["freshness"], fixture["name"])
                    self.assertEqual(source["observed_at"], observed.isoformat().replace("+00:00", "Z"))
                output = json.dumps(record)
                self.assertNotIn(CANARY, output)
                self.assertNotIn("opaque-generation", output)
                self.assertEqual(record["work"]["measurements"]["cost_usd"]["coverage"], "unavailable")

    def test_historical_live_facts_require_closed_reason_and_exact_source_times(self):
        previous = observation(now=NOW - timedelta(minutes=10), available=False)
        snapshot = remote_work_input(self.work, round_number=0,
                                     runs=(RemoteRun(self.work.binding, 0, previous),), now=NOW)
        record = render(self.root, snapshot)
        record["work"]["reasons"] = []
        record["work"]["primary"] = {"actor": "none", "action": "none"}
        with self.assertRaisesRegex(BoardObservationError, "stale_live_claim"):
            validate(record)

    def test_review_requires_fresh_current_github_head_and_exact_round(self):
        current = LocalEvidenceObservation("merge", "open", self.work.binding, NOW)
        review = LocalEvidenceObservation("review", "pass", self.work.binding, NOW, source_kind="review")
        scenarios = [
            (current, RemoteEvidence(1, review), "pass"),
            (None, RemoteEvidence(1, review), "unknown"),
            (replace(current, source_available=False), RemoteEvidence(1, review), "unknown"),
            (replace(current, observed_at=NOW - timedelta(minutes=10)), RemoteEvidence(1, review), "unknown"),
            (current, RemoteEvidence(0, review), "unknown"),
            (current, RemoteEvidence(1, replace(review, binding=replace(review.binding, head_sha=OTHER))), "stale"),
            (current, RemoteEvidence(1, replace(review, observed_at=NOW - timedelta(minutes=10))), "stale"),
        ]
        for pr, evidence, state in scenarios:
            with self.subTest(state=state, pr=pr):
                snapshot = remote_work_input(self.work, round_number=1, runs=(), current_pr=pr,
                                             evidence=(evidence,), now=NOW)
                record = render(self.root, snapshot)
                self.assertEqual(record["work"]["evidence"]["review"]["state"], state)

    def test_round_changes_run_identity_and_refuses_mismatched_binding(self):
        runs = []
        for round_number in (0, 1):
            snapshot = remote_work_input(self.work, round_number=round_number,
                runs=(RemoteRun(self.work.binding, round_number, observation()),), now=NOW)
            runs.append(render(self.root, snapshot)["work"]["runs"][0]["id"])
        self.assertNotEqual(*runs)
        for changed in (replace(self.work.binding, head_sha=OTHER),
                        replace(self.work.binding, work_id="other"),
                        replace(self.work.binding, session_id="b" * 32)):
            with self.assertRaisesRegex(ValueError, "identity_mismatch"):
                remote_work_input(self.work, round_number=0,
                                  runs=(RemoteRun(changed, 0, observation()),), now=NOW)
        with self.assertRaisesRegex(ValueError, "identity_mismatch"):
            remote_work_input(self.work, round_number=1,
                              runs=(RemoteRun(self.work.binding, 0, observation()),), now=NOW)

    def test_controller_assignment_and_done_label_never_become_running_or_pass(self):
        controller = {"schema": CONTROLLER_REPORT_SCHEMA, "repo": "owner/repo",
                      "generated_at": NOW.isoformat(), "decision": {
                          "pr_number": 42, "lane_id": "fake", "decision_state": "ready_to_merge",
                          "reviewer_outcomes": [{"verdict": "PASS"}], "promoted_reviewers_passed": True,
                          "head_sha_prefix": HEAD[:12], "next_detail": CANARY,
                      }}
        snapshot = remote_work_input(replace(self.work, assigned_provider=None), round_number=0,
                                     runs=(), controller=controller, now=NOW)
        record = render(self.root, snapshot)
        self.assertEqual(record["work"]["runs"][0]["phase"], "assigned")
        self.assertEqual(record["work"]["evidence"]["review"]["state"], "unknown")
        self.assertEqual(record["work"]["evidence"]["merge"]["state"], "unknown")
        self.assertNotIn(CANARY, json.dumps(record))

    def test_board_existing_hook_consumes_remote_input_without_writing(self):
        snapshot = remote_work_input(self.work, round_number=0,
                                     runs=(RemoteRun(self.work.binding, 0, observation()),), now=NOW)
        before = tree(self.root)
        def producer(**kwargs):
            return observe_local_work(**kwargs, now=NOW, current_session_resolver=resolver)
        output = board.observations_payload(
            board.BoardConfig(repo="owner/repo", repo_path=str(self.root)),
            local_observation=snapshot, local_observation_producer=producer,
        )
        self.assertEqual(output["produced_records"], 1)
        self.assertEqual(output["records"][0]["work"]["runs"][0]["provider"], "another_provider")
        self.assertEqual(before, tree(self.root))


class ReadOnlyRemoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.provider = FakeProvider(self.root / "provider")
        self.remote = RemoteSessions(self.root / "remote", self.provider)
        self.remote.run("dispatch", "work", prose=CANARY, repo="owner/repo", apply=True)
        self.record = self.remote.store.read_only(_key("work"))
        self.binding = self.record["binding"]

    def observe(self, **kwargs):
        return self.remote.observe("work", repo="owner/repo", **kwargs)

    def test_refresh_is_read_only_and_does_not_consume_result_or_credential_backend(self):
        self.provider.set_state(self.binding, "running", result={"private": CANARY})
        before = tree(self.root)
        with patch.object(self.remote, "run", side_effect=AssertionError("mutation")), \
             patch.object(self.remote, "private_result", side_effect=AssertionError("result")), \
             patch.object(self.provider, "get", side_effect=AssertionError("private result GET")), \
             patch.object(self.provider, "create", side_effect=AssertionError("create")), \
             patch.object(self.provider, "message", side_effect=AssertionError("message")), \
             patch.object(self.provider, "cancel", side_effect=AssertionError("cancel")), \
             patch.object(ContextStore, "locked", side_effect=AssertionError("lock")):
            result = self.observe(now=NOW)
        self.assertTrue(result.available)
        self.assertEqual(result.lifecycle["state"], "running")
        self.assertEqual(before, tree(self.root))
        self.assertNotIn(CANARY, repr(result))
        self.assertNotIn(self.binding, repr(result))

    def test_get_failure_retains_dated_safe_fact_across_restart(self):
        self.provider.set_state(self.binding, "owner_action", reason="waiting_for_owner")
        first = self.observe(now=NOW)
        # A retained allowlisted snapshot is portable across process restarts.
        stored = json.loads(json.dumps(asdict(first), default=lambda value: value.isoformat()))
        for key in ("observed_at", "checked_at"):
            stored[key] = datetime.fromisoformat(stored[key])
        previous = RemoteObservation(**stored)
        self.remote = RemoteSessions(self.root / "remote", FakeProvider(self.root / "provider"))
        later = NOW + timedelta(minutes=1)
        before = tree(self.root)
        with patch.object(self.remote.provider, "observe", side_effect=RuntimeError(CANARY)):
            current = self.observe(previous=previous, now=later)
        self.assertFalse(current.available)
        self.assertEqual(current.lifecycle, first.lifecycle)
        self.assertEqual(current.observed_at, NOW)
        self.assertEqual(current.checked_at, later)
        self.assertNotIn(CANARY, repr(current))
        self.assertEqual(before, tree(self.root))

    def test_unavailable_without_previous_has_no_invented_observation_time(self):
        with patch.object(self.provider, "observe", side_effect=RuntimeError(CANARY)):
            result = self.observe(now=NOW)
        self.assertIsNone(result.observed_at)
        self.assertIsNone(result.lifecycle)
        self.assertFalse(result.available)

    def test_unknown_creation_is_never_reconciled_or_retried(self):
        with self.remote.store.locked(_key("work")) as locked:
            locked.write({**self.record, "binding": None, "state": "uncertain",
                          "reason": "reconcile_dispatch", "checkpoint": {"private": CANARY}})
        before = tree(self.root)
        with patch.object(self.provider, "reconcile", side_effect=AssertionError("reconcile")), \
             patch.object(self.provider, "create", side_effect=AssertionError("create")), \
             patch.object(self.provider, "observe", side_effect=AssertionError("GET")):
            result = self.observe(now=NOW)
        self.assertEqual(result.lifecycle["reason"], "reconcile_dispatch")
        self.assertEqual(before, tree(self.root))

    def test_fix_or_cancel_generation_cannot_retain_prior_result(self):
        previous = self.observe(now=NOW)
        self.remote.run("message", "work", request="fix1", prose=CANARY, apply=True)
        with patch.object(self.provider, "observe", side_effect=RuntimeError(CANARY)):
            current = self.observe(previous=previous, now=NOW + timedelta(seconds=1))
        self.assertNotEqual(current.generation, previous.generation)
        self.assertIsNone(current.lifecycle)

    def test_concurrent_intent_change_refuses_response(self):
        def raced(_binding):
            with self.remote.store.locked(_key("work")) as locked:
                changed = locked.read()
                changed["operations"]["message:fix"] = {"state": "pending"}
                locked.write(changed)
            return Session(self.binding, "complete")
        with patch.object(self.provider, "observe", side_effect=raced):
            with self.assertRaisesRegex(RemoteError, "binding_mismatch"):
                self.observe(now=NOW)

    def test_observer_never_accesses_structured_result_property(self):
        class Metadata:
            session_id = self.binding
            state = "running"
            reason = "none"
            @property
            def structured_output(self):
                raise AssertionError("private result accessed")
        with patch.object(self.provider, "observe", return_value=Metadata()):
            self.assertTrue(self.observe(now=NOW).available)

    def test_read_only_store_missing_symlink_and_permissions(self):
        missing = self.root / "missing"
        self.assertIsNone(ContextStore(missing).read_only("work"))
        self.assertFalse(missing.exists())
        symlink = self.root / "link"
        symlink.symlink_to(self.root / "remote", target_is_directory=True)
        with self.assertRaises(ContextError):
            ContextStore(symlink).read_only(_key("work"))
        path = self.root / "remote" / (_key("work") + ".json")
        path.chmod(0o644)
        with self.assertRaises(ContextError):
            self.observe(now=NOW)

    def test_hosted_devin_uses_get_only_and_never_structured_result_precedence(self):
        calls = []
        def api(method, url, body, _headers):
            calls.append((method, url, body))
            return {"session_id": "devin-observation", "status": "running",
                    "status_detail": "waiting_for_approval", "structured_output": {"secret": CANARY},
                    "messages": [CANARY], "questions": [CANARY], "cost": 88}
        provider = DevinProvider(DevinClient("org-test", "test-key", api_runner=api))
        remote = RemoteSessions(self.root / "devin", provider)
        with remote.store.locked(_key("work")) as locked:
            locked.write({**self.record, "provider": "devin", "account": "org-test",
                          "binding": "devin-observation"})
        before = tree(self.root)
        result = remote.observe("work", repo="owner/repo", now=NOW)
        self.assertTrue(result.available)
        self.assertEqual(result.lifecycle["state"], "waiting_for_approval")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "GET")
        self.assertIsNone(calls[0][2])
        self.assertEqual(before, tree(self.root))
        self.assertNotIn(CANARY, repr(result))


class HostedBoardTests(WorkOrderCase):
    def setUp(self):
        super().setUp()
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        (self.checkout / ".git").mkdir()
        self.run_order("dispatch")

    def test_completed_implementation_pr_review_and_merge_remain_separate(self):
        self.complete()
        self.run_order("collect")
        before = tree(self.root)
        with patch.object(self.remote, "run", side_effect=AssertionError("mutation")), \
             patch.object(self.remote, "private_result", side_effect=AssertionError("result")):
            observed = self.service.observe(self.order, now=NOW)
        self.assertTrue(observed.implementation_verified)
        snapshot = hosted_work_input(work(self.checkout), observed, expected_round=0, now=NOW)
        record = render(self.checkout, snapshot)
        self.assertEqual(record["work"]["runs"][0]["phase"], "implementation_complete")
        self.assertEqual(record["work"]["evidence"]["review"]["state"], "unknown")
        self.assertEqual(record["work"]["evidence"]["merge"]["state"], "open")
        self.assertEqual(before, tree(self.root))
        self.github.pr = replace(self.github.pr, state="merged")
        observed = self.service.observe(self.order, now=NOW)
        record = render(self.checkout, hosted_work_input(work(self.checkout), observed, expected_round=0, now=NOW))
        self.assertEqual(record["work"]["stage"], "merged")

    def test_changed_head_is_verified_independently_of_old_completion_and_review(self):
        self.complete()
        self.run_order("collect")
        old = work(self.checkout)
        review = RemoteEvidence(0, LocalEvidenceObservation("review", "pass", old.binding, NOW, source_kind="review"))
        self.github.pr = replace(self.github.pr, head_sha=OTHER)
        observed = self.service.observe(self.order, now=NOW)
        self.assertFalse(observed.implementation_verified)
        record = render(self.checkout, hosted_work_input(old, observed, expected_round=0, evidence=(review,), now=NOW))
        self.assertEqual(record["work"]["pull_request"]["head_sha"], OTHER)
        self.assertEqual(record["work"]["evidence"]["review"]["state"], "stale")

    def test_github_failure_withholds_cached_current_head_review_and_merge(self):
        old = work(self.checkout)
        review = RemoteEvidence(0, LocalEvidenceObservation("review", "pass", old.binding, NOW, source_kind="review"))
        with patch.object(self.github, "candidates", side_effect=RuntimeError(CANARY)):
            observed = self.service.observe(self.order, now=NOW)
        record = render(self.checkout, hosted_work_input(old, observed, expected_round=0, evidence=(review,), now=NOW))
        self.assertEqual(record["work"]["evidence"]["review"]["state"], "unknown")
        self.assertEqual(record["work"]["evidence"]["merge"]["state"], "unknown")
        self.assertIn("source_unavailable", record["work"]["reasons"])

    def test_crash_before_remote_fix_and_stale_completion_after_restart(self):
        self.complete()
        self.run_order("collect")
        previous = self.service.observe(self.order, now=NOW)
        # Real persisted pre-message crash window: round advanced, remote unchanged.
        with self.service.store.locked(self.key) as locked:
            record = locked.read()
            record.update(round=1, evidence=None, claim=None,
                          message={"pending": True, "request": "fix", "fingerprint": "opaque"})
            locked.write(record)
        from code_mower.devin_work_orders import DevinWorkOrders
        service = DevinWorkOrders(self.root / "builder", self.remote, self.github)
        observed = service.observe(self.order, previous=previous, now=NOW)
        self.assertEqual(observed.round_number, 1)
        self.assertNotEqual(observed.session.lifecycle["state"], "complete")
        with self.assertRaisesRegex(ValueError, "identity_mismatch"):
            hosted_work_input(work(self.checkout), observed, expected_round=0, now=NOW)
        # After the message is acknowledged, an old completed provider status
        # still cannot be promoted into the new round's implementation result.
        with service.store.locked(self.key) as locked:
            record = locked.read()
            record["message"]["pending"] = False
            locked.write(record)
        observed = service.observe(self.order, previous=previous, now=NOW)
        self.assertEqual(observed.session.lifecycle["reason"], "result_not_ready")
        with patch.object(self.provider, "observe", side_effect=RuntimeError(CANARY)):
            unavailable = service.observe(self.order, previous=previous, now=NOW)
        self.assertIsNone(unavailable.session.lifecycle)

    def test_round_change_during_github_read_refuses_whole_observation(self):
        original = self.github.read
        def race(*args):
            with self.service.store.locked(self.key) as locked:
                record = locked.read()
                record["round"] += 1
                locked.write(record)
            return original(*args)
        with patch.object(self.github, "read", side_effect=race):
            with self.assertRaisesRegex(RemoteError, "work_order_binding_mismatch"):
                self.service.observe(self.order, now=NOW)


if __name__ == "__main__":
    unittest.main()
