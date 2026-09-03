# Code Mower v0.9.4-beta.1 Release Notes

This beta packages the final v0.9 pre-announcement hardening pass before wider
friend-repo installs. Install the pinned beta with
`CODE_MOWER_PYTHON="$(command -v python3.12)"` followed by
`pipx install --python "$CODE_MOWER_PYTHON" code-mower==0.9.4b1`.

## Headline

Code Mower v0.9.4 makes the announcement path steadier: Code Mower now dogfoods
from a real root `code-mower.yml`, doctor separates owner actions from ordinary
warnings in adoption output, setup-drift catches builder-lane upgrade posture
more clearly, and the tagged adoption prompts point cold orchestrators at the
right install, upgrade, doctor, and Board restart docs.

## What's New

- Code Mower's own repository now has a root dogfood `code-mower.yml`, so
  release checks and local commands exercise the same config path adopters use.
- `doctor --adoption` has an explicit owner-action category for setup work such
  as branch protection, auto-merge, and owner-managed secrets. Text and JSON
  summaries distinguish those actions from ordinary warnings and failures.
- `code-mower migration setup-drift --repo-path .` always reports standalone pin
  posture and warns when existing builder/dispatch files are present but
  `--builders` was omitted. The hint covers Codex, Claude, Cursor, and the
  self-hosted Mac runner files.
- The orchestrator prompt pack now sends agents to `docs/install.md` and
  `docs/upgrade-existing-repo.md` from the same release tag, and hosted-agent
  examples lead with `--orchestrator-only` or `--hosted-builders` doctor
  posture where appropriate.
- The Board contract and troubleshooting docs document `/api/status`,
  `board.version.restart_recommended`, and the restart flow after package
  upgrades.
- The experimental Muse CLI lane remains available for calibration as
  `muse_cli`, with `builder:muse` and `needs-muse-audit` labels. Keep it
  informational until local evidence supports promotion.

## Privacy

The privacy boundary is unchanged. Code Mower uploads remain opt-in and
metadata-only. Do not upload source code, raw diffs, transcripts, issue body
text, raw stdout/stderr, auth output, local secret values, or secrets.

## Adoption Guidance

Use this release for supervised early-adopter pilots. Start with the reviewer
gate, use `code-mower lanes status --repo OWNER/REPO` and
`code-mower board serve --repo OWNER/REPO` for day-to-day visibility, keep
reviewer lanes informational until repository-specific calibration supports
promotion, and capture builder/reviewer diversity data before broadening merge
authority.
