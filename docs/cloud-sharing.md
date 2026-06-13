# Code Mower Cloud Sharing

Code Mower is local-first. Cloud sharing is optional and exists to help users
compare AI builders and reviewers across time, repositories, languages, and
teams.

## Privacy Boundary

The default bundle excludes:

- source code;
- raw diffs;
- raw model transcripts;
- raw stdout/stderr;
- auth probe output; and
- secrets.

Report files are copied into the local bundle exactly as supplied. Inspect them
before sharing. Upload report contents only when you intentionally pass
`--include-reports`.

## Export

Create a local bundle:

```bash
code-mower cloud export \
  --report reviewer-metrics=reviewer-metrics.json \
  --report lane-policy=lane-policy.json \
  --report value-report=reviewer-value-report.md \
  --output-dir .code-mower/cloud-benchmark-bundle \
  --anonymous \
  --json
```

The bundle contains:

- `code-mower-cloud-bundle.json`
- `README.md`
- copied report files under `reports/`
- metadata-only structured events when supplied

## Structured Events

Cloud bundles may include metadata-only benchmark events. Events use schema
`code_mower.benchmarkEvent.v1` and must not contain source code, raw diffs, raw
model transcripts, raw stdout/stderr, auth output, or secrets.

Supported event types are:

- `dogfood_upload`
- `reviewer_run`
- `calibration_run`
- `value_report_snapshot`
- `lane_policy_snapshot`
- `workflow_run`

Include events from JSON, JSON arrays, or JSONL:

```bash
code-mower cloud export \
  --event reviewer_run=reviewer-run-events.jsonl \
  --output-dir .code-mower/cloud-benchmark-bundle \
  --json
```

## Upload Dry Run

Preview the upload without network transfer:

```bash
code-mower cloud upload .code-mower/cloud-benchmark-bundle --dry-run --json
```

Without `--yes`, upload stays in dry-run mode.

Check endpoint, token, and bundle readiness:

```bash
code-mower cloud doctor .code-mower/cloud-benchmark-bundle --json
```

## Upload

When you are ready to send metadata to Code Mower Cloud:

```bash
export CODE_MOWER_CLOUD_TOKEN="your-token"
code-mower cloud upload .code-mower/cloud-benchmark-bundle --yes --json
```

To include report file contents:

```bash
code-mower cloud upload .code-mower/cloud-benchmark-bundle \
  --yes \
  --include-reports \
  --json
```

For local development:

```bash
code-mower cloud upload .code-mower/cloud-benchmark-bundle \
  --endpoint http://localhost:3000/api/ingest \
  --yes \
  --json
```

Non-local HTTP endpoints are rejected; production uploads should use HTTPS.

## Routine Dogfood Upload

For repositories that want ongoing metadata uploads, use the dogfood command.
It auto-detects the GitHub repo slug when possible, includes common shareable
reports when they exist, adds a structured `dogfood_upload` event, runs cloud
doctor, and stays dry-run by default:

```bash
code-mower cloud dogfood --source codex-local --json
```

To upload after inspection:

```bash
export CODE_MOWER_CLOUD_TOKEN="<token>"
export CODE_MOWER_CLOUD_TEAM_ID="your-team-slug"
export CODE_MOWER_INSTALL_ID="codex-code-mower"
code-mower cloud dogfood --source codex-local --yes --json
```

For GitHub Actions, keep the workflow low-cost and optional: run it on `main`
pushes, use `secrets.CODE_MOWER_CLOUD_TOKEN`, and skip the upload when the
secret is absent.

## What codemower.com Should Store First

The v0.5 cloud service should start with:

- upload id;
- privacy mode;
- upload mode;
- install id, team id, and repo slug when the user opts in;
- report count and report kinds;
- structured event count and event types;
- excluded-content declaration; and
- optional report text when `--include-reports` is explicit.

Do not require account login for local export. Hosted dashboards can add
accounts, teams, deletion, and retention controls after the ingest path is
stable.
