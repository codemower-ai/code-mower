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

## Reading the pinned provider's own output

A generation's artifact is the provider's own state, packed reproducibly. This
adapter reads one member of it, `graph.json`. There is no Code Mower graph
schema and no normalization pass between the build and the query: the lifecycle
archives what the provider wrote, so this is what gets read.

### The supported document: the raw `--no-cluster` extraction

`context_graph_lifecycle` runs the pinned release as
`extract --code-only --no-cluster` and accepts no other options
(`_REQUIRED_EXTRACT_OPTIONS`). At the pinned commit that branch of
`graphify/cli.py` dumps the merged extractor result straight to `graph.json`
through `write_json_atomic`. It never builds a NetworkX graph and never calls
`export.py::to_json`, so the document carries **no** `directed`, `multigraph`,
`graph`, `links` or `built_at_commit` key:

```json
{
  "nodes": [
    {"id": "n-config", "label": "parse_config", "file_type": "code",
     "source_file": "example_pkg/config.py", "source_location": "L12",
     "type": "function", "metadata": {"namespace": "example_pkg"}}
  ],
  "edges": [
    {"source": "n-load", "target": "n-config", "relation": "calls",
     "confidence": "EXTRACTED", "source_file": "example_pkg/loader.py",
     "source_location": "L41", "weight": 1.0}
  ],
  "hyperedges": [],
  "input_tokens": 0, "output_tokens": 0,
  "extracted_sources": ["example_pkg/config.py", "example_pkg/loader.py"]
}
```

Direction here is the extractor's own claim. `engine.py::add_edge` writes the
call site's `source` and `target`, and the raw dump carries that record through
unchanged, so reading the edge record reads the provider's semantics. Permuting
the node list cannot reverse a relationship, because nothing about the endpoint
order is derived from node order.

An edge's own `source_file` and `source_location` are that call site — the file
and, where the extractor recorded one, the line the relationship was written
at. This adapter retains both, holds them to the same citation rules as a
node's location, and carries a resolvable one into the packet, so two call
sites of one relationship are two distinguishable pieces of evidence rather
than one relationship with the second one's location discarded.

**Upstream limitation.** The pinned release's `--no-cluster` branch itself
calls `build.dedupe_edges`, keyed by `(source, target, relation)`, before
`graph.json` is written, so two call sites the extractor found while walking
the source may already be down to one record by the time this module reads the
artifact. What this adapter guarantees is narrower and still real: every edge
record the artifact actually carries — including two that differ only by call
site, which the supported directed node-link input carries the same way — is
preserved as a distinct relationship rather than collapsed a second time on the
way to a packet. It does not establish exhaustive live-provider call-site
coverage upstream of that dedupe step.

### The other document: a NetworkX node-link export

A generation built through the clustered path instead carries
`export.py::to_json`'s output — `networkx.json_graph.node_link_data`, which
always writes `directed`, `multigraph`, `graph` and a `links` list (NetworkX
later renamed that key to `edges`; the pinned validator accepts either). That
document is read too, but it is held to one extra requirement the raw format
does not need and does not get:

> **`directed` must be `true`.** The clustered build stores an `nx.Graph` by
> default, and undirected storage canonicalizes endpoint order. `to_json` tries
> to repair this — the build stashes the true endpoints in `_src`/`_tgt` and the
> exporter restores them — but the written link carries no record that the
> repair happened, so a reader cannot distinguish a restored edge from one
> ordered by node iteration, and an undirected build additionally collapses a
> pair related in both directions onto whichever it saw first. Every answer this
> module produces is an oriented claim, so an undirected node-link export is
> refused rather than answered from.

The two are told apart structurally, by the presence of a node-link marker key,
never by guessing from the name of the edge list — otherwise a newer NetworkX
export that names its links `edges` would be misread as a raw extraction and
skip the direction requirement it needs.

`built_at_commit` is likewise a node-link-only stamp. Where it is present it
must equal the commit the generation is bound to; the raw path writes none, and
there the binding rests on the lifecycle's own commit binding and on the census
check every citation goes through.

What is validated is the *provider's* contract, not one of ours, and it is the
same for both documents:

- The required node and edge fields of `graphify/validate.py` — `id`, `label`,
  `file_type`, `source_file` on a node; `source`, `target`, `relation`,
  `confidence`, `source_file` on an edge. A record missing one would not have
  passed the provider's own validator, so it is a refusal here.
- Its vocabularies. `file_type` must be one of the six it defines, and
  `confidence` must be uppercase `EXTRACTED`/`INFERRED`/`AMBIGUOUS`. Lowercase
  is the *packet* vocabulary, and a graph using it was not written by the
  pinned provider.
- Its locations, on a node's `source_location` and an edge's own. Each is
  `L<line>` or empty; anything else — including a non-ASCII digit shape such as
  `L²`, which reads as a digit but is not one `int()` can parse — is a location
  this module could not check against the bound commit, so it refuses rather
  than traversing past it or letting the shape escape as an unhandled error.
  One line per node or call site, never a span: neither document records an
  extent, and claiming one would be this adapter inventing it. A citable path —
  a node's `source_file` or an edge's own — is held to the same repository-
  scope rules either way: absolute, parent-traversing, and indexer-private
  paths are refused, and a record whose path fails that check is not
  traversable at all.

Three things are deliberately *not* refusals, because a real generation carries
them and rejecting them would reject every ordinary one:

- **Extra annotations.** The extractor adds `weight`, `context`, `type` and a
  free-form `metadata` dict from an LLM extraction; a node-link export adds
  `community`, `community_name` and `norm_label` to nodes and
  `confidence_score` to links. None of them changes a traversal, so none is
  read. Everything this module *does* read is read by name and bounded.
- **Relations outside the mapped set.** The provider's validator does not
  constrain `relation` at all. Mapped relations (`calls`, `imports`, `defines`,
  `contains`, `references`, `inherits`, `implements`, `tests`) decide which
  traversals an edge participates in; anything else is grouped as `related`,
  reachable only from the `symbol` neighbourhood. Either way the sentence in
  the packet states the provider's own word, so an `implements` edge reads as
  "implements" and a `supersedes` edge reads as "supersedes".
- **Sourceless stubs and non-code corpora.** The extractor emits nodes with an
  empty `source_file` for cross-file references it could not resolve; those
  stay traversable and are never cited. Nodes whose `file_type` is not `code`
  are dropped, and edges onto a dropped node are pruned — which is the pinned
  exporter's own treatment in `prune_dangling_edges`. No count of the dropped
  records is kept or reported: what a packet says about its own incompleteness
  is the traversal's truncation and omission fields, not a tally of corpora
  this module never queries.

Node kinds — `file`, `symbol`, `test` — are **derived**, not read: a Graphify
node declares its corpus and, rarely, a `type`, but never whether it is a file,
a definition, or a test. A file node is the one the extractor emits per file,
whose label is that file's base name; a test is a code node whose path sits in
this repository's test layout; everything else is a symbol. That derivation is
the one place this adapter infers something the provider did not state, and it
is named in `_node_kind` for that reason.

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
breadth-first over adjacency sorted by the full relationship identity described
below — source, target, the provider's relation word, its normalized kind and
confidence, and the call site's path and line — so one generation and one
question produce one answer, every time; permuting the provider's own node or
edge order changes nothing about it; and a budget cut removes the furthest
relationships rather than arbitrary ones.

A target resolves in three ordered tiers, and a later tier is consulted only
when every earlier one is empty:

1. the provider's label, exactly as written;
2. the provider's canonical callable label with only its syntactic trailing
   argument list removed;
3. the path, exactly as written.

Tier 2 exists because the pinned extractor labels a callable with its argument
list: a real 0.9.58 graph of this repository names the function
`parse_graph_citation` as `parse_graph_citation()`. Without it, the natural bare
spelling of a function resolves to nothing and every question about it answers
"unresolved". It is an equality test against a label with one suffix stripped —
not a prefix, substring, or edit-distance match. `parse_graph` does not reach
`parse_graph_citation()`; an attribute `packet` and a function `packet()` stay
different definitions, and the exact label wins; and two overloads that differ
only in their argument lists both resolve, which is real ambiguity and is
reported as `unresolved_entities` rather than settled by picking one.

Reaching the node budget sets `truncated` and raises `provider_has_more`. It
never silently shortens the answer. A target name carried by more definitions
than the seed bound allows does the same: seeds the bound drops take their whole
reachable neighbourhood out of the answer, so that is truncation too, not a
complete result over the seeds that happened to sort first.

The depth limit is the third. A walk that stops at its requested depth on a node
that still has eligible relationships behind it has left evidence out, and
saying so is the whole point of the limit being explicit: for `a -> b -> c -> d`
a default `dependency` question about `a` ends at `c`, and a reader told that
answer is complete would conclude `c` depends on nothing. So the boundary sets
`truncated` and raises `provider_has_more`, exactly as the node budget does.

It is not raised for reaching the boundary as such. Eligibility is measured the
way the walk measures it — this question's direction and relationship filter,
against relationships not already reported — so a chain that genuinely ends at
the boundary stays `complete`, and so does a cycle or a `symbol` neighbourhood
whose boundary edges were already stated from their other side. A parallel edge
the provider worded differently is a different relationship and does count.

Every reported relationship is the one edge the walk crossed, between that
edge's own two endpoints. A second-hop result names the intermediate node and
cites it — `render calls load (inferred, hop 2, reached from parse_config, …)` —
rather than asserting a direct relationship between the seed and the node two
hops away, which the graph does not carry.

What bounds the walk and what bounds the answer are two different things. A node
is stepped through once, which is what keeps a traversal linear and terminating.
A *relationship* is reported once per distinct provider edge record — its two
endpoints, its own wording, its normalized confidence, and its own call site
(path and, where the provider recorded one, line) together — including when
both endpoints have already been seen. So if `a` calls `b` and `b` calls `a`,
both directions are reported; relationships among the definitions a path target
selects as seeds are reported rather than dropped for having no unseen
endpoint; a walk that reconverges keeps both edges into the node it reached
twice; a self-loop reached from both sides of a `symbol` neighbourhood, or an
edge the provider recorded twice at the same call site, is one relationship;
and the same relationship recorded at two different call sites — two lines, or
the same line in two different files — is two, because the call site is part
of the identity rather than a detail dropped on the way to reporting it.
Nothing about the bound changes: a relationship that does not fit the node
budget still sets `truncated` and raises `provider_has_more`, and the depth
limit still applies and still reports what it stopped, including when what it
stopped on differs from an already-reported relationship only by call site.

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
or an edge's own call site that could never be cited is not traversable either,
and again at citation time.

## A dropped relationship is two different facts

The provider's node list can be missing an edge's endpoint for two reasons, and
they are not reported the same way.

The endpoint was **declared as a corpus this module does not query** — a
document, paper, image, rationale or concept. That is a scope stated in these
rules, the relationship is out of it, and the edge is pruned silently. No count
of those is kept: what a packet says about its own incompleteness is its
truncation and omission fields, not a tally of corpora never queried.

The endpoint was **never declared at all**. The provider stated a relationship
and then described one of its ends nowhere, so this is evidence its own document
does not carry, not a scope this module chose. The surviving endpoint is
recorded, and a traversal that seeds or reaches that node reports
`provider_partial` and delivers a `partial` packet. It is not `truncated`: no
budget or depth limit cut it. Only the nodes the walk actually touches count —
a hole elsewhere in the repository is not a hole in this answer, and marking
every query partial for it would make the flag say nothing.

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

A document's citations are both endpoints of the relationship it states plus,
where the provider recorded a call-site location and it resolves against the
bound commit, the edge's own — titled `call site: <relation>` so a recipient
can tell it apart from either endpoint. A relationship whose call site carries
no location leaves the endpoint citations exactly as before; one whose stated
call site the bound commit does not carry — an untracked path, or a line past
the end of the file — is never presented as verified evidence: the citation is
dropped and the packet still raises `provider_warning`, the same signal an
unresolved endpoint raises.

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

## Guided sessions

`context-graph query` is the standalone verb. The same graph is also reachable
from the ordinary guided route, by registering it as a context connection:

```
code-mower context-graph connect --connection local-graph \
    --repository owner/repo --recipient claude:builder --recipient codex:reviewer
code-mower context-graph connection-status --connection local-graph
code-mower context-graph disconnect --connection local-graph
```

A local connection has no principal, no workspace, and no credential. Nothing
is written to the OS credential vault, no browser opens, and no endpoint is
contacted; the connection's whole state is one checkout and the repositories
and recipients the operator approved for it. `session context prepare` then
reaches it through the shared packet store, with the same protected handle, the
same authorization scope, the same work item and recipient contract, and the
same attachment and delivery path an organization connection uses:

```
code-mower session context prepare SESSION.json \
    --question impact --query-stdin   # stdin names the symbol or path
```

`--question` is this connection's retrieval source, and the query names the
symbol or repository-relative path. Both are explicit: a graph answers about a
named target, and guessing one out of a work item's prose would produce
confident evidence about whatever happened to match. `--question` defaults to
`symbol`.

What replaces the credential is the graph. Authorization is re-derived from
current local state on every load and every replay — never cached — and the
envelope carries the **published generation** as its `generation`. Two rules
then fall out of the shared packet contract rather than out of new checks:

- A **rebuilt** graph publishes a new generation, so a packet bound to the old
  one no longer matches its envelope and is refused at load.
- A **moved `HEAD`** makes the published generation stale for that revision, so
  authorization fails outright and nothing is delivered.

The revision both rules are evaluated against is the **consuming** one — the
commit the checkout doing the work is at — and it is never defaulted. A guided
session reads it from the directory it is preparing work in; `context fetch`
reads it from the directory it was run in. Neither falls back to resolving
`HEAD` in the checkout the connection was registered against, because that is a
different directory that moves on its own: a graph built for commit A would
otherwise answer work at commit B. A caller that is not in a Git checkout at all
names no revision, and a repository graph refuses rather than guessing one.
Organization context is unaffected: its sources carry document versions that
have no reason to equal a code commit.

Required context that is refused pauses the dependent work; optional context
degrades and the session continues with ordinary repository tools. Claude,
Codex and Devin receive byte-identical approved evidence, and no recipient
needs the provider, the pin, or any Graphify tool to read it.

## Boundary

This change adds no dependency, no background service, and no mandatory
indexing step. Nothing is selected by default: a graph is indexed only when an
operator builds one, and reached from a guided session only when an operator
connects one. Hosted
Graphify, semantic or model-based extraction, clustering, watchers, and provider
API keys remain out of scope and separate decisions.
