# Code Mower Quickstart

This is the reference for Code Mower's first-user command surface. For the two
guided starts, use [Try Code Mower In 10 Minutes](try-in-10-minutes.md) for the
reviewer gate or [Build Loop In 30 Minutes](build-loop-in-30-minutes.md) for
builders plus orchestrator convention. Code Mower v0.9 is beta software; start
on one repository and keep all reviewer lanes manual until the output is useful
on your codebase.

To see the value loop before you touch a product repository, open the
[Demo Calibration Example](../examples/demo-calibration/README.md), the
[Board Demo Rehearsal](../examples/board-demo/README.md), and the
[First-User Demo Transcript](first-user-demo-transcript.md).

## 1. Install

Code Mower requires Python 3.12 or newer. For hosted agents, minimal Linux
boxes, and contributor checkouts, use the full
[Install And Bootstrap](install.md) matrix. The laptop path is:

```bash
python3.12 --version
export CODE_MOWER_PYTHON="$(command -v python3.12)"
pipx install --python "$CODE_MOWER_PYTHON" code-mower==0.9.0b1
code-mower --version
```

`0.9.0b1` is a beta release. If you want the newest prerelease instead of this
exact verified beta, use:

```bash
pipx install --python "$CODE_MOWER_PYTHON" --pip-args="--pre" code-mower
```

For hosted agents or CI boxes without pipx:

```bash
uv python install 3.12
uv tool install --python 3.12 code-mower==0.9.0b1
code-mower --version
```

For a Code Mower source checkout, use `scripts/dev-python` and the editable
venv path documented in [Install And Bootstrap](install.md#contributor-checkout).

For upgrades, do not assume the command on `PATH` changed. Run
`command -v code-mower` and `code-mower --version` before and after reinstall,
and follow [Cold Install Vs Upgrade](install.md#cold-install-vs-upgrade) when
moving between pipx and uv.

If `code-mower` is not on your path:

```bash
pipx ensurepath
exec "$SHELL" -l
```

For the reference multi-agent adoption loop, use Claude Code as the
orchestrator convention, Claude Code/Codex/Cursor as builders, Claude
Code/Codex as reviewer lanes, and Gitar plus Antigravity as informational
reviewer signal until local calibration supports promotion. On shared machines,
read [Multi-Agent Coexistence](install.md#multi-agent-coexistence) before
running multiple builders against the same repository.

## 2. Authenticate GitHub

Code Mower v0.9 is GitHub-first.

```bash
gh auth login -h github.com -s repo,workflow,read:org
gh auth status >/dev/null 2>&1 && echo "gh auth ok" || { echo "gh auth NOT ready"; false; }
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
codex login status >/dev/null 2>&1 && echo "codex auth ok" || { echo "codex auth NOT ready"; false; }
codex exec --skip-git-repo-check --sandbox read-only "Reply with exactly: ok"
```

For non-interactive runner or hosted-agent auth, load the API key from a secret
store and pipe it into Codex without printing it:

```bash
printf '%s\n' "$OPENAI_API_KEY" | codex login --with-api-key
codex login status >/dev/null 2>&1 && echo "codex auth ok" || { echo "codex auth NOT ready"; false; }
codex exec --skip-git-repo-check --sandbox read-only "Reply with exactly: ok"
```

Do not paste raw provider auth/status output into issues, pull requests, chats,
or logs. Use quiet probes like these or `code-mower doctor` so account and
credential-adjacent details stay local.

Verify Claude:

```bash
claude auth status >/dev/null 2>&1 && echo "claude auth ok" || { echo "claude auth NOT ready"; false; }
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
  expiry date in `YYYY-MM-DD` format, or `never` when the PAT has no
  expiration date.

Use a human account or explicitly delegated machine user. The default
`DISPATCH_TOKEN` needs:

- Contents: read
- Issues: read/write
- Pull requests: read/write

Set the secret and expiry metadata:

```bash
gh secret set DISPATCH_TOKEN --repo OWNER/REPO
gh variable set DISPATCH_TOKEN_EXPIRES_AT --repo OWNER/REPO --body YYYY-MM-DD
# or, for a non-expiring PAT:
gh variable set DISPATCH_TOKEN_EXPIRES_AT --repo OWNER/REPO --body never
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
It also includes `.code-mower.generated/code-mower.yml`; edit the repository
slug, owner login, decision authorities, status issue, and trusted audit-comment
authors before copying it to the repository root.
The generated `smoke-tests.sh` should run without leaving bytecode caches or
other setup noise in your first PR.

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

During the initial informational pilot, leave this section as a promotion todo:
do not require `code-mower/gate`, do not enable repository auto-merge, and merge
manually only after clean audit evidence for the current PR head. A
`doctor --adoption` failure for `allow_auto_merge` is expected in that posture;
promote the setting only after the reviewer lane meets
[Lane Promotion Policy](lane-promotion-policy.md).

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
code-mower doctor --adoption --repo OWNER/REPO --json
```

`doctor --adoption` is the recommended early-adopter preset for GitHub auth,
Python/runtime checks, provider CLI probes for machines that run local lanes,
private-repo caveats, Actions cost
diagnostics, branch-protection source, repository auto-merge, human automation
token metadata, optional cloud-token setup, and first-run setup gaps such as
starter config or missing owner/trusted-author posture. Use `--strict` only
when warnings should fail a bootstrap job. For auth-specific doctor failures, see
[Troubleshooting](troubleshooting.md).
If this machine observes or dispatches hosted builders but does not run local
Codex/Claude audits, use `--hosted-builders` or `--orchestrator-only` with
`doctor --adoption`; those postures keep GitHub, cloud, setup, and privacy
checks visible while marking local CLI probes skipped and treating local wrapper
env gaps as setup tasks for the machine that will execute those lanes.
When default adoption output shows local provider setup gaps on an observer
host, doctor includes the same posture commands as next-step hints in text and
JSON.

When setup is visible enough to start work, use one command to check live lane
state:

```bash
code-mower lanes status --repo OWNER/REPO
```

It reports open Code Mower PR lanes, audit/gate labels, major checks, recent
Code Mower workflows, local board/process hints when present, and the next
operator action. It does not upload data or require any external viewer.
Local cwd paths are redacted by default; pass `--show-local-paths` only when you
are debugging on your own machine.
When a PR needs the gate recomputed manually, the text and JSON output include a
copy-pasteable dispatch command with `pr_number` and the current `head_sha`.

For a browser view of the same local-first metadata, run:

```bash
code-mower board serve --repo OWNER/REPO
```

The board serves only on loopback by default. It is read-only, does not upload
data, and uses the same local-path redaction as `lanes status`. If the default
port is busy, it picks a nearby free loopback port and prints the URL to open;
an explicit `--port` fails with a friendly conflict instead. The printed URL is
local to that machine or VM unless you create your own tunnel. `lanes status`
discovers local Board listeners best-effort across common macOS and Linux tools;
if listener inventory is restricted, GitHub PR/check status still reports.
Visible Board timestamps render in the browser's local timezone and keep the
original UTC value in hover text for precise handoffs.
When you want the Recent Local History panel to fill while the board is open,
start it explicitly with:

```bash
code-mower board serve --repo OWNER/REPO --record-events
```

Live recording writes metadata-only snapshots to `.code-mower/board/events.jsonl`
at most once every 60 seconds by default. The Board also shows an owner queue,
and it summarizes reviewer verdict history and spend/latency from local board
events plus `.code-mower/reviewer-spend.json` when those files exist. Use
`code-mower board doctor --repo OWNER/REPO` to diagnose Board inputs, local
history, gate alerts, and optional agent cards without showing local paths by
default. Use `code-mower board reset --repo OWNER/REPO --yes` only when you want
to clear the local Board history file. Local
agents can publish opt-in status cards by writing metadata-only JSON files to
`.code-mower/board/agents/*.json`; the Board redacts local paths by default and
does not upload those cards.
For an explicit CodeMower.com Board mirror, use
`code-mower cloud board-snapshot --repo-slug OWNER/REPO --json` to inspect one
zero-report, metadata-only event. Add `--yes` only after reviewing the dry run.

## 7. Rehearse The Package Install Path

This proves Code Mower can be installed fresh and run the starter workflow in a
toy repository. It now also leaves the first-user evidence artifacts behind: a
starter calibration plan, reviewer metrics, lane policy, value report, cloud
export bundle, upload dry run, and CodeMower.com dogfood dry run.

```bash
code-mower migration package-install-rehearsal \
  --package-spec code-mower==0.9.0b1 \
  --allow-package-index \
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

code-mower cloud upload .code-mower/cloud-benchmark-bundle --yes --json
```

`cloud setup` writes a private `0600` env file and prints only a token prefix.
Paste the dashboard token when prompted by stdin, then press Ctrl-D. The command
also records the file as the current local cloud profile, so future cloud upload
commands can load it after a shell or app restart. If the machine has multiple
stored profiles and no current profile, pass `--install-id your-laptop`,
`--token-file ~/.config/code-mower/tokens/your-laptop.env`, or source the file
before rerunning. Use `--force` only when intentionally replacing an existing token file.
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

- `doctor --adoption --repo OWNER/REPO` has no unexplained failures.
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
- [Orchestrator Prompt Pack](orchestrator-prompt-pack.md)
- [Quickstart Reference](quickstart.md)
- [Planning And Work Orders](planning-work-orders.md)
- [Builder Providers: Grok And Cursor](builders-grok-cursor.md)
- [Self-Hosted Mac Runner](self-hosted-mac-runner.md)
- [Build Loop Operations](build-loop.md)
- [Local Audit Runner](local-audit-runner.md)
- [Provider Matrix](provider-matrix.md)
- [GitHub Setup](github-setup.md)
