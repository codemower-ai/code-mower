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
