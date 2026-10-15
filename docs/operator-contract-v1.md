# Operator contract v1

This is the single authoritative product, authority, state, recovery, privacy,
and failure contract for the Code Mower v1.7 single-tenant Operator. It freezes
the boundary that later storage, service, GitHub App, Board, and provider work
must implement. It does not provide an Operator loop, dispatch a provider, or
grant a process permission to mutate anything.

The machine-readable contract consists of
`operator_policy_v1.schema.json`, `operator_state_v1.schema.json`, and
`operator_contract_v1.fixtures.json` in the `code_mower` package. Every object
is closed: an unknown field is invalid. A consumer must select behavior from
the record's versioned `schema` value and fail closed on an unknown version.
`operator_contract_v1.py` supplies the normative semantic checks for time,
generation, and recovery transitions that portable JSON Schema cannot express.
Consumers validate the closed record first and then apply those checks; schema
validation alone does not grant authority.

The record versions are `code_mower.operatorPolicy.v1`,
`code_mower.operatorQualification.v1`, `code_mower.operatorWorkItem.v1`,
`code_mower.operatorLease.v1`, `code_mower.operatorActionIntent.v1`, and
`code_mower.operatorProjection.v1`.

## Product boundary and authority

The Operator serves one tenant and only repositories in its explicit
allowlist. Repository admission, credentials, policy, and budgets come from
owner-controlled configuration. An observed issue, label, comment, provider
message, Board row, or model output is untrusted input and cannot widen that
configuration.

The Operator may observe bounded repository, issue, pull request, check,
provider-session, and Operator-record metadata. It may propose work selection,
provider assignment, review, and owner escalation. After policy, capability,
lease, head, intent, and budget checks, a later runtime may perform only the
mutation names present in `authority.mutations`. Absence from that array is a
denial.

The v1 pilot always requires a human to approve and perform a merge. The
Operator never merges a pull request, approves its own work, weakens branch
protection, expands its repository allowlist, exposes private content, or
mints or rotates credentials. A provider record for `merge_authority` remains
denied even if the same provider qualifies for other roles.

The contract authorizes no current implementation. Active dispatch, GitHub App
mutation, remote Board ingress, and cloud-schema changes remain separate work.

## Work lifecycle

One durable `code_mower.operatorWorkItem.v1` record identifies a tenant,
repository, work item, and monotonically increasing generation. Its state is:

`observed -> admitted -> claimed -> executing -> waiting_provider ->
reconciling -> awaiting_review -> completed`

The canonical path is illustrative; only edges listed by the schema are legal.
`awaiting_owner` is a nonterminal stop. Owner action can return it to
`admitted` or `executing`, or cancel it. `completed`, `failed`, and `cancelled`
are terminal and cannot re-enter active work. Each write atomically records the
new state, allowed transition, reason, generation, and timestamps. A
generation mismatch rejects the write rather than overwriting newer state.
Active states other than `awaiting_owner` require reason `none`; every
`awaiting_owner` record uses one of the policy stop reasons; and `completed`,
`failed`, and `cancelled` require `work_completed`, `work_failed`, and
`owner_cancelled`, respectively. These combinations are closed in the schema.
The work record also carries cumulative `elapsed_seconds` and `spend_usd`;
neither may move backwards across a transition or exceed owner policy.

The Operator stops in `awaiting_owner` for required approval, exhausted budget,
missing or stale qualification, unavailable credentials, policy denial,
inconclusive reconciliation, a repository outside the allowlist, a stale
head, a stale lease, or required user input. Escalations use a durable
`owner_escalation_key` and the configured count limit. Redelivery and restart
reuse that key, so the same stop does not create another owner notification.
Exhausting that limit leaves the item stopped; it does not create a new
escalation channel or enlarge a budget. Policy and work records use the same
stop-reason vocabulary, including `stale_head` and `stale_lease`.

## Singleton lease and fencing

A deployment has one lease for each tenant and repository scope. Acquisition
and renewal are compare-and-swap writes in the durable store. Every successful
acquisition or takeover increments `epoch` and produces a new `fence_token`.
Renewal retains both. A holder stops starting work before `renew_by` if renewal
does not succeed, and it has no authority after `expires_at`.

Every mutation intent binds the work generation, lease identity, dispatch lease
epoch, and dispatch fencing token. The durable store and each mutation adapter compare
the generation, tenant and repository scope, original epoch and token, exact
head, and a live independently read lease immediately before dispatch, retry,
and result commit. Stored `fence_status` and `head_status` labels never prove
authority. A stale holder cannot dispatch, retry, or overwrite the new holder's
state. A prepared, never-dispatched intent whose generation, fence, or target
head is stale is abandoned. An already-dispatched unknown intent retains its
immutable original dispatch fence and stays unknown. The new holder records a
separate current `reconciliation_authority` and reconciles it without
redispatch. The semantic check compares that authority with the current durable
lease rather than trusting the record's `current` label. Takeover first
increments the epoch, then recovers all nonterminal intents; it never creates a
replacement intent merely because the previous process disappeared.

Lease time is an availability mechanism. Fencing is the correctness mechanism.
Clock skew, delayed workers, and a process resuming after expiry must therefore
fail the fencing comparison even when they locally believe the lease is valid.
Lease chronology is `acquired_at <= renew_by < expires_at`; active authority
ends at `renew_by` unless a compare-and-swap renewal advances both deadlines.
`acquired_at` is the durable anchor for the current authority interval. A
successful acquisition, takeover, or renewal atomically sets that anchor and
the two deadlines. The policy binding check requires `renew_by` to equal the
anchor plus `lease_renewal_seconds` and `expires_at` to equal the anchor plus
`lease_ttl_seconds`; shifting both deadlines cannot extend authority.

## Durable intent, certainty, and reconciliation

Before any remote mutation, the Operator atomically persists a
`code_mower.operatorActionIntent.v1` record. Its `action_id`, operation,
request digest, idempotency key, work generation, exact target head, dispatch
lease epoch, and dispatch fence token are immutable dispatch inputs. Budget
counters are durable and cumulative across the intent's transitions.
Re-delivery of the same idempotency key and request digest returns the recorded
outcome. Reuse of the key with a different digest is a policy error. A
generation mismatch rejects dispatch, retry, and result commit rather than
letting an intent from an earlier admission act on current work.

An intent starts `prepared` with `not_attempted` certainty. The runtime may
dispatch it only while the lease fence and target head are current and a
mutation slot and all budgets remain available. A provider or transport
timeout, disconnect, malformed response after dispatch, crash before response
persistence, or partial response produces `unknown` certainty. Unknown is not
failure. Its only next action is reconciliation or bounded owner action.

Reconciliation queries remote state using the idempotency key, stable remote
reference when known, target head, and operation-specific metadata. Confirmed
remote success records `confirmed_success` without another mutation.
Confirmed absence or failure records `confirmed_failure`; a retry can then be
considered under the same durable intent and cumulative budget. Under the
original holder, retry requires the original dispatch fence to remain current.
After takeover, the original fence remains stale and immutable, while retry
requires the separately recorded current `reconciliation_authority` to match
the independently read live lease. Inconclusive reconciliation remains unknown
and eventually stops for owner action. A new intent cannot be used to bypass
uncertainty.

Restart loads nonterminal intents before admitting new work. Duplicate
delivery, restart, and lease takeover therefore converge on the saved intent.
A stale target head or fence abandons a prepared intent and requires fresh
observation. When dispatch already happened and certainty is unknown, the
current holder instead reconciles the original immutable target and fence;
neither path silently retargets or repeats a mutation.

## Budgets and failure semantics

Policy places ceilings on concurrent work and mutations, attempts,
reconciliations, owner escalations, work and action time, spend, lease TTL, and
renewal cadence. Counters are cumulative across retries, restarts, and lease
takeovers. Reservation and increment happen atomically before dispatch. A
failure to reserve a unit is a stop, not permission to run and account later.
Only an owner can install a new policy with larger limits.

The normative `policy_binding_errors` API binds every state record to the
owner policy. It checks the tenant, repository allowlist, authorized mutation,
action attempts/reconciliations/time/spend, work time/spend/escalations, and
lease cadence. Concurrent reservation still belongs to the durable-store
transaction, while each persisted record must independently remain within
these ceilings.

Failures have these required outcomes:

| Event | Required source and edge | Durable outcome | Forbidden shortcut |
| --- | --- | --- | --- |
| Process restart after dispatch | Dispatched or uncertain unknown intent -> reconciling | Load the intent and reconcile | Blind retry |
| Duplicate delivery | Any saved intent -> the identical record | Return the idempotent recorded result | Second mutation |
| Lease takeover | Dispatched or uncertain unknown intent -> reconciling under a new lease | Increment epoch and record separate current reconciliation authority | Accept the old token or blindly retry |
| Stale head | Prepared, not-attempted intent -> abandoned | Abandon before dispatch; reconcile an already-dispatched unknown effect through the takeover path | Mutate or silently retarget the stale head |
| Provider timeout | Prepared, not-attempted intent -> uncertain unknown | Record unknown and reconcile | Erase a known result or assume failure |
| Partial success | Dispatched or uncertain unknown intent -> reconciling | Reconcile each remote effect | Repeat the mutation set |
| Budget exhaustion | Active nonterminal work -> `awaiting_owner`, with `last_transition` naming that exact edge | Stop and escalate within the remaining limit | Implicitly extend budget or invent a source edge |
| Owner stop | Nonterminal work -> `cancelled`; prepared action -> `abandoned` | Cancel or abandon without new mutation | Continue dispatch |

`failed` means a confirmed terminal work failure. Transport errors do not make
a mutation a confirmed failure. Cancellation is complete only when no new
mutation can start and any already-dispatched uncertain mutation has been
reconciled or explicitly left for owner action.

## Provider capability and role qualification

Qualification is per provider, transport, role, and exact source head. The
provider identifier is opaque data, never a switch statement. Eligibility
requires a capability declaration, deterministic harness result, exact-head
result, policy decision, and unexpired evidence. Missing, failed, stale, or
incomplete evidence is denied.

The schema binds each status to a matching decision, reason, and evidence
shape. The normative `qualification_semantic_errors` check additionally
requires `observed_at < expires_at`, rejects future or expired observations,
applies the policy's maximum evidence age at an explicit durable-store `now`,
and verifies that every required capability was declared. Failed-evidence
records retain the evidence that failed, and `capability_missing` names an
actual declaration gap. Callers use that shared check instead of defining
their own clock or expiry ordering.

Codex and Claude are the initial qualified targets represented by the accepted
fixtures. Devin is represented as pending until equivalent evidence exists;
its name is neither a denial nor an exception. Any future provider can qualify
under the same record shape. A provider qualified as a builder does not thereby
qualify as orchestrator or reviewer, and an author cannot satisfy an
independent review requirement for its own head.

## Privacy and projection

Operator projection is metadata-only. The closed
`code_mower.operatorProjection.v1` record is the only v1 shape that may be
projected to local status, a future private Board ingress, or a future cloud
adapter. It contains opaque identifiers, role and state values, bounded counts,
elapsed time, and decimal spend metadata.

Projection combinations are event-specific and closed. `work_state` carries a
work identifier and a valid work state/reason pair; `lease_state` carries no
work, action, or provider identity and uses only lease state/reason pairs;
`action_state` carries work, action, provider, and role identities and a valid
action state/reason pair; `owner_action` carries only the affected work
identity and a stop or cancellation pair; and `qualification_state` carries
provider and role identity with a qualification state/reason pair. A state or
reason from another event family is invalid even when each value is separately
known to the contract.

Source, diffs, prompts, transcripts, issue bodies, raw provider output,
credentials, private content, and personal paths are excluded. Hashing private
content does not make it allowed metadata unless this contract explicitly
names the digest. Raw durable intent and lease records stay in the private
Operator store; their presence in the package does not authorize upload.

This contract does not change an existing Board, telemetry, or cloud schema.
A future adapter must validate the closed projection, perform its own
authorization, and preserve the same denylist.

## Threat and authority notes

The main authority threats are a second process acting after takeover, an
untrusted work item widening scope, a provider being treated as qualified by
name, a timeout being retried after remote success, a stale head receiving a
mutation, budget counters resetting on restart, and content leaking through a
status surface. Lease epochs and fencing contain split brain; closed policy and
allowlist checks contain scope injection; evidence records contain provider
confusion; durable intents and reconciliation contain duplicate mutation;
head binding contains retargeting; durable counters contain retry amplification;
and closed projections contain content leakage.

Credentials remain outside these records and are supplied to the narrow
adapter that needs them. Schema-valid data is necessary but insufficient for
authority: a runtime must also authenticate the owner-controlled policy,
compare current durable state, enforce repository and role separation, and
receive an adapter-level authorization decision. Logs and errors follow the
same metadata-only boundary.

The canonical accepted and rejected fixtures are executable examples of these
rules. Each recovery fixture contains schema-valid before, required-after, and
named forbidden-after record sequences. Named rejected transition fixtures
also cover invalid sources and false source-edge claims;
`recovery_transition_errors` verifies
tenant, repository, Operator-lease identity, lease chronology, timestamps,
every cumulative integer counter, decimal spend, fencing, and certainty.
Dependent implementations must consume them without weakening a rejected case,
and must add implementation-specific failure injection without changing the
meaning of the v1 records.
