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

#: The graph document Code Mower reads. This is the pinned provider's own
#: ``graph.json`` at commit ``23f2ffa`` (release 0.9.58), which is the release
#: the lifecycle (#913) pins and archives. There is no Code Mower graph schema
#: and no normalization step between the two: an adapter that required a shape
#: the build never produces would reject every real generation, so this module
#: reads the provider's actual output and does the narrowing itself.
GRAPH_MEMBER = "graph.json"

QUERY_SCHEMA = "code_mower.contextGraphQuery.v1"

#: The two documents that can appear under ``graph.json``, which are *not* the
#: same file in two dialects and are not read as though they were.
#:
#: ``raw_extraction`` is what the lifecycle's own pinned invocation writes.
#: ``context_graph_lifecycle`` requires ``extract --code-only --no-cluster``
#: (``_REQUIRED_EXTRACT_OPTIONS``), and the pinned CLI's ``--no-cluster`` branch
#: dumps the merged extractor result directly -- ``nodes``, ``edges``,
#: ``hyperedges``, token counts, ``extracted_sources`` -- through
#: ``write_json_atomic``. It never builds a NetworkX graph, never calls
#: ``to_json``, and therefore writes no ``directed``, ``multigraph``, ``graph``
#: or ``built_at_commit`` key. Its ``source``/``target`` are the endpoints the
#: extractor's own ``add_edge`` recorded at the call site, so the orientation is
#: the provider's semantic claim and is preserved by reading the edge record.
#:
#: ``node_link`` is ``graphify/export.py::to_json``: a NetworkX
#: ``node_link_data`` document, which always carries ``directed``,
#: ``multigraph``, ``graph`` and ``links``. Its direction is *not* self-evident
#: and is handled separately, below.
GRAPH_FORMAT_RAW = "raw_extraction"
GRAPH_FORMAT_NODE_LINK = "node_link"

#: Top-level keys only a NetworkX ``node_link_data`` document carries. Any one
#: of them means the file came through ``to_json`` rather than the raw
#: ``--no-cluster`` dump, and it is then read under the node-link rules --
#: including the direction requirement -- whichever key names its edge list.
NODE_LINK_MARKERS = ("links", "directed", "multigraph", "graph")

#: The edge list, per format. The raw dump writes ``edges``. ``node_link_data``
#: writes ``links`` at the pinned commit (``to_json`` passes ``edges="links"``)
#: but NetworkX renamed the key, and the pinned validator accepts either, so a
#: node-link document is read under both names.
RAW_EDGE_KEY = "edges"
NODE_LINK_EDGE_KEYS = ("links", "edges")

#: Whether a **node-link** export preserves the direction of its relationships,
#: which every question here depends on and no question here can recover.
#:
#: The clustered build writes a NetworkX graph that is undirected by default
#: (``build.py::build_from_json(directed=False)``). Undirected storage
#: canonicalizes endpoint order, so ``source``/``target`` in an undirected
#: export are an endpoint *pair*, not a caller and a callee. The exporter does
#: try to repair that -- it stashes the true endpoints in ``_src``/``_tgt`` and
#: restores them before writing -- but the restored link carries no record that
#: the repair happened, so a reader cannot tell a restored edge from one whose
#: order came out of node iteration, and an undirected build additionally
#: collapses a pair related in both directions onto whichever it saw first.
#:
#: Every answer this module produces is an oriented claim: ``impact`` and
#: ``dependency`` are the same relationships walked in opposite directions, and
#: even a ``symbol`` neighbourhood states "A calls B" rather than "A and B are
#: adjacent". Reading orientation out of a *node-link* document that does not
#: establish it is how a packet comes to assert the reverse of what the code
#: does, so such a document is refused rather than answered from.
#: ``directed: true`` is the provider's own statement that the graph was stored
#: as a ``DiGraph``, where source and target *are* the edge.
#:
#: This check belongs to the node-link format alone. Demanding the marker of a
#: raw extraction would refuse every generation the lifecycle's own pinned
#: options actually produce, since that path emits no marker and has no
#: undirected container to have lost direction in.
GRAPH_DIRECTED_KEY = "directed"

#: Required node and edge fields, taken from the pinned validator's
#: ``REQUIRED_NODE_FIELDS`` and ``REQUIRED_EDGE_FIELDS``. A record missing one
#: of these is a refusal: the provider's own validator would not have passed
#: it, so a graph carrying it was not produced by the build this adapter was
#: reviewed against.
GRAPH_NODE_FIELDS = ("id", "label", "file_type", "source_file")
GRAPH_EDGE_FIELDS = ("source", "target", "relation", "confidence", "source_file")

#: The pinned validator's ``VALID_FILE_TYPES``. Only ``code`` is traversable
#: here -- the others are the provider's document/paper/image/rationale/concept
#: corpora, which are not the repository relationships #914 is about and carry
#: no line-checkable location in the bound commit.
GRAPH_FILE_TYPES = frozenset({"code", "document", "paper", "image", "rationale", "concept"})
CODE_FILE_TYPE = "code"

#: The pinned validator's ``VALID_CONFIDENCES``, lowercased. The provider
#: writes these uppercase; the packet contract's vocabulary is lowercase, and
#: this is the whole of the difference.
GRAPH_CONFIDENCES = {"EXTRACTED": "extracted", "INFERRED": "inferred", "AMBIGUOUS": "ambiguous"}

#: A node's location in the pinned export is ``source_location``, a string of
#: the form ``L<line>`` written by the extractor's ``add_node``/``add_edge``.
#: Cross-file stubs carry ``""`` -- a real node with no location, which this
#: module keeps traversable and refuses to cite.
_LOCATION_MAX_LINE = 10_000_000

#: The pinned extractor's relation vocabulary, normalized onto the
#: relationships a query filters by. ``contains`` is the extractor's
#: file-to-definition and class-to-member edge, which is what ``defines``
#: means here; ``implements`` is inheritance by another name. The provider's
#: own word is kept on the edge and is what a packet sentence states -- this
#: mapping only decides which traversals an edge participates in.
#:
#: Relations outside this table are *not* a refusal. The pinned validator does
#: not constrain ``relation`` at all, and the provider's LLM extraction emits
#: relations beyond the extractor's fixed set. An unmapped relation is grouped
#: as ``related``: it is carried, it is citable, and it is reachable by the
#: ``symbol`` neighbourhood, but it never stands in for a ``calls`` or an
#: ``imports`` claim it was not.
GRAPH_RELATIONS = {
    "calls": "calls",
    "imports": "imports",
    "defines": "defines",
    "contains": "defines",
    "references": "references",
    "inherits": "inherits",
    "implements": "inherits",
    "tests": "tests",
}
OTHER_RELATION = "related"
RELATION_KINDS = frozenset({*GRAPH_RELATIONS.values(), OTHER_RELATION})

#: The questions a code graph answers better than ``rg`` and an ordinary
#: reading of the tree, from the comparison set in the adoption record. Each
#: names one direction and one relationship filter; there is no free-form
#: traversal, because a traversal whose shape comes from the question text
#: cannot be bounded or reproduced.
QUESTIONS = ("impact", "dependency", "symbol", "related_tests")

#: The node kinds a *query* is expressed in. The pinned export does not carry
#: them: a Graphify node declares ``file_type`` (its corpus) and, for a
#: handful of constructs, ``type`` (e.g. ``namespace``) -- never whether it is
#: a file, a definition, or a test. So these are derived, in ``_node_kind``,
#: from the shape the pinned extractor actually emits, and the derivation is
#: named here rather than hidden because it is the one place this adapter
#: infers something the provider did not say.
NODE_KINDS = frozenset({"file", "symbol", "test"})

#: How the provider's own qualification of a relationship maps onto the packet
#: contract's confidence vocabulary. ``ambiguous`` is the important one: the
#: provider resolved the relationship to more than one candidate, which is a
#: claim a recipient must be able to see as unresolved rather than read as
#: fact. The contract has no ``ambiguous`` value, so it maps to ``unknown``
#: and also raises the ``unresolved_entities`` omission on the packet.
EVIDENCE_CONFIDENCE = {"extracted": "extracted", "inferred": "inferred", "ambiguous": "unknown"}

#: Path segments that make a code file a test in this repository's own layout.
#: A derivation, like ``_node_kind`` itself, and deliberately conservative:
#: naming a non-test file a test would put it in a ``related_tests`` answer.
_TEST_PREFIXES = ("tests/", "test/")
_TEST_DIRECTORIES = ("/tests/", "/test/")
_TEST_STEM_SUFFIXES = ("_test", "_spec")

#: Relationship filters per question, and whether the traversal runs along
#: edges or against them. ``impact`` asks who is affected by a change, which is
#: the reverse of ``dependency`` over the same relationships. The filters are
#: written in the normalized vocabulary of ``GRAPH_RELATIONS``, so an
#: ``implements`` edge participates wherever ``inherits`` does and an unmapped
#: relation participates only in the ``symbol`` neighbourhood.
_TRAVERSALS: dict[str, tuple[str, frozenset[str]]] = {
    "impact": ("incoming", frozenset({"calls", "imports", "references", "tests", "inherits"})),
    "dependency": ("outgoing", frozenset({"calls", "imports", "references", "inherits"})),
    "symbol": ("both", RELATION_KINDS),
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
#: The adapter's own hard ceiling on documents per packet. The selected
#: policy's ``max_documents`` applies on top of it and only ever downward: a
#: packet carries the smaller of the two, and a policy asking for more than this
#: still gets this.
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

#: The syntactic argument list the pinned extractor appends to a callable's
#: label. A real 0.9.58 graph of this repository labels the function
#: ``parse_graph_citation`` as ``parse_graph_citation()`` and the function
#: ``packet`` as ``packet()``, so the label a human would type and the label the
#: provider wrote differ by exactly this suffix.
_CALLABLE_OPEN = "("
_CALLABLE_CLOSE = ")"


def _callable_base(label: str) -> str:
    """A callable label's name with only its trailing argument list removed.

    ``""`` for anything that is not a callable label, and that emptiness is
    load-bearing: ``seed_matches`` compares this against a target that ``_text``
    has already rejected as empty, so a non-callable label can never match by
    both sides being blank.

    Deliberately syntactic and deliberately narrow. The suffix must begin at the
    label's *first* ``(``, must be a balanced parenthesized group, and that group
    must close on the label's last character; the name before it must be
    non-empty and carry no ``)`` of its own. So ``render(int)``,
    ``render(Callable[(int)])`` and ``render()`` all reduce to ``render`` while
    ``render(int))``, ``render()x``, ``render(int`` and ``()`` reduce to nothing.
    It is not a parser: the pinned extractor writes the label, this reads the one
    suffix it writes, and everything else stays an exact comparison.
    """
    if not label.endswith(_CALLABLE_CLOSE):
        return ""
    opened = label.find(_CALLABLE_OPEN)
    if opened <= 0:
        return ""
    base = label[:opened]
    if _CALLABLE_CLOSE in base:
        return ""
    depth = 0
    for index in range(opened, len(label)):
        character = label[index]
        if character == _CALLABLE_OPEN:
            depth += 1
        elif character == _CALLABLE_CLOSE:
            depth -= 1
            if depth == 0:
                # The first group closes here. Anything after it means the tail
                # is not one argument list, so the label is left alone.
                return base if index == len(label) - 1 else ""
    return ""


@dataclass(frozen=True)
class GraphNode:
    """One node of the pinned export, narrowed and held to the citation rules.

    ``path`` is the export's ``source_file`` and ``line`` is its
    ``source_location``. Both may be absent: the pinned extractor emits
    *sourceless stubs* for cross-file references it could not resolve locally
    (``source_file`` and ``source_location`` set to ``""``), so that a
    corpus-level pass can collapse them onto a real definition. Those nodes are
    real relationships and stay traversable; they are simply not citable, and
    ``citation`` is ``None`` for them rather than a path that points nowhere.
    """

    id: str
    kind: str
    name: str
    path: str
    line: int | None

    @property
    def citation(self) -> str | None:
        """The node's location as a citation, or ``None`` if it has no location.

        The pinned export records a single line per node, not a span, so a
        located node cites one line. Claiming a span the provider never stated
        would be this adapter inventing the extent of a definition.
        """
        if not self.path:
            return None
        if self.line is None:
            return self.path
        return f"{self.path}#L{self.line}"


@dataclass(frozen=True)
class GraphEdge:
    """One relationship, with the provider's own word for it and its own caveat.

    ``relation`` is what the provider wrote; ``kind`` is that relation
    normalized onto ``GRAPH_RELATIONS`` for filtering. A packet sentence states
    ``relation``, so a recipient reads the provider's claim and not this
    module's grouping of it.
    """

    source: str
    target: str
    relation: str
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
    #: Code nodes that lost at least one relationship because its other endpoint
    #: is an id the provider's node list never declared at all. Distinct from a
    #: relationship onto a corpus this module deliberately does not query: that
    #: endpoint *was* declared, and dropping it is a stated scope rather than
    #: missing evidence. A traversal that touches one of these nodes is reporting
    #: a neighbourhood the document itself could not state in full, and says so.
    incomplete: frozenset[str] = frozenset()

    def seed_matches(self, target: str) -> tuple[tuple[GraphNode, ...], bool]:
        """The seeds a target names, and whether the seed bound dropped any.

        Symbol-first, as the adoption record requires: a bare name resolves to
        the symbols that carry it, and only a target that names no symbol at
        all is read as a path. Ordered by id so two runs against one generation
        seed identically.

        Resolution is three ordered tiers, and a later tier is consulted only
        when every earlier one is empty, so a literal label always wins over
        the same string read as a bare callable and both win over a path:

        1. The provider's label, exactly as written.
        2. The provider's *canonical callable label* with only its syntactic
           trailing argument list removed (``_callable_base``). A real pinned
           graph labels a function ``parse_graph_citation()``, so without this
           tier the natural bare spelling of a function name resolves to
           nothing and every query about it answers "unresolved".
        3. The path, exactly as written.

        Tier 2 is an equality test against a label with one suffix stripped --
        not a prefix, substring, or edit-distance match. ``parse_graph`` does
        not reach ``parse_graph_citation()``, and two overloads that differ
        only in their argument lists both match, which is real ambiguity and is
        reported as such rather than resolved by picking one.

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
            matches = [
                node for node in self.nodes.values() if _callable_base(node.name) == name
            ]
        if not matches:
            matches = [node for node in self.nodes.values() if node.path == name]
        ordered = tuple(sorted(matches, key=lambda node: node.id))
        return ordered[:MAX_SEEDS], len(ordered) > MAX_SEEDS

    def seeds(self, target: str) -> tuple[GraphNode, ...]:
        """The bounded seed set alone, for callers that do not report truncation."""
        return self.seed_matches(target)[0]


def _required(value: Any, fields: Sequence[str], *, what: str) -> Mapping[str, Any]:
    """A provider record with every field its own validator requires.

    Required fields only. Unrecognized *extra* keys are not a refusal here,
    which is a deliberate change from reading an invented schema: the pinned
    exporter already annotates nodes with ``community``, ``community_name`` and
    ``norm_label``, edges with ``confidence_score`` and ``weight``, and either
    with a free-form ``metadata`` dict whose contents come from an LLM
    extraction. None of those change a traversal. Refusing them would reject
    every real generation -- which is exactly what a strict reader of a
    hand-written schema did.

    What *is* strict: every field this module reads is read by name, bounded,
    and validated against the pinned vocabulary. Nothing else is looked at.
    """
    if not isinstance(value, Mapping):
        raise ContextError(f"local graph {what} must be an object")
    missing = [field_name for field_name in fields if field_name not in value]
    if missing:
        raise ContextError(f"local graph {what} is missing required provider fields")
    return value


def _maybe_text(value: Any, *, maximum: int) -> str:
    """Bounded single-line text, or ``""`` for a field the provider left empty.

    ``_text`` rejects the empty string, which is correct for every identifier
    in the packet contract and wrong for exactly one field here: a sourceless
    stub's ``source_file``. Everything non-empty goes through ``_text``
    unchanged, so the bound and the control-character rules are the same ones.
    """
    if value is None or value == "":
        return ""
    return _text(value, maximum=maximum)


def _location(value: Any) -> int | None:
    """Parse the pinned export's ``source_location``: ``L<line>``, or nothing.

    The extractor writes ``f"L{line}"``; a sourceless stub writes ``""``. A
    value in any other shape is a refusal rather than a node with an unknown
    location, because a location this module cannot read is one it cannot
    check against the bound commit.
    """
    if value is None or value == "":
        return None
    text = _text(value, maximum=32)
    if not text.startswith("L") or not text[1:].isdigit():
        raise ContextError("local graph source location is not a supported provider location")
    line = int(text[1:])
    if not 1 <= line <= _LOCATION_MAX_LINE:
        raise ContextError("local graph line number is out of range")
    return line


def _node_kind(path: str, label: str, node_type: Any) -> str:
    """Derive a query-level node kind from what the pinned export does carry.

    The provider states no such kind, so this reads the shape its extractor
    emits:

    * A **file** node is the one the extractor creates per file, whose label is
      that file's base name at ``L1`` (``add_node(_make_id(str(path)),
      path.name, 1)``). Matching on the base name is what distinguishes it from
      a definition inside the same file.
    * A **test** is a code node whose file sits in this repository's test
      layout. A path convention, not a provider claim -- ``related_tests``
      answers from it, so it is kept narrow.
    * Everything else is a **symbol**: a definition, a member, a namespace, or
      an unresolved cross-file stub.
    """
    if node_type == "namespace":
        return "symbol"
    if path:
        base = path.rsplit("/", 1)[-1]
        lowered = path.lower()
        stem = base.lower().rsplit(".", 1)[0]
        is_test = (
            lowered.startswith(_TEST_PREFIXES)
            or any(directory in lowered for directory in _TEST_DIRECTORIES)
            or stem.startswith("test_")
            or stem.endswith(_TEST_STEM_SUFFIXES)
        )
        if is_test:
            return "test"
        if base == label:
            return "file"
    return "symbol"


@dataclass(frozen=True)
class _ParsedNode:
    """One provider node record: its declared id, and the code node it is or is not.

    The id survives the ``code``-only filter on purpose. An edge onto an id the
    document *declared* as a document, paper, image, rationale or concept is a
    relationship this module has stated it does not query; an edge onto an id
    the document never declared at all is evidence the provider itself could
    not state. Without keeping the declared ids those two are the same dangling
    edge, and ``_edge`` cannot tell a scope decision from missing evidence.
    """

    id: str
    node: GraphNode | None


def _node(value: Any) -> _ParsedNode:
    """One pinned-export node: a queryable code node, or a declared exclusion.

    A non-``code`` node is dropped rather than refused. The provider indexes
    documents, papers, images, rationales and concepts into the same graph, and
    those are not repository relationships: they carry no location in the bound
    commit, so no traversal here could cite one. Dropping them is bounded --
    every edge that named one becomes a dangling edge, which ``load_graph``
    prunes. No count of the dropped records is kept or reported: what a packet
    states about its own incompleteness is the traversal's truncation and
    omission fields, not a tally of corpora this module never queries. Their
    *ids* are kept, and only so that ``_edge`` can tell this stated exclusion
    apart from an endpoint the document never carried.
    """
    record = _required(value, GRAPH_NODE_FIELDS, what="node")
    # Bounded text before membership: a vocabulary field is looked up in a set,
    # and a JSON array or object there is unhashable, so testing it first
    # raises TypeError out of a reader whose only failure is ``ContextError``.
    file_type = _text(record["file_type"], maximum=64)
    if file_type not in GRAPH_FILE_TYPES:
        raise ContextError("unsupported local graph node file type")
    identifier = _text(record["id"], maximum=512)
    if file_type != CODE_FILE_TYPE:
        return _ParsedNode(id=identifier, node=None)
    path = _maybe_text(record["source_file"], maximum=1024)
    label = _text(record["label"], maximum=512)
    node = GraphNode(
        id=identifier,
        kind=_node_kind(path, label, record.get("type")),
        name=label,
        path=path,
        line=_location(record.get("source_location")),
    )
    # Held to the citation rules here, at parse time, rather than when a packet
    # is written: a node that could never be cited inside the indexed checkout
    # must not be traversable either, or an out-of-scope path reaches a
    # recipient as a relationship whose citation was quietly dropped. A
    # sourceless stub has nothing to hold to the rules and is exempt.
    if node.citation is not None:
        parse_graph_citation(node.citation)
    return _ParsedNode(id=identifier, node=node)


@dataclass(frozen=True)
class _ParsedEdge:
    """One provider link: the relationship it is, or the evidence it costs.

    ``incomplete`` names the endpoints that *are* in the graph on a link whose
    other end the document never declared. Those are the nodes whose reported
    neighbourhood is smaller than the provider's own, so a traversal reaching
    one of them has to say the answer is partial rather than call it complete.
    Both fields empty is the third case, and the only silent one: a link whose
    missing endpoints were all declared exclusions.
    """

    edge: GraphEdge | None
    incomplete: tuple[str, ...]


def _edge(
    value: Any, nodes: Mapping[str, GraphNode], excluded: frozenset[str]
) -> _ParsedEdge:
    """One pinned-export link, and what dropping it costs when it is dropped.

    Pruned rather than refused, which is the pinned exporter's own treatment:
    ``export.py::prune_dangling_edges`` drops links whose endpoints are not in
    the node set and reports a count. But the two links that reach that path
    are not the same fact and must not be reported as one:

    * A link onto a node ``_node`` dropped for its corpus. The document
      declared that endpoint and this module has stated it does not query that
      corpus, so the relationship is out of scope by a rule a recipient can
      read. Dropped silently, as before.
    * A link onto an id the node list never carries at all. The provider
      emitted a relationship and then did not describe one of its ends, so this
      is evidence the document is missing -- not a scope this module chose.
      Reporting a complete answer over such a neighbourhood would state that
      nothing was left out when something was. The surviving endpoint is named
      so the traversal can raise ``provider_partial`` if it reaches it.

    A link whose *every* endpoint is undeclared names no node any traversal can
    start from or reach, so there is nothing to attribute it to and nothing to
    report: no query's answer can be narrowed by it.
    """
    record = _required(value, GRAPH_EDGE_FIELDS, what="edge")
    # Bounded text first, for the same reason as a node's ``file_type``: the
    # lookup below is a dict membership test, which a non-text value turns into
    # a TypeError instead of the refusal this module promises.
    confidence = _text(record["confidence"], maximum=64)
    if confidence not in GRAPH_CONFIDENCES:
        raise ContextError("unsupported local graph edge confidence")
    relation = _text(record["relation"], maximum=128)
    source = _text(record["source"], maximum=512)
    target = _text(record["target"], maximum=512)
    endpoints = ((source, source in nodes), (target, target in nodes))
    if all(present for _, present in endpoints):
        return _ParsedEdge(
            edge=GraphEdge(
                source=source,
                target=target,
                relation=relation,
                kind=GRAPH_RELATIONS.get(relation, OTHER_RELATION),
                evidence=GRAPH_CONFIDENCES[confidence],
            ),
            incomplete=(),
        )
    if all(present or endpoint in excluded for endpoint, present in endpoints):
        # Every absent end was a node the document declared and this module
        # deliberately does not query. A stated scope, not missing evidence.
        return _ParsedEdge(edge=None, incomplete=())
    return _ParsedEdge(
        edge=None,
        incomplete=tuple(endpoint for endpoint, present in endpoints if present),
    )


def _graph_format(payload: Mapping[str, Any]) -> str:
    """Which of the provider's two ``graph.json`` documents this is.

    The discriminator is the presence of a NetworkX node-link marker, not the
    name of the edge list. ``node_link_data`` always writes ``directed``,
    ``multigraph`` and ``graph`` alongside its links, and the raw
    ``--no-cluster`` dump writes none of them -- it writes the extractor's own
    merged result, whose only structural keys are ``nodes`` and ``edges``.
    Keying off ``edges`` instead would misread a newer NetworkX node-link
    document, which names its links ``edges``, as a raw extraction and so skip
    the direction requirement the node-link format needs.
    """
    if any(key in payload for key in NODE_LINK_MARKERS):
        return GRAPH_FORMAT_NODE_LINK
    return GRAPH_FORMAT_RAW


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
    """Read the pinned provider's ``graph.json`` into a bounded queryable graph.

    The document is the pinned provider's own output, so what is validated here
    is the provider's own contract -- the required fields of its validator, its
    ``file_type`` and ``confidence`` vocabularies, its ``L<line>`` locations --
    and not a shape Code Mower invented. Every value this module reads is read
    by name and bounded; every value it does not read is left alone.

    The *supported* document is the raw extraction the lifecycle's pinned
    ``extract --code-only --no-cluster`` writes. A NetworkX node-link export is
    also read, because a generation may have been produced by the clustered
    path, but its provenance differs and it is held to the extra direction
    requirement that path needs. The two are told apart structurally in
    ``_graph_format``, never by guessing from an edge key.

    Two provenance checks are worth more than any field check. The first is
    ``built_at_commit``: the *exporter* stamps the commit the graph was built
    from, and if that disagrees with the commit the generation is bound to then
    the artifact and the manifest describe different revisions, which is a
    refusal no traversal should be run past. The raw path writes no such stamp
    -- it bypasses ``to_json`` entirely -- so for it this check is vacuous and
    the binding rests on the lifecycle's own commit binding and on the second
    check: the census every citation goes through later.
    """
    if not isinstance(payload, Mapping):
        raise ContextError("local graph document must be an object")
    graph_format = _graph_format(payload)
    raw_nodes = payload.get("nodes")
    if graph_format == GRAPH_FORMAT_RAW:
        raw_edges = payload.get(RAW_EDGE_KEY)
    else:
        raw_edges = next((payload[key] for key in NODE_LINK_EDGE_KEYS if key in payload), None)
    if raw_edges is None or raw_nodes is None:
        raise ContextError("local graph document carries no provider nodes and edges")
    if not isinstance(raw_nodes, list) or len(raw_nodes) > MAX_NODES:
        raise ContextError("local graph node count exceeds its budget")
    if not isinstance(raw_edges, list) or len(raw_edges) > MAX_EDGES:
        raise ContextError("local graph edge count exceeds its budget")
    # Before a single edge is read, because direction is not a property of any
    # one link: an undirected node-link export's endpoints are a pair the
    # storage ordered, and no traversal, filter or sentence below can be honest
    # about a relationship whose orientation the document never stated. A raw
    # extraction is exempt because its endpoints never passed through a NetworkX
    # container at all -- see ``GRAPH_DIRECTED_KEY``.
    if graph_format == GRAPH_FORMAT_NODE_LINK and payload.get(GRAPH_DIRECTED_KEY) is not True:
        raise ContextError(
            "local graph node-link export does not preserve relationship direction; "
            "rebuild the generation with the pinned --no-cluster extraction or as a "
            "directed graph"
        )
    stamped = payload.get("built_at_commit")
    if stamped is not None and _text(stamped, maximum=64) != commit:
        raise ContextError("local graph was built from a different commit than its generation")
    nodes: dict[str, GraphNode] = {}
    excluded: set[str] = set()
    for value in raw_nodes:
        parsed = _node(value)
        node = parsed.node
        if node is None:
            excluded.add(parsed.id)
            continue
        if node.id in nodes:
            # The provider's own validator does not check this, but neither of
            # its write paths can produce it: the raw dump runs its node list
            # through ``build.dedupe_nodes`` and the clustered one through a
            # NetworkX node set, and both collapse same-id nodes. A document
            # that carries two was not written by the pinned provider.
            raise ContextError("local graph node identifiers must be unique")
        nodes[node.id] = node
    frozen = frozenset(excluded)
    parsed_edges = [_edge(value, nodes, frozen) for value in raw_edges]
    edges = tuple(sorted(
        (item.edge for item in parsed_edges if item.edge is not None),
        key=lambda edge: (edge.kind, edge.source, edge.target),
    ))
    # Attributed to the surviving endpoint rather than counted: a traversal that
    # never reaches one of these nodes is not answering over missing evidence
    # and must not claim it is, and one that does reach it has to say so.
    incomplete = frozenset(
        endpoint for item in parsed_edges for endpoint in item.incomplete
    )
    return CodeGraph(
        generation=generation,
        commit=commit,
        nodes=nodes,
        edges=edges,
        outgoing=_grouped(edges, by="source"),
        incoming=_grouped(edges, by="target"),
        incomplete=incomplete,
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

    Node expansion and relationship reporting are bounded separately. A node is
    walked through once; a distinct directed relationship is reported once,
    including when both of its endpoints have already been seen. Anything left
    out is left out by the budget or the depth limit, and says so.
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
    # Two separate identities, because they answer two separate questions.
    # ``expanded`` bounds the *walk*: a node is stepped through once, which is
    # what keeps the traversal linear and terminating. ``reported`` bounds the
    # *answer*: a distinct directed relationship is stated once. Sharing one
    # set between them silently deleted evidence -- if A calls B and B calls A,
    # the second edge was suppressed because its endpoint had been walked, and
    # every relationship among a path's seeds disappeared because all of its
    # endpoints were seeds -- while the result still claimed to be complete.
    expanded = {node.id for node in seeds}
    # Did this traversal read a neighbourhood the provider's own document could
    # not state in full? Set from the nodes the walk actually touches, never
    # from the graph as a whole: a dangling endpoint somewhere else in the
    # repository is not a hole in *this* answer, and marking every query partial
    # because of one would make the flag mean nothing.
    incomplete = any(node.id in graph.incomplete for node in seeds)
    reported: set[tuple[str, str, str, str, str]] = set()
    relations: list[Relation] = []
    over_budget = False
    frontier: list[tuple[GraphNode, GraphNode, int]] = [(node, node, 0) for node in seeds]
    while frontier:
        node, seed, level = frontier.pop(0)
        if level >= limit:
            continue
        for edge, other_id in _neighbours(graph, node.id, direction, kinds):
            # The provider's own record, endpoints and wording together: two
            # parallel edges that say different things about the same pair are
            # two relationships, a self-loop reached from both sides is one,
            # and a byte-identical duplicate record is one.
            identity = (edge.source, edge.target, edge.relation, edge.kind, edge.evidence)
            if identity in reported:
                continue
            if len(relations) >= node_budget:
                over_budget = True
                break
            reported.add(identity)
            reached = graph.nodes[other_id]
            incomplete = incomplete or other_id in graph.incomplete
            # ``node``, not ``seed``: the relationship being reported is the one
            # this edge carries, between the node the walk expanded and the node
            # it just reached. The seed travels alongside as provenance.
            relations.append(Relation(node=reached, via=edge, origin=node, seed=seed, depth=level + 1))
            # Reporting the edge never re-queues an endpoint the walk has
            # already stepped through, so retaining these relationships costs
            # the bound nothing: the frontier still holds each node once.
            if other_id not in expanded:
                expanded.add(other_id)
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
    if incomplete:
        # The provider declared a relationship onto an end it never described,
        # so the node this walk read has neighbours no reader of this document
        # can name. Not ``truncated`` -- no budget and no depth limit cut this,
        # the evidence was never in the artifact -- but still partial.
        omissions.append("provider_partial")
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
    # The provider's own word for the relationship, not this module's grouping
    # of it: an ``implements`` edge is filtered as ``inherits`` but must read as
    # "implements", and an LLM-extracted relation outside the mapped set must
    # read as itself rather than as the ``related`` bucket it was filed under.
    verb = item.via.relation
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
    result: QueryResult, validator: CitationValidator, budget: int
) -> tuple[list[dict[str, Any]], list[str]]:
    """One document per relationship, with only citations that actually resolve.

    A relationship whose own location cannot be confirmed against the bound
    commit is dropped rather than downgraded: the packet's whole claim is that
    its citations point at the immutable tree, and evidence that cannot be
    pointed at is not weaker evidence, it is none.

    ``budget`` is the selected policy's document allowance. The effective
    ceiling is the smaller of it and this adapter's own ``MAX_DOCUMENTS``, so a
    policy may only tighten what one packet carries, never lift the adapter
    limit. Answering past the policy's budget is not an option: the contract
    refuses such a packet at delivery, which turns an honestly truncated answer
    into no answer at all.
    """
    omissions: list[str] = []
    documents: list[dict[str, Any]] = []
    limit = min(MAX_DOCUMENTS, budget)
    dropped = False
    unvalidated = False
    citations_used = 0
    for item in result.relations:
        if len(documents) >= limit:
            dropped = True
            break
        # Both endpoints of the edge the sentence states, never the seed the
        # walk started from: a citation is where a recipient goes to check the
        # claim, and the claim is about these two nodes. Keyed by citation and
        # first-write-wins, so a self-referential relationship cites one
        # location once, in a fixed order.
        # A sourceless stub contributes no citation at all -- it is an
        # unresolved cross-file reference, which is the one endpoint shape the
        # pinned extractor emits with no location to point at. It counts as an
        # endpoint the bound commit does not confirm, same as a path the census
        # does not carry.
        endpoints: dict[str, GraphNode] = {}
        located = 0
        for endpoint in (item.node, item.origin):
            citation = endpoint.citation
            if citation is None:
                continue
            located += 1
            endpoints.setdefault(citation, endpoint)
        cited = [(citation, endpoint) for citation, endpoint in endpoints.items()
                 if validator.validate(citation)]
        if len(cited) != len(endpoints) or located < 2:
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
    what the evidence is, never who may read it. What it does check is that the
    two describe one graph -- an envelope authorizing a generation the
    traversal did not read is refused rather than reconciled.
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
    # The binding names one generation, and the freshness rules the delivery
    # contract enforces all read it: a rebuilt graph is refused because the
    # published generation moved. Copying the envelope's word for it would make
    # that check vacuous whenever the traversal came from a different
    # generation than the one authorized -- the packet would claim provenance
    # it does not have, and still pass every later comparison. The envelope is
    # not rewritten to match, because it is an authorization this module did
    # not mint: the disagreement is the refusal.
    if connection["generation"] != result.generation:
        raise ContextError("local graph evidence is not from the authorized graph generation")
    if completeness not in (lifecycle.COMPLETE, lifecycle.PARTIAL):
        raise ContextError("unsupported local graph completeness")
    if not result.resolved:
        raise ContextError("local graph query resolved no symbol or path to cite")
    documents, dropped = _documents(result, validator, limits["max_documents"])
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
    # ``provider_partial`` covers both of its sources -- the generation's own
    # partial build, added just above, and a traversal that read a relationship
    # whose far end the document never declared. Neither is truncation, and a
    # packet carrying either must not call itself complete.
    packet_completeness = "partial" if truncated or "provider_partial" in omissions else "complete"
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
    "GRAPH_CONFIDENCES",
    "GRAPH_FILE_TYPES",
    "GRAPH_MEMBER",
    "GRAPH_RELATIONS",
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
