# Troubleshooting

Code Mower setup checks should prove that the configured tools can run the
same kind of work the reviewer lanes will ask them to do. A CLI status command
is useful, but it is not enough for merge-gating lanes.

## Claude Code Reports Logged In But Audits Fail

`claude auth status` can report `loggedIn: true` while real non-interactive
requests still fail with `401 Invalid authentication credentials`. Code Mower
therefore treats the real prompt smoke as the useful signal:

```bash
claude auth status
claude -p "Reply with exactly: ok" --output-format json
```

If prompt auth only fails inside a long-lived parent process such as Codex,
first try the non-destructive bounce helper:

```bash
code-mower claude-bounce --json
code-mower claude-bounce --write-env ~/.config/code-mower/claude-clean-env.sh
source ~/.config/code-mower/claude-clean-env.sh
```

The bounce helper runs the same kind of real Claude prompt smoke twice: once
with the inherited environment and once with known stale Claude/Anthropic auth
override variables removed from the child process. It does not delete Claude
credentials, modify keychain state, or log raw provider output. If `clean_env`
passes while `inherited_env` fails, restart the parent app or source the
generated unset snippet before retrying.

`code-mower doctor --preflight` runs the provider-configured Claude smoke probe
with a bounded model and budget cap. If that probe reports a provider
authentication failure, refresh Claude Code auth and rerun the smoke:

```bash
cp ~/.claude/.credentials.json ~/.claude/.credentials.json.bak.$(date +%Y%m%d-%H%M%S) 2>/dev/null || true
rm -f ~/.claude/.credentials.json

security delete-generic-password -s "claude-code" 2>/dev/null || true
security delete-generic-password -s "Claude Code" 2>/dev/null || true

claude
claude -p "Reply with exactly: ok" --output-format json
```

If the prompt still fails, treat the Claude lane as unavailable until local auth
is repaired. For automation, prefer a provider/API-key credential path instead
of depending on interactive Claude.ai OAuth state.

## Doctor Output Is Safe To Share

Doctor redacts raw provider stdout/stderr. For provider smoke probes it reports
only shape and status metadata such as return code, JSON parse status, expected
sentinel match, output line count, and content-free auth failure flags. Provider
configs can declare `doctor_probe_auth_status_fields` to identify structured
status-code fields such as `api_error_status`, `status_code`, or `http_status`.
Doctor only reports sanitized auth status codes (`401` or `403`), never raw
provider-supplied status text.

## Python Is Too Old

Use the checked-in developer wrapper instead of bare `python3`:

```bash
scripts/dev-python --version
scripts/dev-python -m unittest discover -s tests
```

The wrapper resolves Python 3.12+ and refuses old system Python shims.

## GitHub Auth Or Private Repo Checks Fail

Verify the GitHub CLI independently:

```bash
gh auth status
gh repo view OWNER/REPO --json nameWithOwner,visibility
```

Private repositories need tokens and app installations that can read pull
requests, comments, checks, and Actions metadata. If `doctor --github` reports
recent Actions billing blocks or expensive labeler workflows, review
[docs/github-setup.md](github-setup.md) before enabling hosted reviewer lanes.
