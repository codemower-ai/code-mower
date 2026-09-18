# Optional Slack setup and operator runbook

Slack v1.5.0 is an explicit opt-in for **one private workspace**, an authorized
private, unshared channel, and a bound repository. Initial setup remains
Claude + Codex: no Slack prompt, dependency, login or service. Slack conveys requests;
the qualified supervisor owns execution. Hosted Devin is a bounded builder,
never an orchestrator qualification.

These commands are included in v1.5.0. Use its reviewed wheel for offline
preparation; live operation requires the final immutable v1.5.0 package and
separately qualified hosted deployment. The private bridge verifies its
implementation lock and rejects editable/VCS installs for live operation.
Version alone is insufficient. Live completion/cancellation qualification
belongs to #920 under #923; this guide authorizes neither spend nor deployment. Telemetry
readiness, Board/cloud links, a general integrations picker, Slack Connect,
public channels and rich Slack UX are deferred to v1.5.1.

## Fresh installation

1. Install Code Mower normally. Only if you want Slack, prepare the dedicated
   **hosted** manifest in an existing local directory. Choose either interactive
   or scripted opt-in:

   ```sh
   code-mower slack setup --manifest slack-app.json --interactive
   code-mower slack setup --manifest slack-app.json --yes
   ```

   The command exclusively creates a mode-0600 static manifest, refusing existing
   files and symlinks. It performs no network request, credential lookup, policy
   change or service installation. The old `templates/slack/app-manifest.json`
   belongs to the OSS `/code-mower` seam; the new `hosted-app-manifest.json` is
   for the `/codemower` operator path.

2. An authorized administrator imports the generated manifest into the private
   Slack app. Preserve exactly the bot `commands` scope, no user scopes or Events
   API subscriptions, no organization-wide install, and token rotation. The
   hosted routes are fixed in the manifest: command and interactivity requests go
   to the application, while the OAuth redirect goes only through the dedicated,
   owner-controlled query-scrubbing relay before a query-free browser handoff to
   the application. The fixed relay is the dedicated production `workers.dev`
   route. It deliberately avoids a customer-zone custom domain because Cloudflare
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

4. Enable the hosted control plane only for the private installation. A signed-in
   owner/admin uses **Setup → Manage Slack integration**, supplies the expected
   immutable workspace (and enterprise identity where applicable), and completes
   OAuth as the same administrator. Wrong app/workspace, revoked membership,
   expired/reused OAuth state or missing rotating credentials must deny. Inspect
   connection/rotation health privately. Recover uncertain rotation with a fresh
   OAuth attempt; never replay a refresh token.

5. Map the exact Slack user ID to an existing active member, never a display
   name/email. Select an authorized catalog repository and alias. Bind the exact
   private, unshared channel in the installed workspace and renew verification
   within one hour. Connect, public channels and DMs are unsupported for this
   release. A changed person/repository behind a mapping requires removal and a
   new binding; an existing grant cannot change meaning. Observer permits
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
   Qualify the two explicitly capped #920 canaries before treating the candidate as a
   supported live installation.

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
prompts, source/diffs or task/message prose. These commands emit no cloud
telemetry. Live deployment and capped canary evidence remain separate.
