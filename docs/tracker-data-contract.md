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
and apply support. `can_apply_mutations` is always `False` here: applying a
mutation needs an explicit runtime flag from a future guarded mutation
surface (issue #799).

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
