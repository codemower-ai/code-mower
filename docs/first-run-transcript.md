# First Run Transcript

This page shows the intended first-run shape before a user installs anything.
It is a static transcript, not a guarantee that every machine will produce the
same provider warnings.

## Install

```bash
python3.12 --version
pipx install --python python3.12 code-mower==0.5.0b28
code-mower --version
```

Expected shape:

```text
Python 3.12.x
code-mower 0.5.0b28
```

## Generate Local Setup

From the repository you want to pilot:

```bash
code-mower init --easy
code-mower init --easy --apply --output-dir .code-mower.generated
```

Expected shape:

```text
Code Mower easy-mode setup

Profile: recommended local/manual reviewer lanes
Writes: .code-mower.generated/
Default posture:
- local structured audits first
- hosted/SaaS lanes informational until calibrated
- no recurring reviewer schedules
- no cloud upload unless explicitly configured
```

## Run Doctor

```bash
code-mower doctor --preflight
```

Shortened example:

```text
PASS  config.validate             config validates
PASS  profile.select              selected profile: codex, claude_audit, gitar
PASS  runtime.python              Python 3.12 satisfies Code Mower requirements
PASS  runtime.github_auth         GitHub CLI auth probe succeeded
PASS  runtime.local_cli codex     codex found
PASS  runtime.local_cli claude    claude auth smoke probe succeeded
WARN  env.tokens codex            missing CODEX_AUDIT_LABEL_TOKEN or GITHUB_TOKEN
WARN  github.actions_cost         private repo has high-frequency metadata workflows
PASS  cloud.token                 optional Code Mower Cloud token file is configured

Summary: warn, 20 checks, 0 failures, 5 warnings
Next: fix token warnings, keep paid lanes manual, then generate a value report.
```

The warnings are useful. They show whether the repo is safe to pilot before
you add labels, GitHub workflows, provider CLIs, or cloud upload.

## Generate The Starter Value Report

```bash
code-mower calibration auto-discover \
  --repo OWNER/REPO \
  --last-n 20 \
  --output .code-mower/draft-calibration-corpus.json

code-mower calibration value-report .code-mower/draft-calibration-corpus.json \
  --output .code-mower/draft-reviewer-value-report.md

code-mower calibration value-report .code-mower.generated/calibration-corpus.json \
  --output .code-mower/reviewer-value-report.md
```

Expected shape:

```text
Wrote .code-mower/reviewer-value-report.md
Wrote .code-mower/draft-reviewer-value-report.md
```

The generated starter corpus proves the command path. The auto-discovered
draft corpus uses recent PR metadata to reduce blank-page friction, but it is
not an adjudicator. Confirm every disposition before using a report to promote
reviewer lanes.

## Optional Cloud Dry Run

```bash
code-mower cloud export \
  --report value-report=.code-mower/reviewer-value-report.md \
  --output-dir .code-mower/cloud-benchmark-bundle \
  --anonymous \
  --json

code-mower cloud upload .code-mower/cloud-benchmark-bundle --dry-run --json
```

Nothing uploads unless the user passes `--yes`.
