# Qualified supervisor adapter v1

Issue #977 adds `code_mower.supervisor`, the maintained OSS boundary between a
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
| `request` → `Supervisor.operate(action, claim)` | Closed `handoff`, `status`, `result`, `cancel` operations. The authenticated bridge validates the request before calling the corresponding method. |
| `result` | Private claim and exact verified PR/branch/head target, plus closed status. No task prose or provider result is returned. |
| `public_status(result)` | The only Board/cloud/public projection. Closed state/reason/action, implementation, writer, review, gate, merge and unchanged `remote_session.v1` lifecycle metadata. No arbitrary strings, identities, paths, hashes, provider references or private bindings. |

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

Admission/handoff require start authority. Status requires status authority;
cancel requires an independently authenticated cancellation request. Result is
the authenticated orchestrator collection route, never an inbound Slack
completion command. Messages/clarifications and fixes require existing session
facilities and a separate authorized decision; they are not implicit new writer
or recovery allowances in this v1 boundary.

## Maintained local runtime connection

Construct `supervisor_codex.CodexRuntime` with an explicitly selected absolute
Codex executable, model and private runtime directory. The caller retains the
already configured runtime authentication; the adapter neither discovers nor
reads credentials. `CodexRuntime` runs fixed `codex exec` invocations with
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
                       model=selected_model)
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

A claim lasts at most 15 minutes and no later than its admission expiry. Every
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

A durable pending marker precedes every runtime/builder/review mutation. A lost
response, crash, disconnect or ambiguous handoff remains pending/uncertain and
cannot cause a repeat create, review request or cancellation. Status never
acknowledges uncertain delivery. A restart requires owner reconciliation through
the existing lifecycle and `lane_handoff` exact-head/quiescence facilities;
unsafe or unknown writer exit never starts a replacement. This adapter provides
no automatic recovery, fix-round, force-takeover or claim-reset operation.

An authenticated cancellation before handoff prevents builder creation. After
handoff, cancellation uses the exact existing remote operation and is reserved
once. Acceptance is `cancel_pending` until a fresh writer observation proves
termination. Ambiguous cancellation stays uncertain even if the transport says
it accepted the request. Cancellation does not need another model call, so it
remains available when the runtime invocation budget is exhausted; revoked or
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
terminated writer, eligible independent passing review and passing gate. The
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
