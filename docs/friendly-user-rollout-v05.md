# v0.5 Friendly-User Rollout Plan

This is the operating plan for the first 5-10 friendly users before Code Mower
widens to 20-50 early adopters.

## Goal

Prove that a fresh user can get value from the public package without knowing
the private history behind Code Mower:

1. install the public beta;
2. run setup/doctor safely;
3. generate a local value report;
4. optionally upload a sanitized metadata bundle; and
5. understand what Code Mower recommends enabling next.

## Current Baseline

Use the current public beta unless a newer release is explicitly called out in
the invite:

```bash
pipx install --python python3.12 code-mower==0.5.0b37
```

The beta.37 baseline has been rehearsed from PyPI and against a private
JavaScript/mobile repository without requiring committed support files.

## Invite Criteria

Start with users who have:

- a GitHub repository they can safely run diagnostics against;
- Python 3.12 available through Homebrew, pyenv, or system package manager;
- willingness to run a local-first tool before enabling any cloud upload; and
- patience to report rough edges in install, doctor, or first report output.

Do not start with users who require GitLab/Bitbucket, non-GitHub code review,
managed enterprise controls, or fully automated paid reviewer lanes.

## First Session Script

Ask each user to run:

```bash
code-mower init --easy
code-mower doctor --preflight --json
code-mower next-steps --profile recommended
```

Then ask them to generate a starter report:

```bash
code-mower init --easy --apply --output-dir .code-mower.generated
code-mower calibration evidence .code-mower.generated/calibration-corpus.json --json > calibration-evidence.json
code-mower reviewer-metrics calibration-evidence.json --json > reviewer-metrics.json
code-mower calibration policy reviewer-metrics.json --json > lane-policy.json
code-mower calibration value-report .code-mower.generated/calibration-corpus.json \
  --output reviewer-value-report.md \
  --html-output reviewer-value-report.html
```

If they want cloud sharing, keep it dry-run-first:

```bash
code-mower cloud export \
  --report reviewer-metrics=reviewer-metrics.json \
  --report lane-policy=lane-policy.json \
  --report value-report=reviewer-value-report.md \
  --output-dir .code-mower/cloud-benchmark-bundle \
  --json

code-mower cloud upload .code-mower/cloud-benchmark-bundle --dry-run --json
```

Only after inspecting the bundle should they run:

```bash
code-mower cloud upload .code-mower/cloud-benchmark-bundle --yes --json
```

## What To Capture

For each friendly user, capture:

- install method and exact Code Mower version;
- operating system and Python version;
- repository language/framework category;
- doctor status and warnings;
- whether native checks were detected correctly;
- whether first report generation succeeded;
- whether cloud dry-run made the privacy boundary clear;
- whether the dashboard made the upload useful; and
- the first confusing sentence, command, or dashboard row they encountered.

Do not collect source code, raw diffs, raw model transcripts, auth output, or
secrets.

## Exit Criteria For v0.5

The friendly-user loop is good enough for a wider v0.5 push when:

- at least 5 fresh users complete install, doctor, and first report;
- at least 3 private repositories complete package-install rehearsal or
  equivalent setup checks;
- at least 3 users understand cloud sharing without needing operator help;
- dashboard uploads show provenance, report kinds, and next actions clearly;
- no user has to read private product-repo history to make progress; and
- the most common failure has a documented troubleshooting path.

## Known Limits To Say Out Loud

- Code Mower v0.5 is GitHub-first.
- Provider setup varies by CLI and account.
- Cloud sharing is optional and metadata-only by default.
- Auto-discovered calibration cases are starter dispositions, not benchmark
  truth until a human reviews them.
- New reviewer/lens lanes start informational until calibrated on the user's
  repository.
