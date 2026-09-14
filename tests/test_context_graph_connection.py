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

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from code_mower import context_delivery, context_packets, context_prepare, context_session
from code_mower import context_graph_connection as connection
from code_mower import context_graph_lifecycle as lifecycle
from code_mower import context_graph_query as query
from code_mower.context_contract import ContextError, ContextRequest
from code_mower.context_store import ContextStore
from test_context_connections import MemoryVault
from test_context_graph_query import PIN, git, graph_document, indexer, make_repository


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

    def fetch(self, **overrides):
        return context_packets.fetch(self.store, "local-graph", self.spec(**overrides))

    def load(self, handle: str, recipient: str):
        return context_packets.load_authorized(
            self.store, "local-graph", handle, POLICY,
            ContextRequest("owner/repo", "WORK-1", recipient),
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

    def test_attachment_refuses_a_packet_whose_graph_was_rebuilt(self) -> None:
        handle = self.fetch()["packet_handle"]
        self.publish()
        with self.assertRaises(ContextError):
            context_delivery.reserve_attachment(
                self.store, "local-graph", handle, POLICY,
                ContextRequest("owner/repo", "WORK-1", "codex:orchestrator"),
                pr=1, head="c" * 40,
            )


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
