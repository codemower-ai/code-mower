# Cloud Data Contract

Code Mower Cloud sharing is optional. The local OSS package remains useful
without a CodeMower.com account or token.

This document defines the public v0.5 data boundary for metadata uploads.

## Personas

- **OSS user:** installs Code Mower, runs local checks/reports, optionally gets
  a CodeMower.com team token, inspects a dry run, then uploads metadata.
- **CodeMower.com operator:** runs Supabase, Vercel, OAuth providers, DNS,
  service-role/admin secrets, retention, abuse handling, and hosted reporting.

OSS users should not need Supabase, Vercel, OAuth-app, DNS, database,
service-role, or hosted-secret access.

## Default Exclusions

Default cloud bundles exclude:

- source code;
- raw diffs;
- raw model transcripts;
- raw stdout/stderr;
- auth probe output; and
- secrets.

Report text is not uploaded by default. Uploading report contents requires an
explicit `--include-reports` flag, and the hosted service may still discard
report text depending on operator retention settings.

## Bundle Shape

`code-mower cloud export` writes an inspectable local directory:

```text
.code-mower/cloud-benchmark-bundle/
  code-mower-cloud-bundle.json
  README.md
  reports/
```

The manifest uses schema `code_mower.cloudUpload.v1`. It contains metadata such
as privacy mode, upload mode, install id, optional team id, optional repository
slug, report count and report kinds, structured event count and event types,
excluded-content declaration, and copied report file metadata.

## Structured Events

Metadata events use schema `code_mower.benchmarkEvent.v1`. They are intended to
capture reviewer and workflow facts without raw code artifacts.

Supported event types include:

- `dogfood_upload`
- `reviewer_run`
- `calibration_run`
- `value_report_snapshot`
- `lane_policy_snapshot`
- `workflow_run`

Events may include provider/lens names, timing, cost, verdict, useful finding
counts, false-positive counts, repository slug, install id, and coarse runtime
metadata. They must not include source code, raw diffs, raw transcripts,
stdout/stderr, auth output, or secrets.

## Token Model

CodeMower.com uses team ingest tokens for upload authorization. Users create or
receive a token, then store it locally with:

```bash
code-mower cloud setup \
  --token-stdin \
  --team-id "your-team-slug" \
  --install-id "your-install-id" \
  --out ~/.config/code-mower/tokens/your-install-id.env
```

The hosted service stores token hashes and short prefixes, not full token
values. A token can be revoked without rotating every team credential.

## Safe Upload Flow

The recommended flow is dry-run first:

```bash
code-mower cloud export \
  --report value-report=.code-mower/reviewer-value-report.md \
  --output-dir .code-mower/cloud-benchmark-bundle \
  --anonymous \
  --json

code-mower cloud upload .code-mower/cloud-benchmark-bundle --dry-run --json
```

Nothing uploads unless `--yes` is supplied:

```bash
source ~/.config/code-mower/tokens/your-install-id.env
code-mower cloud upload .code-mower/cloud-benchmark-bundle --yes --json
```

## Hosted Storage In v0.5

The v0.5 hosted service stores upload ids and timestamps, token/team linkage,
repository slug when supplied, report summaries and counts, structured metadata
events, cost/latency/usefulness fields when supplied, and recommendation inputs
derived from metadata.

It should not store source, raw diffs, raw transcripts, stdout/stderr, auth
output, or secrets by default.

## Roadmap

Before broad public adoption, Code Mower Cloud should add user-visible deletion
controls, retention settings, team export, clearer anonymization/cohort rules,
schema migration notes, and public examples of useful aggregate benchmark
outputs.
