# Code Mower v0.8.0-beta.1 Release Notes

This beta packages the v0.7 adoption hardening and v0.8 native Board work.
Install the pinned beta with
`pipx install --python python3.12 code-mower==0.8.0b1`.

## Headline

Code Mower v0.8 is a better first install for supervised multi-agent adoption:
clearer Python/install expectations, less noisy doctor output for hosted
builders, a one-command lane status snapshot, and a native local Board for
operator visibility without third-party hook setup.

## What's New

- Python support is now explicit: Code Mower requires Python 3.12 or newer, and
  CI exercises Python 3.12, 3.13, and 3.14.
- Install docs cover pipx for laptops, `uv tool install` for hosted agents and
  minimal Linux boxes, and editable venvs for contributors.
- Doctor has adoption-oriented profiles for local runners, hosted builders, and
  orchestrator-only setups, plus clearer dispatch-token diagnostics including
  non-expiring tokens.
- `code-mower lanes status --repo OWNER/REPO` provides a concise,
  metadata-only operator snapshot of active PR lanes, labels, checks, workflow
  runs, gate health, and next action.
- `code-mower board serve --repo OWNER/REPO` starts a local read-only Board with
  redacted lane status, local history, owner queue, spend/latency, verdicts, and
  optional metadata-only agent cards.
- Board history/admin commands (`board record`, `board doctor`, and
  `board reset`) make local visibility manageable without requiring cloud
  upload.
- `code-mower cloud board-snapshot` uploads explicit zero-report Board snapshot
  metadata for CodeMower.com mirrors while preserving the existing privacy
  boundary.
- The cloud token resolver is shared across cloud commands: environment tokens
  still win, `--token-file` is explicit, stored local profiles are supported,
  and ambiguous token stores fail with safe filename-only guidance.
- Generated builder-provenance workflows now install this beta package by
  default.
- Release, quickstart, install, and first-user rehearsal docs now use the same
  current beta pin and keep the GitHub release marked as Latest even though the
  software is still beta.

## Privacy

The privacy boundary is unchanged. Code Mower uploads remain opt-in and
metadata-only. Do not upload source code, raw diffs, transcripts, issue body
text, raw stdout/stderr, auth output, local secret values, or secrets.

## Adoption Guidance

Use this release for supervised early-adopter pilots, not unattended autonomous
merge infrastructure. Start with the reviewer gate, keep lanes informational
until repository-specific calibration supports promotion, use `lanes status`
and the Board for day-to-day visibility, and route a few small issues through
non-Codex builders/reviewers before making promotion claims.
