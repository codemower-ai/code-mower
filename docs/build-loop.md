# Build Loop Operations

The Code Mower build loop turns ready GitHub Issues into builder PRs, then uses
peer audits and fix rounds to converge on a mergeable change.

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
active dispatch labels, open dependencies, or an open PR that already references
the issue.

Dependencies are read from an issue section named `## Dependencies`. Items like
`#123` must be closed. External keys such as `PROJ-123` are matched by title
prefix if the repository mirrors them as GitHub Issues.

The dispatcher posts a short lane mention that points at `docs/lanes/<lane>.md`
and `docs/build-loop.md`. Issue bodies remain the source of task detail and
acceptance criteria.

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

Runner setup checklist:

- Add the configured runner labels, by default `self-hosted`, `macOS`, and
  `code-mower-lane`.
- Authenticate `gh` for the runner user.
- Authenticate the selected lane CLIs for the runner user.
- Set `LANE_MAC_RUNNER_ENABLED=true` only after the above checks pass.
- Set `LANE_CODEX_EXTRA_FLAGS` or `LANE_CLAUDE_EXTRA_FLAGS` in the runner
  environment only when the owner wants to widen the default sandbox.

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

Set `DISPATCH_TOKEN_EXPIRES_AT` as a repository variable in `YYYY-MM-DD` format
so `code-mower doctor --github` can report expiry posture.

## Rehearsal

To rehearse a fresh repository:

1. Run `code-mower init --builders codex,claude,cursor --dry-run` and inspect the
   plan, including the labels listed under `Labels to ensure`.
2. Run `code-mower init --builders codex,claude,cursor --apply`.
   This creates missing labels in the target GitHub repo unless
   `--skip-github-labels` is passed.
3. Copy or commit the generated workflows, lane docs, and `tools/lanes` script.
4. Set `DISPATCH_TOKEN`, `DISPATCH_TOKEN_EXPIRES_AT`, and any runner variables.
5. Create a test issue with `tier:R` and `builder:codex`.
6. Run the dispatcher manually with `dry_run=true` first.
7. Remove `dry_run` and confirm the issue receives `dispatched:codex` and the
   dispatch comment in one cycle.
