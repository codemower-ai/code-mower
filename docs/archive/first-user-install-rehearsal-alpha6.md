# v0.5.0-alpha.6 First-User Install Rehearsal

This is the recorded fresh-install rehearsal for `v0.5.0-alpha.6`. It proves a
new user can install Code Mower from the public GitHub tag, run easy-mode setup,
generate a first value report, and inspect the cloud upload dry run without
using any source checkout state from the maintainer machine.

Run date: 2026-06-14

## Environment

```console
$ python3.12 --version
Python 3.12.13
```

The rehearsal used a brand-new temporary directory and a brand-new virtual
environment.

## Install From Public Tag

```console
$ python3.12 -m venv .venv
$ .venv/bin/python -m pip install --upgrade pip
$ .venv/bin/python -m pip install git+https://github.com/codemower-ai/code-mower.git@v0.5.0-alpha.6
Successfully installed PyYAML-6.0.3 code-mower-0.5.0a6

$ .venv/bin/code-mower --version
code-mower 0.5.0a6
```

## Easy Init

```console
$ git init .
Initialized empty Git repository

$ .venv/bin/code-mower init --easy --json
{
  "mode": "dry-run",
  "profile": {
    "id": "recommended",
    "lanes": [
      "codex",
      "claude_audit",
      "gitar"
    ]
  },
  "merge_authority_lanes": [
    "codex",
    "claude_audit"
  ],
  "informational_lanes": [
    "gitar"
  ]
}

$ .venv/bin/code-mower init --easy --apply --output-dir .code-mower.generated
Code Mower init apply wrote 20 files
Output: .code-mower.generated
```

The generated starter files included:

```console
.code-mower.generated/calibration-corpus.json
.code-mower.generated/context-packs.json
.code-mower.generated/reviewer-spend.json
.code-mower.generated/reviewer-value-report.example.md
.code-mower.generated/tools/code_mower
.code-mower.generated/tools/run_claude_audit_pr.sh
.code-mower.generated/tools/run_codex_audit_pr.sh
.code-mower.generated/tools/safe_gh_comment.py
```

## Doctor Preflight

```console
$ .venv/bin/code-mower doctor --preflight --json > doctor-preflight.json
doctor_exit=0
doctor_status=warn
```

Expected first-run warnings:

- `pytest` was not installed. Standalone easy mode does not require it, but some
  product-side wrappers use it.
- Audit label token environment variables were not set in the fresh temporary
  repo.

Useful passes:

- Config validation passed.
- Provider templates loaded.
- Recommended profile selected.
- Python 3.12 satisfied the runtime requirement.
- GitHub CLI auth probe succeeded.
- `rg` was found.
- Local Codex CLI probe succeeded.

## First Value Report

```console
$ .venv/bin/code-mower calibration value-report \
    .code-mower.generated/calibration-corpus.json \
    --output .code-mower/reviewer-value-report.md \
    --json
```

The starter corpus generated a first report with one historical clean-run row:

```markdown
# Code Mower Reviewer Value Report

Corpus: `small-known-pr-pilot`
Items: 5
Adjudicated evidence: 0
Finding evidence: 0
Run dispositions: 0
Reviewer runs: 1
```

The expected recommendation was:

```text
codex-audit: collect human dispositions before comparing reviewer accuracy.
```

## Cloud Export And Dry Run

```console
$ .venv/bin/code-mower cloud export \
    --repo-slug example/rehearsal \
    --anonymous \
    --report value-report=.code-mower/reviewer-value-report.md \
    --output-dir .code-mower/cloud-bundle \
    --json
```

Result:

```json
{
  "mode": "cloud-export",
  "event_count": 0,
  "included_reports": [
    {
      "kind": "value-report",
      "target": "reports/01-report.md"
    }
  ],
  "upload_ready": false
}
```

Then:

```console
$ .venv/bin/code-mower cloud upload .code-mower/cloud-bundle --dry-run --json
```

Result:

```json
{
  "endpoint": "https://codemower.com/api/ingest",
  "mode": "cloud-upload-dry-run",
  "privacy_mode": "anonymous",
  "report_count": 1,
  "requires_yes": true,
  "upload_mode": "metadata_only",
  "would_upload": false
}
```

The dry-run output explicitly excluded:

- source code
- raw diffs
- raw model transcripts
- raw stdout/stderr
- auth probe output
- secrets

## Notes

- Install from the public tag succeeded.
- `init --easy` and `init --easy --apply` succeeded in a fresh repo.
- `doctor --preflight` exited 0 with actionable warnings, not hard failures.
- The starter value report path worked.
- The cloud bundle and upload dry-run path remained privacy-first and
  metadata-only.
- A bundle with no structured events correctly reported `upload_ready: false`
  while still allowing the user to inspect the report-only dry run.

