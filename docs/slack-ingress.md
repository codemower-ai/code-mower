# Slack authenticated ingress

Issue #917 implements `code_mower.slack_ingress`, a stdlib-only request seam for
[slack_contract](slack-contract.md). There is no server, OAuth installation,
network client, worker dispatch, or deployment. Default installation is still
Claude + Codex. Slack has no participant, provider, review, or merge authority.

The mirrored `templates/slack/app-manifest.json` is an optional template. Replace
the reserved example endpoint privately before later live acceptance. It requests
only the bot `commands` scope, with one `/code-mower` command and interactivity.
It requests no user scopes/tokens, history, files, email, or posting permissions.
There are no Events API subscriptions: the contract requires explicit commands
and modal replies, not passive message events. JSON URL verification is supported;
all other event types (including message and completion events) fail closed.
Future subscriptions require a separately reviewed scope and event allowlist.

## HTTP adapter contract

Construct `Ingress` with private signing-secret bytes, `Clock`, `Bindings`, an
atomic durable `ReceiptStore`, and registered runner names. Call `handle` with
`POST`, the exact raw bytes, a tuple of header pairs, and an absolute monotonic
`deadline = arrival_monotonic + ACK_DEADLINE_MS / 1000`. Arrival starts before
reading the body. The adapter must bound the read to 65,536 bytes and enforce
read/write and dependency timeouts within that **three-second** budget. This
synchronous seam cannot interrupt a blocked dependency; it passes the deadline
to both dependencies, checks it before reservation and after commit, and rejects
late results with `delivery_uncertain`. A late/ambiguous commit may already be
durable; retry must resolve against the same reservation, never dispatch again.

Preserve case-insensitive header multiplicity before any coalescing. Pass only
Content-Type, X-Slack-Signature, X-Slack-Request-Timestamp and optional
X-Slack-Retry-Num/Reason; HTTP framing, routes, content encoding and other headers
belong to the adapter. Reject ambiguous framing/encoding there. The seam rejects
unknown or duplicate headers, at most 32 headers and 8 KiB total; each value is
bounded to 1,024 characters. It binds `v0:timestamp:raw_body` with HMAC-SHA256,
uses `hmac.compare_digest`, and accepts signed timestamps only within ±300
seconds. Authentication precedes body decoding, binding resolution and storage.

Forms reject malformed percent/UTF-8 encoding, duplicate fields and more than
32 fields. JSON rejects duplicate keys, nonfinite numbers, invalid UTF-8,
unknown fields, depth above 10, more than 32 members/items per container and
more than 256 value nodes. Private text is capped at 16,000 characters; the
contract also enforces its UTF-8 byte limit. Unknown command/interaction kinds,
enterprise-wide contexts, arbitrary modal metadata and richer Block Kit are denied.

`/code-mower start <text>`, `status`, `message <text>`,
`clarification_reply <text>`, and `cancel` use the merged operation vocabulary.
Status and cancel reject arguments; other operations require nonblank text.
All requests use private scope. Slash-command delivery derives from authenticated
app/team/trigger fields, stable across timestamp and retry-header changes.
Changed normalized content under that delivery conflicts. Modal submissions
support callback IDs `start` and `clarification_reply`, one input block `input`
with plain_text_input action `text`, and empty private_metadata/external_id.
Modal delivery derives from app/installed-team/view ID, bound to a server-held
one-time correlation. The optional `view.hash` is a mutable revision, not a
delivery identifier: hash changes alone remain duplicates, while changed
normalized request content conflicts under the same view ID. A modal opens only
in a later integration; this handler neither opens nor updates views.

Workspace installs within Enterprise Grid accept bounded enterprise ID/name
metadata and a false `is_enterprise_install` (omitted defaults to false). True
organization-wide installs and missing concrete action workspaces are rejected.
`Submission.enterprise` and `is_enterprise_install` are ephemeral binding inputs;
enterprise names are validated and discarded. No Slack installation metadata is
added to the provider-neutral contract or durable receipt.

For Slack Connect modals, `Submission.team` remains the action workspace and
`installed_team` prefers `view.app_installed_team_id`, falling back to the action
team only when absent. A present installed-team ID must be nonempty and bounded.
The optional `view_team` is also preserved for correlation and must match either
the action or installed workspace. The installation lookup must use app plus
installed team and independently resolve its enterprise membership; the payload
enterprise alone cannot establish that relationship across Slack Connect.

`Bindings.resolve` receives ephemeral routing IDs and must independently resolve
an active policy for the exact app, action team, installed team, enterprise/install
scope, actor and conversation. For modals,
resolve the view ID against server-held correlation, including the operation,
both teams, view team, original actor/conversation/session and waiting-for-user
grant. Deny unknown or cross-install mismatches, even when the other installation
is otherwise authorized. Preserve that same one-time context through the dedupe
retention window so exact retries reconcile without authorizing new work. Never infer a
grant from modal metadata, Slack membership, or a user-provided session. The seam
validates the returned policy and calls `slack_contract.normalize(verified=True)`.
Binding and storage dependencies must not log inputs, perform remote work, or
include private data in diagnostics. Authorization is rechecked by a future
consumer; receipt acknowledges neither authorization to execute nor execution.

## Durable receipt and response

`ReceiptStore.reserve(receipt, deadline=...)` must atomically persist the validated
private request and intent before returning `Reservation.ACCEPTED`. Key and
fingerprint come from contract normalization. `DUPLICATE` means an existing key
and matching fingerprint in **any** state; return the same acknowledgement,
without overwriting or extending retention. A differing fingerprint returns
`CONFLICT`. Exceptions, unknown results, and deadline overruns return 503 with
`delivery_uncertain`; no handler path dispatches work. No volatile implementation
is shipped. Deployment must supply and verify the durable store's atomicity,
crash recovery, encryption/access controls, expiry and sweep behavior.

Receipts contain internal bindings, private text and fingerprints, never original
bodies, Slack IDs, tokens, response URLs, or trigger IDs. Content expires after
one hour and dedupe metadata after two days; the store must enforce these limits,
shorten retention on handoff/revocation and preserve replay suppression when
content expires. URL verification also reserves a digest-only receipt before
returning its challenge; neither challenge nor token is persisted.

Command acknowledgement is exactly ephemeral `Received.`; modal success has an
empty body. Errors use the contract's closed private error schema and HTTP
400/401/403/409/503. The sole dynamic response is the authenticated, bounded URL
verification `challenge`, required by Slack's handshake. It is returned only to
the requester and must never be logged or published. Request/receipt/response
objects suppress dataclass representations. No payloads, IDs, credentials,
private receipt data, dependency exceptions, or hashes belong in logs, Board,
cloud, public metadata, or checked-in fixtures. Tests synthesize nonproduction
routing values and signing keys in memory; no captured Slack traffic is used.

## Official guidance and validation

- [Slack request signing](https://docs.slack.dev/authentication/verifying-requests-from-slack/)
  specifies the original bytes, v0 timestamp binding, freshness, and comparison.
- [App manifest reference](https://docs.slack.dev/reference/app-manifest/)
  defines command, interactivity, and OAuth scope configuration.
- [Slash commands](https://docs.slack.dev/interactivity/implementing-slash-commands/)
  documents ephemeral acknowledgements and the three-second response deadline.
- [URL verification](https://docs.slack.dev/reference/events/url_verification/)
  describes the challenge handshake.
- [View interaction payloads](https://docs.slack.dev/reference/interaction-payloads/view-interactions-payload/)
  documents workspace-in-Grid enterprise metadata and optional mutable hashes.
- [Bolt installation lookup](https://docs.slack.dev/tools/bolt-python/reference/request/internals.html)
  prefers `view.app_installed_team_id` for Slack Connect modal submissions.

Run `python -m unittest discover -s tests -p 'test_slack*.py'`. Offline tests cover
raw-byte signing, malformed/unknown/oversized inputs, replay and conflicts,
storage failure, deadlines, challenge handling and receipt-before-ack ordering.
Live manifest import, signed endpoint delivery, modal rendering/correlation,
durable-store crash/race tests and end-to-end acknowledgement latency remain
acceptance for a later adapter/deployment issue.
