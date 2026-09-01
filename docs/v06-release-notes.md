# Code Mower v0.6 Release Notes Draft

This is the draft release note for the v0.6 provider-contract hardening
workstream. The package beta line remains `0.5.0b53` until the next tagged beta
is cut; update the concrete version and release link during release execution.

## Headline

Code Mower v0.6 makes the early-adopter loop more predictable: install, run
preflight, add reviewer lanes, promote only with evidence, distinguish provider
runtimes, and capture builder/reviewer metadata without uploading source.

## What's New

- Provider runner contracts are more explicit. Shared comment, verdict,
  exit-code, and schema helpers now live under `code_mower.provider_runners`
  without forcing a flag-day runner rewrite.
- Generated gate workflows publish a required `code-mower/gate` commit status,
  honor author-never-gates, keep `needs-owner` pending, fail on blocker labels,
  and can request GitHub auto-merge when a merge-capable token or App is
  configured.
- Local audit workflows are safer to adopt. They stay skipped until the
  self-hosted runner enablement variable is set, and source-backed generated
  workflows use the checked-out package path instead of relying on ambient
  imports.
- Reviewer-run uploads remain narrow by default. Spend and reviewer evidence
  uploads stay metadata-only and do not widen ordinary dogfood uploads.
- Provider fixture contracts lock representative PASS, BLOCKED, and UNKNOWN
  artifacts so future parser changes have stable examples.
- Gemini CLI and Antigravity are separate Code Mower lanes. Gemini remains the
  legacy compatibility path; Antigravity CLI is the forward Google CLI research
  lane; Antigravity SDK probing is optional and does not import or call the SDK
  unless explicitly requested.
- `code-mower providers antigravity-sdk-probe` records metadata-only SDK
  readiness facts without reading source, starting a harness, calling auth, or
  calling a model by default.
- `code-mower builder-experiment run` wraps an explicit local command and writes
  a source-free `code_mower.authoringRun.v1` artifact with timing, status,
  builder, branch/PR, and command-hash metadata.
- Public docs now steer cold adopters through the reviewer gate first, then the
  build loop, then builder experiments and optional provider research.

## Adoption Notes

- Start with [Try Code Mower In 10 Minutes](try-in-10-minutes.md), then move to
  [Build Loop In 30 Minutes](build-loop-in-30-minutes.md).
- During a pilot, keep reviewer lanes informational and merge manually.
- Promote reviewer lanes only when local known-clean and known-blocked evidence
  meets [Lane Promotion Policy](lane-promotion-policy.md).
- For unattended merges, configure repository auto-merge, require
  `code-mower/gate` from Any source, and install a dedicated
  `CODE_MOWER_GATE_AUTOMERGE_TOKEN` or GitHub App token that can enable PR
  auto-merge.
- For local audit wrappers, pass a posting token with `GITHUB_TOKEN` or
  `--read-token-from-stdin`, and pass `--repo-paths` as
  `OWNER/REPO:/absolute/path/to/pr-head-checkout`.

## Privacy Boundary

The v0.6 workstream does not widen the privacy boundary. Cloud-bound artifacts
and uploads remain metadata-only by default: no source code, raw diffs, raw
model transcripts, issue body text, raw stdout/stderr, auth output, or secrets.

## Upgrade Checklist

1. Install the tagged beta package and verify `code-mower --version`.
2. Re-run `code-mower init --easy` in dry-run mode and review generated
   workflow diffs.
3. Regenerate workflows only after reviewing token, runner, and branch
   protection settings.
4. Run `code-mower doctor --preflight --json`.
5. Run one small PR through Codex and Claude audits before enabling unattended
   merge behavior.
6. Use CodeMower.com uploads only after inspecting the dry-run bundle.
