# Code Mower v1.0.0 Release Notes

Code Mower v1.0.0 is the supervised autonomous pilot release. It is ready for
senior engineers and agent operators to install from PyPI, wire into one
repository, run visible builder/reviewer lanes, and decide promotion from
evidence. It is still a supervised pilot: keep a human or trusted orchestrator
responsible for owner decisions, calibration, and merge policy.

Install the pinned package:

```bash
export CODE_MOWER_PYTHON="$(command -v python3.12)"
pipx install --python "$CODE_MOWER_PYTHON" code-mower==1.0.0
code-mower --version
```

Hosted agents and CI boxes can use:

```bash
uv tool install --python 3.12 code-mower==1.0.0
code-mower --version
```

## What Is New

- Supervised controller dry-runs summarize lane state, merge posture, and the
  policy decision without mutating the repository.
- `doctor --supervised-pilot`, `--manual-pilot`, and `--promoted-pilot` make the
  adoption posture explicit and classify promotion todos separately from
  ordinary warnings.
- Board surfaces the supervised-pilot snapshot locally, including next actions,
  owner actions, lane health, and package/serving-version status.
- CodeMower.com can receive metadata-only supervised Board snapshot events while
  remaining backward-compatible with v0.9 uploads.
- provider-diversity fixtures cover Claude Code, Codex, Cursor/Grok Bot,
  Antigravity, Muse, Devin, Gitar, and CodeRabbit-style evidence so teams can
  compare lanes without widening the privacy boundary.
- The universal orchestrator prompt pack gives Claude Code, Codex, Cursor/Grok
  Bot, Antigravity, Devin, Muse, and future providers the same install/upgrade
  and reporting path.
- Release readiness now includes cold install, upgrade/setup-drift, package
  index, Board, cloud dry-run, and documentation consistency checks.

## Recommended First Run

1. Read the tagged
   [Try Code Mower In 10 Minutes](https://github.com/codemower-ai/code-mower/blob/v1.0.0/docs/try-in-10-minutes.md)
   guide.
2. Install `code-mower==1.0.0` with pipx or uv.
3. Run `code-mower init --easy --dry-run`, then apply the reviewed setup.
4. Run `code-mower doctor --adoption --repo OWNER/REPO`.
5. Run `code-mower lanes status --repo OWNER/REPO`.
6. Run `code-mower board serve --repo OWNER/REPO` for the local visibility
   board.
7. Keep reviewer lanes informational/manual until repository-specific evidence
   meets `docs/lane-promotion-policy.md`.

## Release Proof

The release PR records the v1.0 release-readiness check, focused and full test
passes, source-package rehearsal, package-index publication workflow links, and
PyPI install rehearsal. The public docs keep the privacy boundary unchanged:
metadata only, no source, raw diffs, transcripts, raw auth output, raw
stdout/stderr, or secrets in uploads.
