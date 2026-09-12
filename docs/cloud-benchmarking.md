# Code Mower Cloud Benchmarking

The OSS package provides installation, diagnostics, review, calibration, local
reports, and the local Board without a hosted account. CodeMower.com is an
optional destination for longitudinal team reporting and future aggregate
benchmarks.

## Current v1.3.1 Surface

The current client can:

- build an inspectable local bundle;
- preview uploads without network transfer;
- upload only after an explicit `--yes`;
- send metadata-only Board snapshots with zero reports;
- send aggregate productivity summaries and provider scorecards;
- import bounded GitHub Actions history as history rather than calibration
  evidence; and
- upload release-qualification outcomes separately from builder-quality and
  reviewer-promotion evidence.

Representative commands:

```bash
code-mower productivity report --repo OWNER/REPO --json
code-mower cloud export --repo-slug OWNER/REPO --json
code-mower cloud upload .code-mower/cloud-benchmark-bundle --dry-run --json
code-mower cloud dogfood --json
code-mower cloud board-snapshot --repo-slug OWNER/REPO --json
```

The dry run is the first operation. A network upload requires `--yes`. Report
text is excluded unless the caller explicitly selects it; a Board snapshot
always contains zero reports.

## What Local And Hosted Views Answer

Local reports can answer:

- Which reviewer caught known blockers?
- Which reviewer stayed quiet on known-clean cases?
- What cost, latency, fix rounds, and interventions were observed?
- Which lanes have enough local evidence to consider for promotion?

The hosted service can add private team history, cross-repository rollups,
evidence drill-down, and repeated provider/lens comparisons. Cross-team cohorts
and recommendations from aggregate populations are future capabilities and
must not be presented as current product value.

## Data Classes

The public data contract distinguishes:

- **operational activity:** current runs and uploads;
- **imported history:** prior workflow activity that was not adjudicated;
- **calibration evidence:** known-clean/known-blocked cases with reviewed truth;
- **builder runs:** source-free authoring provenance and outcomes;
- **reviewer outcomes:** verdicts and finding dispositions; and
- **productivity summaries:** aggregate counts, rates, time, cost, and outcome
  fields.

These classes must remain separate in dashboards and analysis. A successful
transport or installation run is not reviewer-quality evidence.

## Default Privacy Boundary

Default bundles exclude:

- source code;
- raw diffs;
- model prompts and transcripts;
- raw stdout/stderr;
- auth output;
- issue body text;
- credentials and secret values; and
- private organizational context and account bindings.

Safe fields include bounded provider/lane identifiers, task classes,
repository buckets, verdict and disposition counts, elapsed time, known cost,
merge/post-merge outcomes, and random or installation-scoped event IDs.

Do not persist content-derived fingerprints of redacted auth output. Even a
hash can correlate predictable account state.

## Consent Model

| Command | Network behavior |
| --- | --- |
| `cloud export` | Local files only |
| `cloud upload --dry-run` | Validates and previews; no transfer |
| `cloud dogfood --json` | Builds the preview; no transfer |
| `cloud board-snapshot --json` | Builds a zero-report preview; no transfer |
| Any supported upload command with `--yes` | Transfers the validated payload using the selected team token |

Repository/team identity and rich reports are separate choices. Inspect the
manifest before uploading.

## Bundle Contract

The local manifest uses `code_mower.cloudBenchmarkBundle.v1`. Its core privacy
shape is:

```json
{
  "schema": "code_mower.cloudBenchmarkBundle.v1",
  "privacy_mode": "metadata_and_reports",
  "upload_ready": true,
  "upload_status": "ready_for_dry_run",
  "included_reports": [],
  "excluded_content": [
    "source_code",
    "raw_diffs",
    "raw_model_transcripts",
    "raw_stdout_stderr",
    "auth_probe_output",
    "secrets"
  ]
}
```

The complete additive event and backward-compatibility rules are in the
[Cloud Data Contract](cloud-data-contract.md).

## Near-Term Direction

Hosted work should prioritize reliable private-team usefulness: clear evidence
provenance, understandable recommendations, retention automation, and stable
export/deletion. Aggregate cohorts should follow only after enough consenting
teams contribute comparable, adjudicated evidence.

Slack task ingress and Graphify repository context do not widen the cloud
contract by implication. Each needs an explicit privacy and event decision
before any new field can enter a bundle.
