# Changelog

All notable public Code Mower OSS changes should be summarized here. The
project uses alpha tags while the first-user setup path, provider posture, and
optional cloud sharing loop are still hardening.

## v0.5.0-alpha.10

This alpha fixes the first-user cloud dogfood preview path after alpha.9 exposed
that a production dry run still failed without a token. Dry runs now remain
network-safe and token-optional, while confirmed uploads still require an
explicit token.

### Changed

- `code-mower cloud dogfood --json` no longer fails when targeting
  `https://codemower.com/api/ingest` without `CODE_MOWER_CLOUD_TOKEN`, as long
  as `--yes` is not supplied.
- Cloud doctor can distinguish upload-readiness checks from dry-run previews, so
  missing tokens are warnings for previews and failures for confirmed uploads.
- Public install docs now point to `v0.5.0-alpha.10`.

### Fixed

- First-user dogfood dry runs now match the documented privacy posture: no token
  is required to inspect the metadata-only bundle, and no network upload occurs
  without `--yes`.

## v0.5.0-alpha.9

This alpha hardens the first-useful-report path. The package-install rehearsal
now proves that a fresh install can bootstrap a draft project corpus from PR
metadata and generate a draft reviewer value report before any paid or
networked lane is enabled.

### Added

- `code-mower next-steps` now recommends `calibration auto-discover` between
  first calibration runs and the first reviewer value report.
- Package-install rehearsal now writes an offline PR-list fixture, runs
  `calibration auto-discover`, and round-trips the generated draft corpus
  through `calibration value-report`.
- Alpha.9 first-user install rehearsal transcript covering the draft corpus and
  draft value-report artifacts.

### Changed

- Public install docs now point to `v0.5.0-alpha.9`.
- Public release docs now treat calibration auto-discovery as part of the
  release gate, while still requiring human disposition review before lane
  promotion.

## v0.5.0-alpha.8

This alpha hardens the first-user trust path: clearer package-index release
steps, tested cloud dogfood defaults, and a recorded fresh-install rehearsal.

### Added

- `cloud_client.dogfood` helpers for routine dogfood report discovery and
  dry-run preview shape.
- Public CLI contract tests for `code-mower cloud dogfood` dry-run default
  behavior and `code-mower cloud setup` token redaction.
- PyPI/TestPyPI release runbook for moving from GitHub-tag installs to package
  index installs once trusted publishing is configured.
- Alpha.8 first-user install rehearsal transcript.

### Changed

- Public install docs now point to `v0.5.0-alpha.8`.
- Cloud dogfood docs more clearly distinguish preview-by-default from
  confirmed upload with `--yes`.
- Packaging metadata now uses the modern SPDX license string form, removing a
  setuptools release-build deprecation warning.

## v0.5.0-alpha.7

This alpha tightens the early-adopter handoff from local reports into the
optional CodeMower.com dogfood loop.

### Added

- `code-mower next-steps` now includes the routine `cloud dogfood` dry-run and
  confirmed-upload commands after the lower-level bundle upload preview.
- Easy-mode smoke rehearsal coverage for `code-mower cloud dogfood`,
  so release checks exercise the dashboard-oriented metadata path.

### Changed

- Public install docs now point to `v0.5.0-alpha.7`.
- Cloud sharing docs more clearly separate one-off bundle upload from routine
  dogfood metadata uploads.

## v0.5.0-alpha.5

This alpha sharpens the first-user preflight path based on fresh-eyes feedback
from the public repo and CodeMower.com onboarding flow.

### Added

- `code-mower doctor --preflight` as a friendlier alias for the v0.5
  early-adopter doctor preset.
- Release-hygiene tests proving preflight defaults and tokenless cloud
  upload dry-run behavior.

### Changed

- First-user docs now lead with `doctor --preflight` while keeping
  `doctor --v05` as the versioned scripting alias.

## v0.5.0-alpha.4

This alpha adds the public release plumbing needed before inviting a wider
friendly-user cohort.

### Added

- GitHub release workflow that builds source/wheel distributions and can publish
  to PyPI after trusted publishing is configured.
- Reviewer-metrics core tests covering spend, latency, event-log aggregation,
  and unsupported calibration report modes.
- Alpha.4 first-run rehearsal transcript for the public org repository.

### Changed

- Version and public install docs now point to `v0.5.0-alpha.4`.
- Public release checklist now treats PyPI packaging as present but gated until
  repository publishing credentials are configured.

### Known Limitations

- GitHub tag install remains the primary early-adopter install path until PyPI
  trusted publishing is enabled for `codemower-ai/code-mower`.

## v0.5.0-alpha.3

This is the first public alpha intended to be shared from the
`codemower-ai/code-mower` organization.

### Added

- Public-first README copy with concrete `doctor --v05` output.
- A sample doctor transcript in `docs/sample-doctor-output.md`.
- A 10-minute first-run path that starts with GitHub install, `init --easy`,
  `doctor --v05`, and a starter value report.
- Optional CodeMower.com cloud-sharing docs with metadata-only privacy
  boundaries.
- Conservative Ruff linting in CI (`E`/`F`, with line length formatting left
  alone for now).
- Fresh-clone and easy-mode smoke rehearsals for release validation.

### Changed

- Public documentation now points at
  `https://github.com/codemower-ai/code-mower` instead of the earlier personal
  repository.
- Cloud sharing is framed as optional: local audits, local value reports, and
  dry-run upload checks do not require a CodeMower.com account.
- Provider guidance is conservative by default. Codex and Claude are the first
  structured local audit lanes; other providers start informational or manual
  until calibration evidence supports promotion.

### Known Limitations

- PyPI publishing is not live yet. Install from the tagged GitHub repository.
- Code Mower is GitHub-first; GitLab, Bitbucket, and ACP bridges are roadmap
  items.
- Some provider integrations are calibration/manual lanes, not production merge
  gates.
- The hosted benchmark dashboard is early and metadata-first. Cohort
  benchmarking becomes more valuable as more teams opt in.
- Large extraction-era modules remain readable but should be decomposed before
  v1.0 where that improves contributor onboarding.
