# Build Loop In 30 Minutes

This is path B from the README: start from the reviewer gate, then enable a
builder dispatch loop.

Roles: Claude Code as orchestrator (workflow pattern, see
[Build Loop Operations](build-loop.md)); Claude Code, Codex, and Cursor as
builders; Claude Code and Codex as gating reviewers; Gitar informational.

Code Mower's templates support the orchestrator pattern end to end: GitHub issue
plus optional work order, ready labels, one owning builder lane, single-writer
branch, peer audit lanes, fix round, gate status, and merge. The orchestrator is
still a workflow convention, not a hosted controller. Humans still write or
approve work orders, create tokens, set branch protection, run account-bound
runner setup, decide owner escalations, and calibrate reviewers.

Use [Quickstart](quickstart.md) as the reference after this guided path. For the
operating model behind the labels and WIP cap, use
[Build Loop Operations](build-loop.md). For the macOS service details, use
[Self-Hosted Mac Runner](self-hosted-mac-runner.md).

## 1. Set Repository Variables

Run from the repository checkout that already completed the reviewer-gate path:

```bash
export REPO=OWNER/REPO
export DEFAULT_BRANCH=main
export OWNER_LOGIN=OWNER_LOGIN
```

If path A has not been completed in this repository, do this reviewer-gate
checkpoint first. If you already have a merged setup PR with Codex and Claude
audit evidence, skip to section 2.

```bash
python3.12 --version
pipx install --python python3.12 code-mower==0.5.0b51
gh auth status
code-mower init --easy
code-mower init --easy --apply --output-dir .code-mower.generated
code-mower doctor --preflight --json

git switch -c chore/code-mower-reviewer-gate
cp -R .code-mower.generated/. .
git add .github tools calibration-corpus.json context-packs.json \
  reviewer-spend.json reviewer-value-report.example.md
git commit -m "chore: add code mower reviewer gate"
git push -u origin HEAD
gh pr create \
  --repo "$REPO" \
  --base "$DEFAULT_BRANCH" \
  --head "$(git branch --show-current)" \
  --title "chore: add Code Mower reviewer gate" \
  --body "Install Code Mower generated reviewer-gate support."
export PR_NUMBER="$(gh pr view --repo "$REPO" --json number --jq .number)"
gh pr edit "$PR_NUMBER" --repo "$REPO" \
  --add-label needs-codex-audit \
  --add-label needs-claude-audit

export PR_HEAD_PATH="$(mktemp -d)"
gh repo clone "$REPO" "$PR_HEAD_PATH"
git -C "$PR_HEAD_PATH" fetch origin "pull/${PR_NUMBER}/head:code-mower-pr-${PR_NUMBER}"
git -C "$PR_HEAD_PATH" switch "code-mower-pr-${PR_NUMBER}"
export GITHUB_TOKEN="$(gh auth token)"
tools/run_codex_audit_pr.sh \
  --repo "$REPO" \
  --pr "$PR_NUMBER" \
  --repo-paths "$REPO:$PR_HEAD_PATH" \
  --merge-authority
tools/run_claude_audit_pr.sh \
  --repo "$REPO" \
  --pr "$PR_NUMBER" \
  --repo-paths "$REPO:$PR_HEAD_PATH" \
  --merge-authority
gh pr checks "$PR_NUMBER" --repo "$REPO" --watch
gh pr merge "$PR_NUMBER" --repo "$REPO" --squash --delete-branch
```

Path B assumes the generated reviewer-gate workflows and tools have already
landed on the default branch and that one setup PR has been audited and merged.

## 2. Create The Automation Token

Create a fine-grained personal access token owned by a human or delegated
machine user. Scope it to the selected repository only.

Exact repository permissions:

- Contents: read-only
- Issues: read and write
- Pull requests: read and write
- Metadata: read-only, implicit

Do not grant workflow, administration, secret, or organization-wide scopes for
`DISPATCH_TOKEN`.

Store the token and its expiry metadata:

```bash
gh secret set DISPATCH_TOKEN --repo "$REPO"
gh variable set DISPATCH_TOKEN_EXPIRES_AT --repo "$REPO" --body YYYY-MM-DD
```

Proof:

```bash
code-mower doctor --preflight --json
```

The GitHub stage should report the human automation token posture instead of a
missing `DISPATCH_TOKEN` or invalid `DISPATCH_TOKEN_EXPIRES_AT` warning.

## 3. Enable The Gate And Auto-Merge

Enable repository auto-merge:

```bash
gh api -X PATCH "repos/$REPO" -f allow_auto_merge=true
```

Require the `code-mower/gate` commit status from Any source, alongside your
normal CI checks. Inspect the current required checks first:

```bash
gh api "repos/$REPO/branches/$DEFAULT_BRANCH/protection/required_status_checks"
```

Then update the same endpoint with every existing required context plus
`code-mower/gate`:

```bash
gh api -X PATCH "repos/$REPO/branches/$DEFAULT_BRANCH/protection/required_status_checks" \
  -f strict=true \
  -F contexts[]=EXISTING_REQUIRED_CONTEXT \
  -F contexts[]=code-mower/gate
```

Repeat `-F contexts[]=...` for every existing required context returned by the
inspection call. If your repository uses the GitHub settings UI instead, choose
the source shown as Any source for `code-mower/gate`. Do not choose GitHub
Actions.

Proof:

```bash
gh api "repos/$REPO/branches/$DEFAULT_BRANCH/protection/required_status_checks" \
  --jq '.checks[]? | select(.context == "code-mower/gate")'
code-mower doctor --preflight --json
```

For `code-mower/gate`, the API response must show `"app_id": null`. If it shows
`"app_id": 15368`, the required check is bound to the GitHub Actions check-run
instead of the Code Mower commit status. The doctor should pass
`github.branch_protection` for the gate status and `github.repo.auto_merge` for
repository auto-merge.

## 4. Configure The Reference Build Loop

The worked example uses Claude Code as orchestrator convention; Claude Code,
Codex, and Cursor as builders; Claude Code and Codex as gating reviewers; and
Gitar as informational signal.

The important `code-mower.yml` stanza is:

```yaml
owner_surface:
  owner_login: OWNER_LOGIN
  lane_runner_labels:
    - self-hosted
    - macOS
    - code-mower-lane
  lane_runner_enabled_var: LANE_MAC_RUNNER_ENABLED
  builder_dispatch_cron: "*/30 * * * *"
  builder_wip_cap: "5"
  dispatch_token_env: DISPATCH_TOKEN
  dispatch_token_expires_var: DISPATCH_TOKEN_EXPIRES_AT
  ready_label: "tier:R"

merge_authority_excludes_author: true
builder_identity:
  labels:
    builder:codex: codex
    builder:claude: claude
    builder:grok-bot: grok-bot
  authors:
    chatgpt-codex-connector[bot]: codex
    claude[bot]: claude
    grok-bot[bot]: grok-bot
  branch_prefixes:
    cursor/: grok-bot
  fix_round_mentions:
    grok-bot: "@cursor"
  trailers:
    CODE_MOWER_BUILDER:codex: codex
    CODE_MOWER_BUILDER:claude: claude
    CODE_MOWER_BUILDER:grok-bot: grok-bot

lanes:
  codex:
    type: audit
    driver: local_cli
    provider: codex
    merge_authority: true
    labels:
      needs: needs-codex-audit
      done: codex-audit-done
      blocked: codex-audit-blocked

  claude_audit:
    type: audit
    driver: local_cli
    provider: claude
    trailer_lane: claude
    merge_authority: true
    labels:
      needs: needs-claude-audit
      done: claude-audit-done
      blocked: claude-audit-blocked

  gitar:
    type: audit
    driver: saas_event
    provider: gitar
    adapter: gitar
    informational: true
    labels:
      needs: needs-gitar-audit
      done: gitar-audit-done
      blocked: gitar-audit-blocked
```

Render the generated build-loop support:

```bash
git switch "$DEFAULT_BRANCH"
git pull --ff-only
code-mower init --builders codex,claude,cursor
code-mower init --builders codex,claude,cursor --apply --output-dir .code-mower.generated
```

The `cursor` input aliases to the existing hosted Cursor lane identity
`grok-bot`, so the generated lane docs use `docs/lanes/grok.md` and dispatch
comments mention `@cursor`.

The generated labels for this reference route include:

- ready and owner labels: `tier:R`, `needs-owner`, `owner-decision`,
  `owner-sitting`, `gate:override`;
- builder labels: `builder:codex`, `builder:claude`, `builder:grok-bot`;
- dispatch labels: `dispatched:codex`, `dispatched:claude`,
  `dispatched:grok-bot`;
- audit labels: `needs-codex-audit`, `codex-audit-done`,
  `codex-audit-blocked`, `needs-claude-audit`, `claude-audit-done`,
  `claude-audit-blocked`, `needs-gitar-audit`, `gitar-audit-done`,
  `gitar-audit-blocked`.

The easy profile may also create `builder:gitar` for provenance visibility, but
Gitar remains informational and is not a builder lane in this reference loop.

Review and land the generated files on the default branch:

```bash
git switch -c chore/code-mower-build-loop
cp -R .code-mower.generated/. .
git status --short
git add .github tools docs/lanes calibration-corpus.json context-packs.json \
  reviewer-spend.json reviewer-value-report.example.md
git commit -m "chore: add code mower build loop"
git push -u origin HEAD
gh pr create \
  --repo "$REPO" \
  --base "$DEFAULT_BRANCH" \
  --head "$(git branch --show-current)" \
  --title "chore: add Code Mower build loop" \
  --body "Add generated Code Mower build-loop workflows, runner script, and lane standing instructions."
export PR_NUMBER="$(gh pr view --repo "$REPO" --json number --jq .number)"
gh pr edit "$PR_NUMBER" --repo "$REPO" \
  --add-label needs-codex-audit \
  --add-label needs-claude-audit

export PR_HEAD_PATH="$(mktemp -d)"
gh repo clone "$REPO" "$PR_HEAD_PATH"
git -C "$PR_HEAD_PATH" fetch origin "pull/${PR_NUMBER}/head:code-mower-pr-${PR_NUMBER}"
git -C "$PR_HEAD_PATH" switch "code-mower-pr-${PR_NUMBER}"
export GITHUB_TOKEN="$(gh auth token)"
tools/run_codex_audit_pr.sh \
  --repo "$REPO" \
  --pr "$PR_NUMBER" \
  --repo-paths "$REPO:$PR_HEAD_PATH" \
  --merge-authority
tools/run_claude_audit_pr.sh \
  --repo "$REPO" \
  --pr "$PR_NUMBER" \
  --repo-paths "$REPO:$PR_HEAD_PATH" \
  --merge-authority
gh pr checks "$PR_NUMBER" --repo "$REPO" --watch
gh pr merge "$PR_NUMBER" --repo "$REPO" --squash --delete-branch
git switch "$DEFAULT_BRANCH"
git pull --ff-only
```

## 5. Prove The Mac Lane Runner

Register a macOS self-hosted runner from repository settings with these labels:

```text
self-hosted
macOS
code-mower-lane
```

Run these smoke checks as the macOS user that owns the runner process:

```bash
gh auth status
codex --version
codex exec --skip-git-repo-check --sandbox read-only --ask-for-approval never --ephemeral "Reply with exactly: ok"
claude auth status
claude -p "Reply with exactly: ok" --output-format json
```

In service mode, set `USER`, `LOGNAME`, `SHELL`, `LANG`, and a PATH that can
find `gh`, `git`, `codex`, `claude`, and `python3` in the runner `.env`, then
fully restart the listener.

Proof:

```bash
code-mower doctor --runner --json
```

The runner preset should pass the `runtime.runner_*` checks for LaunchAgent
posture, required environment variables, Codex and Claude auth probes, workflow
runner labels, `actionlint`, generated workflow lint, and listener freshness.

Enable the lane runner only after those checks pass:

```bash
gh variable set LANE_MAC_RUNNER_ENABLED --repo "$REPO" --body true
```

## 6. Dispatch Your First Issue

Create a small, low-risk issue. Pick one builder lane. This example uses Codex.

```bash
ISSUE_URL="$(gh issue create \
  --repo "$REPO" \
  --title "Code Mower build-loop smoke" \
  --body "## Goal
Make one tiny docs or test-fixture change that proves the build loop can open a PR.

## Acceptance
- Open exactly one PR.
- Keep the PR small.
- Do not deploy, publish, change credentials, or touch releases.
- Request the configured peer audit before exiting." \
  --label "tier:R" \
  --label "builder:codex")"
export ISSUE_NUMBER="${ISSUE_URL##*/}"
```

Run the dispatcher in dry-run mode first:

```bash
gh workflow run dispatch-lanes.yml --repo "$REPO" -f dry_run=true
gh run list --repo "$REPO" --workflow dispatch-lanes.yml --limit 1
```

If the dry run selected the issue, dispatch for real:

```bash
gh workflow run dispatch-lanes.yml --repo "$REPO" -f dry_run=false
gh run watch --repo "$REPO" "$(gh run list --repo "$REPO" --workflow dispatch-lanes.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
gh issue view "$ISSUE_NUMBER" --repo "$REPO" --json labels,comments
```

Watch for `dispatched:codex` and the dispatch comment. Then run one lane unit
immediately instead of waiting for the schedule:

```bash
gh workflow run lane-mac-runner.yml \
  --repo "$REPO" \
  -f lane=codex \
  -f target="issue:$ISSUE_NUMBER"
gh run watch --repo "$REPO" "$(gh run list --repo "$REPO" --workflow lane-mac-runner.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
gh pr list --repo "$REPO" --state open --label builder:codex --json number,title,headRefName,labels,url
```

The Codex lane should open a `codex/...` branch and a PR carrying
`builder:codex` plus `needs-claude-audit`. The generated gate excludes the
builder's own lane from satisfying merge authority, so the Claude audit is the
peer gate for this PR.

## 7. Watch Audit, Fix Round, And Merge

Sequence diagram in prose for the Codex example:

1. Owner or orchestrator writes the issue and adds `tier:R` plus
   `builder:codex`.
2. `dispatch-lanes.yml` sees the ready issue, verifies trust and WIP cap, adds
   `dispatched:codex`, and posts the Codex lane work order.
3. `lane-mac-runner.yml` runs the Codex lane on the self-hosted Mac, checks out
   the target, installs the single-writer push guard, opens a `codex/...` PR,
   adds `builder:codex`, and requests `needs-claude-audit`.
4. `local-cli-audit.yml` or a direct Claude audit run posts a current-head
   verdict. PASS adds `claude-audit-done`; BLOCKED adds
   `claude-audit-blocked`.
5. If Claude blocks the PR, `code-mower-fix-round-dispatch.yml` comments back
   to the owning Codex lane. Codex pushes only to the same branch, clears stale
   terminal labels through the generated cleanup path, and requests the audit
   again.
6. When the current head has clean peer audit evidence and normal CI is green,
   `code-mower-gate.yml` publishes `code-mower/gate` as success and asks GitHub
   to enable auto-merge.
7. GitHub merges after branch protection is satisfied. The issue closes through
   the PR's closing keyword.

Watch the PR:

```bash
export PR_NUMBER=PR_NUMBER
gh pr view "$PR_NUMBER" --repo "$REPO" --json labels,mergeStateStatus,statusCheckRollup
gh pr checks "$PR_NUMBER" --repo "$REPO" --watch
gh pr merge "$PR_NUMBER" --repo "$REPO" --auto --squash --delete-branch
```

What remains manual: the owner still chooses the issue, resolves `needs-owner`
items, rotates credentials, edits branch protection, calibrates whether Codex
and Claude should stay merge-gating, and handles any provider or account UI
steps that cannot safely run unattended.
