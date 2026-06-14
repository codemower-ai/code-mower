# Code Mower Quickstart

This guide gets one developer from install to a first local Code Mower setup
check. Code Mower is still alpha software; start on one repository and keep
all reviewer lanes manual until the output is useful on your codebase.

## 1. Install

Code Mower requires Python 3.11 or newer. Python 3.12 is recommended.

```bash
python3.12 --version
pipx install --python python3.12 "git+https://github.com/jeffhuber/code-mower.git@v0.5.0-alpha.1"
code-mower --version
```

If you are working from a source checkout instead of an installed package, use
the checked-in development wrapper so old system Python shims cannot enter the
release path:

```bash
scripts/dev-python
scripts/dev-python -m venv .venv
.venv/bin/python -m pip install -e .
```

If `code-mower` is not on your path:

```bash
pipx ensurepath
exec "$SHELL" -l
```

## 2. Authenticate GitHub

Code Mower v0.5 is GitHub-first.

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

Keep SaaS reviewers such as Gitar, Cursor BugBot, CodeRabbit, Qodo, Greptile,
and Devin informational/manual until your own calibration data supports
promotion.

## 4. Run Easy Mode

From a clean checkout of the repository you want to pilot:

```bash
code-mower init --easy
code-mower init --easy --apply --output-dir .code-mower.generated
code-mower doctor --v05 --json
code-mower next-steps --profile recommended --repo OWNER/REPO
```

`init --easy` is non-mutating by default. `--apply` writes a generated tree for
review; it does not edit live workflows or trigger paid providers.
`doctor --v05` is the recommended early-adopter preset for GitHub auth,
Python/runtime checks, provider CLI probes, private-repo caveats, Actions cost
diagnostics, and optional cloud-token setup. Use `--strict` only when warnings
should fail a bootstrap job.

## 5. Rehearse The Package Install Path

This proves Code Mower can be installed fresh and run the starter workflow.

```bash
code-mower migration package-install-rehearsal \
  --package-spec "git+https://github.com/jeffhuber/code-mower.git@v0.5.0-alpha.1" \
  --repo-path "$PWD" \
  --python "$(command -v python3.12)" \
  --json
```

## 6. Generate A Local Value Report

The starter corpus is only a command-path proof. Replace it with your own
known-clean and known-blocked PRs before making lane promotion decisions.

```bash
code-mower calibration value-report templates/calibration-corpus.json \
  --output .code-mower/reviewer-value-report.md
```

## 7. Optional Cloud Export

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

## First Pilot Definition Of Done

One repository is ready for broader Code Mower use when:

- `doctor --v05` has no unexplained failures.
- Codex and Claude can both run local audits.
- A small PR can be reviewed manually without recurring workflows.
- Private-repo GitHub Actions cost is understood.
- Cloud export output has been inspected before any upload.
- If cloud upload is enabled, the team token was created intentionally and is
  stored outside source control.
