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

The current public alpha baseline is `v0.5.0-alpha.44`. It is intended to be
installed from the `codemower-ai/code-mower` public repository and has proved:

- source checkout and package-install rehearsals from a clean Python 3.12 path;
- `code-mower init --easy`, `doctor --preflight`, `next-steps`, and starter
  value-report generation;
- pinned standalone consumption from the private reference/product repos;
- mirror-removal pilots where product repos use package-backed wrappers instead
  of maintaining duplicate implementation files;
- self-hosted package materialization from installed checkouts, with generated
  package metadata stamped from the source checkout version;
- release-readiness checks that materialize the standalone package and fail if
  generated package versions drift from source metadata;
- generated product-support wrappers for compatibility shims and shell-safe
  GitHub comments;
- optional sanitized cloud export/upload commands with fail-closed structured
  event metadata guards for raw output, auth previews, transcripts, and
  secret-like values;
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
- `migration package-install-rehearsal` now emits a first-user readiness
  scorecard, so release candidates can show install, doctor, first-report, and
  cloud dry-run privacy gates in one compact JSON artifact.
- CI now runs the package-install first-user rehearsal from the current
  checkout, turning the public installed-package path into a routine PR gate
  instead of a purely manual pre-release check.
- `code_mower_calibration.py` has been reduced to a backwards-compatible CLI
  adapter; calibration corpus, evidence, policy, value-report, context-pack,
  command-materialization, run-result, and runner logic now live under
  `code_mower.calibration`.
- `doctor.py` is now a much thinner backwards-compatible CLI adapter.
  Runtime/toolchain, cloud-token, GitHub, provider, and Actions diagnostics
  plus human-readable output rendering, first-run presets, and package-aware
  config/template path resolution live under `code_mower.doctor_checks`. Doctor
  report orchestration also now lives under `code_mower.doctor_checks.runner`,
  leaving `doctor.py` as a small CLI adapter. Provider doctor checks are now
  split into token/env checks, local CLI discovery/probes, API-model probes,
  and a thin provider catalog/runtime orchestrator.
  GitHub doctor internals are also split so redacted API calls and Actions
  billing/cost diagnostics can evolve without bloating repo setup checks.
- `cloud.py` has completed its first major transition into a thin compatibility
  adapter: local cloud setup/token handling, cloud doctor diagnostics, local
  bundle materialization, structured event/repo helpers, and dogfood/catch-up/
  reviewer-run/repo-sync orchestration now live under `code_mower.cloud_client`,
  reducing the CLI adapter significantly while preserving the public command
  surface.
- Package materialization has started the same intentional split:
  package file manifests now live under `code_mower.package_manifest`, and
  generated package content builders and CLI command inventory now live under
  `code_mower.package_content`; generated static package file bodies live under
  `code_mower.package_static`; YAML/provider-catalog rendering helpers live
  under `code_mower.package_rendering`.
  Package-aware config/template path helpers live under
  `code_mower.package_paths`, while `package.py` remains the
  backwards-compatible CLI and manifest-generation surface.
- Package-install rehearsal now lives under `code_mower.migration_rehearsal`,
  first-user readiness scoring now lives under `code_mower.migration_readiness`,
  and mirror-removal planning and runner-alias reporting now live under
  `code_mower.migration_mirror`; `migration.py` remains the backwards-compatible
  migration command adapter for wrapper rehearsal, release-readiness routing,
  mirror planning, and package-install orchestration.

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

## Senior-Engineer Readiness Gate

The next product gate is a first-impression gate, not a new-provider gate. A
fresh senior engineer landing on the public repository should be able to answer
these questions in the first few minutes:

- What problem does Code Mower solve that a single local agent does not?
- What happens locally, and what is optional cloud sharing?
- What commands prove the install path without mutating a repository?
- What data, if any, leaves the machine?
- Which provider lanes are safe to try first?
- What would make a lane eligible for merge-gating?
- Where is the code intentionally structured, and where is it still being
  refactored from extraction-era shape?

The v0.5-to-v1.0 work should optimize for that trust test. More provider
adapters are useful only after install, doctor, first report, privacy, and code
structure feel boring and credible.

## Fresh-Eyes Feedback Incorporated

Recent external first-impression reviews converged on the same pattern: the
thesis, privacy posture, and package layout are compelling, but the path from
"I found this repo" to "I learned which AI reviewer is useful on my codebase"
still has too much setup friction.

Treat these as product gates before widening beyond friendly early adopters:

- **Install friction:** GitHub-tag installs are acceptable for alpha users, but
  public adoption needs a normal package-index path.
- **CLI overwhelm:** default help should show the launch-safe commands first;
  provider bridges, labelers, migration internals, and operator commands belong
  behind `code-mower --help-all` or deeper docs.
- **Time to value:** users should not have to hand-build a full calibration
  corpus before seeing a useful report. The current auto-discovery command
  bootstraps a draft corpus from recent merged PRs and known review signals;
  release rehearsals prove that path and docs should keep emphasizing human
  disposition review before lane promotion.
- **Code confidence:** release hygiene tests prove broad behavior, but v1.0
  needs more focused unit coverage around doctor checks, cloud bundle privacy,
  calibration math, verdict parsing, and provider-runner seams.
- **Cloud incentive:** CodeMower.com must show immediate insight after upload,
  not just receipt rows. Cohort benchmarks, recommendation quality, and
  public/dogfood examples are the reasons a careful team would opt in.

## v1.0 Direction

v1.0 should be "easy mode with a path to power":

- GitHub-first, with private-repo behavior and Actions cost made explicit.
- Local-first, with cloud export/upload strictly optional.
- No source code, raw diffs, raw model transcripts, stdout/stderr, auth output,
  or secrets in default cloud bundles.
- Provider and lens expansion gated by calibration evidence, not enthusiasm.
- Product repos consume a pinned standalone package instead of mirrored
  implementation files.
- Public docs explain Code Mower as a local operating layer for peer
  programmers and reviewer lanes, not as a hosted service that must be adopted
  wholesale.

GitLab, Bitbucket, ACP bridges, hosted builder harnesses, and fully automated
authoring-run capture remain post-v1.0 work.

## Builder And Orchestrator Direction

Reviewer calibration is the current executable loop: compare reviewers and
lenses against known-clean, known-blocked, and subtle-risk PRs. Builder-side
experiments are the next major extension: compare which AI peer programmer plus
review policy ships verified code fastest and cleanest.

The roadmap should borrow the useful shape from multi-agent/orchestrator
systems without adopting their full runtime:

- record a normalized `run_role` or `purpose` such as `implement`, `review`,
  `calibrate`, `release`, or `explore`;
- keep one worktree/branch per builder run;
- review via diff plus task contract, not builder transcript;
- record provider, lens, context pack, elapsed time, user interventions, audit
  blocker iterations, checks, merge result, post-merge health, and known cost;
- keep local runners responsible for source and credentials;
- keep CodeMower.com responsible for optional metadata storage, private team
  dashboards, and future aggregate benchmarks.

This keeps Code Mower's center of gravity GitHub-native and local-first while
leaving room for future orchestrator adapters.

## Near-Term Roadmap

1. Pass the senior-engineer readiness gate: README, quickstart, architecture,
   privacy, install, and first report should tell one coherent story.
2. Finish the public/installable v0.5 path: docs, package install, doctor,
   first audit, first value report, and optional cloud token setup.
3. Make the public repository the unambiguous source of truth: keep public docs
   and releases flowing from `codemower-ai/code-mower`, reduce extraction-era
   compatibility shims where they confuse contributors, and keep private
   product repos as consumers of pinned releases.
4. Create a public GitHub prerelease for the current alpha, verify the release
   workflow builds source/wheel artifacts, and configure PyPI trusted
   publishing before widening beyond friendly early adopters.
5. Add a short terminal recording or screenshot showing `doctor --preflight`
   and the first value-report path. A static transcript now exists in
   `docs/first-run-transcript.md`; replace or augment it with a recording
   before a wider launch.
6. Enable Supabase Auth providers for CodeMower.com and verify GitHub, Google,
   and Apple login end to end.
7. Turn the current team-controlled deletion/export basics into a published
   retention policy with automated retention jobs before broad cloud-data
   invitations.
8. Continue dogfooding metadata uploads from Code Mower and private product
   work.
9. Expand the calibration corpus with known-clean, known-blocked, and subtle
   architecture-risk PRs.
10. Run reviewer/lens calibration across Codex, Claude, Antigravity/Gemini,
   Gitar, and available informational lanes.
11. Produce durable reviewer value reports with useful-rate, false positives,
   latency, and cost.
12. Promote lanes only after evidence shows they deserve informational,
   selective, or merge-gating status.
13. Increase tests around verdict parsing, calibration/value-report math,
    provider runner stubs, and cloud bundle privacy before presenting Code
    Mower as merge-gate infrastructure.
14. Triage CLI help into a smaller first-user command set, with advanced
    operator/internal commands documented separately.
15. Harden calibration auto-discovery with more real PR shapes, first-user
    examples, and package-install rehearsal coverage so first reports can be
    bootstrapped from project history with human review.
16. Reduce first-read README friction: one-screen pitch, install, doctor sample,
    demo report, and links to deeper docs.
17. Add builder-experiment capture only after the reviewer/value loop is
    producing durable evidence.
18. Keep commercial implementation, hosted reporting, telemetry products, and
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
