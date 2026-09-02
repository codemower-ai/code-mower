# Code Mower GitHub Setup

Code Mower v1.0 is GitHub-first. The easy path assumes GitHub pull requests,
labels, issue comments, pull request reviews, check runs, branch protection,
GitHub Actions, and the `gh` CLI.

Public and private repositories are both supported. The difference is not the
Code Mower lane model; it is provider access, token scope, and data exposure.

## Required GitHub Surfaces

Code Mower needs:

- read access to pull request metadata, head SHAs, labels, comments, reviews,
  changed files, and check/status state
- write access for labels and comments when a lane posts or updates audit state
- optional merge permission only after the repository explicitly delegates merge
  authority and the required review lanes are clean

The first setup should be non-mutating:

```bash
code-mower init --easy
code-mower doctor --easy --github
code-mower next-steps --profile recommended
```

`doctor --github` reads repository metadata and reports setup risks. It should
not create labels, comments, workflows, or provider reviews.

## Owner Surface Templates

`init --easy` emits owner-surface templates:
`.github/workflows/needs-owner-notify.yml` comments on issues or PRs labeled
with `owner_surface.needs_owner_label`, adds the configured owner as an assignee
without replacing existing assignees, and
`.github/workflows/weekly-status.yml` refreshes a pinned status issue from
`tools/status_report.py`. Repositories with audit lanes also receive
`.github/workflows/code-mower-gate-health.yml`, which alerts the owner when
audit labels, blocked builder PRs, local audit runs, or the self-hosted runner
stall.
For fork PRs where GitHub grants a read-only token, owner notify emits a
workflow warning instead of failing the run.
The generated merge gate treats `owner_surface.gate_override_label` as an
owner-only escape hatch: the label succeeds the gate only when the configured
owner applied it after the current PR head appeared in the GitHub timeline; a
non-owner-applied or stale override fails the gate. Set `gate_override_label`
to an empty string to disable the override path entirely. On `synchronize`, the
generated gate workflow removes a stale override label before recomputing the
status; if the label cannot be removed, the workflow still publishes a failing
gate status for the new head.

Configure the owner surface in `code-mower.yml` before enabling the weekly
schedule:

```yaml
owner_surface:
  owner_login: YOUR_GITHUB_LOGIN
  needs_owner_label: needs-owner
  owner_decision_label: owner-decision
  owner_sitting_label: owner-sitting
  gate_override_label: "gate:override"
  status_issue: "123"
  weekly_cron: "0 14 * * 1"
  gate_health_cron: "*/15 * * * *"
  gate_health_max_wait_minutes: "30"
  gate_health_liveness_minutes: "45"
  local_audit_runner_label: code-mower-audit
  local_audit_runner_enabled_var: CODE_MOWER_LOCAL_AUDIT_RUNNER_ENABLED
  dispatch_token_env: DISPATCH_TOKEN
  dispatch_token_expires_var: DISPATCH_TOKEN_EXPIRES_AT
  ready_label: "tier:R"
```

Create and pin the status issue with GitHub CLI:

```bash
gh issue create \
  --title "Code Mower status" \
  --label code-mower \
  --body-file - <<'CM_BODY'
Weekly Code Mower status will appear here.
CM_BODY
gh issue pin 123
```

The generated digest reads GitHub metadata only: labels, titles, PR state,
assignees, timestamps, and optional local Code Mower spend/value files. It does
not read source, diffs, transcripts, or issue bodies.
Use `owner_surface.owner_sitting_label` for physical or account-bound sittings
that no builder lane can complete. Generated owner surfaces include those items
in the owner queue and keep them out of dispatchable ready/WIP lists.
Use `owner_surface.owner_decision_label` for decision asks that survived
orchestration triage. A raw `needs-owner` label triggers the generated owner
notification, but the weekly owner queue includes it only after the item also
has the decision label. Before adding that decision label, the ask should name
the options, the recommendation, and the existing ADR, operating-model section,
issue comment, or policy it touches. If accepted project context already
answers the ask, the orchestration lane may comment with the resolution, remove
`needs-owner`, and continue without paging the owner.

Decision markers are honored only when the comment author is configured as a
decision authority. Code Mower includes `owner_surface.owner_login` by default
when it is set. Add any additional trusted logins under `decisions.authorities`:

```yaml
decisions:
  authorities:
    - release-manager
```

When an authorized owner or orchestrator closes an audit finding by policy,
record a hidden decision marker in the issue or PR conversation:

```bash
code-mower decide \
  --id ADR-007 \
  --scope finding \
  --finding-id codex:b93829375d1f7c3d27fa \
  --by owner \
  --ref ADR-007
```

The command prints a PR-comment body containing
`<!-- CODE_MOWER_DECISION: ... -->`; add `--repo OWNER/REPO --issue 123 --post`
to post it directly. Codex and Claude audits render a stable `Finding ID` line
for each structured finding. A marker covers a finding only when its
`finding_id` equals that stable fingerprint, or when `scope=topic` and
`resolves` equals the finding title verbatim. Substring, leading-token,
detail-only, and file-line-only matches are intentionally ignored. Markers from
other authors, including builders and bots, are ignored and should be reported
as P3 `unauthorized decision marker`. Covered findings must be reported as P3
`acknowledged by decision <id>` and must not block. The trailer labeler and
merge gate also treat a blocked audit comment as done when all P0, P1, and P2
findings in that comment are covered by trusted decisions.
Gate health also treats open PRs with a `builder:<lane>` label plus an
unresolved `*-audit-blocked` label as stalled with work when that lane has not
authored a PR commit or comment within `gate_health_liveness_minutes`; the
generated workflow gets builder authors from `builder_identity.authors`. Lanes
with no blocked builder PRs do not alert.

## Multi-Repo Rollout

When one Code Mower config controls several sibling repositories, keep the
lanes and labels in the control config and render each sibling repo from that
same file:

```bash
code-mower init ../control-repo/code-mower.yml \
  --add-repo OWNER/SIBLING_REPO \
  --profile recommended \
  --apply \
  --repo OWNER/SIBLING_REPO \
  --output-dir .code-mower.generated
code-mower doctor ../control-repo/code-mower.yml --preflight --json
```

`--add-repo` does not edit `code-mower.yml`; it adds a reviewable target to the
init manifest so the generated labeler, clear-stale, labels, and support files
can be copied into the sibling repository. `--repo` is the GitHub repository
where apply mode creates missing labels; omit it only when running from the
target checkout. After review, add the sibling slug to `repositories:` in the
control config. `doctor` reports the committed
repository count and slugs even without `--github`, then `doctor --github`
checks each configured repo's metadata, Actions settings, and branch
protection.

## Recommended Public Repo Hardening

For the public Code Mower source repo, and for any repository that wants to run
Code Mower as a normal development gate, use these GitHub defaults before
inviting outside users:

- protect `main`;
- require the Code Mower CI check before merge;
- block force pushes and branch deletion on protected branches;
- enable automatic branch deletion after merge;
- enable secret scanning and push protection where the plan allows it;
- enable Dependabot alerts and dependency update pull requests;
- add a security policy and clear private vulnerability reporting path;
- add issue templates and a pull request template so first-user feedback is
  structured;
- keep Discussions enabled for setup questions that are not bugs;
- require at least two owner/admin-capable maintainers on the org or repo; and
- keep the old source location clearly redirected or archived so users do not
  install from a stale repository.

Use the repository settings URL directly when doing a manual pass:

<https://github.com/codemower-ai/code-mower/settings>

Code Mower should warn about missing required branch protection and dangerous
workflow-token defaults, but it should not silently mutate a user's repo
settings in easy mode.

## Public Repositories

Public repositories are the lowest-friction OSS path:

- GitHub Apps and hosted reviewers usually need less manual access work.
- Review output can be public, so third-party code exposure is less surprising.
- Fork pull requests are common, so workflow safety matters more.

For public repos with outside contributors, keep this invariant:

> Jobs that run with base-repository write permissions must not checkout or
> execute untrusted pull request code.

Code Mower labeler workflows can use `pull_request_target` for label writes
only when they operate on event metadata and base-branch workflow code. Audit
execution should happen in a trusted local runner or another explicitly trusted
environment.

## Private Repositories

Private repositories work, but each provider needs explicit access:

- local CLI lanes need a local checkout and GitHub auth that can read the repo
  and post comments or labels
- hosted SaaS lanes need the provider's GitHub App installed on the selected
  private repository
- provider plans may differ for private repositories
- private code or diffs may be sent to the selected provider unless the lane is
  a truly local model lane

Use the `privacy` profile when a team wants the local/private benchmark floor:

```bash
code-mower doctor --profile privacy --probe-runtime --github
```

Local LLM lanes still send selected source context to the configured endpoint.
That endpoint may be local, private, or hosted; the repo owner owns that trust
decision.

## Standalone Package Checkout

The public Code Mower source repo can be fetched from GitHub Actions over
unauthenticated HTTPS. Use that path when possible; it is the lowest-friction
v1.0 setup and avoids spreading broad personal tokens across repositories.

When a repository consumes a private Code Mower fork, a private source branch,
or a private package index, GitHub Actions needs an explicit read credential.
The recommended proof path is a read-only deploy key:

1. Generate an Ed25519 SSH keypair dedicated to Code Mower package checkout.
2. Add the public key as a read-only deploy key on the private Code Mower source
   repository or fork.
3. Add the private key to each product repository as the Actions secret
   `CODE_MOWER_STANDALONE_DEPLOY_KEY`.
4. Use the `Code Mower standalone shadow` workflow to fetch the pinned
   standalone commit over SSH, run `doctor --easy`, and run
   `migration wrapper-rehearsal` against the repo-local mirror.

This proves private-source checkout without giving the product repository a
broad personal token. The deploy key can be deleted once the repo uses public
source or a package-index install path.

## Token And Secret Model

The built-in `GITHUB_TOKEN` is enough for some workflows, but not all repos.
Repository or organization settings may make the workflow token read-only. Fork
pull requests also have restricted secret access.

Code Mower lanes therefore support one human-owned automation token by default:

- `DISPATCH_TOKEN`
- `DISPATCH_TOKEN_EXPIRES_AT` as a repository variable containing the PAT expiry
  date in `YYYY-MM-DD` format, or `never` when the PAT has no expiration date

Keep older per-lane token names only as beta compatibility fallbacks:

- `CODEX_AUDIT_LABEL_TOKEN`
- `CLAUDE_AUDIT_LABEL_TOKEN`
- `GITAR_AUDIT_LABEL_TOKEN`
- `GREPTILE_AUDIT_LABEL_TOKEN`
- `QODO_AUDIT_LABEL_TOKEN`
- `CURSOR_BUGBOT_AUDIT_LABEL_TOKEN`
- `DEVIN_AUDIT_LABEL_TOKEN`
- lane-specific local or research tokens when enabled

Use a fine-grained PAT owned by a human or explicitly delegated machine user
with the smallest useful permissions. The default `DISPATCH_TOKEN` needs:

- Issues: read/write
- Pull requests: read/write
- Contents: read

Set the token and expiry metadata with:

```bash
gh secret set DISPATCH_TOKEN
gh variable set DISPATCH_TOKEN_EXPIRES_AT --body YYYY-MM-DD
# or, for a non-expiring PAT:
gh variable set DISPATCH_TOKEN_EXPIRES_AT --body never
```

`code-mower doctor --github` fails when the generated human-token workflows are
enabled but the secret is missing, the expiry variable is missing, or the
recorded expiry date is malformed or in the past. It warns when rotation is due
within 14 days or when the value is still the `YYYY-MM-DD` setup placeholder.
It passes `never` as an explicit non-expiring posture. The check reads only
GitHub secret/variable metadata; it cannot read the PAT value.

Generated workflows still grant `GITHUB_TOKEN` write permissions where GitHub
allows them, but human-token templates prefer `DISPATCH_TOKEN` so label events
and agent mentions trigger downstream automation. Per-lane PAT names remain
compatibility fallbacks for repositories that intentionally use separate
credentials.

Do not store provider API keys in repository docs. Use environment variables,
GitHub secrets, or provider-specific local auth stores.

Generated issue-comment labelers are fail-closed. They only run for comments
from the lane's configured audit bot authors plus any authors you explicitly
add with repository variables such as `CLAUDE_AUDIT_BOT_AUTHORS`,
`DEVIN_BOT_AUTHORS`, or `GITAR_BOT_AUTHORS`. The built-in Codex and Claude
local audit lanes trust `github-actions[bot]` by default so
`local-cli-audit.yml` can post verdicts with `GITHUB_TOKEN`; custom and SaaS
lanes should add that shared bot identity only when their workflow is meant to
post authoritative verdicts.

For merge-gating Codex and Claude lanes, `github-actions[bot]` comments also
need the hidden `CODE_MOWER_AUDIT_RUN` marker emitted by the wrappers inside
GitHub Actions. The wrappers post the comment, immediately edit it to bind the
marker to GitHub's created comment id plus a SHA-256 digest of the final comment
body, and the generated labeler listens for both created and edited comments.
The labeler and gate verify that bound marker against a trusted
`local-cli-audit.yml` run for the same PR/head before accepting the terminal
trailer. The generated gate also re-checks when `Code Mower Local CLI Audits`
completes, resolving missing workflow-run PR metadata from the run commit or
head branch before fetching the current PR head, so a status left pending while
the audit workflow was still active can settle after the terminal verdict
lands. The in-flight scan fetches only non-terminal workflow runs before local
PR/head/lane filtering, so historical audit volume does not dominate each gate
check. If GitHub omits the run's `pull_requests` array, Code Mower resolves the
run head SHA through the commit-to-PRs API; without that match, the shared-bot
comment is ignored. When GitHub does provide `pull_requests`, the run must
belong to the target PR before it can keep that PR's gate pending. Use
lane-specific posting tokens or remove
`github-actions[bot]` from the lane authors when a repository wants a stricter
separation between Actions jobs and merge-gating audit verdicts.
If the labeler cannot fetch enough comment history to identify the latest
trusted current-head verdict, it leaves labels unchanged so an older delayed
comment cannot overwrite a newer audit result. When a webhook event comment is
not yet visible in the comments API snapshot, the labeler still includes that
event comment in the same freshness comparison.

When one operator or shared machine user posts multiple local-lane comments,
each configured-author comment must carry the matching hidden
`*_AUDIT_STATE` trailer. The trailer, not the shared GitHub login, is the
authoritative lane signal for labeler prefilters and cloud reviewer metadata.

## Actions Billing And Spending Limits

GitHub can report Actions as enabled while refusing to start every job because
private-repo minutes, billing, or spending limits are not healthy. In that
state branch protection may show failed CI, labeler, or deploy checks even
though the jobs never executed.

`code-mower doctor --github` inspects recent failed run annotations and warns
when GitHub reports that jobs were blocked by billing or spending limits. Treat
that as an account setup issue, not a code failure:

1. fix GitHub billing or Actions spending limits
2. rerun failed workflows
3. only then rely on branch protection or deployment checks as merge signals

If Actions are account-blocked during a migration, local validation plus clean
audits can establish code quality, but the repo owner should still repair
Actions before restoring unattended merge flow.

`doctor --github` also samples recent Actions runs and reports workflow names,
events, run counts, and approximate minutes. In private repositories it warns
when optional metadata or reviewer-labeler workflows dominate the sampled runs,
or when scheduled workflows are still present. Tune the sample size with:

```bash
code-mower doctor --easy --github --actions-cost-sample 100 --json
```

The cost sample is content-free: it does not fetch logs, diffs, source, or
secrets.

## Private Repo Cost Controls

Private repositories consume GitHub Actions minutes for started jobs. Code
Mower should therefore keep metadata workflows cheap:

- avoid recurring cron sweeps for hosted or informational lanes
- prefer explicit labels, trusted comments, or manual bridge/stale workflow
  dispatches
- add job-level `if:` guards to every `issue_comment` labeler before checkout
- require informational SaaS lanes to opt in with an existing lane label
- keep branch-protection merge gates limited to promoted structured audit lanes

The reference Devin bridge is event-driven plus manual dispatch only. The
Gitar, Qodo, and Cursor BugBot labelers are passive: they do not trigger the
hosted reviewer, and they skip unrelated issue comments before checking out
code.

## Self-Hosted Local CLI Audit Runner

`code-mower init --easy --apply` emits `.github/workflows/local-cli-audit.yml`
when the selected profile includes supported local CLI audit lanes such as Codex
or Claude. The workflow runs on `[self-hosted, macOS, code-mower-audit]` for
same-repository pull requests on `opened`, `synchronize`, and relevant `labeled`
events. It runs from the trusted base-branch workflow with
`pull_request_target`, checks out trusted support scripts from the default
branch, checks out the PR head separately as audit context, and runs the repo-local
`tools/run_codex_audit_pr.sh` or `tools/run_claude_audit_pr.sh` wrapper for
each present `needs-*-audit` label.

In product repositories, those wrappers and companion labeler/stale-clear
workflows normally delegate to the pinned standalone `tools/code_mower` shim. In
the Code Mower repository itself, the dogfood workflows use the trusted
default-branch checkout with `scripts/dev-python -m code_mower.cli` so gate
decisions exercise the source that just landed on `main`.

Runner setup recipe:

1. Register a macOS self-hosted runner from repository settings, using the
   architecture that matches the machine.
2. Add the custom runner label from `owner_surface.local_audit_runner_label`
   (default `code-mower-audit`). The generated workflow's `runs-on` uses that
   label, so product repos can set values such as `bridge-pro-audit` and keep
   regeneration clean.
3. Start with `./run.sh` from the same macOS user account that owns provider
   CLI logins. Install it as a service only after smoke tests pass.
4. If the runner runs as a service or launch daemon, set `USER`, `LOGNAME`,
   `SHELL`, and `LANG` in the runner `.env` file. The generated workflow checks
   those variables before checkout because missing launchd environment breaks
   local CLI and keychain auth in non-obvious ways. After editing `.env`, fully
   recycle the runner listener; `svc.sh stop/start` may leave the old listener
   process alive with the previous environment.
5. Ensure `gh`, `git`, `python3`, `codex`, and `claude` are on PATH. If they
   live outside the default Homebrew/system paths, update
   `CODE_MOWER_LOCAL_AUDIT_PATH` in the generated workflow.
6. Verify local auth from that same account: `gh auth status`,
   `codex --version`, `claude auth status`, and
   `claude -p "Reply with exactly: ok" --output-format json`.
7. Set the repository variable named by
   `owner_surface.local_audit_runner_enabled_var` to `true` only after the
   runner is registered, labeled, online, and authenticated. Until then the
   workflow is skipped instead of queueing self-hosted jobs that cannot start.
8. Add the shared posting-token secret `DISPATCH_TOKEN` and expiry variable
   `DISPATCH_TOKEN_EXPIRES_AT`. The workflow can post with `GITHUB_TOKEN`, but
   GitHub does not fire `issue_comment` workflows for comments created by the
   built-in token, so runner lanes need a human-owned PAT/App token for trailer
   labelers to flip `needs-*-audit` to done or blocked.

The generated workflow grants `pull-requests: write`, runs one matrix job per
configured local audit lane, uses concurrency keyed by workflow, PR, head SHA,
and lane with `cancel-in-progress: true`, and wipes the `pr-head` workspace
before every checkout so one run cannot inherit another run's worktree. After
checkout, it re-reads the PR head SHA and exits cleanly without running or
uploading audits when a newer commit has superseded the queued run. Use the
runner workflow as the dispatch mechanism for local lanes:
it invokes `tools/run_codex_audit_pr.sh` and
`tools/run_claude_audit_pr.sh`, which in turn run `codex exec` or `claude -p`
from the authenticated macOS account. Do not rely on `@codex` issue mentions
to dispatch local audit lanes; those mentions are not a dependable Actions
trigger.
Generated clear-stale workflows include the lane id in their workflow name and
concurrency group so Codex and Claude stale-label cleanup cannot cancel each
other on the same PR push.

If `CODE_MOWER_CLOUD_TOKEN` is configured, the generated workflow also uploads
metadata-only reviewer evidence after every audit attempt. It sends saved
verdict artifacts with `code-mower cloud reviewer-runs`, merging matching
runner-temp `reviewer-spend.json` rows, plus trusted default-branch work-order
`*.cloud-event.json` sidecars through `cloud dogfood`. The generated template
sets the runner-temp spend path at step scope because GitHub Actions does not
allow the `runner` context in job-level `env`. The upload step runs with
`if: always()` for non-superseded audit attempts, skips successfully when the
token is absent, and must not block merge authority if the cloud service is
unavailable.

The audit wrappers verify the GitHub API head SHA first, then skip the
`pull/N/head` fetch when the `--repo-paths` checkout is already at that SHA.
That makes the generated `persist-credentials: false` PR-head checkout viable
for normal runner-dispatched audits. If a custom workflow intentionally passes
a checkout that is not at the PR head, keep credentials on that checkout or
fetch the PR head before invoking the wrapper.
If the head moves after an audit starts, the wrapper posts a STALE requeue note
and exits successfully; gate health counts STALE separately from UNKNOWN/infra
failures because the newer head's audit owns the merge signal.

macOS Keychain access is user-session sensitive. If `svc.sh install` writes
`SessionCreate=true` into `~/Library/LaunchAgents/actions.runner.*.plist`, the
runner starts in a new security session and Claude Code cannot read the login
keychain. `code-mower doctor --preflight` warns on that plist shape; remove the
`SessionCreate` key, unload/reload the LaunchAgent or fully recycle the runner
listener, then rerun the Claude prompt smoke from a runner job before trusting
unattended audits.

## Gate Health Alarm

`code-mower init --easy --apply` emits
`.github/workflows/code-mower-gate-health.yml` when the selected profile has
audit lanes. The workflow runs on `owner_surface.gate_health_cron` and can also
be launched with `workflow_dispatch` plus a temporary `max_wait_minutes`
override.

The gate-health workflow calls `tools/code_mower gate-health`, which uses the
pinned standalone package rather than embedded workflow Python. The alarm
comments once per incident on `owner_surface.status_issue`, chunks large alert
batches, and adds `gate-stalled` to PRs whose current head has waited too long.
It detects these metadata-only conditions:

- a configured `needs-*-audit` label stays present longer than the threshold
  without a trusted terminal verdict comment bound to the PR head SHA
- the latest completed local CLI audit check on the PR head failed, timed out,
  or required action, unless a newer audit check is still pending
- the latest trusted comments for one lane are repeatedly UNKNOWN; STALE
  requeues are counted separately because a moved head should be superseded by
  the newer head's audit. The generated default is 3 and can be changed with
  `CODE_MOWER_GATE_HEALTH_UNKNOWN_STREAK_THRESHOLD`; the streak comment history
  defaults to 168 hours via
  `CODE_MOWER_GATE_HEALTH_UNKNOWN_STREAK_HISTORY_HOURS`, including up to 100
  recently closed PRs via `CODE_MOWER_GATE_HEALTH_UNKNOWN_STREAK_CLOSED_PR_LIMIT`
- no self-hosted runner with the configured local audit runner label is online
- GitHub runner inventory cannot be inspected with the configured token

The alarm only reads labels, label events, verdict comments, Actions run
metadata, PR head SHAs, and runner status. It does not read source, diffs,
transcripts, logs, or issue body text.

GitHub's default `GITHUB_TOKEN` cannot be granted repository Administration
permission through workflow `permissions`. To enable the runner-online check,
store `CODE_MOWER_GATE_HEALTH_RUNNER_TOKEN` as a fine-grained PAT or GitHub App
installation token with repository Administration read access. Without that
token, the workflow still alerts the owner that runner inventory could not be
checked.

## Branch Protection And Merge Authority

Code Mower should not assume it can merge. A repository should make merge
authority explicit:

- protect the default branch
- require normal CI and deployment checks
- require the merge-gating audit lanes that the repo has promoted
- keep new or uncalibrated lanes informational

The default v1.0 posture is:

- Codex audit and Claude audit can be merge-authority lanes when configured.
- Gitar and other SaaS reviewers start informational.
- Cursor BugBot, CodeRabbit CLI, Gemini/Antigravity, Hermes, local LLMs, Qodo,
  Greptile, Devin, and future hosted lanes require calibration before promotion.

`code-mower init --easy --apply` emits `.github/workflows/code-mower-gate.yml`
when the selected profile has merge-authority lanes. The gate is metadata-only:
it reads PR labels, audit comments, and authenticated PR metadata; validates
the requested SHA against the PR's current head; treats `needs-owner` as
pending; treats configured current-head `*-blocked` labels as failure; applies
`merge_authority_excludes_author` from `code-mower.yml`; publishes the
`code-mower/gate` commit status; keeps the workflow job name distinct from that
status so GitHub does not bind branch protection to the Actions check-run; and
calls GitHub's
`enablePullRequestAutoMerge` when the status is green. A `*-done` or
`*-blocked` label counts only when the matching terminal audit comment carries
the same head SHA and comes from the lane's configured bot authors, so stale
labels and forged comments cannot win races against cleanup. When several
trusted terminal comments exist for the same lane and head, the most recent
comment wins; a later BLOCKED trailer demotes an earlier PASS even if a stale
`*-done` label is still present. Before success, the gate also checks Actions
metadata for queued or in-progress local audit runs for required lanes on the
current head and publishes `pending: audit in flight` until those runs settle.
If a previously audited head is no longer an ancestor of the current head, the
gate posts a metadata-only notice that commits may have been dropped and keeps
using only current-head audit verdicts.
The generated product-support files include `tools/audit_labeler_lib.py` and
`tools/decisions.py`, which the gate uses for GitHub Actions audit-comment
attestation and decision-covered blocker handling without uploading source,
diffs, or transcripts.
Hosted builder tokens usually cannot enable auto-merge themselves, so keep that
call in the repository gate workflow. If GitHub rejects that optional
auto-merge call after a green gate, the workflow logs a notice and leaves the
published gate status green.
For unattended merges, configure a dedicated machine-user or GitHub App token in
the `CODE_MOWER_GATE_AUTOMERGE_TOKEN` secret; `DISPATCH_TOKEN` is accepted as a
fallback when it already belongs to the same trusted automation identity. The
gate still uses the default `github.token` for repository reads and status
publication, and uses the merge-capable token only for the final
`enablePullRequestAutoMerge` GraphQL call. Do not use hosted builder tokens as
merge tokens.

The recommended three-builder pattern is:

- Codex-built PRs carry `builder:codex` or are opened by a mapped Codex author,
  so Claude audit gates them.
- Claude-built PRs carry `builder:claude` or are opened by a mapped Claude
  author, so Codex audit gates them.
- Hosted-built PRs carry a third builder identity such as `builder:cursor`,
  so both Codex and Claude audit gates still apply.

Use a single-writer rule for PR branches: only the lane named by the owning
`builder:<lane>` identity may push commits to that branch. Peer lanes should
review, comment, or open follow-up work, not push competing commits. When the
owning builder must rewrite a branch, use `git push --force-with-lease` and
never a blind force push.

Configure those identities in `code-mower.yml`:

```yaml
merge_authority_excludes_author: true
builder_identity:
  labels:
    builder:codex: codex
    builder:claude: claude
    builder:cursor: cursor
    builder:grok-bot: cursor
  authors:
    chatgpt-codex-connector[bot]: codex
    claude[bot]: claude
    cursor[bot]: cursor
    grok-bot[bot]: cursor
  branch_prefixes:
    cursor/: cursor
  fix_round_mentions:
    cursor: "@cursor"
```

PR-body trailer mappings remain configuration-valid for non-gating provenance
experiments, but merge-authority author exclusion uses only labels and
authenticated PR authors until trailer sources can be trusted.
Generated gates register default `builder:<lane>` labels for audit lanes,
including informational lanes, so conflicting builder provenance is visible
before a merge-authority lane is excluded.
When `builder_identity.branch_prefixes` is configured, generated agent PR
labeling adds the matching `builder:<lane>` label, drops stale audit terminal
labels, and re-adds merge-authority `needs-*-audit` labels on PR open/reopen and
synchronize. When `builder_identity.fix_round_mentions` is configured,
generated fix-round dispatch comments once per blocked head and audit lane.
Both generated workflows use `owner_surface.dispatch_token_env`, which must name
a human-owned secret so label events and agent mentions are not authored by the
built-in workflow token. Record the same token's expiry date in
`owner_surface.dispatch_token_expires_var` so `doctor --github` can report the
rotation countdown.

Branch protection should require the `code-mower/gate` commit status from **Any
source**, alongside normal CI, before autonomous merge is trusted. Do not select
the GitHub Actions source for `code-mower/gate` in the branch-protection UI:
that binds protection to the workflow job's check-run instead of the commit
status.

Inspect the existing status-check protection first:

```bash
gh api repos/OWNER/REPO/branches/main/protection/required_status_checks
```

For `code-mower/gate`, a correct Any-source binding shows
`"app_id": null` in the `checks[]` entry. `"app_id": 15368` means the context is
bound to GitHub Actions and should be changed before relying on unattended
merge.

Then update the same endpoint with all existing required contexts plus
`code-mower/gate`:

```bash
gh api -X PATCH repos/OWNER/REPO/branches/main/protection/required_status_checks \
  -f strict=true \
  -F contexts[]=EXISTING_REQUIRED_CONTEXT \
  -F contexts[]=code-mower/gate
```

Repeat `-F contexts[]=...` for every existing required context returned by the
inspection call, and confirm the follow-up API response shows `app_id: null`
for `code-mower/gate`.

If the repository does not allow auto-merge yet, enable it explicitly:

```bash
gh api -X PATCH repos/OWNER/REPO -f allow_auto_merge=true
```

## Stale Merge-Authority Labels

Terminal merge-authority labels are head-bound. A label such as
`devin-audit-done` must not satisfy the merge bar after a PR receives new
commits unless the latest trusted terminal reviewer comment is explicitly tied
to the current head SHA.

Install the generated stale-clear workflow, or call the command directly from a
`pull_request_target.synchronize` workflow:

```bash
tools/code_mower clear-stale --lane devin --repo owner/repo --pr 123 --json
```

For paid or hosted lanes, use `--dispatch-workflow` and `--dispatch-input` when
the stale requeue needs to fire a bridge workflow immediately instead of relying
on a newly-added label to trigger another workflow.

The Devin provider template includes this stale-label hygiene by default when
you deliberately enable that hosted merge-authority lane. It remains opt-in:
the default first-user profiles do not enable Devin or other paid hosted lanes.

`code-mower doctor --preflight` checks both sides of this setup for installed
repo configs: the lane must declare `review_hygiene.workflow` and
`review_hygiene.token_env`, and the configured workflow file must exist in the
repo. That keeps merge-authority lanes from looking safe when the generated
stale-clear workflow was not committed.

## Provider-Unavailable Bypass

A promoted reviewer can fail for reasons that are not code findings: expired
local CLI auth, provider rate limits, malformed provider output, or unavailable
hosted service state. Treat those as setup incidents.

If repository policy allows a bypass, the maintainer should:

- prove the provider failure with a harmless sanity command or provider status;
- leave a PR comment that names the provider, head SHA, failure class, and other
  clean merge evidence;
- announce any admin merge or branch-protection bypass before using it, with
  the head SHA, reason, and evidence that the remaining merge bar is clean;
- remove the stale `needs-*-audit` label only after the bypass is documented;
- avoid counting the failed provider run as PASS evidence; and
- repair provider auth/setup before relying on that lane again.

Do not make this automatic in v1.0. A provider-unavailable bypass is an explicit
human or delegated-maintainer action.

Generated dispatchers also treat GitHub API rate limits as infrastructure
pressure: they validate the reset timestamp before calling `date`, wait briefly
when the reset fits the job timeout, and otherwise exit 0 with a notice so a
later event can retry.

## Fork Pull Requests

Fork pull requests are the sharpest security edge.

Safe defaults:

- do not run provider CLIs with secrets against untrusted fork code in GitHub
  Actions
- keep labeler workflows metadata-only
- run audit lanes locally or in trusted infrastructure
- treat comments from untrusted users as requests, not executable instructions
- avoid workflows that checkout `github.event.pull_request.head.sha` while also
  using write tokens from the base repository

## GitHub Doctor Checks

`code-mower doctor --github` should help users answer:

- Can `gh` read the configured repositories?
- Are the repositories public or private?
- Does the current token appear write-capable or read-only?
- Are GitHub Actions permissions inspectable?
- Are recent Actions failures actually billing/spending-limit blocks?
- Are recent Actions runs dominated by optional metadata/reviewer labelers?
- Is default-branch protection inspectable?
- Do merge-authority profiles require `code-mower/gate` in branch protection?
- Does the repository allow auto-merge for the generated gate workflow?
- Are private repositories being used with hosted/SaaS lanes?
- Which provider apps or token fallbacks are likely needed?

Warnings are setup guidance, not automatic failures. Use `--strict` when a CI
or bootstrap job should fail on warnings.

## Non-GitHub Systems

v1.0 is GitHub-first.

GitLab is the best next source-control target because merge requests,
discussions, labels, approval rules, pipelines, and API concepts map closely to
Code Mower lanes.

Bitbucket is a later target. It has pull requests, comments, and branch
restrictions, but the API and hosted reviewer ecosystem diverge more from the
current GitHub model.

Keep the benchmark data model source-control-neutral now: repository slug,
pull-request or merge-request id, head SHA, provider id, lane id, lens id, and
adjudicated outcomes.
