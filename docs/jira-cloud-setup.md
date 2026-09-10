# Jira Cloud Setup

Code Mower uses GitHub Issues as its default, built-in work tracker. For teams
that manage tasks in Atlassian Jira Cloud, Code Mower provides an optional,
guarded integration for issue adoption, status verification, dry-run mutation
planning, Jira queue intake, and guarded GitHub pull request synchronization.

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
4. **Closed Operation Allow-List:** Only four user-visible operations can ever be performed:
   `assign`, `transition`, `link`, and templated `comment`. Deletes, arbitrary
   field writes, and general bulk operations are permanently rejected. PR sync
   uses one internal transactional property update, filtered to the already
   verified issue and only when its Code Mower association property is absent.
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
export JIRA_API_TOKEN="<token>"
```

Tenant and project identity belong in `code-mower.yml`; credential environment
variables do not override that reviewed repository configuration.

> **Note:** Generate an API token from your Atlassian Account Settings under
> **Security > Create and manage API tokens**. Never use your account password.

### Stored Profile File

You can store credentials in `~/.config/code-mower/jira.env`:

```bash
mkdir -p ~/.config/code-mower
cat << 'PROFILE_EOF' > ~/.config/code-mower/jira.env
JIRA_API_EMAIL=user@example.com
JIRA_API_TOKEN=<token>
PROFILE_EOF
chmod 0600 ~/.config/code-mower/jira.env
```

> **Security Requirement:** Profile files must have restrictive file permissions
> (`0600` or `0400`). Code Mower refuses to read files accessible to group or
> other users.

If more than one Jira profile exists, Code Mower never guesses. Pass the safe
filename selector explicitly, for example `--provider-profile jira.env`; doctor
reports candidate filenames only, never credential values or absolute paths.

---

## 2. Configuration (`code-mower.yml`)

Add the optional `tracker:` block to your repository's `code-mower.yml`. You can
generate this block automatically during initialization using `code-mower init --jira`.

For an existing Code Mower repository, add the tracker block in a reviewed PR.
First inspect generated-template drift without changing the checkout:

```bash
code-mower migration setup-drift --repo-path .
```

Keep repository-specific workflow customizations, copy only the Jira config and
documentation changes you intend to adopt, and review the resulting diff. Do
not replace an existing generated workflow tree with `init --apply` wholesale.

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
      lifecycle_category: "status"
      labels: "labels"
      assigned: "assignee"

    sync:
      # Empty means every PR-triggered Jira write fails closed.
      trusted_pr_authors:
        - "trusted-builder"

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
| `field_mappings` | mapping | Optional map from normalized metadata fields (`lifecycle_category`, `labels`, `assigned`) to Jira field IDs. Issue prose is never mapped. |
| `sync.trusted_pr_authors` | list | GitHub logins allowed to drive Jira state from bounded PR metadata. An empty list fails closed. |
| `mutations.writes_enabled`| boolean | Master repository guard. Defaults to `false`. |
| `mutations.allowed_operations`| list | Subsets allowed writes: `assign`, `transition`, `link`, `comment`. |
| `mutations.transitions` | mapping | Maps lifecycle categories to numeric Jira transition IDs. |

---

## 3. Verifying Readiness (`doctor --adoption`)

Run adoption diagnostics to verify configuration and read connectivity:

```bash
code-mower doctor --adoption --repo example-org/example-repo
```

When more than one Jira profile exists, use the same explicit selector on each
command:

```bash
code-mower doctor --adoption --repo example-org/example-repo \
  --provider-profile jira.env
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
   allowed operations (`ASSIGN_ISSUES`, `EDIT_ISSUES`, `TRANSITION_ISSUES`,
   `ADD_COMMENTS`), and
   confirms transition targets match `status_category_map`.

Queue reads use bounded 25-item pages so metadata-rich Jira projects remain
inside Code Mower's response-size limit. When Jira returns 404 for the
create-metadata issue-type inventory, doctor falls back to Jira's project
issue-type endpoint. Required create-field metadata still fails closed when
unavailable; Code Mower never treats missing metadata as an empty requirement.

### Read-only queue and controller preview

After doctor passes, inspect the live Jira-backed queue alongside GitHub lane
state, then ask the controller for a non-mutating decision:

```bash
code-mower lanes status --repo example-org/example-repo \
  --config code-mower.yml --provider-profile jira.env
code-mower controller run --repo example-org/example-repo \
  --config code-mower.yml --provider-profile jira.env \
  --mode dry_run --json
```

Omit `--provider-profile` when credentials resolve unambiguously from the
environment or a single secure profile. These commands read Jira metadata and
GitHub state only. Controller dry-run never dispatches, merges, or writes Jira.

---

## 4. Shared Orchestrator Contract

This shared session-brief contract, the Jira REST commands, and the double
write guard described elsewhere in this guide are available in
`code-mower==1.2.0`.

`code-mower session start` adds a `tracker` section to the operating brief
whenever `tracker.kind` is `jira_cloud`. Codex, Claude, and every other
selected orchestrator host receive identical rules from that section:

- Code Mower's Jira REST transport is authoritative for queue reads and every
  Jira mutation.
- Atlassian Rovo MCP, if the host has it connected, is optional local
  read/context enrichment only. It never gains queue or mutation authority.
- Every Jira write flows through the guarded `code-mower tracker mutate` or
  `code-mower tracker pr-sync` commands below, never through Rovo tools or
  any other path.
- The brief names the configured project by `project_key` (or `project_id`
  when no key is configured) only. It never includes issue body text,
  comments, attachments, or credentials.

See [Participants And Sessions](sessions.md#jira-tracker-contract) for the
full session brief contract.

---

## 5. Guarded Mutation Workflow

### Dry-Run Planning (Default)

Plan a mutation without making network writes:

```bash
code-mower tracker mutate --issue ABC-1 --claim --transition in_progress --json
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
code-mower tracker mutate --issue ABC-1 --claim --transition in_progress --apply --json
```

If either guard is absent, Code Mower refuses to write and emits an informative report.

### Pull Request Sync

PR sync also requires the PR author to be present in
`tracker.jira_cloud.sync.trusted_pr_authors`. Preview one bounded milestone
before adding `--apply`:

```bash
code-mower tracker pr-sync \
  --milestone opened \
  --pr-url https://github.com/example-org/example-repo/pull/42 \
  --branch feature/ABC-1-example \
  --pr-author trusted-builder \
  --json
```

The issue key is read only from the bounded branch name or leading PR-title
token, never from a PR body, issue prose, comment, source, or diff. GitHub
remains the only check and merge-gate authority.

---

## 6. Rollback and Safe De-adoption

To revert Jira integration at any time:

1. **Disable Writes Immediately:** Set `tracker.jira_cloud.mutations.writes_enabled: false`
   in `code-mower.yml`. This halts all mutation commands while preserving read diagnostics.
2. **Return to GitHub Default:** Delete the `tracker:` block from `code-mower.yml`.
   Code Mower immediately defaults to GitHub Issues.

---

## 7. Troubleshooting

| Symptom | Cause | Remediation |
|---|---|---|
| `tracker.jira.credentials: fail` | Missing or insecure credential file | Verify `JIRA_API_EMAIL` and `JIRA_API_TOKEN` are exported, or check `chmod 0600 ~/.config/code-mower/jira.env`. |
| `tracker.jira.read: fail (forbidden)` | Account lacks project access | Grant the Atlassian user account `BROWSE_PROJECTS` on the target project. |
| `tracker.jira.mutations: fail (permission_denied)` | Missing assignment, edit, transition, or comment permissions | Grant the listed `ASSIGN_ISSUES`, `EDIT_ISSUES`, `TRANSITION_ISSUES`, or `ADD_COMMENTS` permissions in the project permission scheme. |
| `target_status_not_configured` | Configured transition lacks destination status mapping | Ensure `status_category_map` defines status IDs for the transition's lifecycle category. |
| `transition_target_mismatch` | Jira workflow leads to an unmapped status | Update `status_category_map` to include the target status ID of the workflow transition. |
| `rate_limited` | Jira gateway capacity reached | Doctor logs a warning and defers probes without writing. Wait briefly and retry. |
| Controller reports `jira_unavailable` | Credentials did not resolve or the live Jira read failed | Run doctor with the same `--provider-profile` selector, fix its closed diagnostic, then repeat the dry-run preview. |
