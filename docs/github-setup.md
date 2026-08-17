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
audit labels, local audit runs, or the self-hosted runner stall.
For fork PRs where GitHub grants a read-only token, owner notify emits a
workflow warning instead of failing the run.
The generated merge gate treats `owner_surface.gate_override_label` as an
owner-only escape hatch: the label succeeds the gate only when the configured
owner applied it; a non-owner-applied override fails the gate. Set
`gate_override_label` to an empty string to disable the override path entirely.

Configure the owner surface in `code-mower.yml` before enabling the weekly
schedule:

```yaml
owner_surface:
  owner_login: YOUR_GITHUB_LOGIN
  needs_owner_label: needs-owner
  gate_override_label: "gate:override"
  status_issue: "123"
  weekly_cron: "0 14 * * 1"
  gate_health_cron: "*/15 * * * *"
  gate_health_max_wait_minutes: "30"
  local_audit_runner_label: code-mower-audit
  ready_label: "tier:R"
```

Create and pin the status issue with GitHub CLI:

```bash
gh issue create --title "Code Mower status" --label code-mower --body "Weekly Code Mower status will appear here."
gh issue pin 123
```

The generated digest reads GitHub metadata only: labels, titles, PR state,
assignees, timestamps, and optional local Code Mower spend/value files. It does
not read source, diffs, transcripts, or issue bodies.

## Multi-Repo Rollout

When one Code Mower config controls several sibling repositories, keep the
lanes and labels in the control config and render each sibling repo from that
same file:

```bash
code-mower init ../control-repo/code-mower.yml \
  --add-repo OWNER/SIBLING_REPO \
  --profile recommended \
  --apply \
  --output-dir .code-mower.generated
code-mower doctor ../control-repo/code-mower.yml --preflight --json
```

`--add-repo` does not edit `code-mower.yml`; it adds a reviewable target to the
init manifest so the generated labeler, clear-stale, labels, and support files
can be copied into the sibling repository. After review, add the sibling slug
to `repositories:` in the control config. `doctor` reports the committed
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

Code Mower lanes therefore support explicit token fallbacks:

- `CODEX_AUDIT_LABEL_TOKEN`
- `CLAUDE_AUDIT_LABEL_TOKEN`
- `GITAR_AUDIT_LABEL_TOKEN`
- `GREPTILE_AUDIT_LABEL_TOKEN`
- `QODO_AUDIT_LABEL_TOKEN`
- `CURSOR_BUGBOT_AUDIT_LABEL_TOKEN`
- `DEVIN_AUDIT_LABEL_TOKEN`
- lane-specific local or research tokens when enabled

Use fine-grained tokens with the smallest useful permissions. A common labeler
fallback needs:

- Issues: read/write
- Pull requests: write for label mutation on PR-backed issues
- Contents: read only when a lane must fetch files through GitHub

Generated labeler, hosted-requeue, clear-stale, and audit-label-cleanup
workflows grant this permission to `GITHUB_TOKEN`; fine-grained PAT secrets are
optional fallbacks for repositories that intentionally use separate
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
GitHub Actions. The generated labeler and gate verify that marker against a
trusted `local-cli-audit.yml` run for the same PR and head before accepting the
terminal trailer. If GitHub omits the run's `pull_requests` array, Code Mower
resolves the run head SHA through the commit-to-PRs API; without that match,
the shared-bot comment is ignored. Use lane-specific posting tokens or remove
`github-actions[bot]` from the lane authors when a repository wants a stricter
separation between Actions jobs and merge-gating audit verdicts.

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
   local CLI and keychain auth in non-obvious ways.
5. Ensure `gh`, `git`, `python3`, `codex`, and `claude` are on PATH. If they
   live outside the default Homebrew/system paths, update
   `CODE_MOWER_LOCAL_AUDIT_PATH` in the generated workflow.
6. Verify local auth from that same account: `gh auth status`,
   `codex --version`, `claude auth status`, and
   `claude -p "Reply with exactly: ok" --output-format json`.
7. Add optional posting-token secrets `CODEX_AUDIT_LABEL_TOKEN` and
   `CLAUDE_AUDIT_LABEL_TOKEN`; the workflow falls back to `GITHUB_TOKEN` when
   those secrets are absent.

The generated workflow grants `pull-requests: write`, uses job-level
concurrency keyed by PR and head SHA without `cancel-in-progress`, and wipes
the `pr-head` workspace before every checkout so one run cannot inherit another
run's worktree. After checkout, it re-reads the PR head SHA and exits cleanly
without running or uploading audits when a newer commit has superseded the
queued run. Use the runner workflow as the dispatch mechanism for local lanes:
it invokes `tools/run_codex_audit_pr.sh` and
`tools/run_claude_audit_pr.sh`, which in turn run `codex exec` or `claude -p`
from the authenticated macOS account. Do not rely on `@codex` issue mentions
to dispatch local audit lanes; those mentions are not a dependable Actions
trigger.
Generated clear-stale workflows include the lane id in their concurrency group
so Codex and Claude stale-label cleanup cannot cancel each other on the same PR
push.

If `CODE_MOWER_CLOUD_TOKEN` is configured, the generated workflow also uploads
metadata-only reviewer evidence after every audit attempt. It sends saved
verdict artifacts with `code-mower cloud reviewer-runs`, spend rows captured in
a runner-temp `reviewer-spend.json` with `cloud dogfood --spend`, and trusted
default-branch work-order `*.cloud-event.json` sidecars. The upload step runs
with `if: always()` for non-superseded audit attempts, skips successfully when
the token is absent, and must not block merge authority if the cloud service is
unavailable.

The audit wrappers verify the GitHub API head SHA first, then skip the
`pull/N/head` fetch when the `--repo-paths` checkout is already at that SHA.
That makes the generated `persist-credentials: false` PR-head checkout viable
for normal runner-dispatched audits. If a custom workflow intentionally passes
a checkout that is not at the PR head, keep credentials on that checkout or
fetch the PR head before invoking the wrapper.

macOS Keychain access is user-session sensitive. If the runner later runs as a
service or launch daemon, re-check provider CLI auth under that service account
and unlock/configure the login keychain before trusting unattended audits.

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
`code-mower/gate` commit status; and calls GitHub's
`enablePullRequestAutoMerge` when the status is green. A `*-done` or
`*-blocked` label counts only when the matching terminal audit comment carries
the same head SHA and comes from the lane's configured bot authors, so stale
labels and forged comments cannot win races against cleanup.
The generated product-support files include `tools/audit_labeler_lib.py`, which
the gate uses for GitHub Actions audit-comment attestation without uploading
source, diffs, or transcripts.
Hosted builder tokens usually cannot enable auto-merge themselves, so keep that
call in the repository gate workflow.

The recommended three-builder pattern is:

- Codex-built PRs carry `builder:codex` or are opened by a mapped Codex author,
  so Claude audit gates them.
- Claude-built PRs carry `builder:claude` or are opened by a mapped Claude
  author, so Codex audit gates them.
- Hosted-built PRs carry a third builder identity such as `builder:grok-bot`,
  so both Codex and Claude audit gates still apply.

Configure those identities in `code-mower.yml`:

```yaml
merge_authority_excludes_author: true
builder_identity:
  labels:
    builder:codex: codex
    builder:claude: claude
    builder:grok-bot: grok-bot
  authors:
    chatgpt-codex-connector[bot]: codex
    claude[bot]: claude
```

PR-body trailer mappings remain configuration-valid for non-gating provenance
experiments, but merge-authority author exclusion uses only labels and
authenticated PR authors until trailer sources can be trusted.

Branch protection should require `code-mower/gate` alongside normal CI before
autonomous merge is trusted. Inspect the existing status-check protection first:

```bash
gh api repos/OWNER/REPO/branches/main/protection/required_status_checks
```

Then update the same endpoint with all existing required contexts plus
`code-mower/gate`:

```bash
gh api -X PATCH repos/OWNER/REPO/branches/main/protection/required_status_checks \
  -f strict=true \
  -F contexts[]=EXISTING_REQUIRED_CONTEXT \
  -F contexts[]=code-mower/gate
```

Repeat `-F contexts[]=...` for every existing required context returned by the
inspection call.

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
- remove the stale `needs-*-audit` label only after the bypass is documented;
- avoid counting the failed provider run as PASS evidence; and
- repair provider auth/setup before relying on that lane again.

Do not make this automatic in v1.0. A provider-unavailable bypass is an explicit
human or delegated-maintainer action.

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
