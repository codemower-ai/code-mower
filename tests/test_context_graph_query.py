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


def node(identifier: str, label: str, path: str, line=None, **extra) -> dict:
    """One node in the pinned exporter's own shape.

    The field names and the annotations are the ones Graphify writes at the
    pinned commit: the four required fields of ``graphify/validate.py``
    (``id``, ``label``, ``file_type``, ``source_file``), the extractor's
    ``source_location`` of the form ``L<line>``, and the ``community`` /
    ``community_name`` / ``norm_label`` annotations ``export.py::to_json``
    adds to every node on the way out. No Code Mower node ``kind`` and no
    line span: neither exists in the real export.
    """
    return {
        "id": identifier,
        "label": label,
        "file_type": "code",
        "source_file": path,
        "source_location": "" if line is None else f"L{line}",
        "community": 0,
        "community_name": "Community 0",
        "norm_label": label.lower(),
        **extra,
    }


def edge(source: str, target: str, relation: str, confidence: str = "EXTRACTED", **extra) -> dict:
    """One link in the pinned exporter's own shape.

    The five required edge fields, the uppercase ``confidence`` vocabulary of
    the pinned validator, and the ``weight`` / ``confidence_score`` the
    extractor and exporter attach. ``confidence_score`` uses the exporter's
    own ``_CONFIDENCE_SCORE_DEFAULTS``.
    """
    scores = {"EXTRACTED": 1.0, "INFERRED": 0.55, "AMBIGUOUS": 0.2}
    return {
        "source": source,
        "target": target,
        "relation": relation,
        "confidence": confidence,
        "source_file": "example_pkg/config.py",
        "source_location": "L12",
        "weight": 1.0,
        "confidence_score": scores[confidence],
        **extra,
    }


def graph_nodes() -> list:
    """A symbol, its caller, its caller's caller, a test, and a file node."""
    return [
        node("n-config", "parse_config", "example_pkg/config.py", 12),
        node("n-load", "load", "example_pkg/loader.py", 40),
        node("n-report", "render", "example_pkg/report.py", 5),
        node("n-test", "test_parse_config", "tests/test_config.py", 8),
        # The extractor's per-file node: label is the file's base name at L1.
        node("n-config-file", "config.py", "example_pkg/config.py", 1),
    ]


def graph_edges() -> list:
    return [
        edge("n-load", "n-config", "calls"),
        edge("n-report", "n-load", "calls", "INFERRED"),
        edge("n-test", "n-config", "tests"),
        edge("n-config-file", "n-config", "contains"),
    ]


def graph_document(**extra) -> dict:
    """A small graph in the format the lifecycle's pinned invocation writes.

    ``context_graph_lifecycle`` requires ``extract --code-only --no-cluster``,
    and the pinned CLI's ``--no-cluster`` branch dumps the merged extractor
    result straight to ``graph.json`` through ``write_json_atomic``. So the
    top-level shape is the raw extraction: ``nodes``, ``edges``, ``hyperedges``,
    the token counters and ``extracted_sources``.

    What it deliberately does **not** carry is a ``directed`` marker, or
    ``multigraph``, ``graph``, ``links`` or ``built_at_commit``. That path never
    constructs a NetworkX graph and never calls ``export.py::to_json``, so none
    of those keys exist in a real generation, and a fixture that added one to
    satisfy the reader would be testing a file the provider never writes.
    """
    return {
        "nodes": graph_nodes(),
        "edges": graph_edges(),
        "hyperedges": [],
        "input_tokens": 0,
        "output_tokens": 0,
        "extracted_sources": sorted(SOURCES),
        **extra,
    }


def wide_graph_document(callers: int = 6, **extra) -> dict:
    """``parse_config`` with ``callers`` distinct, separately citable callers.

    Every caller is a real node at its own line of an indexed file, so each
    relationship the impact query reports carries two citations the bound commit
    confirms. Nothing here is uncitable, duplicated, or dangling: the only
    reason a document can go missing from a packet built over this graph is a
    budget, which is what the tests using it are about.
    """
    lines = SOURCES["example_pkg/loader.py"]
    if callers + 1 > lines:  # pragma: no cover - guards the fixture, not the code
        raise AssertionError("the fixture file has no line left to cite")
    return {
        "nodes": [
            node("n-config", "parse_config", "example_pkg/config.py", 12),
            *(
                node(f"n-caller-{index}", f"caller_{index}", "example_pkg/loader.py", index + 1)
                for index in range(1, callers + 1)
            ),
        ],
        "edges": [edge(f"n-caller-{index}", "n-config", "calls") for index in range(1, callers + 1)],
        "hyperedges": [],
        "input_tokens": 0,
        "output_tokens": 0,
        "extracted_sources": sorted(SOURCES),
        **extra,
    }


def node_link_document(**extra) -> dict:
    """The same graph as ``export.py::to_json`` writes it, for the clustered path.

    ``networkx.json_graph.node_link_data(G, edges="links")`` plus the
    ``hyperedges`` list and the ``built_at_commit`` stamp the exporter appends.
    Directed, because this format's endpoint order is only a caller/callee claim
    when the graph was stored as a ``DiGraph``.
    """
    return {
        "directed": True,
        "multigraph": False,
        "graph": {},
        "nodes": graph_nodes(),
        "links": graph_edges(),
        "hyperedges": [],
        **extra,
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

    def authorized(self, **overrides) -> dict:
        """An envelope as the connection mints one: bound to what is published now.

        ``authorize_locked`` reads the current generation on every call rather
        than remembering one, and a packet may only carry the generation its
        evidence came from. A literal fixed at ``setUp`` would authorize one
        generation and cite another as soon as a test rebuilds the graph --
        the disagreement the packet builder refuses.
        """
        published = lifecycle.graph_status(self.repository, root=self.state).generation
        return envelope(**{"generation": published, **overrides})

    def context(self, **overrides) -> query.GraphContext:
        arguments = {
            "question": "impact",
            "target": "parse_config",
            "envelope": self.authorized(),
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
            authorize=lambda: self.authorized(),
            now=NOW,
        )


class GraphSchemaTests(unittest.TestCase):
    """The pinned provider's own output is what gets read, and read bounded.

    The default fixture is the raw extraction the lifecycle's own pinned
    ``extract --code-only --no-cluster`` writes. The node-link export the
    clustered path writes is covered separately in ``NodeLinkFormatTests``,
    because its provenance and its direction guarantee are different and must
    not be tested by editing this fixture into that shape. The tests split into
    two halves on purpose: what a real generation carries must load, and what
    the provider's own validator would reject must refuse.
    """

    def load(self, document: dict) -> query.CodeGraph:
        return query.load_graph(document, generation="a" * 32, commit="b" * 40)

    def test_reads_the_pinned_provider_export(self) -> None:
        graph = self.load(graph_document())
        self.assertEqual(len(graph.nodes), 5)
        # One line per node, because ``source_location`` records one line.
        self.assertEqual(graph.nodes["n-config"].citation, "example_pkg/config.py#L12")
        self.assertEqual(
            {node.id: node.kind for node in graph.nodes.values()},
            {
                "n-config": "symbol", "n-load": "symbol", "n-report": "symbol",
                # Derived: label equals the file's base name.
                "n-config-file": "file",
                # Derived: the repository's test layout.
                "n-test": "test",
            },
        )

    def test_keeps_the_providers_own_relation_and_lowercases_confidence(self) -> None:
        """``contains`` filters as ``defines`` but still reads as ``contains``."""
        graph = self.load(graph_document())
        by_pair = {(edge.source, edge.target): edge for edge in graph.edges}
        contains = by_pair[("n-config-file", "n-config")]
        self.assertEqual((contains.relation, contains.kind), ("contains", "defines"))
        self.assertEqual(by_pair[("n-report", "n-load")].evidence, "inferred")

    def test_reads_the_raw_extraction_without_a_directed_marker(self) -> None:
        """The supported document declares no direction, and must not be asked to.

        The pinned ``--no-cluster`` branch writes the merged extractor result
        directly: no NetworkX graph is built, ``to_json`` is never called, and
        so ``directed``, ``multigraph``, ``graph`` and ``built_at_commit`` are
        absent from every real generation. A reader that demanded the marker
        would refuse the only path the lifecycle actually runs.
        """
        document = graph_document()
        for absent in ("directed", "multigraph", "graph", "links", "built_at_commit"):
            self.assertNotIn(absent, document)
        graph = self.load(document)
        self.assertEqual(len(graph.edges), 4)
        # The orientation is the extractor's, and it is the one queried.
        self.assertEqual(
            {(item.source, item.target) for item in graph.edges if item.relation == "calls"},
            {("n-load", "n-config"), ("n-report", "n-load")},
        )

    def test_reversed_node_iteration_does_not_reverse_a_raw_edge(self) -> None:
        """A raw edge's endpoints come off the edge record, not from node order.

        This is the property the ``--no-cluster`` path has and an undirected
        NetworkX container does not. ``add_edge`` writes the call site's own
        source and target, so permuting the node list -- the only thing an
        undirected container's endpoint order would follow -- cannot change
        which way a relationship points.
        """
        forward = self.load(graph_document())
        reversed_nodes = graph_document()
        reversed_nodes["nodes"] = list(reversed(reversed_nodes["nodes"]))
        permuted = self.load(reversed_nodes)
        self.assertEqual(
            [(item.source, item.target, item.relation) for item in forward.edges],
            [(item.source, item.target, item.relation) for item in permuted.edges],
        )
        # And the claim itself, stated the way a packet states it.
        calls = next(item for item in permuted.edges if item.target == "n-config"
                     and item.relation == "calls")
        self.assertEqual((calls.source, calls.target), ("n-load", "n-config"))

    def test_maps_an_unlisted_relation_without_asserting_a_listed_one(self) -> None:
        """An LLM-extracted relation is carried, grouped as ``related``, never renamed."""
        document = graph_document()
        document["edges"].append(edge("n-config", "n-report", "supersedes"))
        graph = self.load(document)
        extra = next(edge for edge in graph.edges if edge.relation == "supersedes")
        self.assertEqual(extra.kind, query.OTHER_RELATION)
        # ``related`` is not in the impact filter, so it cannot stand in for a call.
        self.assertNotIn(query.OTHER_RELATION, query._TRAVERSALS["impact"][1])

    def test_keeps_a_sourceless_stub_traversable_and_uncitable(self) -> None:
        """The extractor's cross-file stub: a real node with no location."""
        document = graph_document()
        document["nodes"].append({
            "id": "n-stub", "label": "Thing", "file_type": "code",
            "source_file": "", "source_location": "", "origin_file": "example_pkg/config.py",
        })
        document["edges"].append(edge("n-config", "n-stub", "references"))
        graph = self.load(document)
        self.assertIsNone(graph.nodes["n-stub"].citation)
        self.assertEqual(len(graph.edges), 5)

    def test_drops_non_code_corpora_and_prunes_their_edges(self) -> None:
        """Documents and concepts are not repository relationships."""
        document = graph_document()
        document["nodes"].append(
            {**node("n-doc", "design.md", "docs/design.md", 1), "file_type": "document"}
        )
        document["edges"].append(edge("n-config", "n-doc", "references"))
        graph = self.load(document)
        self.assertNotIn("n-doc", graph.nodes)
        self.assertEqual(len(graph.edges), 4)

    def test_tolerates_provider_annotations_it_does_not_read(self) -> None:
        """Extra exporter and LLM metadata must not reject a real generation."""
        document = graph_document()
        document["nodes"][0]["metadata"] = {"namespace": "example_pkg", "scope_chain": ["mod"]}
        document["nodes"][0]["type"] = "namespace"
        document["edges"][0]["context"] = "call site"
        self.assertEqual(len(self.load(document).nodes), 5)

    def test_refuses_a_document_with_no_provider_nodes_and_edges(self) -> None:
        for document in (
            {"nodes": []}, {"edges": []}, {"links": []}, {"schema": "something.else"}, [],
        ):
            with self.subTest(document=document):
                with self.assertRaises(ContextError):
                    self.load(document)

    def test_refuses_a_graph_built_from_another_commit(self) -> None:
        """``built_at_commit`` disagreeing with the generation is a refusal.

        The stamp is ``to_json``'s, so the raw path never writes one and for a
        real ``--no-cluster`` generation this check is vacuous -- the binding
        rests on the lifecycle's commit binding and the citation census. It is
        still honoured wherever it appears, which is what this covers.
        """
        with self.assertRaises(ContextError):
            self.load(graph_document(built_at_commit="c" * 40))
        # Agreeing is fine, and is the ordinary case.
        self.assertEqual(len(self.load(graph_document(built_at_commit="b" * 40)).nodes), 5)

    def test_refuses_records_missing_the_providers_required_fields(self) -> None:
        for mutate in (
            lambda doc: doc["nodes"][0].pop("label"),
            lambda doc: doc["nodes"][0].pop("source_file"),
            lambda doc: doc["nodes"][0].pop("file_type"),
            lambda doc: doc["edges"][0].pop("relation"),
            lambda doc: doc["edges"][0].pop("confidence"),
        ):
            with self.subTest(mutate=mutate):
                document = graph_document()
                mutate(document)
                with self.assertRaises(ContextError):
                    self.load(document)

    def test_refuses_vocabularies_the_providers_validator_rejects(self) -> None:
        for mutate in (
            lambda doc: doc["nodes"][0].update(file_type="diagram"),
            lambda doc: doc["edges"][0].update(confidence="GUESSED"),
            # Lowercase is the packet contract's vocabulary, not the provider's.
            lambda doc: doc["edges"][0].update(confidence="extracted"),
        ):
            with self.subTest(mutate=mutate):
                document = graph_document()
                mutate(document)
                with self.assertRaises(ContextError):
                    self.load(document)

    def test_refuses_a_vocabulary_field_that_is_not_text_at_all(self) -> None:
        """A JSON array or object where a word belongs is a refusal, not a crash.

        Both vocabulary fields are checked by membership in a set or a dict, and
        an unhashable value there raises ``TypeError`` -- out of a reader whose
        callers only ever catch ``ContextError``, so the graph-context path
        would propagate it instead of reporting the graph unreadable.
        """
        for field_name, record in (("file_type", "nodes"), ("confidence", "edges")):
            for value in ([], {}, ["code"], {"value": "EXTRACTED"}, 3, None, True):
                with self.subTest(field=field_name, value=value):
                    document = graph_document()
                    document[record][0][field_name] = value
                    with self.assertRaises(ContextError):
                        self.load(document)

    def test_refuses_an_unreadable_source_location(self) -> None:
        for location in ("12", "line 12", "L", "L0", "L-4", "L99999999999"):
            with self.subTest(location=location):
                document = graph_document()
                document["nodes"][0]["source_location"] = location
                with self.assertRaises(ContextError):
                    self.load(document)

    def test_refuses_a_node_outside_the_indexed_checkout(self) -> None:
        """A node that could never be cited must not be traversable either."""
        for path in ("/etc/passwd", "../sibling/config.py", ".git/config", ".graphify/nodes.bin"):
            with self.subTest(path=path):
                document = graph_document()
                document["nodes"][0]["source_file"] = path
                with self.assertRaises(ContextError):
                    self.load(document)

    def test_refuses_duplicate_node_identifiers(self) -> None:
        document = graph_document()
        document["nodes"].append(dict(document["nodes"][0]))
        with self.assertRaises(ContextError):
            self.load(document)


class NodeLinkFormatTests(unittest.TestCase):
    """The other document that can appear under ``graph.json``, read on its own terms.

    ``export.py::to_json`` is the clustered path's writer, and its provenance is
    not the extractor's: the endpoints it writes came out of a NetworkX
    container that may have been undirected. So it is read, but only when it
    says it preserved direction -- and a raw extraction is never held to that,
    because its producer writes no such marker.
    """

    def load(self, document: dict) -> query.CodeGraph:
        return query.load_graph(document, generation="a" * 32, commit="b" * 40)

    def test_reads_a_directed_node_link_export(self) -> None:
        graph = self.load(node_link_document())
        self.assertEqual(len(graph.nodes), 5)
        self.assertEqual(len(graph.edges), 4)

    def test_reads_the_renamed_edges_key_of_a_node_link_export(self) -> None:
        """NetworkX renamed ``links`` to ``edges``; the pinned validator takes either.

        The marker keys are what identify the format, so the renamed document is
        still node-link and is still held to the direction requirement.
        """
        document = node_link_document()
        document["edges"] = document.pop("links")
        self.assertEqual(len(self.load(document).edges), 4)
        undirected = node_link_document(directed=False)
        undirected["edges"] = undirected.pop("links")
        with self.assertRaises(ContextError):
            self.load(undirected)

    def test_refuses_a_node_link_export_that_does_not_preserve_direction(self) -> None:
        """An undirected export states an endpoint pair, not a caller and callee.

        The provider's undirected storage canonicalizes endpoint order and its
        export's repair leaves no mark a reader can check, so every oriented
        answer here -- ``impact``, ``dependency``, and the ``calls`` sentence a
        ``symbol`` neighbourhood states -- would be asserting an orientation the
        document never established. The refusal names direction, so an operator
        reads it as "rebuild" rather than as a corrupt graph.
        """
        for flag in (False, None, "true", 1):
            with self.subTest(directed=flag):
                document = node_link_document()
                if flag is None:
                    # Still node-link: ``multigraph``, ``graph`` and ``links``
                    # are markers of their own, so dropping one key does not
                    # make this document pass as a raw extraction.
                    document.pop("directed")
                else:
                    document["directed"] = flag
                with self.assertRaises(ContextError) as caught:
                    self.load(document)
                self.assertIn("direction", str(caught.exception))

    def test_each_marker_alone_identifies_the_node_link_format(self) -> None:
        for marker in query.NODE_LINK_MARKERS:
            with self.subTest(marker=marker):
                document = graph_document()
                document[marker] = {} if marker == "graph" else False
                if marker == "links":
                    document["links"] = document.pop("edges")
                with self.assertRaises(ContextError) as caught:
                    self.load(document)
                self.assertIn("direction", str(caught.exception))

    def test_the_raw_format_is_what_the_reader_reports_for_the_pinned_options(self) -> None:
        self.assertEqual(query._graph_format(graph_document()), query.GRAPH_FORMAT_RAW)
        self.assertEqual(
            query._graph_format(node_link_document()), query.GRAPH_FORMAT_NODE_LINK
        )


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

    def test_a_second_hop_reports_the_edge_it_actually_walked(self) -> None:
        """The graph says ``render`` calls ``load``, and nothing about render and parse_config."""
        reached = {item.node.name: item for item in self.query().relations}
        self.assertEqual(reached["load"].origin.name, "parse_config")
        self.assertEqual(reached["render"].depth, 2)
        self.assertEqual(reached["render"].origin.name, "load")
        # The seed the walk started from is still carried, as provenance rather
        # than as a relationship anybody asserted.
        self.assertEqual(reached["render"].seed.name, "parse_config")

    def test_budget_truncates_and_says_so(self) -> None:
        result = self.query(node_budget=1)
        self.assertEqual(len(result.relations), 1)
        self.assertTrue(result.truncated)
        self.assertIn("provider_has_more", result.omissions)

    def test_more_definitions_than_the_seed_bound_is_reported_as_truncation(self) -> None:
        """Seeds the bound dropped take their whole reachable neighbourhood with them."""
        document = graph_document()
        for index in range(query.MAX_SEEDS + 1):
            document["nodes"].append(
                node(f"n-extra-{index}", "parse_config", "example_pkg/loader.py", 2))
        graph = query.load_graph(document, generation="a" * 32, commit="b" * 40)
        result = query.run_query(graph, question="impact", target="parse_config")
        self.assertEqual(len(result.seeds), query.MAX_SEEDS)
        self.assertTrue(result.truncated)
        self.assertIn("provider_has_more", result.omissions)
        self.assertIn("unresolved_entities", result.omissions)

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


class RetainedRelationshipTests(unittest.TestCase):
    """A relationship between two already-seen nodes is still evidence.

    Expansion and reporting were once bounded by one set of node ids, so a
    graph that says two things about a pair had one of them deleted while the
    result still reported itself complete. These build their own graphs because
    the shapes that expose it -- a cycle, a seed set that is already connected,
    a walk that reconverges -- are not in the shared fixture.

    The bound is unchanged: each node is still walked through once, and what a
    budget or a depth limit removes is still reported as truncation.
    """

    def load(self, *nodes, edges=()) -> query.CodeGraph:
        document = graph_document(nodes=list(nodes), edges=list(edges))
        return query.load_graph(document, generation="a" * 32, commit="b" * 40)

    def stated(self, result: query.QueryResult) -> set:
        """Every reported relationship as the provider's own edge record."""
        return {(item.via.source, item.via.relation, item.via.target) for item in result.relations}

    def test_a_two_way_cycle_reports_both_directions(self) -> None:
        graph = self.load(
            node("n-a", "alpha", "example_pkg/config.py", 12),
            node("n-b", "beta", "example_pkg/loader.py", 40),
            edges=[edge("n-a", "n-b", "calls"), edge("n-b", "n-a", "calls")],
        )
        result = query.run_query(graph, question="impact", target="alpha")
        self.assertEqual(self.stated(result),
                         {("n-b", "calls", "n-a"), ("n-a", "calls", "n-b")})
        # The second direction is the edge it says it is, between its own two
        # endpoints -- not the seed relabelled.
        self.assertEqual([(item.origin.name, item.node.name)
                          for item in result.relations if item.depth == 2],
                         [("beta", "alpha")])
        self.assertFalse(result.truncated)
        self.assertNotIn("provider_has_more", result.omissions)

    def test_relationships_among_path_seeds_are_still_reported(self) -> None:
        """Every endpoint is a seed, so the old reader answered with nothing."""
        graph = self.load(
            node("n-a", "alpha", "example_pkg/config.py", 12),
            node("n-b", "beta", "example_pkg/config.py", 20),
            edges=[edge("n-a", "n-b", "calls")],
        )
        result = query.run_query(graph, question="dependency", target="example_pkg/config.py")
        self.assertEqual({item.id for item in result.seeds}, {"n-a", "n-b"})
        self.assertEqual(self.stated(result), {("n-a", "calls", "n-b")})

    def test_a_reconvergent_walk_keeps_both_paths_into_one_node(self) -> None:
        graph = self.load(
            node("n-a", "alpha", "example_pkg/config.py", 12),
            node("n-b", "beta", "example_pkg/loader.py", 40),
            node("n-c", "gamma", "example_pkg/report.py", 5),
            node("n-d", "delta", "example_pkg/config.py", 30),
            edges=[edge("n-a", "n-b", "calls"), edge("n-a", "n-c", "calls"),
                   edge("n-b", "n-d", "calls"), edge("n-c", "n-d", "calls")],
        )
        result = query.run_query(graph, question="dependency", target="alpha")
        self.assertEqual(self.stated(result), {
            ("n-a", "calls", "n-b"), ("n-a", "calls", "n-c"),
            ("n-b", "calls", "n-d"), ("n-c", "calls", "n-d"),
        })
        self.assertEqual({(item.origin.name, item.node.name)
                          for item in result.relations if item.depth == 2},
                         {("beta", "delta"), ("gamma", "delta")})
        self.assertFalse(result.truncated)

    def test_a_self_loop_is_reported_once_and_parallel_relations_stay_distinct(self) -> None:
        """Endpoints alone are not the identity; the provider's wording is part of it."""
        graph = self.load(
            node("n-a", "alpha", "example_pkg/config.py", 12),
            node("n-b", "beta", "example_pkg/loader.py", 40),
            edges=[edge("n-a", "n-a", "calls"),
                   edge("n-a", "n-b", "calls"),
                   edge("n-a", "n-b", "references")],
        )
        result = query.run_query(graph, question="symbol", target="alpha")
        self.assertEqual(len(result.relations), 3)
        self.assertEqual(sorted(self.stated(result)), [
            ("n-a", "calls", "n-a"), ("n-a", "calls", "n-b"), ("n-a", "references", "n-b"),
        ])

    def test_a_duplicated_edge_record_is_reported_once(self) -> None:
        """Reached from both sides of a ``both`` walk, or written twice: one relationship."""
        graph = self.load(
            node("n-a", "alpha", "example_pkg/config.py", 12),
            node("n-b", "beta", "example_pkg/loader.py", 40),
            edges=[edge("n-a", "n-b", "calls"), edge("n-a", "n-b", "calls")],
        )
        result = query.run_query(graph, question="dependency", target="alpha")
        self.assertEqual(len(result.relations), 1)
        self.assertFalse(result.truncated)

    def test_a_retained_relationship_the_budget_cuts_is_reported_as_truncation(self) -> None:
        """The budget still bounds the answer, and still says what it removed."""
        graph = self.load(
            node("n-a", "alpha", "example_pkg/config.py", 12),
            node("n-b", "beta", "example_pkg/loader.py", 40),
            edges=[edge("n-a", "n-b", "calls"), edge("n-b", "n-a", "calls")],
        )
        result = query.run_query(graph, question="impact", target="alpha", node_budget=1)
        self.assertEqual(self.stated(result), {("n-b", "calls", "n-a")})
        self.assertTrue(result.truncated)
        self.assertIn("provider_has_more", result.omissions)

    def test_depth_still_bounds_a_walk_that_retains_relationships(self) -> None:
        graph = self.load(
            node("n-a", "alpha", "example_pkg/config.py", 12),
            node("n-b", "beta", "example_pkg/loader.py", 40),
            edges=[edge("n-a", "n-b", "calls"), edge("n-b", "n-a", "calls")],
        )
        result = query.run_query(graph, question="impact", target="alpha", depth=1)
        self.assertEqual(self.stated(result), {("n-b", "calls", "n-a")})


def cyclic_graph_document() -> dict:
    """The shared fixture, plus the back-edge that makes the pair mutual.

    ``load`` already calls ``parse_config``; this adds the provider's own
    record that ``parse_config`` also calls ``load``.
    """
    return graph_document(edges=[*graph_edges(), edge("n-config", "n-load", "calls")])


class RetainedRelationshipPacketTests(GraphWorkspace):
    """What a recipient actually reads for a retained relationship."""

    document = cyclic_graph_document()

    def documents(self) -> dict:
        return {item["text"]: item for item in self.context().packet["documents"]}

    def test_a_retained_back_edge_states_and_cites_its_own_endpoints(self) -> None:
        documents = self.documents()
        [text] = [item for item in documents if "parse_config calls load" in item]
        self.assertIn("hop 2", text)
        self.assertEqual(
            {citation["source"] for citation in documents[text]["citations"]},
            {"example_pkg/config.py#L12", "example_pkg/loader.py#L40"},
        )
        # The other direction between the same pair is still its own document,
        # stated the way the provider recorded it.
        self.assertTrue(any("load calls parse_config" in item for item in documents))

    def test_the_rest_of_the_walk_is_unchanged(self) -> None:
        documents = self.documents()
        self.assertTrue(any("render calls load" in item and "reached from parse_config" in item
                            for item in documents))
        self.assertNotIn("provider_has_more", self.context().summary["omissions"])


def missing_endpoint_document() -> dict:
    """The shared fixture, plus a relationship onto an id it never declares.

    ``parse_config`` calls something the provider named ``n-ghost`` and then
    described nowhere. The relationship is real and unreadable, which is not
    the same fact as a relationship onto a corpus this module has stated it
    does not query.
    """
    return graph_document(edges=[*graph_edges(), edge("n-config", "n-ghost", "calls")])


class MissingEndpointTests(unittest.TestCase):
    """A dropped edge is two different facts, and must not be reported as one.

    The provider's node list can be missing an endpoint for two reasons. It
    declared the endpoint as a document, paper, image, rationale or concept,
    and this module has stated in its own contract that it does not query those
    -- a scope, readable in the rules. Or it declared the endpoint nowhere at
    all, which is evidence its own document does not carry. Pruning both
    silently let a query over a neighbourhood the provider could not state in
    full come back marked ``complete``.
    """

    def load(self, *nodes, edges=()) -> query.CodeGraph:
        document = graph_document(nodes=list(nodes), edges=list(edges))
        return query.load_graph(document, generation="a" * 32, commit="b" * 40)

    def test_a_declared_exclusion_leaves_no_node_incomplete(self) -> None:
        """The stated scope stays silent, exactly as before."""
        document = graph_document()
        document["nodes"].append(
            {**node("n-doc", "design.md", "docs/design.md", 1), "file_type": "document"}
        )
        document["edges"].append(edge("n-config", "n-doc", "references"))
        graph = query.load_graph(document, generation="a" * 32, commit="b" * 40)
        self.assertNotIn("n-doc", graph.nodes)
        self.assertEqual(graph.incomplete, frozenset())

    def test_an_undeclared_endpoint_marks_the_surviving_node(self) -> None:
        graph = query.load_graph(
            missing_endpoint_document(), generation="a" * 32, commit="b" * 40
        )
        self.assertEqual(graph.incomplete, frozenset({"n-config"}))
        # Still pruned: a relationship with one end unstated is not citable.
        self.assertNotIn("n-ghost", {edge_.target for edge_ in graph.edges})

    def test_both_ends_of_an_undeclared_relationship_are_marked(self) -> None:
        """Direction is not what decides it; being in the graph is."""
        graph = self.load(
            node("n-a", "alpha", "example_pkg/config.py", 12),
            node("n-b", "beta", "example_pkg/loader.py", 40),
            edges=[edge("n-ghost", "n-a", "calls"), edge("n-b", "n-ghost", "calls")],
        )
        self.assertEqual(graph.incomplete, frozenset({"n-a", "n-b"}))

    def test_a_relationship_with_no_surviving_endpoint_marks_nothing(self) -> None:
        """No node any traversal can reach is narrowed by it, so nothing claims it."""
        graph = self.load(
            node("n-a", "alpha", "example_pkg/config.py", 12),
            edges=[edge("n-ghost", "n-other-ghost", "calls")],
        )
        self.assertEqual(graph.incomplete, frozenset())
        self.assertEqual(graph.edges, ())

    def test_a_mixed_relationship_counts_as_missing_evidence(self) -> None:
        """One declared exclusion does not excuse the end that was never declared."""
        document = graph_document()
        document["nodes"].append(
            {**node("n-doc", "design.md", "docs/design.md", 1), "file_type": "document"}
        )
        document["edges"].append(edge("n-config", "n-ghost", "calls"))
        document["edges"].append(edge("n-load", "n-doc", "references"))
        graph = query.load_graph(document, generation="a" * 32, commit="b" * 40)
        self.assertEqual(graph.incomplete, frozenset({"n-config"}))

    def test_a_traversal_that_reaches_the_node_reports_partial(self) -> None:
        graph = query.load_graph(
            missing_endpoint_document(), generation="a" * 32, commit="b" * 40
        )
        result = query.run_query(graph, question="symbol", target="parse_config")
        self.assertIn("provider_partial", result.omissions)
        # Not truncation: no budget and no depth limit cut this. The evidence
        # was never in the artifact.
        self.assertFalse(result.truncated)
        self.assertNotIn("provider_has_more", result.omissions)

    def test_a_traversal_that_reaches_the_node_indirectly_reports_partial(self) -> None:
        """Touched by the walk, not just seeded: the answer still spans that node."""
        graph = query.load_graph(
            missing_endpoint_document(), generation="a" * 32, commit="b" * 40
        )
        result = query.run_query(graph, question="dependency", target="load")
        self.assertTrue(any(item.node.id == "n-config" for item in result.relations))
        self.assertIn("provider_partial", result.omissions)

    def test_a_traversal_elsewhere_in_the_graph_stays_complete(self) -> None:
        """A hole somewhere else is not a hole in this answer.

        Marking every query partial because one node in the repository lost an
        endpoint would make the flag say nothing about the answer carrying it.
        """
        graph = self.load(
            node("n-a", "alpha", "example_pkg/config.py", 12),
            node("n-b", "beta", "example_pkg/loader.py", 40),
            node("n-c", "gamma", "example_pkg/report.py", 5),
            edges=[edge("n-a", "n-b", "calls"), edge("n-c", "n-ghost", "calls")],
        )
        result = query.run_query(graph, question="symbol", target="alpha")
        self.assertNotIn("provider_partial", result.omissions)
        self.assertFalse(result.truncated)


class MissingEndpointPacketTests(GraphWorkspace):
    """What the recipient reads when the provider could not state a neighbourhood."""

    document = missing_endpoint_document()

    def test_the_packet_is_partial_and_says_why(self) -> None:
        context = self.context()
        self.assertIn("provider_partial", context.packet["omissions"])
        self.assertEqual(context.packet["completeness"], "partial")
        # A packet may be partial without being truncated; the delivery contract
        # only forbids the other pairing.
        self.assertFalse(context.packet["truncated"])
        self.assertIn("provider_partial", context.summary["omissions"])
        self.assertEqual(context.summary["completeness"], "partial")

    def test_the_relationships_it_could_state_are_still_stated(self) -> None:
        """Partial is not empty: what the document did carry is still evidence."""
        texts = [item["text"] for item in self.context().packet["documents"]]
        self.assertTrue(any("load calls parse_config" in text for text in texts))


class CallableLabelSeedTests(unittest.TestCase):
    """Bare names against the labels the pinned extractor actually writes.

    A fresh contained extraction of the public fixture at 0.9.58 labels the
    function ``parse_graph_citation`` as ``parse_graph_citation()`` and the
    function ``packet`` as ``packet()``. An exact-label-only reader answers
    "unresolved" for every natural spelling of a function name, so a bare symbol
    resolves to the canonical callable label with only its syntactic trailing
    argument list removed -- and to nothing else. These tests are as much about
    what that must *not* reach.
    """

    def load(self, *nodes, edges=()) -> query.CodeGraph:
        document = graph_document(nodes=list(nodes), edges=list(edges))
        return query.load_graph(document, generation="a" * 32, commit="b" * 40)

    def test_a_bare_name_resolves_to_the_canonical_callable_label(self) -> None:
        graph = self.load(
            node("n-citation", "parse_graph_citation()", "example_pkg/config.py", 12))
        self.assertEqual([item.id for item in graph.seeds("parse_graph_citation")], ["n-citation"])

    def test_the_literal_label_still_resolves_exactly(self) -> None:
        """Stripping is an addition, not a replacement: the written label wins."""
        graph = self.load(
            node("n-citation", "parse_graph_citation()", "example_pkg/config.py", 12))
        self.assertEqual(
            [item.id for item in graph.seeds("parse_graph_citation()")], ["n-citation"])

    def test_an_exact_label_beats_the_same_string_read_as_a_callable(self) -> None:
        """Both exist, and the tier that matched what was written is the answer.

        A module attribute ``packet`` and a function ``packet()`` are different
        definitions. Merging them would report a relationship of one as a
        relationship of the other, so the exact label resolves alone and the
        result is not ambiguous.
        """
        graph = self.load(
            node("n-attribute", "packet", "example_pkg/config.py", 4),
            node("n-function", "packet()", "example_pkg/config.py", 12),
        )
        self.assertEqual([item.id for item in graph.seeds("packet")], ["n-attribute"])

    def test_a_near_name_does_not_reach_a_longer_callable(self) -> None:
        """No prefix, substring, or edit-distance matching -- an equality test."""
        graph = self.load(
            node("n-citation", "parse_graph_citation()", "example_pkg/config.py", 12),
            node("n-other", "parse_graph_citations()", "example_pkg/config.py", 20),
        )
        for target in ("parse_graph", "parse", "graph_citation", "arse_graph_citation"):
            with self.subTest(target=target):
                self.assertEqual(graph.seeds(target), ())

    def test_overload_like_labels_are_ambiguity_rather_than_a_pick(self) -> None:
        """Two labels reduce to one name, so the target names two definitions."""
        graph = self.load(
            node("n-int", "render(int)", "example_pkg/report.py", 5),
            node("n-str", "render(str)", "example_pkg/report.py", 9),
        )
        result = query.run_query(graph, question="symbol", target="render")
        self.assertEqual([item.id for item in result.seeds], ["n-int", "n-str"])
        self.assertTrue(result.ambiguous)
        self.assertIn("unresolved_entities", result.omissions)

    def test_the_seed_bound_and_its_truncation_still_apply_to_bare_names(self) -> None:
        nodes = [
            node(f"n-{index:02d}", f"render(arg{index})", "example_pkg/report.py", index + 1)
            for index in range(query.MAX_SEEDS + 1)
        ]
        result = query.run_query(self.load(*nodes), question="symbol", target="render")
        self.assertEqual(len(result.seeds), query.MAX_SEEDS)
        self.assertTrue(result.truncated)
        self.assertIn("provider_has_more", result.omissions)

    def test_a_path_target_is_unchanged_and_still_last(self) -> None:
        """Paths are literal, and a callable label never stands in for one."""
        graph = self.load(
            node("n-citation", "parse_graph_citation()", "example_pkg/config.py", 12))
        self.assertEqual(
            [item.id for item in graph.seeds("example_pkg/config.py")], ["n-citation"])
        self.assertEqual(graph.seeds("example_pkg"), ())

    def test_only_a_whole_trailing_argument_list_is_removed(self) -> None:
        """The rule as a table, including every shape that must reduce to nothing."""
        for label, expected in (
            ("parse_graph_citation()", "parse_graph_citation"),
            ("render(int)", "render"),
            ("render(Callable[(int)])", "render"),
            ("parse_graph_citation", ""),
            ("()", ""),
            ("render(int))", ""),
            ("render()x", ""),
            ("render(int", ""),
            ("ren)der()", ""),
            ("", ""),
        ):
            with self.subTest(label=label):
                self.assertEqual(query._callable_base(label), expected)


class CitationValidationTests(GraphWorkspace):
    def validator(self) -> query.CitationValidator:
        census = lifecycle.read_tracked_census(self.repository, self.manifest.commit)
        return query.CitationValidator(self.repository, census)

    def test_validates_line_claims_against_the_bound_commit(self) -> None:
        validator = self.validator()
        self.assertTrue(validator.validate("example_pkg/config.py#L12"))
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
    def test_a_packet_binds_the_generation_its_evidence_actually_came_from(self) -> None:
        """An envelope authorizing another generation is refused, not copied.

        The delivery contract's freshness rules all read the binding's
        generation, so a packet that carried an authorized-but-unqueried
        generation would keep passing them after a rebuild -- the one case the
        rules exist to catch. Nothing here rewrites the envelope: it is an
        authorization this module did not mint.
        """
        republished = self.publish(graph_document())
        self.assertNotEqual(republished.generation, self.manifest.generation)
        # An authorization minted before the rebuild: it names the generation
        # that is gone, while the traversal reads the one published now.
        outcome = self.context(envelope=self.authorized(generation=self.manifest.generation))
        self.assertEqual(outcome.status, query.REQUIRED_UNAVAILABLE)
        self.assertEqual(outcome.summary["reason"], "uncitable")
        self.assertIsNone(outcome.packet)

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
        document["nodes"][1]["source_location"] = "L400"
        self.publish(document)
        outcome = self.context()
        cited = {citation["source"]
                 for item in outcome.packet["documents"] for citation in item["citations"]}
        self.assertNotIn("example_pkg/loader.py#L400", cited)
        self.assertIn("provider_warning", outcome.packet["omissions"])

    def test_confidence_maps_extracted_inferred_and_ambiguous(self) -> None:
        document = graph_document()
        document["edges"][2]["confidence"] = "AMBIGUOUS"
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

    def test_multi_hop_evidence_names_and_cites_the_edge_it_walked(self) -> None:
        """A transitive result reads as a path, never as a direct relationship."""
        documents = {item["text"]: item for item in self.context().packet["documents"]}
        [text] = [item for item in documents if "render" in item]
        self.assertIn("render calls load", text)
        self.assertIn("reached from parse_config", text)
        self.assertEqual(
            {citation["source"] for citation in documents[text]["citations"]},
            {"example_pkg/report.py#L5", "example_pkg/loader.py#L40"},
        )
        # Each citation is titled with the node it points at, not with the node
        # the relationship happened to reach.
        self.assertEqual(
            {citation["title"] for citation in documents[text]["citations"]},
            {"symbol render", "symbol load"},
        )

    def test_packet_text_carries_no_indexed_content(self) -> None:
        outcome = self.context()
        prose = " ".join(item["text"] for item in outcome.packet["documents"])
        self.assertNotIn("line 12", prose)
        for name in ("parse_config", "impact"):
            self.assertIn(name, prose)


class DocumentBudgetTests(GraphWorkspace):
    """The selected policy's document budget is what the packet is built to.

    ``MAX_DOCUMENTS`` is this adapter's own ceiling, and the shared contract
    carries a separate, smaller default. A packet built to the ceiling alone is
    not merely generous: ``context_contract`` refuses it at delivery, so the
    answer a wide question deserves -- five documents and an honest
    ``document_limit`` -- is instead no answer at all. These tests hold the two
    budgets together and in the right direction: a policy may tighten what one
    packet carries, never lift the ceiling.
    """

    def wide(self, callers: int = 6, **overrides) -> dict:
        self.publish(wide_graph_document(callers))
        outcome = self.context(**overrides)
        self.assertEqual(outcome.status, query.AVAILABLE)
        return outcome.packet

    def test_the_default_policy_budget_bounds_a_wider_answer(self) -> None:
        packet = self.wide()
        self.assertEqual(len(packet["documents"]), 5)
        self.assertLessEqual(len(packet["documents"]), contract.normalize_policy(policy())["max_documents"])
        self.assertTrue(packet["truncated"])
        self.assertEqual(packet["completeness"], "partial")
        self.assertIn("document_limit", packet["omissions"])

    def test_the_bounded_packet_is_the_deterministic_prefix_of_the_whole_answer(self) -> None:
        """Nothing is reordered to fit: the budget cuts the tail, in place."""
        whole = self.wide(policy=policy(max_documents=6))
        bounded = self.wide(policy=policy(max_documents=5))
        self.assertEqual(len(whole["documents"]), 6)
        self.assertEqual(
            [item["text"] for item in bounded["documents"]],
            [item["text"] for item in whole["documents"]][:5],
        )

    def test_an_answer_that_exactly_fits_its_budget_is_not_called_truncated(self) -> None:
        """No eligible relationship was left out, so there is nothing to report."""
        packet = self.wide(policy=policy(max_documents=6))
        self.assertEqual(len(packet["documents"]), 6)
        self.assertFalse(packet["truncated"])
        self.assertEqual(packet["completeness"], "complete")
        self.assertNotIn("document_limit", packet["omissions"])

    def test_a_budget_below_the_shared_default_bounds_the_packet_further(self) -> None:
        """A policy may tighten past the contract's default, and is obeyed."""
        packet = self.wide(policy=policy(max_documents=2))
        self.assertEqual(len(packet["documents"]), 2)
        self.assertTrue(packet["truncated"])
        self.assertEqual(packet["completeness"], "partial")
        self.assertIn("document_limit", packet["omissions"])

    def test_a_budget_above_the_adapters_ceiling_does_not_lift_it(self) -> None:
        """The policy bounds the packet downward only; the ceiling still holds."""
        self.assertGreater(20, query.MAX_DOCUMENTS)
        packet = self.wide(callers=20, policy=policy(max_documents=20))
        self.assertEqual(len(packet["documents"]), query.MAX_DOCUMENTS)
        self.assertTrue(packet["truncated"])
        self.assertEqual(packet["completeness"], "partial")
        self.assertIn("document_limit", packet["omissions"])

    def test_the_summary_reports_the_same_bounded_count_as_the_packet(self) -> None:
        """A metadata-only reader must not be told the answer was whole."""
        self.publish(wide_graph_document(6))
        outcome = self.context()
        self.assertEqual(outcome.summary["documents"], len(outcome.packet["documents"]))
        self.assertEqual(outcome.summary["documents"], 5)
        self.assertTrue(outcome.summary["truncated"])
        self.assertEqual(outcome.summary["completeness"], "partial")
        self.assertIn("document_limit", outcome.summary["omissions"])


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

    def test_a_generation_without_a_provider_export_is_unreadable(self) -> None:
        """A member that is not the pinned exporter's document at all."""
        self.publish({"schema": "graphify.native.v1", "entities": [], "relations": []})
        outcome = self.context()
        self.assertEqual(outcome.status, query.REQUIRED_UNAVAILABLE)
        self.assertEqual(outcome.summary["reason"], "unreadable")

    def test_a_malformed_vocabulary_field_reports_unreadable_both_ways(self) -> None:
        """Required blocks and optional degrades, which is what "unreadable" means.

        The reader refuses a ``file_type`` that is a JSON array rather than a
        word, and this is the path that refusal has to arrive on: a graph the
        caller is told it cannot use, not an exception out of an opt-in
        feature. Both dispositions are covered because only one of them pauses
        the dependent work.
        """
        document = graph_document()
        document["nodes"][0]["file_type"] = ["code"]
        self.publish(document)
        blocked = self.context()
        self.assertEqual(blocked.status, query.REQUIRED_UNAVAILABLE)
        self.assertEqual(blocked.summary["reason"], "unreadable")
        self.assertEqual(blocked.dependent_work, "paused")
        degraded = self.context(policy=policy(required=False))
        self.assertEqual(degraded.status, query.OPTIONAL_UNAVAILABLE)
        self.assertEqual(degraded.summary["reason"], "unreadable")
        self.assertEqual(degraded.dependent_work, "usable")

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
            "connection": self.authorized(expires_at=live.isoformat()),
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

    def test_an_existing_readable_destination_is_replaced_by_a_private_file(self) -> None:
        """A creation mode binds only a file the open created; this one replaces."""
        destination = self.root / "packet.json"
        destination.write_text("stale", encoding="utf-8")
        destination.chmod(0o644)
        code, _ = self.invoke(
            "--question", "impact", "--target", "parse_config",
            "--packet-out", str(destination),
        )
        self.assertEqual(code, 0)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(destination.read_text())["kind"], "repository")
        # Nothing is left behind under a name the operator did not ask for.
        self.assertEqual([item.name for item in self.root.iterdir() if "partial" in item.name], [])

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
