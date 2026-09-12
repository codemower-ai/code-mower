# Graphify local repository provider: evaluation and decision

Status: **adopt, as an optional and bounded local provider**. Recorded
2026-09-12 for
[issue #876](https://github.com/codemower-ai/code-mower/issues/876) under
[epic #868](https://github.com/codemower-ai/code-mower/issues/868), and carried
forward to [epic #902](https://github.com/codemower-ai/code-mower/issues/902).

This document does not announce a shipped Graphify integration. Code Mower still
has no Graphify dependency, no indexer, no graph cache, and no graph provider in
any default install path. The decision records that a bounded local provider is
worth building behind the repository-context contract; it changes nothing a user
installs or runs today.

## Decision

Adopt Graphify as an **optional** local repository context provider, behind the
existing repository-kind context contract, subject to the conditions in
[Adoption conditions](#adoption-conditions).

Adopting is the recorded outcome of the first acceptance criterion, which permits
adopt or defer. Both halves of the evidence now exist:

- the **compatibility half**, proven offline in this change: a repository-kind
  graph packet reaches a recipient through the shared delivery path, and its
  citations can be scope-checked, freshness-checked, and scored;
- the **package half**, from the clean-room run recorded in
  [Clean-room experiment](#clean-room-experiment).

What adoption does *not* authorize: a hosted Graphify service, semantic or
model-based extraction, clustering, a watcher or hooks, an MCP HTTP service,
provider API keys, or any mandatory indexing step. Each of those remains a
separate explicit decision.

## Package record

| Field | Value |
| --- | --- |
| Official repository | `Graphify-Labs/graphify` |
| Official site | `graphify.com` |
| Distribution name | `graphifyy` |
| Evaluated release | `0.9.58` |
| Official tag commit | `23f2ffaa43fd12f25d9eabe91e6d184b5d89b474` |
| Wheel SHA-256 | `e239803288e91c723d6e30540860bd6d5a1dc3f0914b9fc1104b0233e98aaeb8` |

The distribution name differs from the repository name by one character. That is
resolved, not suspicious: `Graphify-Labs/graphify` publishes under `graphifyy`,
and the pinned tag commit and wheel hash above are what an install must match.
`graphify.net` remains unaffiliated and is not an approved interchangeable
endpoint. Any future install pins this release **and** verifies the wheel hash;
an artifact that does not match is a different package regardless of its name.

## Clean-room experiment

Conditions: an immutable detached Code Mower checkout at `7cb2a8e`, model
credentials scrubbed, network blocked, query logging disabled, extraction run as
`extract --code-only --no-cluster --max-workers 4`.

| Measure | Cold build | No-op repeat |
| --- | --- | --- |
| Elapsed | 12.44 s | 1.63 s |
| Code files | 429 | — |
| Nodes | 10,060 | — |
| Edges | 27,808 | — |
| Graph on disk | 13.8 MB | — |
| Total state | 38.4 MB | — |

The run stayed well inside the bounds the work order set (1,000 tracked files,
10 minutes, 128 MB graph state). One defect is recorded rather than smoothed
over: on the no-op repeat, **54 manifest entries were requeued** because they
emitted no nodes. A file that produces no nodes is indistinguishable from a file
that has not been indexed yet, so those entries are re-extracted on every run.
An adapter must not treat "no-op repeat finished quickly" as "the graph is
complete".

## Product constraints this spike revealed

These are requirements on the Code Mower side, not Graphify bugs. They are the
reason adoption is bounded.

1. **Graphify owns no provenance.** The graph carries no repository revision and
   no build timestamp. Code Mower must bind revision and build time itself and
   must resolve `matching` / `stale` / `unknown` on its own, as
   `context_graph` already does. A graph that cannot say which revision it
   describes is not evidence.
2. **Default queries truncate.** Default BFS traversals can return 700–900 nodes
   and truncate. The adapter must issue symbol-first, relationship-filtered
   queries with explicit budgets, and must report truncation rather than
   presenting a truncated traversal as a complete answer.
3. **Graph state stays outside the checkout.** Index state must never be written
   inside the indexed repository. `context_graph` enforces the citation half of
   this by rejecting any citation into `.git`, `.graph`, `.graphify`, or
   `.code-mower`.
4. **Installation stays opt-in.** No default install path acquires the package,
   and no command requires an index to exist.

### Provenance of this record

The clean-room run above was performed independently in an isolated checkout and
reported by the orchestrator on
[PR #924](https://github.com/codemower-ai/code-mower/pull/924); this lane did not
run it and did not install any package. Everything in
[What was established](#what-was-established) is the opposite: it was produced
and is re-checkable in this repository, offline, with no network and no
third-party package. Read the two sections with that difference in mind.

## What was established

The contract side of the question is settled and tested offline.

| Question | Result | Evidence |
| --- | --- | --- |
| Can a local graph packet reach a recipient through the existing delivery path? | Yes, with no OAuth principal, workspace, provider SDK, or network. | `GraphPacketDeliveryCompatibilityTests` |
| Is stale graph state explicit to a consumer? | Yes. Revision binding resolves to `matching`, `stale`, or `unknown`, and stale/unknown fail the quality gate regardless of citation quality. | `test_stale_graph_is_detected_at_delivery_and_fails_the_gate`, `test_unknown_revision_binding_is_not_reported_as_fresh` |
| Is incompleteness explicit? | Yes. `completeness` and `truncated` survive into the shareable summary; a `complete` packet may not claim truncation. | `test_truncation_and_completeness_stay_explicit` |
| Is cache and worktree isolation enforceable? | Yes, but only with the new check. The generic packet schema accepts any citation text, so a graph could cite its own cache, a sibling worktree, or an absolute path outside the indexed root. `context_graph` closes that gap. | `test_rejects_citations_outside_the_indexed_checkout`, `test_excluded_roots_are_matched_case_insensitively`, `test_rejects_excluded_directories_at_any_depth`, `test_out_of_scope_citation_rejects_the_whole_packet` |
| Does the policy hold when a symlink hides the real target? | Yes. The declared path and the resolved repository-relative target are held to the same policy, so an escaping link or an alias such as `metadata -> .git` rejects the packet — including for a citation with no line span, which is never scored. | `test_symlink_out_of_the_checkout_rejects_the_packet`, `test_file_only_symlink_out_of_the_checkout_rejects_the_packet`, `test_alias_symlink_into_excluded_state_rejects_the_packet`, `test_symlink_inside_the_checkout_still_resolves` |
| Can citation resolution be scored? | Yes. Line claims past end-of-file and missing files count as unresolved rather than silently passing. Scope is a gate rather than a score, so an out-of-scope citation cannot be averaged away by healthy ones. | `test_line_claim_past_end_of_file_is_unresolved`, `test_line_claim_ending_on_the_last_line_resolves`, `test_missing_file_is_unresolved_rather_than_an_error` |

`src/code_mower/context_graph.py` is stdlib-only and performs no retrieval. It
is worth keeping independent of the Graphify outcome: it hardens the
repository-kind path that
[#870](https://github.com/codemower-ai/code-mower/issues/870) introduced, which
until now bounded *who* a packet was for but not *where* its citations pointed.

Constraint 1 above is the reason this half matters. Because the graph carries no
revision of its own, the staleness binding Code Mower owns is the only thing
standing between a consumer and confidently wrong evidence.

## Adopt gate

A later implementation should not re-derive these thresholds. They are taken from
the work order and, where they are machine-checkable, implemented in
`GraphEvidenceReport`:

| Gate | Where it is checked |
| --- | --- |
| No out-of-scope or private file is indexed | `parse_graph_citation` rejects the packet |
| Every citation stays inside the immutable checkout | `parse_graph_citation` on the declared path, plus the resolved-target scope check in `evaluate_graph_evidence` |
| At least 90% of line citations resolve | `GraphEvidenceReport.meets_gate(minimum_resolution=0.9)` |
| Completeness and truncation are explicit | `completeness` / `truncated` in the shareable summary |
| Useful incremental relationships on at least half the graph-suited questions | Human judgment against the comparison set below; not automatable |

Comparison set against `rg` and ordinary repository inspection: callers,
dependents, related tests, cross-module paths, config coupling, blast radius, and
exact-text negative controls. Record build and query time, disk, citations,
precision and recall, useful extra relationships, false positives, truncation,
and staleness behavior.

## Adoption conditions

Adoption is conditional on the implementing change meeting all of these. They
are engineering conditions on the adapter, not requests for a decision.

1. The package is pinned to `graphifyy==0.9.58` with the wheel hash above
   verified at install time, and acquiring it stays opt-in.
2. Index state is written outside the indexed checkout.
3. Code Mower binds revision and build time and surfaces
   `matching`/`stale`/`unknown`; stale and unknown evidence fails the gate.
4. Queries are symbol-first and relationship-filtered with explicit budgets, and
   truncation is reported rather than hidden.
5. Extraction runs code-only, with no clustering, watcher, hooks, hosted service,
   semantic or model extraction, MCP HTTP service, or provider API keys.
6. Evidence passes `GraphEvidenceReport.meets_gate()` before a packet is
   delivered to a recipient.
7. The requeue defect in [Clean-room experiment](#clean-room-experiment) is
   accounted for: an incremental run's completion is not treated as proof the
   graph is complete.

## Boundary

Graphify stays out of v1.3.1 and does not block Coworker's 1.3.0 or 1.3.1
completion. This change adds no dependency, no background service, no provider
subscription, and no mandatory indexing step; the runtime dependency arrives, if
at all, with the implementing change under epic #902.

The fixture in `tests/fixtures/local_graph_contract.json` is invented content
against a generic public example tree. It proves the extension point only; it is
not an export of any indexed repository, and a passing suite is not Graphify
compatibility evidence.
