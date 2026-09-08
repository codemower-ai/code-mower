# Work Tracker Data Contract

Controller and lane status will read work items through a provider-neutral
contract instead of GitHub-shaped issue dictionaries, so a future tracker
(Jira Cloud, issue #797) does not require every caller to learn GitHub's
issue shape. `src/code_mower/tracker_contract.py` is the source of truth; it
has no network calls and no mutation/apply logic.

## Schema

`code_mower.trackerWorkItem.v1`:

- `source_kind`: `github` or `jira_cloud`.
- `identity`: stable identity fields only. GitHub uses `repo` + `number`.
  Jira Cloud uses `cloud_id`, `project_id`, `issue_id` (required) and an
  optional `issue_key` for display. A display name is never identity.
- `url`: the tracker's bounded canonical HTTPS browse URL.
- `lifecycle_category`: one portable category — `new`, `in_progress`,
  `blocked`, `done` — not a raw provider status name.
- `labels`: sorted label/tag strings.
- `assigned`: whether anyone is assigned, as a boolean.
- `created_at` / `updated_at`: bounded, timezone-aware ISO 8601 timestamps.
- `provider_metadata`: closed, bounded short fields (`status_name`,
  `issue_type`) only.

The contract must never contain an issue body, description, comment,
attachment, source, diff, transcript, raw output, auth output, local path, or
secret field. Identity and label values are bounded single-line strings.
`validate_tracker_work_item()` rejects unknown fields and a
denylist of known-unsafe field names with a distinct message each.

`TrackerCapabilities` models read support separately from mutation planning
and apply support. `can_apply_mutations` is `False` unless the caller passes
`apply_requested=True` *and* the config enables writes: configuration alone
never grants apply authority (see the guarded mutation surface below).

## Configuration

`tracker` is an optional, additive top-level config block. Configs that omit
it validate exactly as before and mean GitHub.

```yaml
tracker:
  kind: jira_cloud          # default: github
  jira_cloud:
    site_url: "https://example.atlassian.net"
    cloud_id: "11111111-2222-3333-4444-555555555555"
    project_id: "10001"
    project_key: "ABC"      # optional, operator display only
    issue_type_id: "10001"  # optional effective-permission probe target
    jql: "project = 10001 ORDER BY updated DESC"  # optional local queue query
    status_category_map:
      new: ["10000"]
      in_progress: ["10001"]
    field_mappings:
      lifecycle_category: "status"
    mutations:
      writes_enabled: false
      allowed_operations: []   # subset of assign, transition, comment, link
      transitions:             # lifecycle category -> Jira transition id
        in_progress: "31"
```

`field_mappings` targets are restricted to safe normalized fields
(`lifecycle_category`, `labels`, `assigned`) — mapping onto `description` is
rejected. `mutations.allowed_operations` excludes delete. This adds
validation only: no Jira network calls or writes happen from this config.
The JQL string remains local configuration and must not be persisted in Board
event history, provider prompts, or cloud uploads.

## Read queue

`tracker_queue.py` implements read policy through the injectable
`JiraQueueReader.search_page(jql, fields, max_results, next_page_token)`
keyword-only protocol. A doctor-validated reader supplies enhanced-search
responses (`issues`, `nextPageToken`, optional `isLast`); transport, credentials,
timeouts, retries, and concrete CLI binding belong to #800. Until that binding
lands, configured Jira surfaces explicitly report `jira_reader_unavailable`.
No Jira calls or writes occur without an injected reader.

The adapter wraps the configured predicate with immutable numeric project-id
scoping, replaces its unquoted ordering with `created ASC, key ASC`, verifies
each returned item's project id, deduplicates by issue id (newest update wins),
and sorts deterministically. Defaults are five pages of 50 items; hard limits
are ten pages of 100. Repeated/missing cursors and malformed responses fail
closed. A page-limit result is partial and cannot authorize a dispatch.
Search uses the [enhanced JQL response contract](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/).

Only contract fields are normalized. Labels are limited to 100 input entries
and 128 characters each; provider metadata values are limited to 64 characters.
Custom mappings accept `customfield_<id>` fields with bounded label scalars,
boolean assignment, or portable lifecycle/status-category values. Status-id
overrides take priority over portable Jira categories. Unknown categories
make the read unavailable rather than guessing eligibility. Summaries/titles
are deliberately omitted even from local rendering. Queries, cursors, raw
responses, and exception text never enter returned reports.

With Jira configured, controller reports, `lanes status` (local
`code-mower.yml`, or `--config`), and Board current state add a `tracker` view.
Each row carries a validated work item, freshness, lane, optional linked PR
number, live GitHub gate state, and next action. `queue_view` accepts explicit
local `(cloud_id, project_id, issue_id)` → PR-number references; discovery and
persistence of links belong to #802. It never infers links from issue prose.
Historical queue snapshots cannot supply current PR/gate state or dispatch
eligibility. Observation age, not the issue's last edit, determines freshness.
Unavailable Jira preserves all live GitHub PR/check/gate decisions. With no
tracker configured, existing GitHub report fields and decisions are unchanged.

Board history may retain these metadata-only rows, but history is never merged
into the live queue. Existing cloud-event allowlists omit the new tracker
payload entirely. This read adapter adds no dispatch, assignment, transition,
comment, link, or issue-update operation.

## Read-only Jira Cloud probe

`src/code_mower/jira_cloud.py` is the single bounded read-only transport
(issue #800). It performs no mutation call: every primitive is an HTTP GET,
except the JQL search and the bulk permission check, which use read-only
POST endpoints. Writes are unimplemented here.

- Scoped API tokens go through the gateway
  `https://api.atlassian.com/ex/jira/{cloud_id}`. The configured
  `site_url` (for example `https://example.atlassian.net`) is
  browse/display identity only, never the REST gateway.
- Credentials resolve fail-closed: `JIRA_API_EMAIL` + `JIRA_API_TOKEN`
  from the environment first, then an explicit credential file or profile
  (`--provider-credential-file`, `--provider-profile`,
  `--provider-config-dir`), then exactly one secure discovered
  `jira*.env` profile. Files broader than mode 0600 on POSIX are
  rejected, and diagnostics carry filenames only.
- A profile may name a macOS Keychain generic-password service through
  `JIRA_KEYCHAIN_SERVICE` instead of storing the token on disk. The token
  is read from Keychain stdout only and never appears in argv, logs,
  exceptions, JSON diagnostics, Board data, or cloud data. Where Keychain
  is unavailable, set `JIRA_API_TOKEN` directly.
- `code-mower doctor --adoption` reports stable checks
  `tracker.jira.config`, `tracker.jira.credentials`, and
  `tracker.jira.read`, distinguishing missing, malformed, ambiguous,
  insecure, expired/unauthorized, forbidden, wrong-cloud, wrong-project,
  and rate-limited posture with safe remediation. GitHub-only
  repositories get no new checks.
- Retries are bounded (exponential backoff plus jitter, capped
  `Retry-After`); only bounded metadata fields are requested and
  returned. Public examples use `example.atlassian.net` and synthetic
  ids such as cloud id `11111111-2222-3333-4444-555555555555`, project
  id `10001`, and issue keys like `ABC-1`.

## Guarded Jira mutation plan/apply

`src/code_mower/jira_mutations.py` is the only Jira write surface (issue
#799). `code-mower tracker mutate` plans by default and performs no Jira
call at all in that mode. A network mutation happens only when **both**
guards are present:

1. `tracker.jira_cloud.mutations.writes_enabled: true` in the repository
   config, and
2. an explicit `--apply` at runtime.

With either guard absent the command emits a plan or a refusal with
`write_request_count: 0` and issues zero Jira requests. `doctor`, tests, and
normal dry runs therefore cannot write.

```bash
code-mower tracker mutate --issue ABC-1 --claim --transition in_progress \
  --link-pr --comment pr_opened --pr-url https://github.com/owner/repo/pull/12
code-mower tracker mutate --issue ABC-1 --claim --apply    # both guards
```

Four operations exist, each allow-listed twice: by
`mutations.allowed_operations` in config, and by the closed transport
allow-list on `JiraMutationClient`.

- `assign` — claim for the authenticated account
  (`PUT /rest/api/3/issue/{key}/assignee`).
- `transition` — one *configured* transition id resolved from
  `mutations.transitions[<lifecycle category>]`
  (`POST /rest/api/3/issue/{key}/transitions`). The command line never
  supplies a raw transition id.
- `comment` — one bounded comment from a closed template table
  (`claimed`, `pr_opened`, `pr_merged`, `pr_blocked`), rendered inside the
  transport from a template id plus a validated GitHub pull request URL, so
  no free-form text can reach Jira
  (`POST /rest/api/3/issue/{key}/comment`, Atlassian Document Format).
- `link` — one pull request remote link
  (`POST /rest/api/3/issue/{key}/remotelink`).

Deliberately absent and rejected by the transport allow-list: every delete,
attachments, arbitrary field updates, `PUT /issue/{key}` issue edits, raw
issue-body replacement, project/workflow administration, and writing any
issue property other than this module's advisory ledger and its per-comment
claim keys. `DELETE` is refused for every path.

Apply re-reads live state immediately before writing: the issue's project,
status, and assignee (`fields=status,assignee,project,issuetype` only, so no
summary, description, comment, or attachment prose is fetched) plus the
available transitions. An issue outside the configured project, a
transition the live workflow no longer offers, or a lost permission blocks
with a closed reason instead of guessing.

A transition is blocked unless the live transition's destination status id
is one of the ids `status_category_map` configures for the requested
lifecycle category (`transition_target_mismatch`, or
`target_status_not_configured` when the category has no configured ids). A
configured transition id names a workflow edge, and a workflow can be
re-pointed under it; verifying the destination before the write keeps a
`--transition in_progress` from moving an issue somewhere that category
never meant.

Idempotency is reconciled from authoritative state where it exists:
assignment from the current assignee, transition from the current status
(against `status_category_map` and the transition's target status), and the
remote link from its deterministic `globalId`
(`code-mower:github:<owner>/<repo>/pull/<number>`), which makes Jira upsert
rather than duplicate.

Retries follow the same rule. Reads, the assignee PUT, the `globalId`
remote-link upsert, and the property PUTs are all safe to repeat and keep
the bounded budget (exponential backoff plus jitter, capped `Retry-After`).
The comment POST and the transition POST have no server-side idempotency
key, so they are attempted exactly once: an ambiguous timeout, 429, or 5xx
cannot be told apart from a write Jira already committed, and a transport
retry there would double-apply. 409 conflicts fail fast for the same reason,
and a failing operation aborts the rest of the run instead of continuing.

### Comment claims

A comment is the one effect with nothing in live Jira to reconcile against:
this surface never reads comment bodies back, so "is it already there?"
cannot be answered by looking. At-most-once therefore comes from Jira's own
create-or-update contract on issue properties. Each comment intent has its
own property key, `code-mower-comment-v1.<fingerprint>`, and `PUT` of an
issue property answers **201 when it created the key and 200 when it
replaced an existing value**. Only a 201 acquires the right to post. A 200,
or a value that was already there, means another apply owns that comment,
and this run never posts.

The claim is per intent and never evicts, which is what a bounded shared
ledger could not offer: two applies racing the same intent contend on one
key instead of both reading an absent ledger and both posting, and the 33rd
comment on an issue cannot push an older comment's protection out of a
32-entry store and let it repost.

The claim value is bounded metadata — schema, `comment`, the fingerprint,
the closed template id, a closed state, an opaque per-attempt owner token,
and a timestamp. The rendered text, the pull request URL, and any local
identity are all absent.

Sequence, and what each outcome means:

- claim absent, `PUT` answers 201 → this run owns the post, posts once, then
  rewrites the key to `posted`.
- claim present as `posted` → `already_commented`, no post.
- claim present as `claimed` or `unverified`, or unreadable → the comment may
  have committed, been lost in flight, or never left, and this tool cannot
  tell. It reports `comment_unverified` with report status `unverified` and a
  non-zero exit, and asks an owner to look once and add the note by hand only
  if it is missing. It is never reposted automatically.
- `PUT` answers 200 → `comment_claim_held`, also `unverified`: another apply
  owns this comment, including the duty to report whether it landed.
- the claim `PUT` itself fails ambiguously → the claim is read back once. A
  stored owner token matching this attempt means the lost response was this
  run's own 201, so it may post; any other owner means it may not; an absent
  or unreadable claim means nothing was claimed, the run fails with the
  transport reason, and a later run may claim it cleanly.
- the post fails, ambiguously or outright → the claim is finalized to
  `unverified` and the operation reports `comment_unverified` with the closed
  transport cause in `detail.post_error`. One owner reconciliation, never a
  repost.

Nothing records a comment intent before the comment step runs, so an
assignment, transition, or link that fails first cannot leave a claim behind
for a comment that was never attempted.

### Fingerprints and the advisory ledger

Fingerprints are computed over the **immutable numeric issue id**, not the
caller's spelling of the issue key. A key is mutable — a project move or
rename rewrites it, and an operator may type it in any case — so `abc-1`,
`ABC-1`, and the id all resolve to one replay identity. Planning performs no
Jira call and so may not have the id: a plan built from a key reports
`tracker.fingerprint_basis: issue_ref_provisional` and carries provisional
fingerprints, and apply recomputes them from the live id it read immediately
before writing, reporting `issue_id`.

The `code-mower-mutations-v1` property remains as a bounded advisory ledger
of the three effects that *are* reconcilable from live state (assign,
transition, link). It is capped at 32 entries and evicts oldest-first, which
costs nothing because every entry it can lose is re-derived from live Jira
on the next run. It never gates a write, and comment entries — including any
written by an earlier build — are dropped from it.

### Write accounting

`write_request_count` is counted at the transport attempt boundary, so it
reports **every write attempt one apply made**, including attempts that
timed out, were rejected, or were retried, and reports the delta for that
apply. An attempt Jira may have committed and then failed to acknowledge
changed Jira as much as one that answered 201, so counting only successful
calls would understate what a run may have done. Reads are never counted, and
a refusal still reports `0` against zero issued requests.

GitHub remains the sole pull request, check, review, and merge-gate
authority. This surface reads no gate state and changes none; a Jira
refusal, conflict, rate limit, timeout, or outage cannot weaken a gate
decision. Reports (`code_mower.jiraMutationPlan.v1`) and anything retained
with `--plan-out` are bounded metadata only: identity, closed reason codes,
fingerprints, and counts. No issue prose, Jira response payload, exception
text, credential, account email, or absolute path is printed or retained.

A live write remains owner-authorized and disposable: enable
`writes_enabled` on a scratch repository config pointed at a disposable Jira
issue, run the command once without `--apply` to review the plan, then once
with `--apply`, and re-run the same command to confirm the replay reports
`already_applied` with no new effect. Offline tests
(`tests/test_jira_mutations.py`) cover both guards, every operation, replay,
drift, conflict, retry, timeout, cancellation, and the no-delete allow-list.
They also interleave two applies of one comment intent against a single
stateful fake Jira that honours the 201/200 property contract and prove at
most one comment POST, drive more comments than the ledger bound to prove
replay protection does not evict, replay one issue by key and by id, and
account for write attempts across timeout, retry, and success. No live Jira
write occurs in CI.
