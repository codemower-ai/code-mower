# Build Loop Operations

The Code Mower build loop turns ready GitHub Issues into builder PRs, then uses
peer audits and fix rounds to converge on a mergeable change.

The orchestrator is a workflow convention, not a hosted controller: issue plus
optional work order, ready label, builder lane, single-writer branch, reviewer
lanes, fix round, gate status, and merge. Code Mower's generated templates now
support that convention end to end. Humans still own credentials, branch
protection, owner escalations, reviewer calibration, and any account-bound or UI
steps.

## Labels

- `tier:R`: the issue is ready for builder dispatch.
- `builder:<lane>`: the issue or PR belongs to one builder lane.
- `dispatched:<lane>`: the dispatcher already handed this issue to a lane.
- `needs-owner`: a human decision, credential, UI click, or sitting is required.
- `owner-sitting`: the owner is actively doing the physical or account-bound step.
- `needs-*-audit`, `*-audit-done`, `*-audit-blocked`: reviewer lane state.

Product repos can rename these through `owner_surface` and `builder_identity` in
`code-mower.yml`; the generated workflows should use the configured values.

## Dispatch

`.github/workflows/dispatch-lanes.yml` runs on a schedule and by manual dispatch.
For each configured builder lane, it searches for open issues that have both the
ready label and that lane's builder label. It skips issues with assignees, open
`blocked-by:*` labels, owner labels such as `needs-owner` or `owner-sitting`,
active dispatch labels, open dependencies, or an open PR that already closes the
issue.

The dispatcher only hands an issue to a builder when the issue author is trusted
or a trusted author has left a work-order comment. For untrusted-author issues,
the work-order comment must come from the repository owner, a configured
decision authority, or an explicitly opted-in trusted author, and its first
content line must start with `# Work Order:`, `## Work Order:`, or
`Work order:`. If neither condition is met, the dispatcher leaves one idempotent
comment asking for a work order from an authority and does not add a dispatch
label.

Dependencies are read from an issue section named `## Dependencies`. Items like
`#123` must be closed. External keys such as `PROJ-123` are matched by title
prefix if the repository mirrors them as GitHub Issues.

The dispatcher posts a short lane mention that points at `docs/lanes/<lane>.md`
and `docs/build-loop.md`. Trusted-author issue bodies remain the source of task
detail and acceptance criteria. For untrusted-author issues, the trusted
work-order comment is the task source and issue title/body text is treated as an
opaque reference.

## WIP Cap

Each lane has a WIP cap. The dispatcher counts open PRs with the lane builder
label plus active dispatched issues that do not yet have a PR. If that count is
greater than or equal to the cap, the lane gets no new work in that cycle.

Set `CODE_MOWER_MAX_WIP` as a repository variable to override the default cap.
Manual workflow dispatch can also override the cap for one run.

## Single Writer

The branch owner is the only writer for a builder PR branch. The owning lane may
push fix rounds to that branch. Other builder and audit lanes must comment,
audit, or trigger a fix round instead of pushing.

The Mac runner installs a `pre-push` hook in the lane checkout before invoking
the builder CLI. The hook rejects pushes to branches outside the lane's allowed
prefixes, such as `codex/` or `claude/`, and outside the exact targeted PR
branch for fix rounds. Audit-duty runs reject all pushes.

If the owning lane must rewrite history, it should use `--force-with-lease`.
Unconditional force pushes are outside the build-loop contract.

## Owner Sitting

Use `needs-owner` when a lane needs a human-only decision, credentials, account
approval, local UI action, or signing step. The lane must leave a numbered action
list and stop that unit. The dispatcher excludes owner-labeled work from ready
dispatch and WIP accounting until the owner clears the label.

Use `owner-sitting` while the owner is actively doing the physical step. Builders
should not start work that depends on the sitting until that label is removed.

## Mac Runner

`.github/workflows/lane-mac-runner.yml` runs selected local CLI builder lanes on a
self-hosted macOS runner. It is disabled until the repository variable named by
`owner_surface.lane_runner_enabled_var` is set to `true`.

The runner uses the runner user's `gh`, `git`, Codex CLI, and Claude CLI
credentials. The workflow intentionally unsets `GH_TOKEN` and `GITHUB_TOKEN`
before starting lane work so PRs and comments are attributed to the runner user.
The generated job timeout is `owner_surface.lane_runner_max_minutes` plus a
15-minute cleanup grace period.

The runner includes GitHub issue and PR text only from trusted authors. By
default, that means the repository owner login plus the configured
`decisions.authorities` values, including `owner_surface.owner_login` when set.
Hosted builder bot accounts are not trusted by default because their comments can
restate untrusted issue content. To opt in a specific bot account, add it to
`owner_surface.lane_runner_trusted_authors`.

When the selected issue was opened by an untrusted author, the runner omits the
issue title and body and includes the latest trusted work-order comment instead.

Runner setup checklist:

- Add the configured runner labels, by default `self-hosted`, `macOS`, and
  `code-mower-lane`.
- Authenticate `gh` for the runner user.
- Authenticate the selected lane CLIs for the runner user.
- Set `LANE_MAC_RUNNER_ENABLED=true` only after the above checks pass.
- Set `LANE_CODEX_EXTRA_FLAGS` or `LANE_CLAUDE_EXTRA_FLAGS` in the runner
  environment only when the owner wants to widen the default sandbox.

## Operator Snapshot

Use Code Mower's lane-status command first when you need the pasteable operator
snapshot:

```bash
code-mower lanes status --repo OWNER/REPO
code-mower lanes status --repo OWNER/REPO --json
code-mower board serve --repo OWNER/REPO
```

It summarizes open PR lanes, audit/gate labels, major checks, recent Code Mower
workflows, local board/process hints when visible, local likely lane processes,
and the next action. It is read-only and metadata-only.
Local cwd paths are redacted by default; pass `--show-local-paths` only when you
are debugging locally.

Use `lanes status` as the pasteable visibility surface during supervised pilots.
Use `board serve` when you want the same state in a local browser. The board is
read-only, serves on loopback by default, and does not require a separate
observer setup. When `code-mower.yml` is present, the Board also shows a
supervised-pilot section backed by the controller policy engine: current
decision, queue counts, selected issue or PR, reviewer evidence, gate state, and
next action.
Run `code-mower board record --repo OWNER/REPO` for a one-shot local history
snapshot, or `code-mower board serve --repo OWNER/REPO --record-events` when
you want the browser Board to append redacted snapshots while it polls.

For the v1.0 supervised pilot, run the controller in dry-run mode before an
orchestrator acts on the queue:

```bash
code-mower controller run --repo OWNER/REPO
code-mower controller run --repo OWNER/REPO --dry-run
code-mower controller run --repo OWNER/REPO --mode promoted --json
```

The controller reads the same metadata surface as `lanes status`, adds ready
issue selection from safe labels, applies the merge-policy checks, and emits a
sanitized `controller_decision`, `merge_decision`, `queue_state_snapshot`, or
`owner_intervention` event when requested with `--event-file`.

## Required Tokens

`DISPATCH_TOKEN` should be a human-owned fine-grained PAT or delegated machine
user token. It is required for dispatch comments, agent PR labeling, and
fix-round mentions because events created by the built-in `GITHUB_TOKEN` do not
reliably trigger downstream workflows and some hosted agents ignore bot-authored
mentions.

Minimum token permissions:

- Contents: read
- Issues: read/write
- Pull requests: read/write

Set `DISPATCH_TOKEN_EXPIRES_AT` as a repository variable in `YYYY-MM-DD` format,
or `never` for a non-expiring token, so `code-mower doctor --github` can report
expiry posture.

## Rehearsal

To rehearse a fresh repository:

1. Run `code-mower init --builders codex,claude,cursor --dry-run` and inspect the
   plan, including the labels listed under `Labels to ensure`.
2. Run `code-mower init --builders codex,claude,cursor --apply`.
   This creates missing labels in the current checkout's GitHub repo unless
   `--skip-github-labels` is passed. If you are rendering outside the target
   checkout, pass `--repo OWNER/REPO` explicitly.
3. Copy or commit the generated workflows, lane docs, and `tools/lanes` script.
4. Set `DISPATCH_TOKEN`, `DISPATCH_TOKEN_EXPIRES_AT`, and any runner variables.
   For local audit runner workflows, set
   `CODE_MOWER_LOCAL_AUDIT_RUNNER_ENABLED=true` only after the runner is online
   and authenticated.
5. Create a test issue with `tier:R` and `builder:codex`.
6. Run the dispatcher manually with `dry_run=true` first.
7. Remove `dry_run` and confirm the issue receives `dispatched:codex` and the
   dispatch comment in one cycle.
