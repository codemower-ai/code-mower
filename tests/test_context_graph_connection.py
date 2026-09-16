"""The guided route over a local repository graph (issue #914).

These tests drive the ordinary guided path -- ``context_prepare.prepare``, the
shared packet store, and ``context_delivery`` -- against a Graphify-kind local
connection rather than an organization one. What they are here to prove is not
the packet format, which ``test_context_graph_query`` already covers, but that
the guided route reaches it: that preparation mints a packet through the shared
store, that reuse does not re-query, that Claude, Codex and Devin receive
byte-identical approved evidence, and that a graph which was rebuilt or whose
revision has moved on is refused rather than replayed.

No Coworker SDK, credential, or network call takes part. The graph document is
written by an injected indexer, as in the query tests, so nothing here claims a
provider was installed.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import chdir, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from code_mower import context_delivery, context_packets, context_prepare, context_session
from code_mower import context_graph_connection as connection
from code_mower import context_graph_lifecycle as lifecycle
from code_mower import context_graph_query as query
from code_mower.context_contract import ContextError, ContextRequest
from code_mower.context_store import ContextStore
from test_context_connections import MemoryVault
from test_context_graph_query import (
    PIN, git, graph_document, indexer, make_repository, wide_graph_document,
)


POLICY = {
    "schema": "code_mower.contextPolicy.v1",
    "connection": "local-graph",
    "policy_version": "v1",
    "required": True,
}
RECIPIENTS = [
    "codex:orchestrator", "claude:orchestrator", "devin:orchestrator",
    "codex:builder", "claude:builder", "devin:builder",
    "codex:reviewer", "claude:reviewer", "devin:reviewer",
]


def session_value(session_id: str = "a" * 32, *, host: str = "codex") -> dict:
    return {
        "schema": "code_mower.session.v1",
        "id": session_id,
        "repo": "owner/repo",
        "host": host,
        "orchestrator": host,
        "participants": [{"id": "claude"}, {"id": "codex"}],
        "lease": {"state": "held", "mutating": True},
    }


@unittest.skipUnless(os.name == "posix", "private context needs POSIX protections")
class GuidedGraphSessionTests(unittest.TestCase):
    """One checkout, one published generation, one guided session over it."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repository = make_repository(self.root)
        # One private root for both the graph's generations and the packet
        # store, which is the arrangement an operator actually gets: the
        # lifecycle and the context store share ``default_context_root``.
        self.private = self.root / "private"
        self.private.mkdir(mode=0o700)
        self.manifest = self.publish()
        self.store = ContextStore(self.private, vault=MemoryVault())
        self.associations = context_session.association_store(self.private)
        connection.connect(self.store, "local-graph", {
            "repository_root": str(self.repository),
            "repositories": ["owner/repo"],
            "recipients": RECIPIENTS,
        })

    def publish(self, document: dict | None = None):
        return lifecycle.build_graph(
            self.repository,
            pin=PIN,
            indexer=indexer(document or graph_document()),
            root=self.private,
        )

    def spec(self, *, question: str = "impact", target: str = "parse_config",
             recipient: str = "codex:orchestrator", required: bool = True) -> dict:
        return {
            "repository": "owner/repo", "work_item": "WORK-1", "recipient": recipient,
            "query": target, "source": question,
            "policy": {**POLICY, "required": required},
        }

    def fetch(self, *, revision: str = "HEAD", **overrides):
        return context_packets.fetch(
            self.store, "local-graph", self.spec(**overrides), revision=revision,
        )

    def load(self, handle: str, recipient: str, revision: str = "HEAD"):
        return context_packets.load_authorized(
            self.store, "local-graph", handle, POLICY,
            ContextRequest("owner/repo", "WORK-1", recipient, revision),
        )

    # -- retrieval through the shared store ------------------------------

    def test_retrieval_binds_the_published_generation_and_the_graphs_commit(self) -> None:
        result = self.fetch()
        self.assertEqual((result["status"], result["reused"]), ("available", False))
        # No paid-provider usage exists for a local graph, and reporting a
        # fabricated zero would read as "a search happened and cost nothing".
        self.assertIsNone(result["usage"])
        packet = self.load(result["packet_handle"], "claude:builder").private_payload()
        self.assertEqual(packet["provider"], connection.PROVIDER)
        self.assertEqual(packet["kind"], "repository")
        self.assertEqual(packet["source_revision"], self.manifest.commit)
        self.assertEqual(packet["binding"]["generation"], self.manifest.generation)
        self.assertEqual(
            packet["binding"]["identity"], {"repository_root": str(self.repository)},
        )

    def test_reuse_returns_the_same_packet_without_traversing_again(self) -> None:
        first = self.fetch()
        again = self.fetch()
        self.assertEqual(again["packet_handle"], first["packet_handle"])
        self.assertTrue(again["reused"])

    def test_a_different_question_is_a_different_packet(self) -> None:
        impact = self.fetch(question="impact")
        symbol = self.fetch(question="symbol")
        self.assertNotEqual(symbol["packet_handle"], impact["packet_handle"])
        self.assertFalse(symbol["reused"])

    def test_an_unsupported_question_is_refused_before_any_traversal(self) -> None:
        with self.assertRaises(ContextError):
            self.fetch(question="everything")

    def test_an_unapproved_recipient_is_refused(self) -> None:
        with self.assertRaises(ContextError):
            self.fetch(recipient="cursor:builder")

    # -- identical approved evidence -------------------------------------

    def test_claude_codex_and_devin_receive_identical_approved_evidence(self) -> None:
        handle = self.fetch()["packet_handle"]
        rendered = {
            host: context_delivery.render_evidence(
                self.load(handle, f"{host}:builder"), handle,
            )
            for host in context_delivery.SUPPORTED_HOSTS
        }
        self.assertEqual(set(rendered), {"claude", "codex", "devin"})
        self.assertEqual(len(set(rendered.values())), 1)
        evidence = rendered["claude"]
        self.assertIn("example_pkg/config.py#L12", evidence)
        # The recipient needs no provider, pin, or graph tool to read this: the
        # evidence names the provider, as it does for an organization packet,
        # and carries no private path, checkout root, or connection identity.
        for private in (str(self.private), str(self.repository), "local-graph"):
            self.assertNotIn(private, evidence)

    # -- freshness: rebuilt, moved, or absent ----------------------------

    def test_a_rebuilt_graph_refuses_the_packet_bound_to_the_old_generation(self) -> None:
        handle = self.fetch()["packet_handle"]
        republished = self.publish()
        self.assertNotEqual(republished.generation, self.manifest.generation)
        with self.assertRaises(ContextError):
            self.load(handle, "claude:builder")

    def test_a_moved_head_refuses_delivery_rather_than_answering_for_the_old_commit(self) -> None:
        handle = self.fetch()["packet_handle"]
        (self.repository / "example_pkg" / "config.py").write_text("changed\n", encoding="utf-8")
        git(self.repository, "add", ".")
        git(self.repository, "commit", "-q", "-m", "second")
        with self.assertRaises(ContextError):
            self.load(handle, "claude:builder")

    def test_an_explicitly_named_prior_revision_still_loads_its_own_packet(self) -> None:
        """Freshness is about the requested revision, not about wall-clock time."""
        handle = self.fetch()["packet_handle"]
        (self.repository / "example_pkg" / "config.py").write_text("changed\n", encoding="utf-8")
        git(self.repository, "add", ".")
        git(self.repository, "commit", "-q", "-m", "second")
        packet = context_packets.load_authorized(
            self.store, "local-graph", handle, POLICY,
            ContextRequest("owner/repo", "WORK-1", "claude:builder"),
            revision=self.manifest.commit,
        )
        self.assertEqual(packet.private_payload()["source_revision"], self.manifest.commit)

    def _second_commit_the_checkout_is_not_on(self) -> str:
        """Make a commit, then leave the registered checkout back on the first.

        This is the arrangement the consuming-revision rule exists for: the
        connected checkout still sits at the commit its graph was built from,
        while the work consuming the evidence is at another commit entirely --
        a builder's branch, a PR head, a worktree. Both commits are real and
        resolvable, so nothing here fails for want of an object.
        """
        first = lifecycle.resolve_revision(self.repository)[0]
        (self.repository / "example_pkg" / "config.py").write_text("changed\n", encoding="utf-8")
        git(self.repository, "add", ".")
        git(self.repository, "commit", "-q", "-m", "consuming work")
        second = lifecycle.resolve_revision(self.repository)[0]
        git(self.repository, "reset", "-q", "--hard", first)
        self.assertEqual(lifecycle.resolve_revision(self.repository)[0], self.manifest.commit)
        self.assertNotEqual(second, self.manifest.commit)
        return second

    def test_retrieval_refuses_a_graph_that_is_not_the_consuming_revisions(self) -> None:
        """The registered checkout's ``HEAD`` is not the revision being worked on."""
        consuming = self._second_commit_the_checkout_is_not_on()
        with self.assertRaises(ContextError):
            self.fetch(revision=consuming)

    def test_replay_refuses_a_packet_for_another_revisions_code(self) -> None:
        handle = self.fetch()["packet_handle"]
        consuming = self._second_commit_the_checkout_is_not_on()
        # The checkout's own HEAD still authorizes, which is exactly why the
        # consuming revision has to be the one asked about.
        self.assertEqual(self.load(handle, "claude:builder").revision_state, "matching")
        with self.assertRaises(ContextError):
            self.load(handle, "claude:builder", revision=consuming)

    def test_a_load_that_cannot_name_its_consuming_revision_is_refused(self) -> None:
        handle = self.fetch()["packet_handle"]
        with self.assertRaises(ContextError):
            context_packets.load_authorized(
                self.store, "local-graph", handle, POLICY,
                ContextRequest("owner/repo", "WORK-1", "claude:builder"),
            )

    def test_removing_the_graph_makes_the_connection_unavailable(self) -> None:
        handle = self.fetch()["packet_handle"]
        lifecycle.remove_graph(self.repository, root=self.private)
        with self.assertRaises(ContextError):
            self.load(handle, "claude:builder")

    def test_disconnecting_disables_the_connection_and_drops_its_packets(self) -> None:
        handle = self.fetch()["packet_handle"]
        summary = connection.disconnect(self.store, "local-graph")
        self.assertEqual((summary["status"], summary["packet_cleanup"]), ("disconnected", "complete"))
        with self.assertRaises(ContextError):
            self.load(handle, "claude:builder")

    def _reconnect(self, **overrides) -> dict:
        spec = {
            "repository_root": str(self.repository),
            "repositories": ["owner/repo"],
            "recipients": RECIPIENTS,
        }
        spec.update(overrides)
        return connection.connect(self.store, "local-graph", spec)

    def test_reconnect_completes_pending_packet_cleanup_that_disconnect_could_not(self) -> None:
        """A cleanup failure on disconnect must not let reconnect skip it.

        Reproduces codex:57555c67a42d8aeba514: previously ``connect`` wrote
        ``verified`` state straight back once a disconnected connection was
        found, regardless of whether the packets it once authorized were ever
        actually purged. With the same graph and approved scope, that let a
        surviving packet and its delivery/attachment binding become authorized
        again.
        """
        handle = self.fetch()["packet_handle"]
        binding = self._attach(handle, self.manifest.commit, consuming_revision=self.manifest.commit)
        with patch("code_mower.context_packets.purge_connection", side_effect=RuntimeError("boom")):
            summary = connection.disconnect(self.store, "local-graph")
            self.assertEqual((summary["status"], summary["packet_cleanup"]), ("disconnected", "needs_attention"))
            with self.assertRaises(ContextError):
                self._reconnect()
        # Cleanup is still pending: the connection stays disconnected, and
        # neither the surviving packet nor its attachment binding is usable.
        with self.store.locked("local-graph") as locked:
            self.assertEqual(connection.saved_state(locked.read(), "local-graph")["state"], "disconnected")
        with self.assertRaises(ContextError):
            self.load(handle, "claude:builder")
        with self.assertRaises(ContextError):
            context_delivery.read_binding(self.store, binding["revision"])
        # Cleanup now succeeds: reconnect completes it and only then verifies.
        self.assertEqual(self._reconnect()["status"], "verified")
        with self.assertRaises(ContextError):
            self.load(handle, "claude:builder")
        with self.assertRaises(ContextError):
            context_delivery.read_binding(self.store, binding["revision"])

    def test_connection_status_reports_the_graph_without_minting_evidence(self) -> None:
        report = connection.status(self.store, "local-graph", root=self.private)
        self.assertEqual(report["provider"], connection.PROVIDER)
        self.assertEqual(report["authorization"], "available")
        self.assertTrue(report["graph"]["usable"])

    def test_authorization_names_a_repository_kind_without_an_account(self) -> None:
        with self.store.locked("local-graph") as locked:
            envelope = connection.authorize_locked(
                locked, "local-graph", root=self.private, now=datetime.now(timezone.utc),
            )
        self.assertEqual(envelope["kind"], "repository")
        self.assertEqual(envelope["generation"], self.manifest.generation)
        self.assertEqual(
            envelope["capabilities"],
            {"search": True, "memory": False, "revision_binding": True},
        )
        self.assertNotIn("principal", envelope["identity"])

    # -- the guided session verbs ----------------------------------------

    def record(self, *, host: str = "codex", required: bool = True):
        return context_session.create(
            self.associations,
            session_value(host=host),
            work_item="WORK-1",
            policy={**POLICY, "required": required},
        )

    def prepare(self, record, **kwargs):
        arguments = {"query": "parse_config", "source": "impact"}
        arguments.update(kwargs)
        return context_prepare.prepare(
            self.associations,
            record,
            repo_root=self.repository,
            context_root=self.private,
            packet_store=self.store,
            **arguments,
        )

    def test_prepare_then_reuse_produces_one_packet_the_builder_can_read(self) -> None:
        record = self.record()
        first, code = self.prepare(record)
        self.assertEqual((code, first["status"], first["reused"]), (0, "prepared", False))
        saved = context_session.read(self.associations, record["session_id"])
        self.assertEqual(saved["stage"], "prepared")
        self.assertIsNotNone(saved["packet"])

        again, code = self.prepare(saved)
        self.assertEqual((code, again["status"], again["reused"]), (0, "prepared", True))
        self.assertEqual(
            context_session.read(self.associations, record["session_id"])["packet"],
            saved["packet"],
        )
        evidence = context_delivery.render_evidence(
            self.load(saved["packet"], "codex:builder"), saved["packet"],
        )
        self.assertIn("example_pkg/config.py#L12", evidence)

    def test_prepare_pauses_required_work_when_the_graph_is_stale(self) -> None:
        record = self.record(required=True)
        lifecycle.remove_graph(self.repository, root=self.private)
        report, code = self.prepare(record)
        self.assertEqual((code, report["status"]), (1, "required_unavailable"))
        self.assertEqual(report["dependent_work"], "paused")

    def test_prepare_degrades_optional_work_when_the_graph_is_stale(self) -> None:
        record = self.record(required=False)
        lifecycle.remove_graph(self.repository, root=self.private)
        report, code = self.prepare(record)
        self.assertEqual((code, report["status"]), (0, "optional_unavailable"))
        self.assertEqual(report["dependent_work"], "usable")

    def test_reuse_after_a_rebuild_pauses_required_work(self) -> None:
        record = self.record(required=True)
        self.prepare(record)
        saved = context_session.read(self.associations, record["session_id"])
        self.publish()
        report, code = self.prepare(saved)
        self.assertEqual((code, report["status"]), (1, "required_unavailable"))

    # -- a wide answer under the default document budget -------------------

    def test_a_wide_answer_is_prepared_bounded_rather_than_refused_at_delivery(self) -> None:
        """Six citable relationships, a budget of five, one usable packet.

        The default policy carries no ``max_documents``, so the contract's own
        default of five applies -- and the contract enforces it when the packet
        is loaded. A packet built to the query adapter's ceiling instead would
        pass preparation and then fail every authorized load, which is a
        required session paused over evidence the graph actually had. What the
        builder must get is the bounded answer, told plainly that it is bounded.
        """
        self.manifest = self.publish(wide_graph_document(6))
        record = self.record()
        report, code = self.prepare(record)
        self.assertEqual((code, report["status"], report["dependent_work"]), (0, "prepared", "usable"))

        saved = context_session.read(self.associations, record["session_id"])
        loaded = self.load(saved["packet"], "claude:builder")
        packet = loaded.private_payload()
        self.assertEqual(len(packet["documents"]), 5)
        self.assertTrue(packet["truncated"])
        self.assertEqual(packet["completeness"], "partial")
        self.assertIn("document_limit", packet["omissions"])
        self.assertEqual(packet["binding"]["generation"], self.manifest.generation)
        # The builder reads it as evidence, not as a handle it cannot open.
        evidence = context_delivery.render_evidence(loaded, saved["packet"])
        self.assertIn("example_pkg/config.py#L12", evidence)

    def test_replaying_a_bounded_packet_keeps_its_omissions_and_binding(self) -> None:
        self.manifest = self.publish(wide_graph_document(6))
        record = self.record()
        self.prepare(record)
        saved = context_session.read(self.associations, record["session_id"])
        again, code = self.prepare(saved)
        self.assertEqual((code, again["status"], again["reused"]), (0, "prepared", True))
        self.assertEqual(
            context_session.read(self.associations, record["session_id"])["packet"],
            saved["packet"],
        )
        first = self.load(saved["packet"], "claude:builder").private_payload()
        replayed = self.load(saved["packet"], "codex:builder").private_payload()
        self.assertEqual(replayed["omissions"], first["omissions"])
        self.assertEqual(replayed["truncated"], first["truncated"])
        self.assertEqual(replayed["binding"], first["binding"])
        self.assertEqual(len(replayed["documents"]), 5)

    def test_an_optional_wide_answer_is_delivered_rather_than_degraded(self) -> None:
        """A budget is not unavailability: optional work gets the evidence too."""
        self.manifest = self.publish(wide_graph_document(6))
        record = self.record(required=False)
        report, code = self.prepare(record)
        self.assertEqual((code, report["status"]), (0, "prepared"))
        saved = context_session.read(self.associations, record["session_id"])
        self.assertEqual(
            len(self.load(saved["packet"], "claude:builder").private_payload()["documents"]), 5,
        )

    def _attach(self, handle: str, head: str, *, consuming_revision=None):
        return context_delivery.reserve_attachment(
            self.store, "local-graph", handle, POLICY,
            ContextRequest("owner/repo", "WORK-1", "codex:orchestrator"),
            pr=1, head=head, consuming_revision=consuming_revision,
        )

    def test_attachment_refuses_a_packet_whose_graph_was_rebuilt(self) -> None:
        handle = self.fetch()["packet_handle"]
        self.publish()
        with self.assertRaises(ContextError):
            self._attach(handle, self.manifest.commit, consuming_revision=self.manifest.commit)

    def test_attachment_succeeds_when_packet_pr_and_consumer_all_match(self) -> None:
        handle = self.fetch()["packet_handle"]
        # The head the graph *is* for, and the checkout it is actually on, both attach.
        self.assertEqual(
            self._attach(handle, self.manifest.commit, consuming_revision=self.manifest.commit)["head"],
            self.manifest.commit,
        )

    def test_attachment_refuses_a_pull_request_head_that_differs_from_the_packets_revision(self) -> None:
        """Packet A versus PR B is refused even though the consumer is A."""
        handle = self.fetch()["packet_handle"]
        consuming = self._second_commit_the_checkout_is_not_on()
        with self.assertRaises(ContextError):
            self._attach(handle, consuming, consuming_revision=self.manifest.commit)

    def test_attachment_refuses_a_consumer_the_checkout_is_not_actually_on(self) -> None:
        """A checkout at another commit must not enable a binding for this PR head."""
        handle = self.fetch()["packet_handle"]
        consuming = self._second_commit_the_checkout_is_not_on()
        with self.assertRaises(ContextError):
            self._attach(handle, self.manifest.commit, consuming_revision=consuming)

    def test_attachment_never_substitutes_the_pr_head_for_an_unknown_consumer(self) -> None:
        """A caller that cannot name its consuming revision (e.g. non-Git) fails closed."""
        handle = self.fetch()["packet_handle"]
        with self.assertRaises(ContextError):
            self._attach(handle, self.manifest.commit)


@unittest.skipUnless(os.name == "posix", "private context needs POSIX protections")
class StandaloneGraphFetchCommandTests(unittest.TestCase):
    """``code-mower context fetch`` run outside a guided session (issue #914).

    The guided route reads the consuming checkout's revision itself, from the
    directory the session is preparing work in. The standalone command has no
    session record to read it from, so what is proven here is that it reads that
    revision from the checkout it was *run in*, rather than leaving ``fetch`` to
    resolve the word ``HEAD`` in whichever checkout the connection happens to
    have been registered against. Those are two directories that move
    independently, and the second reading is exactly how evidence describing one
    commit's code reaches work on another.

    The real entrypoint runs. Only the store root is injected -- the command
    otherwise builds its own -- so the packet these tests read back came through
    the same protected file, the same index, and the same authorization a
    guided delivery goes through.
    """

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repository = make_repository(self.root)
        self.private = self.root / "private"
        self.private.mkdir(mode=0o700)
        self.manifest = lifecycle.build_graph(
            self.repository, pin=PIN, indexer=indexer(graph_document()), root=self.private,
        )
        self.store = ContextStore(self.private, vault=MemoryVault())
        connection.connect(self.store, "local-graph", {
            "repository_root": str(self.repository),
            "repositories": ["owner/repo"],
            "recipients": RECIPIENTS,
        })

    def run_command(self, cwd: Path, *, required: bool = True) -> tuple[int, dict]:
        """Drive the real command from ``cwd``. No provider backend may load."""
        spec = {
            "repository": "owner/repo", "work_item": "WORK-1", "recipient": "claude:builder",
            "query": "parse_config", "source": "impact",
            "policy": {**POLICY, "required": required},
        }
        output = io.StringIO()
        with patch("code_mower.context_packets.ContextStore", return_value=self.store), \
                patch("code_mower.context_packets._backend",
                      side_effect=AssertionError("a local graph must not reach the provider SDK")), \
                patch("sys.stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(spec).encode()))), \
                chdir(cwd), redirect_stdout(output):
            code = context_packets.main(["--connection", "local-graph", "--request-stdin", "--json"])
        return code, json.loads(output.getvalue())

    def _another_checkout_at_a_commit_this_one_is_not_on(self) -> Path:
        """A second checkout at a commit the registered one has moved off.

        The clone is taken while the second commit is current, so that checkout
        keeps it; the registered checkout is then put back on the commit its
        graph was built from. The object stays reachable there, so the refusal
        under test is a real comparison of two resolvable commits rather than a
        name the graph's repository could not look up at all.
        """
        first = lifecycle.resolve_revision(self.repository)[0]
        (self.repository / "example_pkg" / "config.py").write_text("changed\n", encoding="utf-8")
        git(self.repository, "add", ".")
        git(self.repository, "commit", "-q", "-m", "consuming work")
        second = lifecycle.resolve_revision(self.repository)[0]
        consuming = self.root / "consuming"
        git(self.root, "clone", "-q", str(self.repository), str(consuming))
        git(self.repository, "reset", "-q", "--hard", first)
        self.assertEqual(lifecycle.resolve_revision(self.repository)[0], self.manifest.commit)
        self.assertEqual(lifecycle.resolve_revision(consuming)[0], second)
        self.assertNotEqual(second, self.manifest.commit)
        return consuming

    def test_running_in_the_graphs_own_checkout_delivers_its_packet(self) -> None:
        code, report = self.run_command(self.repository)
        self.assertEqual((code, report["status"]), (0, "available"))
        packet = context_packets.load_authorized(
            self.store, "local-graph", report["packet_handle"], POLICY,
            ContextRequest("owner/repo", "WORK-1", "claude:builder", self.manifest.commit),
        )
        self.assertEqual(packet.private_payload()["source_revision"], self.manifest.commit)
        self.assertEqual(packet.private_payload()["binding"]["generation"], self.manifest.generation)

    def test_a_consumer_at_another_revision_is_refused_rather_than_answered(self) -> None:
        consuming = self._another_checkout_at_a_commit_this_one_is_not_on()
        code, report = self.run_command(consuming)
        self.assertEqual((code, report["status"]), (1, "required_unavailable"))
        # Refused at authorization, before anything is reserved: no packet of
        # the wrong commit's evidence exists to be replayed later.
        self.assertEqual(list(self.private.glob(".p-*.json")), [])
        # And the discrimination is the consuming revision alone. The registered
        # checkout still sits at the graph's commit, so a command that resolved
        # ``HEAD`` there would have answered the request above as it answers
        # this one.
        self.assertEqual(self.run_command(self.repository)[1]["status"], "available")

    def test_an_optional_consumer_at_another_revision_degrades_instead_of_pausing(self) -> None:
        consuming = self._another_checkout_at_a_commit_this_one_is_not_on()
        code, report = self.run_command(consuming, required=False)
        self.assertEqual((code, report["status"]), (0, "optional_unavailable"))
        self.assertEqual(list(self.private.glob(".p-*.json")), [])

    def _publish_wide(self, callers: int = 6):
        """Republish this checkout's graph with ``callers`` citable relationships."""
        self.manifest = lifecycle.build_graph(
            self.repository, pin=PIN, indexer=indexer(wide_graph_document(callers)),
            root=self.private,
        )
        return self.manifest

    def test_a_wide_answer_is_delivered_bounded_instead_of_failing_validation(self) -> None:
        """Six relationships, the contract's default budget of five, one packet.

        Before the document budget reached the traversal, the command built six
        documents and the shared contract refused them on the way into the
        store: ``packet_invalid``, required work paused, over evidence the graph
        had and the policy simply did not have room for. The answer is five
        documents that say they are five of more, and no validation failure.
        """
        manifest = self._publish_wide(6)
        code, report = self.run_command(self.repository)
        self.assertEqual((code, report["status"]), (0, "available"))
        self.assertNotEqual(report.get("reason"), "packet_invalid")
        self.assertNotIn("failure_reason", report)
        self.assertEqual(report["documents"], 5)
        self.assertEqual(report["completeness"], "partial")
        self.assertTrue(report["truncated"])

        packet = context_packets.load_authorized(
            self.store, "local-graph", report["packet_handle"], POLICY,
            ContextRequest("owner/repo", "WORK-1", "claude:builder", manifest.commit),
        ).private_payload()
        self.assertEqual(len(packet["documents"]), 5)
        self.assertIn("document_limit", packet["omissions"])
        self.assertEqual(packet["binding"]["generation"], manifest.generation)
        # Every delivered citation still points at the immutable tree.
        self.assertTrue(all(
            citation["source"].startswith(("example_pkg/", "tests/"))
            for item in packet["documents"] for citation in item["citations"]
        ))

    def test_an_optional_wide_answer_is_delivered_rather_than_degraded(self) -> None:
        """A budget is not unavailability: the optional caller gets evidence too."""
        self._publish_wide(6)
        code, report = self.run_command(self.repository, required=False)
        self.assertEqual((code, report["status"]), (0, "available"))
        self.assertNotEqual(report.get("reason"), "packet_invalid")
        self.assertEqual(report["documents"], 5)
        self.assertTrue(report["truncated"])

    def test_an_answer_that_fits_the_budget_exactly_is_still_complete(self) -> None:
        """The bound is only reported when it actually left evidence out."""
        self._publish_wide(5)
        code, report = self.run_command(self.repository)
        self.assertEqual((code, report["status"]), (0, "available"))
        self.assertEqual(report["documents"], 5)
        self.assertEqual(report["completeness"], "complete")
        self.assertFalse(report["truncated"])

    def test_a_consumer_that_is_not_a_checkout_is_refused_rather_than_defaulted(self) -> None:
        """No revision at all is a refusal, not a fall back to the graph's."""
        elsewhere = self.root / "not-a-checkout"
        elsewhere.mkdir()
        code, report = self.run_command(elsewhere)
        self.assertEqual((code, report["status"]), (1, "required_unavailable"))
        self.assertEqual(list(self.private.glob(".p-*.json")), [])
        code, report = self.run_command(elsewhere, required=False)
        self.assertEqual((code, report["status"]), (0, "optional_unavailable"))


@unittest.skipUnless(os.name == "posix", "private context needs POSIX protections")
class GraphConnectionStateTests(unittest.TestCase):
    """What the saved connection will and will not accept."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.private = self.root / "private"
        self.private.mkdir(mode=0o700)
        self.repository = make_repository(self.root)
        self.store = ContextStore(self.private, vault=MemoryVault())

    def connect(self, **overrides):
        spec = {
            "repository_root": str(self.repository),
            "repositories": ["owner/repo"],
            "recipients": ["claude:builder"],
        }
        spec.update(overrides)
        return connection.connect(self.store, "local-graph", spec)

    def test_connecting_stores_no_credential_and_reports_no_vault(self) -> None:
        summary = self.connect()
        self.assertEqual(summary["credential_storage"], "none")
        self.assertEqual(summary["kind"], "repository")

    def test_a_relative_checkout_is_refused(self) -> None:
        with self.assertRaises(ContextError):
            self.connect(repository_root="checkout")

    def test_reconnecting_a_live_connection_is_refused(self) -> None:
        self.connect()
        with self.assertRaises(ContextError):
            self.connect(repositories=["owner/other"])

    def test_reconnecting_after_disconnect_is_allowed(self) -> None:
        self.connect()
        connection.disconnect(self.store, "local-graph")
        self.assertEqual(self.connect(repositories=["owner/other"])["status"], "verified")

    def test_the_question_defaults_to_symbol_and_rejects_anything_else(self) -> None:
        self.assertEqual(
            connection.question_and_target({"query": "parse_config", "source": None}),
            (connection.DEFAULT_QUESTION, "parse_config"),
        )
        self.assertIn(connection.DEFAULT_QUESTION, query.QUESTIONS)
        with self.assertRaises(ContextError):
            connection.question_and_target({"query": "parse_config", "source": "anything"})

    def test_saved_state_refuses_another_providers_connection(self) -> None:
        self.assertFalse(connection.is_graph({"schema": "code_mower.contextLocalConnection.v1"}))
        with self.assertRaises(ContextError):
            connection.saved_state({"schema": connection.GRAPH_SCHEMA}, "local-graph")


if __name__ == "__main__":  # pragma: no cover - direct invocation
    unittest.main()
