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
- Pull requests: read
- Contents: read only when a lane must fetch files through GitHub

Do not store provider API keys in repository docs. Use environment variables,
GitHub secrets, or provider-specific local auth stores.

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
- prefer explicit labels, trusted comments, or manual `workflow_dispatch`
- add job-level `if:` guards to every `issue_comment` labeler before checkout
- require informational SaaS lanes to opt in with an existing lane label
- keep branch-protection merge gates limited to promoted structured audit lanes

The reference Devin bridge is event-driven plus manual dispatch only. The
Gitar, Qodo, and Cursor BugBot labelers are passive: they do not trigger the
hosted reviewer, and they skip unrelated issue comments before checking out
code.

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
