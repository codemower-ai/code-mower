# Cloud Data Contract

Code Mower Cloud sharing is optional. The local OSS package remains useful
without a CodeMower.com account or token.

This document defines the public metadata-only data boundary for Code Mower
Cloud uploads.

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

## Local Board Status

`code-mower lanes status --repo OWNER/REPO` and
`code-mower board serve --repo OWNER/REPO` use local schemas
`code_mower.laneStatus.v1` and `code_mower.board.v1`. The explicit local
history commands use `code_mower.boardEvent.v1` and
`code_mower.boardEventStore.v1` under `.code-mower/board/events.jsonl`, and
`code_mower.boardRecord.v1` for explicit write acknowledgements. Optional local
agent card adapters use `code_mower.boardAgentAdapters.v1` from
`.code-mower/board/agents/*.json`. Board admin commands use
`code_mower.boardDoctor.v1` for local diagnostics and
`code_mower.boardReset.v1` for explicit local-history reset acknowledgements.
`code-mower productivity report --repo OWNER/REPO` emits local-only
`code_mower.productivityReport.v1` by reading Board history, reviewer-spend
rows, and optional aggregate `productivity_summary` event files.
Those payloads are operator visibility data, not cloud upload data.

Current board/status JSON and local board event-store data are local-only and
are not uploaded by default. The explicit
`code-mower cloud board-snapshot --repo-slug OWNER/REPO --json` command exports
one summarized `board_snapshot` event with zero report text; adding `--yes`
uploads that metadata-only summary. Any future dashboard mirror expansion must
land as a paired OSS and dashboard change, update both this contract and
[Board Data Contract](board-data-contract.md), keep the hosted service
backward-compatible with v0.6/v0.7 uploads, and preserve the metadata-only
boundary: no source, raw diffs, transcripts, issue body text, raw stdout/stderr,
auth output, browser history, local secret values, or secrets.

## Structured Events

Metadata events use schema `code_mower.benchmarkEvent.v1`. They are intended to
capture reviewer and workflow facts without raw code artifacts.

Supported event types include:

- `adoption_run`
- `dogfood_upload`
- `builder_run`
- `reviewer_run`
- `calibration_run`
- `value_report_snapshot`
- `lane_policy_snapshot`
- `provider_catalog_snapshot`
- `work_order`
- `workflow_run`
- `board_snapshot`
- `controller_decision`
- `merge_decision`
- `queue_state_snapshot`
- `owner_intervention`
- `pr_outcome`
- `productivity_summary`

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

`board_snapshot` events are explicit CodeMower.com Board mirror metadata. They
use `dimensions.snapshot_schema=code_mower.cloudBoardSnapshot.v1`, include zero
reports, and summarize the local Board/status surface into whitelisted
metadata: repository, generated time, next action, remote availability, gate
status, PR number/branch/author/draft/merge/check/label summaries, workflow run
summaries, owner-queue reason summaries, opt-in agent card summaries, and
verdict/spend group counts. In v1.0, the same event may also include
`dimensions.supervised_pilot`, a compact controller-backed summary with the
supervised-pilot schema, controller mode, cycle state, decision state,
stop condition, next action/detail, lane/check references, reviewer outcome
states, queue metrics, active lane counts, active PR metadata, and active ready
issue metadata. Matching top-level metrics may include
`supervised_open_pr_count`, `supervised_ready_issue_count`, and
`supervised_owner_action_count`. The uploader intentionally does not send the
full local Board payload, PR titles, owner note titles, issue titles, local cwd
paths, PIDs, full head SHAs, gate rerun commands, source, raw diffs,
transcripts, issue body text, raw stdout/stderr, auth output, browser history,
local secret values, or secrets. The event type and supervised-pilot fields are
additive and optional, so CodeMower.com must continue accepting v0.6/v0.7/v0.9
uploads that omit them.

Supervised-pilot events are additive v1.0 metadata for controller and merge
visibility. `controller_decision`, `merge_decision`, `queue_state_snapshot`,
and `owner_intervention` events use
`dimensions.supervised_pilot_schema=code_mower.supervisedPilot.v1` and record
state, next action, lane/check references, owner-action reasons, and coarse
counts without raw work content. See
[Supervised Pilot Contract](supervised-pilot-contract.md) for the product
boundary, stop conditions, reviewer outcome references, example fixtures, and
privacy rules. CodeMower.com must keep accepting v0.9.x uploads that omit these
event types.

## PR Outcome And Cost

`pr_outcome` is the additive atomic event for Dashboard 2.0. It uses the normal
`code_mower.benchmarkEvent.v1` envelope with
`dimensions.pr_outcome_schema=code_mower.prOutcome.v1`. One event describes one
PR observation; `repo_slug` plus `dimensions.pr_number` is the PR identity.
Producers should make retries idempotent with the same `event_id`. When a newer
lifecycle observation uses another event id, consumers select the latest
`created_at` observation per PR before aggregating.

Required dimensions are `pr_outcome_schema`, positive integer-string
`pr_number`, ISO 8601 `opened_at`, `outcome` (`open`, `merged`,
`closed_unmerged`, or `reverted`), and `cost_coverage` (`complete`, `partial`,
or `unknown`). Merged and reverted outcomes require `merged_at`, closed-unmerged
requires `closed_at`, and reverted requires `reverted_at`. Timestamps include a
UTC offset and cannot precede `opened_at`; `reverted_at` cannot precede
`merged_at`.

Metrics are atomic values, never precomputed dashboard rates:

- `pr_count` is always `1`; optional `fix_round_count`,
  `reviewer_catch_count`, and `blocking_bug_count` are non-negative integers.
- `reported_cost_usd` is the observed builder-plus-reviewer spend for the PR.
  `cost_reported_run_count` and `cost_expected_run_count` expose source coverage.
- `cost_covered_pr_count` is `1` only for complete cost coverage and `0`
  otherwise. Partial coverage requires `0 < reported runs < expected runs`.
  Unknown coverage omits `reported_cost_usd`; missing cost is never zero.

Dashboard Total Spend may sum `reported_cost_usd` across complete and partial
observations while displaying their coverage mix. Dashboard Cost per PR uses
only merged observations with `cost_coverage=complete` and computes
`sum(reported_cost_usd) / sum(cost_covered_pr_count)`. No `cost_per_pr` value is
uploaded. Events that omit `pr_outcome`, including v0.6 through v1.0 uploads,
remain valid.

The event contains identifiers, timestamps, categorical outcomes, and numeric
counts/cost only. Its dimension and metric names are closed in v1, so undeclared
fields are rejected rather than becoming accidental prose channels. It must not
contain PR or issue prose, source, diffs, prompts, transcripts, issue body text,
raw stdout/stderr, auth output, local paths, or secrets.

## Work Type Taxonomy And Lane Attribution

Work-type dimensions are additive, versioned metadata that may be attached
only to `builder_run`, `reviewer_run`, `work_order`, and `productivity_summary`
events so Dashboard 2.0 can compare builders and reviewers by development
work type. They use `dimensions.work_type_schema=code_mower.workType.v1`.
Events that omit these dimensions, including all uploads before this
contract, remain valid; validation only runs when `work_type_schema` is
present. `work_type_schema` on any other event type (for example
`workflow_run` or `dogfood_upload`) is rejected outright.

`dimensions.work_type` is one of `web`, `backend`, `ios`, `macos`, `android`,
`infrastructure`, `documentation`, or `unknown`. `dimensions.work_type_source`
records classification precedence: `explicit_user` metadata wins first,
deterministic `repository_metadata` (for example a GitHub primary language)
or coarse `file_category_metadata` (a bucket label such as `web-frontend`,
never a filename) is checked second, and `unknown` otherwise. `work_type`
`unknown` requires source `unknown` or `explicit_user`.

`dimensions.work_type_role` is event-shape-pinned, not free text:
`builder_run` events must record `work_type_role=builder` and
`reviewer_run` events must record `work_type_role=reviewer`; any other value
on those event types is rejected rather than silently reattributed.
`work_order` and `productivity_summary` events leave `work_type_role`
optional and it is never guessed — when absent, `work_type_attribution` must
also be absent.

When `work_type_role` is present it stays distinct from
`dimensions.work_type_attribution`, which is `builder_credit`,
`reviewer_credit`, or `excluded_self_review`. Builder role requires
`builder_credit`; reviewer role must use `reviewer_credit` or
`excluded_self_review`. When `work_type_lane_id` equals
`work_type_builder_lane_id`, an author lane cannot count as independent
review: the reviewer role must record `excluded_self_review` rather than
`reviewer_credit`.

Optional `work_type_provider` and `work_type_model` mirror the same
provider/model identity already carried on `tool`. When both the work-type
identity and `tool.provider`/`tool.model` are present, they must agree after
the same whitespace-collapsing normalization tool provenance already uses;
disagreement is rejected. Either side may be omitted, which keeps
provider/model-free work-type events backward-compatible.

Work-type metadata must not include filenames, source, diffs, prompts, or
issue text; repository and file-category inputs are already-coarse category
labels, never paths.

## Adoption Run And Release Qualification

`adoption_run` is the additive atomic event for release-qualification
campaigns. It uses the normal `code_mower.benchmarkEvent.v1` envelope with
`dimensions.adoption_run_schema=code_mower.adoptionRun.v1`. One event describes
one `code_mower.adoptionResult.v1` observation produced by
`code-mower release qualify`; the release identity is `release_tag` plus
`normalized_version`, `qualification_context`, and `provider`. The converter
derives a deterministic `event_id` from the source result content, so retrying
an export/upload of the same result file reuses the same event id and stays
idempotent. Newer observations of the same campaign use another event id, and
consumers select the latest `created_at` observation per release before
aggregating.

Two local routes produce these events, and both go through one converter, so
the same result always yields the same event id: `code-mower cloud dogfood
--event adoption_run=path/to/result.json` (and `cloud export`) converts one
result file at a time, while `code-mower release campaign upload` converts every
completed provider result a campaign holds. The campaign route previews by
default and posts only with `--yes`, using the identical event set both times;
providers that are not complete are counted as skipped, and a completed provider
whose stored result no longer validates stops the upload with a bounded error
instead of publishing a partial set.

Required dimensions are `adoption_run_schema`, `release_tag`
(`v<major>.<minor>.<patch>[-<stage>.<num>]`), `package_identity`
(`code-mower`), `normalized_version`, `qualification_context`
(`cold_install`, `upgrade`, or `unknown`), `provider`, `executor`,
`host_class` (`local`, `ci`, `github_actions`, or `unknown`),
`runtime_class` (`python_<major>.<minor>` or `unknown`), `execution_state`
(`planned` or `executed`), `outcome` (`pass`, `pass_with_warnings`, `fail`,
or `incomplete`), `result_timestamp` (ISO 8601 with a UTC offset), and
`provenance_coverage` (`complete`, `partial`, or `unknown`). Optional
dimensions are `starting_version` and `ending_version`, which must be empty or
normalized versions. Tag and spec versions must agree: the tag-derived
normalized version must equal `normalized_version`. Upgrade context requires a
`starting_version` lower than the target; other contexts must leave it empty.
Executed runs must not report `incomplete`, and planned runs must report
`incomplete` or `fail`. Complete provenance coverage requires a known
provider, executor, host class, and runtime class.

Metrics are atomic values, never precomputed dashboard rates:

- `adoption_run_count` is always `1`; `step_count` is the observed step total,
  and `step_pass_count`, `step_warn_count`, `step_fail_count`,
  `step_unavailable_count`, and `step_planned_count` are non-negative integers
  that must sum to `step_count`.
- `elapsed_seconds` is the observed qualification wall time and must be finite
  and non-negative. Step timings must sum to within 1.0 second of this total
  (rounding/overhead tolerance).
- `warning_count` and `owner_action_count` are non-negative integer summaries
  across steps. A `pass` outcome requires zero owner actions, matching the
  local adoption-result contract.

Conversion applies the same semantic validation as local campaign paths:
executed-result timestamp bounds (not older than `2020-01-01T00:00:00Z`, not
more than 300 seconds in the future; planned previews exempt), the built-in
step taxonomy (`board`, `doctor`, `lanes_status`, `overhead`,
`package_install`) or an
explicit `<namespace>__<name>` provider extension, and the timing and
owner-action rules above. Rejections use bounded errors that never echo result
content, paths, auth output, or raw provider output.

Missing model, token, cost, and optional measurements stay unavailable and
omitted, never zero-filled: the closed metric set contains no cost, token, or
model metrics, and the reporter tool provenance uses
`model_source=not_applicable` because no AI model generated the operational
event. No `cost_per_run` or dashboard rate is uploaded.

The event contains identifiers, coarse environment classes, categorical
outcomes, and numeric counts/timings only. Its dimension and metric names are
closed in v1, so undeclared fields are rejected rather than becoming accidental
prose, path, or output channels. Dimensions must also stay single-line and
path-free. It must not contain report text (never uploaded by default),
command output, source, diffs, prompts, transcripts, issue body text, raw
stdout/stderr, auth output, local paths, or secrets. Events that omit
`adoption_run`, including v0.6 through v1.0.4 uploads, remain valid, and
CodeMower.com must keep accepting uploads that omit this event type.

## Productivity Metrics

`productivity_summary` is the v1.0.1 aggregate metric event for local reports
and CodeMower.com dashboards. It uses the normal
`code_mower.benchmarkEvent.v1` envelope with
`dimensions.productivity_schema=code_mower.productivityMetrics.v1`. It is an
aggregate snapshot, not a raw trace: dashboards may group it by repository,
lane, provider, builder, reviewer, issue, PR, or release without receiving
source, diffs, prompts, transcripts, issue body text, raw stdout/stderr, auth
output, local paths, or secrets.

Required dimensions:

- `productivity_schema`: `code_mower.productivityMetrics.v1`;
- `repo_slug`: `OWNER/REPO`;
- `window_start` and `window_end`: UTC timestamps for the measured window;
- `window_granularity`: `cycle`, `day`, `week`, `release`, or `custom`; and
- `aggregation_subject`: `repo`, `lane`, `provider`, `builder`, `reviewer`,
  `issue`, `pr`, or `release`.

Optional dimensions are intentionally small and metadata-only:
`aggregation_key`, `lane_id`, `provider`, `builder_provider`,
`reviewer_provider`, `role`, `issue_number`, `pr_number`, `branch`, `release`,
`pilot_posture`, `policy_state`, `merge_state`, `verdict`, `manual_outcome`,
`automated_vs_manual`, `owner_action_reason`, and `event_source`.

Metric names and units are stable:

- Time metrics use seconds: `cycle_time_seconds`, `active_time_seconds`,
  `wait_time_seconds`, `queue_wait_seconds`,
  `time_to_first_review_seconds`, `time_to_green_seconds`,
  `time_to_merge_seconds`, and `owner_wait_seconds`.
- Count metrics use integer counts: `builder_run_count`,
  `reviewer_run_count`, `audit_pass_count`, `audit_blocked_count`,
  `reviewer_catch_count`, `blocking_bug_count`, `blocked_finding_count`,
  `false_blocker_count`, `missed_blocker_count`, `fix_round_count`,
  `owner_intervention_count`, `manual_override_count`,
  `automerge_eligible_count`, `automerge_requested_count`,
  `automerge_completed_count`, `merged_pr_count`, `abandoned_pr_count`,
  `reverted_pr_count`, `checks_failed_count`, `checks_passed_count`, and
  `post_merge_defect_count`.
- Cost metrics use US dollars: `cost_usd`.
- Token metrics use integer token counts: `input_tokens`, `output_tokens`,
  `total_tokens`, `cached_input_tokens`, and `reasoning_tokens`.

Missing metrics mean unknown, not zero. Derived percentiles, rates, and
rankings are report/dashboard calculations and should reference the source
metric names used to compute them. The OSS validator rejects unknown
`productivity_summary` metric names, negative or non-finite values, and
fractional count/token values so future producers do not drift from the
contract accidentally. CodeMower.com must continue accepting v0.9.x and v1.0
uploads that omit `productivity_summary`.
Consumers may total count, token, and cost metrics across multiple
`productivity_summary` events only within one headline aggregation subject. The
recommended headline subject priority is `repo`, then `release`, `issue`, then
`pr`. Time metrics describe a measured window and must stay latest-window or
unknown unless a producer emits an explicit aggregate window event. Provider-,
builder-, reviewer-, and lane-scoped `productivity_summary` events are
scorecard inputs; consumers should not add them into headline repo/release
totals for the same window. Scorecard promotion recommendations remain advisory
until reviewed against `docs/lane-promotion-policy.md`.

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

`cloud setup` writes a sourceable `0600` env file and records it as the current
local cloud profile. Upload commands resolve tokens from the live env first,
then explicit `--token-file`, then `--install-id`, then the current profile, then
one unambiguous stored profile. If multiple stored token files exist without a
current selection, Code Mower refuses to guess and reports filenames only.

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
code-mower cloud upload .code-mower/cloud-benchmark-bundle --yes --json
```

## Hosted Storage In The Current Beta

The current hosted service stores upload ids and timestamps, token/team
linkage, repository slug when supplied, report summaries and counts, structured
metadata events, cost/latency/usefulness fields when supplied, and
recommendation inputs derived from metadata.

It should not store source, raw diffs, raw transcripts, stdout/stderr, auth
output, or secrets by default.

## Data Controls In The Current Beta

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
