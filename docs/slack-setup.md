# Optional Slack setup

Slack in the v1.5.x line is an explicit opt-in for **one private workspace**,
an authorized private, unshared channel, and a bound repository. The normal
hosted setup is a short dashboard OAuth flow. It does not require a local manifest, Slack app
creation, or Slack credentials on the user's machine. The default Claude +
Codex setup has no Slack prompt, dependency, login, or service.

Slack conveys a bounded request to the qualified supervisor; it does not run an
agent or gain provider, review, approval, or merge authority. Hosted Devin is a
bounded builder, never an orchestrator qualification.

Live operation requires the immutable reviewed `code-mower==1.6.0` package
after publication, or the exact retained candidate during qualification, plus
a separately qualified hosted deployment. The private bridge verifies its implementation lock and
rejects editable/VCS installs for live operation. Version alone is insufficient.
Metadata-only lifecycle summaries are included in v1.6.0 but remain disabled
unless the hosted service advertises the exact accepted contract identity.
Board links in Slack, a general integrations picker, Slack Connect, public
channels, DMs, and rich Slack UX remain outside v1.6.0.

## Hosted setup for a workspace administrator

Use this path when your team uses the Code Mower hosted service. A Slack
workspace owner or administrator and a Code Mower team owner or administrator
must complete the setup. The hosted Code Mower app is already configured; do
not create or import a Slack app manifest.

1. Sign in to Code Mower, select the intended team, then open **Setup → Manage
   Slack integration**. Supply the exact Slack workspace ID. If the deployment
   supports an Enterprise Grid workspace, also supply the expected enterprise
   ID; v1.6.0 still rejects organization-wide installation.

2. Choose **Install**, review Slack's consent screen, and authorize the Code
   Mower app in that same workspace. The v1.6.0 hosted app requests only the bot
   `commands` scope. It does not request message or channel history, posting,
   files, email, user tokens, Events API subscriptions, Socket Mode, or an
   organization-wide grant. Token rotation is enabled.

3. Return to **Manage Slack integration** and verify that the installation is
   active and names the expected workspace. A successful OAuth redirect is not
   enough: a wrong workspace, expired or replayed state, revoked administrator,
   or failed token rotation must leave the integration unavailable. If rotation
   is uncertain, reconnect with a fresh OAuth attempt; never reuse a refresh
   token.

4. Map the exact Slack user ID to an existing active Code Mower member. Select
   one authorized catalog repository and give it a private command alias. Bind
   the exact private, unshared Slack channel and renew its verification within
   one hour. Display names and email addresses are not authorization. Public
   channels, DMs, Slack Connect channels, and cross-workspace use are denied.

5. Ask the hosted operator to confirm the qualified supervisor, builder
   transport, numeric spend/task caps, and the fresh readiness checks described
   below. Enabling the web integration alone starts no agent work. Do not run a
   paid task until those checks pass and the owner has approved the cap.

6. In the bound private channel, begin with `/codemower help`, then use the
   operations in [Start, status, answer and cancel](#start-status-answer-and-cancel).
   Replies are requester-private. Treat command and modal text as private task
   input: Slack delivers it to Code Mower, and an authorized task can pass the
   bounded work request to the configured supervisor and builder.

To disable the integration, stop new admission in Code Mower first, reconcile
or cancel active work and confirm provider exit, then disable/delete the hosted
installation and remove the app in Slack. Removing the Slack app does not cancel
provider work already started.

### What crosses the Slack boundary

- Slack sees the slash command or modal text submitted through Slack. It sends
  that text, authenticated workspace/channel/user routing fields, and short-lived
  response/interaction capabilities to the hosted Code Mower service.
- Code Mower verifies Slack's signature, resolves the exact installation,
  member, repository alias, and private channel against server-held policy, and
  stores a minimized bounded receipt. Raw Slack requests, OAuth queries,
  response URLs, trigger IDs, and private mappings must not enter logs, Board,
  cloud exports, diagnostics, or public evidence.
- After current policy, supervisor, and numeric caps pass, the supervisor and
  configured builder can receive the bounded task request and the repository
  context their existing authorization permits. Slack cannot approve provider
  permissions, change safe mode, or authorize a merge.
- The hosted service stores rotating Slack credentials and policy bindings in
  its protected stores. Ordinary users do not copy those secrets into the CLI,
  a repository, support ticket, or local manifest.

See [Privacy and threat model](privacy-threat-model.md#hosted-slack-boundary) for
the complete trust-boundary description.

## Operator or self-host setup

This section is for the party that owns the Slack app and hosted deployment. It
is not part of ordinary hosted adoption. The public package supplies a static
manifest generator and a redacted readiness protocol; it does not include the
private host, database, secrets, or deployment.

1. Install the reviewed Code Mower package. Prepare the dedicated **hosted**
   manifest in an existing local directory, choosing interactive or scripted
   opt-in:

   ```sh
   code-mower slack setup --manifest slack-app.json --interactive
   code-mower slack setup --manifest slack-app.json --yes
   ```

   The command creates only a mode-0600 static manifest, refusing existing files
   and symlinks. It performs no network request, credential lookup, OAuth,
   policy change, or service installation. The
   authored `src/code_mower/templates/slack/app-manifest.json` file belongs to
   the standalone OSS `/code-mower` request seam; `hosted-app-manifest.json` belongs to this
   operator-owned `/codemower` deployment path.

2. An authorized app administrator imports the generated manifest into the
   operator-owned Slack app. Preserve exactly the bot `commands` scope, no user
   scopes or Events API subscriptions, no organization-wide install, and token
   rotation. The hosted routes are fixed in the manifest: command and
   interactivity requests go to the application, while the OAuth redirect goes
   only through the dedicated, owner-controlled query-scrubbing relay before a
   query-free browser handoff to the application. The fixed relay is the
   dedicated production `workers.dev` route. It deliberately avoids a
   customer-zone custom domain because Cloudflare
   Security Analytics samples all traffic for such a zone and can retain OAuth
   query strings even when Worker logs and traces are disabled. Preview URLs stay
   disabled, and no custom domain or zone route may expose the Worker.
   Do not substitute previews, localhost or private URLs. Verify the installed
   settings match: a generated file does not prove installation. See Slack's official
   [manifest](https://docs.slack.dev/reference/app-manifest/),
   [OAuth](https://docs.slack.dev/authentication/installing-with-oauth/) and
   [token rotation](https://docs.slack.dev/authentication/using-token-rotation/)
   documentation.

3. The hosted operator verifies the accepted OAuth, interaction and supervisor
   bridge migrations and upgrade rehearsal. Configure app/client IDs, client
   secret, signing secret, fixed redirect and encryption key versions through
   the service's secret manager. Never put credentials in CLI arguments, shell
   history, source, tickets or diagnostic snapshots. OSS setup does not receive
   or store them. Verify suppression of Slack OAuth queries, headers, bodies,
   response URLs and routing identities at every platform, application, tracing,
   database and export layer. Leave Slack disabled if suppression is unverified.

4. Enable the hosted control plane only for a private installation. The
   workspace administrator then follows the hosted OAuth path above. Wrong
   app/workspace, revoked membership, expired/reused OAuth state, or missing
   rotating credentials must deny. Inspect connection/rotation health privately.

5. Verify that the hosted administrator mapped the exact Slack user ID to an
   existing active member, authorized catalog repository and private alias, and
   the exact private, unshared channel. A changed person/repository behind a
   mapping requires removal and a new binding; an existing grant cannot change
   meaning. Observer permits
   status; operator permits start, answer and owned cancellation. Team-wide
   cancellation requires separate explicit admin authority.

6. Connect the maintained **Codex supervisor v2** adapter with effective
   orchestrator qualification, current generation/session lease, scoped
   authorization, independent eligible review broker and fresh heartbeat. The
   currently maintained adapter qualifies Codex; registering Claude or Devin
   does not extend that qualification. Legacy registrations require explicit v2
   registration and new admission, never an imported claim. A saved registration
   is not proof of a reachable supervisor.

7. Configure hosted Devin through its existing private credential/repository
   authorization path; check transport readiness without creating paid work.
   Record owner-approved numeric **task and aggregate campaign ACU caps**, task
   count/expiry, runtime-call/time, review spend and review/answer/fix ceilings.
   The ingress open-task cap is not a monetary cap. At least one answer allowance
   is needed for this runbook; the bridge defaults to zero. Recovery creates
   remain zero. Reserve the full task allowance once and never refund it on
   cancellation, timeout or unsettled billing. Credits are not authorization.

8. Obtain fresh scoped readiness observations below. Enable hosted interaction
   and bridge flags only after their owner-controlled deployment/logging gates
   pass. Explicitly enable host composition too; web configuration starts no
   worker. Missing supervision leaves work waiting/denied and prevents dispatch.
   Qualify the release's two explicitly capped canaries before treating the
   candidate as a supported live installation.

## Readiness and redaction

```sh
code-mower slack doctor
code-mower doctor --slack --json
# Explicitly selected trusted private host adapter:
code-mower slack doctor --probe /absolute/operator/slack-readiness-probe --json
# Saved observations are offline diagnostics only:
code-mower slack doctor --snapshot observation.json --json
```

Default doctor contacts nothing and reports not ready. `--slack` runs only Slack
checks, excluding generic doctor's repository/path/provider output. Text/JSON
contain fixed component names, states and remediation. No exception, raw output,
timestamp, nonce, cap amount, identity, channel, mapping, URL, provider reference
or task prose is rendered or uploaded. Exit 0 means fresh live observations pass
all checks; exit 1 means a gap; exit 2 means invalid arguments.
`dispatch_authorized` is always false: the supervisor independently reauthorizes
execution. An offline snapshot never passes live readiness.

The probe is a **trusted private host adapter**, not a Slack API endpoint or a
script generated by setup. This package has no private host credentials,
database or runtime connection. The host operator supplies the adapter using
those existing authenticated interfaces. Without one this CLI cannot establish
live readiness; do not substitute a hand-written all-green snapshot. Supplying
the adapter is an explicit operator prerequisite. Do not select repository
scripts or untrusted downloaded executables as probes.

The executable receives one JSON line on stdin:
`{"schema":"code_mower.slack_probe.v1","nonce":"<fresh 64-character hex>"}`.
It has five seconds, no shell/arguments, and a combined 16 KiB stdout/stderr
budget. Timeout/overflow terminates its process group. It must perform read-only
checks without task/provider creation or model calls, returning one closed JSON
object. Stderr is discarded, never logged. The inherited host environment is
trusted and may supply its existing authentication; this is not a sandbox for
untrusted code.

The response has exactly these fields:

| Field | Required observation |
| --- | --- |
| `schema`, `nonce` | Same schema and current request nonce |
| `observed_at`, `expires_at` | Epoch seconds; observation begins after this request, expires within 120 seconds, and remains fresh at return |
| `components` | Exactly the twelve components below; closed states in `slack_readiness.COMPONENTS` |
| `supervisor_product`, `supervisor_contract` | Effective runtime product and `code_mower.supervisor.v2`; other versions project as `unsupported` |
| `caps` | Explicit effective numeric limits/reservations with exactly the keys below |

For each invocation the host must resolve one immutable installation/user/
channel/repository scope using its independently authenticated administrator and
policy resolver, revalidate it at return, then project only the facts. Never send
the admin health response directly: it contains private bindings. Do not log
the nonce or scope. The nonce rejects cached responses; it is not authentication
or proof against a malicious probe.

| Component | Ready state and host check |
| --- | --- |
| `ingress` | `enabled`: owner-authorized control-plane/interaction flags and verified private logging gate |
| `bridge` | `enabled`: hosted flag and host composition enabled with the qualified immutable package |
| `manifest` | `matched`: installed routes, exact scopes, rotation and workspace settings match |
| `installation` | `active`: current installation, unexpired credential generation, no rotation/uninstall uncertainty |
| `oauth` | `configured`: exact app/workspace/redirect and rotating bot credentials agree |
| `identity` | `bound`: immutable mapping to a currently authorized member |
| `repository` | `authorized`: current repository/catalog grant for that member |
| `channel` | `private_verified`: exact private, unshared channel with verification under one hour |
| `registration` | `configured`: enabled, scoped v2 orchestrator registration |
| `supervisor` | `qualified_reachable`: actual connected runtime, effective qualification, matching generation/lease/claim and heartbeat under two minutes; never registration alone |
| `transport` | `ready`: implemented hosted Devin transport, scoped credentials and read-only reachability, no unresolved mutation |
| `campaign` | `active`: unexpired owner authorization, immutable revision, matching allowances |

Caps: `task_acu` (1–100), `campaign_acu`, `reserved_acu`, `task_limit` (1–50),
`reserved_tasks`, `runtime_calls` (2–32), `runtime_seconds` (1–300),
`review_rounds` (1–9), `review_budget_usd` (positive), `clarification_answers`
(1–32 for this path), `fix_requests` (0–8), and `recovery_creates` (zero).
All except review spend are integers. Remaining campaign allowance must cover
the entire next task, with task-count capacity remaining. Missing/extra fields,
nonfinite values and wrong types reject. The host enforces ceilings at admission
and around mutations. Doctor reserves no budget and changes no allowance.

## Start, status, answer and cancel

Use the bound private, unshared channel and private configured alias. Replies
stay requester-private; keep task prose out of diagnostics.

| Command/action | Expectation |
| --- | --- |
| `/codemower help` | Private help; no work created |
| `/codemower start <alias>` | Opens the bounded task modal. Submit once. Receipt/queued is not execution or completion. |
| `/codemower status <alias>` | Observes existing work and refreshes its private reply route without creating work. |
| `/codemower answer <alias>` | Opens an answer form for the current waiting-for-user checkpoint. Cannot approve provider permissions or merge. |
| `/codemower cancel <alias>` | Request, then use the private confirmation button. Observe status until builder and reviewer exit are confirmed. |

Completion requires verified implementation, exact PR/head, writer exit,
independent eligible review and authoritative gate evidence. It does not mean
merged or billing settled. Cancel acknowledgement does not prove exit. Unknown
exit keeps the original task occupied: never replace it or reset a claim.
After timeout/ambiguous mutation reconcile the original receipt/lifecycle through
the authorized operator. Provider approvals use the existing authorized provider
interface; Slack answers never change safe mode. Expired private delivery must
not fall back to a channel post.

## Troubleshooting, upgrade and removal

| Diagnostic | Bounded remediation |
| --- | --- |
| Disabled/missing installation | Verify owner enablement and complete fresh administrator OAuth. |
| Revoked/expired/uncertain credentials | Block execution; inspect the original rotation and reconnect if needed. Never replay refresh. |
| Identity/repository/channel mismatch | Recheck the immutable tuple privately; remove/recreate the wrong binding, never broaden grants. |
| Stale channel policy | Reverify private/unshared state and renew within one hour. |
| Registered but supervisor unavailable | Restore qualified v2 connection/heartbeat and reconcile claims. Devin cannot become supervisor. |
| Transport unreachable/uncertain | Inspect the original lifecycle privately; no automatic retry/create/fallback. |
| Missing/exhausted/mismatched caps | Stop admission; get a new owner decision before any increase. Retain old reservations. |
| Probe missing/stale/malformed | Repair the trusted host adapter and obtain fresh observations. Private errors are intentionally hidden. |

**Upgrade:** pause admission; inventory live/uncertain work privately. Reconcile
or cancel and confirm exit before changing runtimes. Install the reviewed
immutable release, apply hosted migrations through their owned rollout, verify
the implementation lock and re-register v2. Preserve bindings and receipt/
reservation evidence. Compare the installed manifest/scopes, renew channel
policy, and repeat fresh readiness and approved canaries. Upgrade never enables
Slack automatically.

**Disable:** stop admission with the hosted interaction/bridge flags and host
enablement control. Ingress disable is not provider cancellation. Keep the
authorized lifecycle recovery path available to cancel/reconcile active work and
observe exit. Use private admin Disable to invalidate OAuth attempts/credentials;
verify local denial even when remote uninstall fails. Inspect removal in Slack
privately if needed; preserve original work evidence.

**Rollback:** keep Slack disabled and preserve receipts, claims and full budget
reservations. Restore only the previously reviewed compatible runtime/deployment
through its owner-controlled rollback. Do not downgrade live v2 claims, revert
schema destructively, clear uncertainty or refund allowances. Re-enable only
after fresh qualification and explicit owner decision.

**Uninstall:** after confirmed writer/reviewer exit and reconciliation, disable
and verify app removal, then use private admin Delete and its retention/backup
process. Preserve required campaign reservation tombstones and reconciliation
evidence under hosted policy; never delete records to reopen spend. Remove the
local generated manifest if desired. Reinstall requires fresh OAuth and policy.
There is no local Slack service/dependency to remove.

Retain only approved check names/states, pass/fail counts, public immutable
release/PR/head and review/gate outcomes as release evidence. Never upload
snapshots, host logs, credentials, identities, mappings, URLs, provider output,
prompts, source/diffs or task/message prose. The local setup and doctor commands
emit no cloud telemetry. Live deployment and capped canary evidence remain
separate.
