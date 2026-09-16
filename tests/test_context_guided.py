"""Guided session attachment, private handoff, and crash reconciliation."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from code_mower import context_audit, context_guided, context_prepare, context_session
from code_mower import context_graph_connection as graph_connection
from code_mower import context_graph_lifecycle as lifecycle
from code_mower.context_contract import ContextError
from code_mower.context_delivery import deliver, read_binding, retire_attachment, save_feedback
from code_mower.context_packets import _index
from code_mower.context_review import INPUT_HEADER
from code_mower.context_store import ContextStore
import test_context_delivery as fixtures
import test_context_graph_query as graph_fixtures
from test_context_connections import MemoryVault
from test_context_graph_connection import POLICY as GRAPH_POLICY, RECIPIENTS as GRAPH_RECIPIENTS


@unittest.skipUnless(os.name == "posix", "private storage needs POSIX")
class GuidedContextTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ContextDeliveryTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.associations = ContextStore(self.root / "sessions", vault=mock.Mock())
        self.session = {
            "id": "b" * 32,
            "repo": "owner/repo",
            "host": "codex",
            "orchestrator": "codex",
            "participants": [{"id": "claude"}, {"id": "codex"}],
        }
        selected = context_session.create(
            self.associations,
            self.session,
            work_item="EXAMPLE-1",
            policy=self.fixture.spec["policy"],
        )
        self.record = context_session.update(
            self.associations,
            self.session["id"],
            expected_generation=selected["generation"],
            changes={
                "stage": "prepared",
                "builder": "codex",
                "query_mode": "work_item",
                "request_hash": "c" * 64,
                "packet": self.fixture.result["packet_handle"],
                "work_order": ".code-mower/work-orders/example.md",
            },
        )
        self.head = self.fixture.head
        self.comments: list[dict] = []
        self.events: list[str] = []
        self.fail_status: BaseException | None = None
        self.fail_comment: BaseException | None = None

    def _pull(self, *_args, **_kwargs):
        return {"head": {"sha": self.head}}

    def _comments(self, *_args, **_kwargs):
        return list(self.comments)

    def _github(self, method, path, **_kwargs):
        self.events.append(method + " " + path)
        if self.fail_status is not None:
            failure, self.fail_status = self.fail_status, None
            raise failure
        return {}

    def _post(self, _repo, _pr, body, **_kwargs):
        self.events.append("comment")
        self.comments.append({"id": len(self.comments) + 1, "user": {"login": "controller"}, "body": body})
        if self.fail_comment is not None:
            failure, self.fail_comment = self.fail_comment, None
            raise failure
        return {"id": len(self.comments)}

    def patches(self):
        return (
            mock.patch.object(context_guided, "_github_access", return_value=("token", ("controller",))),
            mock.patch.object(context_guided, "fetch_pull_request", side_effect=self._pull),
            mock.patch.object(context_guided, "fetch_issue_comments", side_effect=self._comments),
            mock.patch.object(context_guided, "_gh_request", side_effect=self._github),
            mock.patch.object(context_guided, "post_pr_comment", side_effect=self._post),
        )

    def attach(self, **kwargs):
        with self.patches()[0], self.patches()[1], self.patches()[2], self.patches()[3], self.patches()[4]:
            current = context_session.read(self.associations, self.session["id"])
            return context_guided.attach_session(
                self.associations,
                self.fixture.store,
                current,
                repo_path=self.root,
                pr=42,
                backend=self.fixture.backend,
                **kwargs,
            )

    def test_normal_attach_is_current_head_idempotent_and_public_output_is_redacted(self):
        report, code = self.attach()
        self.assertEqual((report["status"], code), ("attached", 0))
        saved = context_session.read(self.associations, self.session["id"])
        self.assertEqual((saved["stage"], saved["attachment_state"]), ("attached", "published"))
        self.assertEqual(len(self.comments), 1)
        self.assertTrue(self.comments[0]["body"].startswith(INPUT_HEADER + "\n"))
        revision = saved["revision"]
        report, code = self.attach()
        self.assertEqual((report["reused"], report["reconciled"], code), (True, True, 0))
        self.assertEqual(len(self.comments), 1)
        public = json.dumps(report) + self.comments[0]["body"]
        for private in ("EXAMPLE-1", "one@example.invalid", saved["packet"], revision):
            if private == revision:
                self.assertIn(private, self.comments[0]["body"])
            else:
                self.assertNotIn(private, public)

    def test_builder_delivery_and_review_feedback_need_no_private_request_or_revision(self):
        before = context_guided.deliver_session(
            self.associations,
            self.fixture.store,
            self.record,
            repo_path=self.root,
            backend=self.fixture.backend,
        )
        self.assertIn("Private evidence", before)
        self.attach()
        saved = context_session.read(self.associations, self.session["id"])
        current = read_binding(self.fixture.store, saved["revision"])["metadata"]
        review = deliver(
            self.fixture.store,
            saved["revision"],
            repository="owner/repo",
            pr=42,
            head=self.head,
            recipient="claude:reviewer",
            current=current,
            backend=self.fixture.backend,
        )
        save_feedback(self.fixture.store, review, "claude", "Private finding for the fix round.")
        with self.patches()[0], self.patches()[1], self.patches()[2]:
            feedback = context_guided.feedback_session(
                self.associations,
                self.fixture.store,
                saved,
                repo_path=self.root,
                reviewer="claude",
                backend=self.fixture.backend,
            )
        self.assertEqual(feedback, "Private finding for the fix round.")
        self.assertEqual(
            context_session.read(self.associations, self.session["id"])["stage"], "reviewed"
        )

    def test_published_delivery_from_a_non_git_directory_succeeds_for_organization_evidence(self):
        """Organization evidence never depends on a code revision (issue #982)."""
        self.attach()
        saved = context_session.read(self.associations, self.session["id"])
        with self.patches()[0], self.patches()[1], self.patches()[2]:
            evidence = context_guided.deliver_session(
                self.associations, self.fixture.store, saved, repo_path=self.root,
                backend=self.fixture.backend,
            )
        self.assertIn("Private evidence", evidence)
        self.assertEqual(
            context_session.read(self.associations, self.session["id"])["context_state"], "ready",
        )

    def test_published_delivery_still_refuses_a_moved_remote_head(self):
        """The existing bound-packet/current-PR-head validation is untouched (issue #982)."""
        self.attach()
        saved = context_session.read(self.associations, self.session["id"])
        self.head = "d" * 40
        with self.patches()[0], self.patches()[1], self.patches()[2]:
            with self.assertRaises(ContextError):
                context_guided.deliver_session(
                    self.associations, self.fixture.store, saved, repo_path=self.root,
                    backend=self.fixture.backend,
                )
        self.assertEqual(
            context_session.read(self.associations, self.session["id"])["context_state"],
            "unavailable",
        )

    def test_devin_host_builder_and_reviewer_use_the_common_packet(self):
        self.session.update(host="devin", orchestrator="devin",
                            participants=[{"id": "claude"}, {"id": "devin"}])
        selected = context_session.create(
            self.associations, {**self.session, "id": "e" * 32}, work_item="EXAMPLE-1",
            policy=self.fixture.spec["policy"],
        )
        self.session["id"] = "e" * 32
        self.record = context_session.update(
            self.associations, self.session["id"], expected_generation=selected["generation"],
            changes={"stage": "prepared", "builder": "devin", "query_mode": "work_item",
                     "request_hash": "c" * 64, "packet": self.fixture.result["packet_handle"],
                     "work_order": ".code-mower/work-orders/example.md"},
        )
        before = context_guided.deliver_session(
            self.associations, self.fixture.store, self.record, repo_path=self.root,
            backend=self.fixture.backend,
        )
        self.assertIn("Private evidence", before)
        report, code = self.attach()
        self.assertEqual((report["status"], code), ("attached", 0))
        saved = context_session.read(self.associations, self.session["id"])
        current = read_binding(self.fixture.store, saved["revision"])["metadata"]
        texts = {
            deliver(self.fixture.store, saved["revision"], repository="owner/repo", pr=42, head=self.head,
                    recipient=recipient, current=current, backend=self.fixture.backend).text
            for recipient in ("devin:orchestrator", "devin:builder", "devin:reviewer", "claude:reviewer")
        }
        self.assertEqual(len(texts), 1)
        review = deliver(self.fixture.store, saved["revision"], repository="owner/repo", pr=42,
                         head=self.head, recipient="devin:reviewer", current=current,
                         backend=self.fixture.backend)
        save_feedback(self.fixture.store, review, "devin", "Private Devin finding.")
        with self.patches()[0], self.patches()[1], self.patches()[2]:
            with self.assertRaises(ContextError):
                context_guided.feedback_session(
                    self.associations, self.fixture.store, saved, repo_path=self.root,
                    reviewer="devin", backend=self.fixture.backend,
                )
        for private in ("Private Devin finding.", "one@example.invalid", saved["packet"]):
            self.assertNotIn(private, self.comments[0]["body"])

    def test_devin_reviewer_feedback_reaches_a_different_builder(self):
        self.session.update(participants=[{"id": "codex"}, {"id": "devin"}])
        selected = context_session.create(
            self.associations, {**self.session, "id": "f" * 32}, work_item="EXAMPLE-1",
            policy=self.fixture.spec["policy"],
        )
        self.session["id"] = "f" * 32
        context_session.update(
            self.associations, self.session["id"], expected_generation=selected["generation"],
            changes={"stage": "prepared", "builder": "codex", "query_mode": "work_item",
                     "request_hash": "c" * 64, "packet": self.fixture.result["packet_handle"],
                     "work_order": ".code-mower/work-orders/example.md"},
        )
        self.attach()
        saved = context_session.read(self.associations, self.session["id"])
        current = read_binding(self.fixture.store, saved["revision"])["metadata"]
        review = deliver(self.fixture.store, saved["revision"], repository="owner/repo", pr=42,
                         head=self.head, recipient="devin:reviewer", current=current,
                         backend=self.fixture.backend)
        save_feedback(self.fixture.store, review, "devin", "Private Devin finding.")
        with self.patches()[0], self.patches()[1], self.patches()[2]:
            feedback = context_guided.feedback_session(
                self.associations, self.fixture.store, saved, repo_path=self.root,
                reviewer="devin", backend=self.fixture.backend,
            )
        self.assertEqual(feedback, "Private Devin finding.")

    def test_lost_comment_response_reconciles_without_a_duplicate(self):
        self.fail_comment = RuntimeError("response lost")
        report, code = self.attach()
        self.assertEqual((report["status"], code), ("attachment_uncertain", 1))
        saved = context_session.read(self.associations, self.session["id"])
        revision = saved["revision"]
        self.assertFalse(read_binding(self.fixture.store, revision)["published"])
        report, code = self.attach()
        self.assertEqual((report["status"], report["reconciled"], code), ("attached", True, 0))
        self.assertEqual(len(self.comments), 1)
        self.assertTrue(read_binding(self.fixture.store, revision)["published"])

    def test_uncertain_status_failure_needs_explicit_same_revision_retry(self):
        self.fail_status = RuntimeError("status response lost")
        report, code = self.attach()
        self.assertEqual((report["status"], code), ("attachment_uncertain", 1))
        saved = context_session.read(self.associations, self.session["id"])
        revision = saved["revision"]
        report, code = self.attach()
        self.assertEqual((report["status"], code), ("attachment_uncertain", 1))
        self.assertEqual(len(self.comments), 0)
        report, code = self.attach(retry_uncertain=True)
        self.assertEqual((report["status"], code), ("attached", 0))
        self.assertEqual(context_session.read(self.associations, self.session["id"])["revision"], revision)

    def test_process_crash_after_comment_resumes_the_pending_intent(self):
        self.fail_comment = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.attach()
        saved = context_session.read(self.associations, self.session["id"])
        self.assertEqual(saved["attachment_state"], "pending")
        revision = saved["revision"]
        report, code = self.attach()
        self.assertEqual((report["reconciled"], code), (True, 0))
        self.assertEqual(context_session.read(self.associations, self.session["id"])["revision"], revision)
        self.assertEqual(len(self.comments), 1)

    def test_posted_pending_intent_on_an_old_head_is_retired_before_replacement(self):
        self.fail_comment = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.attach()
        old = context_session.read(self.associations, self.session["id"])
        old_revision = old["revision"]
        self.head = "d" * 40
        report, code = self.attach()
        self.assertEqual((report["status"], code), ("attached", 0))
        saved = context_session.read(self.associations, self.session["id"])
        self.assertNotEqual(saved["revision"], old_revision)
        with self.assertRaises(ContextError):
            read_binding(self.fixture.store, old_revision)
        self.assertEqual(len(self.comments), 2)

    def test_process_crash_after_status_reuses_the_pending_revision(self):
        self.fail_status = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.attach()
        saved = context_session.read(self.associations, self.session["id"])
        revision = saved["revision"]
        self.assertEqual(saved["attachment_state"], "pending")
        self.assertEqual(self.comments, [])
        report, code = self.attach()
        self.assertEqual((report["status"], code), ("attached", 0))
        self.assertEqual(context_session.read(self.associations, self.session["id"])["revision"], revision)

    def test_process_crashes_around_local_reservation_and_publish_mark_resume(self):
        # A crash inside ``reserve_attachment`` itself leaves the durable
        # ``reserving`` marker behind with no binding ever created -- a
        # process stop before reservation creates any binding
        # (codex:1a4bc7b34687da726908). A later attach must recognize that
        # state, safely finish cleanup, and only then let a fresh attach
        # succeed; it must never silently resume toward publication in the
        # same call that recognized the stale marker.
        with self.patches()[0], self.patches()[1], self.patches()[2], self.patches()[3], self.patches()[4], \
                mock.patch.object(context_guided, "reserve_attachment", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                context_guided.attach_session(
                    self.associations,
                    self.fixture.store,
                    self.record,
                    repo_path=self.root,
                    pr=42,
                    backend=self.fixture.backend,
                )
        reserving = context_session.read(self.associations, self.session["id"])
        self.assertEqual(reserving["attachment_state"], "reserving")
        self.assertIsNotNone(reserving["revision"])

        with self.assertRaisesRegex(ContextError, "rerun attach"):
            self.attach()
        recovered = context_session.read(self.associations, self.session["id"])
        self.assertEqual(
            (recovered["attachment_state"], recovered["pr"], recovered["head"], recovered["revision"]),
            ("none", None, None, None),
        )
        self.assertEqual(self.comments, [])

        report, code = self.attach()
        self.assertEqual((report["status"], code), ("attached", 0))

        # Move to a new head so the same session creates another intent. GitHub
        # accepts its comment and the binding is enabled, then the process stops
        # before the association can record completion.
        self.head = "d" * 40
        old_revision = context_session.read(self.associations, self.session["id"])["revision"]
        original_mark = context_guided.mark_published

        def mark_then_crash(*args, **kwargs):
            original_mark(*args, **kwargs)
            raise KeyboardInterrupt()

        with self.patches()[0], self.patches()[1], self.patches()[2], self.patches()[3], self.patches()[4], \
                mock.patch.object(context_guided, "mark_published", side_effect=mark_then_crash):
            current = context_session.read(self.associations, self.session["id"])
            with self.assertRaises(KeyboardInterrupt):
                context_guided.attach_session(
                    self.associations,
                    self.fixture.store,
                    current,
                    repo_path=self.root,
                    pr=42,
                    backend=self.fixture.backend,
                )
        pending = context_session.read(self.associations, self.session["id"])
        self.assertEqual(pending["attachment_state"], "pending")
        self.assertTrue(read_binding(self.fixture.store, pending["revision"])["published"])
        with self.assertRaises(ContextError):
            read_binding(self.fixture.store, old_revision)
        report, code = self.attach()
        self.assertEqual((report["reconciled"], code), (True, 0))

    def test_head_change_before_first_write_discards_only_the_unpublished_intent(self):
        pulls = iter((self.head, "d" * 40))
        with self.patches()[0], mock.patch.object(
            context_guided,
            "fetch_pull_request",
            side_effect=lambda *_a, **_k: {"head": {"sha": next(pulls)}},
        ), self.patches()[2], self.patches()[3], self.patches()[4]:
            with self.assertRaisesRegex(Exception, "head changed"):
                context_guided.attach_session(
                    self.associations,
                    self.fixture.store,
                    self.record,
                    repo_path=self.root,
                    pr=42,
                    backend=self.fixture.backend,
                )
        saved = context_session.read(self.associations, self.session["id"])
        self.assertEqual((saved["attachment_state"], saved["revision"]), ("none", None))
        self.assertEqual(self.comments, [])

    def test_status_closes_pending_and_uncertain_recovery_states(self):
        self.fail_status = RuntimeError("status response lost")
        self.attach()
        saved = context_session.read(self.associations, self.session["id"])
        status = context_session.status(saved, lease_live=True)
        self.assertEqual(status["stage"], "attachment_uncertain")
        self.assertTrue(status["owner_action"])
        encoded = json.dumps(status)
        for private in (saved["revision"], saved["packet"], "EXAMPLE-1", "owner/repo"):
            self.assertNotIn(private, encoded)

    def test_stale_pending_intent_retires_before_reauthorizing_its_own_evidence(self):
        """When the trusted remote head has moved beyond a saved pending
        intent, the old identity must be retired before the state machine
        ever calls ``reserve_attachment`` again for that stale evidence
        (codex:1a4bc7b34687da726908)."""
        self.fail_comment = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.attach()
        pending = context_session.read(self.associations, self.session["id"])
        old_revision = pending["revision"]
        old_head = pending["head"]
        self.head = "d" * 40

        real_reserve = context_guided.reserve_attachment

        def spying_reserve(*args, **kwargs):
            if kwargs.get("head") == old_head:
                raise AssertionError("stale evidence was reauthorized before retirement")
            return real_reserve(*args, **kwargs)

        with (
            self.patches()[0], self.patches()[1], self.patches()[2],
            self.patches()[3], self.patches()[4],
            mock.patch.object(context_guided, "reserve_attachment", side_effect=spying_reserve),
        ):
            current = context_session.read(self.associations, self.session["id"])
            report, code = context_guided.attach_session(
                self.associations, self.fixture.store, current, repo_path=self.root, pr=42,
                backend=self.fixture.backend,
            )
        self.assertEqual((report["status"], code), ("attached", 0))
        with self.assertRaises(ContextError):
            read_binding(self.fixture.store, old_revision)
        self.assertNotEqual(
            context_session.read(self.associations, self.session["id"])["revision"], old_revision,
        )

    def test_interrupted_association_update_recovers_after_stale_retirement(self):
        """A crash between retiring a stale published binding and updating
        the session association must be retry-safe: the next call finishes
        the same identity-checked cleanup rather than reading the now-missing
        binding as current evidence (codex:1a4bc7b34687da726908)."""
        self.attach()
        published = context_session.read(self.associations, self.session["id"])
        old_revision = published["revision"]
        retire_attachment(
            self.fixture.store, published["connection"], published["packet"], old_revision,
        )
        self.head = "d" * 40
        report, code = self.attach()
        self.assertEqual((report["status"], code), ("attached", 0))
        saved = context_session.read(self.associations, self.session["id"])
        self.assertNotEqual(saved["revision"], old_revision)
        with self.assertRaises(ContextError):
            read_binding(self.fixture.store, old_revision)
        self.assertEqual(len(self.comments), 2)

    def test_interrupted_index_write_during_retirement_completes_on_retry(self):
        """A crash between the index write and the artifact delete inside
        delivery cleanup must complete on the next attach rather than
        report an inconsistent index (codex:1a4bc7b34687da726908)."""
        self.attach()
        published = context_session.read(self.associations, self.session["id"])
        old_revision = published["revision"]
        with self.fixture.store.locked(published["connection"]) as locked:
            index_file, index = _index(locked)
            entry = next(item for item in index["entries"] if item["handle"] == published["packet"])
            entry["deliveries"].remove(old_revision)
            index_file.write(index)
        self.head = "d" * 40
        report, code = self.attach()
        self.assertEqual((report["status"], code), ("attached", 0))
        saved = context_session.read(self.associations, self.session["id"])
        self.assertNotEqual(saved["revision"], old_revision)
        with self.assertRaises(ContextError):
            read_binding(self.fixture.store, old_revision)

    def test_malformed_remote_head_cannot_retire_or_clear_a_published_binding(self):
        """Missing, null, wrong-type, shortened, and non-hex ``head.sha``
        values are not authoritative head movement and must never retire or
        clear an existing binding or session (codex:1a4bc7b34687da726908)."""
        self.attach()
        published = context_session.read(self.associations, self.session["id"])
        missing = object()
        for malformed in (missing, None, 123, ["d" * 40], "d" * 39, "d" * 41, "g" * 40, "D" * 40):
            with self.subTest(head=malformed):
                def _pull(*_a, _sha=malformed, **_k):
                    return {"head": {}} if _sha is missing else {"head": {"sha": _sha}}
                with self.patches()[0], mock.patch.object(
                    context_guided, "fetch_pull_request", side_effect=_pull,
                ), self.patches()[2], self.patches()[3], self.patches()[4]:
                    with self.assertRaises(ContextError):
                        context_guided.attach_session(
                            self.associations, self.fixture.store, published, repo_path=self.root,
                            pr=42, backend=self.fixture.backend,
                        )
        unchanged = context_session.read(self.associations, self.session["id"])
        self.assertEqual(unchanged, published)
        self.assertTrue(read_binding(self.fixture.store, published["revision"])["published"])

    def test_malformed_remote_head_cannot_disturb_a_pending_intent(self):
        self.fail_comment = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.attach()
        pending = context_session.read(self.associations, self.session["id"])
        with self.patches()[0], mock.patch.object(
            context_guided, "fetch_pull_request", side_effect=lambda *_a, **_k: {"head": {"sha": None}},
        ), self.patches()[2], self.patches()[3], self.patches()[4]:
            with self.assertRaises(ContextError):
                context_guided.attach_session(
                    self.associations, self.fixture.store, pending, repo_path=self.root,
                    pr=42, backend=self.fixture.backend,
                )
        unchanged = context_session.read(self.associations, self.session["id"])
        self.assertEqual(unchanged, pending)

    def test_concurrent_reconciliation_is_not_overwritten_by_the_rollback(self):
        """If another operation already reconciled the exact freshly minted
        intent before the rollback runs, the rollback must not overwrite that
        result or claim it cleared anything (codex:1a4bc7b34687da726908)."""
        real_record_failure = context_session.record_failure

        def racing_record_failure(store, record, error):
            updated = real_record_failure(store, record, error)
            # A concurrent operation reconciles the exact same intent first.
            return context_session.update(
                store, record["session_id"], expected_generation=updated["generation"],
                changes={
                    "stage": "prepared", "pr": None, "head": None, "revision": None,
                    "attachment_state": "none",
                },
            )

        with (
            self.patches()[0], self.patches()[1], self.patches()[2],
            self.patches()[3], self.patches()[4],
            mock.patch.object(context_session, "record_failure", side_effect=racing_record_failure),
            mock.patch.object(context_guided, "reserve_attachment", side_effect=ContextError("boom")),
        ):
            with self.assertRaisesRegex(ContextError, "boom"):
                context_guided.attach_session(
                    self.associations, self.fixture.store, self.record, repo_path=self.root,
                    pr=42, backend=self.fixture.backend,
                )
        after = context_session.read(self.associations, self.session["id"])
        self.assertEqual(
            (after["attachment_state"], after["pr"], after["head"], after["revision"]),
            ("none", None, None, None),
        )

    def test_rollback_failure_retains_the_reserving_intent_rather_than_claiming_success(self):
        """If the abandon step itself fails, the saved durable pre-publication
        (``reserving``) intent is left in place rather than reported as
        cleared -- a ``ContextError`` there does not prove no local write
        occurred (codex:1a4bc7b34687da726908)."""
        with (
            self.patches()[0], self.patches()[1], self.patches()[2],
            self.patches()[3], self.patches()[4],
            mock.patch.object(context_guided, "reserve_attachment", side_effect=ContextError("boom")),
            mock.patch.object(context_guided, "abandon_attachment", side_effect=ContextError("cleanup failed")),
        ):
            with self.assertRaisesRegex(ContextError, "boom"):
                context_guided.attach_session(
                    self.associations, self.fixture.store, self.record, repo_path=self.root,
                    pr=42, backend=self.fixture.backend,
                )
        reserving = context_session.read(self.associations, self.session["id"])
        self.assertEqual(reserving["attachment_state"], "reserving")
        self.assertIsNotNone(reserving["revision"])

    def test_interrupted_association_clear_after_reservation_recovers_on_retry(self):
        """A crash between abandoning an unpublished binding and clearing the
        session association leaves the durable ``reserving`` marker behind,
        with the reservation it once named already gone. The next attach
        recognizes that state, finishes cleanup idempotently, and only then
        permits an explicit refresh and a successful reattachment
        (codex:1a4bc7b34687da726908)."""
        revision = uuid.uuid4().hex
        reserving = context_session.update(
            self.associations, self.session["id"], expected_generation=self.record["generation"],
            changes={
                "stage": "prepared", "pr": 42, "head": self.head, "revision": revision,
                "attachment_state": "reserving",
            },
        )
        context_guided.reserve_attachment(
            self.fixture.store, reserving["connection"], reserving["packet"], reserving["policy"],
            context_guided.ContextRequest(reserving["repo"], reserving["work_item"], "codex:orchestrator"),
            pr=42, head=self.head, revision=revision, backend=self.fixture.backend,
        )
        # The reservation genuinely exists on disk; a crash then lands
        # between the two writes ``_abandon_and_clear`` itself makes.
        context_guided.abandon_attachment(
            self.fixture.store, reserving["connection"], reserving["packet"], revision,
        )
        with self.assertRaisesRegex(ContextError, "rerun attach"):
            self.attach()
        recovered = context_session.read(self.associations, self.session["id"])
        self.assertEqual(
            (recovered["attachment_state"], recovered["pr"], recovered["head"], recovered["revision"]),
            ("none", None, None, None),
        )
        self.assertEqual(self.comments, [])
        with self.assertRaises(ContextError):
            read_binding(self.fixture.store, revision)
        report, code = self.attach()
        self.assertEqual((report["status"], code), ("attached", 0))

    def test_concurrent_failure_after_cleanup_read_preserves_the_diagnostic(self):
        """A ``record_failure`` that lands after ``_abandon_and_clear`` reads
        the association but before it clears it -- bumping the generation
        while keeping the same revision -- must not be lost: the same
        revision still clears safely on a later attach while the latest
        failure diagnostic survives (codex:1a4bc7b34687da726908)."""
        calls = {"n": 0}
        real_read = context_session.read

        def racing_read(store, session_id):
            calls["n"] += 1
            current = real_read(store, session_id)
            if calls["n"] == 2:
                context_session.update(
                    store, session_id, expected_generation=current["generation"],
                    changes={"context_state": "authorization_failed"},
                )
            return current

        with (
            self.patches()[0], self.patches()[1], self.patches()[2],
            self.patches()[3], self.patches()[4],
            mock.patch.object(context_guided, "reserve_attachment", side_effect=ContextError("boom")),
            mock.patch.object(context_session, "read", side_effect=racing_read),
        ):
            with self.assertRaisesRegex(ContextError, "boom"):
                context_guided.attach_session(
                    self.associations, self.fixture.store, self.record, repo_path=self.root,
                    pr=42, backend=self.fixture.backend,
                )
        stranded = context_session.read(self.associations, self.session["id"])
        self.assertEqual(stranded["attachment_state"], "reserving")
        self.assertEqual(stranded["context_state"], "authorization_failed")
        revision = stranded["revision"]

        with self.assertRaisesRegex(ContextError, "rerun attach"):
            self.attach()
        recovered = context_session.read(self.associations, self.session["id"])
        self.assertEqual(
            (recovered["attachment_state"], recovered["pr"], recovered["head"], recovered["revision"]),
            ("none", None, None, None),
        )
        # The failure diagnostic recorded during the race is preserved
        # rather than silently erased by the eventual cleanup.
        self.assertEqual(recovered["context_state"], "authorization_failed")
        with self.assertRaises(ContextError):
            read_binding(self.fixture.store, revision)

    def test_abandon_and_clear_never_touches_a_different_or_published_revision(self):
        """The identity check the reserving cleanup relies on must never
        clear a different, replaced, or already-published revision, even
        under a stale generation (codex:1a4bc7b34687da726908)."""
        report, code = self.attach()
        self.assertEqual((report["status"], code), ("attached", 0))
        published = context_session.read(self.associations, self.session["id"])
        stale = {**published, "revision": "0" * 32}
        context_guided._abandon_and_clear(self.associations, self.fixture.store, stale)
        unchanged = context_session.read(self.associations, self.session["id"])
        self.assertEqual(unchanged, published)

    def test_interrupted_pending_transition_recovers_without_public_writes(self):
        """A crash after a fresh reservation succeeds but before its session
        association durably transitions to ``pending`` -- still strictly
        before any GitHub write -- must recover on retry by abandoning the
        unpublished binding and restoring prepared/none, with zero public
        writes, rather than resuming toward publication in that same
        recovery (codex:1a4bc7b34687da726908)."""
        real_update = context_session.update

        def failing_transition(store, session_id, *, expected_generation, changes):
            if changes == {"attachment_state": "pending"}:
                raise OSError("disk full")
            return real_update(store, session_id, expected_generation=expected_generation, changes=changes)

        with mock.patch.object(context_session, "update", side_effect=failing_transition):
            # The mocked ``OSError`` never reaches the store boundary that
            # would normally raise it, but it still propagates out through
            # the surrounding ``association_store.locked`` block that
            # ``attach_session`` holds for its whole body, so it is normalized
            # to the same fail-closed public ``ContextError`` a genuine
            # storage fault there would produce.
            with self.assertRaisesRegex(ContextError, "private context store is unavailable or unsafe"):
                self.attach()
        reserving = context_session.read(self.associations, self.session["id"])
        self.assertEqual(reserving["attachment_state"], "reserving")
        revision = reserving["revision"]
        # The reservation itself genuinely completed before the interrupted
        # transition -- a real, unpublished binding exists.
        self.assertFalse(read_binding(self.fixture.store, revision)["published"])
        self.assertEqual(self.comments, [])
        self.assertEqual(self.events, [])

        with self.assertRaisesRegex(ContextError, "rerun attach"):
            self.attach()
        recovered = context_session.read(self.associations, self.session["id"])
        self.assertEqual(
            (recovered["attachment_state"], recovered["pr"], recovered["head"], recovered["revision"]),
            ("none", None, None, None),
        )
        self.assertEqual(self.comments, [])
        self.assertEqual(self.events, [])
        with self.assertRaises(ContextError):
            read_binding(self.fixture.store, revision)

        report, code = self.attach()
        self.assertEqual((report["status"], code), ("attached", 0))

    def test_provider_free_cross_process_qualification_is_symmetric_for_both_hosts(self):
        delivered = []

        def session_store():
            return ContextStore(self.root / "sessions", vault=mock.Mock())

        def context_store():
            return ContextStore(self.fixture.store.root, vault=self.fixture.store._vault)

        for index, (host, reviewer) in enumerate((("codex", "claude"), ("claude", "codex"))):
            with self.subTest(host=host):
                session_id = format(index + 12, "032x")
                associations = session_store()
                selected = context_session.create(
                    associations,
                    {
                        "id": session_id,
                        "repo": "owner/repo",
                        "host": host,
                        "orchestrator": host,
                        "participants": [{"id": "claude"}, {"id": "codex"}],
                    },
                    work_item="EXAMPLE-1",
                    policy=self.fixture.spec["policy"],
                )
                prepared = context_session.update(
                    associations,
                    session_id,
                    expected_generation=selected["generation"],
                    changes={
                        "stage": "prepared", "builder": host, "query_mode": "work_item",
                        "request_hash": "f" * 64,
                        "packet": self.fixture.result["packet_handle"],
                        "work_order": f".code-mower/work-orders/{host}.md",
                    },
                )
                # A new store object for every phase models separate CLI
                # processes while retaining the same protected on-disk state.
                evidence = context_guided.deliver_session(
                    session_store(),
                    context_store(),
                    prepared,
                    repo_path=self.root,
                    backend=self.fixture.backend,
                )
                self.assertIn("Private evidence", evidence)
                delivered.append(evidence)
                self.comments = []
                with self.patches()[0], self.patches()[1], self.patches()[2], self.patches()[3], self.patches()[4]:
                    report, code = context_guided.attach_session(
                        session_store(),
                        context_store(),
                        context_session.read(session_store(), session_id),
                        repo_path=self.root,
                        pr=42 + index,
                        backend=self.fixture.backend,
                    )
                self.assertEqual((report["status"], code), ("attached", 0))
                attached = context_session.read(session_store(), session_id)
                review = context_audit.prepare(
                    repository="owner/repo",
                    pr=42 + index,
                    head=self.head,
                    host=reviewer,
                    authorities=("controller",),
                    fetch_comments=lambda: list(self.comments),
                    store=context_store(),
                    backend=self.fixture.backend,
                )
                self.assertTrue(review.ready)
                self.assertTrue(
                    review.finish(head=self.head, prose=f"Private {reviewer} finding for {host}.")
                )
                with self.patches()[0], self.patches()[1], self.patches()[2]:
                    feedback = context_guided.feedback_session(
                        session_store(),
                        context_store(),
                        attached,
                        repo_path=self.root,
                        reviewer=reviewer,
                        backend=self.fixture.backend,
                    )
                self.assertEqual(feedback, f"Private {reviewer} finding for {host}.")
                public = json.dumps(report) + "".join(item["body"] for item in self.comments)
                for private in ("EXAMPLE-1", "one@example.invalid", attached["packet"]):
                    self.assertNotIn(private, public)
        self.assertEqual(len(set(delivered)), 1)
        self.assertEqual(self.fixture.backend.searches, 1)


@unittest.skipUnless(os.name == "posix", "private storage needs POSIX")
class GuidedRepositoryDeliveryTests(unittest.TestCase):
    """Published repository evidence must match the actual consuming checkout (issue #982).

    Unlike ``GuidedContextTests``, the connection here is a real local
    repository graph over a real throwaway Git checkout, so a mismatch
    between the checkout doing the work and the bound PR revision is an
    actual divergence between two resolvable commits, not a mocked value.
    """

    SESSION_ID = "a" * 32

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repository = graph_fixtures.make_repository(self.root)
        self.private = self.root / "private"
        self.private.mkdir(mode=0o700)
        self.manifest = lifecycle.build_graph(
            self.repository, pin=graph_fixtures.PIN,
            indexer=graph_fixtures.indexer(graph_fixtures.graph_document()), root=self.private,
        )
        self.store = ContextStore(self.private, vault=MemoryVault())
        self.associations = context_session.association_store(self.private)
        graph_connection.connect(self.store, "local-graph", {
            "repository_root": str(self.repository),
            "repositories": ["owner/repo"],
            "recipients": GRAPH_RECIPIENTS,
        })
        selected = context_session.create(
            self.associations,
            {
                "id": self.SESSION_ID, "repo": "owner/repo", "host": "codex",
                "orchestrator": "codex", "participants": [{"id": "claude"}, {"id": "codex"}],
            },
            work_item="WORK-1",
            policy=GRAPH_POLICY,
        )
        report, code = context_prepare.prepare(
            self.associations, selected, repo_root=self.repository, context_root=self.private,
            packet_store=self.store, query="parse_config", source="impact", builder="codex",
        )
        self.assertEqual((code, report["status"]), (0, "prepared"))
        self.record = context_session.read(self.associations, self.SESSION_ID)
        self.head = self.manifest.commit
        self.comments: list[dict] = []

    def _pull(self, *_args, **_kwargs):
        return {"head": {"sha": self.head}}

    def _comments(self, *_args, **_kwargs):
        return list(self.comments)

    def _github(self, _method, _path, **_kwargs):
        return {}

    def _post(self, _repo, _pr, body, **_kwargs):
        self.comments.append(
            {"id": len(self.comments) + 1, "user": {"login": "controller"}, "body": body}
        )
        return {"id": len(self.comments)}

    def patches(self):
        return (
            mock.patch.object(
                context_guided, "_github_access", return_value=("token", ("controller",)),
            ),
            mock.patch.object(context_guided, "fetch_pull_request", side_effect=self._pull),
            mock.patch.object(context_guided, "fetch_issue_comments", side_effect=self._comments),
            mock.patch.object(context_guided, "_gh_request", side_effect=self._github),
            mock.patch.object(context_guided, "post_pr_comment", side_effect=self._post),
        )

    def attach(self, pr=1):
        with (
            self.patches()[0], self.patches()[1], self.patches()[2],
            self.patches()[3], self.patches()[4],
        ):
            current = context_session.read(self.associations, self.SESSION_ID)
            return context_guided.attach_session(
                self.associations, self.store, current, repo_path=self.repository,
                pr=pr, backend=None,
            )

    def _advance_the_checkout(self):
        """Commit new work so the checkout's HEAD moves off the graph's commit."""
        (self.repository / "example_pkg" / "config.py").write_text("changed\n", encoding="utf-8")
        graph_fixtures.git(self.repository, "add", ".")
        graph_fixtures.git(self.repository, "commit", "-q", "-m", "second")

    def _advance_without_disturbing_citations(self):
        """Move HEAD to a new commit without touching any file the fixture's
        synthetic graph cites, so a graph rebuilt at the new commit still
        validates the same citations."""
        (self.repository / "NOTES.md").write_text("advance\n", encoding="utf-8")
        graph_fixtures.git(self.repository, "add", ".")
        graph_fixtures.git(self.repository, "commit", "-q", "-m", "advance")

    def test_repository_delivery_matches_the_actual_consuming_checkout(self):
        report, code = self.attach()
        self.assertEqual((report["status"], code), ("attached", 0))
        published = context_session.read(self.associations, self.SESSION_ID)
        with self.patches()[0], self.patches()[1], self.patches()[2]:
            evidence = context_guided.deliver_session(
                self.associations, self.store, published, repo_path=self.repository, backend=None,
            )
        self.assertIn("Private evidence", evidence)
        self.assertIn("example_pkg/config.py#L12", evidence)
        self.assertEqual(
            context_session.read(self.associations, self.SESSION_ID)["context_state"], "ready",
        )

    def test_published_delivery_refuses_when_the_actual_checkout_has_moved(self):
        self.attach()
        published = context_session.read(self.associations, self.SESSION_ID)
        # The remote PR head the stub reports is untouched; only the actual
        # consuming checkout moves, which is exactly the divergence issue
        # #982 describes.
        self._advance_the_checkout()
        with self.patches()[0], self.patches()[1], self.patches()[2]:
            with self.assertRaises(ContextError):
                context_guided.deliver_session(
                    self.associations, self.store, published,
                    repo_path=self.repository, backend=None,
                )
        after = context_session.read(self.associations, self.SESSION_ID)
        self.assertEqual(after["context_state"], "unavailable")
        self.assertEqual(after["stage"], "attached")

    def test_published_repository_delivery_from_a_non_git_directory_fails_closed(self):
        self.attach()
        published = context_session.read(self.associations, self.SESSION_ID)
        outside = self.root / "not-a-checkout"
        outside.mkdir()
        with self.patches()[0], self.patches()[1], self.patches()[2]:
            with self.assertRaises(ContextError):
                context_guided.deliver_session(
                    self.associations, self.store, published, repo_path=outside, backend=None,
                )
        self.assertEqual(
            context_session.read(self.associations, self.SESSION_ID)["context_state"],
            "unavailable",
        )

    def test_unpublished_repository_delivery_still_binds_the_consuming_checkout(self):
        evidence = context_guided.deliver_session(
            self.associations, self.store, self.record, repo_path=self.repository, backend=None,
        )
        self.assertIn("Private evidence", evidence)
        self._advance_the_checkout()
        moved = context_session.read(self.associations, self.SESSION_ID)
        with self.assertRaises(ContextError):
            context_guided.deliver_session(
                self.associations, self.store, moved, repo_path=self.repository, backend=None,
            )

    def test_fresh_attachment_refuses_a_moved_checkout_before_any_publication(self):
        """A fresh reservation must bind the actual consumer, not merely the
        remote PR head (codex:65a17212478a56416b1c)."""
        self._advance_the_checkout()
        with self.assertRaises(ContextError):
            self.attach()
        self.assertEqual(self.comments, [])
        pending = context_session.read(self.associations, self.SESSION_ID)
        self.assertEqual(pending["attachment_state"], "none")
        self.assertNotEqual(pending["context_state"], "ready")

    def test_fresh_attachment_recovers_after_checkout_and_pr_advance(self):
        """A fresh reservation that fails before publication must roll back
        to a refreshable prepared/none state -- with zero public writes and
        no leaked usable binding -- rather than stranding a permanently
        pending intent that every retry reauthorizes and that
        ``prepare --refresh`` alone could never clear
        (codex:1a4bc7b34687da726908)."""
        self._advance_without_disturbing_citations()
        self.head = lifecycle.resolve_revision(self.repository)[0]
        fixed = uuid.UUID(hex="1" * 32)
        with mock.patch.object(context_guided.uuid, "uuid4", return_value=fixed):
            with self.assertRaises(ContextError):
                self.attach()
        self.assertEqual(self.comments, [])
        with self.assertRaises(ContextError):
            read_binding(self.store, fixed.hex)
        recovered = context_session.read(self.associations, self.SESSION_ID)
        self.assertEqual(recovered["attachment_state"], "none")
        self.assertEqual(
            (recovered["pr"], recovered["head"], recovered["revision"]), (None, None, None),
        )
        self.assertEqual(recovered["stage"], "prepared")
        self.assertNotEqual(recovered["context_state"], "ready")

        self.manifest = lifecycle.build_graph(
            self.repository, pin=graph_fixtures.PIN,
            indexer=graph_fixtures.indexer(graph_fixtures.graph_document()), root=self.private,
        )
        report, code = context_prepare.prepare(
            self.associations, recovered, repo_root=self.repository, context_root=self.private,
            packet_store=self.store, query="parse_config", source="impact", builder="codex",
            refresh=True,
        )
        self.assertEqual((code, report["status"]), (0, "prepared"))
        report, code = self.attach()
        self.assertEqual((report["status"], code), ("attached", 0))
        attached = context_session.read(self.associations, self.SESSION_ID)
        self.assertIsNotNone(attached["revision"])
        self.assertNotEqual(attached["revision"], fixed.hex)

    def test_same_head_failures_recover_without_stranding_refresh(self):
        """A same-head graph generation replacement, or an unresolvable
        consuming revision, must roll back exactly like a moved checkout --
        never stranding the session on a permanently pending intent
        (codex:1a4bc7b34687da726908)."""
        # The graph is rebuilt for the same commit, minting a new generation
        # the saved packet was never authorized against.
        lifecycle.build_graph(
            self.repository, pin=graph_fixtures.PIN,
            indexer=graph_fixtures.indexer(graph_fixtures.graph_document()), root=self.private,
        )
        with self.assertRaises(ContextError):
            self.attach()
        recovered = context_session.read(self.associations, self.SESSION_ID)
        self.assertEqual(recovered["attachment_state"], "none")
        self.assertEqual(
            (recovered["pr"], recovered["head"], recovered["revision"]), (None, None, None),
        )

        # The consuming checkout cannot be resolved at all.
        outside = self.root / "not-a-checkout"
        outside.mkdir()
        with (
            self.patches()[0], self.patches()[1], self.patches()[2],
            self.patches()[3], self.patches()[4],
        ):
            current = context_session.read(self.associations, self.SESSION_ID)
            with self.assertRaises(ContextError):
                context_guided.attach_session(
                    self.associations, self.store, current, repo_path=outside, pr=1, backend=None,
                )
        recovered = context_session.read(self.associations, self.SESSION_ID)
        self.assertEqual(recovered["attachment_state"], "none")

        # The same session can still explicitly refresh and later attach.
        self.manifest = lifecycle.build_graph(
            self.repository, pin=graph_fixtures.PIN,
            indexer=graph_fixtures.indexer(graph_fixtures.graph_document()), root=self.private,
        )
        report, code = context_prepare.prepare(
            self.associations, recovered, repo_root=self.repository, context_root=self.private,
            packet_store=self.store, query="parse_config", source="impact", builder="codex",
            refresh=True,
        )
        self.assertEqual((code, report["status"]), (0, "prepared"))
        report, code = self.attach()
        self.assertEqual((report["status"], code), ("attached", 0))

    def test_pending_retry_revalidates_the_actual_consumer_before_publication(self):
        """A resumed pending/uncertain attachment must revalidate the current
        consumer rather than replay a stale reservation, while preserving the
        saved intent and its failure diagnostics (codex:65a17212478a56416b1c).
        The crash is injected at the start of ``_finish_publication`` so the
        reservation has already durably transitioned to ``pending`` -- still
        strictly before any GitHub write -- rather than leaving the
        pre-publication ``reserving`` marker this same scenario would leave
        if the crash instead landed inside ``reserve_attachment`` itself."""
        with (
            self.patches()[0], self.patches()[1], self.patches()[2],
            self.patches()[3], self.patches()[4],
            mock.patch.object(context_guided, "_finish_publication", side_effect=KeyboardInterrupt()),
        ):
            with self.assertRaises(KeyboardInterrupt):
                context_guided.attach_session(
                    self.associations, self.store, self.record, repo_path=self.repository,
                    pr=1, backend=None,
                )
        pending = context_session.read(self.associations, self.SESSION_ID)
        self.assertEqual(pending["attachment_state"], "pending")
        revision = pending["revision"]
        self._advance_the_checkout()
        with self.assertRaises(ContextError):
            self.attach()
        after = context_session.read(self.associations, self.SESSION_ID)
        self.assertEqual(after["attachment_state"], "pending")
        self.assertEqual(after["revision"], revision)
        self.assertNotEqual(after["context_state"], "ready")
        self.assertEqual(self.comments, [])

    def test_reserving_retry_recovers_before_any_reservation_ever_completes(self):
        """A crash inside ``reserve_attachment`` itself -- before the saved
        intent ever durably transitions to ``pending`` -- leaves the durable
        pre-publication ``reserving`` marker instead. A later attach must
        recognize it, safely abandon the identity (idempotently, since no
        binding was ever created), and restore prepared/none rather than
        replaying it as if it were a genuine pending publication attempt
        (codex:1a4bc7b34687da726908)."""
        with (
            self.patches()[0], self.patches()[1], self.patches()[2],
            self.patches()[3], self.patches()[4],
            mock.patch.object(context_guided, "reserve_attachment", side_effect=KeyboardInterrupt()),
        ):
            with self.assertRaises(KeyboardInterrupt):
                context_guided.attach_session(
                    self.associations, self.store, self.record, repo_path=self.repository,
                    pr=1, backend=None,
                )
        reserving = context_session.read(self.associations, self.SESSION_ID)
        self.assertEqual(reserving["attachment_state"], "reserving")
        with self.assertRaisesRegex(ContextError, "rerun attach"):
            self.attach()
        recovered = context_session.read(self.associations, self.SESSION_ID)
        self.assertEqual(
            (recovered["attachment_state"], recovered["pr"], recovered["head"], recovered["revision"]),
            ("none", None, None, None),
        )
        self.assertEqual(self.comments, [])
        report, code = self.attach()
        self.assertEqual((report["status"], code), ("attached", 0))


if __name__ == "__main__":
    unittest.main()
