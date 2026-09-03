# Code Mower Current State And Roadmap

This is the short source-of-truth snapshot for the public OSS package, the
hosted CodeMower.com surface, and the near-term path from the current v1.0.2
line toward broader supervised pilots.

## Positioning

Code Mower is the fastest way to create a peer-programmer and reviewer system
around the top AI coding agents and reviewers. The OSS core helps teams move
from plan to merge at maximum safe velocity while preserving code quality,
architecture, and deployment confidence.

Code Mower v1.0 is supervised-pilot software for teams willing to calibrate
reviewers and keep a human or trusted orchestrator responsible for the loop. It
is not a drop-in unattended merge gate for arbitrary repositories.

It also creates a quality, speed, and cost benchmark loop on a team's actual
product: which AI builders and reviewers produce useful results on this
codebase, at what cost, and with which review policy.

## Current OSS State

The public OSS repository is:

```text
https://github.com/codemower-ai/code-mower
```

The current package-index release baseline is `v1.0.2`, with pinned package
install spec `code-mower==1.0.2`. Release evidence is recorded on the GitHub
release and in the first-user install rehearsal. It is intended to be installed
from the package index for supervised pilots, with GitHub tag/source installs
kept as a fallback and development path.

The v1.0.2 supervised-pilot release keeps the Python 3.12+ runtime contract,
pipx/uv install matrix, non-expiring dispatch-token diagnostics, native redacted
lane status, the local Board, Board history and admin commands, spend/verdict
timelines, owner queue, optional metadata-only agent cards, explicit cloud Board
snapshots, the CodeMower.com Board mirror, and a public Board demo rehearsal. It
adds controller dry-run policy, supervised-pilot doctor readiness,
provider-diversity fixtures, the common install/upgrade prompt pack, Board
multi-instance handling, local productivity reports, Board productivity
summaries, provider scorecards, CodeMower.com productivity views, setup drift
reporting, quieter hosted-builder and orchestrator-only doctor postures,
truth-preserving unavailable/warn states, current Codex CLI smoke flags, clearer
install/upgrade docs, and explicit package-index rehearsal opt-ins. See the
[v1.0.2 Effectiveness Assessment](v101-effectiveness-assessment.md) for the
current dogfood assessment and lane-readiness interpretation; the
[Post-v0.8 Effectiveness Assessment](post-v08-effectiveness-assessment.md)
remains historical context.

This baseline keeps the PyPI-first install path, trusted publishing, release
rehearsal, production dogfood upload shape, catch-up provenance, stale-audit
inspection, AI tool/model source diagnostics, CodeMower.com trust guidance,
generated gate hardening, owner-bound WIP hygiene, lane liveness checks,
fix-round templates, human-token diagnostics, owner-decision escalation, and
provider sandbox/live guardrails in one coherent supervised-pilot release line.
The beta-to-v1.0 line has proved:

- source checkout and package-install rehearsals from a clean Python 3.12 path;
- `code-mower init --easy`, `doctor --adoption --repo OWNER/REPO`,
  `next-steps`, and starter value-report generation;
- `code-mower checks detect` and `code-mower checks run` for repository-native
  lint/test/build discovery instead of assuming Ruff, ESLint, or any other
  single check surface applies to every codebase;
- merge-authority stale-audit protection via generated workflow/template
  support and `clear-stale`, so stale `*-audit-done` / `*-audit-blocked` labels
  cannot satisfy a merge bar after new commits land;
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
- `code-mower doctor --adoption --repo OWNER/REPO` as the friendly
  early-adopter preset for easy mode, runtime probes, GitHub/private-repo
  setup, Actions cost diagnostics, and optional cloud-token setup.
  `doctor --preflight` and `doctor --v05` remain compatibility presets for
  scripts. Doctor JSON now exposes a top-level `run_plan`, and human output
  prints the same plan near the header so support tooling and first-time users
  can see exactly which optional GitHub/cloud stages ran;
- Code Mower Cloud dogfood events from the OSS repo, CodeMower.com, and two
  private reference/product repos, with the current release preserving that client
  path for the next rollout; and
- metadata-only AI tool/model provenance in cloud bundles and structured
  events, so dashboards can distinguish known provider/model/version signal
  from missing provenance before making benchmark claims. Local CLI provenance
  now resolves configured alternate command names before declaring tool/version
  metadata missing, which matters for transitions such as Gemini CLI to
  Antigravity-style commands; and
- calibration result normalization that preserves provider-observed model ids
  from structured CLI stats when explicit model configuration is absent.
  This improves Google/Antigravity-style value-report provenance without uploading
  raw prompts, diffs, or transcripts; and
- local project-context and work-order planning commands:
  `project-context init`, `context add --external`, `plan from-issue`,
  `work-order draft`, `work-order critique-plan`, and
  `work-order builder-experiment`. These give teams a source-free path from
  issue/spec context to implementation contracts and builder-experiment seeds
  without turning Code Mower into a mandatory agent orchestrator; and
- a real metadata-only `repo-sync --mode catch-up --limit 100` import across
  the OSS repo, hosted service repo, and two private reference/product repos,
  with imported history flagged as `history_only: true` and
  `calibration_evidence: false`; and
- a package-installed calibration/value-report pipeline target that keeps
  reviewer metrics, lane policy, value-report artifacts, and sanitized report
  upload in the release rehearsal path; and
- a private-repo install rehearsal target against
  [DrinkBetter-AI/mobile-app](https://github.com/DrinkBetter-AI/mobile-app)
  that proved the package-installed CLI can detect and dry-run
  repository-native checks in an external-ish private repo without committing
  support files first.
  The rehearsal passed with 10/10 first-user readiness and 0 readiness
  warnings, detected `npm run lint`, `npm run typecheck`, and `npm run test`
  from `package.json`, and separately reported only expected setup diagnostics
  for optional provider tokens and unprobed GitHub auth; and
- report-snapshot provenance cleanup, so Code Mower-generated value-report and
  lane-policy snapshot events carry Code Mower reporter provenance by default
  instead of appearing as unknown-provider benchmark gaps; and
- provider-vs-lens effect-report output cleanup, so `--output` writes a
  human-readable report while `--json` remains structured stdout for automation;
  and
- a friendly-user rollout plan that turns install, doctor, first report,
  optional cloud dry-run/upload, and dashboard usefulness into explicit
  acceptance criteria for the first 5-10 users; and
- the current public PyPI package-install rehearsal from `v1.0.2` /
  `code-mower==1.0.2` with a
  10/10 first-user readiness score, proving install, generated setup, doctor,
  draft calibration, value-report, cloud export, and dry-run dogfood without a
  local Code Mower checkout. The earlier beta.52 package rehearsal remains
  historical evidence, not the current adoption proof; and
- stable CodeMower.com evidence URLs for signed-in users, with per-upload and
  per-event detail pages plus token-safe JSON export links for support,
  debugging, and dashboard trust checks; and
- a first-class CodeMower.com lineage drilldown at `/dashboard/lineage`, so
  signed-in users can inspect issue -> posted plan -> work order -> pull
  request -> reviewer checks -> merge -> upload chains without confusing
  operational dogfood with calibrated reviewer evidence; and
- a clearer cloud catch-up story: routine dogfood uploads represent current
  metadata, while historical imports must be run explicitly through
  `code-mower cloud catch-up` or `repo-sync --mode catch-up` and are displayed
  as imported history rather than calibrated reviewer evidence; and
- a local Codex dogfood proof using a dashboard-issued token: the public beta
  uploaded current metadata for Code Mower OSS, CodeMower.com, and two private
  reference/product repos, preserving metadata-only payloads and surfacing
  provider/model provenance gaps without blocking operational uploads; and
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
- provider metadata helpers now live under `code_mower.providers`, including
  local CLI version probes used by doctor and cloud provenance. This is the
  start of the broader provider-adapter cleanup while keeping the CLI-first API
  stable for beta users.
- Package materialization has started the same intentional split:
  package file manifests now live under `code_mower.package_manifest`, and
  generated package content builders and CLI command inventory now live under
  `code_mower.package_content`; generated static package file bodies live under
  `code_mower.package_static`; YAML/provider-catalog rendering helpers live
  under `code_mower.package_rendering`.
  Package-aware config/template path helpers live under
  `code_mower.package_paths`, while `package.py` remains the
  backwards-compatible CLI and manifest-generation surface.
- Package-install rehearsal flow now lives under
  `code_mower.migration_rehearsal`; clean-venv/pip/toy-repo command primitives
  live under `code_mower.migration_install`; first-user readiness scoring lives
  under `code_mower.migration_readiness`; and mirror-removal planning plus
  runner-alias reporting live under `code_mower.migration_mirror`.
  `migration.py` remains the backwards-compatible migration command adapter for
  wrapper rehearsal, release-readiness routing, mirror planning, and
  package-install orchestration.
- native local visibility through `code-mower lanes status` and
  `code-mower board serve`, with local paths redacted by default, explicit
  local-history recording, Board doctor/reset commands, and an explicit
  zero-report `cloud board-snapshot` upload path for CodeMower.com mirrors.
- local productivity and provider-scorecard visibility through
  `code-mower productivity report --repo OWNER/REPO` and the Board's embedded
  productivity block, backed by metadata-only `productivity_summary` events for
  CodeMower.com.

Code Mower is ready for small, supervised pilots in real repositories. It is not
yet ready for broad, automatic org-wide rollout or uncalibrated merge gates.
The v0.6 provider-contract hardening queue started from the dated
[v0.6 truth baseline](v06-truth-baseline.md), which records the current
release, provider-runner, Gemini/Antigravity, SDK-research, and privacy-boundary
facts that future refactors must preserve.

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
- `https://codemower.com/dashboard/productivity`

The cloud service currently supports:

- metadata-only ingest bundles;
- structured benchmark events;
- per-team ingest tokens;
- a protected dashboard for team/token management;
- GitHub, Google, and Apple login UI through Supabase Auth;
- dogfood uploads from Code Mower and product development;
- per-upload and per-event evidence detail URLs plus JSON export links for
  signed-in users; and
- productivity-summary and provider-scorecard views from metadata-only
  `productivity_summary` events; and
- self-service metadata export and deletion for signed-in team members/admins.

The next CodeMower.com product slice is dashboard usefulness rather than raw
receipt volume: clearer imported-history versus calibrated-evidence labeling,
more visual provider/lens signal, and team-level recommendations that answer
"what should I enable next?" That plan is maintained in the CodeMower.com
operator docs so the public OSS repository stays focused on the installable
client and metadata contract.

It does not yet provide automated retention jobs or true cross-team cohort
benchmark calculations. Those are preconditions for broad cloud-data collection
beyond friendly pilots.

Dashboard provenance is part of the product contract: routine dogfood/current
metadata, imported GitHub Actions history, and calibrated reviewer/lens evidence
must stay visually and analytically distinct. Workflow history can prove
activity and upload health, but it is not the same as reviewer-quality evidence.

OAuth, Supabase, Vercel, DNS, and hosted-secret setup are CodeMower.com
operator responsibilities. OSS users should only need a dashboard-issued or
operator-issued developer/team token when they opt into cloud sharing.

## Current Supervised-Pilot Goal

The current v1.0 release is the shareable supervised-pilot package line for
20-50 early OSS users who can follow a guide without knowing the original
reference repos.

The early-adopter experience should be:

1. install Code Mower from PyPI;
2. run `code-mower init --easy`;
3. run `code-mower doctor --adoption --repo OWNER/REPO`;
4. run a first manual/local audit;
5. run `code-mower lanes status --repo OWNER/REPO` and
   `code-mower board serve --repo OWNER/REPO`;
6. generate a local reviewer value report;
7. optionally create or receive a CodeMower.com developer/team token; and
8. optionally upload sanitized benchmark metadata and an explicit Board
   snapshot.

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

The v1.0.2 line now gives adopters that trust test plus first productivity
visibility. More provider adapters are useful only when install, doctor, first
report, privacy, measurement, and code structure remain boring and credible.

## v0.5 Beta Learning Addendum

A short PRD addendum captures the v0.5 beta lessons without rewriting the
product requirements: installed-package rehearsals are release-gating, dashboard
trust depends on provenance labels, and current dogfood metadata is not the same
as historical benchmark backfill. See
[`docs/prd-addendum-v05-beta.md`](prd-addendum-v05-beta.md).

## Fresh-Eyes Feedback Incorporated

Recent external first-impression reviews converged on the same pattern: the
thesis, privacy posture, and package layout are compelling, but the path from
"I found this repo" to "I learned which AI reviewer is useful on my codebase"
still has too much setup friction.

Treat these as product gates before widening beyond friendly early adopters:

- **Install friction:** GitHub-tag installs are acceptable as a fallback, but
  public adoption should default to the PyPI package path.
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

The next fresh-eyes round added a sharper engineering-readiness point: Code
Mower now looks like a real beta from the outside, but v1.0 should not merely
polish the first-run path. It should also make the implementation look
intentional to a senior engineer reading the package for the first time. That
means the remaining "extraction-era" seams are product work, not cleanup
churn:

- **Provider wrappers:** `codex_audit_pr.py`, `claude_audit_pr.py`,
  `gemini_cli_audit_pr.py`, `local_llm_audit_pr.py`, and similar wrappers
  should share a provider-runner base instead of duplicating checkout, PR
  loading, subprocess, verdict parsing, comment posting, and cleanup flow.
- **CLI/package imports:** direct-source compatibility shims and `tools`
  fallbacks should be removed from shipped package entrypoints once the PyPI
  path is the public happy path. A direct source checkout can fail with a clear
  "install the package or use scripts/dev-python" message instead of carrying
  confusing legacy import branches.
- **Top-level shape:** provider-specific runners, package/materialization
  internals, cloud commands, and experiment harnesses should continue moving
  into subpackages until the package root reads like a product API, not a
  scripts directory.
- **Static confidence:** broaden lint/type checks gradually. Ruff should move
  beyond syntax/undefined-name once module boundaries stabilize, and a
  narrowly scoped type-checking gate should start with the most stable domain
  modules before becoming a repo-wide v1.0 bar.
- **Zero-config first value:** `init --easy` and
  `doctor --adoption --repo OWNER/REPO` are good, but a future
  `code-mower try OWNER/REPO` or equivalent should produce a draft
  corpus/value report from recent PR history with minimal setup.

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

GitLab, Bitbucket, ACP bridges, hosted builder harnesses that launch sessions,
and fully automated authoring-run capture remain post-v1.0 work. v0.6 includes
source-free builder provenance through `code-mower builder record` and the
first subprocess-backed `code-mower builder-experiment run` path: hosted or
local builders can open a PR, then Code Mower records provider/executor, issue,
work order, PR, branch, model/version hints, timing, status, and intervention
metadata without source, diffs, or transcripts.

## Builder And Orchestrator Direction

Reviewer calibration is the current executable loop: compare reviewers and
lenses against known-clean, known-blocked, and subtle-risk PRs. Builder-side
experiments are the next major extension: compare which AI peer programmer plus
review policy ships verified code fastest and cleanest. The first supported
posture is observation, not orchestration: Grok Bot, Cursor Cloud Agents, Devin,
Claude, Codex, or another builder can create a branch/PR through their normal
surface, then `code-mower builder record` captures the source-free delivery
provenance.

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

1. Keep the v1.0 package install path verified from PyPI, and mark the newest
   GitHub release as Latest.
2. Run one cold-repo adoption rehearsal from the published package: install,
   `init --easy`, `doctor --adoption`, `lanes status`, `board serve`, one tiny
   audited PR, and optional metadata-only cloud upload.
3. Continue dogfooding metadata uploads from Code Mower, CodeMower.com, and
   product/reference work while keeping operational uploads separate from
   calibrated reviewer-quality evidence.
4. Deliberately route some small follow-up issues through Claude Code,
   Cursor/Grok Bot, Antigravity, Devin, and other available builders/reviewers
   so promotion decisions can use measured data rather than Codex-only
   throughput.
5. Make the public repository the unambiguous source of truth: keep public docs
   and releases flowing from `codemower-ai/code-mower`, reduce extraction-era
   compatibility shims where they confuse contributors, and keep private
   product repos as consumers of pinned releases.
6. Keep PyPI-first releases boring: every wider supervised-pilot release should
   verify GitHub release artifacts, PyPI trusted publishing, exact-version
   install, and package-install rehearsal from the published package.
7. Add a short terminal recording or screenshot showing `doctor --adoption`,
   `lanes status`, `board serve`, and the first value-report path. A static
   transcript now exists in
   `docs/first-run-transcript.md`; replace or augment it with a recording
   before a wider launch.
8. Enable Supabase Auth providers for CodeMower.com and verify GitHub, Google,
   and Apple login end to end.
9. Turn the current team-controlled deletion/export basics into a published
   retention policy with automated retention jobs before broad cloud-data
   invitations.
10. Expand the calibration corpus with known-clean, known-blocked, and subtle
   architecture-risk PRs.
11. Run reviewer/lens calibration across Codex, Claude, Antigravity/Gemini,
   Gitar, and available informational lanes.
12. Produce durable reviewer value reports with useful-rate, false positives,
   latency, and cost.
13. Promote lanes only after evidence shows they deserve informational,
   selective, or merge-gating status.
14. Increase tests around verdict parsing, calibration/value-report math,
    provider runner stubs, and cloud bundle privacy before presenting Code
    Mower as merge-gate infrastructure.
15. Extract shared provider-runner primitives so the main provider wrappers are
    thin adapters around a tested PR-audit pipeline.
16. Remove remaining shipped-package dual-import and `tools` fallback shims once
    release rehearsals prove source-checkout users have a clear supported path.
17. Add a file-size/module-boundary review gate for the root package. Start by
    splitting `init`, `cloud`, `config`, `cli`, and provider wrappers where it
    improves contributor comprehension.
18. Introduce static analysis in stages: broaden Ruff on stable packages first,
    then add a scoped type-checking gate before making it a v1.0 release
    requirement.
19. Triage CLI help into a smaller first-user command set, with advanced
    operator/internal commands documented separately.
20. Harden calibration auto-discovery with more real PR shapes, first-user
    examples, and package-install rehearsal coverage so first reports can be
    bootstrapped from project history with human review.
21. Keep first-read README friction low: one-screen pitch, install, doctor
    sample, demo report, Board demo, and links to deeper docs.
22. Keep hardening reusable stale-audit lane handling with real product-repo
    feedback now that `clear-stale` and generated stale-clear workflows ship
    in the default merge-authority lane support.
23. Keep repository-native checks central: detect and run each repo's declared
    ESLint/Vitest/Ruff/pytest/build surface instead of treating Code Mower's own
    tooling as a universal product-repo lint policy.
24. Expand builder-experiment capture now that the reviewer/value loop and
    Board visibility path are producing durable evidence.
25. Keep commercial implementation, hosted reporting, telemetry products, and
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
