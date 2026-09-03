# Code Mower v0.9.1-beta.1 Release Notes

This beta packages the v0.9.1 announcement-hardening pass after the v0.9
adoption and upgrade release. Install the pinned beta with
`CODE_MOWER_PYTHON="$(command -v python3.12)"` followed by
`pipx install --python "$CODE_MOWER_PYTHON" code-mower==0.9.1b1`.

## Headline

Code Mower v0.9.1 makes the friendly-adopter loop less surprising: local Board
state is easier to detect, Board timestamps render in the operator's local time
with UTC on hover, setup docs avoid raw auth/status output, doctor output is
clearer for hosted-builder and orchestrator-only machines, trusted author
repository variables are recognized before warning, and upgrade operators get a
reviewed-PR path for generated setup drift.

## What's New

- `code-mower lanes status --repo OWNER/REPO` now detects local Board/listener
  state on macOS and Linux, including Linux hosts where `lsof` is unavailable.
- `code-mower board serve --repo OWNER/REPO` scans the standard local port band
  by default, keeps local paths out of API output, and degrades gracefully when
  GitHub is temporarily unavailable.
- Board-visible timestamps now render with the browser's local timezone; the
  original UTC timestamp remains available as a hover tooltip.
- Public setup docs now use quiet auth/status probes and warn operators not to
  paste raw credential or auth output into issues, chats, or reports.
- `doctor --adoption` gives clearer guidance for configless repositories and
  points hosted-builder or orchestrator-only users at the matching profiles
  instead of making missing local CLIs look like broken setup.
- `doctor --adoption --github --repo OWNER/REPO` recognizes the trusted-author
  repository variables used by Claude and Codex audit lanes without printing
  their values.
- The new existing-repo upgrade guide explains the `setup-drift` to reviewed PR
  flow, including how to handle repo-only files, generated-file drift, wrapper
  pins, and builder/reviewer identity hints.
- Contributor docs now say default checks should stay offline-friendly; live
  package-index rehearsals remain explicit release/integration checks.

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
