# Code Mower Current State And Roadmap

This is the short source-of-truth snapshot for the public OSS package, the
hosted CodeMower.com surface, and the near-term path to a shareable v0.5.

## Positioning

Code Mower is the fastest way to create a peer-programmer and reviewer system
around the top AI coding agents and reviewers. The OSS core helps teams move
from plan to merge at maximum safe velocity while preserving code quality,
architecture, and deployment confidence.

It also creates a quality, speed, and cost benchmark loop on a team's actual
product: which AI builders and reviewers produce useful results on this
codebase, at what cost, and with which review policy.

## Current OSS State

The public OSS repository is:

```text
https://github.com/jeffhuber/code-mower
```

The current public alpha baseline is `v0.1.0-alpha.26`. It has proved:

- source checkout and package-install rehearsals from a clean Python 3.12 path;
- `code-mower init --easy`, `doctor --easy`, `next-steps`, and starter
  value-report generation;
- pinned standalone consumption from the private reference/product repos;
- mirror-removal pilots where product repos use package-backed wrappers instead
  of maintaining duplicate implementation files;
- generated product-support wrappers for compatibility shims and shell-safe
  GitHub comments;
- optional sanitized cloud export/upload commands;
- Code Mower Cloud dogfood events from the OSS repo and product work; and
- GitHub-first setup checks, including private-repo Actions cost visibility.

Code Mower is ready for small, supervised pilots in real repositories. It is not
yet ready for broad, automatic org-wide rollout or uncalibrated merge gates.

## Current CodeMower.com State

The hosted surface is:

```text
https://codemower.com
```

Current live paths:

- `https://codemower.com/api/health`
- `https://codemower.com/api/ingest`
- `https://codemower.com/login`
- `https://codemower.com/dashboard`

The cloud service currently supports:

- metadata-only ingest bundles;
- structured benchmark events;
- per-team ingest tokens;
- a protected dashboard for team/token management;
- GitHub, Google, and Apple login UI through Supabase Auth; and
- dogfood uploads from Code Mower and product development.

OAuth provider credentials still need to be enabled in Supabase before external
GitHub/Google/Apple sign-in is fully usable by early adopters. Until then,
operator-issued team tokens remain the fallback.

## v0.5 Early-Adopter Goal

v0.5 should be shareable with 20-50 early OSS users who can follow a guide
without knowing the original reference repos.

The v0.5 experience should be:

1. install Code Mower from GitHub or a package index;
2. run `code-mower init --easy`;
3. run `code-mower doctor --easy --github --probe-runtime`;
4. run a first manual/local audit;
5. generate a local reviewer value report;
6. optionally create a CodeMower.com team token; and
7. optionally upload sanitized benchmark metadata.

The default lane policy remains conservative: Codex and Claude are the first
local structured audit lanes; Gitar and other hosted reviewers start
informational/manual until a user's own data supports promotion.

## v1.0 Direction

v1.0 should be "easy mode with a path to power":

- GitHub-first, with private-repo behavior and Actions cost made explicit.
- Local-first, with cloud export/upload strictly optional.
- No source code, raw diffs, raw model transcripts, stdout/stderr, auth output,
  or secrets in default cloud bundles.
- Provider and lens expansion gated by calibration evidence, not enthusiasm.
- Product repos consume a pinned standalone package instead of mirrored
  implementation files.

GitLab, Bitbucket, ACP bridges, hosted builder harnesses, and fully automated
authoring-run capture remain post-v1.0 work.

## Near-Term Roadmap

1. Finish the public/installable v0.5 path: docs, package install, doctor,
   first audit, first value report, and optional cloud token setup.
2. Enable Supabase Auth providers for CodeMower.com and verify GitHub, Google,
   and Apple login end to end.
3. Continue dogfooding metadata uploads from Code Mower and private product
   work.
4. Expand the calibration corpus with known-clean, known-blocked, and subtle
   architecture-risk PRs.
5. Run reviewer/lens calibration across Codex, Claude, Antigravity/Gemini,
   Gitar, and available informational lanes.
6. Produce durable reviewer value reports with useful-rate, false positives,
   latency, and cost.
7. Promote lanes only after evidence shows they deserve informational,
   selective, or merge-gating status.
8. Keep commercial implementation, hosted reporting, telemetry products, and
   monetization plans in the private CodeMower.com repo.

## Documentation Ownership

Public OSS docs live in the Code Mower repo. Private SaaS deployment docs live
in the CodeMower.com repo. Product repos should keep only thin support wrappers
and product-specific notes.
