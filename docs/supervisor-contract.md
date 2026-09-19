# Qualified supervisor adapter v1 and v2

The initial sections describe frozen v1. The opt-in v2 extension is specified in
[Checkpointed clarification and bounded fixes](#checkpointed-clarification-and-bounded-fixes-v2-1017).

Issue #977 introduced `code_mower.supervisor`, the maintained OSS boundary between a
privately authorized Slack task and a configured Code Mower supervisor. The
initial runtime is **Codex**, using its existing repository-policy orchestrator
eligibility. Claude remains an eligible independent reviewer when excluded from
the actual contributor lineage. This does not add a new role qualification or
promote Devin: hosted Devin remains an explicitly selected bounded builder.

The adapter owns admission, claims and bounded delegation decisions. It invokes
the selected Codex runtime for acceptance, dispatch and completion/review
decisions; it cannot infer those decisions from registration or provider exit.
It reuses `session`/`session_lease`, `role_eligibility`, `DevinWorkOrders`,
`RemoteSessions`, `builder_lineage`, and the existing process supervisor.
`session start` still only prepares a brief and lease. No Slack scheduler,
universal process launcher, implicit provider fallback, new tracker-write or
merge capability is added.

## Integration boundary for #919 and #920

The wheel includes `supervisor_contract.schema.json` and
`supervisor_contract.fixtures.json`. Load them with `importlib.resources` from
`code_mower`. The frozen contract is `code_mower.supervisor.v1`; use both the
structural schemas and `supervisor_contract.validate`/`decode`. Unknown versions,
fields, operations and enum values, duplicate JSON keys, nonfinite numbers,
invalid nested records and oversized input fail closed. Fixtures contain invented
bindings and explicitly set `live_hosted_evidence: false`.

| Record / call | Contract and effect |
| --- | --- |
| `admission` → `Supervisor.admit` | Exact opaque tenant/repository/work/run/operation binding, grant revision, runner, expiry and authorized limits. Contacts the configured runtime; no builder create. |
| `claim` | Opaque token, exact admission binding and grant, saved session ID, runtime generation, scope digest and expiry. A private correlation record, **not authentication**. |
| `request` → `Supervisor.operate(action, claim)` | Closed `handoff`, `renew`, `status`, `result`, `cancel` operations. The authenticated bridge validates the request before calling the corresponding method. |
| `result` | Private claim and exact verified PR/branch/head target, plus closed status. No task prose or provider result is returned. |
| `public_status(result)` | The only Board/cloud/public projection. Closed state/reason/action, implementation, builder/review writer observations, review, gate, merge and unchanged `remote_session.v1` lifecycle metadata. No arbitrary strings, identities, paths, hashes, provider references or private bindings. |

`#919` owns durable hosted receipts/queues and retention. `#920` owns the
authenticated bridge, Slack transport and provider connections. They supply
`Authorization.resolve`, the configured runtime, builder and existing review
broker connections. No credentials, signing secrets, tokens or raw ingress
payloads are accepted in the supervisor records. Campaign-authentication
ownership remains with #983. This PR changes none of those source files.

The resolver must independently authenticate the caller **on every call** and
resolve the requested action against the current private authorization store.
It binds the tenant, installation/team/actor, repository, conversation, approved
work item, operation, exact run and session to the stored verified Slack receipt.
It must not construct a grant by copying the bridge body, accept a claim as a
bearer capability, or cache a positive authorization through revocation.
`AuthorizedTask.admission` must equal the validated admission exactly. Its
`WorkOrder`, session, repository policy, registered runner set and optional
context packet come from that same trusted resolution. The adapter also repeats
Slack normalization and compares runner, repository and session bindings.

Admission/handoff and live renewal require start authority. Status requires status authority;
cancel requires an independently authenticated cancellation request. Result is
the authenticated orchestrator collection route, never an inbound Slack
completion command. Messages/clarifications and fixes require existing session
facilities and a separate authorized decision; they are not implicit new writer
or recovery allowances in this v1 boundary.

## Maintained local runtime connection

Construct `supervisor_codex.CodexRuntime` with an explicitly selected absolute
Codex executable, model, private runtime directory and credential store. The
store is `keyring` by default; headless hosts that deliberately use the isolated
home's private `auth.json` pass `credential_store="file"`. No other store is
accepted. The caller retains the already configured runtime authentication; the
adapter neither discovers nor reads credentials. `CodexRuntime` passes that
closed credential-store selection explicitly because `--ignore-user-config`
also ignores the isolated home's non-secret store setting. It runs fixed
`codex exec` invocations with
`--output-schema`, `--output-last-message`, `--ephemeral`, `--ignore-user-config`,
read-only sandboxing and no approvals. Shell execution, apps, plugins, hooks,
multi-agent, browser/computer use and other execution features are disabled;
MCP configuration is empty and web search is disabled. The agent decides through
structured output; builder/review side effects belong to the checked adapter.
An older CLI that cannot support this posture fails closed, without fallback.

Private task text travels on stdin, never in command arguments. Prompt, output,
schema and bounded logs live in a temporary operator-owned private directory
outside Git. The existing `lane_delivery.supervise_process` bounds time/output,
terminates its process group on timeout/interruption and reaps the child. Private
temporary files are removed after every response or failure; dependency errors
become closed diagnostics. A process crash can leave private temporary files:
the hosting service must sweep them within its private input retention window.

Each decision turn receives the exact approved scope digest, current observations
and remaining fixed plan. Codex accepts that scope or rejects it, chooses a cap
at or below the authorized builder ACUs and selects a configured reviewer. The
adapter freezes scope, cap and reviewer for the claim. Further turns receive
that state explicitly; arbitrary CLI sessions are never resumed. A new runtime
connection object has a new generation, invalidating earlier claims.

An embedding composes the maintained pieces as follows (all variables are
already privately configured/resolved objects; this is not a public CLI):

```python
runtime = CodexRuntime(executable=codex_executable, private_root=runtime_root,
                       model=selected_model, credential_store="keyring")
supervisor = Supervisor(root=supervisor_root, runner=registered_runner,
                        authorization=authorized_private_store, runtime=runtime,
                        builder=HostedBuilder(configured_work_orders),
                        reviews=existing_audit_broker)
receipt = supervisor.admit(validated_admission)
if receipt["claim"] is not None:
    progress = supervisor.operate("handoff", receipt["claim"])
```

The root, runner and runtime must be stable for the host process, and the saved
session must hold the live lease for the exact checkout/repository/orchestrator.
There is no automatic lease acquisition or takeover here. Keep the supervisor
and builder private roots stable, tenant isolated and shared by all consumers of
the same work. Do not make a new store to work around a refused reservation.

## Claims, uncertainty and bounded spend

One atomic, fsynced private reservation exists per tenant/repository/work, using
`ContextStore`'s existing locking and path/inode protections. Run and operation
are deliberately excluded from the reservation key and included in its exact
fingerprint: another run cannot silently acquire the same work. Duplicates
return the saved claim without calling the runtime again or creating a builder.
Duplicate receipts carry no fresh exact-head completion assertion; only a fresh
`result` call can report completion.

A claim lasts at most 15 minutes and no later than its admission expiry. `renew`
can extend a still-live exact claim after another successful runtime acceptance,
within that same admission expiry and original runtime-call allowance. It never
resets scope, spend, run or delegation reservations. Old copies of a renewed
claim fail closed; a duplicate admission can retrieve the current receipt after
an ambiguous renewal response. An expired claim cannot be renewed. Every
handoff/re-entry checks the exact stored claim, current authorization, runner,
runtime generation, original private work-order fingerprint, provider connection
and live session lease acquisition. Checks repeat after external/runtime calls
and immediately before delegation. A renewed original lease is acceptable; a
replaced lease, revoked grant, changed scope, changed provider account, changed
run or restarted supervisor is not. An expired claim cannot be renewed into a
new writer. `revoke` is a trusted authorization-store notification, not a public
unauthenticated endpoint.

Limits are trusted owner-approved ceilings: builder ACUs, runtime invocation
count, seconds per runtime invocation, and **one** independent review request.
Runtime calls are durably charged against their count before invocation,
including lost responses. Builder ACUs apply to the existing work-order lifecycle.
The runtime can lower but cannot increase the builder cap. Review transport must
enforce its existing authorized lane spending/time limits. Call/time limits do
not imply a dollar price or prove provider billing termination; paid deployment
still requires an explicit campaign-wide spending cap and stable private
authentication/budget bindings. The hosting authorization service must reserve
that allowance before resolving an executable task. No live or paid session is
authorized by these fixtures, and no new recovery allowance is inferred.

A durable pending marker precedes every runtime/builder/review invocation. A lost
response, crash, disconnect or ambiguous handoff remains pending/uncertain and
cannot cause a repeat create, review request or cancellation. Status never
acknowledges uncertain delivery. A restart requires owner reconciliation through
the existing lifecycle and `lane_handoff` exact-head/quiescence facilities;
unsafe or unknown writer exit never starts a replacement. This adapter provides
no automatic recovery, fix-round, force-takeover or claim-reset operation.

An authenticated cancellation before handoff prevents builder creation. After
handoff, cancellation uses the exact existing remote operation and, when review
has started, the exact audit broker request. It is reserved once. Acceptance is
`cancel_pending` until fresh observations prove both runtimes terminated (or
never started). Ambiguous cancellation stays uncertain even if the transport says
it accepted the request. Cancellation does not need another model call, so it
remains available when the runtime invocation budget is exhausted. An unanswered
decision can also be abandoned for cancellation because the decision runtime has
no builder/review side effects; its invocation allowance is not refunded. Revoked or
stale claims still require owner recovery using the original lifecycle binding.

## Result and review decisions

`HostedBuilder` reuses the existing WorkOrder/round binding and authoritative
GitHub closing-issue/author/repository/branch/head verification. It accepts only
the original bounded round. Logical builder completion and provider writer exit
are observed separately; structured output is never proof of quiescence.

Before review the adapter requires a verified target, terminated writer, ready
selected reviewer and a resolved `builder_lineage.Lineage` for that exact target.
The actual builder must appear in the contributor set and the selected reviewer
must be outside **every** contributor. Missing, stale or conflicting lineage
fails closed. The broker must use existing independently authenticated audit
evidence and authoritative `code-mower/gate` state. It cannot synthesize a pass
from the builder, a provider result, or the supervisor's decision.

Completion requires the runtime's explicit decision, freshly verified PR/head,
terminated builder/review runtimes, eligible independent passing review and passing gate. The
adapter repeats exact-head, writer, authorization, lineage and gate checks after
the runtime decision. A changed head needs separately authorized review/recovery;
the one-review allowance is never automatically expanded. Merge status is a
separate GitHub observation: implementation completion can coexist with an open
PR, and the adapter cannot merge it. Both authority fields remain false.

## Evidence and consumer acceptance

Run `python -m unittest discover -s tests -p test_supervisor.py`. The synthetic
local executable exercises the **production** Codex argv/stdin/schema/private-file
and process-supervision path through builder and independent review interfaces.
Fake hosted queue and provider fixtures cover concurrent duplicate claims,
ambiguous handoff/review/cancel, disconnect and restart, expiry/revocation,
changed scope/account/lease, budget exhaustion, current-head checks and separate
completion/exit/review/gate/merge observations. A schema-equivalence test keeps
`remote_session.v1` metadata unchanged.

These fixtures are offline integration evidence, not live hosted completion or
cancellation evidence. Those verifications belong to #919/#920 and final #923,
with the explicit campaign cap and private authorization in place. Only closed
metadata, test outcomes, PR and head provenance should enter public evidence.


## Checkpointed clarification and bounded fixes (v2, #1017)

`code_mower.supervisor.v2` is an **opt-in, separately packaged contract**. Load
`supervisor_contract_v2.schema.json` and `supervisor_contract_v2.fixtures.json`
with `importlib.resources`, and use `supervisor_contract_v2.validate`/`decode`
and `public_status`. Every v1 record, fixture, validator and consumer remains
supported. The v1 operation enum is still exactly handoff/renew/status/result/
cancel; a v1 claim cannot call v2 operations or migrate into a new writer.
V2 uses the existing decision-only `supervisor_decision.v1` runtime protocol:
Codex still decides admission, handoff and independent review/completion, and
never sends messages or grants recovery itself.

V2 admission adds integer `limits.clarification_answers` (0–32) and
`limits.fix_requests` (0–8). `limits.review_requests` (1–9) covers the first
independent review plus any reviews after fixes. These are pre-authorized
**total campaign allowances**, frozen with the original scope, builder cap and
selected reviewer. Set either mutation allowance to zero to prohibit it.
Renewal, answers and fixes cannot enlarge any allowance. A fix also requires
room for its subsequent independent review. Runtime calls retain their original
separate budget. Every attempted mutation with a persisted intent consumes its
allowance, even if its response is lost.

| V2 operation | Wire metadata | Additional private authorization and observations |
| --- | --- | --- |
| `clarify` | v2 request schema, action, exact v2 claim, `request_key` | An observed `waiting_for_user` checkpoint, answer resolved from private input, unchanged original scope and remaining answer allowance. |
| `fix` | v2 request schema, action, exact v2 claim, `request_key` | Original fix/review allowances, exact open PR/branch/reviewed head, bounded private finding reference, failed independent review and terminated builder and review runtimes. |

`request_key` is 1–64 ASCII letters, digits, underscores or hyphens. It is a
stable idempotency key scoped to the claim, **not authorization**. The five
existing operations have v2 schema/claim equivalents without a request key.
Unknown fields (including prose, approval flags, limits, paths, finding bodies
or provider IDs) are rejected. No public or Slack message body becomes authority.

For either new operation, the embedding implements
`Authorization.resolve(admission, action, *, request_key=...)`. Every invocation
independently authenticates the caller and re-reads current private task/grant
state. The returned `AuthorizedTask.checkpoint_input` is an `AuthorizedInput`
from `code_mower.supervisor_checkpoint`: the exact validated request, private
prose, observed private checkpoint, original `scope_digest`, and—for a fix—the
exact `builder_lineage.Target` and a bounded `finding_ref`. The private store
must authorize the answer or finding scope against that actor, grant revision,
work and review. Never construct this object by copying untrusted ingress text.
The supervisor checks the request and input binding again before and after
external calls. Resolver failures, changed inputs, lease replacement, revoked
grants, changed provider accounts and runtime restarts fail closed.

```python
from code_mower import supervisor_contract_v2

# The authenticated bridge validates metadata; the resolver supplies private input.
request = supervisor_contract_v2.decode("request", wire_bytes)
receipt = supervisor.operate(request["action"], request["claim"],
                             request_key=request["request_key"])
public = supervisor_contract_v2.public_status(receipt)
```

A private v2 result includes a `checkpoint` digest for correlation with the
input store. It binds the original provider session/account, work-order round,
observed lifecycle state and durable remote mutation history. It is not a
provider question ID or a capability, and never enters public status. The
provider must still report the appropriate checkpoint immediately before the
message. Approval remains a distinct `waiting_for_approval` state with
`approval_required` / `owner_action`; **neither operation approves a provider
request or disables safe mode**. Only the existing message lifecycle is used.

The supervisor compares exact work/run/session/claim, grant revision, runtime
generation, provider binding, lease acquisition and original scope. The maintained
`HostedBuilder` wraps the already configured `DevinWorkOrders` / `RemoteSessions`
connections with reauthorization checks around external calls. It checks fresh
provider usage below the original cumulative ACU cap before reserving a mutation
and immediately before sending. Missing or invalid usage fails closed. The
provider's original hard cap remains unchanged. A fix rechecks the reviewed head,
writer termination and contributor/reviewer independence before sending, clears
old completion/review evidence, and uses a fresh round-specific audit key after
new exact-head completion. There is no replacement session, fallback provider,
new reviewer, review/merge grant or extra recovery allowance.

Before entering the maintained message/fix lifecycle, `ContextStore` writes and
fsyncs a pending intent (file and directory) under the original work reservation.
It stores only private bindings/input digests and closed outcomes, never input
prose. Concurrent duplicates serialize on that reservation. A completed duplicate
returns its saved outcome after fresh authorization; it does not poll or resend.
This is a **receipt**, not current completion evidence; use `result` to collect
current exact-head completion. A renewed live claim can retrieve the same saved
receipt, which may carry the earlier claim expiry; use the renewed claim for
further work. All other input changes under the same key are conflicts.
Crashes before/after a send, timeouts, ambiguous delivery and failed outcome
writes leave `mutation_uncertain` / `owner_action`. New keys, renewal, handoff,
status and cancellation cannot blindly replay a pending mutation. There is no
wire acknowledgement/recovery operation: an operator must reconcile the private
provider and lifecycle records through the existing owner recovery boundary.
A restarted runtime cannot reuse the old claim. No automatic refund, takeover
or new admission is a recovery mechanism.

V2 public status adds only closed `waiting_for_user` / `waiting_for_approval`
states, reasons and `clarify` / `fix` actions. Claim/input/checkpoint digests,
provider identifiers, PR bindings, paths and prose stay in private records;
`remote_session.v1` lifecycle metadata is unchanged. The supervisor root and all
provider/work-order roots must stay private, stable and outside Git.

Run `python -m unittest discover -s tests -p 'test_supervisor*.py'` for fake-only
compatibility, resume, crash, ambiguity, revocation, checkpoint, allowance and
exact-head coverage. Fixtures explicitly mark `live_hosted_evidence: false`.
The private #920 bridge may develop against an exact independently reviewed
post-v1.4.2 source pin. **Live canaries and deployment require the final v1.5
package** containing the accepted contract; this change does not publish a
release, run a canary, install credentials or add Slack/hosted queue logic.
