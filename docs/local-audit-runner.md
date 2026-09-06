# Local Audit Runner

For the full macOS service setup, use
[Self-Hosted Mac Runner](self-hosted-mac-runner.md).

Use `./run.sh` first from the macOS account that owns `gh`, Codex, and Claude
logins. Install the GitHub runner as a service only after the generated local
audit workflow passes the same smoke checks.

For service mode, set `USER`, `LOGNAME`, `SHELL`, and `LANG` in the runner
`.env`, then fully recycle the listener after edits. `svc.sh stop/start` may
leave an older `Runner.Listener` process alive with the previous environment.

Check `~/Library/LaunchAgents/actions.runner.*.plist` after `svc.sh install`.
If it contains `SessionCreate=true`, remove that key and unload/reload the
LaunchAgent or recycle the listener. That launchd setting creates a new security
session without login-keychain access, so Claude Code OAuth can look logged in
interactively while runner jobs return `Not logged in`.

Verify from a runner job, not only from an interactive terminal:

```bash
gh auth status >/dev/null 2>&1 && echo "gh auth ok" || { echo "gh auth NOT ready"; false; }
codex --version
claude auth status >/dev/null 2>&1 && echo "claude auth ok" || { echo "claude auth NOT ready"; false; }
claude -p "Reply with exactly: ok" --output-format json
devin auth status >/dev/null 2>&1 && echo "devin auth ok" || { echo "devin auth NOT ready"; false; }
```

Set `DISPATCH_TOKEN` as a human-owned PAT or GitHub App posting-token secret,
and set `DISPATCH_TOKEN_EXPIRES_AT` as a repository variable with the expiry
date in `YYYY-MM-DD` format, or `never` when the token has no expiration date.
Runner jobs can post with `GITHUB_TOKEN`, but GitHub does not trigger
`issue_comment` labeler workflows for comments created by the built-in token,
so the labels will not flip without that posting token.

## Wrapper Contract

The Codex and Claude audit wrappers need a GitHub posting token and a separate
PR-head checkout.

Token input must use one of these forms:

- `GITHUB_TOKEN` in the process environment.
- `--read-token-from-stdin` with the token piped as the first stdin line.

The generated self-hosted workflow uses `--read-token-from-stdin` so the token
does not appear in the Python process's initial environment. Direct local runs
may use `GITHUB_TOKEN` when that exposure is acceptable for the operator's
machine.

During package-install adoption, `tools/run_codex_audit_pr.sh`,
`tools/run_claude_audit_pr.sh`, and `tools/run_devin_cli_audit_pr.sh` use the
installed `code-mower` on `PATH` while
`tools/code_mower_standalone_pin.env` still has placeholder values. Configure
the standalone pin file, or set `CODE_MOWER_USE_STANDALONE=1`, when you are
ready to shadow a reviewed Code Mower source ref through `tools/code_mower`.

Repository paths must use this format:

```text
OWNER/REPO:/absolute/path/to/pr-head-checkout
```

Pass it with `--repo-paths` or with the lane-specific env var:

```bash
tools/run_codex_audit_pr.sh \
  --repo OWNER/REPO \
  --pr 123 \
  --repo-paths OWNER/REPO:/absolute/path/to/pr-head-checkout

tools/run_claude_audit_pr.sh \
  --repo OWNER/REPO \
  --pr 123 \
  --repo-paths OWNER/REPO:/absolute/path/to/pr-head-checkout

tools/run_devin_cli_audit_pr.sh \
  --repo OWNER/REPO \
  --pr 123 \
  --repo-paths OWNER/REPO:/absolute/path/to/pr-head-checkout
```

The Devin CLI lane uses the `needs-devin-cli-audit` label, runs with
`devin --sandbox --permission-mode auto` (source-edit tools are not approved
for this reviewer), and never passes `--export`, `--continue`, or `--resume`.
Its stdout and stderr are streamed under independent byte bounds against a
wall-clock deadline; a timeout or an overflow on either stream terminates the
whole spawned process group and fails closed to `needs-devin-cli-audit` with a
metadata-only reason, never raw provider output.

The lane has no `--allow-dirty` escape hatch (passing it is rejected by the
argument parser). The checkout must be clean at the exact PR head before the
provider runs, and clean again afterwards, so a verdict is only ever produced
for the committed tree it claims to have reviewed. Commit or stash local
changes before invoking it.

The path must point at an existing checkout of the pull request head. It must
not be the Code Mower support checkout or the wrapper's current working
directory. This separation keeps product PR code out of the support checkout
and lets the wrapper verify the PR head SHA before posting a verdict.

Wrapper and doctor errors for missing tokens, malformed `--repo-paths`, relative
paths, missing directories, or same-directory checkouts point back to this page.

Generated local-audit workflows run one matrix job per configured lane and
cancel older runs for the same workflow, PR, head SHA, and lane. A queued or
in-progress required lane keeps `code-mower/gate` pending until the current-head
audit finishes; the generated gate re-evaluates on local-audit workflow
completion so that pending status can settle after the terminal verdict lands.
Matrix lanes whose `needs-*-audit` label is absent exit without uploading audit
metadata, so optional lanes do not duplicate cached reviewer-run or dogfood
events.

## Budgets and diff limits

`code-mower.yml` can set local audit defaults under `audit`:

```yaml
audit:
  budget_usd: ""
  max_diff_bytes: "180000"
  max_diff_hard_limit_bytes: "1500000"
```

Leave `audit.budget_usd` blank or omit it to use the size-aware default. Claude
audit starts at $2, adds $1 for each 150 KB above `audit.max_diff_bytes`, and
caps the default at $10. Set `audit.budget_usd` only when you want a fixed
provider budget instead of that scaling.

`audit.max_diff_bytes` is the normal target. Complete diffs larger than that
target may still be included when they fit under
`audit.max_diff_hard_limit_bytes`. The generated local-audit workflow passes
these values to both wrappers as:

- `CLAUDE_AUDIT_MAX_BUDGET_USD`
- `CLAUDE_AUDIT_MAX_DIFF_BYTES`
- `CLAUDE_AUDIT_MAX_DIFF_HARD_LIMIT_BYTES`
- `CODEX_AUDIT_MAX_BUDGET_USD`
- `CODEX_AUDIT_MAX_DIFF_BYTES`
- `CODEX_AUDIT_MAX_DIFF_HARD_LIMIT_BYTES`

If Claude audit must truncate a diff at the hard limit, it posts an UNKNOWN
requeue with the truncation reason in the verdict header instead of posting a
blocking finding whose only content is that the diff was truncated. Raise
`audit.max_diff_hard_limit_bytes` for repositories whose normal PRs exceed the
hard limit, then regenerate the local-audit workflow.

`code-mower doctor` reports the effective local audit limits. With
`code-mower doctor --github`, it also samples recent PR diff sizes and warns
when the median sampled diff is above the configured hard limit.

After regenerating `.github/workflows/local-cli-audit.yml`, run `actionlint` on
the generated workflow. If GitHub reports a failed workflow run with no jobs,
treat it as workflow syntax or context validation failure before debugging the
self-hosted runner.
