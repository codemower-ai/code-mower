# Jira Cloud Setup

Code Mower uses GitHub Issues as its default, built-in work tracker. For teams
that manage tasks in Atlassian Jira Cloud, Code Mower provides an optional,
guarded integration for issue adoption, status verification, dry-run mutation
planning, and bidirectional pull request synchronization.

## Design and Safety Principles

1. **GitHub Remains Default:** A default `code-mower init` produces a pure
   GitHub setup. Jira Cloud is entirely opt-in.
2. **Double-Guarded Mutations:** Writing to Jira requires *both* an explicit
   config guard (`tracker.jira_cloud.mutations.writes_enabled: true`) and a
   runtime flag (`--apply`). Without both, all operations remain safe dry-run
   plans.
3. **Read-First Doctor:** `code-mower doctor --adoption` inspects metadata,
   verifies credentials, and validates permissions without creating or modifying
   any Jira issues.
4. **Closed Operation Allow-List:** Only four operations can ever be performed:
   `assign`, `transition`, `link`, and templated `comment`. Deletes, arbitrary
   field writes, and bulk operations are permanently rejected.
5. **Replay-Safe Idempotency:** Comments and links carry deterministic markers
   and global IDs so repeated applies never create duplicates.

---

## 1. Authentication and Credentials

Code Mower resolves Jira Cloud credentials from environment variables, secure
profile files, or the system keychain. It never prints credentials or tokens.

### Environment Variables

Set the following variables in your local environment or CI runner:

```bash
export JIRA_API_EMAIL="user@example.com"
export JIRA_API_TOKEN="your-atlassian-api-token"
# Optional: pre-seed cloud ID if already known
export JIRA_CLOUD_ID="11111111-2222-3333-4444-555555555555"
```

> **Note:** Generate an API token from your Atlassian Account Settings under
> **Security > Create and manage API tokens**. Never use your account password.

### Stored Profile File

You can store credentials in `~/.config/code-mower/profiles/jira.env`:

```bash
mkdir -p ~/.config/code-mower/profiles
cat << 'PROFILE_EOF' > ~/.config/code-mower/profiles/jira.env
JIRA_API_EMAIL=user@example.com
JIRA_API_TOKEN=your-atlassian-api-token
PROFILE_EOF
chmod 0600 ~/.config/code-mower/profiles/jira.env
```

> **Security Requirement:** Profile files must have restrictive file permissions
> (`0600` or `0400`). Code Mower refuses to read files accessible to group or
> other users.

---

## 2. Configuration (`code-mower.yml`)

Add the optional `tracker:` block to your repository's `code-mower.yml`. You can
generate this block automatically during initialization using `code-mower init --jira`.

```yaml
version: 1
repositories:
  - owner: "example-org"
    name: "example-repo"

tracker:
  kind: "jira_cloud"
  jira_cloud:
    site_url: "https://example.atlassian.net"
    cloud_id: "11111111-2222-3333-4444-555555555555"
    project_id: "10001"
    project_key: "ABC"
    issue_type_id: "10002"
    jql: "project = 10001 AND statusCategory != Done"

    status_category_map:
      new:
        - "1"
      in_progress:
        - "2"
      blocked:
        - "4"
      done:
        - "3"

    field_mappings:
      component: "components"

    mutations:
      writes_enabled: false
      allowed_operations:
        - "assign"
        - "transition"
        - "link"
        - "comment"
      transitions:
        in_progress: "31"
        done: "41"
```

### Configuration Fields

| Field | Type | Description |
|---|---|---|
| `site_url` | string | HTTPS URL of your Jira Cloud site (e.g. `https://example.atlassian.net`). |
| `cloud_id` | string | Immutable tenant UUID from Atlassian `/_edge/tenant_info`. |
| `project_id` | string | Immutable numeric project ID (preferred query primitive). |
| `project_key` | string | Display key of the project (e.g. `ABC`). |
| `issue_type_id` | string | Optional default numeric issue type ID for tasks. |
| `jql` | string | Bounded single-line JQL query for queue polling. |
| `status_category_map` | mapping | Maps normalized lifecycle categories (`new`, `in_progress`, `blocked`, `done`) to numeric Jira status IDs. |
| `field_mappings` | mapping | Optional map from safe target fields (`component`, `environment`, `fix_version`, `priority`) to Jira field IDs. |
| `mutations.writes_enabled`| boolean | Master repository guard. Defaults to `false`. |
| `mutations.allowed_operations`| list | Subsets allowed writes: `assign`, `transition`, `link`, `comment`. |
| `mutations.transitions` | mapping | Maps lifecycle categories to numeric Jira transition IDs. |

---

## 3. Verifying Readiness (`doctor --adoption`)

Run adoption diagnostics to verify configuration and read connectivity:

```bash
code-mower doctor --adoption --repo example-org/example-repo
```

The adoption check evaluates four Jira readiness checks:

1. **`tracker.jira.config`**: Validates URL format, project IDs, mapping shapes,
   and allowed operations.
2. **`tracker.jira.credentials`**: Verifies email, token resolution, and file
   permissions (`0600`) without exposing secrets.
3. **`tracker.jira.read`**: Connects to Jira Cloud, verifies cloud tenant
   matching, checks project access, fetches status categories, and validates
   `BROWSE_PROJECTS` permission.
4. **`tracker.jira.mutations`**: Checks write guards, verifies permissions for
   allowed operations (`EDIT_ISSUES`, `TRANSITION_ISSUES`, `ADD_COMMENTS`), and
   confirms transition targets match `status_category_map`.

---

## 4. Guarded Mutation Workflow

### Dry-Run Planning (Default)

Plan a mutation without making network writes:

```bash
code-mower jira-mutations plan --issue ABC-1 --claim --transition in_progress
```

This generates a deterministic plan JSON report showing:
- Operations to be executed
- Idempotency fingerprints
- Target status verification

### Applying Mutations

When ready, execute with both guards active:
1. Ensure `tracker.jira_cloud.mutations.writes_enabled: true` in `code-mower.yml`.
2. Supply `--apply` on the command line:

```bash
code-mower jira-mutations apply --issue ABC-1 --claim --transition in_progress --apply
```

If either guard is absent, Code Mower refuses to write and emits an informative report.

---

## 5. Rollback and Safe De-adoption

To revert Jira integration at any time:

1. **Disable Writes Immediately:** Set `tracker.jira_cloud.mutations.writes_enabled: false`
   in `code-mower.yml`. This halts all mutation commands while preserving read diagnostics.
2. **Return to GitHub Default:** Delete the `tracker:` block from `code-mower.yml`.
   Code Mower immediately defaults to GitHub Issues.

---

## 6. Troubleshooting

| Symptom | Cause | Remediation |
|---|---|---|
| `tracker.jira.credentials: fail` | Missing or insecure credential file | Verify `JIRA_API_EMAIL` and `JIRA_API_TOKEN` are exported, or check `chmod 0600 ~/.config/code-mower/profiles/jira.env`. |
| `tracker.jira.read: fail (forbidden)` | Account lacks project access | Grant the Atlassian user account `BROWSE_PROJECTS` on the target project. |
| `tracker.jira.mutations: fail (permission_denied)` | Missing edit or transition permissions | Grant `EDIT_ISSUES`, `TRANSITION_ISSUES`, and `ADD_COMMENTS` permissions in the project permission scheme. |
| `target_status_not_configured` | Configured transition lacks destination status mapping | Ensure `status_category_map` defines status IDs for the transition's lifecycle category. |
| `transition_target_mismatch` | Jira workflow leads to an unmapped status | Update `status_category_map` to include the target status ID of the workflow transition. |
| `rate_limited` | Jira gateway capacity reached | Doctor logs a warning and defers probes without writing. Wait briefly and retry. |
