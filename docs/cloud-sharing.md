# Code Mower Cloud Sharing

Code Mower is local-first. Cloud sharing is optional and exists to help users
compare AI builders and reviewers across time, repositories, languages, and
teams.

The public upload boundary is defined in
[Cloud Data Contract](cloud-data-contract.md).

## Why Share Metadata?

Local reports answer "what happened in this repository?" CodeMower.com is meant
to answer the questions that become more useful over time and across teams:

- Which reviewer lanes catch real blockers without drowning the team in noise?
- Which providers are slow, expensive, or redundant on repositories like mine?
- Which prompt lenses improve useful signal, and which only change wording?
- What should this team enable next: another local reviewer, a SaaS reviewer,
  a security lens, an operability lens, or nothing?
- How are reviewer usefulness, false positives, latency, and cost trending over
  the last few weeks?

The network-effect value is comparison. As more teams opt in, CodeMower.com can
show private team dashboards plus anonymized aggregate benchmarks such as
"your Codex audit useful-rate is above/below the cohort median" or "this SaaS
lane is noisy for repos with similar shape." The default upload payload is
metadata-only so teams can contribute to that benchmark without sharing source.

## Setup Personas

There are two different setup jobs:

- **OSS user:** install Code Mower, run local checks/reports, optionally create
  or receive a CodeMower.com developer/team token, and store it with
  `code-mower cloud setup --token-stdin`.
- **CodeMower.com operator:** run the hosted service, including
  Supabase/Postgres, Vercel, OAuth providers, DNS, service-role/admin secrets,
  retention, abuse handling, and token administration fallback.

An OSS user does not need Supabase, Vercel, OAuth-app, DNS, database, or
service-role access to export local bundles or upload opt-in metadata.

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

To also verify the CodeMower.com service is reachable:

```bash
code-mower cloud doctor .code-mower/cloud-benchmark-bundle --probe-service --json
```

The service probe checks the endpoint's `/api/health` route. Doctor output also
includes the dashboard URL and token-safe next-step commands, so it is suitable
for support screenshots and CI logs.

## Upload

When you are ready to send metadata to Code Mower Cloud:

```bash
code-mower cloud setup \
  --token-stdin \
  --team-id "your-team-slug" \
  --install-id "your-install-id" \
  --out ~/.config/code-mower/tokens/your-install-id.env

source ~/.config/code-mower/tokens/your-install-id.env
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

## Team Tokens And Login

Human team/token management happens on CodeMower.com:

```text
https://codemower.com/login
https://codemower.com/dashboard
```

The intended early-adopter flow is:

1. sign in to CodeMower.com with GitHub, Google, or Apple;
2. create or join a team;
3. issue a team ingest token from the dashboard;
4. run `code-mower cloud setup --token-stdin` to store it locally, or store it
   directly in a CI secret; and
5. run `cloud doctor`, `cloud upload --dry-run`, then `cloud upload --yes`.

The local OSS package does not require login for local export, local value
reports, or dry-run upload checks. Login is only needed to create and manage
hosted team tokens. Operator-issued tokens remain a fallback for users who
cannot use self-service login yet.

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
source ~/.config/code-mower/tokens/codex-code-mower.env
code-mower cloud dogfood --source codex-local --yes --json
```

For GitHub Actions, keep the workflow low-cost and optional: run it on `main`
pushes, use `secrets.CODE_MOWER_CLOUD_TOKEN`, and skip the upload when the
secret is absent.

## What codemower.com Stores First

The v0.5 cloud service starts with:

- upload id;
- privacy mode;
- upload mode;
- install id, team id, and repo slug when the user opts in;
- report count and report kinds;
- structured event count and event types;
- excluded-content declaration; and
- optional report text when `--include-reports` is explicit.

CodeMower.com now has a protected dashboard for team and token management.
Deletion, retention controls, richer hosted reports, and aggregate cohort views
remain next-stage hosted-service work.
