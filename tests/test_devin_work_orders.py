"""Offline trusted-work-order delivery and hostile builder evidence fixtures."""
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from code_mower.context_contract import ContextRequest
from code_mower.context_connections import connect, disconnect
from code_mower.context_packets import fetch, load_authorized
from code_mower.context_store import ContextStore
from code_mower.devin_sessions import DevinClient
from code_mower.devin_work_orders import (
    COMPLETION_SCHEMA, Candidates, DevinWorkOrders, PullRequest, WorkOrder, _hash, packet_context,
)
from code_mower.remote_session import FakeProvider, RemoteError, RemoteSessions, _key
from code_mower.work_orders import WORK_ORDER_SCHEMA
import test_context_delivery as fixtures

CANARY = "PRIVATE_PROSE_SOURCE_DIFF_CREDENTIAL_RESULT"
HEAD = "a" * 40


class GitHubFixture:
    def __init__(self):
        self.pr = PullRequest("owner/repo", 42, (("owner/repo", 907),), 123,
                              "builder[bot]", "owner/repo", "devin/907", HEAD, "main")
        self.page = None
        self.reads = []
        self.calls = []

    def candidates(self, repository, branch, *, limit):
        self.calls.append((repository, branch, limit))
        return self.page or Candidates((self.pr,), True)

    def read(self, repository, number):
        self.calls.append((repository, number))
        return self.reads.pop(0) if self.reads else self.pr


class WorkOrderCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.provider = FakeProvider(self.root / "provider")
        self.remote = RemoteSessions(self.root / "remote", self.provider)
        self.github = GitHubFixture()
        self.service = DevinWorkOrders(self.root / "builder", self.remote, self.github)
        self.order = WorkOrder.from_manifest(
            {"schema": WORK_ORDER_SCHEMA, "repo": "owner/repo",
             "source": {"repo": "owner/repo", "issue_number": "907"},
             "output_path": CANARY, "context_manifest": CANARY}, CANARY,
            repository="owner/repo", issue=907, branch="devin/907", base="main",
            author_id=123, author_login="builder[bot]", acu_limit=5)
        self.key = self.service._key(self.order)

    def run_order(self, command, **kwargs):
        return self.service.run(command, self.order, apply=True, **kwargs)

    def binding(self):
        with self.remote.store.locked(_key(self.key)) as locked:
            return locked.read()["binding"]

    def claim(self, **kwargs):
        return {"schema": COMPLETION_SCHEMA, "round": 0, "repository": "owner/repo",
                "issue": 907, "pr_number": 42, "head_sha": HEAD, **kwargs}

    def complete(self, **kwargs):
        self.provider.set_state(self.binding(), "complete", result=self.claim(**kwargs))


class DeliveryTests(WorkOrderCase):
    def test_preview_and_manifest_binding(self):
        self.assertTrue(self.service.run("dispatch", self.order)["apply_required"])
        self.assertFalse((self.root / "builder").exists())
        self.assertFalse((self.root / "remote").exists())
        self.assertNotIn(CANARY, repr(self.order))
        with self.assertRaisesRegex(RemoteError, "work_order_binding"):
            WorkOrder.from_manifest({}, CANARY, repository="owner/repo", issue=907,
                                    branch="devin/907", base="main", author_id=123,
                                    author_login="builder[bot]")
        for changes in ({"branch": "main"}, {"branch": "bad/../ref"}, {"acu_limit": True},
                        {"issue": True}, {"author_login": CANARY + "\n"}):
            with self.subTest(changes=changes), self.assertRaises(RemoteError):
                replace(self.order, **changes)

    def test_delivery_progress_clarification_completion_fix_cancel_privacy(self):
        with patch.object(self.provider, "create", wraps=self.provider.create) as create:
            self.assertEqual(self.run_order("dispatch")["session"]["state"], "running")
            prompt, repo, cap, _ = create.call_args.args
            self.assertIn(CANARY, prompt)
            self.assertIn(COMPLETION_SCHEMA, prompt)
            self.assertEqual((repo, cap), ("owner/repo", 5))
            self.run_order("dispatch")
            self.assertEqual(create.call_count, 1)
        outputs = []
        for reason, state in (("waiting_for_owner", "waiting_for_user"),
                              ("approval_required", "waiting_for_approval")):
            self.provider.set_state(self.binding(), "owner_action", reason=reason)
            outputs.append(self.run_order("status"))
            self.assertEqual(outputs[-1]["session"]["state"], state)
        outputs.append(self.run_order("clarify", request="clarification", prose=CANARY))
        self.complete(round=1)
        with patch.object(self.remote, "observed_acu", return_value=1.25):
            evidence = self.run_order("collect")
        outputs.append(evidence)
        self.assertEqual(evidence["verified_pr"]["head_sha"], HEAD)
        self.assertEqual(evidence["observed_acu"], 1.25)
        self.assertEqual(len(self.github.calls), 3)
        self.assertFalse(evidence["merge_authority"])
        outputs.append(self.run_order("fix", request="fix-1", prose=CANARY, reviewed_head=HEAD))
        self.assertIsNone(self.remote.private_result(self.key))
        self.assertIsNone(outputs[-1]["verified_pr"])
        self.assertEqual(self.run_order("fix", request="fix-1", prose=CANARY,
                                        reviewed_head=HEAD)["round"], 2)
        self.github.pr = replace(self.github.pr, head_sha="b" * 40)
        self.complete(round=2, head_sha="b" * 40)
        outputs.append(self.run_order("collect"))
        self.assertEqual(outputs[-1]["verified_pr"]["head_sha"], "b" * 40)
        outputs.append(self.run_order("cancel", request="cancel-1"))
        self.assertEqual(outputs[-1]["session"]["state"], "terminated")
        self.assertEqual(self.run_order("cancel", request="cancel-1")["session"]["counts"]["cancel"], 1)
        serialized = json.dumps(outputs)
        for private in (CANARY, str(self.root), "structured_output", "linked_issues", "builder[bot]"):
            self.assertNotIn(private, serialized)
        for path in self.root.rglob("*"):
            self.assertEqual(path.stat().st_mode & 0o077, 0)

    def test_hostile_github_claims_fail_closed(self):
        mutations = [
            {"repository": "other/repo"}, {"linked_issues": (("owner/repo", 908),)},
            {"linked_issues": (("other/repo", 907),)}, {"linked_issues": ()},
            {"linked_issues": (("owner/repo", 907), ("owner/repo", 908))},
            {"author_id": 456}, {"author_login": "imposter"}, {"head_branch": "other"},
            {"head_repository": "fork/repo"}, {"head_sha": "b" * 40},
            {"base_branch": "other"}, {"number": 43}, {"state": "closed"},
        ]
        self.run_order("dispatch")
        self.complete()
        original = self.github.pr
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.github.pr = replace(original, **mutation)
                with self.assertRaisesRegex(RemoteError, "pull_request_binding"):
                    self.run_order("collect")
        self.github.pr = original
        for page in (Candidates((), True), Candidates((original, original), True),
                     Candidates((original,), False)):
            self.github.page = page
            with self.assertRaisesRegex(RemoteError, "ambiguous"):
                self.run_order("collect")
        self.github.page = None
        self.github.reads = [original, replace(original, head_sha="b" * 40)]
        with self.assertRaisesRegex(RemoteError, "pull_request_binding"):
            self.run_order("collect")
        self.assertIsNone(self.run_order("status")["verified_pr"])

    def test_completion_validation_and_private_adapter_failures(self):
        self.run_order("dispatch")
        self.complete()
        for claim in (None, {"raw": CANARY}, self.claim(issue=True), self.claim(round=1),
                      self.claim(repository="other/repo"), self.claim(head_sha=CANARY),
                      self.claim(extra=CANARY), self.claim(pr_number=True)):
            with patch.object(self.remote, "private_result", return_value=claim):
                with self.assertRaisesRegex(RemoteError, "invalid_completion"):
                    self.run_order("collect")
        with patch.object(self.github, "read", side_effect=RuntimeError(CANARY)):
            with self.assertRaisesRegex(RemoteError, "^github_unavailable$"):
                self.run_order("collect")
        with patch.object(self.remote, "observed_acu", return_value=float("nan")):
            with self.assertRaisesRegex(RemoteError, "invalid_usage"):
                self.run_order("collect")

    def test_binding_and_exact_head_fix_rules(self):
        self.run_order("dispatch")
        for mutation in ({"body": "changed"}, {"branch": "devin/other"}, {"acu_limit": 6},
                         {"author_id": 456}):
            with self.assertRaisesRegex(RemoteError, "work_order_binding"):
                self.service.run("dispatch", replace(self.order, **mutation), apply=True)
        with self.assertRaisesRegex(RemoteError, "stale_review_head"):
            self.run_order("fix", request="fix", prose=CANARY, reviewed_head=HEAD)
        self.complete()
        self.run_order("collect")
        with self.assertRaisesRegex(RemoteError, "fix_requires"):
            self.run_order("clarify", request="bypass", prose=CANARY)
        self.github.pr = replace(self.github.pr, head_sha="b" * 40)
        with self.assertRaisesRegex(RemoteError, "pull_request_binding"):
            self.run_order("fix", request="fix", prose=CANARY, reviewed_head=HEAD)
        self.assertIsNone(self.run_order("status")["verified_pr"])

    def test_lost_create_reconciles_without_paid_duplicate_and_schema(self):
        calls, tags, matches = [], [], []
        def runner(method, url, body, headers):
            calls.append((method, url))
            if method == "POST":
                self.assertEqual(body["repos"], ["owner/repo"])
                self.assertEqual(body["max_acu_limit"], 5)
                self.assertTrue(body["structured_output_required"])
                self.assertFalse(body["structured_output_schema"]["additionalProperties"])
                tags[:] = body["tags"]
                raise TimeoutError(CANARY)
            if "?" in url:
                return {"items": [{"session_id": sid, "status": "running", "tags": tags}
                                  for sid in matches], "has_next_page": False}
            return {"session_id": "devin-one", "status": "running"}
        client = DevinClient("org-example", "test-key", api_runner=runner)
        self.service = DevinWorkOrders.hosted(self.root / "hosted", client, self.github)
        with self.assertRaisesRegex(RemoteError, "reconcile_dispatch"):
            self.run_order("dispatch")
        self.assertEqual(self.run_order("dispatch")["session"]["state"], "uncertain")
        matches[:] = ["devin-one", "devin-two"]
        self.assertEqual(self.run_order("status")["session"]["next_action"], "inspect_provider")
        matches[:] = ["devin-one"]
        self.service = DevinWorkOrders.hosted(self.root / "hosted", client, self.github)
        self.assertEqual(self.run_order("status")["session"]["state"], "running")
        self.assertEqual(sum(m == "POST" for m, _ in calls), 1)
        self.assertRegex(tags[-1], r"^cm-[0-9a-f]{32}$")

    def test_branch_single_writer_and_lost_message_acknowledgement(self):
        self.run_order("dispatch")
        with self.assertRaisesRegex(RemoteError, "branch_writer_conflict"):
            self.service.run("dispatch", replace(self.order, issue=908), apply=True)
        original = self.provider.message
        def lost(binding, prose):
            original(binding, prose)
            raise TimeoutError(CANARY)
        with patch.object(self.provider, "message", side_effect=lost) as message:
            with self.assertRaisesRegex(RemoteError, "provider_unavailable"):
                self.run_order("clarify", request="m1", prose=CANARY)
            with self.assertRaisesRegex(RemoteError, "inspect_provider"):
                self.run_order("clarify", request="m2", prose=CANARY)
            with self.assertRaisesRegex(RemoteError, "inspect_provider"):
                self.run_order("clarify", request="m1", prose=CANARY)
            result = self.run_order("clarify", request="m1", prose=CANARY,
                                    acknowledge_delivered=True)
            self.assertEqual(result["round"], 1)
            self.assertEqual(message.call_count, 1)
        self.complete()  # Provider replays a completion from before the message.
        with self.assertRaisesRegex(RemoteError, "invalid_completion"):
            self.run_order("collect")

    def test_not_ready_and_missing_results_have_no_evidence(self):
        self.run_order("dispatch")
        self.assertIsNone(self.run_order("collect")["verified_pr"])
        self.provider.set_state(self.binding(), "complete")
        result = self.run_order("collect")
        self.assertIsNone(result["verified_pr"])
        self.assertEqual(result["session"]["reason"], "result_unavailable")
        self.assertFalse(self.github.calls)

    def test_hosted_completion_usage_fix_and_cancellation(self):
        status = {"status": "running"}
        calls = []
        def runner(method, url, body, headers):
            calls.append((method, url))
            if "/consumption/" in url:
                return {"total_acus": 3.25, "private": CANARY}
            if method == "DELETE":
                status.clear()
                status["status"] = "exit"
                return {"session_id": "devin-one", **status}
            if url.endswith("/messages"):
                status.clear()
                status["status"] = "running"
            return {"session_id": "devin-one", **status}
        client = DevinClient("org-example", "test-key", api_runner=runner)
        self.service = DevinWorkOrders.hosted(self.root / "hosted", client, self.github)
        self.run_order("dispatch")
        status.update(status="exit", status_detail="finished", structured_output=self.claim())
        result = self.run_order("collect")
        self.assertEqual(result["observed_acu"], 3.25)
        self.assertEqual(result["transport"], "devin_api_v3")
        self.assertEqual(result["verified_pr"]["pr_number"], 42)
        self.run_order("fix", request="fix-1", prose=CANARY, reviewed_head=HEAD)
        self.github.pr = replace(self.github.pr, head_sha="b" * 40)
        status.update(status="exit", status_detail="finished",
                      structured_output=self.claim(round=1, head_sha="b" * 40))
        self.assertEqual(self.run_order("collect")["verified_pr"]["head_sha"], "b" * 40)
        self.assertEqual(self.run_order("cancel", request="c1")["session"]["state"], "terminated")
        self.assertEqual(sum(method == "POST" and url.endswith("/sessions")
                             for method, url in calls), 1)

    def test_usage_api_is_bounded_and_not_completion_content(self):
        calls = []
        def runner(method, url, body, headers):
            calls.append((method, url))
            return {"total_acus": 2.5, "private": CANARY}
        client = DevinClient("org-example", "test-key", api_runner=runner)
        self.assertEqual(client.session_acu("devin-one"), 2.5)
        self.assertTrue(calls[0][1].endswith("/consumption/daily/sessions/devin-one"))


CONTEXT_CANARY = "PRIVATE_CONTEXT_PACKET_CANARY_TEXT"


class Crash(Exception):
    """Process stop between the local intent write and the remote intent write."""
PAUSED = {"outcome": "UNKNOWN", "state": "paused", "reason": "context_unavailable"}


def _expire(proof):
    proof.expires_at = int(time.time()) - 1
    return proof


def _packet_of(context, order):
    """Test-only view of the authorized packet a bound context names for this order."""
    return load_authorized(context.store, context.name, context.handle, context.policy,
                           ContextRequest(order.repository, order.work_item, "devin:builder"),
                           backend=context.backend)


class Unprotected(ContextStore):
    """A store subclass a caller might substitute; never accepted at the work-order boundary."""


@unittest.skipUnless(os.name == "posix", "private packets need POSIX protections")
class ContextInjectionTests(WorkOrderCase):
    """The hosted builder receives the common packet only inside create/message input."""

    def setUp(self):
        super().setUp()
        self.order = replace(self.order, context_policy="required")
        self.key = self.service._key(self.order)
        self.optional_order = replace(self.order, context_policy="optional")
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        self.outside = Path(outside.name).resolve()
        self.fixture = fixtures.ContextDeliveryTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.backend = self.fixture.backend
        self.backend.result["result"]["results"][0]["text"] = CONTEXT_CANARY
        self.policy = self.fixture.spec["policy"]
        self.optional_policy = {**self.policy, "required": False}
        self.spec = {**self.fixture.spec, "work_item": str(self.order.issue)}
        self.result = fetch(self.fixture.store, "example", self.spec, backend=self.backend, refresh=True)
        self.context = self.bind()
        self.optional = self.bind(order=self.optional_order, policy=self.optional_policy)

    def bind(self, order=None, policy=None, handle=None):
        return packet_context(self.fixture.store, "example", handle or self.result["packet_handle"],
                              policy or self.policy, order=order or self.order, backend=self.backend)

    def synthetic(self, *, text=None, recipients=None, order=None, handle=None):
        """A local packet outside the protected store with this order's own binding fields."""
        order = order or self.order
        context = self.context if handle is None else replace(self.context, handle=handle)
        payload = _packet_of(context, order).private_payload()
        if recipients is not None:
            payload["binding"]["recipients"] = recipients
        if text is not None:
            payload["documents"][0]["text"] = text
        root = Path(tempfile.mkdtemp(dir=self.outside))
        store = ContextStore(root)
        with self.fixture.store.locked("example") as locked:
            index = locked.artifact("i-" + hashlib.sha256(b"example").hexdigest()[:48]).read()
        with store.locked("example") as locked:
            for entry in index["entries"]:
                entry["reference"] = None
            raw = json.dumps(payload, sort_keys=True).encode()
            entry = next(e for e in index["entries"] if e["handle"] == context.handle)
            entry["reference"] = {"path": ".p-" + entry["handle"] + ".json", "sha256": hashlib.sha256(raw).hexdigest()}
            (root / entry["reference"]["path"]).write_bytes(raw)
            locked.artifact("i-" + hashlib.sha256(b"example").hexdigest()[:48]).write(index)
        self.assertEqual(payload["binding"]["repository"], order.repository)
        self.assertEqual(payload["binding"]["work_item"], order.work_item)
        return replace(context, store=store)

    def reconnect(self, recipients):
        disconnect(self.fixture.store, "example", backend=self.backend)
        connect(self.fixture.store, "example", {"principal": "one@example.invalid", "workspace": "example",
                "repositories": ["owner/repo"], "recipients": recipients}, backend=self.backend)

    def unavailable(self):
        """A bound context whose store no longer holds an authorizable connection."""
        empty = ContextStore(Path(tempfile.mkdtemp(dir=self.outside)))
        return replace(self.context, store=empty)

    def run_optional(self, command, **kwargs):
        return self.service.run(command, self.optional_order, apply=True, **kwargs)

    def large(self):
        """Refresh the packet with a document large enough to break the 64 KiB input budget."""
        self.backend.result["result"]["results"][0]["text"] = CONTEXT_CANARY + "y" * 19_000
        self.result = fetch(self.fixture.store, "example", self.spec, backend=self.backend, refresh=True)
        self.context = self.bind()
        return self.context

    def private_files(self):
        return [path for path in self.root.rglob("*") if path.is_file()]

    def assert_not_persisted(self, *outputs):
        serialized = json.dumps(outputs)
        for private in (CONTEXT_CANARY, "Packet identity", "one@example.invalid", self.result["packet_handle"]):
            self.assertNotIn(private, serialized)
        for path in self.private_files():
            self.assertNotIn(CONTEXT_CANARY.encode(), path.read_bytes())

    def assert_paused(self, output, slot, policy="required"):
        self.assertEqual({k: output[k] for k in PAUSED}, PAUSED)
        self.assertEqual(output["context"], {"policy": policy, slot: "unavailable"})
        self.assertFalse(output["merge_authority"])
        self.assertNotIn("session", output)

    def test_packet_context_carries_no_identity_and_policy_must_agree_with_the_order(self):
        self.assertEqual(tuple(self.context.__dataclass_fields__), ("store", "name", "handle", "policy", "backend"))
        self.assertNotIn(CONTEXT_CANARY, repr(self.context))
        self.assertNotIn(self.result["packet_handle"], repr(self.context))
        for handle in ("", "not-a-handle", self.result["packet_handle"].upper(), None):
            with self.subTest(handle=handle), self.assertRaisesRegex(RemoteError, "invalid_request"):
                packet_context(self.fixture.store, "example", handle, self.policy, order=self.order,
                               backend=self.backend)
        for store in (self.fixture.store.root, Unprotected(self.fixture.store.root)):
            with self.subTest(store=type(store).__name__), self.assertRaisesRegex(RemoteError, "invalid_request"):
                packet_context(store, "example", self.result["packet_handle"], self.policy,
                               order=self.order, backend=self.backend)
        with self.assertRaises(RemoteError):
            replace(self.order, context_policy="always")
        for order, policy in ((self.order, self.optional_policy), (self.optional_order, self.policy),
                              (replace(self.order, context_policy="none"), self.policy),
                              (self.order, None), (self.order, {}), (self.order, {**self.policy, "required": "yes"})):
            with self.subTest(policy=order.context_policy), self.assertRaisesRegex(RemoteError, "invalid_request"):
                packet_context(self.fixture.store, "example", self.result["packet_handle"], policy,
                               order=order, backend=self.backend)

    def test_preview_never_retrieves_context(self):
        with patch("code_mower.devin_work_orders.load_authorized", wraps=load_authorized) as load:
            for command in ("dispatch", "clarify", "fix"):
                output = self.service.run(command, self.order, context=self.context, request="r", prose="p")
                self.assertTrue(output["apply_required"])
        self.assertEqual(load.call_count, 0)
        self.assertFalse((self.root / "builder").exists())

    def test_create_and_message_receive_the_common_evidence_once_each(self):
        outputs = []
        identity = "Packet identity: " + self.result["packet_handle"]
        with patch.object(self.provider, "create", wraps=self.provider.create) as create, \
                patch.object(self.provider, "message", wraps=self.provider.message) as message:
            outputs.append(self.run_order("dispatch", context=self.context))
            self.assertEqual(outputs[-1]["context"], {"policy": "required", "dispatch": "delivered", "message": None})
            prompt = create.call_args.args[0]
            self.assertIn(CANARY, prompt)
            self.assertIn(CONTEXT_CANARY, prompt)
            self.assertIn(identity, prompt)
            self.assertNotIn("one@example.invalid", prompt)
            self.assertEqual(prompt.split("\n").count("Private evidence for this work item. " + identity), 1)
            outputs.append(self.run_order("dispatch", context=self.context))
            self.assertEqual(create.call_count, 1)
            self.assert_paused(self.run_order("dispatch"), "dispatch")  # Required context cannot be dropped on replay.
            self.assertEqual(create.call_count, 1)
            outputs.append(self.run_order("clarify", request="c-1", prose=CANARY, context=self.context))
            self.assertEqual(outputs[-1]["context"]["message"], "delivered")
            self.assertIn(CONTEXT_CANARY, message.call_args.args[1])
            self.assertIn(CANARY, message.call_args.args[1])
            self.complete(round=1)
            outputs.append(self.run_order("collect"))
            self.assertEqual(outputs[-1]["context"], {"policy": "required", "dispatch": "delivered", "message": "delivered"})
            outputs.append(self.run_order("fix", request="fix-1", prose=CANARY, reviewed_head=HEAD,
                                          context=self.context))
            self.assertIn(CONTEXT_CANARY, message.call_args.args[1])
            self.assertEqual(message.call_count, 2)
        self.assert_not_persisted(*outputs)
        with self.remote.store.locked(_key(self.key)) as locked:
            self.assertNotIn(CONTEXT_CANARY, json.dumps(locked.read()))

    def test_rejected_dispatch_context_writes_no_reservation(self):
        with patch.object(self.provider, "create", wraps=self.provider.create) as create:
            for context in (self.unavailable(), None, self.bind(handle=uuid.uuid4().hex)):
                self.assert_paused(self.run_order("dispatch", context=context), "dispatch")
            with self.assertRaisesRegex(RemoteError, "context_binding_mismatch"):
                self.run_order("dispatch", context=self.result["packet_handle"])
            with self.assertRaisesRegex(RemoteError, "context_budget_exceeded"):
                self.service.run("dispatch", replace(self.order, body=CANARY + "b" * 47_000), apply=True,
                                 context=self.large())
            self.assertEqual(create.call_count, 0)
        with self.assertRaisesRegex(RemoteError, "work_order_not_found"):
            self.run_order("status")
        # The undispatched order binds neither its body nor its branch.
        corrected = replace(self.order, body=CANARY + " corrected")
        self.service.run("dispatch", corrected, apply=True, context=self.bind(order=corrected))
        other = replace(self.order, issue=908, branch="devin/908", context_policy="none")
        self.service.run("dispatch", other, apply=True)

    def test_status_collect_and_cancel_reject_context(self):
        self.run_order("dispatch", context=self.context)
        for command in ("status", "collect", "cancel"):
            with self.subTest(command=command), self.assertRaisesRegex(RemoteError, "invalid_request"):
                self.run_order(command, request="cancel-1", context=self.context)

    def test_context_must_be_bound_to_this_work_order_and_recipient(self):
        ticket_b = replace(self.order, issue=908, branch="devin/908")
        spec_b = {**self.spec, "work_item": "908"}
        handle_b = fetch(self.fixture.store, "example", spec_b, backend=self.backend, refresh=True)["packet_handle"]
        valid_b = self.bind(order=ticket_b, handle=handle_b)
        self.service.run("dispatch", ticket_b, apply=True, context=valid_b)  # Ticket B's own packet is fine.
        # The context names a packet only; the order it is used with supplies the binding.
        self.assertEqual(self.bind(order=replace(self.order, repository="other/repo")), self.context)
        policy_none = replace(self.order, issue=909, branch="devin/909", context_policy="none")
        paused = {
            "packet_for_ticket_b": (self.order, valid_b),  # Ticket B's handle does not authorize for A.
            "wrong_recipient": (self.order, self.synthetic(recipients=["codex:builder", "claude:reviewer"])),
            "synthetic_same_binding_changed_text": (self.order, self.synthetic(text="forged " + CONTEXT_CANARY)),
            "synthetic_verbatim_copy": (self.order, self.synthetic()),
        }
        for name, (order, context) in paused.items():
            with self.subTest(case=name), patch.object(self.provider, "create", wraps=self.provider.create) as create:
                self.assert_paused(self.service.run("dispatch", order, apply=True, context=context), "dispatch")
                self.assertEqual(create.call_count, 0)
        mismatched = {
            "bare_handle": (self.order, self.result["packet_handle"]),
            "bare_packet": (self.order, _packet_of(self.context, self.order)),
            "evidence_text": (self.order, "Packet identity: forged\n" + CONTEXT_CANARY),
            "look_alike": (self.order, type("PacketContext", (), dict(asdict(self.context)))()),
            "store_subclass": (self.order, replace(self.context, store=Unprotected(self.fixture.store.root))),
            "policy_none_order_with_context": (policy_none, self.context),
        }
        for name, (order, context) in mismatched.items():
            with self.subTest(case=name), patch.object(self.provider, "create", wraps=self.provider.create) as create:
                with self.assertRaisesRegex(RemoteError, "context_binding_mismatch"):
                    self.service.run("dispatch", order, apply=True, context=context)
                self.assertEqual(create.call_count, 0)
        with self.assertRaisesRegex(RemoteError, "work_order_not_found"):
            self.run_order("status")
        # Evidence is rendered from the authorized packet itself under its own handle.
        with patch.object(self.provider, "create", wraps=self.provider.create) as create:
            self.run_order("dispatch", context=self.context)
            self.assertIn("Packet identity: " + self.result["packet_handle"], create.call_args.args[0])
            self.assertNotIn(handle_b, create.call_args.args[0])
            self.assertNotIn("forged", create.call_args.args[0])
        with patch.object(self.provider, "message", wraps=self.provider.message) as message:
            for context in (valid_b, self.synthetic(recipients=["codex:builder"])):
                self.assert_paused(self.run_order("clarify", request="c-1", prose=CANARY, context=context), "message")
            self.assertEqual(message.call_count, 0)
        self.assertEqual(self.run_order("status")["round"], 0)

    def test_authorization_is_rechecked_and_fails_closed_without_a_provider_write(self):
        cases = {
            "wrong_identity": lambda: setattr(self.backend, "wrong_identity", True),
            "revoked": lambda: setattr(self.backend, "revoked", True),
            "packet_for_another_work_item": lambda: setattr(self, "context", self.bind(
                handle=fetch(self.fixture.store, "example", self.fixture.spec,
                             backend=self.backend, refresh=True)["packet_handle"])),
            "changed_evidence": lambda: fetch(self.fixture.store, "example", self.spec,
                                              backend=self.backend, refresh=True),
            "expired": lambda: setattr(self.backend, "proof", lambda expected, sequence: _expire(proof(expected, sequence))),
            "changed_recipients": lambda: self.reconnect(["codex:builder", "claude:reviewer"]),
            "synthetic_packet": lambda: setattr(self, "context", self.synthetic(text="forged " + CONTEXT_CANARY)),
            "store_without_connection": lambda: setattr(self, "context", self.unavailable()),
        }
        proof = self.backend.proof
        for name, arrange in cases.items():
            with self.subTest(case=name):
                self.setUp()
                arrange()
                with patch.object(self.provider, "create", wraps=self.provider.create) as create:
                    self.assert_paused(self.run_order("dispatch", context=self.context), "dispatch")
                    self.assertEqual(create.call_count, 0)
                with self.assertRaisesRegex(RemoteError, "work_order_not_found"):
                    self.run_order("status")
                self.assertNotIn(CONTEXT_CANARY.encode(), b"".join(p.read_bytes() for p in self.private_files()))
        self.setUp()
        self.run_order("dispatch", context=self.context)
        with patch.object(self.provider, "message", wraps=self.provider.message) as message:
            for context in (self.unavailable(), None):
                self.assert_paused(self.run_order("clarify", request="c-1", prose=CANARY, context=context), "message")
            self.assertEqual(message.call_count, 0)
        self.assertEqual(self.run_order("status")["round"], 0)  # No local round was consumed.
        self.assertEqual(self.run_order("clarify", request="c-1", prose=CANARY,
                                        context=self.context)["round"], 1)
        self.backend.revoked = True
        with patch.object(self.provider, "message", wraps=self.provider.message) as message:
            self.assert_paused(self.run_order("clarify", request="c-2", prose=CANARY, context=self.context), "message")
            self.assertEqual(message.call_count, 0)
        status = self.run_order("status")
        self.assertEqual((status["round"], status["context"]["message"]), (1, "delivered"))

    def test_optional_context_degrades_explicitly_and_states_persist(self):
        unavailable = self.unavailable()
        with patch.object(self.provider, "create", wraps=self.provider.create) as create:
            output = self.run_optional("dispatch", context=unavailable)
            self.assertEqual(output["context"], {"policy": "optional", "dispatch": "degraded", "message": None})
            self.assertNotIn("Packet identity", create.call_args.args[0])
            self.assertIn(CANARY, create.call_args.args[0])
            with self.assertRaisesRegex(RemoteError, "request_conflict"):
                self.run_optional("dispatch", context=self.optional)  # Input changed.
            self.assertEqual(self.run_optional("dispatch", context=unavailable)["context"]["dispatch"], "degraded")
        handle_b = fetch(self.fixture.store, "example", {**self.spec, "work_item": "908"},
                         backend=self.backend, refresh=True)["packet_handle"]
        with patch.object(self.provider, "message", wraps=self.provider.message) as message:
            # Optional never relaxes binding: another ticket's packet degrades to code-only, not delivery.
            output = self.run_optional("clarify", request="c-0", prose=CANARY,
                                       context=replace(self.optional, handle=handle_b))
            self.assertEqual(output["context"]["message"], "degraded")
            self.assertNotIn(CONTEXT_CANARY, message.call_args.args[1])
            self.assertNotIn("Packet identity", message.call_args.args[1])
        with patch.object(self.provider, "message", wraps=self.provider.message) as message:
            output = self.run_optional("clarify", request="c-1", prose=CANARY, context=None)
            self.assertEqual((output["context"]["message"], output["round"]), ("omitted", 2))
            output = self.run_optional("clarify", request="c-2", prose=CANARY, context=self.optional)
            self.assertEqual((output["context"]["message"], output["round"]), ("delivered", 3))
            self.assertIn(CONTEXT_CANARY, message.call_args.args[1])
            # Acknowledging a delivered message preserves its saved state instead of "omitted".
            output = self.run_optional("clarify", request="c-2", prose=CANARY, acknowledge_delivered=True)
            self.assertEqual(output["context"], {"policy": "optional", "dispatch": "degraded", "message": "delivered"})
            self.assertEqual(message.call_count, 2)
        status = self.run_optional("status")
        self.assertEqual(status["context"], {"policy": "optional", "dispatch": "degraded", "message": "delivered"})
        self.assertEqual(status["round"], 3)
        with self.service.store.locked(self.service._key(self.optional_order)) as locked:
            record = locked.read()
        self.assertEqual((record["context"], record["message"]["context"]), ("degraded", "delivered"))
        self.assert_not_persisted(output, status)

    def test_recovery_cannot_change_evidence_after_the_local_intent_is_durable(self):
        self.run_optional("dispatch", context=self.optional)
        with patch.object(self.remote, "run", side_effect=Crash()), self.assertRaises(Crash):
            self.run_optional("clarify", request="c-1", prose=CANARY, context=self.optional)
        with self.service.store.locked(self.service._key(self.optional_order)) as locked:
            record = locked.read()
        self.assertEqual((record["round"], record["message"]["pending"], record["message"]["context"]),
                         (1, True, "delivered"))
        with patch.object(self.provider, "message", wraps=self.provider.message) as message:
            for context in (None, self.unavailable()):
                with self.assertRaisesRegex(RemoteError, "request_conflict"):
                    self.run_optional("clarify", request="c-1", prose=CANARY, context=context)
            fetch(self.fixture.store, "example", self.spec, backend=self.backend, refresh=True)
            with self.assertRaisesRegex(RemoteError, "request_conflict"):  # Refreshed packet: input differs.
                self.run_optional("clarify", request="c-1", prose=CANARY, context=self.optional)
            self.assertEqual(message.call_count, 0)
            self.result = fetch(self.fixture.store, "example", self.spec, backend=self.backend, refresh=True)
            self.backend.result["result"]["results"][0]["text"] = CONTEXT_CANARY
            self.assertEqual(self.run_optional("status")["context"]["message"], "delivered")
        # Same crash before the remote create intent: the dispatch input is bound too.
        self.setUp()
        with patch.object(self.remote, "run", side_effect=Crash()), self.assertRaises(Crash):
            self.run_optional("dispatch", context=self.optional)
        with self.assertRaisesRegex(RemoteError, "request_conflict"):
            self.run_optional("dispatch")
        output = self.run_optional("dispatch", context=self.optional)
        self.assertEqual(output["context"]["dispatch"], "delivered")

    def test_tracker_key_binds_context_while_the_github_issue_binds_the_pull_request(self):
        """A guided Jira session keeps its own work item; the delivery issue stays the integer one."""
        manifest = {"schema": WORK_ORDER_SCHEMA, "repo": "owner/repo", "source": {"repo": "owner/repo"},
                    "output_path": CANARY, "context_manifest": CANARY}
        common = dict(repository="owner/repo", issue=907, branch="devin/907", base="main",
                      author_id=123, author_login="builder[bot]", acu_limit=5, context_policy="required")
        with self.assertRaisesRegex(RemoteError, "work_order_binding_mismatch"):  # No key: the issue is required.
            WorkOrder.from_manifest(manifest, CANARY, **common)
        for present in ("908", 908, 0, False, "", None, True, 907.0, [907]):  # A present issue must still match.
            with self.subTest(issue_number=present), self.assertRaisesRegex(RemoteError, "work_order_binding_mismatch"):
                WorkOrder.from_manifest({**manifest, "source": {"repo": "owner/repo", "issue_number": present}},
                                        CANARY, **common, context_work_item="EXAMPLE-1")
        for present in ("907", 907):
            WorkOrder.from_manifest({**manifest, "source": {"repo": "owner/repo", "issue_number": present}},
                                    CANARY, **common, context_work_item="EXAMPLE-1")
        for bad in ("", " EXAMPLE-1", "EXAMPLE\n1", "x" * 129, 907):
            with self.subTest(work_item=bad), self.assertRaisesRegex(RemoteError, "invalid_work_order|binding_mismatch"):
                WorkOrder.from_manifest(manifest, CANARY, **common, context_work_item=bad)
        with self.assertRaisesRegex(RemoteError, "invalid_work_order"):  # Context-free orders carry no key.
            replace(self.order, context_policy="none", context_work_item="EXAMPLE-1")
        self.order = WorkOrder.from_manifest(manifest, CANARY, **common, context_work_item="EXAMPLE-1")
        self.key = self.service._key(self.order)
        self.assertEqual((self.order.issue, self.order.work_item), (907, "EXAMPLE-1"))
        self.assertEqual(self.service._fields(self.order)["context_work_item"], "EXAMPLE-1")
        self.assertNotEqual(self.service._binding(self.order),
                            self.service._binding(replace(self.order, context_work_item="EXAMPLE-2")))
        # The GitHub issue's packet is not this order's context; the Jira packet is.
        github_packet = self.bind()
        jira_handle = self.fixture.result["packet_handle"]
        self.assertNotEqual(jira_handle, self.result["packet_handle"])
        jira = self.bind(handle=jira_handle)
        with patch.object(self.provider, "create", wraps=self.provider.create) as create:
            self.assert_paused(self.run_order("dispatch", context=github_packet), "dispatch")
            self.assert_paused(self.run_order("dispatch", context=self.synthetic(order=self.order, handle=jira_handle,
                                                                                 text="forged")), "dispatch")
            self.assertEqual(create.call_count, 0)
            with self.assertRaisesRegex(RemoteError, "work_order_not_found"):
                self.run_order("status")
            output = self.run_order("dispatch", context=jira)
            self.assertEqual(output["context"], {"policy": "required", "dispatch": "delivered", "message": None})
            prompt = create.call_args.args[0]
            self.assertIn("Packet identity: " + jira_handle, prompt)
            self.assertNotIn(self.result["packet_handle"], prompt)
            self.assertNotIn("forged", prompt)
        # Cross-participant identity: the Claude/Codex peer paths render the very same handle.
        current = self.fixture.attach("codex")
        for recipient in ("claude:reviewer", "codex:builder", "devin:builder"):
            peer = self.fixture.delivery(current, recipient)
            self.assertEqual(peer.binding["handle"], jira_handle)
            self.assertIn("Packet identity: " + jira_handle, peer.text)
        # The retry digest binds the Jira packet; a refreshed or absent packet conflicts.
        with patch.object(self.remote, "run", side_effect=Crash()), self.assertRaises(Crash):
            self.run_order("clarify", request="c-1", prose=CANARY, context=jira)
        with patch.object(self.provider, "message", wraps=self.provider.message) as message:
            for context in (None, self.unavailable(), github_packet):
                self.assert_paused(self.run_order("clarify", request="c-1", prose=CANARY, context=context), "message")
            refreshed = fetch(self.fixture.store, "example", self.fixture.spec, backend=self.backend, refresh=True)
            self.assertNotEqual(refreshed["packet_handle"], jira_handle)
            with self.assertRaisesRegex(RemoteError, "request_conflict"):  # Refreshed packet: input differs.
                self.run_order("clarify", request="c-1", prose=CANARY, context=self.bind(handle=refreshed["packet_handle"]))
            self.assert_paused(self.run_order("clarify", request="c-1", prose=CANARY, context=jira), "message")
            self.assertEqual(message.call_count, 0)
        self.assertEqual(self.run_order("status")["context"]["message"], "delivered")
        # A fresh Jira session: PR verification still closes and checks the integer GitHub issue only.
        self.setUp()
        self.order = WorkOrder.from_manifest(manifest, CANARY, **common, context_work_item="EXAMPLE-1")
        self.key = self.service._key(self.order)
        jira_handle = self.fixture.result["packet_handle"]
        jira = self.bind(handle=jira_handle)
        self.run_order("dispatch", context=jira)
        with patch.object(self.provider, "message", wraps=self.provider.message) as message:
            self.assertEqual(self.run_order("clarify", request="c-1", prose=CANARY, context=jira)["round"], 1)
            self.assertIn("Packet identity: " + jira_handle, message.call_args.args[1])
        self.complete(round=1)
        verified = self.run_order("collect")["verified_pr"]
        self.assertEqual((verified["issue"], verified["pr_number"]), (907, 42))
        self.assertNotIn("EXAMPLE-1", json.dumps(verified))
        self.github.pr = replace(self.github.pr, linked_issues=(("owner/repo", 908),))
        with self.assertRaisesRegex(RemoteError, "pull_request_binding_mismatch"):
            self.run_order("collect")

    def test_context_free_orders_keep_their_pre_context_binding_and_input(self):
        legacy = replace(self.order, context_policy="none")
        legacy_fields = {k: v for k, v in asdict(legacy).items() if k not in ("context_policy", "context_work_item")}
        self.assertEqual(self.service._binding(legacy),
                         _hash([legacy_fields, self.provider.name, self.provider.account]))
        self.assertIn(json.dumps({k: v for k, v in legacy_fields.items() if k != "body"}, sort_keys=True)[1:-1],
                      self.service._prompt(legacy))
        self.assertNotIn("context_policy", self.service._prompt(legacy))
        self.assertIn('"context_policy": "required"', self.service._prompt(self.order))
        self.assertNotEqual(self.service._binding(self.order), self.service._binding(legacy))
        self.assertNotEqual(self.service._binding(self.optional_order), self.service._binding(legacy))
        with patch.object(self.provider, "create", wraps=self.provider.create) as create:
            self.service.run("dispatch", legacy, apply=True)
            prompt = create.call_args.args[0]
        # Simulate a record and remote intent written before the context field existed.
        with self.service.store.locked(self.key) as locked:
            record = locked.read()
            del record["context"], record["input"]
            locked.write(record)
        for command in ("dispatch", "status"):
            output = self.service.run(command, legacy, apply=True)
            self.assertEqual(output["context"], {"policy": "none", "dispatch": None, "message": None})
        self.assertEqual(create.call_count, 1)
        output = self.service.run("clarify", legacy, apply=True, request="c-1", prose=CANARY)
        self.assertEqual((output["round"], output["context"]["message"]), (1, "omitted"))
        with self.service.store.locked(self.key) as locked:
            record = locked.read()
            del record["message"]["input"], record["message"]["context"]
            locked.write(record)
        output = self.service.run("clarify", legacy, apply=True, request="c-1", prose=CANARY)
        self.assertEqual((output["round"], output["context"]["message"]), (1, None))
        self.complete(round=1)
        self.assertEqual(self.service.run("collect", legacy, apply=True)["verified_pr"]["pr_number"], 42)
        self.service.run("cancel", legacy, apply=True, request="cancel-1")
        for order in (self.order, self.optional_order):
            with self.assertRaisesRegex(RemoteError, "work_order_binding_mismatch"):
                self.service.run("status", order, apply=True)
        self.assertNotIn("context_policy", prompt)

    def test_combined_input_is_bounded(self):
        self.run_order("dispatch", context=self.context)
        self.complete()
        self.run_order("collect")
        big = self.large()
        with patch.object(self.provider, "message", wraps=self.provider.message) as message:
            with self.assertRaisesRegex(RemoteError, "context_budget_exceeded"):
                self.run_order("fix", request="fix-1", prose=CANARY + "p" * 47_000, reviewed_head=HEAD, context=big)
            self.assertEqual(message.call_count, 0)
        self.assertEqual(self.run_order("status")["round"], 0)  # Oversized input leaves the record unchanged.
        with self.service.store.locked(self.key) as locked:
            record = locked.read()
        self.assertIsNone(record["message"])
        self.assertIsNotNone(record["claim"])
        self.assertEqual(record["requests"], [])
        self.assertEqual(self.run_order("fix", request="fix-1", prose=CANARY, reviewed_head=HEAD,
                                        context=big)["round"], 1)


if __name__ == "__main__":
    unittest.main()
