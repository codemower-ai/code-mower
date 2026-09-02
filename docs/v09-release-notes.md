# Code Mower v0.9.0-beta.1 Release Notes

This beta packages the v0.9 adoption and upgrade hardening work after the v0.8
native Board release. Install the pinned beta with
`CODE_MOWER_PYTHON="$(command -v python3.12)"` followed by
`pipx install --python "$CODE_MOWER_PYTHON" code-mower==0.9.0b1`.

## Headline

Code Mower v0.9 smooths the cold-install and upgrade path: multiple local Board
instances can coexist, upgrade drift is visible before generated setup files are
applied, hosted-builder and orchestrator-only doctors are quieter, and live
package-index rehearsals are explicit release gates instead of accidental unit
test/network work.

## What's New

- `code-mower board serve --repo OWNER/REPO` now falls forward to nearby
  available loopback ports unless an operator requests an exact port.
- `code-mower migration setup-drift` reports read-only generated setup drift
  before upgrade PRs touch labels, workflows, or local setup files.
- `doctor --adoption --profile hosted-builders` and
  `doctor --adoption --profile orchestrator-only` avoid local CLI/token noise
  that does not apply to hosted-agent-only machines.
- Doctor and lane status output now preserve uncertainty: missing workflow file
  evidence and unavailable GitHub state are warnings/unavailable states, not
  quiet PASS results.
- The Codex runner smoke probe uses current noninteractive Codex CLI flags.
- Install and upgrade docs distinguish cold installs, version upgrades,
  pipx-to-uv migration, sandboxed pipx paths, and hosted-agent installs.
- `migration package-install-rehearsal` keeps package-index specs behind
  `--allow-package-index` and pip upgrades behind `--upgrade-pip`; local source
  and normal test paths fail fast instead of invoking live package-index work by
  accident.

## Privacy

The privacy boundary is unchanged. Code Mower uploads remain opt-in and
metadata-only. Do not upload source code, raw diffs, transcripts, issue body
text, raw stdout/stderr, auth output, local secret values, or secrets.

## Adoption Guidance

Use this release for supervised early-adopter pilots. Start with the reviewer
gate, use `code-mower lanes status --repo OWNER/REPO` and the Board for
day-to-day visibility, keep reviewer lanes informational until
repository-specific calibration supports promotion, and capture builder/reviewer
diversity data before broadening merge authority.
