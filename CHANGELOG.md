# Changelog

All notable public Code Mower OSS changes should be summarized here. The
project uses alpha tags while the first-user setup path, provider posture, and
optional cloud sharing loop are still hardening.

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
