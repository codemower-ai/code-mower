# v1.3.1 guided context qualification

The guided session workflow is qualified for supervised Claude and Codex use.
The release proof covers both host directions and recovery across process
boundaries without requiring a live organizational-memory account.

## Lifecycle proof

The automated qualification runs the same lifecycle twice:

| Session host | Builder | Independent reviewer |
| --- | --- | --- |
| Codex | Codex | Claude |
| Claude | Claude | Codex |

Each case starts from a work-item-bound session, opens a new private store
object for each phase, prepares one synthetic bounded packet, delivers it to
the builder, attaches it to a synthetic current pull-request head, performs an
independent context-aware review, and returns the private findings to the
builder. The two cases share one retrieval result so host selection cannot
change the evidence bytes.

Assertions cover repository, work item, participant, connection and policy
derivation; current-head and input-revision binding; fresh authorization before
delivery, review and feedback; peer-review separation; and absence of private
handles and identity fields from status output.

## Recovery matrix

Targeted tests stop or fail at every protected mutation boundary:

- before and after packet persistence and work-order creation;
- before GitHub publication, after accepted publication, and after an
  uncertain publication result;
- after a code-head change or explicit packet refresh; and
- before and after private review-feedback persistence.

Retries preserve the saved intent, reconcile trusted remote state, retire stale
bindings, and avoid duplicate retrieval or attachment revisions. The operator
must explicitly request retry after an uncertain publication that cannot be
reconciled.

## Compatibility and boundaries

Sessions without a configured context policy retain their existing behavior.
The lower-level expert commands and request schemas remain supported. The base
package does not load the optional Coworker dependencies. Graphify remains a
future provider candidate and is represented only by the existing neutral
packet contract and synthetic local-graph fixture.

This proof validates workflow consistency, authorization boundaries, recovery,
and packaging. It does not validate retrieval relevance, time saved, monetary
cost, or reviewer promotion. A separate active product-repository pilot retains
ownership of its repository and private account state; its results should be
assessed independently rather than folded into this release test.
