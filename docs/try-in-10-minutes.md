# Try Code Mower In 10 Minutes

This is the shortest v0.5 early-adopter path. It is local-first and safe to run
on one GitHub repository before you enable any recurring workflows or paid
reviewer lanes.

## 1. Install

Code Mower requires Python 3.11 or newer. Python 3.12 is recommended.

```bash
python3.12 --version
pipx install --python python3.12 "git+https://github.com/jeffhuber/code-mower.git@v0.5.0-alpha.1"
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

## 4. Run The v0.5 Doctor Preset

```bash
code-mower doctor --v05 --json
```

`--v05` expands to the first-run checks early adopters need:

- recommended profile selection;
- Python/runtime checks;
- local provider CLI discovery and smoke probes;
- GitHub repository visibility, permissions, branch protection, and Actions
  cost diagnostics; and
- optional Code Mower Cloud token setup diagnostics.

Warnings are setup guidance. They are only fatal when you pass `--strict`.

## 5. Generate The Starter Value Report

```bash
code-mower calibration evidence .code-mower.generated/calibration-corpus.json --json > calibration-evidence.json
code-mower reviewer-metrics calibration-evidence.json --spend .code-mower.generated/reviewer-spend.json --json > reviewer-metrics.json
code-mower calibration policy reviewer-metrics.json --json > lane-policy.json
code-mower calibration value-report .code-mower.generated/calibration-corpus.json \
  --spend .code-mower.generated/reviewer-spend.json \
  --output reviewer-value-report.md
```

The starter corpus proves the command path. Replace it with your own known-clean
and known-blocked PRs before promoting lanes to selective or merge-gating roles.

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

If you choose to connect Code Mower Cloud, create or receive a developer/team
token, then store it locally without putting the token in shell history:

```bash
code-mower cloud setup \
  --token-stdin \
  --team-id "YOUR_TEAM_SLUG" \
  --install-id "your-laptop" \
  --out ~/.config/code-mower/tokens/your-laptop.env
```

## What To Read Next

- `docs/quickstart.md` for the fuller walkthrough.
- `docs/github-setup.md` for private repositories, Actions cost, and branch
  protection.
- `docs/provider-matrix.md` for provider cost, privacy, and merge authority.
- `docs/cloud-sharing.md` for opt-in cloud export/upload details.
