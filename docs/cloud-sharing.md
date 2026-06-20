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

The live v0.5 value is private team signal. The network-effect value is a
roadmap feature: as enough teams opt in, CodeMower.com can add anonymized
aggregate benchmarks such as "your Codex audit useful-rate is above/below the
cohort median" or "this SaaS lane is noisy for repos with similar shape." The
default upload payload is metadata-only so teams can contribute toward that
future benchmark without sharing source.

CodeMower.com should also make every visible number inspectable. Signed-in
users can drill from recent uploads and events to token-safe evidence detail
pages and export JSON for support/debugging. Those pages are meant to answer
"what data is this chart using?" without exposing source, raw diffs, raw model
transcripts, auth output, or secrets.

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

Unsafe event fields are rejected before export or upload. If an event contains
raw output, transcripts, tokens, secrets, auth previews, or secret-like values,
fix the event producer and rerun export.

Supported event types are:

- `dogfood_upload`
- `reviewer_run`
- `calibration_run`
- `value_report_snapshot`
- `lane_policy_snapshot`
- `provider_catalog_snapshot`
- `workflow_run`

Events may include a `tool` object with metadata-only provenance: tool name,
tool version, provider, model, integration, runtime environment, lens, and
prompt-pack version. This is the field CodeMower.com uses to answer "which AI
builder/reviewer/version produced this signal?" Missing tool/model provenance
is still accepted for operational dogfood, but it is not enough for strong
benchmark claims. The dashboard should therefore treat provenance coverage as
a quality signal, not a vanity count.

Include events from JSON, JSON arrays, or JSONL:

```bash
code-mower cloud export \
  --event reviewer_run=reviewer-run-events.jsonl \
  --output-dir .code-mower/cloud-benchmark-bundle \
  --json
```

To convert saved Code Mower verdict artifacts into metadata-only reviewer run
events:

```bash
code-mower telemetry export-verdict-events \
  ~/.cache/code-mower-audits/verdicts \
  --repo owner/repo \
  --output reviewer-run-events.jsonl
```

This exports verdict, provider, lane, PR number, and severity counts. It does
not include the raw review comment body, raw model output, source code, diffs,
or head SHAs by default. Use `--include-git-ref` only after deciding that head
SHA metadata is acceptable for your team.

If you omit the verdict path, Code Mower reads `CODE_MOWER_VERDICT_ARTIFACT_DIR`
when set, otherwise `~/.cache/code-mower-audits/verdicts`.

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

- [https://codemower.com/login](https://codemower.com/login)
- [https://codemower.com/dashboard](https://codemower.com/dashboard)

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

Dogfood is a current-state signal. It uploads metadata about the current
checkout/workflow run and any current shareable reports that exist. It does not
reconstruct old pull requests, old reviewer comments, older GitHub Actions runs,
or historical benchmark outcomes.

Current Code Mower dogfood bundles include Code Mower client provenance and a
`provider_catalog_snapshot` event for each configured provider lane. Catalog
snapshots report what Code Mower can safely know from configuration and harmless
local version probes: tool surface, version when available, provider, model when
configured, lane/lens id, integration, and runtime environment. Reviewer and
calibration summaries may also preserve provider-observed model ids from
structured CLI stats when the run output includes them, such as the main
Google-compatible model used for a review. They are coverage evidence, not
reviewer-quality evidence. CodeMower.com should use them to identify missing
tool/model/version metadata and should use reviewer or calibration events for
usefulness, false-positive, and lane-promotion claims.

## Historical Catch-Up

If you enabled cloud sharing after already running Code Mower, you can catch up
recent GitHub Actions metadata without uploading source, diffs, transcripts, or
workflow logs:

```bash
code-mower cloud catch-up --repo-slug owner/repo --limit 50 --json
```

This command calls `gh run list`, builds a local
`.code-mower/cloud-catch-up-bundle`, runs cloud doctor, and prints a dry-run
upload preview. Nothing is sent without `--yes`:

```bash
source ~/.config/code-mower/tokens/your-install-id.env
code-mower cloud catch-up --repo-slug owner/repo --limit 50 --yes --json
```

The catch-up payload contains `workflow_run` events with workflow name,
trigger, run status, conclusion, URL, and timestamps. Branch names and commit
SHAs are intentionally excluded by default because branch naming can reveal
product or customer details. Use `--include-git-ref` only when your team has
reviewed and accepted that metadata tradeoff.

The command result also includes a `catch_up` summary with workflow, status,
and conclusion counts plus `provenance: imported_history`,
`history_only: true`, `calibration_evidence: false`, and `trust_guidance`
entries for `use_for`, `do_not_use_for`, and `next_step`. Dashboards and
support tools should use that summary to avoid treating historical GitHub
Actions imports as calibrated provider/lens evidence.

Use catch-up once or occasionally after onboarding a repository. Use
`cloud dogfood` for ongoing current-state uploads.

Catch-up is intentionally separate from dogfood. Use `code-mower cloud
catch-up` for sanitized GitHub Actions history, or `code-mower cloud repo-sync
--mode catch-up` when syncing multiple repositories from an operator machine.
Use `reviewer-runs` for existing local verdict artifacts. Keeping these modes
separate lets CodeMower.com label provenance honestly: current dogfood,
historical workflow history, and reviewer/calibration evidence are different
signals.

### Safe Catch-Up Checklist

Use this checklist before turning on `--yes`:

1. Run the dry run first:

   ```bash
   code-mower cloud catch-up --repo-slug owner/repo --limit 50 --json
   ```

2. Confirm the preview reports `workflow_run` events only. Catch-up is for
   GitHub Actions metadata, not source code, raw logs, raw diffs, or model
   transcripts.
3. Keep `--include-git-ref` off unless your team has decided that branch names
   and head SHAs are acceptable metadata.
4. Use a small `--limit` first, then widen if the dashboard value is useful.
5. Upload only after loading a team token:

   ```bash
   source ~/.config/code-mower/tokens/your-install-id.env
   code-mower cloud catch-up --repo-slug owner/repo --limit 50 --yes --json
   ```

6. After the one-time catch-up, prefer `code-mower cloud dogfood --yes --json`
   or a low-cost main-branch workflow for ongoing uploads.

## Historical Reviewer Runs

If you already have local Code Mower audit verdict artifacts, upload them
separately from GitHub Actions catch-up. This is the path that makes
CodeMower.com show reviewer/lens signal instead of only workflow history.
The command stays dry-run unless `--yes` is explicit:

```bash
code-mower cloud reviewer-runs --repo-slug owner/repo --json
```

After inspecting the dry run:

```bash
source ~/.config/code-mower/tokens/your-install-id.env
code-mower cloud reviewer-runs --repo-slug owner/repo --yes --json
```

Use `--verdicts` to point at a non-default artifact directory and
`--include-git-ref` only after deciding that head SHA metadata is acceptable for
your team.

The lower-level export path remains available when you want to inspect or edit
the JSONL before bundling:

```bash
code-mower telemetry export-verdict-events \
  ~/.cache/code-mower-audits/verdicts \
  --repo owner/repo \
  --output reviewer-run-events.jsonl

code-mower cloud export \
  --event reviewer_run=reviewer-run-events.jsonl \
  --output-dir .code-mower/reviewer-run-bundle \
  --json

code-mower cloud upload .code-mower/reviewer-run-bundle --dry-run --json
```

Use historical reviewer export once when onboarding a machine or repo, then rely
on normal dogfood/current-run uploads for ongoing data.

## Multi-Repo Operator Sync

If one machine regularly drives several repos, use `repo-sync` to preview the
same dogfood/reviewer-run upload loop across each checkout. This keeps
repo-specific paths local to the operator and out of public configuration:

```bash
code-mower cloud repo-sync \
  --repo owner/repo=/path/to/repo \
  --repo owner/other-repo=/path/to/other-repo \
  --json
```

By default, `repo-sync` runs `dogfood` plus `reviewer-runs` for each repo and
stays dry-run. After inspecting the preview:

```bash
source ~/.config/code-mower/tokens/your-install-id.env
code-mower cloud repo-sync \
  --repo owner/repo=/path/to/repo \
  --repo owner/other-repo=/path/to/other-repo \
  --yes \
  --json
```

Use `--mode` to choose exact modes:

```bash
code-mower cloud repo-sync \
  --repo owner/repo=/path/to/repo \
  --mode dogfood \
  --mode reviewer-runs \
  --mode catch-up \
  --limit 50 \
  --json
```

`catch-up` uses the GitHub CLI and uploads sanitized workflow metadata only.
Branch names and SHAs remain excluded unless `--include-git-ref` is explicit.
This command is intended for trusted local/operator environments, not as a
background cron and not as a requirement for every OSS user.

Recent dogfood/catch-up imports used this shape across the OSS repo, the
hosted service repo, and two private reference/product repos with
`--mode catch-up --limit 100`. Those uploads are intentionally displayed as
imported history, not as calibrated reviewer/lens evidence. Beta.14 makes that
distinction explicit in the catch-up command result and terminal output.

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
Team metadata export and owner/admin deletion are live early-adopter controls.
Automated retention jobs, richer hosted reports, and aggregate cohort views
remain next-stage hosted-service work.
