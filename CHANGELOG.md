# Changelog

All notable public Code Mower OSS changes should be summarized here. The
project uses alpha tags while the first-user setup path, provider posture, and
optional cloud sharing loop are still hardening.

## v0.5.0-alpha.55

This alpha is the public package marker after extracting CodeMower.com Git
metadata helpers.

### Changed

- Cloud Git remote metadata helpers now live in
  `code_mower.cloud_client.git_metadata`.
- Cloud event construction keeps building dogfood events while importing the
  Git helper from the narrower cloud metadata boundary.
- Package manifests now include the cloud Git metadata helper.
- Added focused unit coverage for GitHub remote slug parsing, non-repository
  fallback behavior, and best-effort Git command output.
- Public install and release-readiness docs now point to
  `v0.5.0-alpha.55`.

## v0.5.0-alpha.54

This alpha is the public package marker after extracting shared provider
process environment helpers.

### Changed

- Shared provider child-process environment construction now lives in
  `code_mower.provider_runners.process`.
- Gemini CLI, Hermes CLI, and CodeRabbit CLI wrappers use the shared helper for
  allowlisted ambient variables, isolated HOME/XDG paths, and provider-specific
  explicit environment values.
- Package manifests now include the shared provider process helper.
- Added focused unit coverage for ambient env filtering, isolated home handling,
  provider auth-key mapping, Hermes quiet flags, and CodeRabbit env allowlisting.
- Public install and release-readiness docs now point to
  `v0.5.0-alpha.54`.

## v0.5.0-alpha.53

This alpha is the public package marker after extracting shared provider git
helpers.

### Changed

- Shared provider-runner git helpers now live in
  `code_mower.provider_runners.git`.
- Gemini CLI and CodeRabbit CLI audit wrappers use the shared helper for local
  git command execution and HEAD lookup instead of keeping duplicate
  implementations.
- Package manifests now include the shared provider git helper.
- Added focused unit coverage for provider-runner git helper behavior.
- Public install and release-readiness docs now point to
  `v0.5.0-alpha.53`.

## v0.5.0-alpha.52

This alpha is the public package marker after extracting the doctor privacy
redaction helper.

### Changed

- Auth-probe output shape reporting now lives in
  `code_mower.doctor_checks.privacy`.
- Runtime and doctor compatibility exports still expose the redaction helper,
  while provider and GitHub doctor checks import it from the privacy boundary.
- Added focused `unittest` coverage that verifies auth probe details never
  preserve raw output content.
- Public install and release-readiness docs now point to
  `v0.5.0-alpha.52`.

## v0.5.0-alpha.51

This alpha is the public package marker after extracting shared doctor provider
auth smoke-probe helpers.

### Changed

- Provider auth smoke-probe JSON parsing and remediation now live in
  `code_mower.doctor_checks.provider_probe`.
- Local CLI doctor checks and provider registry exports share the same probe
  helper implementation.
- Added focused `unittest` coverage for noisy JSON extraction and auth-detail
  redaction.
- Public install and release-readiness docs now point to
  `v0.5.0-alpha.51`.

## v0.5.0-alpha.50

This alpha is the public package marker after extracting shared GitHub comment
formatting from the provider wrappers.

### Changed

- Shared audit comment truncation now lives in
  `code_mower.provider_runners.comments`.
- Codex and Claude audit wrappers both use the provider-runner comment limiter,
  preserving trailer state while avoiding duplicated GitHub comment-size logic.
- Added focused `unittest` coverage for comment truncation edge cases.
- Public install and release-readiness docs now point to
  `v0.5.0-alpha.50`.

## v0.5.0-alpha.49

This alpha is the public package marker after extracting shared repo-path
parsing from the provider wrappers.

### Changed

- Shared local repository path parsing now lives in
  `code_mower.provider_runners.repo_paths`.
- Codex and Claude audit wrappers both use the provider-runner parser, removing
  the remaining generic Claude dependency on Codex wrapper internals.
- Added focused `unittest` coverage for repo-path parsing so the package CI path
  does not depend on pytest.
- Public install and release-readiness docs now point to
  `v0.5.0-alpha.49`.

## v0.5.0-alpha.48

This alpha is the public package marker after the provider text/schema helper
extraction.

### Changed

- Shared provider text clipping, one-line sanitizing, and strict key-validation
  helpers now live in `code_mower.provider_runners.text_schema`.
- Codex and Claude audit wrappers both import those helpers from the
  provider-runner layer, removing another Claude dependency on Codex internals.
- Added focused unit coverage for the provider text/schema helper contract.
- Public install and release-readiness docs now point to
  `v0.5.0-alpha.48`.

## v0.5.0-alpha.47

This alpha is the public package marker after the first provider-runner seam
extractions.

### Changed

- Shared GitHub PR metadata and PR comment helpers now live in
  `code_mower.provider_runners.github_pr` instead of the Codex provider.
- Shared audit verdict artifact write/load/repost helpers now live in
  `code_mower.provider_runners.verdict_artifacts` instead of the Codex
  provider.
- Codex and Claude audit wrappers now import those provider-neutral helpers
  from `provider_runners`, reducing cross-provider coupling.
- Public install and release-readiness docs now point to
  `v0.5.0-alpha.47`.

## v0.5.0-alpha.46

This alpha is the next public package marker after the migration structure
hardening slices.

### Changed

- Package-install rehearsal internals now split install, venv, pip,
  command-runner, and toy-repo helpers into `code_mower.migration_install`.
- `code_mower.migration_rehearsal` now focuses on the fresh-repo rehearsal flow
  while preserving the previous compatibility import surface.
- Public install and release-readiness docs now point to
  `v0.5.0-alpha.46`.

## v0.5.0-alpha.45

This alpha continues the public-package structure hardening path after the
doctor and migration refactors.

### Changed

- GitHub doctor internals now split redacted `gh api` helpers from Actions
  billing/cost diagnostics, leaving repository setup orchestration in a smaller
  `github.py` module.
- Package-install rehearsals now keep first-user readiness artifacts and
  scorecards in `code_mower.migration_readiness`, while
  `code_mower.migration_rehearsal` stays focused on fresh-repo rehearsal
  execution.
- Public install and release-readiness docs now point to
  `v0.5.0-alpha.45`.

## v0.5.0-alpha.44

This alpha consolidates the recent structural cleanup into a cleaner public
package baseline for first-user rehearsals.

### Changed

- `code_mower.cloud` is now backed by `code_mower.cloud_client.operations` for
  dogfood upload, repo-sync, and reviewer-run orchestration.
- `code_mower.doctor` is now a smaller CLI adapter backed by split doctor
  modules for output rendering, first-run presets, package-aware template
  paths, and report orchestration.
- Package template/config path helpers moved into `code_mower.package_paths`,
  reducing package-materialization coupling while preserving the public command
  surface.
- Public install and release-readiness docs now point to
  `v0.5.0-alpha.44`.

## v0.5.0-alpha.21

This alpha makes package-index promotion setup more self-service.

### Changed

- `code-mower migration release-readiness` now reports setup URLs for the
  GitHub environments, release workflow, TestPyPI project, PyPI project, and
  trusted-publishing configuration pages.
- Release-readiness next actions now include relevant URLs in both JSON and text
  output.
- The PyPI release runbook documents those setup URLs directly.
- Public install docs now point to `v0.5.0-alpha.21`.

## v0.5.0-alpha.20

This alpha makes the full package-installed first-user rehearsal part of routine
public CI.

### Changed

- The main Code Mower CI job now runs
  `python -m code_mower.migration package-install-rehearsal` from the current
  checkout, proving the installed-package path in a fresh toy repository.
- `code-mower migration release-readiness --json` now fails if the CI
  package-install rehearsal gate is removed or weakened.
- Public release/readiness docs now treat the installed-package rehearsal as a
  routine PR gate, not only a manual pre-release habit.
- Public install docs now point to `v0.5.0-alpha.20`.

## v0.5.0-alpha.19

This alpha adds public maintainer hygiene for early adopters and makes release
readiness enforce the public support, security, and community-safety surface.

### Changed

- Added `SUPPORT.md` and `CODE_OF_CONDUCT.md`, with explicit guidance to avoid
  sharing tokens, private source, raw diffs, raw model transcripts, auth output,
  credentials, and customer data in public support channels.
- The README docs map now links support, security policy, and conduct docs.
- The source distribution now includes the support and conduct docs.
- `code-mower migration release-readiness --json` now verifies public
  maintainer docs are present, linked, and privacy-forward.
- Release hygiene tests now cover missing-doc, missing-link, and incomplete
  redaction-guidance failure paths.
- Public install docs now point to `v0.5.0-alpha.19`.

## v0.5.0-alpha.18

This alpha makes the package release-readiness check part of routine CI, so
future release candidates cannot drift from the package-index promotion gate.

### Changed

- The main Code Mower CI job now runs
  `python -m code_mower.migration release-readiness --json`.
- Release hygiene tests assert that the CI workflow keeps the release-readiness
  gate wired.
- Public install docs now point to `v0.5.0-alpha.18`.

## v0.5.0-alpha.17

This alpha adds a static release-readiness gate for package-index promotion,
so maintainers can verify the GitHub Release, TestPyPI, and PyPI plumbing
before cutting wider early-adopter releases.

### Changed

- `code-mower migration release-readiness --json` now checks package version
  consistency, release workflow gates, TestPyPI/PyPI publishing posture, and
  package-index rehearsal docs.
- The release-readiness gate verifies that the TestPyPI and PyPI jobs use the
  official `pypa/gh-action-pypi-publish` action, not just matching job names.
- Release hygiene tests now cover alpha, beta, release-candidate, and final
  tag derivation.
- Public install docs now point to `v0.5.0-alpha.17`.

## v0.5.0-alpha.16

This alpha adds the first package-index rehearsal lane for TestPyPI while
keeping production PyPI disabled by default.

### Changed

- The release workflow now has a dedicated `publish_testpypi` dispatch input
  and `publish-testpypi` job.
- TestPyPI publishing is gated by the separate `testpypi` GitHub environment
  and `CODE_MOWER_TESTPYPI_PUBLISH` repository variable.
- Production PyPI publishing remains gated by the separate `pypi` environment
  and `CODE_MOWER_PYPI_PUBLISH` variable.
- The PyPI release runbook now includes a workflow dispatch matrix for
  build-only, TestPyPI, and production PyPI release rehearsals.
- Public install docs now point to `v0.5.0-alpha.16`.

## v0.5.0-alpha.15

This alpha fixes the first-run package rehearsal command surfaces for current
GitHub-tag alpha installs.

### Changed

- `code-mower --help` now recommends the current GitHub alpha tag for
  `migration package-install-rehearsal`.
- `code-mower next-steps --profile recommended` derives the current GitHub
  alpha package spec from the installed package version.
- Alpha-facing docs and generated command catalogs no longer recommend the
  unavailable PyPI package placeholder before PyPI publishing is promoted.
- Public install docs now point to `v0.5.0-alpha.15`.

## v0.5.0-alpha.14

This alpha hardens the public release pipeline after the artifact-action
maintenance updates.

### Changed

- The GitHub Release workflow now downloads the built distribution artifact and
  runs `twine check dist/*` before optional PyPI publishing can start.
- The release workflow's artifact download path is exercised even when PyPI
  publishing is skipped.
- The PyPI release runbook documents the artifact verification job as a release
  gate.
- Public install docs now point to `v0.5.0-alpha.14`.

## v0.5.0-alpha.13

This alpha fixes the public repo's Dependabot Dependency Graph compatibility
after the pytest 9.1.0 maintenance update exposed that the standalone repo used
a nonstandard pip requirements filename.

### Changed

- The standalone repo now uses `requirements/requirements.txt` as its pip
  tooling requirements file, matching Dependabot's supported manifest naming.
- `code-mower bootstrap` defaults to `requirements/requirements.txt` instead
  of the extraction-era `tools/code_mower_requirements.txt` path.
- Package extraction metadata now renders the requirements file to
  `requirements/requirements.txt`.
- Public install docs now point to `v0.5.0-alpha.13`.

## v0.5.0-alpha.12

This alpha makes the first-user readiness scorecard more discoverable from the
default CLI and `next-steps` guidance.

### Changed

- Top-level `code-mower --help` now includes the package-install rehearsal in
  the common first-run path.
- `code-mower next-steps --profile recommended` now points to the
  `first_user_readiness` rehearsal result and lists the readiness scorecard
  artifact path.
- Public install docs now point to `v0.5.0-alpha.12`.

## v0.5.0-alpha.11

This alpha adds a first-user readiness scorecard to the package-install
rehearsal. The rehearsal already proved install, easy-mode setup, doctor, first
reports, and cloud dry-run behavior; the scorecard now turns that evidence into
an explicit release-gate summary.

### Added

- `migration package-install-rehearsal` now includes `first_user_readiness` in
  its JSON payload and writes `outputs/first-user-readiness.json`.
- The readiness scorecard verifies package install, easy-mode generated output,
  doctor completion, draft corpus/report generation, starter value report
  generation, cloud export, cloud upload dry-run privacy, and dogfood dry-run
  privacy.

### Changed

- Public install docs now point to `v0.5.0-alpha.11`.
- First-user rehearsal docs now list `first_user_readiness.status == pass` as a
  release-gate criterion.

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
