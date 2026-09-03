# Code Mower v0.9.2-beta.1 Release Notes

This beta packages a small v0.9.2 cleanup pass after the v0.9.1
announcement-hardening release. Install the pinned beta with
`CODE_MOWER_PYTHON="$(command -v python3.12)"` followed by
`pipx install --python "$CODE_MOWER_PYTHON" code-mower==0.9.2b1`.

## Headline

Code Mower v0.9.2 makes the friendly-adopter loop less surprising by removing
the retired third-party observe bridge, treating dispatch-token expiry metadata
as advisory once the dispatch secret exists, and making stale audit waits point
operators at the audit runner/dispatcher requeue path.

## What's New

- The retired third-party observe bridge and its legacy status JSON alias have
  been removed. The native read-only Board is the supported local visibility
  surface.
- `code-mower lanes status --repo OWNER/REPO --json` now reports local Board
  listeners under `local_boards` only, with paths redacted by default.
- `doctor --adoption --repo OWNER/REPO` keeps a missing or placeholder
  `DISPATCH_TOKEN_EXPIRES_AT` repository variable at warning level once the
  `DISPATCH_TOKEN` secret exists. Missing dispatch secrets still fail in
  reviewer-gate posture.
- Repositories that intentionally use a non-expiring dispatch token can set
  `DISPATCH_TOKEN_EXPIRES_AT=never`; doctor reports that as a passing,
  non-expiring token posture.
- `code-mower lanes status --repo OWNER/REPO` now distinguishes stale
  `needs-*-audit` waits from generic stuck checks and tells operators to check
  the audit runner/dispatcher and requeue the named lane.
- Stale pending gate waits now surface `rerun stale gate` at the PR and report
  headline level, while preserving the paste-safe gate rerun command.
- Board shows the same `next_detail` guidance that appears in the CLI text and
  JSON output.
- `code-mower board serve --repo OWNER/REPO` remains the recommended local
  read-only dashboard after install, init, and doctor.

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
