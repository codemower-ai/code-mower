"""Bounded local-graph queries and revision-bound context packets (issue #914).

``context_graph_lifecycle`` (#913) publishes an immutable generation: an
artifact bound to one commit and tree, with a manifest that says which provider
release produced it and whether that run finished. ``context_graph`` (#876)
scores a delivered packet's citations. Between those two there was nothing: no
way to ask the graph a question and no way to turn an answer into evidence a
recipient may read.

This module is that middle. It reads the pinned graph schema out of the
published artifact directly rather than through provider query tooling, runs
deterministic bounded traversals for the four questions a code graph answers
better than ordinary repository tools, validates every citation against the
immutable tracked tree of the bound commit, and emits an ordinary
``code_mower.contextPacket.v1`` repository-kind packet. The packet is the whole
interface to a recipient: Claude, Codex and Devin read the same approved bytes
through the existing delivery path, with no graph, no provider install, and no
credentials of their own.

Two properties the adoption record (``docs/graphify-evaluation.md``) asks for
are load-bearing here and are therefore not configurable:

* **Queries are symbol-first, relationship-filtered and budgeted.** Default
  traversals in the evaluated provider returned 700-900 nodes and truncated
  silently. Every traversal here starts from named seeds, follows one filtered
  relationship set, and stops at an explicit budget that is reported as
  truncation rather than presented as a complete answer.
* **Stale or unknown graph state is never answered from.** A required context
  request blocks; an optional one degrades to ordinary repository tools. There
  is no third outcome where a consumer is handed an older graph that looks
  fresh.

Nothing here installs, downloads, or executes a provider. The only subprocess
is Git, reading blobs of the commit the generation is already bound to.
"""

from __future__ import annotations

import json
import subprocess
import tarfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import context_graph_lifecycle as lifecycle
from .context_contract import (
    CAPABILITY_VERSION,
    PACKET_SCHEMA,
    ContextError,
    _text,
    _timestamp,
    normalize_policy,
    validate_connection,
)
from .context_graph import MAX_GRAPH_CITATIONS, parse_graph_citation

#: The graph document Code Mower reads, by name and by schema. Pinned on both:
#: the artifact is whatever the pinned provider release wrote, and a member
#: that is merely *shaped* like a graph is not the schema this adapter was
#: reviewed against. An artifact without it is unreadable rather than
#: best-effort -- see ``read_graph``.
GRAPH_MEMBER = "graph.json"
GRAPH_SCHEMA = "code_mower.contextGraph.v1"

QUERY_SCHEMA = "code_mower.contextGraphQuery.v1"

#: The questions a code graph answers better than ``rg`` and an ordinary
#: reading of the tree, from the comparison set in the adoption record. Each
#: names one direction and one relationship filter; there is no free-form
#: traversal, because a traversal whose shape comes from the question text
#: cannot be bounded or reproduced.
QUESTIONS = ("impact", "dependency", "symbol", "related_tests")

#: Node kinds and edge kinds of the pinned schema. An unrecognized kind is a
#: refusal, not a node this adapter quietly ignores: a graph that carries
#: relationships this code does not model would have its traversals silently
#: truncated by the model rather than by a budget anyone reported.
NODE_KINDS = frozenset({"file", "symbol", "test"})
EDGE_KINDS = frozenset({"calls", "imports", "defines", "references", "tests"})

#: How the provider's own qualification of a relationship maps onto the packet
#: contract's confidence vocabulary. ``ambiguous`` is the important one: the
#: provider resolved the relationship to more than one candidate, which is a
#: claim a recipient must be able to see as unresolved rather than read as
#: fact. The contract has no ``ambiguous`` value, so it maps to ``unknown``
#: and also raises the ``unresolved_entities`` omission on the packet.
EVIDENCE_CONFIDENCE = {"extracted": "extracted", "inferred": "inferred", "ambiguous": "unknown"}

#: Relationship filters per question, and whether the traversal runs along
#: edges or against them. ``impact`` asks who is affected by a change, which is
#: the reverse of ``dependency`` over the same relationships.
_TRAVERSALS: dict[str, tuple[str, frozenset[str]]] = {
    "impact": ("incoming", frozenset({"calls", "imports", "references", "tests"})),
    "dependency": ("outgoing", frozenset({"calls", "imports", "references"})),
    "symbol": ("both", frozenset({"defines", "calls", "imports", "references", "tests"})),
    "related_tests": ("incoming", frozenset({"tests", "calls", "references"})),
}

#: Depth ceilings per question. ``symbol`` is a neighbourhood, not a walk:
#: what defines this name and what touches it directly.
_DEFAULT_DEPTH = {"impact": 2, "dependency": 2, "symbol": 1, "related_tests": 2}
MAX_DEPTH = 4

#: Budgets. The node budget is what stops a traversal; the rest bound what one
#: packet may carry and are enforced before the contract's own limits so that
#: an over-budget answer is reported as truncated rather than refused at
#: delivery.
DEFAULT_NODE_BUDGET = 40
MAX_NODE_BUDGET = 200
MAX_DOCUMENTS = 16
MAX_CITATIONS_PER_DOCUMENT = 10
MAX_SEEDS = 8

#: Graph bounds. The artifact is already bounded to ``MAX_ARTIFACT_BYTES`` by
#: the lifecycle and its digest is verified before this module reads it, so
#: these bound what one *member* may cost this process to hold and parse.
MAX_GRAPH_BYTES = 64 * 1024 * 1024
MAX_NODES = 200_000
MAX_EDGES = 500_000

#: How much of a blob to read while confirming one line claim. A claim is
#: confirmed as soon as its last claimed line is seen, so a citation into a
#: large generated file costs the lines up to the claim.
_BLOB_CHUNK_BYTES = 256 * 1024


@dataclass(frozen=True)
class GraphNode:
    """One node of the pinned schema, already held to the citation scope rules."""

    id: str
    kind: str
    name: str
    path: str
    start_line: int | None
    end_line: int | None

    @property
    def citation(self) -> str:
        """The node's location as a citation string, with its line span if it has one."""
        if self.start_line is None:
            return self.path
        if self.end_line is None or self.end_line == self.start_line:
            return f"{self.path}#L{self.start_line}"
        return f"{self.path}#L{self.start_line}-L{self.end_line}"


@dataclass(frozen=True)
class GraphEdge:
    """One relationship, with the provider's own qualification of it."""

    source: str
    target: str
    kind: str
    evidence: str


@dataclass(frozen=True)
class CodeGraph:
    """A parsed, bounded, deterministically ordered graph of one generation."""

    generation: str
    commit: str
    nodes: Mapping[str, GraphNode]
    edges: tuple[GraphEdge, ...]
    outgoing: Mapping[str, tuple[GraphEdge, ...]]
    incoming: Mapping[str, tuple[GraphEdge, ...]]

    def seed_matches(self, target: str) -> tuple[tuple[GraphNode, ...], bool]:
        """The seeds a target names, and whether the seed bound dropped any.

        Symbol-first, as the adoption record requires: a bare name resolves to
        the symbols that carry it, and only a target that names no symbol at
        all is read as a path. Ordered by id so two runs against one generation
        seed identically.

        The overflow flag is not cosmetic. A name carried by more than
        ``MAX_SEEDS`` definitions has definitions this traversal will never
        start from, and every relationship reachable only from those is absent
        from the answer. Silently slicing here would let ``run_query`` report a
        complete, untruncated result over a graph it only partly read, which is
        exactly the failure the adoption record's condition 4 is about.
        """
        name = _text(target, maximum=512)
        matches = [node for node in self.nodes.values() if node.name == name]
        if not matches:
            matches = [node for node in self.nodes.values() if node.path == name]
        ordered = tuple(sorted(matches, key=lambda node: node.id))
        return ordered[:MAX_SEEDS], len(ordered) > MAX_SEEDS

    def seeds(self, target: str) -> tuple[GraphNode, ...]:
        """The bounded seed set alone, for callers that do not report truncation."""
        return self.seed_matches(target)[0]


def _member(value: Any, keys: set[str], *, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ContextError(f"local graph {what} fields are missing or unrecognized")
    return value


def _line(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 1 <= value <= 10_000_000:
        raise ContextError("local graph line number is out of range")
    return value


def _node(value: Any) -> GraphNode:
    record = _member(value, {"id", "kind", "name", "path", "start_line", "end_line"}, what="node")
    kind = record["kind"]
    if kind not in NODE_KINDS:
        raise ContextError("unsupported local graph node kind")
    start = _line(record["start_line"])
    end = _line(record["end_line"])
    if start is None and end is not None:
        raise ContextError("local graph line span must start before it ends")
    if start is not None and end is not None and end < start:
        raise ContextError("local graph line span must start before it ends")
    # Held to the citation rules here, at parse time, rather than when a packet
    # is written: a node that could never be cited inside the indexed checkout
    # must not be traversable either, or an out-of-scope path reaches a
    # recipient as a relationship whose citation was quietly dropped.
    node = GraphNode(
        id=_text(record["id"], maximum=512),
        kind=kind,
        name=_text(record["name"], maximum=512),
        path=_text(record["path"], maximum=1024),
        start_line=start,
        end_line=end,
    )
    parse_graph_citation(node.citation)
    return node


def _edge(value: Any, nodes: Mapping[str, GraphNode]) -> GraphEdge:
    record = _member(value, {"source", "target", "kind", "evidence"}, what="edge")
    if record["kind"] not in EDGE_KINDS:
        raise ContextError("unsupported local graph edge kind")
    if record["evidence"] not in EVIDENCE_CONFIDENCE:
        raise ContextError("unsupported local graph edge evidence")
    source = _text(record["source"], maximum=512)
    target = _text(record["target"], maximum=512)
    if source not in nodes or target not in nodes:
        raise ContextError("local graph edge names a node the graph does not carry")
    return GraphEdge(source=source, target=target, kind=record["kind"], evidence=record["evidence"])


def _grouped(edges: Iterable[GraphEdge], *, by: str) -> dict[str, tuple[GraphEdge, ...]]:
    """Adjacency in one fixed order, so a traversal cannot depend on input order."""
    buckets: dict[str, list[GraphEdge]] = {}
    for edge in edges:
        buckets.setdefault(getattr(edge, by), []).append(edge)
    return {
        key: tuple(sorted(group, key=lambda edge: (edge.kind, edge.target, edge.source)))
        for key, group in buckets.items()
    }


def load_graph(payload: Mapping[str, Any], *, generation: str, commit: str) -> CodeGraph:
    """Validate the pinned graph schema. Every unreadable shape is a refusal.

    Read strictly for the same reason the manifest is: this document is
    provider output, and an adapter that repairs what it does not understand
    reports a traversal over a graph nobody reviewed.
    """
    document = _member(payload, {"schema", "nodes", "edges"}, what="document")
    if document["schema"] != GRAPH_SCHEMA:
        raise ContextError("unsupported local graph schema")
    raw_nodes = document["nodes"]
    raw_edges = document["edges"]
    if not isinstance(raw_nodes, list) or len(raw_nodes) > MAX_NODES:
        raise ContextError("local graph node count exceeds its budget")
    if not isinstance(raw_edges, list) or len(raw_edges) > MAX_EDGES:
        raise ContextError("local graph edge count exceeds its budget")
    nodes: dict[str, GraphNode] = {}
    for value in raw_nodes:
        node = _node(value)
        if node.id in nodes:
            raise ContextError("local graph node identifiers must be unique")
        nodes[node.id] = node
    edges = tuple(sorted(
        (_edge(value, nodes) for value in raw_edges),
        key=lambda edge: (edge.kind, edge.source, edge.target),
    ))
    return CodeGraph(
        generation=generation,
        commit=commit,
        nodes=nodes,
        edges=edges,
        outgoing=_grouped(edges, by="source"),
        incoming=_grouped(edges, by="target"),
    )


def read_graph(state: lifecycle.GraphStateRoot, status: lifecycle.GenerationStatus) -> CodeGraph:
    """Read the pinned schema out of a generation whose digest already matched.

    ``status`` must be a usable verdict from ``graph_status``: that is what
    bound the artifact to a commit and verified its digest, and re-deriving
    either here would be a second answer to a question already settled.

    The member is read from the archive as a stream and bounded as it is read.
    The whole artifact is already inside the lifecycle's budget, but one member
    of it is not: a compressed or sparse entry can declare a size this process
    would otherwise allocate before looking at it.
    """
    if not status.usable or status.manifest is None or status.generation is None:
        raise ContextError("local graph is not usable for queries")
    path = state.artifact_path(status.generation)
    try:
        with tarfile.open(path, mode="r:") as archive:
            try:
                info = archive.getmember(GRAPH_MEMBER)
            except KeyError:
                raise ContextError("local graph generation carries no pinned graph document") from None
            if not info.isfile() or info.size > MAX_GRAPH_BYTES:
                raise ContextError("local graph document is missing or exceeds its budget")
            stream = archive.extractfile(info)
            if stream is None:
                raise ContextError("local graph document is unreadable")
            raw = stream.read(MAX_GRAPH_BYTES + 1)
    except (OSError, tarfile.TarError):
        raise ContextError("local graph artifact is unreadable") from None
    if len(raw) > MAX_GRAPH_BYTES:
        raise ContextError("local graph document exceeds its budget")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise ContextError("local graph document is not supported JSON") from None
    return load_graph(payload, generation=status.generation, commit=status.manifest.commit)


@dataclass(frozen=True)
class Relation:
    """One traversal result: a reached node, the edge that reached it, and both ends.

    ``origin`` is the *other endpoint of ``via``* -- the node the walk expanded
    when it found this one -- and never the seed it started from. Those differ
    from the second hop onwards, and conflating them is how a traversal comes
    to assert a relationship the graph does not carry: a two-hop walk from
    ``parse_config`` that reaches ``render`` through ``load`` would otherwise
    read as "render calls parse_config" and cite two locations that have no
    edge between them. ``seed`` keeps the provenance that conflation was
    standing in for, without putting it in the claim.
    """

    node: GraphNode
    via: GraphEdge
    origin: GraphNode
    seed: GraphNode
    depth: int


@dataclass(frozen=True)
class QueryResult:
    """A bounded traversal, with everything a consumer needs to distrust it."""

    question: str
    target: str
    generation: str
    commit: str
    seeds: tuple[GraphNode, ...]
    relations: tuple[Relation, ...]
    truncated: bool
    ambiguous: bool
    omissions: tuple[str, ...]

    @property
    def resolved(self) -> bool:
        return bool(self.seeds)

    def shareable_summary(self) -> dict[str, Any]:
        """Metadata only: counts, states and the binding, never indexed content."""
        return {
            "schema": QUERY_SCHEMA,
            "question": self.question,
            "generation": self.generation,
            "source_revision": self.commit,
            "seeds": len(self.seeds),
            "relations": len(self.relations),
            "truncated": self.truncated,
            "ambiguous": self.ambiguous,
            "omissions": list(self.omissions),
        }


def _neighbours(graph: CodeGraph, node_id: str, direction: str, kinds: frozenset[str]) -> list[tuple[GraphEdge, str]]:
    """Filtered adjacency in a fixed order: the only place direction is read."""
    found: list[tuple[GraphEdge, str]] = []
    if direction in ("outgoing", "both"):
        found.extend((edge, edge.target) for edge in graph.outgoing.get(node_id, ()) if edge.kind in kinds)
    if direction in ("incoming", "both"):
        found.extend((edge, edge.source) for edge in graph.incoming.get(node_id, ()) if edge.kind in kinds)
    return found


def run_query(
    graph: CodeGraph,
    *,
    question: str,
    target: str,
    depth: int | None = None,
    node_budget: int = DEFAULT_NODE_BUDGET,
) -> QueryResult:
    """One deterministic, bounded, relationship-filtered traversal.

    Breadth-first from the seeds in a fixed order, so the same generation and
    the same question produce the same answer every time and a budget cut
    removes the *furthest* relationships rather than arbitrary ones. Reaching
    the budget sets ``truncated``; it never silently shortens the answer.
    """
    if question not in QUESTIONS:
        raise ContextError("unsupported local graph question")
    if type(node_budget) is not int or not 1 <= node_budget <= MAX_NODE_BUDGET:
        raise ContextError("local graph node budget is out of range")
    limit = _DEFAULT_DEPTH[question] if depth is None else depth
    if type(limit) is not int or not 1 <= limit <= MAX_DEPTH:
        raise ContextError("local graph traversal depth is out of range")
    direction, kinds = _TRAVERSALS[question]
    seeds, seed_overflow = graph.seed_matches(target)
    omissions: list[str] = []
    if not seeds:
        return QueryResult(
            question=question, target=target, generation=graph.generation, commit=graph.commit,
            seeds=(), relations=(), truncated=False, ambiguous=False,
            omissions=("unresolved_entities",),
        )
    # More than one definition carries the target's name, so every relationship
    # below is reported from a seed set the provider could not disambiguate --
    # and a name with more definitions than the seed bound allows is the same
    # uncertainty, only worse.
    ambiguous = len(seeds) > 1 or seed_overflow
    seen = {node.id for node in seeds}
    relations: list[Relation] = []
    over_budget = False
    frontier: list[tuple[GraphNode, GraphNode, int]] = [(node, node, 0) for node in seeds]
    while frontier:
        node, seed, level = frontier.pop(0)
        if level >= limit:
            continue
        for edge, other_id in _neighbours(graph, node.id, direction, kinds):
            if other_id in seen:
                continue
            if len(relations) >= node_budget:
                over_budget = True
                break
            seen.add(other_id)
            reached = graph.nodes[other_id]
            # ``node``, not ``seed``: the relationship being reported is the one
            # this edge carries, between the node the walk expanded and the node
            # it just reached. The seed travels alongside as provenance.
            relations.append(Relation(node=reached, via=edge, origin=node, seed=seed, depth=level + 1))
            frontier.append((reached, seed, level + 1))
        if over_budget:
            break
    if question == "related_tests":
        # Relationship-filtered is not the same as answer-filtered: the walk
        # reaches callers so that a test two hops away is found, but only the
        # tests are the answer.
        relations = [item for item in relations if item.node.kind == "test"]
    # Two different ways to have left something out, reported as one state: a
    # relationship budget that stopped the walk, and a seed bound that stopped
    # it from ever starting at some of the target's definitions.
    truncated = over_budget or seed_overflow
    if truncated:
        omissions.append("provider_has_more")
    if ambiguous or any(item.via.evidence == "ambiguous" for item in relations):
        omissions.append("unresolved_entities")
    return QueryResult(
        question=question, target=target, generation=graph.generation, commit=graph.commit,
        seeds=seeds, relations=tuple(relations), truncated=truncated, ambiguous=ambiguous,
        omissions=tuple(dict.fromkeys(omissions)),
    )


class CitationValidator:
    """Confirm citations against the immutable tracked tree of the bound commit.

    Not against the working tree, which is the point. The generation binds one
    commit; the checkout it was built from has since been edited, rebased, or
    left dirty, and a line claim confirmed against an edited file is a claim
    confirmed against a revision nobody asked about. Every path is checked
    against that commit's census -- so an untracked, ignored, or since-deleted
    file is never cited -- and every line claim is confirmed against the blob
    the census names.

    Blob line counts are memoized per blob, so a packet that cites one file
    many times reads it once, and each read stops at the claimed line.
    """

    def __init__(self, repository: Path, census: lifecycle.TrackedCensus):
        self._repository = repository
        self._blobs = {entry.path: entry for entry in census.entries}
        self._counts: dict[str, int | None] = {}

    def tracked(self, path: str) -> bool:
        return path in self._blobs

    def validate(self, citation: str) -> bool:
        """True when the path is tracked at the bound commit and the lines exist."""
        try:
            parsed = parse_graph_citation(citation)
        except ContextError:
            return False
        entry = self._blobs.get(parsed.path)
        if entry is None:
            return False
        if parsed.start_line is None:
            return True
        claimed = parsed.end_line or parsed.start_line
        return claimed <= self._lines(entry)

    def _lines(self, entry: lifecycle.TrackedEntry) -> int:
        cached = self._counts.get(entry.blob)
        if cached is not None:
            return cached
        counted = self._count(entry)
        self._counts[entry.blob] = counted
        return counted

    def _count(self, entry: lifecycle.TrackedEntry) -> int:
        """Lines in one blob of the bound commit, read as a stream.

        A blob is read through ``git cat-file``, never off the working tree:
        the file at that path today may be a different file, or not exist. An
        unreadable blob counts as zero lines, which makes every claim on it
        unresolved rather than silently accepted.
        """
        if entry.size == 0:
            return 0
        try:
            process = subprocess.Popen(
                ["git", "-C", str(self._repository), "--no-optional-locks",
                 "cat-file", "blob", entry.blob],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=lifecycle.git_environment(),
            )
        except (OSError, ValueError):
            return 0
        lines = 0
        trailing = False
        try:
            assert process.stdout is not None
            while True:
                chunk = process.stdout.read(_BLOB_CHUNK_BYTES)
                if not chunk:
                    break
                lines += chunk.count(b"\n")
                trailing = not chunk.endswith(b"\n")
        except OSError:
            return 0
        finally:
            if process.stdout is not None:
                try:
                    process.stdout.close()
                except OSError:
                    pass
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:  # pragma: no cover - git does not hang on a closed pipe
                process.kill()
                process.wait()
        if process.returncode != 0:
            return 0
        # A file whose last line has no terminator still has that line.
        return lines + 1 if trailing else lines


def _relation_text(question: str, item: Relation) -> str:
    """One sentence of metadata about a relationship. Never indexed content.

    Names, paths and relationship kinds only: everything here is already in the
    citations beside it, so the prose adds no claim a recipient cannot check.

    The sentence states exactly the one edge ``via`` carries, between its own
    two endpoints. Where the walk reached that edge from is a separate clause,
    ``reached from``, so a recipient reads a transitive result as a path and
    never as a direct relationship the graph does not assert.
    """
    verb = {
        "calls": "calls", "imports": "imports", "defines": "defines",
        "references": "references", "tests": "tests",
    }[item.via.kind]
    if item.via.source == item.node.id:
        subject, object_ = item.node.name, item.origin.name
    else:
        subject, object_ = item.origin.name, item.node.name
    provenance = "" if item.depth <= 1 else f", reached from {item.seed.name}"
    return (
        f"{question}: {subject} {verb} {object_} "
        f"({item.via.evidence}, hop {item.depth}{provenance}, {item.node.kind} at {item.node.path})"
    )


@dataclass(frozen=True)
class PacketDraft:
    """A packet and the metadata-only account of what it left out."""

    packet: dict[str, Any] = field(repr=False)
    summary: dict[str, Any]


def _documents(
    result: QueryResult, validator: CitationValidator
) -> tuple[list[dict[str, Any]], list[str]]:
    """One document per relationship, with only citations that actually resolve.

    A relationship whose own location cannot be confirmed against the bound
    commit is dropped rather than downgraded: the packet's whole claim is that
    its citations point at the immutable tree, and evidence that cannot be
    pointed at is not weaker evidence, it is none.
    """
    omissions: list[str] = []
    documents: list[dict[str, Any]] = []
    dropped = False
    unvalidated = False
    citations_used = 0
    for item in result.relations:
        if len(documents) >= MAX_DOCUMENTS:
            dropped = True
            break
        # Both endpoints of the edge the sentence states, never the seed the
        # walk started from: a citation is where a recipient goes to check the
        # claim, and the claim is about these two nodes. Keyed by citation and
        # first-write-wins, so a self-referential relationship cites one
        # location once, in a fixed order.
        endpoints: dict[str, GraphNode] = {}
        for endpoint in (item.node, item.origin):
            endpoints.setdefault(endpoint.citation, endpoint)
        cited = [(citation, endpoint) for citation, endpoint in endpoints.items()
                 if validator.validate(citation)]
        if len(cited) != len(endpoints):
            # The graph claimed a location the bound commit does not carry.
            # That is the provider disagreeing with the immutable tree, which
            # a recipient must be told about even when the relationship keeps
            # a second citation that does check out.
            unvalidated = True
        cited = cited[:MAX_CITATIONS_PER_DOCUMENT]
        if not cited:
            dropped = True
            continue
        if citations_used + len(cited) > MAX_GRAPH_CITATIONS:
            dropped = True
            break
        citations_used += len(cited)
        documents.append({
            "text": _relation_text(result.question, item),
            "confidence": EVIDENCE_CONFIDENCE[item.via.evidence],
            "source_kind": "local_repository_graph",
            # Each citation is titled with the node it actually points at, so a
            # two-endpoint relationship does not label the endpoint it came
            # from with the name of the one it reached.
            "citations": [
                {"source": citation, "title": f"{endpoint.kind} {endpoint.name}"}
                for citation, endpoint in cited
            ],
        })
    if unvalidated:
        omissions.append("provider_warning")
    if dropped:
        omissions.append("document_limit")
    return documents, omissions


def build_packet(
    result: QueryResult,
    *,
    validator: CitationValidator,
    envelope: Mapping[str, Any],
    policy: Mapping[str, Any],
    repository: str,
    work_item: str,
    built_at: str,
    completeness: str,
    now: datetime | None = None,
) -> PacketDraft:
    """Turn one traversal into a revision-bound packet a recipient may read.

    The packet binds the graph's commit as ``source_revision`` and the
    generation as its provenance, so a consumer that asked about a different
    revision resolves ``stale`` at delivery without decoding the payload. The
    binding is the authorization envelope's, unchanged: this module decides
    what the evidence is, never who may read it.
    """
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ContextError("context packet time must include a timezone")
    limits = normalize_policy(policy)
    if limits is None:
        raise ContextError("context policy is not configured")
    connection = validate_connection(envelope, now=current)
    if connection["kind"] != "repository":
        raise ContextError("local graph evidence requires a repository-kind connection")
    if completeness not in (lifecycle.COMPLETE, lifecycle.PARTIAL):
        raise ContextError("unsupported local graph completeness")
    if not result.resolved:
        raise ContextError("local graph query resolved no symbol or path to cite")
    documents, dropped = _documents(result, validator)
    if not documents:
        raise ContextError("local graph query produced no citable evidence")
    omissions = list(dict.fromkeys([
        *result.omissions,
        *dropped,
        # The generation's own admission, carried through rather than
        # recomputed: a partial build's answer is partial however complete this
        # traversal was.
        *(["provider_partial"] if completeness == lifecycle.PARTIAL else []),
    ]))
    truncated = result.truncated or "document_limit" in omissions
    packet_completeness = "partial" if truncated or completeness == lifecycle.PARTIAL else "complete"
    expiry = min(
        _timestamp(connection["expires_at"]),
        current + timedelta(seconds=limits["max_age_seconds"]),
    )
    packet = {
        "schema": PACKET_SCHEMA,
        "capability_version": CAPABILITY_VERSION,
        "provider": connection["provider"],
        "kind": "repository",
        "retrieved_at": current.isoformat(),
        "source_revision": result.commit,
        "source_built_at": built_at,
        "completeness": packet_completeness,
        "truncated": truncated,
        "omissions": omissions,
        "documents": documents,
        "binding": {
            **{key: connection[key] for key in ("connection", "generation", "identity", "recipients")},
            "repository": repository,
            "work_item": work_item,
            "policy_version": limits["policy_version"],
            "expires_at": expiry.isoformat(),
        },
    }
    summary = {
        **result.shareable_summary(),
        "graph_generation": result.generation,
        "documents": len(documents),
        "completeness": packet_completeness,
        "truncated": truncated,
        "omissions": omissions,
        "recipients": list(connection["recipients"]),
    }
    return PacketDraft(packet=packet, summary=summary)


#: What a caller does next when the graph cannot answer. ``required_unavailable``
#: is the blocking outcome and ``optional_unavailable`` the degrading one; the
#: words match ``context_prepare`` so a caller branches on one vocabulary.
AVAILABLE = "available"
REQUIRED_UNAVAILABLE = "required_unavailable"
OPTIONAL_UNAVAILABLE = "optional_unavailable"


@dataclass(frozen=True)
class GraphContext:
    """The answer, or the reason there is none and what that means for the work."""

    status: str
    summary: dict[str, Any]
    packet: dict[str, Any] | None = field(default=None, repr=False)

    @property
    def dependent_work(self) -> str:
        return "paused" if self.status == REQUIRED_UNAVAILABLE else "usable"

    @property
    def exit_code(self) -> int:
        return 1 if self.status == REQUIRED_UNAVAILABLE else 0


def _unavailable(required: bool, reason: str, detail: Mapping[str, Any] | None = None) -> GraphContext:
    return GraphContext(
        status=REQUIRED_UNAVAILABLE if required else OPTIONAL_UNAVAILABLE,
        summary={
            "schema": QUERY_SCHEMA,
            "status": REQUIRED_UNAVAILABLE if required else OPTIONAL_UNAVAILABLE,
            "reason": reason,
            "dependent_work": "paused" if required else "usable",
            "next_action": (
                "Refresh or build the local graph for this revision, then retry; dependent work is blocked."
                if required else
                "Continue with ordinary repository tools; optional graph context was not used."
            ),
            **(dict(detail) if detail else {}),
        },
    )


def graph_context(
    repository: Path,
    *,
    question: str,
    target: str,
    envelope: Mapping[str, Any],
    policy: Mapping[str, Any],
    context_repository: str,
    work_item: str,
    root: Path | None = None,
    revision: str = "HEAD",
    depth: int | None = None,
    node_budget: int = DEFAULT_NODE_BUDGET,
    now: datetime | None = None,
) -> GraphContext:
    """Answer one question from the published generation, or say why not.

    This is the whole decision in one call, and the order matters. Usability is
    settled first, against ``graph_status``, which is what binds the revision
    and verifies the artifact digest: a graph that is absent, stale, partial,
    corrupt or oversized never reaches a traversal. Only then is the graph
    read, queried, and turned into evidence.

    Required context that is unavailable blocks the dependent work. Optional
    context that is unavailable returns ``optional_unavailable`` with
    ``dependent_work`` usable, which is the contract's way of saying: carry on
    with ordinary repository tools. Neither outcome raises, because "no graph"
    is a normal state of an opt-in feature, not a failure of the caller.
    """
    limits = normalize_policy(policy)
    if limits is None:
        raise ContextError("context policy is not configured")
    required = bool(limits["required"])
    if question not in QUESTIONS:
        raise ContextError("unsupported local graph question")
    state = lifecycle.GraphStateRoot(repository, root=root)
    status = lifecycle.graph_status(repository, root=root, revision=revision)
    if not status.usable:
        return _unavailable(required, status.state, {"detail": status.detail})
    manifest = status.manifest
    assert manifest is not None  # a usable status always carries one
    try:
        graph = read_graph(state, status)
        census = lifecycle.read_tracked_census(state.repository, manifest.commit)
    except ContextError as error:
        return _unavailable(required, "unreadable", {"detail": str(error)})
    result = run_query(graph, question=question, target=target, depth=depth, node_budget=node_budget)
    if not result.resolved:
        return _unavailable(required, "unresolved", {"detail": "the graph carries no such symbol or path"})
    try:
        draft = build_packet(
            result,
            validator=CitationValidator(state.repository, census),
            envelope=envelope,
            policy=policy,
            repository=context_repository,
            work_item=work_item,
            built_at=manifest.built_at,
            completeness=manifest.completeness,
            now=now,
        )
    except ContextError as error:
        return _unavailable(required, "uncitable", {"detail": str(error)})
    return GraphContext(
        status=AVAILABLE,
        summary={"schema": QUERY_SCHEMA, "status": AVAILABLE, "dependent_work": "usable", **draft.summary},
        packet=draft.packet,
    )


__all__: Sequence[str] = (
    "AVAILABLE",
    "CitationValidator",
    "CodeGraph",
    "DEFAULT_NODE_BUDGET",
    "EVIDENCE_CONFIDENCE",
    "GRAPH_MEMBER",
    "GRAPH_SCHEMA",
    "GraphContext",
    "GraphEdge",
    "GraphNode",
    "MAX_NODE_BUDGET",
    "OPTIONAL_UNAVAILABLE",
    "PacketDraft",
    "QUESTIONS",
    "QUERY_SCHEMA",
    "QueryResult",
    "REQUIRED_UNAVAILABLE",
    "Relation",
    "build_packet",
    "graph_context",
    "load_graph",
    "read_graph",
    "run_query",
)
