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
- `builder_run`
- `reviewer_run`
- `calibration_run`
- `value_report_snapshot`
- `lane_policy_snapshot`
- `provider_catalog_snapshot`
- `work_order`
- `workflow_run`

Events may include provider/lens names, timing, cost, verdict, useful finding
counts, false-positive counts, repository slug, install id, and coarse runtime
metadata. They must not include source code, raw diffs, raw transcripts,
stdout/stderr, auth output, or secrets.

The OSS uploader normalizes and validates every structured event before it is
written into a bundle. Required fields use simple JSON object/string shapes,
`metrics`, `dimensions`, and `tool` remain objects, event types come from the
supported list above, and additive fields are allowed as long as they pass the
same metadata-only privacy scan.

`provider_catalog_snapshot` events are special: they describe configured
provider lanes and safe tool/model/version coverage. They are useful for setup
and benchmark trust diagnostics, but they are not reviewer accuracy evidence and
must not be counted as useful findings, false positives, or lane-promotion
support. Cloud bundle provenance summaries therefore keep both raw upload counts
and benchmark-evidence counts: catalog snapshots still appear in raw inventory,
but they are excluded from `benchmark_*` provenance coverage fields.

`work_order` events are also operational metadata, not reviewer accuracy
evidence. They connect a GitHub issue planning flow to later builder/reviewer
runs by recording issue/work-order provenance, role lenses, review lanes, and
optional delivery metadata: PR URL/number/state, reviewer-check names/statuses,
merge SHA, and merged-at time. They must not include issue bodies, source code,
raw diffs, transcripts, stdout/stderr, auth output, or secrets.

`builder_run` events are authoring-side provenance, not reviewer approval. They
record who/what produced a branch or PR from an issue/work-order contract:
builder provider, executor surface, issue/PR identifiers, branch name, optional
safe run URL, and optional coarse metrics such as elapsed time, cost, or human
intervention count. They exist so CodeMower.com can connect
`issue -> plan -> work order -> builder run -> PR -> reviewer checks -> merge`
without receiving source, issue bodies, diffs, prompts, transcripts,
stdout/stderr, auth output, or secrets.

Local `code_mower.authoringRun.v1` artifacts from `builder-experiment run` may
also be passed as `--event builder_run=PATH`. The OSS uploader converts them to
the normalized `builder_run` event shape and uploads only metadata such as
provider, model, branch/PR, elapsed time, command hash, and
`command_output_capture: disabled`; local privacy markers and executor details
that use raw-output vocabulary are not uploaded as event keys.

Calibration and value-report uploads may add optional automated-vs-manual
metadata to reviewer summaries and `reviewer_run`-shaped rows:
`manual_outcome` (`pass`, `blocked`, or `unknown`), `automated_vs_manual`
(`match`, `missed_blocker`, `false_blocker`, or `unknown`), and aggregate
profile counters such as `auto_manual_match_runs`,
`auto_manual_missed_blocker_runs`, and `auto_manual_false_blocker_runs`. These
fields compare automated reviewer status with manual/adjudicated calibration
truth. They are additive and CodeMower.com must continue accepting beta.40
uploads that omit them. They must not include plan text, issue body text,
source code, raw diffs, prompts, transcripts, stdout/stderr, auth output, or
secrets.

Plan-context audit prompts only read manifest-listed documents/previews that
resolve inside the repository root. Default manifests are read from the trusted
base ref rather than mutable working-tree files; explicit manifest paths are
operator-pinned. The Codex wrapper runs `codex exec review --base` without a
supplemental stdin prompt; trusted plan and decision context are passed to the
structured verdict conversion prompt.

Auto-inferred `builder_run` events may add metadata-only dimensions such as
`auto_inferred`, `builder_inference_confidence`, `builder_inference_signals`,
and `pr_author`. The inference signals are marker names only, for example a bot
author, branch prefix, or detected hosted-agent URL marker; the PR body text and
footer text used for inference are not stored.
Cursor inference only accepts Cursor agent/background-agent URLs or explicit
Cursor-agent footer markers; generic `cursor.com` links are ignored.
When metadata signals disagree, the highest-priority provider signal wins and
provider-specific run URLs are emitted only for that winning provider.

Audit CLIs may also append local spend rows to `reviewer-spend.json` using
schema `code_mower.reviewerSpend.v1`. The file remains backward-compatible with
beta.40 aggregate files that only contain `profiles`; new clients add an
append-only `runs` list. Each run may include `run_id`, `created_at`, `lane`,
`repo`, `pr_number`, `head_sha`, `model`, `wall_seconds`, `verdict`,
`cost_usd`, and token counters such as `input_tokens`, `output_tokens`,
`total_tokens`, cached-input counters, or `reasoning_tokens`. These are
metadata-only fields. The ledger must not contain source, diffs, prompts,
transcripts, stdout/stderr, issue bodies, auth output, or secrets.

`code-mower cloud export --spend reviewer-spend.json`, `cloud dogfood`, and
`cloud reviewer-runs` convert spend `runs` into `reviewer_run` events.
`reviewer-runs` reads `.code-mower/reviewer-spend.json` automatically when the
ledger is present and merges spend metrics into matching verdict events before
upload so dashboards do not double-count reviewer attempts. When `--verdicts`
narrows the exported artifacts, unmatched spend rows are ignored by default; use
`--include-unmatched-spend` only for deliberate reviewer-spend backfill. The
derived event places latency/cost/token numbers under `metrics`, PR/SHA/lane
identifiers under `dimensions`, and model/tool identity under `tool`.
CodeMower.com should accept uploads without these fields from beta.40 clients
and treat missing spend rows as unknown, not zero measured spend.

Generated self-hosted local audit workflows may automatically call
`cloud reviewer-runs` and `cloud dogfood` after audit attempts when a team
configures `CODE_MOWER_CLOUD_TOKEN`; runner-temp spend rows travel with the
reviewer-run upload. This is an upload-path change, not an
event-shape change: verdict artifacts still become `reviewer_run` events,
spend rows still become `reviewer_run` events, work-order sidecars remain
`work_order` events, and beta.40 through beta.46 uploads that omit these
automated events remain valid. When a verdict artifact records audit runtime,
the exported reviewer-run metrics include the legacy `duration_seconds_total`
and dashboard-compatible `duration_seconds` and `wall_seconds` aliases. The
workflow uses trusted default-branch support files and must not upload source,
diffs, prompts, transcripts, stdout/stderr, issue body text, or secrets.
Fixture-shaped or quarantined audit verdict artifacts are excluded from
`reviewer_run` export and upload so local wrapper tests cannot become dashboard
or calibration evidence.
Before conversion, provider verdict artifacts are checked for the local
`code_mower.auditVerdictArtifact.v1` shape: repository, PR number, verdict, and
comment body are required, known verdict values are enforced, and timing fields
must be finite non-negative metadata when present.

`reviewer_run` events may include per-lane audit comment attribution in
`dimensions.audit_comment_lane_id`, `dimensions.audit_comment_identity_source`,
and `dimensions.audit_comment_trailer_prefix`. When present,
`audit_comment_identity_source=trailer` means the hidden audit-state trailer
was the authoritative lane signal for that result. CodeMower.com must continue
to treat beta.40 uploads that only include `dimensions.lane_id` as valid
metadata-only reviewer evidence.

Each event may also include a `tool` object using schema
`code_mower.toolProvenance.v1`. This object is the benchmark-grade provenance
surface for AI tool/version/model data:

- `role`: `builder`, `reviewer`, `workflow`, or another explicit lane role;
- `tool_name` and `tool_version`: the local CLI, GitHub App, hosted reviewer,
  or agent surface that produced the event;
- `provider`, `model`, and `model_version_raw`: the AI provider/model identity
  when known;
- `model_source`: where the normalized model identity came from, such as `env`,
  `profile:<name>`, `default`, `vendor_hidden`, `not_applicable`, or
  `missing`;
- `version_source`: where the tool/package version came from, such as
  `cli_version_probe`, `package_version`, `vendor_hidden`, `not_applicable`,
  `not_probed`, or `missing`;
- `integration` and `runtime_environment`: for example `cli`, `github_app`,
  `hosted`, `local`, or `github_actions`; and
- `lens` and `prompt_pack_version`: the review lens/prompt bundle that shaped
  the run.

Model identity can come from explicit environment configuration, the selected
Code Mower provider profile, a safe default, safe provider metadata, or
structured provider summary stats. For example, Google-compatible CLI summaries
may report multiple internal models; Code Mower records the main review model
when it can identify one, and leaves the model blank when it cannot do so
safely. CodeMower.com should display `model_source` and `version_source`
alongside tool/model rows so benchmark readers can tell the difference between
exact configured provenance, profile-derived provenance, defaults, and missing
metadata.

For local CLI lanes, prefer explicit model environment variables such as
`CODE_MOWER_CODEX_MODEL`, `CODE_MOWER_GEMINI_MODEL`, or the provider's native
model variable when the CLI does not expose model identity through safe
metadata. Model identifiers are benchmark metadata, not secrets.

Hosted/manual reviewer lanes may report `model_source=vendor_hidden` and
`version_source=vendor_hidden` when the review service does not expose the
underlying model or app version. That is known provenance about the provider
surface, not a configured model id. Code Mower's own reporter events use
`model_source=not_applicable` because no AI model generated the operational
event. Local CLI or API lanes that omit a model remain `missing` until the user
configures the relevant model environment variable, provider profile, or
default.

Code Mower treats missing tool/model provenance as acceptable for operational
dogfood, but incomplete for benchmark claims. CodeMower.com therefore displays
provenance coverage separately from upload volume.

The OSS client fails closed for structured events that contain unsafe field
names such as raw output, transcripts, tokens, secrets, auth previews, or
secret-like values. Fix the event producer instead of relying on cloud upload
to silently scrub sensitive data.

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

## Data Controls In v0.5

Current controls:

- uploads are opt-in and dry-run-first;
- team ingest tokens can be revoked;
- full token values are not stored after creation; and
- report text is excluded unless explicitly included by the uploader;
- signed-in team members can export team metadata; and
- team owners/admins can delete uploaded metadata and related summaries/events.

Known gap:

- automated retention jobs and user-configurable retention windows are not
  implemented yet.

For early adopter pilots, deletion/export basics are live, but broad cloud-data
collection should wait until a published retention policy and automated
retention jobs are available.

## Roadmap

Before broad public adoption, Code Mower Cloud should add retention settings,
clearer anonymization/cohort rules, schema migration notes, and public examples
of useful aggregate benchmark outputs.
