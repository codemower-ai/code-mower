# Changelog

All notable public Code Mower OSS changes should be summarized here. The
project used alpha/beta prerelease tags while the first-user setup path,
provider posture, and optional cloud sharing loop were hardening; v1.0 and
later entries are regular releases.

## Unreleased

No changes yet.

## v1.0.10

This patch makes local builder lanes deliverable and recoverable, simplifies
the first run, and publishes the 20-sample provider calibration scorecard. It
preserves supervised pilot gate semantics, Python 3.12+, and the metadata-only
privacy boundary.

### Added

- `code-mower lane-delivery` gives the local builder runner a provider-neutral
  delivery and recovery contract: delivery is classified from a validated
  issue/PR/head transition or a bounded `no_change`/`owner_action` outcome the
  runner validates itself, the wall-clock cap does not exempt a run from that
  classification, snapshot lookups fail closed instead of recording an empty
  value a comparison would read as delivery, providers run in their own process
  group that is terminated and reaped on timeout, interruption, and overflow,
  and an explicit orchestrator handoff (source lane, destination lane, target
  PR, expected head) is the only way to write a PR branch owned by another lane.
  A handoff only authorizes a branch the named source lane actually owns, and a
  bounded outcome only counts with a non-empty one-line summary. The cap is a
  clock, not a verdict: a run the supervisor stopped is judged on the transition
  it left behind, so work pushed before the cap fired still counts and a cap
  that produced nothing still does not, and one that produced nothing exits `3`
  rather than the supervisor's own `124`/`125`/`130`, which would report a cap
  as a provider failure. Only a provider that ended itself keeps its exit code.
  A bounded outcome is an alternative to delivery, never an addition to one: the
  runner reads the transition (`lane-delivery transition`, the comparison
  `classify` makes) before it writes anything to the target and brokers a
  declaration only when it observed no new pull request and no advanced head, so
  a provider that both declared and pushed cannot leave an owner-blocked pull
  request next to a comment saying nothing changed.
- Each local builder run records a metadata-only delivery outcome for Board and
  productivity reporting: provider exit, delivery transition, handoff, elapsed
  time, and intervention count.
- `CODE_MOWER_LANE_DELIVERY_CMD` pins which `lane-delivery` the Mac lane runner
  uses, ahead of this source checkout and any installed `code-mower`. It names
  one executable path, like the runner's other command overrides, so a pinned
  path containing a space is not truncated; pin a wrapper script to supply
  extra arguments.
- A first-class local Devin CLI provider contract, a maintained
  release-qualification adapter, a supervised builder lane with self-hosted
  runner support, and an informational reviewer lane (#746, PRs #747, #748,
  #749, #750).
- `code-mower participants` and host-led session briefs make reviewer and
  builder selection explicit for a session instead of implied by config
  (#756).
- `docs/provider-calibration-scorecard.md` publishes the 20-sample builder
  corpus: five bounded real issues each for Cursor, Muse, Antigravity, and
  Devin, with comparable aggregates, limitations, and role recommendations. It
  promotes no provider to reviewer merge authority (#765).

### Changed

- A supervised provider's own exit ends the run even when a background
  descendant still holds its stdout open. The supervisor drains what is in
  flight, then terminates and reaps the group, instead of waiting out the lane
  timeout behind a lingering transport and reporting a timeout for a provider
  that finished.
- Provider prompts assembled by the Mac lane runner are scanned and rejected if
  they would send a provider looking for token files, credential-helper output,
  or other auth material. The runner brokers the comments and labels the
  contract needs.
- First-run defaults and guidance are simpler: `init --easy` and `next-steps`
  present one recommended path with executable commands instead of a wide
  provider menu (#755).
- Mac lane runner builder prompts require issue-linked pull request delivery, so
  a run is judged from the pull request's GitHub closing-issue reference rather
  than a bare mention (#763).

### Fixed

- Package-install rehearsal omits empty optional pip flags instead of passing
  an empty argument to `pip install` (#740).
- Bounded hosted campaign result rejection reasons are surfaced in status and
  watch output, so a rejected result is distinguishable from a missing one
  (#741).
- The Codex campaign adapter compiles an API-compatible structured-output
  schema, which the API previously rejected before generation (#739).
- The Mac lane runner accepts empty optional provider extra flags under
  `set -u` instead of aborting the run (#753).

## v1.0.9

This patch makes the release-qualification loop resilient and explicit about
which provider evidence decides campaign completion. It preserves supervised
pilot gate semantics, Python 3.12+, and the metadata-only privacy boundary.

### Added

- Package-install qualification records a closed failure phase, reason, and
  bounded remediation without retaining raw command output (#727).
- Release campaigns accept an explicit required-provider subset and carry the
  resulting required or informational posture through status, watch, Board,
  hosted dispatch markers, and opt-in adoption events (#730).

### Changed

- Informational provider failures remain visible but no longer block a campaign
  after every required provider has produced passing evidence. Campaigns that
  omit the new option retain the prior all-required behavior (#730).

### Fixed

- Prevent Board status cache from remaining stuck in `refresh_in_progress` after
  abnormal background thread exits or abandoned refreshes by safely recovering
  into bounded retry backoff (#728).

## v1.0.8

This patch closes trust gaps found while dogfooding the multi-provider release
campaign. It preserves supervised-pilot gate semantics, Python 3.12+, and the
metadata-only privacy boundary.

### Added

- Local campaign adapters prove that the requested Python 3.12+ runtime ran the
  candidate and that the expected closed result artifact was produced (#710).
- Release qualification accepts an explicit, closed TestPyPI package source so
  the exact candidate can be installed before production publication (#713).
- Hosted Cursor Cloud Agent and Devin profiles require five explicit readiness
  checks for dispatch and result transport before an applied campaign can start
  (#718).

### Changed

- Campaign retries retain ordered attempt chronology and terminal failure
  evidence, making timeout and retry behavior auditable without raw output
  (#711).
- Campaign discovery uses a metadata-only user index so status, watch, upload,
  and Board continue to find campaigns after a checkout or worktree changes
  (#712).
- Cursor Cloud Agent is the builder identity; Cursor BugBot and Grok Bot remain
  reviewer identities, preventing hosted builder results from being attributed
  to the review lane (#717).
- Antigravity campaigns require an explicit project identity and isolate each
  execution without relying on ambient IDE state (#721).

### Fixed

- Codex and Claude audit transport preserves a valid verdict when a
  metadata-only P3 finding uses line 0; blocking findings still require an
  actionable source line (#681).

## v1.0.7

This patch hardens multi-provider release qualification using findings from the
first applied v1.0.6 campaign. It preserves supervised-pilot gate semantics,
Python 3.12+, and the metadata-only privacy boundary.

### Added

- Adoption doctor probes isolated authentication readiness for maintained local
  adapters when a provider offers a safe, bounded login-status command. Known
  logged-out states become owner actions; unsupported or indeterminate probes
  remain non-blocking and never expose auth output or local paths (#699).
- Hosted Cursor/Grok Bot and Devin campaign cards expose transport verification
  and bounded response deadlines before dispatch, with manual fallback instead
  of automatic duplicate paid retries when a provider stays silent (#701).

### Changed

- Board projects checkpointed local adapters as running only inside their
  effective timeout, then presents coherent stale retry guidance at card,
  campaign, and top-level scope without mutating persisted campaign state
  (#698).
- Adoption-result validation enforces bounded timestamps, a stable built-in
  step taxonomy with namespaced extensions, explicit overhead, coherent timing
  totals, and zero owner actions for passing results (#700).

### Fixed

- Isolated Codex campaign execution preserves the real OS home needed for macOS
  keychain discovery while keeping provider configuration and state isolated in
  `CODEX_HOME` (#696, PR #697).

## v1.0.6

This patch completes the multi-provider release-qualification operator loop.
It preserves the supervised-pilot gate semantics and metadata-only privacy
boundary while replacing manual prompt relays with maintained local adapters,
bounded hosted dispatch, watch, upload, and readiness diagnostics.

### Added

- Maintained release-campaign adapters for Codex, Claude, Antigravity, and Muse,
  with isolated provider homes, bounded execution, closed result validation,
  and no persisted raw provider output (#683, #691).
- Safe hosted qualification profiles for Cursor/Grok Bot and Devin, including
  idempotent dispatch markers, trusted result polling, explicit retries, and
  fail-closed outage behavior (#684, #688).
- `code-mower release campaign watch` polls local and hosted providers until
  completion, timeout, or a genuine owner action while preserving read-only,
  bounded watch behavior (#686, #692).
- `code-mower release campaign upload` previews a closed metadata-only bundle by
  default and requires explicit confirmation before CodeMower.com upload (#685,
  #689).
- `code-mower doctor --adoption` now reports campaign adapter, hosted credential,
  storage, cloud-profile, Board, and aggregate preview readiness across the six
  supported qualification providers (#687, #690).

### Changed

- Campaign state handling now shares bounded cross-platform locks, atomic
  persistence, exact campaign identity, and the same cloud-token resolver used
  by upload commands.

## v1.0.5

This patch makes release qualification a repeatable, visible, metadata-only
operation across Code Mower's supported provider environments. It preserves
gate semantics and keeps operational qualification separate from builder and
reviewer quality evidence.

### Added

- `code-mower release qualify` installs an exact package in an isolated
  environment and records a closed, path-free `code_mower.adoptionResult.v1`
  result for install, doctor, lane-status, and Board checks (#669, #672).
- `code-mower release campaign` coordinates local and hosted providers with
  fail-closed adapters, trusted result polling, idempotent resume/retry,
  cross-platform locking, atomic state, and live Board campaign cards (#670,
  #675).
- The additive `adoption_run` cloud event carries categorical outcomes,
  provenance coverage, counts, and timings without source, diffs, transcripts,
  issue text, raw output, auth output, local paths, or secrets (#671, #674).
- CodeMower.com aggregates adoption runs by release and provider while remaining
  backward-compatible with uploads that omit the new event type (dashboard
  PR #178).

### Changed

- Public release-qualification and contributor documentation now explains the
  operational evidence boundary and uses a working clean-clone command (#677,
  #678).

## v1.0.4

This patch tightens the supervised-pilot install, diagnostics, and generated
workflow surface without changing gate semantics or the metadata-only privacy
boundary.

### Changed

- The standalone-wrapper reinstall test now uses a deterministic local,
  standard-library-only build backend and proves that its offline fixture does
  not invoke pip or a package index (#656).
- Setup-drift output identifies packaged versus explicitly selected
  configuration, and hosted-agent posture guidance appears once before
  provider-specific warning detail (#656).
- Generated workflows that receive `DISPATCH_TOKEN` use trusted default-branch
  definitions through `pull_request_target`, retain the same-repository guard,
  and never check out pull-request code (#654).
- The tagged installation, upgrade, hosted-agent, CLI, link, and privacy flows
  were audited at the release baseline, with 172 local Markdown targets and
  anchors checked successfully (#657).

## v1.0.3

This patch is the confidence-polish release for experienced senior engineers
adopting the v1 supervised pilot. It preserves gate semantics and the
metadata-only privacy boundary while making Board operation and first-use
diagnostics faster, safer, and clearer.

### Changed

- Board `/api/status` now uses a stale-while-refresh cache, returning a safe
  warming payload on cold start and millisecond-fast cached responses while a
  single background refresh updates GitHub and local state (#643).
- Adoption diagnostics now identify packaged versus explicit configuration,
  productivity evidence reports local coverage honestly, and stale local agent
  cards can be pruned through an explicit confirmed command (#644).
- Board stop and stale-card pruning now defend against PID reuse, symlinked
  adapter directories, malformed PID values, partial cleanup failures, and
  exception-content leakage (#644).
- Public configuration, security posture, contributor-facing docs, and the
  metadata-only privacy contract were reviewed and normalized for an OSS
  checkout without embedding maintainer-specific identity (#645).

## v1.0.2

This patch hardens first-user and release-smoke ergonomics after the v1.0.1
adoption round. It keeps gate semantics, dashboard schemas, and the
metadata-only privacy boundary unchanged.

### Changed

- `migration package-install-rehearsal` now disables pip cache and retries
  package-index installs after `--allow-package-index`, reducing false release
  blockers while PyPI/TestPyPI finishes fresh-package propagation (#635).
- Adoption-polish diagnostics now give clearer cwd/config guidance for
  `doctor` and `init`, label legacy Board listeners as restart recommended,
  include both Board history capture paths in empty productivity reports, make
  hosted-agent `uv tool install` first-class in quickstart, and accept
  `controller run --dry-run` as an explicit dry-run alias (#637).

## v1.0.1

This patch adds local and hosted productivity intelligence to the v1.0
supervised-pilot release while keeping gate semantics and the metadata-only
privacy boundary unchanged.

### Added

- `productivity_summary` is now part of the cloud data contract, and
  `code-mower productivity report --repo OWNER/REPO` summarizes local Board
  history, reviewer-spend rows, and optional aggregate productivity events
  (#613, #614).
- Board status now embeds a compact productivity summary and provider
  scorecards so operators can see reviewer activity, spend/latency, blocked
  findings, and promotion evidence from the local loop (#614, #616).
- CodeMower.com has a protected productivity dashboard for the same
  metadata-only event shape, with the server remaining backward-compatible with
  earlier beta uploads (#615).
- `code-mower board list` and `code-mower board stop` provide safe
  multi-instance Board administration after upgrades or parallel agent sessions
  (#617).
- v1.0.1 release notes and an effectiveness assessment now record the
  productivity, quality, cost, provider-readiness, and measurement limits for
  the dogfood loop (#618).

## v1.0.0

This release is the supervised autonomous pilot baseline. It is ready for
senior engineers and agent operators to install from PyPI, wire into one
repository, run visible builder/reviewer lanes, and make evidence-based
promotion decisions while keeping human or trusted-orchestrator supervision.

### Added

- Supervised controller dry-runs summarize lane state, merge posture, and the
  policy decision without mutating the repository.
- `doctor --supervised-pilot`, `--manual-pilot`, and `--promoted-pilot` make
  adoption posture explicit and classify promotion todos separately from
  ordinary warnings.
- Board surfaces the supervised-pilot snapshot locally, including next actions,
  owner actions, lane health, package/serving-version status, and optional
  metadata-only agent cards.
- CodeMower.com can receive metadata-only supervised Board snapshot events, and
  provider-diversity fixtures cover Claude Code, Codex, Cursor/Grok Bot,
  Antigravity, Muse, Devin, Gitar, and CodeRabbit-style evidence.
- The universal orchestrator prompt pack gives supported builders and reviewers
  the same install, upgrade, privacy, and reporting path.

## v0.9.4-beta.1

This beta is the final pre-announcement hardening pass for the v0.9 line. It
keeps the privacy boundary, gate semantics, and generated workflow names
unchanged while tightening the first-run, upgrade, and dogfood paths.

### Changed

- Added a root `code-mower.yml` dogfood config for the Code Mower repository so
  local checks and release workflows exercise a real adopter-style config
  instead of packaged starter defaults (#590).
- `doctor --adoption` now reports owner actions separately from ordinary
  warnings in text and JSON output, while preserving stricter non-adoption
  reviewer-gate failures (#602).
- `code-mower migration setup-drift --repo-path .` now always reports
  standalone pin posture and warns when existing builder/dispatch files are
  present but `--builders` was omitted, including Codex, Claude, Cursor, and
  self-hosted Mac runner hints (#603).
- Adoption docs and the orchestrator prompt pack now point cold agents at the
  same-tag install and upgrade docs, hosted-agent doctor postures, and Board
  `/api/status` restart diagnostics (#604).
- Release notes call out the experimental Muse CLI lane (`muse_cli`,
  `builder:muse`, `needs-muse-audit`) as calibration-only until repository
  evidence supports promotion.

## v0.9.3-beta.1

This beta is a small confidence-polish release after the v0.9.2 adoption pass.
It tightens first-run and upgrade diagnostics without changing the privacy
boundary, gate semantics, or generated workflow names.

### Changed

- `doctor --adoption` can now verify packaged-starter review-hygiene workflow
  paths against the current repository checkout. Missing workflows remain a
  warning in starter mode, while real repository configs still fail when a
  required clear-stale workflow is absent (#588).
- `code-mower lanes status --repo OWNER/REPO` now promotes the selected
  PR-specific `next_detail` into the top-level text and JSON summary so pasted
  operator updates include the stale audit/gate requeue detail without expanding
  each PR (#591).
- The local Board now reports the Code Mower version currently serving the
  browser page and shows a restart hint when a newer installed package version
  is available (#591).
- `doctor --adoption` now gives a plainer posture hint for hosted/orchestrator
  machines, and trusted audit-author variable probes report failed GitHub reads
  as `not_confirmed` instead of implying confirmed missing configuration (#593).
- `code-mower migration setup-drift --repo-path .` now includes a read-only
  standalone pin summary so existing repos can see when
  `tools/code_mower_standalone_pin.env` points at a different Code Mower ref
  than the currently running package (#595).

## v0.9.2-beta.1

This beta is a small announcement-hardening cleanup after v0.9.1. It removes
the retired third-party observe bridge, keeps dispatch-token expiry metadata
from blocking otherwise configured adopters, and makes stale audit waits easier
to act on from `lanes status` and Board.

### Changed

- The native Board is now the only local visibility surface; the retired
  third-party observe bridge, its port probing, and its legacy JSON alias were
  removed (#579, #580).
- `doctor --adoption --repo OWNER/REPO` treats missing, placeholder, malformed,
  or expired `DISPATCH_TOKEN_EXPIRES_AT` metadata as a warning once the
  `DISPATCH_TOKEN` secret exists; missing dispatch secrets still fail in
  reviewer-gate posture, and `never` is accepted for intentionally
  non-expiring tokens (#582, #583).
- `code-mower lanes status --repo OWNER/REPO` now reports stale
  `needs-*-audit` waits as a requeue action with a short runner/dispatcher
  detail, and stale pending gate waits surface a `rerun stale gate` headline
  while preserving the paste-safe gate rerun command (#581, #584).

## v0.6.0-beta.3

This beta focuses on cold-adoption hardening from the first real v0.6
rehearsals: clearer setup state, fewer first-PR surprises, safer manual gate
reruns, and a copy-pasteable prompt pack for agent orchestrators.

### Added

- `doctor --adoption --repo OWNER/REPO` now targets the real repository during
  first setup and reports repo/config mismatch clearly (#479).
- `init --easy --apply` now emits an editable `code-mower.yml` alongside the
  generated workflows so owner login, decision authorities, and trusted audit
  authors are visible before the first setup PR (#481).
- `code-mower lanes status --repo OWNER/REPO` now includes the current PR head
  SHA and a copy-pasteable `code-mower-gate.yml` workflow dispatch command when
  a PR needs a manual gate recompute (#487).
- `docs/orchestrator-prompt-pack.md` gives early adopters prompts for Claude
  Code orchestration, builder handoffs, reviewer handoffs, and owner status
  snapshots while preserving single-writer and metadata-only boundaries (#489).

### Changed

- Generated GitHub workflow templates, package fallback builders, and
  Code Mower-owned checked-in workflows now use the pinned `actions/checkout`
  v7.0.1 commit SHA; release hygiene guards reject the previous v6 pin or
  mutable checkout tags returning to these surfaces (#476).
- Generated smoke tests now avoid writing Python bytecode caches into the first
  setup PR workspace (#483).
- Generated Claude and Codex audit wrappers now use the installed `code-mower`
  CLI while the standalone pin file is still a placeholder, with explicit
  standalone override behavior preserved (#485).
- Pilot auto-merge doctor remediation now points to lane-promotion policy and
  treats disabled auto-merge as expected until a lane is promoted (#487).

## v0.6.0-beta.2

This beta hardens optional CodeMower.com uploads for first-week adopters by
making local cloud token setup survive shell and app restarts.

### Changed

- `code-mower cloud setup` now records the just-written token file as the current
  local cloud profile, and upload-style cloud commands can resolve the token from
  live env, an explicit token file, an install-id profile, the current profile,
  or one unambiguous stored profile (#474).
- `cloud doctor` reports token-profile ambiguity or malformed stored profiles
  without printing token values, and every network upload path still requires
  explicit `--yes` before sending metadata (#474).

## v0.6.0-beta.1

This beta finishes the v0.6 provider-contract hardening workstream and makes
the cold-adopter loop clearer: install, preflight, observe active lanes, run
manual reviewer gates, calibrate before promotion, and optionally upload
metadata-only evidence to CodeMower.com.

### Added

- `code-mower lanes status --repo OWNER/REPO` gives operators one concise,
  read-only snapshot of active PR lanes, labels/checks, recent Code Mower
  workflows, gate alerts, local Board listeners, likely local lane processes,
  and the next action (#470, #471).
- `code-mower builder-experiment run` records source-free authoring-run
  metadata around an explicit local command, including timing, status,
  branch/PR, command hash, and builder provenance (#458, #459).
- `code-mower providers antigravity-sdk-probe` adds an optional metadata-only
  SDK readiness probe for future Antigravity SDK work without calling auth or a
  model by default (#456, #457).

### Changed

- Provider output parsing is fixture-locked, schema-validated, and backed by
  incremental `code_mower.provider_runners` helpers for comments, exit codes,
  worktrees, PR metadata, subprocess handling, and verdict artifacts (#442,
  #444, #445, #447).
- Static checks are stricter in low-noise areas while keeping the beta package
  lightweight (#449).
- Antigravity CLI stays manual/informational, with clearer lane identity,
  doctor/auth posture, and provenance separation from Gemini CLI (#451, #453).
- Builder-run upload mapping now accepts local `authoringRun` artifacts as
  normalized metadata-only `builder_run` cloud events (#468, #469).
- README, quickstart, build-loop, release, and rollout docs now describe the
  v0.6 adoption path, lane-status command, safe GitHub Markdown posting, and
  release procedure consistently (#467, #470, #472).

## v0.5.0-beta.52

This beta packages the adoption-readiness work from the build-loop dogfood:
merge-gate diagnostics, preflight hard-fails, self-hosted runner guidance,
builder-loop templates, and refreshed first-user docs.

### Changed

- Merge-gate handling now fixes in-flight audit terminal runs and adds
  diagnostics for status-source behavior, closing the stale gate issue found in
  #404 (#412).
- Audit budgets and generated diff limits are now size-aware, giving larger
  reviews bounded but more realistic reviewer capacity (#414, #416).
- `doctor --preflight` now hard-fails missing human-token posture, incorrect
  branch-protection source binding, and disabled `allow_auto_merge` setup
  before a repository depends on the generated gate (#417).
- Self-hosted Mac runner setup now has a dedicated guide and `doctor --runner`
  coverage for runner labels, LaunchAgent health, and generated workflow
  readiness (#419).
- Audit decision markers are authority-gated, and the provider registry
  delivery path no longer leaks registry context into Codex review prompts
  (#418, #424).
- `init --builders` now ships build-loop workflow templates and a lane runner
  for builder dispatch and follow-up work (#420).
- Getting-started docs are restructured around reviewer-gate and build-loop
  paths, including an orchestrator worked example (#425).
- Announcement and early-adopter docs were cleaned up for the beta.52 public
  baseline (#421).

## v0.5.0-beta.51

This beta carries the merge-gate status-source fixes found while dogfooding
beta.50 on Bridge Pro, plus generated dispatcher backoff for GitHub API rate
limits.

### Changed

- Generated `code-mower-gate.yml` now keeps the merge signal as the
  `code-mower/gate` commit status while using a distinct GitHub Actions job
  name, so branch protection can require the status from Any source without a
  competing Actions check-run (#399, #400).
- Doctor, init, and setup docs now call out the required branch-protection
  source binding: `code-mower/gate` should use `checks[].app_id: null`, not the
  GitHub Actions app binding (`15368`) (#400).
- Generated agent-labeler and fix-round dispatch templates detect GitHub API
  rate-limit responses, validate reset timestamps, back off briefly when
  possible, and otherwise exit cleanly so a later event can retry (#400).
- Setup docs now require a pre-announced owner/admin bypass when GitHub
  incidents prevent the normal gate from completing (#400).

## v0.5.0-beta.50

This beta lands the #391 follow-up from the Bridge Pro merge-gate dogfood,
separating owner-bound work from lane capacity while tightening generated
workflow liveness, fix-round, token, escalation, and provider-scaffold
guardrails.

### Added

- Owner-bound tasks can stay visible without consuming lane WIP, and stale WIP
  diagnostics now distinguish active builder capacity from physical owner work
  (#385).
- Lane liveness checks report stalled, missing, or unhealthy reviewer/builder
  lanes from metadata-only GitHub workflow state (#386).
- Generated templates include fix-round dispatch and agent-PR auto-labeling
  support for the review loop (#387).
- Setup and doctor guidance now make the required human GitHub token posture
  explicit before repositories depend on hosted builders or merge automation
  (#388).
- Owner-decision escalation separates raw owner notifications from triaged
  owner decisions in generated owner-surface templates and status reports
  (#389).
- Provider-integration prompts now guard against sandbox/live shared-namespace
  mistakes in commerce-style scaffolds, requiring environment-explicit,
  read-before-create, idempotent setup (#390).

## v0.5.0-beta.49

This beta carries the generated gate fixes found immediately after beta.48
landed in Bridge Pro.

### Changed

- Generated `code-mower-gate.yml` preserves the `code-mower/gate` job display
  name for repositories that require the GitHub Actions check-run (#368).
- Generated `code-mower-gate.yml` treats GitHub auto-merge enablement as an
  optional post-green action, so a token permission denial logs a notice without
  failing an otherwise green gate (#370).

## v0.5.0-beta.48

This beta closes the same-head audit race found while dogfooding Code Mower as
the Bridge Pro merge gate.

### Changed

- Generated `local-cli-audit.yml` now scopes concurrency by workflow, PR,
  current head SHA, and lane, with `cancel-in-progress: true` so superseded
  same-head audit jobs cannot post late contradictory verdicts (#365).
- Generated `code-mower-gate.yml` now reports `pending` while a required
  current-head audit run is queued or in progress, re-checks after the local
  audit workflow completes, and lets the newest attested same-head verdict win
  when deciding whether a lane is green or blocked (#365).
- The gate posts a metadata-only history-rewrite warning when a previously
  attested head is no longer an ancestor of the PR head, and docs now spell out
  the single-writer PR branch rule plus `--force-with-lease` posture (#365).
- The reusable gate-health workflow template, command, and docs are included in
  the beta line for product repos that need stalled-audit and runner-health
  escalation (#337).

## v0.5.0-beta.47

This beta carries the local-audit runner and generated-workflow hardening found
while dogfooding Code Mower as the Bridge Pro merge gate.

### Changed

- Codex and Claude `UNKNOWN`/`STALE` audit comments now include
  machine-readable requeue markers, and gate-health prefers the latest marker
  when deciding whether an audit lane is still actionable (#336).
- The macOS runner LaunchAgent doctor handles unavailable home-directory
  lookups as a warning instead of crashing the check (#336).
- Generated `local-cli-audit.yml` keeps `${{ runner.temp }}` in step-level
  environment only, avoiding invalid job-level `runner` context use (#358).
- CI now actionlints every easy-mode generated workflow with self-hosted runner
  labels configured, and package guards catch generated workflow templates that
  reintroduce job-level `runner` context (#358).
- Doctor and gate-health now flag failed local-audit workflow runs with no jobs
  as likely workflow syntax/context failures, while avoiding cancelled-run
  false positives and keeping recent workflow-run sampling bounded (#358).

## v0.5.0-beta.46

This beta carries generated GitHub workflow fixes needed by product repos that
use Code Mower as a merge gate with owner override and stale-audit cleanup.

### Changed

- Generated `code-mower-gate.yml` keeps owner-only `gate:override` support,
  including non-owner failure and owner-applied success paths.
- Generated stale-audit cleanup workflow concurrency is now lane-specific, so
  Codex and Claude cleanup jobs cannot cancel each other on the same PR.
- GitHub setup docs now call out the owner-only override posture and stale
  cleanup concurrency behavior.

## v0.5.0-beta.45

This beta carries the audit-duration telemetry needed by product repos that use
Code Mower as a merge gate and upload reviewer-run evidence to CodeMower.com.

### Changed

- Codex and Claude audit verdict artifacts now include bounded
  `duration_seconds` metadata for current-head PASS/BLOCKED/UNKNOWN/STALE runs.
- Cloud reviewer-run export maps verdict artifact durations into
  `duration_seconds_total`, so CodeMower.com can report lane latency without
  storing review bodies, diffs, or transcripts.

## v0.5.0-beta.44

This beta fixes the fixture-verdict leak found during the beta.43 release gate
before downstream Bridge Pro pins move again.

### Changed

- Codex and Claude audit wrappers now isolate pytest/runtime fixture output:
  pytest-only quarantines never post to GitHub, while non-test structured
  fixture verdicts post a visible `UNKNOWN` requeue trailer and keep their
  local artifacts quarantined (#339, #341).
- Reviewer-run export and calibration auto-discovery now ignore fixture-shaped
  or quarantined verdict artifacts/comments so test output cannot become cloud
  dashboard or calibration evidence (#339, #341).
- Test runs now isolate `HOME`, `XDG_CACHE_HOME`, audit artifact directories,
  GitHub tokens, and non-local network access; the real audit cache guard fails
  on fixture-shaped mutations without tripping over concurrent legitimate
  runner audits on the shared machine (#339, #341).

## v0.5.0-beta.43

This beta fixes a Claude audit merge-gate safety issue found while dogfooding
the beta.42 local-audit uploads.

### Changed

- Claude audit now rejects schema-placeholder structured verdicts, blocker
  findings that cite files outside the PR diff, and implausibly short blocked
  verdict bodies before a result can become a label-bearing comment (#338,
  #340).
- Guardrail rejections retry Claude once with a corrective trusted instruction;
  a second unusable result becomes `UNKNOWN` with the requeue trailer instead of
  a merge-gating blocked label (#338, #340).
- Saved Claude verdict artifacts now get a local-only raw CLI output sidecar and
  backfill `posted_comment_url` after a successful GitHub comment POST (#338,
  #340).

## v0.5.0-beta.42

This beta lands the post-beta.41 gate hardening from epic #302 so hosted
builders and local reviewer lanes can keep Code Mower as a trusted merge gate.

### Added

- Local audit workflows can auto-ingest verdict, spend, and work-order metadata
  when `CODE_MOWER_CLOUD_TOKEN` is configured, while preserving the existing
  metadata-only cloud data contract (#330, #331).
- `cloud reviewer-runs` now supports incremental offset uploads for local audit
  artifacts (#330, #331).
- Generated local audit workflows include the gate-health support path learned
  from self-hosted runner dogfood (#317, #318).

### Changed

- Audit-run attestation now fails closed for `github-actions[bot]` results and
  is shared by the gate, gate-health, and labeler paths (#332, #333, #334,
  #335).
- Local PR audit dispatch skips the PR-head fetch when the checkout is already
  at the requested head SHA (#315, #316).
- Release automation uses the updated PyPI publish action dependency (#285).

## v0.5.0-beta.41

This beta lands the Code Mower dogfood improvements from epic #302 so Bridge
Pro can use Code Mower as the merge gate for three AI builder lanes.

### Added

- Generated workflows now support the Code Mower merge gate as a commit status,
  auto-merge enablement, author-lane exclusion, builder identity mapping,
  self-hosted local audit dispatch, owner escalation, and multi-repo
  `init --add-repo` rollout (#291, #292, #293, #300, #301).
- Reviewer spend/latency capture, automatic builder provenance, plan-conformance
  audit context, over-generation guardrails, and per-lane audit-comment
  attribution now have metadata-only OSS contracts and tests (#294, #295, #296,
  #297, #298).

### Changed

- Labeler, clear-stale, cleanup, and runner documentation now match the pinned
  wrapper flags, pull-request label permissions, and configured merge posture
  used during Bridge Pro dogfood (#290, #299).
- `docs/cloud-data-contract.md` documents the new optional upload fields while
  keeping CodeMower.com backward-compatible with beta.40 metadata uploads.

## v0.5.0-beta.39

This beta hardens the Grok Build reviewer lane after the first Bridge Pro
dogfood run showed that one-turn headless audits can stop before returning
structured verdict JSON.

### Changed

- `code-mower grok-build` now uses a configurable PR-audit turn budget with a
  default of four turns instead of hardcoding a one-turn run.
- The runner records the selected Grok turn budget in diagnostics so
  CodeMower.com and local artifacts can explain parse/turn-budget failures.
- A regression test now verifies the default and custom Grok turn budget passed
  to the local CLI.

## v0.5.0-beta.38

This beta adds Grok Build as an optional Code Mower reviewer lane.

### Added

- `code-mower grok-build` runs Grok Build as an informational/manual PR audit
  lane with structured verdict parsing and provider provenance capture.
- `grok_build` provider templates, lane labels, doctor provenance checks, and
  calibration planning support are now included in the packaged OSS defaults.
- Provider setup docs now cover Grok Build local OAuth, xAI key alternatives,
  model provenance env vars, and the trusted-local `GROK_BUILD_USE_AMBIENT_HOME`
  opt-in.

## v0.5.0-beta.32

This beta adds full issue-to-delivery lineage capture for the planning workflow.

### Added

- `code-mower work-order attach-delivery` updates a work-order cloud-event
  sidecar with source-free PR, reviewer-check, and merge metadata.
- `--from-github` can read PR URL/state/merge evidence and reviewer-like check
  names/statuses through `gh pr view`, while filtering ordinary CI checks.
- Planning, quickstart, and cloud-data-contract docs now show the full
  `issue -> plan -> work order -> PR -> reviewer checks -> merge` path.

## v0.5.0-beta.4

This beta fixes an installed-package Codex audit regression found while
dogfooding `v0.5.0-beta.3` against CodeMower.com.

### Changed

- Codex structured-output transport steps now pass `--skip-git-repo-check`
  so installed `code-mower codex-audit` and `code-mower
  codex-audit-schema-smoke` can run schema conversion from package context
  while the actual PR review remains anchored in the target repository.
- Codex CLI preflight now checks for the `--skip-git-repo-check` capability
  explicitly, producing a direct setup error instead of a later UNKNOWN audit
  verdict when the CLI is too old.

## v0.5.0-beta.3

This beta adds a small but important reliability helper for Claude Code CLI
auth drift during local dogfood and early-adopter setup.

### Changed

- Added `code-mower claude-bounce`, a guided helper that runs a real Claude
  smoke prompt, clears cached Claude sessions, opens the local Claude config
  folder, and gives explicit next steps when `claude auth status` says logged
  in but actual prompt requests return `401` or other credential failures.
- Claude audit preflight now shares the same environment/auth detection helper
  used by doctor and bounce diagnostics, reducing divergent remediation advice.
- Provider setup docs and troubleshooting now point users to the prompt-level
  Claude smoke test instead of trusting auth-status output alone.

## v0.5.0-beta.2

This beta tightens the first-user rehearsal path after a real external-repo
install exposed a Python command-resolution sharp edge.

### Changed

- `migration package-install-rehearsal --python python3.12` now resolves
  command-style Python names through `PATH` instead of treating them as repo
  relative paths.
- Rehearsed `v0.5.0-beta.1` against `DrinkBetter-AI/mobile-app`: easy init,
  doctor, native check detection, lint, typecheck, tests, and package-install
  readiness all passed when run with Python 3.12.
- Kept the documented public install target and package metadata aligned on
  `v0.5.0-beta.2` / `0.5.0b2`.

## v0.5.0-beta.1

This beta is the public package marker after alpha.79 clean-install rehearsal,
CodeMower.com dogfood/backfill verification, and dashboard data-coverage
clarity improvements.

### Changed

- Promoted the documented public install target from `v0.5.0-alpha.79` to
  `v0.5.0-beta.1`.
- Updated release-readiness output to report `release_tag` while retaining the
  legacy `alpha_tag` field for compatibility.
- Kept public docs, first-user rehearsal commands, package-index rehearsal
  commands, and package manifests aligned on `0.5.0b1`.
- Verified a non-editable local package-install rehearsal with a passing
  first-user readiness scorecard before the beta cut.

## v0.5.0-alpha.63

This alpha is the public package marker after sharing GitHub PR changed-file
fetching across provider runners.

### Changed

- Added `provider_runners.fetch_pull_request_files` as a shared paginated
  helper for GitHub pull-request file metadata.
- Local LLM audit now delegates PR metadata and changed-file fetching to shared
  provider-runner helpers while keeping raw file-content fetches and comment
  posting local to the lane.
- Added focused tests for shared file pagination, invalid GitHub file payloads,
  and local LLM compatibility wrapper behavior.
- Public install and release-readiness docs now point to `v0.5.0-alpha.63`.

## v0.5.0-alpha.62

This alpha is the public package marker after sharing GitHub PR diff fetching
across provider runners.

### Changed

- Added `provider_runners.fetch_pull_request_diff` so provider lanes can reuse
  the same GitHub PR diff request behavior.
- Gemini CLI now delegates PR metadata and diff fetching to shared
  provider-runner helpers while keeping its compatibility wrapper API.
- Hermes continues to call the Gemini compatibility wrappers, so existing
  Hermes calibration paths inherit the shared diff helper without a public
  interface change.
- Added focused tests for JSON PR metadata fetches and `diff` Accept-header
  behavior.
- Public install and release-readiness docs now point to `v0.5.0-alpha.62`.

## v0.5.0-alpha.61

This alpha is the public package marker after moving more provider-runner
plumbing out of CodeRabbit-specific code.

### Changed

- Added a shared `provider_runners.workspace` helper for local checkout status
  and head validation.
- CodeRabbit CLI now uses the shared workspace helper while keeping its
  provider-specific workspace error type.
- CodeRabbit CLI pull-request metadata reads now delegate to the shared
  provider-runner GitHub helper instead of carrying a duplicate REST client.
- Added focused tests for the new workspace helper and CodeRabbit GitHub
  metadata compatibility wrapper.
- Public install and release-readiness docs now point to `v0.5.0-alpha.61`.

## v0.5.0-alpha.60

This alpha is the public package marker after test-enforcing the doctor package
boundary.

### Changed

- Added doctor boundary tests that keep `doctor.py` as a thin CLI and
  compatibility adapter.
- The guard rejects direct imports of doctor check implementation modules such
  as runtime, GitHub, provider, Actions-cost, output, privacy, and cloud
  submodules.
- The guard covers package-relative, fully qualified, and legacy
  `tools.doctor_checks.*` import styles.
- Updated the code-structure roadmap to document the test-enforced doctor
  boundary.
- Public install and release-readiness docs now point to `v0.5.0-alpha.60`.

## v0.5.0-alpha.59

This alpha is the public package marker after adding a named doctor run-plan
registry.

### Changed

- Doctor now builds an explicit run plan from base stages plus optional GitHub
  and Code Mower Cloud stages.
- Doctor JSON/text output includes a token-safe `doctor.plan` setup check so
  first-time users and support logs show which diagnostic stages ran.
- `doctor.plan` is grouped with setup output instead of falling into a generic
  catch-all section.
- Added focused coverage for the doctor registry vocabulary, optional stage
  selection, and runner-level plan emission.
- Public install and release-readiness docs now point to
  `v0.5.0-alpha.59`.

## v0.5.0-alpha.58

This alpha is the public package marker after improving first-run doctor output
readability.

### Changed

- Human-readable `code-mower doctor` output now groups checks into setup,
  runtime, provider lane, GitHub, Code Mower Cloud, output, and other sections.
- Doctor output keeps existing check names, lane markers, messages, and
  remediation text intact while making first-run failures easier to scan.
- Sample doctor documentation now matches the grouped terminal output shape.
- Added focused coverage for doctor output grouping, empty-report rendering, and
  provider-lane grouping.
- Public install and release-readiness docs now point to
  `v0.5.0-alpha.58`.

## v0.5.0-alpha.57

This alpha is the public package marker after extracting CodeMower.com bundle
report payload helpers.

### Changed

- Cloud upload report payload construction now lives in
  `code_mower.cloud_client.reports`.
- Cloud upload orchestration keeps the existing public API while importing
  report payload helpers from the narrower cloud report boundary.
- Package manifests now include the cloud report payload helper module.
- Added focused coverage for metadata-only report summaries, embedded report
  text, invalid manifest report entries, non-UTF-8 report files, and report
  upload size guards.
- Public install and release-readiness docs now point to
  `v0.5.0-alpha.57`.

## v0.5.0-alpha.56

This alpha is the public package marker after extracting CodeMower.com bundle
manifest helpers.

### Changed

- Cloud bundle manifest loading and report target validation now live in
  `code_mower.cloud_client.manifest`.
- Cloud upload payload construction keeps the existing public API while
  importing manifest helpers from the narrower cloud manifest boundary.
- Package manifests now include the cloud manifest helper module.
- Added focused coverage for valid bundle manifests, unsupported schemas,
  unsafe report targets, and missing report files.
- Public install and release-readiness docs now point to
  `v0.5.0-alpha.57`.

## v0.5.0-alpha.55

This alpha is the public package marker after extracting CodeMower.com Git
metadata helpers.

### Changed

- Cloud Git remote metadata helpers now live in
  `code_mower.cloud_client.git_metadata`.
- Cloud event construction keeps building dogfood events while importing the
  Git helper from the narrower cloud metadata boundary.
- Package manifests now include the cloud Git metadata helper.
- Added focused unit coverage for GitHub remote slug parsing, non-repository
  fallback behavior, and best-effort Git command output.
- Public install and release-readiness docs now point to
  `v0.5.0-alpha.55`.

## v0.5.0-alpha.54

This alpha is the public package marker after extracting shared provider
process environment helpers.

### Changed

- Shared provider child-process environment construction now lives in
  `code_mower.provider_runners.process`.
- Gemini CLI, Hermes CLI, and CodeRabbit CLI wrappers use the shared helper for
  allowlisted ambient variables, isolated HOME/XDG paths, and provider-specific
  explicit environment values.
- Package manifests now include the shared provider process helper.
- Added focused unit coverage for ambient env filtering, isolated home handling,
  provider auth-key mapping, Hermes quiet flags, and CodeRabbit env allowlisting.
- Public install and release-readiness docs now point to
  `v0.5.0-alpha.54`.

## v0.5.0-alpha.53

This alpha is the public package marker after extracting shared provider git
helpers.

### Changed

- Shared provider-runner git helpers now live in
  `code_mower.provider_runners.git`.
- Gemini CLI and CodeRabbit CLI audit wrappers use the shared helper for local
  git command execution and HEAD lookup instead of keeping duplicate
  implementations.
- Package manifests now include the shared provider git helper.
- Added focused unit coverage for provider-runner git helper behavior.
- Public install and release-readiness docs now point to
  `v0.5.0-alpha.53`.

## v0.5.0-alpha.52

This alpha is the public package marker after extracting the doctor privacy
redaction helper.

### Changed

- Auth-probe output shape reporting now lives in
  `code_mower.doctor_checks.privacy`.
- Runtime and doctor compatibility exports still expose the redaction helper,
  while provider and GitHub doctor checks import it from the privacy boundary.
- Added focused `unittest` coverage that verifies auth probe details never
  preserve raw output content.
- Public install and release-readiness docs now point to
  `v0.5.0-alpha.52`.

## v0.5.0-alpha.51

This alpha is the public package marker after extracting shared doctor provider
auth smoke-probe helpers.

### Changed

- Provider auth smoke-probe JSON parsing and remediation now live in
  `code_mower.doctor_checks.provider_probe`.
- Local CLI doctor checks and provider registry exports share the same probe
  helper implementation.
- Added focused `unittest` coverage for noisy JSON extraction and auth-detail
  redaction.
- Public install and release-readiness docs now point to
  `v0.5.0-alpha.51`.

## v0.5.0-alpha.50

This alpha is the public package marker after extracting shared GitHub comment
formatting from the provider wrappers.

### Changed

- Shared audit comment truncation now lives in
  `code_mower.provider_runners.comments`.
- Codex and Claude audit wrappers both use the provider-runner comment limiter,
  preserving trailer state while avoiding duplicated GitHub comment-size logic.
- Added focused `unittest` coverage for comment truncation edge cases.
- Public install and release-readiness docs now point to
  `v0.5.0-alpha.50`.

## v0.5.0-alpha.49

This alpha is the public package marker after extracting shared repo-path
parsing from the provider wrappers.

### Changed

- Shared local repository path parsing now lives in
  `code_mower.provider_runners.repo_paths`.
- Codex and Claude audit wrappers both use the provider-runner parser, removing
  the remaining generic Claude dependency on Codex wrapper internals.
- Added focused `unittest` coverage for repo-path parsing so the package CI path
  does not depend on pytest.
- Public install and release-readiness docs now point to
  `v0.5.0-alpha.49`.

## v0.5.0-alpha.48

This alpha is the public package marker after the provider text/schema helper
extraction.

### Changed

- Shared provider text clipping, one-line sanitizing, and strict key-validation
  helpers now live in `code_mower.provider_runners.text_schema`.
- Codex and Claude audit wrappers both import those helpers from the
  provider-runner layer, removing another Claude dependency on Codex internals.
- Added focused unit coverage for the provider text/schema helper contract.
- Public install and release-readiness docs now point to
  `v0.5.0-alpha.48`.

## v0.5.0-alpha.47

This alpha is the public package marker after the first provider-runner seam
extractions.

### Changed

- Shared GitHub PR metadata and PR comment helpers now live in
  `code_mower.provider_runners.github_pr` instead of the Codex provider.
- Shared audit verdict artifact write/load/repost helpers now live in
  `code_mower.provider_runners.verdict_artifacts` instead of the Codex
  provider.
- Codex and Claude audit wrappers now import those provider-neutral helpers
  from `provider_runners`, reducing cross-provider coupling.
- Public install and release-readiness docs now point to
  `v0.5.0-alpha.47`.

## v0.5.0-alpha.46

This alpha is the next public package marker after the migration structure
hardening slices.

### Changed

- Package-install rehearsal internals now split install, venv, pip,
  command-runner, and toy-repo helpers into `code_mower.migration_install`.
- `code_mower.migration_rehearsal` now focuses on the fresh-repo rehearsal flow
  while preserving the previous compatibility import surface.
- Public install and release-readiness docs now point to
  `v0.5.0-alpha.46`.

## v0.5.0-alpha.45

This alpha continues the public-package structure hardening path after the
doctor and migration refactors.

### Changed

- GitHub doctor internals now split redacted `gh api` helpers from Actions
  billing/cost diagnostics, leaving repository setup orchestration in a smaller
  `github.py` module.
- Package-install rehearsals now keep first-user readiness artifacts and
  scorecards in `code_mower.migration_readiness`, while
  `code_mower.migration_rehearsal` stays focused on fresh-repo rehearsal
  execution.
- Public install and release-readiness docs now point to
  `v0.5.0-alpha.45`.

## v0.5.0-alpha.44

This alpha consolidates the recent structural cleanup into a cleaner public
package baseline for first-user rehearsals.

### Changed

- `code_mower.cloud` is now backed by `code_mower.cloud_client.operations` for
  dogfood upload, repo-sync, and reviewer-run orchestration.
- `code_mower.doctor` is now a smaller CLI adapter backed by split doctor
  modules for output rendering, first-run presets, package-aware template
  paths, and report orchestration.
- Package template/config path helpers moved into `code_mower.package_paths`,
  reducing package-materialization coupling while preserving the public command
  surface.
- Public install and release-readiness docs now point to
  `v0.5.0-alpha.44`.

## v0.5.0-alpha.21

This alpha makes package-index promotion setup more self-service.

### Changed

- `code-mower migration release-readiness` now reports setup URLs for the
  GitHub environments, release workflow, TestPyPI project, PyPI project, and
  trusted-publishing configuration pages.
- Release-readiness next actions now include relevant URLs in both JSON and text
  output.
- The PyPI release runbook documents those setup URLs directly.
- Public install docs now point to `v0.5.0-alpha.21`.

## v0.5.0-alpha.20

This alpha makes the full package-installed first-user rehearsal part of routine
public CI.

### Changed

- The main Code Mower CI job now runs
  `python -m code_mower.migration package-install-rehearsal` from the current
  checkout, proving the installed-package path in a fresh toy repository.
- `code-mower migration release-readiness --json` now fails if the CI
  package-install rehearsal gate is removed or weakened.
- Public release/readiness docs now treat the installed-package rehearsal as a
  routine PR gate, not only a manual pre-release habit.
- Public install docs now point to `v0.5.0-alpha.20`.

## v0.5.0-alpha.19

This alpha adds public maintainer hygiene for early adopters and makes release
readiness enforce the public support, security, and community-safety surface.

### Changed

- Added `SUPPORT.md` and `CODE_OF_CONDUCT.md`, with explicit guidance to avoid
  sharing tokens, private source, raw diffs, raw model transcripts, auth output,
  credentials, and customer data in public support channels.
- The README docs map now links support, security policy, and conduct docs.
- The source distribution now includes the support and conduct docs.
- `code-mower migration release-readiness --json` now verifies public
  maintainer docs are present, linked, and privacy-forward.
- Release hygiene tests now cover missing-doc, missing-link, and incomplete
  redaction-guidance failure paths.
- Public install docs now point to `v0.5.0-alpha.19`.

## v0.5.0-alpha.18

This alpha makes the package release-readiness check part of routine CI, so
future release candidates cannot drift from the package-index promotion gate.

### Changed

- The main Code Mower CI job now runs
  `python -m code_mower.migration release-readiness --json`.
- Release hygiene tests assert that the CI workflow keeps the release-readiness
  gate wired.
- Public install docs now point to `v0.5.0-alpha.18`.

## v0.5.0-alpha.17

This alpha adds a static release-readiness gate for package-index promotion,
so maintainers can verify the GitHub Release, TestPyPI, and PyPI plumbing
before cutting wider early-adopter releases.

### Changed

- `code-mower migration release-readiness --json` now checks package version
  consistency, release workflow gates, TestPyPI/PyPI publishing posture, and
  package-index rehearsal docs.
- The release-readiness gate verifies that the TestPyPI and PyPI jobs use the
  official `pypa/gh-action-pypi-publish` action, not just matching job names.
- Release hygiene tests now cover alpha, beta, release-candidate, and final
  tag derivation.
- Public install docs now point to `v0.5.0-alpha.17`.

## v0.5.0-alpha.16

This alpha adds the first package-index rehearsal lane for TestPyPI while
keeping production PyPI disabled by default.

### Changed

- The release workflow now has a dedicated `publish_testpypi` dispatch input
  and `publish-testpypi` job.
- TestPyPI publishing is gated by the separate `testpypi` GitHub environment
  and `CODE_MOWER_TESTPYPI_PUBLISH` repository variable.
- Production PyPI publishing remains gated by the separate `pypi` environment
  and `CODE_MOWER_PYPI_PUBLISH` variable.
- The PyPI release runbook now includes a workflow dispatch matrix for
  build-only, TestPyPI, and production PyPI release rehearsals.
- Public install docs now point to `v0.5.0-alpha.16`.

## v0.5.0-alpha.15

This alpha fixes the first-run package rehearsal command surfaces for current
GitHub-tag alpha installs.

### Changed

- `code-mower --help` now recommends the current GitHub alpha tag for
  `migration package-install-rehearsal`.
- `code-mower next-steps --profile recommended` derives the current GitHub
  alpha package spec from the installed package version.
- Alpha-facing docs and generated command catalogs no longer recommend the
  unavailable PyPI package placeholder before PyPI publishing is promoted.
- Public install docs now point to `v0.5.0-alpha.15`.

## v0.5.0-alpha.14

This alpha hardens the public release pipeline after the artifact-action
maintenance updates.

### Changed

- The GitHub Release workflow now downloads the built distribution artifact and
  runs `twine check dist/*` before optional PyPI publishing can start.
- The release workflow's artifact download path is exercised even when PyPI
  publishing is skipped.
- The PyPI release runbook documents the artifact verification job as a release
  gate.
- Public install docs now point to `v0.5.0-alpha.14`.

## v0.5.0-alpha.13

This alpha fixes the public repo's Dependabot Dependency Graph compatibility
after the pytest 9.1.0 maintenance update exposed that the standalone repo used
a nonstandard pip requirements filename.

### Changed

- The standalone repo now uses `requirements/requirements.txt` as its pip
  tooling requirements file, matching Dependabot's supported manifest naming.
- `code-mower bootstrap` defaults to `requirements/requirements.txt` instead
  of the extraction-era `tools/code_mower_requirements.txt` path.
- Package extraction metadata now renders the requirements file to
  `requirements/requirements.txt`.
- Public install docs now point to `v0.5.0-alpha.13`.

## v0.5.0-alpha.12

This alpha makes the first-user readiness scorecard more discoverable from the
default CLI and `next-steps` guidance.

### Changed

- Top-level `code-mower --help` now includes the package-install rehearsal in
  the common first-run path.
- `code-mower next-steps --profile recommended` now points to the
  `first_user_readiness` rehearsal result and lists the readiness scorecard
  artifact path.
- Public install docs now point to `v0.5.0-alpha.12`.

## v0.5.0-alpha.11

This alpha adds a first-user readiness scorecard to the package-install
rehearsal. The rehearsal already proved install, easy-mode setup, doctor, first
reports, and cloud dry-run behavior; the scorecard now turns that evidence into
an explicit release-gate summary.

### Added

- `migration package-install-rehearsal` now includes `first_user_readiness` in
  its JSON payload and writes `outputs/first-user-readiness.json`.
- The readiness scorecard verifies package install, easy-mode generated output,
  doctor completion, draft corpus/report generation, starter value report
  generation, cloud export, cloud upload dry-run privacy, and dogfood dry-run
  privacy.

### Changed

- Public install docs now point to `v0.5.0-alpha.11`.
- First-user rehearsal docs now list `first_user_readiness.status == pass` as a
  release-gate criterion.

## v0.5.0-alpha.10

This alpha fixes the first-user cloud dogfood preview path after alpha.9 exposed
that a production dry run still failed without a token. Dry runs now remain
network-safe and token-optional, while confirmed uploads still require an
explicit token.

### Changed

- `code-mower cloud dogfood --json` no longer fails when targeting
  `https://codemower.com/api/ingest` without `CODE_MOWER_CLOUD_TOKEN`, as long
  as `--yes` is not supplied.
- Cloud doctor can distinguish upload-readiness checks from dry-run previews, so
  missing tokens are warnings for previews and failures for confirmed uploads.
- Public install docs now point to `v0.5.0-alpha.10`.

### Fixed

- First-user dogfood dry runs now match the documented privacy posture: no token
  is required to inspect the metadata-only bundle, and no network upload occurs
  without `--yes`.

## v0.5.0-alpha.9

This alpha hardens the first-useful-report path. The package-install rehearsal
now proves that a fresh install can bootstrap a draft project corpus from PR
metadata and generate a draft reviewer value report before any paid or
networked lane is enabled.

### Added

- `code-mower next-steps` now recommends `calibration auto-discover` between
  first calibration runs and the first reviewer value report.
- Package-install rehearsal now writes an offline PR-list fixture, runs
  `calibration auto-discover`, and round-trips the generated draft corpus
  through `calibration value-report`.
- Alpha.9 first-user install rehearsal transcript covering the draft corpus and
  draft value-report artifacts.

### Changed

- Public install docs now point to `v0.5.0-alpha.9`.
- Public release docs now treat calibration auto-discovery as part of the
  release gate, while still requiring human disposition review before lane
  promotion.

## v0.5.0-alpha.8

This alpha hardens the first-user trust path: clearer package-index release
steps, tested cloud dogfood defaults, and a recorded fresh-install rehearsal.

### Added

- `cloud_client.dogfood` helpers for routine dogfood report discovery and
  dry-run preview shape.
- Public CLI contract tests for `code-mower cloud dogfood` dry-run default
  behavior and `code-mower cloud setup` token redaction.
- PyPI/TestPyPI release runbook for moving from GitHub-tag installs to package
  index installs once trusted publishing is configured.
- Alpha.8 first-user install rehearsal transcript.

### Changed

- Public install docs now point to `v0.5.0-alpha.8`.
- Cloud dogfood docs more clearly distinguish preview-by-default from
  confirmed upload with `--yes`.
- Packaging metadata now uses the modern SPDX license string form, removing a
  setuptools release-build deprecation warning.

## v0.5.0-alpha.7

This alpha tightens the early-adopter handoff from local reports into the
optional CodeMower.com dogfood loop.

### Added

- `code-mower next-steps` now includes the routine `cloud dogfood` dry-run and
  confirmed-upload commands after the lower-level bundle upload preview.
- Easy-mode smoke rehearsal coverage for `code-mower cloud dogfood`,
  so release checks exercise the dashboard-oriented metadata path.

### Changed

- Public install docs now point to `v0.5.0-alpha.7`.
- Cloud sharing docs more clearly separate one-off bundle upload from routine
  dogfood metadata uploads.

## v0.5.0-alpha.5

This alpha sharpens the first-user preflight path based on fresh-eyes feedback
from the public repo and CodeMower.com onboarding flow.

### Added

- `code-mower doctor --preflight` as a friendlier alias for the v0.5
  early-adopter doctor preset.
- Release-hygiene tests proving preflight defaults and tokenless cloud
  upload dry-run behavior.

### Changed

- First-user docs now lead with `doctor --preflight` while keeping
  `doctor --v05` as the versioned scripting alias.

## v0.5.0-alpha.4

This alpha adds the public release plumbing needed before inviting a wider
friendly-user cohort.

### Added

- GitHub release workflow that builds source/wheel distributions and can publish
  to PyPI after trusted publishing is configured.
- Reviewer-metrics core tests covering spend, latency, event-log aggregation,
  and unsupported calibration report modes.
- Alpha.4 first-run rehearsal transcript for the public org repository.

### Changed

- Version and public install docs now point to `v0.5.0-alpha.4`.
- Public release checklist now treats PyPI packaging as present but gated until
  repository publishing credentials are configured.

### Known Limitations

- GitHub tag install remains the primary early-adopter install path until PyPI
  trusted publishing is enabled for `codemower-ai/code-mower`.

## v0.5.0-alpha.3

This is the first public alpha intended to be shared from the
`codemower-ai/code-mower` organization.

### Added

- Public-first README copy with concrete `doctor --v05` output.
- A sample doctor transcript in `docs/sample-doctor-output.md`.
- A 10-minute first-run path that starts with GitHub install, `init --easy`,
  `doctor --v05`, and a starter value report.
- Optional CodeMower.com cloud-sharing docs with metadata-only privacy
  boundaries.
- Conservative Ruff linting in CI (`E`/`F`, with line length formatting left
  alone for now).
- Fresh-clone and easy-mode smoke rehearsals for release validation.

### Changed

- Public documentation now points at
  `https://github.com/codemower-ai/code-mower` instead of the earlier personal
  repository.
- Cloud sharing is framed as optional: local audits, local value reports, and
  dry-run upload checks do not require a CodeMower.com account.
- Provider guidance is conservative by default. Codex and Claude are the first
  structured local audit lanes; other providers start informational or manual
  until calibration evidence supports promotion.

### Known Limitations

- PyPI publishing is not live yet. Install from the tagged GitHub repository.
- Code Mower is GitHub-first; GitLab, Bitbucket, and ACP bridges are roadmap
  items.
- Some provider integrations are calibration/manual lanes, not production merge
  gates.
- The hosted benchmark dashboard is early and metadata-first. Cohort
  benchmarking becomes more valuable as more teams opt in.
- Large extraction-era modules remain readable but should be decomposed before
  v1.0 where that improves contributor onboarding.
