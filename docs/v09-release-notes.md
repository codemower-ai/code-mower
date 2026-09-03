# Code Mower v0.9.3-beta.1 Release Notes

This beta packages a small v0.9.3 confidence-polish pass after the v0.9.2
announcement-hardening release. Install the pinned beta with
`CODE_MOWER_PYTHON="$(command -v python3.12)"` followed by
`pipx install --python "$CODE_MOWER_PYTHON" code-mower==0.9.3b1`.

## Headline

Code Mower v0.9.3 makes the friendly-adopter loop more confidence-inspiring by
separating starter-package warnings from real repo failures, improving
operator-facing status detail, clarifying hosted/orchestrator doctor posture,
and reporting standalone pin drift during existing-repo upgrades.

## What's New

- `doctor --adoption` verifies packaged-starter review-hygiene workflow paths
  against the current repo checkout. Missing generated review-hygiene workflows
  remain a starter warning, while missing workflows in a real repo config still
  fail.
- `code-mower lanes status --repo OWNER/REPO` promotes the selected PR's
  `next_detail` into the top-level text and JSON summary so pasted status
  updates include the exact stale audit or gate requeue detail.
- Board now reports the Code Mower version currently serving the browser page
  and shows a restart hint when a newer installed package version is available.
- `code-mower board serve --repo OWNER/REPO` remains the recommended local
  read-only dashboard after install, init, and doctor.
- `doctor --adoption` uses plainer hosted/orchestrator posture guidance, and
  trusted audit-author GitHub variable read failures report `not_confirmed`
  instead of implying confirmed missing configuration.
- `code-mower migration setup-drift --repo-path .` reports the current
  `tools/code_mower_standalone_pin.env` posture without sourcing it, including
  placeholder, unreadable, missing, matching, and drifted states.
- Longer term, #590 tracks cleaning up Code Mower's own dogfood
  `code-mower.yml` root posture so product checks do the unsurprising thing by
  default without starter-specific explanation.

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
