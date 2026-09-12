"""Guided session attachment, private handoff, and crash reconciliation."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from code_mower import context_audit, context_guided, context_session
from code_mower.context_contract import ContextError
from code_mower.context_delivery import deliver, read_binding, save_feedback
from code_mower.context_review import INPUT_HEADER
from code_mower.context_store import ContextStore
import test_context_delivery as fixtures


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
        pending = context_session.read(self.associations, self.session["id"])
        revision = pending["revision"]
        self.assertEqual(pending["attachment_state"], "pending")
        report, code = self.attach()
        self.assertEqual((report["status"], code), ("attached", 0))
        self.assertEqual(context_session.read(self.associations, self.session["id"])["revision"], revision)

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


if __name__ == "__main__":
    unittest.main()
