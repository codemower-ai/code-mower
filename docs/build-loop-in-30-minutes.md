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
pipx install --python python3.12 code-mower==0.5.0b52
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
normal CI checks. Read the current required checks, write a PATCH payload from
the `checks[]` entries GitHub returned, and keep existing source-app bindings
intact. Using `contexts[]` for this update can strip source-app bindings from
checks that already require a specific GitHub App.

```bash
gh api "repos/$REPO/branches/$DEFAULT_BRANCH/protection/required_status_checks" \
  > /tmp/required-status-checks.json

python3.12 - <<'PY'
import json
from pathlib import Path

source = Path("/tmp/required-status-checks.json")
data = json.loads(source.read_text(encoding="utf-8"))
checks = data.get("checks") or [
    {"context": context, "app_id": -1}
    for context in data.get("contexts", [])
]
preserved_checks = []
for check in checks:
    if check["context"] == "code-mower/gate":
        continue
    app_id = check.get("app_id")
    if app_id is None:
        app_id = -1
    preserved_checks.append({"context": check["context"], "app_id": app_id})
patch = {
    "strict": data.get("strict", True),
    "checks": preserved_checks + [{"context": "code-mower/gate", "app_id": -1}],
}
Path("/tmp/required-status-checks-patch.json").write_text(
    json.dumps(patch, indent=2) + "\n",
    encoding="utf-8",
)
PY

gh api -X PATCH "repos/$REPO/branches/$DEFAULT_BRANCH/protection/required_status_checks" \
  --input /tmp/required-status-checks-patch.json
```

The patch payload should look like this shape:

```json
{
  "strict": true,
  "checks": [
    {"context": "existing-ci", "app_id": 15368},
    {"context": "existing-deploy", "app_id": 123456},
    {"context": "code-mower/gate", "app_id": -1}
  ]
}
```

Preserve every existing `checks[]` entry and every non-null `app_id`; write
Any-source entries as `app_id: -1`, and add only the `code-mower/gate` entry
with `app_id: -1` for Any source. If your repository uses the GitHub settings
UI instead, choose the source shown as Any source for `code-mower/gate`. Do not
choose GitHub Actions.

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
## 8. Orchestrator Prompt (Copy-Paste)

To bootstrap the reference configuration with an AI orchestrator, paste this
prompt into a Claude Code session running in the repository you want to pilot,
with `gh` authenticated as a repository admin:

> You are my orchestrator for adopting Code Mower
> (https://github.com/codemower-ai/code-mower) on this repository, in the
> reference configuration: you (Claude Code) as orchestrator; Claude Code,
> Codex, and Cursor as builder lanes; Claude Code and Codex as reviewers
> (informational during the pilot, promoted to merge-gating only per
> `docs/lane-promotion-policy.md`); Gitar as an informational reviewer.
>
> Work through it in this order, verifying each step before the next, and stop
> with a numbered click-list whenever a step needs something only I can do
> (tokens, GitHub settings, app installs):
>
> 1. Read `docs/build-loop-in-30-minutes.md` and `docs/build-loop.md` from the
>    latest release tag of codemower-ai/code-mower and follow them rather than
>    improvising.
> 2. Install: `pipx install --python python3.12 code-mower` (allow
>    prereleases with `--pip-args="--pre"`). Verify `code-mower --version`.
> 3. Reviewer gate first: `code-mower init --easy` (dry-run), review the plan,
>    then `--apply`; `code-mower doctor --preflight` must pass, including the
>    human automation token. Tell me exactly which tokens to create, with
>    scopes, and where each goes. Two settings belong to the promotion step,
>    not the pilot: making `code-mower/gate` a required Any-source
>    branch-protection status, and enabling repository `allow_auto_merge` -
>    turn both on together only when a reviewer lane meets
>    `docs/lane-promotion-policy.md`. During the pilot, leave auto-merge off
>    and merge manually, so a PR can never merge on CI alone while the gate is
>    not yet required.
> 4. Builders: `code-mower init --builders codex,claude,cursor` (dry-run
>    first, then `--apply` from the repository checkout). If I have a Mac for
>    the self-hosted lane runner, walk me through
>    `docs/self-hosted-mac-runner.md` and verify with
>    `code-mower doctor --runner`; if not, enable only the Cursor lane for now
>    and say what the Mac would add.
> 5. Gitar: enable the GitHub App if I have it; keep it informational (it is
>    quota-bound and may need a manual "Gitar review" comment) - never
>    required.
> 6. Calibrate before trusting: run the starter value report, and tell me
>    plainly that reviewer lanes stay informational until repository-specific
>    evidence meets `docs/lane-promotion-policy.md`.
> 7. Dry-run the loop end to end: file one small real issue, label it
>    `tier:R` plus the builder label `init --builders` created for the lane
>    you enabled (the hosted Cursor lane's generated label is
>    `builder:grok-bot`; the Mac lanes use `builder:codex` /
>    `builder:claude` - confirm the exact names in the init output), watch it
>    get dispatched, the PR opened, the required audits post verdicts
>    (author lanes never gate their own PR: a hosted Cursor PR gets both
>    Codex and Claude verdicts; a Mac-built Codex or Claude PR gets only its
>    peer's), drive fix rounds by relaying the verdicts, and merge when every
>    required verdict is PASS (during the pilot this is your manual merge
>    criterion; the repo gate enforces it only after lanes are promoted per
>    the policy).
> 8. Throughout: never argue an audit BLOCKED away - either fix the finding or
>    record a decision with `code-mower decide` (I am the decision authority;
>    configure my GitHub login as `owner_login`). Escalate to me only with
>    owner-escalation asks that include options and a recommendation.
> 9. This is a beta, bring-your-own-agent-loop tool: expect to supervise the
>    first days. Report progress after each numbered step.

The prompt intentionally sends the orchestrator to the released docs first, so
it stays correct as the guides evolve.
