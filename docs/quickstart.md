# Code Mower Quickstart

This is the reference for Code Mower's first-user command surface. For the two
guided starts, use [Try Code Mower In 10 Minutes](try-in-10-minutes.md) for the
reviewer gate or [Build Loop In 30 Minutes](build-loop-in-30-minutes.md) for
builders plus orchestrator convention. Code Mower v0.6 is beta software; start
on one repository and keep all reviewer lanes manual until the output is useful
on your codebase.

To see the value loop before you touch a product repository, open the
[Demo Calibration Example](../examples/demo-calibration/README.md) and the
[First-User Demo Transcript](first-user-demo-transcript.md).

## 1. Install

Code Mower requires Python 3.11 or newer. Python 3.12 is recommended.

```bash
python3.12 --version
pipx install --python python3.12 code-mower==0.6.0b1
code-mower --version
```

`0.6.0b1` is a beta release. If you want the newest prerelease instead of this
exact verified beta, use:

```bash
pipx install --python python3.12 --pip-args="--pre" code-mower
```

If you are working from a source checkout instead of an installed package, use
the checked-in development wrapper so old system Python shims cannot enter the
release path:

```bash
scripts/dev-python
scripts/dev-python -m venv .venv
.venv/bin/python -m pip install -e .
```

Avoid hand-wiring source import paths to run the CLI; use the editable venv so
your checkout follows the same package-first path as the public install.

If `code-mower` is not on your path:

```bash
pipx ensurepath
exec "$SHELL" -l
```

For the reference multi-agent adoption loop, use Claude Code as the
orchestrator convention, Claude Code/Codex/Cursor as builders, Claude
Code/Codex as reviewer lanes, and Gitar plus Antigravity as informational
reviewer signal until local calibration supports promotion.

## 2. Authenticate GitHub

Code Mower v0.6 is GitHub-first.

```bash
gh auth login -h github.com -s repo,workflow,read:org
gh auth status
```

For a private repository, verify access before continuing:

```bash
gh repo view OWNER/REPO
```

## 3. Install The Default Local Reviewers

The recommended first reviewers are local Codex and Claude CLI audits.

Verify Codex:

```bash
codex --version
codex "Reply with exactly: ok"
```

Verify Claude:

```bash
claude auth status
claude -p "Reply with exactly: ok" --output-format json
```

The prompt smoke is the real readiness check. If `claude auth status` says
logged in but the prompt returns an auth error, follow
[Troubleshooting](troubleshooting.md#claude-code-reports-logged-in-but-audits-fail).

## 4. Create The Automation Tokens

Create these before you run Easy Mode on a real repository:

- `DISPATCH_TOKEN`: a human-owned fine-grained PAT stored as a repository
  Actions secret.
- `DISPATCH_TOKEN_EXPIRES_AT`: a repository Actions variable with the PAT
  expiry date in `YYYY-MM-DD` format.

Use a human account or explicitly delegated machine user. The default
`DISPATCH_TOKEN` needs:

- Contents: read
- Issues: read/write
- Pull requests: read/write

Set the secret and expiry metadata:

```bash
gh secret set DISPATCH_TOKEN --repo OWNER/REPO
gh variable set DISPATCH_TOKEN_EXPIRES_AT --repo OWNER/REPO --body YYYY-MM-DD
```

Why this is required: comments, labels, and mentions posted by the built-in
`GITHUB_TOKEN` are bot-authored. GitHub does not trigger downstream workflows
from those events, and tools such as Cursor ignore bot-authored `@cursor`
mentions. Code Mower uses the human-owned token for generated agent PR labels,
fix-round comments, and audit rearming so the automation actually fires.

Keep these per-lane names only as compatibility fallbacks when an existing beta
install already uses separate credentials:

- `CODEX_AUDIT_LABEL_TOKEN`
- `CLAUDE_AUDIT_LABEL_TOKEN`

The generated templates prefer `DISPATCH_TOKEN`, then fall back to per-lane
tokens, then to `GITHUB_TOKEN` only where GitHub token inertness is acceptable.

## Optional: Create Planning Context

You do not need this for the first audit. Use it when a change needs product
requirements, architecture constraints, or multiple-agent plan critique before
implementation:

```bash
code-mower project-context init --project-name "My Product"
code-mower context add --external ~/Downloads/product-requirements.md
code-mower plan from-github-issue owner/repo#123 --post \
  --output .code-mower/work-orders/feature-plan.md
code-mower work-order draft \
  --issue-plan .code-mower/work-orders/feature-plan.md \
  --output .code-mower/work-orders/feature.md
code-mower work-order attach-delivery \
  .code-mower/work-orders/feature.cloud-event.json \
  --pr owner/repo#124 \
  --from-github
code-mower cloud export \
  --event work_order=.code-mower/work-orders/feature.cloud-event.json \
  --output-dir .code-mower/cloud-benchmark-bundle \
  --repo-slug owner/repo
```

The GitHub issue remains the source of truth, while the local plan file is a
derived working copy. The work-order command writes a metadata-only
`feature.cloud-event.json` sidecar so CodeMower.com can tie future
builder/reviewer evidence back to the issue without receiving the issue body,
source code, diffs, or transcripts. `attach-delivery` adds PR, reviewer-check,
and merge identifiers only. `work-order draft` rejects pointer-only stubs and
large accidental batches by default. For private/offline drafting:

```bash
code-mower plan from-issue --title "Feature" --body-file issue-body.md \
  --output .code-mower/work-orders/feature-plan.md
code-mower work-order draft \
  --issue-plan .code-mower/work-orders/feature-plan.md \
  --output .code-mower/work-orders/feature.md
```

Details: [Planning And Work Orders](planning-work-orders.md).

Keep SaaS reviewers such as Gitar, Cursor BugBot, CodeRabbit, Qodo, Greptile,
and Devin informational/manual until your own calibration data supports
promotion. Gitar is informational and quota-bound. Automatic processing can
pause until the provider quota resets, and a manual `Gitar review` comment may
be needed to refresh its signal. It is never required for the default Code
Mower gate.

## 5. Run Easy Mode

From a clean checkout of the repository you want to pilot:

```bash
code-mower init --easy
code-mower init --easy --apply --output-dir .code-mower.generated
code-mower next-steps --profile recommended --repo OWNER/REPO
```

`init --easy` is non-mutating by default. `--apply` writes a generated tree for
review; it does not edit live workflows or trigger paid providers. The
generated tree includes owner-surface templates for a configurable
`needs-owner` escalation label, an `owner-sitting` physical-step convention,
and a weekly pinned-issue status digest.

For direct local audit wrapper runs, pass a GitHub posting token with
`GITHUB_TOKEN` or `--read-token-from-stdin`, and pass repository paths as
`--repo-paths OWNER/REPO:/absolute/path/to/pr-head-checkout`. The path must be
a separate PR-head checkout, not the Code Mower support checkout or the current
working directory. See [Local Audit Runner](local-audit-runner.md).

To extend the same lane/label/workflow setup to a sibling repository, run the
same init command from that checkout and add the target repo slug to the
rendered plan:

```bash
code-mower init ../control-repo/code-mower.yml \
  --add-repo OWNER/SIBLING_REPO \
  --profile recommended \
  --apply \
  --repo OWNER/SIBLING_REPO \
  --output-dir .code-mower.generated
code-mower doctor ../control-repo/code-mower.yml --preflight --json
```

Apply mode creates missing labels in `--repo`, or in the current checkout's
GitHub repository when `--repo` is omitted. `doctor` reports whether the config
is single-repo or multi-repo so CI-only sibling repos are easy to spot before
they drift from the merge gate. For a
permanent rollout, add the sibling slug to `repositories:` in the control
config after reviewing the generated plan.

## 6. Configure Branch Protection And Auto-Merge

If the selected profile has merge-authority lanes, Code Mower publishes the
`code-mower/gate` commit status and asks GitHub to enable auto-merge only after
that status is green. Two GitHub settings must match that behavior.

For unattended merges, add a merge-capable machine-user or GitHub App token as
`CODE_MOWER_GATE_AUTOMERGE_TOKEN`. If this secret is absent, the generated gate
falls back to `DISPATCH_TOKEN` and then to the default Actions token, which may
publish the green status but fail to enable auto-merge.

If you enable local CLI audit lanes, also set
`CODE_MOWER_LOCAL_AUDIT_RUNNER_ENABLED=true` only after the self-hosted runner
is online and authenticated. Leaving it unset keeps those jobs skipped instead
of queued.

First, enable repository auto-merge:

```bash
gh api -X PATCH repos/OWNER/REPO -f allow_auto_merge=true
```

Then protect the default branch and require the Code Mower gate status from
Any source:

1. Open GitHub repository settings.
2. Go to Branches, then edit the default branch protection rule.
3. Enable "Require status checks to pass before merging".
4. Add `code-mower/gate` to the required checks.
5. Confirm the source shown next to `code-mower/gate` is **Any source**.

Do not select GitHub Actions as the source for `code-mower/gate`. That binds
branch protection to the Actions check-run app (`app_id: 15368`) instead of the
Code Mower commit status, which can leave every check green while auto-merge
never happens. The API shape for the correct binding is:

```bash
gh api repos/OWNER/REPO/branches/main/protection/required_status_checks
```

For `code-mower/gate`, the `checks[]` entry must show `"app_id": null`. If it
shows `"app_id": 15368`, remove and re-add the required check from Any source.

Now run the preflight:

```bash
code-mower doctor --preflight --json
```

`doctor --preflight` is the recommended early-adopter preset for GitHub auth,
Python/runtime checks, provider CLI probes, private-repo caveats, Actions cost
diagnostics, branch-protection source, repository auto-merge, human automation
token metadata, and optional cloud-token setup. It is equivalent to the
versioned `doctor --v05` preset. Use `--strict` only when warnings should fail
a bootstrap job. For auth-specific doctor failures, see
[Troubleshooting](troubleshooting.md).

When setup is visible enough to start work, use one command to check live lane
state:

```bash
code-mower lanes status --repo OWNER/REPO
```

It reports open Code Mower PR lanes, audit/gate labels, major checks, recent
Code Mower workflows, local AgentTrail boards when present, and the next
operator action. It does not upload data or require AgentTrail to be running.

## 7. Rehearse The Package Install Path

This proves Code Mower can be installed fresh and run the starter workflow in a
toy repository. It now also leaves the first-user evidence artifacts behind: a
starter calibration plan, reviewer metrics, lane policy, value report, cloud
export bundle, upload dry run, and CodeMower.com dogfood dry run.

```bash
code-mower migration package-install-rehearsal \
  --package-spec code-mower==0.6.0b1 \
  --python "$(command -v python3.12)" \
  --json
```

Use `--repo-path /path/to/repo` to validate the installed Code Mower CLI
against a real repository; if `tools/code_mower` exists, the rehearsal also
runs wrapper parity for mirror-removal, otherwise it detects and dry-runs the
repo's native checks.

See [First-User Install Rehearsal](first-user-install-rehearsal.md) for the
release-gate version of this command and the expected output artifacts.

## 8. Generate A Local Value Report

The starter corpus is only a command-path proof. It proves that the commands
run and the report path works. It does not prove that a reviewer should gate
merges. Replace it with your own known-clean and known-blocked PRs before
making lane promotion decisions, and use the
[Lane Promotion Policy](lane-promotion-policy.md) before giving any lane merge
authority.

```bash
code-mower calibration value-report templates/calibration-corpus.json \
  --output .code-mower/reviewer-value-report.md \
  --html-output .code-mower/reviewer-value-report.html
```

The Markdown report is easy to commit or paste into a PR. The optional HTML
report is a local, self-contained dashboard-style view for sharing with a team
before opting into CodeMower.com uploads.

## 9. Optional Cloud Export

Local-first is the default. To prepare an inspectable bundle for optional
cloud sharing:

```bash
code-mower cloud export \
  --report value-report=.code-mower/reviewer-value-report.md \
  --output-dir .code-mower/cloud-benchmark-bundle \
  --anonymous \
  --json
```

Preview an upload without sending data:

```bash
code-mower cloud upload .code-mower/cloud-benchmark-bundle --dry-run --json
```

Check endpoint, token, and bundle readiness:

```bash
code-mower cloud doctor .code-mower/cloud-benchmark-bundle --json
```

When you want to verify the hosted service too:

```bash
code-mower cloud doctor .code-mower/cloud-benchmark-bundle --probe-service --json
```

`--probe-service` calls the endpoint's health route, includes the dashboard URL,
and returns token-safe next-step commands.

Nothing uploads unless you pass `--yes`.

You do not need Supabase, Vercel, OAuth-app, DNS, service-role, database, or
hosted-secret setup to use Code Mower or opt into cloud sharing. Those are
CodeMower.com operator responsibilities.

To upload to Code Mower Cloud, create a team ingest token from:

```text
https://codemower.com/login
https://codemower.com/dashboard
```

Then configure the token locally:

```bash
code-mower cloud setup \
  --token-stdin \
  --team-id "YOUR_TEAM_SLUG" \
  --install-id "your-laptop" \
  --out ~/.config/code-mower/tokens/your-laptop.env

source ~/.config/code-mower/tokens/your-laptop.env
code-mower cloud upload .code-mower/cloud-benchmark-bundle --yes --json
```

`cloud setup` writes a private `0600` env file and prints only a token prefix.
Paste the dashboard token when prompted by stdin, then press Ctrl-D. Use
`--force` only when intentionally replacing an existing token file.
Operator-issued tokens remain a fallback for teams that cannot use the
self-service dashboard yet.

For routine dogfooding after the one-off bundle preview, prefer:

```bash
code-mower cloud dogfood --json
code-mower cloud dogfood --yes --json
```

`cloud dogfood` auto-detects the current GitHub repo when possible, includes
common shareable reports if they exist, adds a metadata-only `dogfood_upload`
event, and is the easiest way to make the CodeMower.com dashboard useful over
time. Reviewer-run uploads automatically include `.code-mower/reviewer-spend.json`
when present, so routine dashboard refreshes do not need a separate spend-only
command.

To catch up recent GitHub Actions history after the token is configured:

```bash
code-mower cloud catch-up --repo-slug OWNER/REPO --limit 50 --json
code-mower cloud catch-up --repo-slug OWNER/REPO --limit 50 --yes --json
```

`cloud catch-up` stores only sanitized workflow metadata and `workflow_run`
events. It omits branch names and commit SHAs by default; add
`--include-git-ref` only after reviewing that privacy tradeoff.

## First Pilot Definition Of Done

One repository is ready for broader Code Mower use when:

- `doctor --preflight` has no unexplained failures.
- Codex and Claude can both run local audits.
- A small PR can be reviewed manually without recurring workflows.
- Private-repo GitHub Actions cost is understood.
- Cloud export output has been inspected before any upload.
- If cloud upload is enabled, the team token was created intentionally and is
  stored outside source control.
- If unattended merges are enabled, `code-mower/gate` is required from Any
  source, repository auto-merge is enabled, and a dedicated
  `CODE_MOWER_GATE_AUTOMERGE_TOKEN` or GitHub App token can request auto-merge.

For a concise map of which commands are launch-safe versus advanced/operator
surfaces, see [Launch Command Surface](launch-command-surface.md).

## What To Read Next

Read in this order when moving from a first reviewer gate to a real build loop:

- [Try Code Mower In 10 Minutes](try-in-10-minutes.md)
- [Build Loop In 30 Minutes](build-loop-in-30-minutes.md)
- [Quickstart Reference](quickstart.md)
- [Planning And Work Orders](planning-work-orders.md)
- [Builder Providers: Grok And Cursor](builders-grok-cursor.md)
- [Self-Hosted Mac Runner](self-hosted-mac-runner.md)
- [Build Loop Operations](build-loop.md)
- [Local Audit Runner](local-audit-runner.md)
- [Provider Matrix](provider-matrix.md)
- [GitHub Setup](github-setup.md)
