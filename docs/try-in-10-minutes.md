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
pipx install --python python3.12 "git+https://github.com/codemower-ai/code-mower.git@v0.5.0-alpha.15"
code-mower --version
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
- GitHub repository visibility, permissions, branch protection, and Actions
  cost diagnostics; and
- optional Code Mower Cloud token setup diagnostics.

Warnings are setup guidance. They are only fatal when you pass `--strict`.
If you want to see the shape of the output before installing, start with
`docs/first-run-transcript.md` and `docs/sample-doctor-output.md`.

## 5. Generate The Starter Value Report

If you want to prove the whole first-user path in one command, run the package
install rehearsal instead:

```bash
code-mower migration package-install-rehearsal \
  --package-spec "git+https://github.com/codemower-ai/code-mower.git@v0.5.0-alpha.15" \
  --python "$(command -v python3.12)" \
  --json
```

That rehearsal installs Code Mower into a clean virtual environment, creates a
fresh toy repository, runs `init --easy`, runs doctor, generates a starter value
report, and proves cloud upload/dogfood paths stay dry-run. See
[First-User Install Rehearsal](first-user-install-rehearsal.md) for the
release-gate checklist. The JSON includes `first_user_readiness`, a compact
scorecard that shows which install, doctor, report, and privacy gates passed.

For the manual report path in your pilot repository:

```bash
code-mower calibration evidence .code-mower.generated/calibration-corpus.json --json > calibration-evidence.json
code-mower reviewer-metrics calibration-evidence.json --spend .code-mower.generated/reviewer-spend.json --json > reviewer-metrics.json
code-mower calibration policy reviewer-metrics.json --json > lane-policy.json
code-mower calibration value-report .code-mower.generated/calibration-corpus.json \
  --spend .code-mower.generated/reviewer-spend.json \
  --output reviewer-value-report.md
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

## 6. Optional Cloud Dry Run

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
