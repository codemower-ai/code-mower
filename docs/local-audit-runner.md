# Local Audit Runner

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
gh auth status
codex --version
claude auth status
claude -p "Reply with exactly: ok" --output-format json
```

Set `DISPATCH_TOKEN` as a human-owned PAT or GitHub App posting-token secret,
and set `DISPATCH_TOKEN_EXPIRES_AT` as a repository variable with the expiry
date in `YYYY-MM-DD` format. Runner jobs can post with `GITHUB_TOKEN`, but
GitHub does not trigger `issue_comment` labeler workflows for comments created
by the built-in token, so the labels will not flip without that posting token.

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
