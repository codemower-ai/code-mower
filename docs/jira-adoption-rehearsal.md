# Jira Adoption Rehearsal

This runbook guides operators through adopting Atlassian Jira Cloud with
Code Mower. It covers three stages:

1. **Offline Rehearsal:** Walk through initialization, configuration, diagnostics,
   and dry-run planning without making any live network calls.
2. **Live Read-Only Checklist:** Connect to live Jira Cloud and verify
   identity, permissions, and workflow metadata without writes.
3. **Disposable Live-Write Checklist:** Perform a guarded, verified end-to-end
   mutation test on a disposable scratch issue under explicit owner authorization.

---

## Stage 1: Offline Rehearsal Walkthrough

This rehearsal can be completed entirely on a workstation without Jira Cloud
credentials or network access.

### 1. Initialize Configuration

Run `init` with the `--jira` flag (or `--tracker jira_cloud`):

```bash
code-mower init --jira --repo example-org/example-repo
```

Observe the generated `code-mower.yml`:
- Repository targets `example-org/example-repo`
- Default easy-mode reviewer lanes (Claude and Codex)
- Optional `tracker:` block configured for `jira_cloud`:
  - `site_url: "https://example.atlassian.net"`
  - `cloud_id: "11111111-2222-3333-4444-555555555555"`
  - `project_id: "10001"`
  - `project_key: "ABC"`
  - `mutations.writes_enabled: false` (safe default)
  - `status_category_map` mapping lifecycle states to numeric IDs

> **Note:** A standard `code-mower init` without `--jira` omits the `tracker`
> block entirely and remains pure GitHub.

### 2. Validate Configuration Posture

Check the configuration structure:

```bash
code-mower doctor --adoption --repo example-org/example-repo
```

In an offline environment without credentials:
- **`tracker.jira.config`**: Passes (`jira_cloud tracker block is configured`).
- **`tracker.jira.credentials`**: Reports `fail` with actionable remediation to
  provide `JIRA_API_EMAIL` and `JIRA_API_TOKEN`.
- **`tracker.jira.read`**: Reports `skip` until credentials resolve.
- **`tracker.jira.mutations`**: Reports `skip` until credentials resolve.

No tokens are exposed, and no network traffic leaves the machine.

### 3. Dry-Run Mutation Planning

Simulate an issue claim and transition plan:

```bash
code-mower jira-mutations plan --issue ABC-1 --claim --transition in_progress
```

The output report (`code_mower.jiraMutationPlan.v1`) shows:
- Mode is `plan`
- Operations `assign` and `transition` are planned
- Determinist replay fingerprints are generated
- `writes_authorized: false` (since `writes_enabled` is `false` and `--apply` is absent)
- Next action explains how to enable writes when ready

### 4. Dry-Run Pull Request Sync

Simulate PR status synchronization:

```bash
code-mower jira-sync --pr https://github.com/example-org/example-repo/pull/42
```

Confirms the linking relationship without mutating the issue.

---

## Stage 2: Live Read-Only Checklist

Execute this checklist when connecting to a real Jira Cloud instance for the
first time. All steps in this stage are strictly read-only.

### Pre-Flight Verification

- [ ] **Site URL:** Verify your organization's HTTPS domain (e.g. `https://myteam.atlassian.net`).
- [ ] **Cloud ID:** Verify the tenant UUID from `https://myteam.atlassian.net/_edge/tenant_info`.
- [ ] **Numeric Project ID:** Look up your project's immutable numeric ID in Jira
      Project Settings > Details (or via Jira REST API).
- [ ] **Project Key:** Confirm the short project key (e.g. `PROJ`).
- [ ] **Credentials:**
  - Export `JIRA_API_EMAIL` and `JIRA_API_TOKEN`, OR
  - Write `~/.config/code-mower/profiles/jira.env` and enforce `chmod 0600 ~/.config/code-mower/profiles/jira.env`.

### Run Read-Only Doctor

Run adoption diagnostics:

```bash
code-mower doctor --adoption --repo example-org/example-repo
```

Verify that all four Jira checks pass:

- [ ] `tracker.jira.config`: PASS
- [ ] `tracker.jira.credentials`: PASS (`Jira credentials resolved (env)` or `(profile)`)
- [ ] `tracker.jira.read`: PASS (`Jira read probe passed (metadata only, no writes)`)
      - Detail reports correct `project_key`
      - `permission_probe.BROWSE_PROJECTS: true`
      - Sample issues retrieved without errors
- [ ] `tracker.jira.mutations`: PASS (`Jira mutations configured (writes disabled by config guard)`)

If any check fails, consult the remediation advice printed in the doctor report
before proceeding.

---

## Stage 3: Disposable Live-Write Checklist

> **Caution:** Only perform live writes after Stage 2 passes completely.
> Always test on a disposable scratch issue first. Never test on active
> production tickets.

### 1. Create a Disposable Scratch Issue

1. In Jira, create a test issue in your project (e.g. `ABC-999`).
2. Set Summary to `Code Mower Disposable Test Issue`.
3. Verify the issue is currently in the `new` status (e.g. `To Do` or `Backlog`)
   and is unassigned.

### 2. Owner Authorization: Enable Writes in Config

Edit `code-mower.yml` to enable writes:

```yaml
tracker:
  kind: "jira_cloud"
  jira_cloud:
    # ...
    mutations:
      writes_enabled: true # <-- Explicit owner authorization
      allowed_operations:
        - "assign"
        - "transition"
        - "link"
        - "comment"
      transitions:
        in_progress: "31" # <-- Replace with your workflow's transition ID
```

### 3. Dry-Run the Plan First

Even with `writes_enabled: true`, omit `--apply` to inspect the plan first:

```bash
code-mower jira-mutations plan --issue ABC-999 --claim --transition in_progress --comment claimed
```

Review the JSON output:
- Verify the operations listed match your expectations.
- Confirm `mode: "plan"`.

### 4. Execute the Guarded Apply

Run with `--apply`:

```bash
code-mower jira-mutations apply --issue ABC-999 --claim --transition in_progress --comment claimed --apply
```

Verify the report:
- `mode: "apply"`
- `status: "applied"`
- `applied_count: 3` (assign, transition, comment)
- `write_request_count: 3`

### 5. Verify in Jira UI

Open `ABC-999` in Jira:
- [ ] Assignee is now set to the authenticated bot account.
- [ ] Status has transitioned to `In Progress`.
- [ ] Comment is posted containing the templated text and idempotency marker:
      `Code Mower idempotency marker: <hash>`.

### 6. Verify Replay Safety (Idempotency)

Re-run the exact same command:

```bash
code-mower jira-mutations apply --issue ABC-999 --claim --transition in_progress --comment claimed --apply
```

Verify that Code Mower recognizes the existing state:
- Assign operation reports `already_assigned`
- Transition operation reports `already_at_target_status`
- Comment operation reports `already_commented`
- `write_request_count: 0` (no redundant writes attempted)

### 7. Teardown and Cleanup

1. In Jira, delete or close the scratch issue `ABC-999`.
2. In `code-mower.yml`, revert `writes_enabled` to `false` until your team is
   ready for production builder dispatches:
   ```yaml
   mutations:
     writes_enabled: false
   ```
3. Re-run `code-mower doctor --adoption` to confirm the repository is back in
   safe read-only posture.
