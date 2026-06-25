# Code Mower Cloud Benchmarking

The OSS package should be useful without any cloud dependency. Cloud sharing is
the premium path for longitudinal reporting, cross-team benchmarks, and
hosted recommendations.

## Product Role

Local Code Mower answers:

- Which AI reviewers are useful on this repository?
- Which lanes should be informational, selective, or merge-gating?
- What quality, speed, and cost results are visible from local artifacts?

Cloud Code Mower can add:

- trends over time
- team dashboards
- benchmark cohorts by language, repo size, and task class
- recommendations from larger anonymized populations
- hosted value reports for builder plus reviewer loops
- cross-tier comparisons between local CLIs, hosted async agents, SaaS
  reviewers, and local/API model lanes

Code Mower Cloud should learn from span/trace/score evaluation systems without
making any of them required for the OSS package. The local bundle should be
portable enough to upload to Code Mower Cloud later or transform into another
observability/evaluation backend.

## Release Stages

### v0.5: Early-Adopter Upload Alpha

The v0.5 path adds an opt-in upload client for early adopters who explicitly
want to share sanitized benchmark metadata with Code Mower Cloud. The OSS tool
is still local-first:

```bash
code-mower cloud upload .code-mower/cloud-benchmark-bundle --dry-run --json
code-mower cloud upload .code-mower/cloud-benchmark-bundle --yes --json
```

Dry run is the first-class experience. A network upload only happens when the
caller passes `--yes`, and the default payload is metadata-only. Rich report
text requires `--include-reports`.

### v1.0: Local-First, Cloud-Ready

Ship no network upload by default. The local package may include an opt-in
upload alpha, but a user should not need a Code Mower Cloud account to get
value from install, doctor, first audit, calibration, value reports, or cloud
export.

Provide:

```bash
code-mower cloud export \
  --report reviewer-metrics=reviewer-metrics.json \
  --report lane-policy=lane-policy.json \
  --report value-report=reviewer-value-report.md \
  --output-dir .code-mower/cloud-benchmark-bundle \
  --json
```

The export command creates a local bundle manifest, README, and copied report
files. It does not upload anything.

Reports and future bundle extensions should use the cloud vocabulary:

- trace: one PR, builder session, or calibration case
- span: one builder or reviewer run inside a trace
- score: one adjudicated finding, useful concern, clean pass, miss, or
  post-merge health result
- dataset: a starter or team calibration corpus
- experiment: a repeatable provider, lens, or tier comparison

The current v1.0 manifest remains intentionally small. Do not document a field
as part of the manifest until the exporter emits it and tests cover it.

### v1.1: Opt-In Upload Beta

Promote upload beyond alpha only after the bundle schema and hosted ingestion
path have real early-adopter mileage:

```bash
open https://codemower.com/login
code-mower cloud upload --dry-run
code-mower cloud upload
```

Upload must continue to show exactly what will be sent before transfer. A dry
run should remain the default first experience.

The upload beta should support metadata-only uploads first. Rich report files,
public-repo slugs, and team identity should each be separate opt-ins. Human
users manage team ingest tokens from:

```text
https://codemower.com/dashboard
```

### v1.2: Premium Reporting

Hosted reporting can include:

- best builders for this repo
- best reviewers by task class
- cost per useful finding
- cost per merged feature
- false-positive interruption rate
- post-merge health trends
- lane promotion recommendations
- benchmark percentile against similar repos
- provider-tier comparisons such as local CLI vs hosted async vs local model
- lens comparisons such as base audit vs security vs operability

## Default Privacy Boundary

The cloud bundle excludes these by default:

- source code
- raw diffs
- raw model transcripts
- raw stdout/stderr
- auth probe output
- secrets

Default shareable fields should be metadata and summaries:

- provider id and lane id
- task class
- repo-size and language buckets
- verdict counts by severity
- disposition counts
- useful-rate, precision, miss-rate, and clean-pass counts
- elapsed time and cost estimates
- merge and post-merge health
- trace/span/score ids that are random or install-scoped, not content-derived

Do not persist content-derived fingerprints of redacted auth output. Even a
hash can leak account-state correlation for predictable CLI output.

## Consent Model

Every cloud path should be explicit:

- `cloud export`: local only
- `cloud upload --dry-run`: no transfer, prints manifest
- `cloud upload --yes`: requires a team token and explicit confirmation

Support modes:

- `--anonymous`: remove repo and team identifiers
- `--team`: attach to an authenticated team
- `--public-repo`: allow public repository slug
- `--include-reports`: future opt-in for richer report files

## Bundle Schema

The v1.0 local bundle manifest uses:

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

The bundle is intentionally conservative. Premium cloud value should come from
aggregation, longitudinal history, and comparison, not from requiring users to
share source code.

Future upload-ready manifests can add explicit `traces`, `spans`, `scores`,
`datasets`, and `experiments` arrays after `code_mower_cloud.py` emits them and
the schema tests cover them.

## Premium Product Path

The cloud service becomes valuable when it can answer questions local reports
cannot answer alone:

- Which AI builders and reviewers are improving over time on this codebase?
- Which provider tier is best value for this repo and task class?
- Which review lenses produce useful independent signal without false-positive
  drag?
- Which setup resembles this repo and has a better lane policy?
- What is the cost of one useful finding or one safely merged feature?

OSS v1.0 should therefore make local data capture clean and privacy-preserving.
Revenue work begins with opt-in upload, longitudinal dashboards, team reports,
and aggregated recommendations after the local schema has real users.
