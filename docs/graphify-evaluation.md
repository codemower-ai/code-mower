# Graphify local repository provider: evaluation and decision

Status: **defer**. Recorded 2026-09-12 for
[issue #876](https://github.com/codemower-ai/code-mower/issues/876) under
[epic #868](https://github.com/codemower-ai/code-mower/issues/868), and carried
forward to [epic #902](https://github.com/codemower-ai/code-mower/issues/902).

This document does not announce a Graphify integration. Code Mower has no
Graphify dependency, no indexer, no graph cache, and no graph provider in any
default or optional install path. The defer decision changes nothing a user
installs or runs.

## Decision

Defer adopting Graphify as a local repository context provider for v1.3.x. The
shared packet and delivery contract is ready for a local graph provider, and
this change proves that offline. The bounded evaluation of the actual package
did not run, so there is no evidence on which to adopt.

Deferring is the recorded outcome of the acceptance criteria, not a failure to
reach them: criterion one permits adopt **or** defer, and the compatibility
half of the evidence is delivered here. The remaining half needs owner action
(see [Owner actions](#owner-actions)).

## What was established

The contract side of the question is settled and tested offline.

| Question | Result | Evidence |
| --- | --- | --- |
| Can a local graph packet reach a recipient through the existing delivery path? | Yes, with no OAuth principal, workspace, provider SDK, or network. | `GraphPacketDeliveryCompatibilityTests` |
| Is stale graph state explicit to a consumer? | Yes. Revision binding resolves to `matching`, `stale`, or `unknown`, and stale/unknown fail the quality gate regardless of citation quality. | `test_stale_graph_is_detected_at_delivery_and_fails_the_gate`, `test_unknown_revision_binding_is_not_reported_as_fresh` |
| Is incompleteness explicit? | Yes. `completeness` and `truncated` survive into the shareable summary; a `complete` packet may not claim truncation. | `test_truncation_and_completeness_stay_explicit` |
| Is cache and worktree isolation enforceable? | Yes, but only with the new check. The generic packet schema accepts any citation text, so a graph could cite its own cache, a sibling worktree, or an absolute path outside the indexed root. `context_graph` closes that gap. | `test_rejects_citations_outside_the_indexed_checkout`, `test_out_of_scope_citation_rejects_the_whole_packet` |
| Can citation resolution be scored? | Yes. Line claims past end-of-file, missing files, and symlinks escaping the checkout all count as unresolved rather than silently passing. | `test_line_claim_past_end_of_file_is_unresolved`, `test_symlink_out_of_the_checkout_does_not_resolve` |

`src/code_mower/context_graph.py` is stdlib-only and performs no retrieval. It
is worth keeping whether or not Graphify is ever adopted: it hardens the
repository-kind path that [#870](https://github.com/codemower-ai/code-mower/issues/870)
introduced, which until now bounded *who* a packet was for but not *where* its
citations pointed.

## What was not established, and why

The bounded package experiment did not run. Two independent blockers, either
of which is sufficient on its own:

### 1. The package identity is unresolved

The issue scope names the official package as **`Graphify-Labs/graphify`**, with
`graphify.com` as the official site, and states plainly that `graphify.net` is
unaffiliated and "not an approved interchangeable endpoint".

The work order names **`graphifyy==0.9.58`** — a different name, differing from
the official one by a single repeated character. That is the exact shape of the
lookalike the issue's own scope warns about. This evaluation will not resolve
that conflict by assumption in either direction:

- Installing `graphifyy` because a work order named it would defeat the point of
  the scope's warning. A one-character name difference is how a typosquat
  reaches a machine, and the install would run arbitrary package code on the
  owner's Mac.
- Declaring `graphifyy` a typosquat and substituting `graphify` would be an
  equally unverified guess, and would silently evaluate a package the work order
  did not name.

Naming the package to evaluate is an owner decision with a supply-chain
consequence. It is not a judgment call a builder lane should make silently.

### 2. The lane has no network, so the experiment cannot run here

The evaluation requires resolving the package, recording its artifact hash,
installing it into a throwaway environment, indexing a pinned checkout, and
timing queries. Outbound network is denied in this lane:

```text
curl -sS https://pypi.org/pypi/graphifyy/json     # denied
curl -sS https://pypi.org/pypi/graphify/json      # denied
```

Package metadata could not be read even read-only, so the name conflict above
could not be checked against the index either. Python execution is also
unavailable in this lane, so the offline suite in this change was verified by
CI rather than locally — see the PR for the CI result.

## Adopt gate, unchanged and now mechanical

A later evaluation should not re-derive these thresholds. They are taken from
the work order and, where they are machine-checkable, implemented in
`GraphEvidenceReport`:

| Gate | Where it is checked |
| --- | --- |
| No out-of-scope or private file is indexed | `parse_graph_citation` rejects the packet |
| Every citation stays inside the immutable checkout | `parse_graph_citation`, plus symlink-escape handling in the resolver |
| At least 90% of line citations resolve | `GraphEvidenceReport.meets_gate(minimum_resolution=0.9)` |
| Completeness and truncation are explicit | `completeness` / `truncated` in the shareable summary |
| Useful incremental relationships on at least half the graph-suited questions | Human judgment against the comparison set below; not automatable |

Bounds for the run: 1,000 tracked files, 10 minutes, 128 MB graph state,
bounded query output, code-only extraction, and no network during extraction or
query. No clustering, hooks, watcher, hosted service, semantic or model
extraction, MCP HTTP service, or provider API keys. Query logging disabled.

Comparison set against `rg` and ordinary repository inspection: callers,
dependents, related tests, cross-module paths, config coupling, blast radius,
and exact-text negative controls. Record build and query time, disk, citations,
precision and recall, useful extra relationships, false positives, truncation,
and staleness behavior.

## Owner actions

1. Confirm the exact package to evaluate: the distribution name, the index it is
   published to, and the release. State whether `graphifyy` in the work order
   was intended, or whether the official `graphify` package from
   `Graphify-Labs` was meant. Record the answer on issue #876 or #902.
2. Confirm the expected artifact hash for that release, or authorize the
   evaluation to record the hash it observes as the pinned reference.
3. Authorize a network-enabled, isolated environment for the install and index
   run — not a developer checkout, and not this lane's runner as currently
   configured.
4. Confirm whether the evaluation may execute third-party package code at all on
   owner hardware, or whether it must run in a disposable VM or container.
5. Re-dispatch the bounded experiment against this adopt gate once 1–4 are
   answered. The offline harness in this change is the starting point; no part
   of it needs to be rebuilt.

Until 1–4 are answered, no builder lane should install a package under either
name.

## Boundary

Graphify remains out of v1.3.1 and does not block Coworker's 1.3.0 or 1.3.1
completion. This change adds no dependency, no background service, no provider
subscription, and no mandatory indexing step. A hosted Graphify service or
semantic/model extraction remains a separate explicit decision and is not
covered by any adopt decision that follows from this gate.

The fixture in `tests/fixtures/local_graph_contract.json` is invented content
against a generic public example tree. It proves the extension point only; it
is not an export of any indexed repository, and a passing suite is not Graphify
compatibility evidence.
