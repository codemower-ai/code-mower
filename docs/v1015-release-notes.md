# Code Mower v1.0.15 Release Notes

Code Mower v1.0.15 is a measurement and release-operations reliability
release. Current-state productivity metrics now prefer live GitHub data,
historical Board observations are clearly labeled, provider credentials can
survive restarts through fail-closed profiles, and the metadata-only evidence
contract gains reviewer finding outcomes, normalized productivity windows, and
PR cost coverage.

Install the pinned package:

```bash
export CODE_MOWER_PYTHON="$(command -v python3.12)"
pipx install --python "$CODE_MOWER_PYTHON" code-mower==1.0.15
code-mower --version
```

Hosted agents and CI boxes can use:

```bash
uv tool install --python 3.12 code-mower==1.0.15
code-mower --version
```

## Current State Means Current

`code-mower productivity report` now collects live lane status and uses it for
current open-PR and gate-alert totals. Historical Board snapshots remain part
of trend and quality analysis, but they no longer override an available live
GitHub observation. Text and JSON output identify whether current state came
from `live_remote` or `historical_board`, include its observation time, and mark
historical fallback explicitly. Use `--offline` when that fallback is the
intended source (issue #793, PR #794).

## Evidence And Cost Coverage

- The additive `reviewer_finding_outcome` event records blocker-level
  dispositions such as accepted-and-fixed, false positive, accepted risk, or
  owner decision. Stable opaque identifiers preserve linkage without uploading
  finding prose, source, diffs, transcripts, or file paths (issue #736,
  PR #787).
- Deterministic normalized productivity windows keep cycle, active, queue,
  review, time-to-green, merge, and owner-wait timing separate. Coverage and
  provenance are explicit, missing values remain unavailable rather than zero,
  and the contract makes no causal claim (issue #738, PR #788).
- `code-mower cloud pr-outcomes` joins builder runs, reviewer-spend evidence,
  and live GitHub metadata into one event per PR. Cost coverage is reported as
  complete, partial, or unknown, with metadata-only missing-cost source ids
  (issue #737, PR #790).

These event additions remain backward-compatible with earlier v0.x and v1.0
uploads. Dashboard consumers can adopt them incrementally.

## Restart-Safe Provider Credentials

Provider credentials now use one fail-closed resolver. Ambient environment
variables win, followed by an explicit profile or file, then unambiguous local
profile discovery under `~/.config/code-mower/`. Credential files must be mode
`0600` or `0400`; ambiguous, malformed, or insecure profiles produce bounded
remediation without exposing values or private paths. Hosted Devin is the first
consumer, and doctor reports the same resolution outcome used by campaigns
(issue #785, PR #789).

## Release Qualification

A release campaign can record one explicit linked release PR with
`--release-pr`. Hosted result discovery reads the campaign issue and that exact
PR only, applies identical trusted-author and closed-schema checks on both, and
deduplicates identical evidence. It never searches arbitrary issues or pull
requests, and stored source metadata contains no comment or issue body text
(issue #791, PR #792).

Each provider still returns the closed `code_mower.adoptionResult.v1` artifact.
An explicit campaign upload converts terminal results into additive
`adoption_run` events; result discovery itself performs no upload.

Qualify one provider environment directly when a full campaign is unnecessary:

```bash
code-mower release qualify \
  --release-tag v1.0.15 \
  --package-spec code-mower==1.0.15 \
  --output adoption-result.json \
  --execute
```

Preview a campaign, then apply only after adoption doctor is ready:

```bash
code-mower release campaign create \
  --release-tag v1.0.15 \
  --package-spec code-mower==1.0.15 \
  --providers claude,codex,antigravity,muse,cursor_cloud_agent,devin \
  --required-providers claude,codex \
  --repo-slug OWNER/REPO

code-mower doctor --adoption --repo OWNER/REPO
code-mower release campaign dispatch \
  --release-tag v1.0.15 \
  --required-providers claude,codex \
  --release-pr PR_NUMBER \
  --apply \
  --repo-slug OWNER/REPO \
  --issue ISSUE_NUMBER
```

Preview the closed cloud bundle before any upload:

```bash
code-mower release campaign upload --release-tag v1.0.15 --json
code-mower release campaign upload --release-tag v1.0.15 --yes --json
```

Cloud sharing remains opt-in and dry-run first. No command in this release
uploads source, raw diffs, prompts, transcripts, issue body text, raw provider
output, authentication output, local paths, or secrets.

## Recommended Update

For an existing pipx install:

```bash
export CODE_MOWER_PYTHON="$(command -v python3.12)"
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" code-mower==1.0.15
code-mower --version
code-mower board list
```

For hosted agents using uv:

```bash
uv tool install --python 3.12 --reinstall --refresh-package code-mower code-mower==1.0.15
code-mower --version
```

Restart any Board still serving an older package. For existing repositories,
review `migration setup-drift` output in a pull request before applying
generated changes.

## Quality And Privacy Proof

Each behavior change landed in its own pull request with focused tests and peer
review. The v1.0.15 release PR runs package CI across Python 3.12, 3.13, and
3.14, release readiness, generated-workflow checks, privacy scanning, and an
author-excluded peer audit before publication.
