# Local repository graph: bounded queries and context packets

How Code Mower asks a published local graph a question, and how the answer
becomes evidence a recipient may read. Recorded for
[issue #914](https://github.com/codemower-ai/code-mower/issues/914) under epic
[#902](https://github.com/codemower-ai/code-mower/issues/902).

This is the third and last piece of the optional local-graph path:

- [`context_graph_lifecycle.py`](context-graph-lifecycle.md) (#913) builds and
  publishes an immutable generation bound to one commit and tree.
- `context_graph_query.py` (this document) reads the pinned schema out of that
  generation, answers four bounded questions, and emits a packet.
- [`context_graph.py`](graphify-evaluation.md) (#876) scores a delivered
  packet's citations and freshness.

Nothing here installs, downloads, or runs a provider, and nothing here is on a
default path. The only subprocess is Git, reading blobs of the commit the
generation is already bound to.

## Reading the pinned schema directly

A generation's artifact is the provider's own state, packed reproducibly. This
adapter reads one member of it, `graph.json`, and requires it to declare
`code_mower.contextGraph.v1`:

```json
{
  "schema": "code_mower.contextGraph.v1",
  "nodes": [
    {"id": "n-config", "kind": "symbol", "name": "parse_config",
     "path": "example_pkg/config.py", "start_line": 12, "end_line": 30}
  ],
  "edges": [
    {"source": "n-load", "target": "n-config", "kind": "calls",
     "evidence": "extracted"}
  ]
}
```

Node kinds are `file`, `symbol`, `test`. Edge kinds are `calls`, `imports`,
`defines`, `references`, `tests`. Every other shape is a refusal, not a
best-effort read: an adapter that repairs what it does not understand reports a
traversal over a graph nobody reviewed. The refusals are exhaustive on purpose —
an unknown kind, a dangling edge, a duplicate identifier, an inverted line span,
an unrecognized field, a node path that escapes the indexed checkout.

Reading the member directly, rather than through provider query tooling, is what
makes the traversal reproducible and the bounds ours. It also means a recipient
never needs the provider: the graph is read once, here, by the operator who
built it.

The member is read as a stream and bounded as it is read. The artifact as a
whole is already inside the lifecycle's budget and its digest was verified by
`graph_status` before this module opened it; one member of it is separately
bounded because a declared size is something this process would otherwise
allocate before looking at it.

## The four questions

Free-form traversal is not offered. A traversal whose shape comes from the
question text cannot be bounded or reproduced, and the adoption record's second
product constraint is that default traversals in the evaluated provider returned
700–900 nodes and truncated silently.

| Question | Direction | Relationships | Default depth |
| --- | --- | --- | --- |
| `impact` | against the edges | `calls`, `imports`, `references`, `tests` | 2 |
| `dependency` | along the edges | `calls`, `imports`, `references` | 2 |
| `symbol` | both | all | 1 |
| `related_tests` | against the edges, answering with test nodes only | `tests`, `calls`, `references` | 2 |

Each traversal is symbol-first: a target resolves to the symbols carrying that
name, and only a target that names no symbol at all is read as a path. Each is
breadth-first over adjacency sorted by `(kind, target, source)`, so one
generation and one question produce one answer, every time, and a budget cut
removes the furthest relationships rather than arbitrary ones.

Reaching the node budget sets `truncated` and raises `provider_has_more`. It
never silently shortens the answer. A target name carried by more definitions
than the seed bound allows does the same: seeds the bound drops take their whole
reachable neighbourhood out of the answer, so that is truncation too, not a
complete result over the seeds that happened to sort first.

Every reported relationship is the one edge the walk crossed, between that
edge's own two endpoints. A second-hop result names the intermediate node and
cites it — `render calls load (inferred, hop 2, reached from parse_config, …)` —
rather than asserting a direct relationship between the seed and the node two
hops away, which the graph does not carry.

## Citations are validated against the bound commit

Not against the working tree, which is the point. The generation binds one
commit; the checkout it was built from has since been edited, rebased, or left
dirty, and a line claim confirmed against an edited file is a claim confirmed
against a revision nobody asked about.

Every cited path must appear in that commit's tracked census — so an untracked,
ignored, or since-deleted file is never cited — and every line claim is
confirmed against the blob the census names, read through `git cat-file` and
stopped at the claimed line. Blob line counts are memoized, so a packet that
cites one file many times reads it once.

A location that cannot be confirmed is dropped rather than downgraded: the
packet's whole claim is that its citations point at the immutable tree, and
evidence that cannot be pointed at is not weaker evidence, it is none. The
drop is reported as `provider_warning`, and a relationship left with no citation
at all is reported as `document_limit`.

The scope rules are `context_graph`'s, applied twice: at parse time, so a node
that could never be cited is not traversable either, and again at citation time.

## What the packet carries

An ordinary `code_mower.contextPacket.v1` repository-kind packet — the same
shape every other provider delivers, so it travels the existing delivery path
with no new contract:

| Field | Bound to |
| --- | --- |
| `source_revision` | the generation's commit, so a consumer asking about another revision resolves `stale` at delivery |
| `source_built_at` | the manifest's build time |
| `completeness`, `truncated` | the traversal's budget *and* the generation's own completeness |
| `omissions` | `provider_has_more`, `unresolved_entities`, `provider_warning`, `document_limit`, `provider_partial` |
| `documents[].confidence` | the provider's qualification of each relationship |
| `documents[].citations` | validated file/line references into the bound commit |
| `binding` | the authorization envelope, unchanged |

Confidence maps the provider's own qualification onto the contract's vocabulary:

| Provider evidence | Packet confidence | Meaning |
| --- | --- | --- |
| `extracted` | `extracted` | parsed from the source |
| `inferred` | `inferred` | derived, not read directly |
| `ambiguous` | `unknown` | resolved to more than one candidate |

`ambiguous` also raises `unresolved_entities` on the packet, so a recipient sees
the uncertainty at the packet level and not only per document. A target name
that matches more than one definition does the same.

Document text is metadata about relationships — names, paths, relationship
kinds, hop counts — and never indexed content. Everything in it is already in
the citations beside it, so the prose adds no claim a recipient cannot check.

## Required blocks, optional degrades

Usability is settled before anything is read, against `graph_status`: a graph
that is absent, stale, partial, corrupt or oversized never reaches a traversal.
Neither outcome raises, because "no graph" is a normal state of an opt-in
feature.

| Policy | Graph unusable | `dependent_work` | Exit code |
| --- | --- | --- | --- |
| `required: true` | `required_unavailable` | `paused` | 1 |
| `required: false` | `optional_unavailable` | `usable` | 0 |

The words match `context_prepare`, so a caller branches on one vocabulary.
Optional unavailable context is not a degraded answer — there is no packet at
all, and the next action is to carry on with ordinary repository tools.

## One packet, three recipients

Claude, Codex and Devin receive the same approved bytes through the same
delivery path. The rendered evidence is identical for each: it names no
connection, no credential, no provider tool, and no local path, and a recipient
needs no graph, no provider install, and no Graphify credentials of its own.
Authorization is unchanged — a recipient the envelope does not name is still
refused at load.

## Command

```
code-mower context-graph query --question impact --target parse_config \
    --authorization AUTH.json [--packet-out PACKET.json] \
    [--revision REV] [--depth N] [--node-budget N] [--json]
```

`--authorization` names a JSON file carrying the connection envelope, the
policy, the repository and the work item. It is read from a file the operator
names rather than discovered: this command mints evidence for named recipients,
and which recipients those are is an authorization decision that belongs to the
connection, not to a query.

Standard output is metadata only — counts, states, the bound revision and
generation, and the omission codes. The evidence itself goes to the private file
named by `--packet-out`, created `0600`, or nowhere at all. A destination that
already exists is replaced rather than reopened: a creation mode binds only a
file the open creates, so writing into an existing world-readable path would put
the evidence behind whatever permissions that path already carried. The packet
is written to a freshly created private sibling and renamed over the
destination, which is also atomic — a reader never sees a half-written packet,
and a failed write leaves the previous file untouched.

## Boundary

This change adds no dependency, no background service, and no mandatory
indexing step. It does not wire the graph into `code-mower context fetch` or any
default guided-context selection: a graph packet is produced by an explicit
command, and the operator attaches it through the ordinary path. Hosted
Graphify, semantic or model-based extraction, clustering, watchers, and provider
API keys remain out of scope and separate decisions.
