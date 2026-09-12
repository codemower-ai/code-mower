# Slack ingress contract v1

Issue #916 in epic #903 defines an optional ingress and conversation transport.
Slack is not a builder, reviewer, participant, scheduler, or orchestrator. It has
no merge, approval, provider selection, or uncertain-delivery acknowledgement
authority. Initial setup remains Claude + Codex; optional discovery belongs to
#922. No worker, SDK, OAuth store, network client, or remote mutation is included.

`src/code_mower/slack_contract.schema.json` contains closed structural schemas;
`slack_contract.validate` adds required cross-field checks. Consumers must use
both structural and semantic validation, including `normalize` for authorization.
The root accepts request/inbox/outbox/retention/error/event records; policy,
identity, private intent, and remote lifecycle are named `$defs`. Unknown versions, fields,
operations, types, and enum values fail closed. Identifiers are bounded opaque
internal bindings, not Slack display names, repository paths, URLs, or provider
session identifiers. Fixtures in `tests/fixtures/slack_contracts.json` are invented.

## Boundary and identity

#917 must verify the signature over the original request bytes before decoding,
check the signed timestamp against a five-minute replay window, and use a
constant-time signature comparison. A JSON field claiming verification is never
accepted. `normalize(verified=True, ...)` is an internal assertion supplied only
by that verifier, not verification itself. Invalid authentication is rejected
without work. Raw bodies, signature headers, tokens, response URLs, and trigger
IDs must never enter these records or logs. Bound raw ingress before parsing;
`decode` limits normalized JSON to 64 KiB and rejects duplicate keys and excessive
nesting. Text is capped at 16,000 characters and 64 KiB UTF-8; it remains private.

Valid requests must receive HTTP 200 within **3 seconds**, independently of
runner execution. The acknowledgement means receipt only, not authorization or
successful dispatch. Perform bounded verification, validation, and a durable
inbox reservation before acknowledging accepted work; if reservation fails,
return an error and do not dispatch. Rejected authenticated requests get only a
private safe diagnostic. Never await provider work in the acknowledgement path.
Modal validation errors also fit that deadline. A modal's original conversation
and session must be resolved from server-held correlation; client modal metadata
cannot create a grant. Trigger expiry and modal opening remain boundary concerns.

Resolve installation + team to one active tenant. Resolve the Slack actor to an
explicit authorized human, then resolve a repository binding and conversation
(channel, thread, visibility), session, and registered runner within that tenant.
`policy` is one independently resolved, active grant for that exact identity
and a bounded operation allowlist. Never build it by copying request fields.
Revalidate membership, grant revocation, repository access, and conversation
visibility at consumption and delivery, not just receipt. `normalize` compares
all identity fields and requires the requested runner in a trusted registration
set; `slack` is always forbidden. Registration does not imply that a provider is
implemented. No ambient credentials or channel membership inferred from a name.

Bindings must enforce ownership for sessions and repositories and installation
ownership for actors/channels, including shared Slack Connect channels. A matching
channel ID alone is not sufficient. Cross-team, cross-install, cross-actor,
cross-repository, cross-session, or cross-conversation requests are rejected.
Enterprise installations must still resolve a concrete team-scoped binding;
unresolved organization-wide contexts are denied in v1. Use an opaque sentinel
binding for an authorized channel root when no thread exists, never a wildcard.

Least privilege: enable only command/interactivity and necessary reply transport
capabilities. Do not subscribe to workspace history or passively ingest messages.
An Events API delivery is not itself a command; only explicitly authorized reply
interactions may normalize to message/clarification. Ignore unsupported events.
Completion responses are **internal** orchestrator notifications, never an inbound
Slack command; resolve their original authorized destination before normalization.
They require the trusted caller argument `origin='orchestrator'`; the default
`origin='slack'` rejects completion. Neither origin nor verified is read from the
request. For completion, verified attests the original verified interaction and
the authenticated orchestrator notification, not a new Slack signature.
Slack OAuth scopes alone never grant repository or orchestration permission.
OAuth installation/storage and exact scopes for chosen endpoints are deferred.

## Remote-session mapping

| Transport operation | remote_session.v1 operation | Meaning |
| --- | --- | --- |
| start | dispatch | Explicit authorized request to existing orchestration |
| status | status | Observe the existing session |
| message | message | Explicit human follow-up |
| clarification_reply | message | Human reply to waiting-for-user context |
| cancel | cancel | Request cancellation; do not claim termination before observation |
| completion | collect | Existing orchestrator collects once; transport delivers metadata |

Commands support start/status/message/clarification_reply/cancel. Modal submissions
support start and clarification_reply with the same identity and policy checks.
The orchestrator issues a clarification grant only for a session currently waiting
for user input. It retains all lifecycle, cancellation, and execution policy;
the transport cannot treat message text as an approval or merge decision.
Completion uses `completion_response`, with empty text. Status/cancel text must
also be empty. Start and message operations require nonblank private text.
`normalize` returns a private, non-executable intent identifying the mapping,
session, registered runner, request key, fingerprint, and scope. Trusted consumers
resolve private text/repository data from the authorized request and bindings;
this helper neither constructs provider calls nor invokes `RemoteSessions.run`.

Lifecycle metadata is exactly `code_mower.remote_session.v1`; tests compare its
schema to the existing remote schema. States, reasons, and next actions pass
through unchanged. In particular, waiting_for_user/user_input_required invites
clarification; waiting_for_approval/approval_required points back to the existing
owner workflow and cannot be satisfied by Slack. Complete does not expose results.
Uncertain/reconcile_dispatch and inspect_provider_then_acknowledge retain the
existing recovery path. Slack never calls acknowledge_delivered or independently
retries dispatch/message/cancel. Future registered Codex/Claude remote runners use
the same mapping without Devin IDs, URLs, account fields, ACU limits, or SDK types.
The current remote-session implementation still has provider-specific internals;
this issue does not refactor them or implement new runners.

## Private inbox/outbox and retry rules

Reserve inbox records atomically by SHA-256(team, installation, delivery).
The normalized delivery binding must be stable across retries: use the Events API
event ID where applicable; slash commands and modals need a verifier-derived
stable request identity because they do not share the Events API envelope. Never
use retry count, arrival time, text alone, or a newly generated per-attempt ID.
The fingerprint includes all normalized content except retry count. The same key
and fingerprint is a duplicate in **every** inbox state: return the saved receipt,
never run again. Different content under a reserved key is request_conflict.
`duplicate` is only a pure comparison, not a store, lock, or expiry check.

Inbox transitions: accepted -> handed_off -> done; failed/ambiguous handoff ->
uncertain; policy rejection -> rejected. A consumer claims atomically and hands
one durable intent to the existing orchestrator. It must reconcile ambiguous
handoff through remote_session.v1, never invent transport-side recovery logic.
Retention expiry does not authorize replay: reject stale deliveries before
reservation and preserve orchestrator operation keys for session lifetime.

Outbox records contain only private destination bindings and lifecycle metadata.
Its key is an orchestrator-assigned stable delivery key for a particular session
notification (not a new key on retry); separate successive notifications get
separate keys. States are pending -> sent/uncertain/expired. Ambiguous sends become
uncertain and require transport reconciliation; they must not trigger remote work.
No result artifact or conversation prose belongs in the outbox v1 schema.
Storage, atomic transitions, transport retries, clocks, and reconciliation are
requirements for later implementation, not behavior implemented by these schemas.

Responses default to private (ephemeral to the invoking actor). Public scope
requires explicit request selection, grant.allow_public, and public conversation
visibility; private channels and DMs remain private scope in v1. Thread replies
retain the bound destination and scope. Never upgrade an existing private reply
to public, broadcast to another channel, or infer public permission from a response
URL. Public responses contain only closed metadata. In particular, do not use a
slash-command `in_channel` response that republishes the original command text;
a later transport must use a separately authorized metadata-only publication.
Recheck scope on completion as well as start. Expired private reply routes fail
closed; never fall back to a public channel. Slack's response URL is a bearer
capability that can bypass channel posting permissions and belongs only in a
separate ephemeral secret facility, never a contract record or event.

## Retention, diagnostics, and Board/cloud

Retention policy bounds private input to at most 24 hours (fixture: one hour),
inbox dedupe metadata to 2–7 days (fixture: two days), and outbox delivery records
to at most 24 hours (fixture: one hour). Shorten private retention on handoff and
delete on revocation; preserve only the content fingerprint/key needed to suppress
replay. Enforce expiry before reading or sending; sweep private content and expired
records, including backups under the same policy. These are Code Mower limits,
not Slack retention guarantees. Slack stores sent messages according to workspace
policy. Private fingerprints are not safe telemetry or anonymization.

The closed error record contains only a fixed code and private scope. Do not echo
input, adapter exceptions, paths, secrets, or authorization details. All future
boundary exceptions must be mapped to these codes before presentation.

Only `board_event` output is suitable for Board/cloud: schema, transport, kind,
operation, scope, state, reason, next_action. Every value is a closed enum/constant.
No identity or arbitrary string identifiers are emitted. Message/modal text, task
prose, prompts, source, diffs, credentials/tokens/signatures, raw Slack/provider
payloads, private context, repository paths, and result content are excluded.
Do not log private requests, intents, grants, inboxes, outboxes, or exception
representations. Unknown lifecycle fields are rejected rather than redacted.
Public event routing/authentication belongs to the existing Board/cloud envelope,
not to user-controlled contract fields.

## Official references

- [Handling user interaction](https://docs.slack.dev/interactivity/handling-user-interaction/):
  acknowledgement deadline, modal submissions, ephemeral/in-channel visibility,
  response URL capabilities and expiry, trigger lifetime.
- [Events API](https://docs.slack.dev/apis/events-api/): event identity, authorization
  context, acknowledgement and retries. Delayed events can retry for 24 hours;
  the two-day dedupe minimum exceeds that window. Unsupported/stale events must
  not become new work after dedupe expiry.
- [Implementing slash commands](https://docs.slack.dev/interactivity/implementing-slash-commands/):
  3000 ms receipt, signature verification, private default, public response behavior.
- [Verifying requests from Slack](https://docs.slack.dev/authentication/verifying-requests-from-slack/):
  raw-body verification, timestamp freshness, and constant-time comparison.

Run `python -m unittest discover -s tests -p test_slack_contract.py` with Python
3.12+. The module imports only the standard library and the schema is included by
the package's existing `*.json` package-data rule. No setup defaults change.

The authenticated boundary and minimal manifest are now implemented by #917;
see [Slack authenticated ingress](slack-ingress.md) for the bounded HTTP seam,
receipt-store interface, supported surface and remaining deployment obligations.
