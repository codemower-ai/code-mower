# Try Code Mower In 10 Minutes

This is the shortest v0.5 early-adopter path. It is local-first and safe to run
on one GitHub repository before you enable any recurring workflows or paid
reviewer lanes.

If you want to see the output shape before installing, read the
[Demo Calibration Example](../examples/demo-calibration/README.md) and
[First-User Demo Transcript](first-user-demo-transcript.md). The demo is
synthetic and contains no source, raw diffs, raw transcripts, auth output, or
private repository names.

## 1. Install

Code Mower requires Python 3.11 or newer. Python 3.12 is recommended.

```bash
python3.12 --version
pipx install --python python3.12 code-mower==0.5.0b28
code-mower --version
```

`0.5.0b28` is a beta release. To follow the newest beta line instead of
pinning this exact build:

```bash
pipx install --python python3.12 --pip-args="--pre" code-mower
```

## 2. Authenticate GitHub

```bash
gh auth login -h github.com -s repo,workflow,read:org
gh auth status
gh repo view OWNER/REPO
```

## 3. Generate Reviewable Setup Output

Run this from the repository you want to pilot:

```bash
code-mower init --easy
code-mower init --easy --apply --output-dir .code-mower.generated
```

The generated tree is reviewable output. It does not mutate live workflows,
create labels, trigger reviewers, or upload data.

## 4. Run The Preflight Doctor

```bash
code-mower doctor --preflight --json
```

`--preflight` is the friendly alias for the versioned v0.5 first-run preset.
It expands to the checks early adopters need:

- recommended profile selection;
- Python/runtime checks;
- local provider CLI discovery and smoke probes;
- stale terminal-label hygiene for merge-authority reviewer lanes;
- GitHub repository visibility, permissions, branch protection, and Actions
  cost diagnostics; and
- optional Code Mower Cloud token setup diagnostics.

Warnings are setup guidance. They are only fatal when you pass `--strict`.
If you want to see the shape of the output before installing, start with
`docs/first-run-transcript.md` and `docs/sample-doctor-output.md`.

In JSON mode, check the top-level `run_plan` field first. It tells you whether
the preflight included GitHub and optional cloud checks before you inspect
individual provider warnings.

For merge-authority lanes such as Codex, Claude audit, or Devin, also look for
`provider.review_hygiene`. It should pass for lanes that can satisfy the merge
bar. That check means Code Mower knows how to clear stale `*-audit-done` or
`*-audit-blocked` labels when a PR receives new commits, so an old review cannot
quietly approve a new head.

## 5. Set Model Provenance For Better Benchmarks

Code Mower can prove uploads and reviewer events without exact model names, but
CodeMower.com will mark those rows as incomplete benchmark evidence. For the
cleanest first run, set the model variables for the lanes you plan to compare:

```bash
export CODE_MOWER_CODEX_MODEL="gpt-5"
export CLAUDE_AUDIT_MODEL="claude-opus-4-8"
export CODE_MOWER_ANTIGRAVITY_MODEL="gemini-3.5-flash"
export CODE_MOWER_HERMES_MODEL="hermes-3-llama-3.1-405b"
```

Only set variables for tools you actually use. Model identifiers are benchmark
metadata, not secrets. Do not put API keys, auth output, prompts, diffs, or raw
model responses in provenance fields. If these are missing, `code-mower cloud
doctor` and the CodeMower.com dashboard will tell you which provider/tool needs
attention.

To generate a provider-specific setup checklist from the current Code Mower
registry:

```bash
code-mower providers provenance-env
code-mower providers provenance-env --provider codex --shell
```

The plain-text report shows which model env vars are configured and whether
Code Mower could probe each local CLI version. The `--shell` form prints only
safe model-env export templates with `TODO_MODEL_NAME` placeholders.

See [Provider Matrix](provider-matrix.md#benchmark-provenance-setup) and
[Cloud Sharing](cloud-sharing.md#structured-events) for the full provider list.

## 6. Detect Your Repo's Native Checks

```bash
code-mower checks detect --json
code-mower checks run --dry-run --json
```

`checks detect` reads your repository's declared check surface. For
JavaScript/TypeScript projects it uses `package.json` scripts and the lockfile
to choose npm, pnpm, yarn, or bun. For Python projects it detects Ruff config,
`tests/`, and a repo-local `.venv` before falling back to the current Python
interpreter. This is intentionally not a replacement for your repo's own
contract: TypeScript applications should still run their package-manager
lint/test/build scripts, while Python projects should run Ruff/pytest when
configured.

Use `checks run --dry-run` first to review commands before executing them.
Then run selected checks explicitly, for example:

```bash
code-mower checks run --only lint,test --json
```

## 7. Generate The Starter Value Report

If you want to prove the whole first-user path in one command, run the package
install rehearsal instead:

```bash
code-mower migration package-install-rehearsal \
  --package-spec code-mower==0.5.0b28 \
  --python "$(command -v python3.12)" \
  --json
```

`--python python3.12` also works when that command is on `PATH`; the
`command -v` form makes the selected interpreter visible in copied logs.

That rehearsal installs Code Mower into a clean virtual environment, creates a
fresh toy repository, runs `init --easy`, runs doctor, generates a starter value
report, and proves cloud upload/dogfood paths stay dry-run. See
[First-User Install Rehearsal](first-user-install-rehearsal.md) for the
release-gate checklist. The JSON includes `first_user_readiness`, a compact
scorecard that shows which install, doctor, report, and privacy gates passed.

To rehearse against a real repository that has not installed Code Mower yet,
add `--repo-path /path/to/repo`. Code Mower will detect the repo-native check
surface and dry-run it instead of trying product-wrapper parity:

```bash
code-mower migration package-install-rehearsal \
  --package-spec code-mower==0.5.0b28 \
  --repo-path /path/to/repo \
  --python "$(command -v python3.12)" \
  --json
```

For the manual report path in your pilot repository:

```bash
code-mower calibration evidence .code-mower.generated/calibration-corpus.json --json > calibration-evidence.json
code-mower reviewer-metrics calibration-evidence.json --spend .code-mower.generated/reviewer-spend.json --json > reviewer-metrics.json
code-mower calibration policy reviewer-metrics.json --json > lane-policy.json
code-mower calibration value-report .code-mower.generated/calibration-corpus.json \
  --spend .code-mower.generated/reviewer-spend.json \
  --output reviewer-value-report.md \
  --html-output reviewer-value-report.html
```

The starter corpus proves the command path. To bootstrap a project-specific
draft from recent merged PRs, run:

```bash
code-mower calibration auto-discover \
  --repo OWNER/REPO \
  --last-n 20 \
  --output .code-mower/draft-calibration-corpus.json

code-mower calibration value-report .code-mower/draft-calibration-corpus.json \
  --output .code-mower/draft-reviewer-value-report.md
```

Auto-discovery is deliberately conservative: it uses merged PR metadata,
structured audit trailers, and review-request signals to propose known-clean
and known-blocked cases. Review every disposition before promoting lanes to
selective or merge-gating roles.

## 8. Optional Cloud Dry Run

Cloud sharing is optional. The default upload payload excludes source code, raw
diffs, raw model transcripts, raw stdout/stderr, auth probe output, and secrets.

```bash
code-mower cloud export \
  --report reviewer-metrics=reviewer-metrics.json \
  --report lane-policy=lane-policy.json \
  --report value-report=reviewer-value-report.md \
  --output-dir .code-mower/cloud-benchmark-bundle \
  --anonymous \
  --json

code-mower cloud upload .code-mower/cloud-benchmark-bundle --dry-run --json
code-mower cloud doctor .code-mower/cloud-benchmark-bundle --json
```

Nothing uploads unless you pass `--yes`.

When you are ready to connect to CodeMower.com, add the service probe:

```bash
code-mower cloud doctor .code-mower/cloud-benchmark-bundle --probe-service --json
```

The service probe checks the upload endpoint's health route and reports the
dashboard URL plus the next setup/upload commands without echoing your token.

If you choose to connect Code Mower Cloud, create or receive a developer/team
token, then store it locally without putting the token in shell history:

```bash
code-mower cloud setup \
  --token-stdin \
  --team-id "YOUR_TEAM_SLUG" \
  --install-id "your-laptop" \
  --out ~/.config/code-mower/tokens/your-laptop.env
```

Then preview the routine metadata upload path before sending anything:

```bash
source ~/.config/code-mower/tokens/your-laptop.env
code-mower cloud dogfood --json
```

If the preview is clean, the confirmed command is:

```bash
code-mower cloud dogfood --yes --json
```

## What To Read Next

- `docs/quickstart.md` for the fuller walkthrough.
- `examples/demo-calibration/README.md` for a tiny known-clean/known-blocked
  reviewer value example.
- `docs/launch-command-surface.md` for the launch-safe command surface.
- `docs/sample-doctor-output.md` for a sanitized example of doctor output.
- `docs/github-setup.md` for private repositories, Actions cost, and branch
  protection.
- `docs/provider-matrix.md` for provider cost, privacy, and merge authority.
- `docs/cloud-sharing.md` for opt-in cloud export/upload details.
