# Participants And Sessions

Choose the tools you want to work with. Claude Code and Codex are the default
pair; Devin and other participants are explicit additions. The agent hosting
your conversation is the default orchestrator for that session.

The participant picker, host-led session brief, single-orchestrator lease,
shared Jira tracker brief, controller host telemetry, and explicit Cursor
qualification documented below are available in `code-mower==1.2.2`.
Install from the matching tag when following release documentation, or use a
contributor checkout when testing later source changes.

## Choose During Setup

After installing Code Mower, run this in the repository:

```bash
code-mower init --interactive
```

The terminal shows checkboxes with Claude and Codex selected. Type a number to
toggle a participant, Enter to preview the configuration, or `q` to cancel.
For example, toggle `3` to add Devin. To choose and write the reviewable setup
in one pass, use `code-mower init --interactive --apply`.

Agents and scripts use the same selection model without a terminal:

```bash
code-mower init --with claude,codex,devin
code-mower init --with claude,codex,devin --apply
```

The generated `.code-mower.generated/code-mower.yml` contains the selected
reviewer profile and `session_defaults.participants`. Review and install it
with the generated support files following [the first-audit guide](try-in-10-minutes.md).
Later changes can start from your existing config:

```bash
code-mower init code-mower.yml --interactive --apply
code-mower next-steps --config .code-mower.generated/code-mower.yml
```

Participant selection configures review lanes and remembers session defaults.
Builder automation is enabled separately with the existing `init --builders`
flow after the first review works. Existing lane definitions and their promotion
flags are retained. The selected profile's active reviewer list changes to match
your selection: the preview explicitly lists removed reviewers and flags any
that currently have merge authority. Review those removals before installing the
generated files, since they change which reviewers the generated gate requires.
When editing interactively, known reviewers already active in the profile are
preselected alongside saved participants. Custom lanes absent from the picker
are still reported if the selection would remove them.

## Start From Any Agent

You can give the hosting agent this request:

```text
Start a Code Mower session with Claude, Codex, and Devin on OWNER/REPO.
You are the orchestrator because I am starting the session here. Use
code-mower session start, supply your own identity with --host, read the
resulting operating brief, and check participant readiness before assigning
work. Keep one writer per branch and independent current-head peer reviews.
```

From Codex, the corresponding command is:

```bash
code-mower session start --repo OWNER/REPO --with claude,codex,devin --host codex
```

From Claude, only `--host claude` changes. The same convention works for
`cursor`, `devin`, `grok-bot`, and `antigravity`. The agent supplies its own
identity; the user does not have to choose the orchestrator every time.
Wrappers can set `CODE_MOWER_HOST` instead. A plain shell with no host context
requires an explicit host rather than guessing from installed CLIs.

Omit `--with` to reuse the repository's saved participants, or Claude + Codex
when no selection has been saved. An orchestrator can coordinate participants
without also being selected as a builder or reviewer. `--orchestrator claude`
explicitly requests a handoff to Claude if the session starts in another tool.

The command writes a local brief under `.code-mower/sessions/` and reports its
path. `session show PATH` reads it; `session start ... --dry-run` previews it.
This is an agent-coordinated session: the hosting agent drives work through
its available tools, manual handoffs, or Code Mower's existing dispatcher.
Creating the brief does not launch provider processes, authenticate tools, or
prove they are available. Readiness remains unchecked until the agent verifies
the chosen execution path. Live PR progress remains in `code-mower lanes status`.

## Single Orchestrator Lease

`session start` takes a local lease before it saves anything, so one repository
working copy has one mutating orchestrator at a time. The lease lives at
`.code-mower/sessions/orchestrator-lease.json`, is written under a file lock
through a temporary file, and holds coordination metadata only: the repository
slug, the normalized orchestrator id, the session id, the acquired/renewed/
expires UTC timestamps, and a schema version. It is never uploaded or exported.

A second agent that starts a mutating session while the lease is live is refused
and told what the owner can do:

```text
error: another session already holds the mutating orchestrator lease for owner/repo
  holder: claude (session 4f1c...)
  expires: 2026-01-01T18:00:00+00:00 (about 5h 42m left)
  owner actions:
    inspect it:              code-mower session lease show
    let its owner release:   code-mower session lease release --session-id <id>
    take it over on purpose: code-mower session lease release --force
  read-only briefs need no lease: add --dry-run or --no-lease to session start
```

Manage the lease directly when a session ends or stalls:

```bash
code-mower session lease show
code-mower session lease renew --session-id <id>
code-mower session lease release --session-id <id>
code-mower session lease release --force
```

The holding session renews with its own session id; the brief reports that id
and the expiry. A lease past `expires_at` is free again, so an abandoned or
crashed session recovers on the next `session start` with no owner action. An
expired lease is not renewed — start a fresh session instead. Taking over a
*live* lease is always explicit: `session lease release --force`, or
`session start --force-lease`, only after the owner decides the holding session
is gone.

Read-only work needs no lease. `session start --dry-run` previews a brief
without touching the lease, and `--no-lease` saves one for reading, planning, and
reporting. Both mark the brief `"lease": {"state": "absent", "mutating": false}`
and say so in the instructions. `session show` still reads briefs saved before
leases existed. The lease coordinates sessions only; it does not change the
one-writer-per-branch rule in [the build loop](build-loop.md), repository merge
policy, or the generated workflows.

## Common Roles, Explicit Product Differences

| Concept | Rule |
| --- | --- |
| Participant | A selected product identity, independent of a particular execution transport. |
| Orchestrator | The hosting agent by default; coordinates assignments, evidence, and recovery. |
| Builder | One writer per branch, using an available execution path. |
| Reviewer | An independent current-head verdict through a supported review lane. |
| Merge authority | Repository policy; selecting or coordinating a tool does not grant it. |

Devin selects the local `devin_cli` reviewer and remains informational under
the starter policy. Devin Cloud needs its own execution setup. Cursor's agent
and Cursor Bugbot are separate selections. Grok Bot retains its own identity;
it is not silently translated into Cursor or Grok Build. Where there is no
dedicated transport, the brief calls for an agent handoff and makes no automatic
execution claim. Optional reviewer services such as Gitar cannot orchestrate
or build.

Session selection uses the same participant definitions as installation. The
reviewer registry and repository config continue to define execution and trust
policy, so a new participant does not require another orchestration algorithm.

## Jira Tracker Contract

When the repository's `tracker.kind` is `jira_cloud`, `session start` adds a
`tracker` section to the brief. Codex and Claude, or any other selected
orchestrator host, receive the same rules from that section regardless of
which one calls `--host`:

- Code Mower's Jira REST transport is authoritative for queue reads and every
  Jira mutation.
- Atlassian Rovo MCP, if the host has it connected, is optional local
  read/context enrichment only. It has no queue or mutation authority.
- Every Jira write must go through the guarded `code-mower tracker mutate` or
  `code-mower tracker pr-sync` commands; see
  [Jira Cloud Setup](jira-cloud-setup.md) for the double write-guard.
- At implementation start, the orchestrator previews and applies the configured
  `in_progress` claim/transition. When a non-draft PR is ready for human review,
  it previews and applies the `ready_for_review` PR-sync milestone, which uses
  the configured `review` transition.
- The brief names the configured Jira project by key or ID only. It never
  includes issue body text, comments, attachments, or credentials.

Sessions for the default GitHub tracker, or a repository with no `tracker`
block, omit this section entirely; existing GitHub-only briefs are unchanged.

## Cursor Orchestrator Posture

Cursor is qualified as an interchangeable orchestrator host under the same
participant session, controller-provider telemetry, and canonical working-copy
lease contracts as Codex and Claude. Cursor-hosted sessions receive identical
Jira tracker authority instructions when configured, and all controller events
(queue snapshots, controller decisions, owner interventions, and merge
decisions) correctly tag the orchestrator identity without changing policy
decisions or tool provenance.

### Jira Access For Cursor

The official Atlassian MCP server provides optional local read and context
enrichment when connected to Cursor. Broad Atlassian OAuth capability does not
grant Code Mower mutation authority; the session brief and guarded tracker
commands remain authoritative for every Jira mutation. Code Mower's Jira REST
transport is authoritative for queue reads.

### Noninteractive CLI Readiness

Cursor's noninteractive CLI requires authenticated and approved Atlassian MCP
plus read-tool auto-review (`--auto-review`) or an equivalent interactive
approval path. Without auto-review enabled, Cursor denies MCP read calls even
when the MCP server is authenticated. This is a host-readiness requirement,
not a Jira reliability issue.
