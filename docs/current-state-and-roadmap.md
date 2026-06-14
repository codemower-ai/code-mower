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
https://github.com/codemower-ai/code-mower
```

The current public alpha baseline is `v0.5.0-alpha.7`. It is the first alpha
intended to be installed from the `codemower-ai/code-mower` public repository
after the org move. It has proved:

- source checkout and package-install rehearsals from a clean Python 3.12 path;
- `code-mower init --easy`, `doctor --preflight`, `next-steps`, and starter
  value-report generation;
- pinned standalone consumption from the private reference/product repos;
- mirror-removal pilots where product repos use package-backed wrappers instead
  of maintaining duplicate implementation files;
- generated product-support wrappers for compatibility shims and shell-safe
  GitHub comments;
- optional sanitized cloud export/upload commands;
- `code-mower doctor --preflight` as the friendly early-adopter preset for easy
  mode, runtime probes, GitHub/private-repo setup, Actions cost diagnostics,
  and optional cloud-token setup. `doctor --v05` remains the versioned alias for
  scripts;
- Code Mower Cloud dogfood events from the OSS repo and product work; and
- GitHub-first setup checks, including private-repo Actions cost visibility.
- public repo hygiene artifacts: issue templates, pull request template,
  Dependabot config, security policy, and an explicit repo-hardening checklist.
- first-impression adoption improvements: README sample output,
  `docs/sample-doctor-output.md`, and a clearer cloud value-exchange section.
- first-run and trust docs: `CHANGELOG.md`, `docs/first-run-transcript.md`,
  `docs/architecture.md`, `docs/cloud-data-contract.md`, and
  `docs/code-structure-roadmap.md`.

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
- dogfood uploads from Code Mower and product development; and
- self-service metadata export and deletion for signed-in team members/admins.

It does not yet provide automated retention jobs or true cross-team cohort
benchmark calculations. Those are preconditions for broad cloud-data collection
beyond friendly pilots.

OAuth, Supabase, Vercel, DNS, and hosted-secret setup are CodeMower.com
operator responsibilities. OSS users should only need a dashboard-issued or
operator-issued developer/team token when they opt into cloud sharing.

## v0.5 Early-Adopter Goal

v0.5 should be shareable with 20-50 early OSS users who can follow a guide
without knowing the original reference repos.

The v0.5 experience should be:

1. install Code Mower from GitHub or a package index;
2. run `code-mower init --easy`;
3. run `code-mower doctor --preflight`;
4. run a first manual/local audit;
5. generate a local reviewer value report;
6. optionally create or receive a CodeMower.com developer/team token; and
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
2. Make the public repository the unambiguous source of truth: keep public docs
   and releases flowing from `codemower-ai/code-mower`, reduce extraction-era
   compatibility shims where they confuse contributors, and keep private
   product repos as consumers of pinned releases.
3. Create a public GitHub Release for the current alpha, verify the release
   workflow builds source/wheel artifacts, and configure PyPI trusted
   publishing before widening beyond friendly early adopters.
4. Add a short terminal recording or screenshot showing `doctor --preflight`
   and the first value-report path. A static transcript now exists in
   `docs/first-run-transcript.md`; replace or augment it with a recording
   before a wider launch.
5. Enable Supabase Auth providers for CodeMower.com and verify GitHub, Google,
   and Apple login end to end.
6. Turn the current team-controlled deletion/export basics into a published
   retention policy with automated retention jobs before broad cloud-data
   invitations.
7. Continue dogfooding metadata uploads from Code Mower and private product
   work.
8. Expand the calibration corpus with known-clean, known-blocked, and subtle
   architecture-risk PRs.
9. Run reviewer/lens calibration across Codex, Claude, Antigravity/Gemini,
   Gitar, and available informational lanes.
10. Produce durable reviewer value reports with useful-rate, false positives,
   latency, and cost.
11. Promote lanes only after evidence shows they deserve informational,
   selective, or merge-gating status.
12. Increase tests around verdict parsing, calibration/value-report math,
    provider runner stubs, and cloud bundle privacy before presenting Code
    Mower as merge-gate infrastructure.
13. Triage CLI help into a smaller first-user command set, with advanced
    operator/internal commands documented separately.
14. Keep commercial implementation, hosted reporting, telemetry products, and
   monetization plans in the private CodeMower.com repo.

## Documentation Ownership

Public OSS docs live in the Code Mower repo. Private SaaS deployment docs live
in the CodeMower.com repo. Product repos should keep only thin support wrappers
and product-specific notes.

Keep setup docs split by persona:

- OSS user docs: install, `doctor`, first audit, first report, optional
  developer/team token.
- CodeMower.com operator docs: Supabase/Postgres, Vercel, OAuth, DNS,
  service-role/admin secrets, token fallback, retention, and hosted reporting.
