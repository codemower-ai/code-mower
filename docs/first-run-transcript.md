# First Run Transcript

This page shows the intended first-run shape before a user installs anything.
It is a static transcript, not a guarantee that every machine will produce the
same provider warnings.

## Install

```bash
python3.12 --version
export CODE_MOWER_PYTHON="$(command -v python3.12)"
pipx install --python "$CODE_MOWER_PYTHON" code-mower==0.9.3b1
code-mower --version
```

Expected shape:

```text
Python 3.12.x
code-mower 0.9.3b1
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
FAIL  github.human_automation_token owner/repo is missing the DISPATCH_TOKEN human automation token secret
FAIL  github.branch_protection    owner/repo@main requires code-mower/gate from GitHub Actions instead of Any source
FAIL  github.repo.auto_merge      owner/repo does not allow auto-merge
WARN  github.actions_cost         private repo has high-frequency metadata workflows
PASS  cloud.token                 optional Code Mower Cloud token file is configured

Summary: fail, 24 checks, 3 failures, 4 warnings
Next: create DISPATCH_TOKEN, rebind code-mower/gate to Any source, enable repository auto-merge, then rerun doctor.
```

The failures are useful. They show whether the repo is safe to pilot before
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
