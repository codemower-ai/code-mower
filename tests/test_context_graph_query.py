"""Offline tests for bounded local-graph queries and packets (issue #914).

Every test builds a real throwaway Git repository, publishes a generation
through the #913 lifecycle with an injected indexer, and asks the resulting
graph a question. No graph package is installed, imported, or required: the
graph document is written by the injected indexer, so what is proved here is
what this repository is responsible for -- how a traversal is bounded, what a
packet binds, which citations survive validation against the immutable tree,
and what happens when the graph cannot answer.

The graph content is synthetic, as in ``tests/fixtures/local_graph_contract.json``.
A passing suite is not evidence that any provider emits this shape.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import subprocess
import tarfile
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from code_mower import context_contract as contract
from code_mower import context_delivery
from code_mower import context_graph
from code_mower import context_graph_command as command
from code_mower import context_graph_lifecycle as lifecycle
from code_mower import context_graph_query as query
from code_mower.context_contract import ContextError


NOW = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
PIN = lifecycle.GraphifyPin(distribution="graphifyy", version="0.9.58", wheel_sha256="a" * 64)

#: Line counts the synthetic graph cites into. Every span below is inside them,
#: except where a test deliberately claims past the end of a file.
SOURCES = {
    "example_pkg/config.py": 40,
    "example_pkg/loader.py": 50,
    "example_pkg/report.py": 20,
    "tests/test_config.py": 30,
}


def git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        env={
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(repository),
        },
    )


def make_repository(root: Path) -> Path:
    repository = root / "checkout"
    repository.mkdir()
    git(repository, "init", "-q", "-b", "main")
    for relative, lines in SOURCES.items():
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(f"line {n}" for n in range(1, lines + 1)) + "\n", encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "commit", "-q", "-m", "initial")
    return repository


def node(identifier: str, kind: str, name: str, path: str, start=None, end=None) -> dict:
    return {"id": identifier, "kind": kind, "name": name, "path": path,
            "start_line": start, "end_line": end}


def edge(source: str, target: str, kind: str, evidence: str = "extracted") -> dict:
    return {"source": source, "target": target, "kind": kind, "evidence": evidence}


def graph_document() -> dict:
    """A small synthetic graph: a symbol, its caller, its caller's caller, a test."""
    return {
        "schema": query.GRAPH_SCHEMA,
        "nodes": [
            node("n-config", "symbol", "parse_config", "example_pkg/config.py", 12, 30),
            node("n-load", "symbol", "load", "example_pkg/loader.py", 40, 44),
            node("n-report", "symbol", "render", "example_pkg/report.py", 5, 12),
            node("n-test", "test", "test_parse_config", "tests/test_config.py", 8, 26),
            node("n-config-file", "file", "config.py", "example_pkg/config.py"),
        ],
        "edges": [
            edge("n-load", "n-config", "calls"),
            edge("n-report", "n-load", "calls", "inferred"),
            edge("n-test", "n-config", "tests"),
            edge("n-config-file", "n-config", "defines"),
        ],
    }


def artifact(document: dict) -> bytes:
    """The generation artifact: the provider's state, packed as the lifecycle packs it."""
    body = json.dumps(document).encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        info = tarfile.TarInfo(query.GRAPH_MEMBER)
        info.size = len(body)
        info.mtime = 0
        info.mode = 0o600
        archive.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


def indexer(document: dict, *, completeness: str = lifecycle.COMPLETE):
    payload = artifact(document)

    def run(request: lifecycle.IndexRequest) -> lifecycle.IndexResult:
        request.output_path.write_bytes(payload)
        return lifecycle.IndexResult(completeness=completeness, indexed_files=len(SOURCES))

    return run


def envelope(**overrides) -> dict:
    value = {
        "schema": contract.CONNECTION_SCHEMA,
        "capability_version": 1,
        "connection": "example-context",
        "provider": "synthetic-graph",
        "kind": "repository",
        "generation": "generation-one",
        "state": "verified",
        "identity": {"repository_root": "/example/repository"},
        "repositories": ["owner/repo"],
        "recipients": ["claude:builder", "codex:reviewer", "devin:builder"],
        "expires_at": "2026-01-01T13:00:00Z",
        "capabilities": {"search": True, "memory": False, "revision_binding": True},
    }
    value.update(overrides)
    return value


def policy(**overrides) -> dict:
    value = {
        "schema": contract.POLICY_SCHEMA,
        "connection": "example-context",
        "policy_version": "v1",
        "required": True,
    }
    value.update(overrides)
    return value


class GraphWorkspace(unittest.TestCase):
    """A published generation over a real commit, with an injected graph."""

    document: dict = {}
    completeness = lifecycle.COMPLETE

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name).resolve()
        self.state = self.root / "state"
        self.repository = make_repository(self.root)
        self.manifest = self.publish(self.document or graph_document())

    def publish(self, document: dict, *, completeness: str | None = None):
        return lifecycle.build_graph(
            self.repository,
            pin=PIN,
            indexer=indexer(document, completeness=completeness or self.completeness),
            root=self.state,
            now=NOW,
        )

    def context(self, **overrides) -> query.GraphContext:
        arguments = {
            "question": "impact",
            "target": "parse_config",
            "envelope": envelope(),
            "policy": policy(),
            "context_repository": "owner/repo",
            "work_item": "work-item-one",
            "root": self.state,
            "now": NOW,
        }
        arguments.update(overrides)
        return query.graph_context(self.repository, **arguments)

    def graph(self) -> query.CodeGraph:
        state = lifecycle.GraphStateRoot(self.repository, root=self.state)
        return query.read_graph(state, lifecycle.graph_status(self.repository, root=self.state))

    def load(self, packet: dict, *, recipient: str, revision: str | None):
        """Load one packet through the shared delivery contract, as a recipient does."""
        encoded = json.dumps(packet).encode()
        target = self.root / "delivered-packet.json"
        target.write_bytes(encoded)
        target.chmod(0o600)
        return contract.load_packet(
            private_root=self.root,
            reference={"path": "delivered-packet.json", "sha256": hashlib.sha256(encoded).hexdigest()},
            policy=policy(),
            request=contract.ContextRequest("owner/repo", "work-item-one", recipient, revision),
            authorize=lambda: envelope(),
            now=NOW,
        )


class GraphSchemaTests(unittest.TestCase):
    """The pinned schema is read strictly: an unreadable shape is a refusal."""

    def load(self, document: dict) -> query.CodeGraph:
        return query.load_graph(document, generation="a" * 32, commit="b" * 40)

    def test_reads_the_pinned_schema(self) -> None:
        graph = self.load(graph_document())
        self.assertEqual(len(graph.nodes), 5)
        self.assertEqual(graph.nodes["n-config"].citation, "example_pkg/config.py#L12-L30")
        self.assertEqual(graph.nodes["n-config-file"].citation, "example_pkg/config.py")

    def test_rejects_another_schema(self) -> None:
        document = graph_document()
        document["schema"] = "graphify.native.v1"
        with self.assertRaises(ContextError):
            self.load(document)

    def test_rejects_unknown_node_and_edge_kinds(self) -> None:
        for mutate in (
            lambda doc: doc["nodes"][0].update(kind="cluster"),
            lambda doc: doc["edges"][0].update(kind="resembles"),
            lambda doc: doc["edges"][0].update(evidence="guessed"),
        ):
            with self.subTest(mutate=mutate):
                document = graph_document()
                mutate(document)
                with self.assertRaises(ContextError):
                    self.load(document)

    def test_rejects_a_node_outside_the_indexed_checkout(self) -> None:
        """A node that could never be cited must not be traversable either."""
        for path in ("/etc/passwd", "../sibling/config.py", ".git/config", ".graphify/nodes.bin"):
            with self.subTest(path=path):
                document = graph_document()
                document["nodes"][0]["path"] = path
                with self.assertRaises(ContextError):
                    self.load(document)

    def test_rejects_a_dangling_edge(self) -> None:
        document = graph_document()
        document["edges"].append(edge("n-config", "n-missing", "calls"))
        with self.assertRaises(ContextError):
            self.load(document)

    def test_rejects_duplicate_node_identifiers(self) -> None:
        document = graph_document()
        document["nodes"].append(dict(document["nodes"][0]))
        with self.assertRaises(ContextError):
            self.load(document)

    def test_rejects_an_inverted_line_span(self) -> None:
        document = graph_document()
        document["nodes"][0].update(start_line=30, end_line=12)
        with self.assertRaises(ContextError):
            self.load(document)

    def test_rejects_unrecognized_fields(self) -> None:
        document = graph_document()
        document["nodes"][0]["cluster"] = "semantic"
        with self.assertRaises(ContextError):
            self.load(document)


class TraversalTests(GraphWorkspace):
    def query(self, **overrides) -> query.QueryResult:
        arguments = {"question": "impact", "target": "parse_config"}
        arguments.update(overrides)
        return query.run_query(self.graph(), **arguments)

    def test_impact_walks_against_the_relationships(self) -> None:
        result = self.query()
        reached = {item.node.name for item in result.relations}
        self.assertEqual(reached, {"load", "test_parse_config", "render"})
        self.assertFalse(result.truncated)

    def test_dependency_walks_along_them(self) -> None:
        """``load`` depends on ``parse_config``; ``parse_config`` depends on nothing."""
        reached = self.query(target="load", question="dependency").relations
        self.assertEqual({item.node.name for item in reached}, {"parse_config"})
        self.assertEqual(self.query(question="dependency").relations, ())

    def test_related_tests_answers_with_tests_only(self) -> None:
        result = self.query(question="related_tests")
        self.assertEqual([item.node.name for item in result.relations], ["test_parse_config"])

    def test_symbol_is_a_one_hop_neighbourhood(self) -> None:
        result = self.query(question="symbol")
        self.assertEqual({item.depth for item in result.relations}, {1})
        self.assertEqual({item.node.name for item in result.relations},
                         {"load", "test_parse_config", "config.py"})

    def test_traversal_is_deterministic(self) -> None:
        first = [item.node.id for item in self.query().relations]
        second = [item.node.id for item in query.run_query(
            self.graph(), question="impact", target="parse_config").relations]
        self.assertEqual(first, second)

    def test_budget_truncates_and_says_so(self) -> None:
        result = self.query(node_budget=1)
        self.assertEqual(len(result.relations), 1)
        self.assertTrue(result.truncated)
        self.assertIn("provider_has_more", result.omissions)

    def test_depth_bounds_the_walk(self) -> None:
        """``render`` is two hops from ``parse_config`` and out of a one-hop walk."""
        self.assertNotIn("render", {item.node.name for item in self.query(depth=1).relations})

    def test_an_unresolved_target_is_reported_rather_than_guessed(self) -> None:
        result = self.query(target="no_such_symbol")
        self.assertFalse(result.resolved)
        self.assertEqual(result.omissions, ("unresolved_entities",))

    def test_out_of_range_budget_and_depth_are_refused(self) -> None:
        for arguments in ({"node_budget": 0}, {"node_budget": query.MAX_NODE_BUDGET + 1},
                          {"depth": 0}, {"depth": query.MAX_DEPTH + 1}):
            with self.subTest(arguments=arguments):
                with self.assertRaises(ContextError):
                    self.query(**arguments)

    def test_an_unsupported_question_is_refused(self) -> None:
        with self.assertRaises(ContextError):
            self.query(question="everything")


class CitationValidationTests(GraphWorkspace):
    def validator(self) -> query.CitationValidator:
        census = lifecycle.read_tracked_census(self.repository, self.manifest.commit)
        return query.CitationValidator(self.repository, census)

    def test_validates_line_claims_against_the_bound_commit(self) -> None:
        validator = self.validator()
        self.assertTrue(validator.validate("example_pkg/config.py#L12-L30"))
        self.assertTrue(validator.validate("example_pkg/config.py#L40"))
        self.assertFalse(validator.validate("example_pkg/config.py#L41"))

    def test_an_untracked_path_is_never_cited(self) -> None:
        (self.repository / "example_pkg" / "scratch.py").write_text("x\n", encoding="utf-8")
        self.assertFalse(self.validator().validate("example_pkg/scratch.py#L1"))

    def test_working_tree_edits_do_not_change_the_verdict(self) -> None:
        """The generation binds a commit; the file on disk today is not evidence."""
        (self.repository / "example_pkg" / "config.py").write_text("one line\n", encoding="utf-8")
        self.assertTrue(self.validator().validate("example_pkg/config.py#L40"))

    def test_an_out_of_scope_citation_is_refused(self) -> None:
        for source in ("../escape.py", "/etc/passwd", ".git/config"):
            with self.subTest(source=source):
                self.assertFalse(self.validator().validate(source))


class PacketTests(GraphWorkspace):
    def test_packet_carries_its_provenance_and_loads_through_the_contract(self) -> None:
        outcome = self.context()
        self.assertEqual(outcome.status, query.AVAILABLE)
        packet = outcome.packet
        self.assertEqual(packet["kind"], "repository")
        self.assertEqual(packet["source_revision"], self.manifest.commit)
        self.assertEqual(packet["source_built_at"], self.manifest.built_at)
        self.assertEqual(outcome.summary["graph_generation"], self.manifest.generation)
        validated = self.load(packet, recipient="claude:builder", revision=self.manifest.commit)
        self.assertEqual(validated.revision_state, "matching")

    def test_every_citation_resolves_against_the_immutable_tree(self) -> None:
        outcome = self.context()
        report = context_graph.evaluate_graph_evidence(
            outcome.packet, repository_root=self.repository, revision_state="matching",
        )
        self.assertEqual(report.resolution_rate, 1.0)
        self.assertTrue(report.meets_gate())

    def test_a_citation_past_the_end_of_a_file_is_dropped_not_delivered(self) -> None:
        """Evidence that cannot be pointed at is not weaker evidence; it is none."""
        document = graph_document()
        document["nodes"][1].update(start_line=400, end_line=440)
        self.publish(document)
        outcome = self.context()
        cited = {citation["source"]
                 for item in outcome.packet["documents"] for citation in item["citations"]}
        self.assertNotIn("example_pkg/loader.py#L400-L440", cited)
        self.assertIn("provider_warning", outcome.packet["omissions"])

    def test_confidence_maps_extracted_inferred_and_ambiguous(self) -> None:
        document = graph_document()
        document["edges"][2]["evidence"] = "ambiguous"
        self.publish(document)
        outcome = self.context()
        confidences = {item["confidence"] for item in outcome.packet["documents"]}
        self.assertEqual(confidences, {"extracted", "inferred", "unknown"})
        self.assertIn("unresolved_entities", outcome.packet["omissions"])

    def test_truncation_is_reported_rather_than_hidden(self) -> None:
        outcome = self.context(node_budget=1)
        self.assertTrue(outcome.packet["truncated"])
        self.assertEqual(outcome.packet["completeness"], "partial")
        self.assertIn("provider_has_more", outcome.packet["omissions"])

    def test_a_partial_generation_is_carried_into_the_packet(self) -> None:
        """A partial build's answer is partial however complete the traversal was."""
        self.publish(graph_document(), completeness=lifecycle.PARTIAL)
        outcome = self.context(policy=policy(required=False))
        # A partial generation is not usable at all by default, so the optional
        # request degrades rather than delivering an answer that looks whole.
        self.assertEqual(outcome.status, query.OPTIONAL_UNAVAILABLE)
        self.assertEqual(outcome.summary["reason"], "partial")

    def test_packet_text_carries_no_indexed_content(self) -> None:
        outcome = self.context()
        prose = " ".join(item["text"] for item in outcome.packet["documents"])
        self.assertNotIn("line 12", prose)
        for name in ("parse_config", "impact"):
            self.assertIn(name, prose)


class RecipientNeutralityTests(GraphWorkspace):
    """One approved packet, three recipients, no provider tools or credentials."""

    #: A packet identity, not a secret: ``render_evidence`` requires a handle.
    DELIVERY = "0123456789abcdef0123456789abcdef"

    def test_claude_codex_and_devin_receive_the_same_packet(self) -> None:
        packet = self.context().packet
        rendered = set()
        for recipient in ("claude:builder", "codex:reviewer", "devin:builder"):
            with self.subTest(recipient=recipient):
                validated = self.load(packet, recipient=recipient, revision=self.manifest.commit)
                self.assertEqual(validated.shareable_summary()["kind"], "repository")
                rendered.add(context_delivery.render_evidence(validated, self.DELIVERY))
        # The delivered payload is identical for every recipient: nothing in it
        # names a connection, a credential, a provider tool, or a local path.
        self.assertEqual(len(rendered), 1)
        payload = rendered.pop()
        self.assertNotIn("graphify", payload.casefold())
        self.assertNotIn(str(self.repository), payload)

    def test_an_unauthorized_recipient_is_still_refused(self) -> None:
        with self.assertRaises(ContextError):
            self.load(self.context().packet, recipient="claude:orchestrator",
                      revision=self.manifest.commit)


class AvailabilityTests(GraphWorkspace):
    """Required unavailable context blocks; optional unavailable context degrades."""

    def move_head(self) -> None:
        (self.repository / "example_pkg" / "config.py").write_text("changed\n", encoding="utf-8")
        git(self.repository, "add", ".")
        git(self.repository, "commit", "-q", "-m", "second")

    def test_stale_graph_blocks_required_context(self) -> None:
        self.move_head()
        outcome = self.context()
        self.assertEqual(outcome.status, query.REQUIRED_UNAVAILABLE)
        self.assertEqual(outcome.dependent_work, "paused")
        self.assertEqual(outcome.exit_code, 1)
        self.assertEqual(outcome.summary["reason"], "stale")
        self.assertIsNone(outcome.packet)

    def test_stale_graph_degrades_optional_context_to_ordinary_tools(self) -> None:
        self.move_head()
        outcome = self.context(policy=policy(required=False))
        self.assertEqual(outcome.status, query.OPTIONAL_UNAVAILABLE)
        self.assertEqual(outcome.dependent_work, "usable")
        self.assertEqual(outcome.exit_code, 0)
        self.assertIn("ordinary repository tools", outcome.summary["next_action"])

    def test_an_absent_graph_is_not_a_failure_of_the_caller(self) -> None:
        lifecycle.remove_graph(self.repository, root=self.state)
        self.assertEqual(self.context(policy=policy(required=False)).status,
                         query.OPTIONAL_UNAVAILABLE)
        self.assertEqual(self.context().status, query.REQUIRED_UNAVAILABLE)

    def test_a_generation_without_the_pinned_document_is_unreadable(self) -> None:
        self.publish({"schema": "graphify.native.v1", "nodes": [], "edges": []})
        outcome = self.context()
        self.assertEqual(outcome.status, query.REQUIRED_UNAVAILABLE)
        self.assertEqual(outcome.summary["reason"], "unreadable")

    def test_an_unresolved_target_blocks_required_context(self) -> None:
        outcome = self.context(target="no_such_symbol")
        self.assertEqual(outcome.status, query.REQUIRED_UNAVAILABLE)
        self.assertEqual(outcome.summary["reason"], "unresolved")

    def test_summaries_carry_metadata_only(self) -> None:
        for outcome in (self.context(), self.context(target="no_such_symbol")):
            rendered = json.dumps(outcome.summary)
            self.assertNotIn(str(self.repository), rendered)
            self.assertNotIn(str(self.state), rendered)


class CommandTests(GraphWorkspace):
    def authorization(self, **overrides) -> Path:
        # The command has no injected clock: it authorizes against the real one,
        # exactly as an operator's run does. So the envelope has to be live now
        # rather than at the fixed ``NOW`` the library-level tests use.
        live = datetime.now(timezone.utc) + timedelta(minutes=30)
        payload = {
            "connection": envelope(expires_at=live.isoformat()),
            "policy": policy(),
            "repository": "owner/repo",
            "work_item": "work-item-one",
        }
        payload.update(overrides)
        path = self.root / "authorization.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def invoke(self, *arguments: str) -> tuple[int, dict]:
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = command.main([
                "query", "--repo-path", str(self.repository), "--state-dir", str(self.state),
                "--authorization", str(self.authorization()), "--json", *arguments,
            ])
        return code, json.loads(stream.getvalue())

    def test_query_emits_a_metadata_summary_and_writes_a_private_packet(self) -> None:
        destination = self.root / "packet.json"
        code, summary = self.invoke(
            "--question", "impact", "--target", "parse_config",
            "--packet-out", str(destination),
        )
        self.assertEqual(code, 0)
        self.assertEqual(summary["status"], query.AVAILABLE)
        self.assertEqual(summary["source_revision"], self.manifest.commit)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(destination.read_text())["kind"], "repository")
        # The summary is what an operator may paste anywhere; the evidence is
        # only in the private file they named.
        self.assertNotIn("citations", json.dumps(summary))

    def test_required_unavailable_exits_non_zero_without_a_packet(self) -> None:
        lifecycle.remove_graph(self.repository, root=self.state)
        destination = self.root / "packet.json"
        code, summary = self.invoke(
            "--question", "impact", "--target", "parse_config",
            "--packet-out", str(destination),
        )
        self.assertEqual(code, 1)
        self.assertEqual(summary["status"], query.REQUIRED_UNAVAILABLE)
        self.assertFalse(destination.exists())

    def test_an_unreadable_authorization_file_fails_closed(self) -> None:
        path = self.root / "authorization.json"
        path.write_text(json.dumps({"connection": envelope()}), encoding="utf-8")
        code = command.main([
            "query", "--repo-path", str(self.repository), "--state-dir", str(self.state),
            "--authorization", str(path), "--question", "impact", "--target", "parse_config",
        ])
        self.assertEqual(code, 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
