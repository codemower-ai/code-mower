# Code Mower OSS v1.0 Checklist

v1.0 should make Code Mower valuable in "easy mode" before asking users to
understand every lane, provider, or calibration option.

## v1.0 Promise

A developer can install Code Mower, point it at a GitHub repository, run a
safe setup plan, verify provider readiness, run the first audits, and generate
a local value report that explains reviewer quality, speed, and cost.

The v1.0 promise is "easy mode with a path to power," not "every provider on
day one." Newer lanes such as Antigravity CLI, Hermes CLI, Grok Build, Jules,
CodeRabbit CLI, local models, and protocol bridges should be discoverable and
calibratable, but they should start informational unless the repository
explicitly promotes them.

## Fresh-Eyes Acceptance Criteria

Before calling Code Mower 1.0, assume an experienced engineer with no project
history opens the repository. They should be able to confirm:

- the README explains the product in one screen and shows the golden path;
- `code-mower --help` shows only the first-user command surface, with
  `code-mower --help-all` for advanced/provider/operator commands;
- `init --easy` and `doctor --preflight` are safe to run before any workflow
  mutation;
- local value reports work without CodeMower.com;
- a new user can bootstrap a draft calibration corpus from recent repository
  history instead of hand-authoring every case before seeing value;
- optional cloud sharing is inspectable, dry-run-first, and metadata-only by
  default;
- provider lanes start manual or informational unless calibrated;
- docs say exactly which setup is OSS-user setup versus CodeMower.com operator
  setup;
- public docs do not depend on private reference repos or personal machine
  context; and
- the code structure has clear package seams for calibration, doctor, provider
  runners, and cloud clients.

## Current Beta Baseline

The current public-release baseline is `v0.5.0-beta.23` of the standalone
package. It has proved:

- non-editable package-install rehearsal in a clean venv;
- fresh toy-repo easy-mode rehearsal from the installed package;
- public package installation from PyPI as `code-mower==0.5.0b23`;
- public-tag/source install validation as a fallback path;
- production dogfood uploads from Code Mower OSS, CodeMower.com, and two
  private reference/product repos, with beta.23 preserving the same client path
  and adding clearer catch-up/stale-audit trust diagnostics;
- production catch-up upload across those four dogfood repos using
  `repo-sync --mode catch-up`, with imported workflow history separated from
  reviewer/lens calibration evidence;
- beta.23 local dogfood and catch-up uploads from a stored dashboard-issued
  token, proving local Codex sessions can contribute current metadata and
  imported history without exposing source, raw diffs, raw transcripts, or
  secrets;
- beta.23 private-repo package-install rehearsal against
  [DrinkBetter-AI/mobile-app](https://github.com/DrinkBetter-AI/mobile-app),
  including repository-native check detection and dry-run execution;
- beta.23 calibration/value-report generation from the installed package plus
  sanitized report upload coverage for CodeMower.com;
- metadata-only AI tool/model provenance in cloud bundles and dogfood events,
  with CodeMower.com distinguishing known provider/model/version signal from
  missing provenance before making benchmark recommendations;
- CodeMower.com evidence/detail URLs for uploads and events, so dashboard rows
  can be inspected and exported without exposing source, raw diffs, transcripts,
  auth output, or secrets;
- explicit catch-up provenance, so imported GitHub Actions history is useful
  for activity/backfill without being confused for calibrated reviewer-quality
  evidence;
- `code-mower init --easy` smoke behavior;
- `doctor --v05` provider probes for configured local CLIs, GitHub setup,
  Actions cost traps, and optional cloud-token setup;
- product-wrapper rehearsal with zero mismatches against the repo-local mirror;
- pinned standalone consumption from both private reference/product repos;
- private-repo standalone checkout shape through a read-only deploy-key shadow
  workflow;
- public-source standalone checkout from the public GitHub repository;
- calibration context-pack injection for Gemini, Antigravity, and Hermes CLI
  fan-out commands, including a dry-run proof on a known-problematic
  solver-runtime lens case;
- standalone labeler and bootstrap entrypoints for migrating product workflows
  away from mirrored Python files;
- explicit mirror-removal completion status when a product repo has already
  deleted mirrored implementation files;
- mirror-removal pilots in both private reference/product repos: wrappers
  prefer the pinned standalone package, workflows use package-backed entrypoints,
  mirrored implementation files are absent, and post-merge CI/deploy checks
  remain green;
- first-run doctor visibility for missing `pytest`, which is not required by
  standalone easy-mode but is commonly required by product-side wrapper tests;
- private-source package-install rehearsal that preserves the deploy-key path by
  separating the checkout URL from the pip-installable package URL and reading
  only the pinned ref from product support files;
- standalone Codex and Claude structured audit commands plus Codex env
  preflight/schema-smoke helpers, so product shell wrappers can remain thin
  compatibility shims without mirrored Python implementation files;
- packaged starter value-report fixture generation, so the public starter
  corpus has an expected first-run report that can be compared in tests and
  shipped through `init --easy --apply`.
- generated product-support wrappers for the standalone package launcher,
  standalone checkout/pin files, Codex/Claude compatibility shims, and
  shell-safe GitHub commenting, so future product repos can review generated
  support files instead of hand-copying them from private reference repos.
- generated product-support wrappers promoted from the private reference repos
  back into the package, including standalone checkout/install locks,
  Python-version selection, deleted-mirror error messages, portable `stat`
  handling, and shell-safe GitHub comment helpers.
- bounded Claude doctor probes that pin a cheap model/budget sentinel instead
  of relying on the local Claude CLI's default model selection.
- private reference-repo generated-support pilot feedback from alpha.18,
  including non-fatal
  missing absolute Python candidates and hash-suffixed ref-scoped default
  standalone checkout/venv directories so concurrent invocations do not mutate
  one another's editable source checkout or console-script install.
- private reference-repo concurrent-audit feedback from alpha.19, including
  Claude diff construction that no longer depends on shared `FETCH_HEAD` state
  after fetching the PR head.
- private reference-repo editable-install feedback from alpha.20, including a
  checkout lock that stays held through delegated standalone execution so a
  shared editable source checkout cannot mutate under an active command.
- optional cloud-upload dry run and explicit `--yes` upload path for sanitized
  benchmark bundles, with metadata-only payloads by default.
- dashboard trust depends on stable row-level evidence links, provenance labels,
  and a clear distinction between current dogfood metadata, imported history,
  and calibrated reviewer/lens evidence.
- `code-mower doctor --v05` as the early-adopter preset for easy profile,
  runtime probes, GitHub setup, private-repo caveats, Actions cost diagnostics,
  and optional cloud-token setup.
- repository-native check detection and execution, so TypeScript/React repos use
  their declared ESLint/Vitest/build surface, Python repos can use Ruff/pytest
  where configured, and Code Mower does not impose its own lint policy on
  product repos.
- reusable stale-audit lane handling for merge-authority lanes through
  `clear-stale` and generated stale-clear workflow templates.

It has not yet proved:

- enough first-user polish that a new user can install, run doctor, perform a
  first audit/report, and understand optional cloud sharing without project
  history;
- enough repeated friendly-user proof that the current first-user path is
  boring across multiple private repos and machines;
- historical catch-up UX that is obvious on the dashboard, not just documented:
  GitHub Actions history, reviewer-run artifacts, and routine dogfood/current
  metadata must remain visibly distinct;
- dashboard trust/value for first-time uploaders beyond receipt rows, including
  sharper provenance labels, benchmark trust scoring, and useful "what should I
  enable next?" guidance;
- enough provider/lens provenance completeness to answer "which AI coding tool,
  model, version, and prompt lens produced this signal?" across dogfood and
  calibration runs instead of only at the Code Mower client level;
- calibration auto-discovery from recent PRs with human disposition review and
  enough evidence to justify lane promotion;
- broad private-repo standalone checkout across arbitrary organizations and
  token policies;
- mirror deletion across arbitrary user repositories;
- broader spend-bearing context-pack lens runs beyond the first solver-runtime
  Gemini proof;
- a large enough reviewer/lens corpus for new merge gates; and
- Dashboard IA phase 1 on CodeMower.com: authenticated tabs, freshness and
  provenance strip, clearer first-upload guidance, and a visibly useful
  "what should I enable next?" panel.

## Easy Mode Flow

```bash
pipx install --python python3.12 code-mower==0.5.0b23
code-mower init --easy
code-mower init --easy --apply --output-dir .code-mower.generated
code-mower doctor --v05
code-mower --help
code-mower --help-all
code-mower next-steps --profile recommended
code-mower migration wrapper-rehearsal --repo-path /path/to/product-repo --json
code-mower migration package-install-rehearsal \
  --package-spec code-mower==0.5.0b23 \
  --repo-path /path/to/repo \
  --json
code-mower audit pr 123
code-mower calibration value-report templates/calibration-corpus.json
python scripts/smoke_easy_mode.py --json
```

Use `migration wrapper-rehearsal` for existing product repos that still carry
repo-local Code Mower wrappers. Use `migration package-install-rehearsal
--repo-path /path/to/repo` for either wrapper-bearing product repos or fresh
external repos; fresh repos get installed-CLI readiness checks instead of
wrapper parity.

The bundled starter corpus is for proving the first report path. It should not
be confused with Code Mower's richer reference corpus or a user's
product-specific benchmark corpus.
`reviewer-value-report.example.md` is the expected report for that starter
corpus before users add real reviewer runs and human dispositions.

The v1 target should add a draft auto-discovery path, for example
`code-mower calibration auto-discover --repo owner/repo --last-n 20`, that scans
recent PR history and proposes known-clean, known-blocked, and review-signal
cases. That output must be explicitly reviewable and never silently promoted to
merge-gating evidence.

`init --easy` is a safe alias for the recommended profile. It should render a
dry-run by default. `--apply` writes generated output to a reviewable directory
and still must not mutate live workflows, create labels, or trigger paid lanes.

## Release Plan

v1.0 should ship in six ordered slices:

1. **Install and easy-mode setup.** Package the CLI, render a starter config,
   and make `doctor --easy` produce actionable remediation without mutating a
   repository.
2. **GitHub readiness.** Document and check public/private repo behavior,
   workflow-token limits, branch protection, provider app access, and fork PR
   safety. GitHub is the only supported SCM for v1.0.
3. **First audit and value report.** Run the default reviewer set on one PR,
   persist local results, and generate a reviewer value report from a starter
   corpus.
4. **Calibration bootstrap.** Build a draft corpus from recent PR history so a
   first-time user can see a project-specific report before investing hours in
   manual corpus curation. Keep human disposition review explicit before any
   lane is promoted.
5. **Cloud-ready export.** Produce a sanitized local benchmark bundle that can
   feed an opt-in cloud service, with current dogfood metadata, historical
   catch-up, reviewer-run artifacts, and calibration evidence labeled as
   separate provenance categories.
6. **Measured lane promotion.** Generate a value report from known-clean and
   known-blocked PRs, then classify lanes as informational, selective, or
   merge-gating eligible.
7. **First builder-experiment scaffold.** Keep this harness-only for v1.0:
   record authoring run metadata and reports, but do not require autonomous
   orchestration or hosted source access.
8. **Reusable merge-authority lane hygiene.** Ship stale-audit label clearing,
   current-head comment validation, trusted-bot author controls, and direct
   redispatch behavior as reusable templates/commands instead of product-repo
   glue.

GitLab and Bitbucket stay post-v1.0. Keep schemas source-control-neutral, but
do not spend v1.0 work on non-GitHub workflow rendering.

## Required v1.0 Actions

### Package

- Publish a normal Python package with `pyproject.toml`.
- Support `pipx install code-mower` and `uv tool install code-mower`.
- Expose the `code-mower` console script.
- Keep Python >=3.11 explicit.
- Create, repair, and report one blessed Code Mower virtualenv or packaged
  runtime path.
- Prove commands do not accidentally inherit unsupported ambient Python after
  bootstrap.
- Treat `scripts/dev-python` as the source-checkout and release-rehearsal
  interpreter contract: it must resolve Python 3.12+ and refuse stale virtualenvs
  or old `python3` shims before any release gate runs.
- Include prompt lenses, context-pack example, provider templates, and docs.
- Provide a product-wrapper rehearsal so existing product repos can compare
  repo-local tools with a pinned standalone package before deleting mirrors.
- Provide a package-install rehearsal that installs Code Mower non-editably into
  a clean venv, creates a fresh toy repository, runs the easy-mode starter path,
  and optionally compares an existing product repo against that installed
  package.
- Keep the distribution and checkout story explicit. A public Code Mower repo
  can use unauthenticated HTTPS checkout; a private fork, private source repo,
  or private package index needs a documented deploy key, fine-grained PAT, or
  GitHub App token.
- Keep the read-only deploy-key workflow template for users who pin a private
  fork or private source checkout.
- Remove legacy direct-source import shims from shipped console entrypoints once
  the public PyPI package is the documented happy path. Source checkouts should
  use the checked-in dev/runtime helpers; unsupported direct execution should
  fail loudly with a precise remediation.
- Keep provider-specific audit wrappers as thin adapters over shared,
  fixture-tested provider-runner primitives. The duplicated audit lifecycle
  should not remain spread across every provider wrapper at v1.0.
- Keep the package root intentionally small. Provider wrappers, cloud commands,
  package materialization, and experiment harnesses should live in named
  subpackages unless they are part of the public CLI entry surface.

### Init

- `code-mower init --easy` renders the recommended profile.
- `code-mower init --easy --apply --output-dir .code-mower.generated` writes a
  reviewable generated tree.
- Generated output includes labels, required secrets, workflows, smoke tests,
  starter data, product-support wrappers, and a manifest.
- Live repository mutation remains a later explicit feature.

### Doctor

- Check GitHub CLI, GitHub auth, local provider CLIs, Python, provider catalog,
  required env vars, and optional runtime probes.
- For local CLI lanes, support provider-declared smoke probes that can validate
  a harmless version command or an auth-bearing sentinel prompt.
- Treat real provider prompt smoke as stronger evidence than CLI login/status
  commands. Claude Code is the canonical example: `claude auth status` can say
  logged in while `claude -p "Reply with exactly: ok" --output-format json`
  returns a provider auth error.
- For paid or usage-metered local CLIs, provider-declared smoke probes should
  pin a cheap model and budget cap when the CLI supports it. The default
  Claude probe uses `--model sonnet` and `--max-budget-usd 0.25`.
- Support `code-mower doctor --github` for repository visibility, token
  write-adjacent permission hints, Actions permission inspection, branch
  protection inspection, recent billing blocks, sampled Actions cost hotspots,
  and private-repo/SaaS provider warnings.
- Check the blessed runtime, Python version, and GitHub HTTPS/certificate
  behavior before provider commands run.
- Emit content-free JSON suitable for sharing.
- Redact raw provider-smoke stdout/stderr and expose only shape, status,
  return code, JSON-parse status, expected-sentinel match, and remediation.
- Keep auth-failure status diagnostics provider-configured through
  `doctor_probe_auth_status_fields`; expose only sanitized `401`/`403` codes
  and content-free flags in shareable doctor JSON.
- Provide exact remediation hints for missing provider auth, CLIs, runtime
  probes, provider catalog coverage, config loading, config validation, and
  Python version checks.
- Support `code-mower doctor --easy` as a first-run readiness check that uses
  the packaged recommended starter config when a project config has not been
  created yet.

### Next Steps

- `code-mower next-steps --profile recommended` renders the ordered actions a
  new user should take after `doctor --easy`: write reviewable setup output,
  request a first PR audit, run a starter calibration corpus, generate a value
  report, review lane promotion policy, add context packs, and export a local
  cloud benchmark bundle.
- JSON output must be content-free and safe to share in support/debug reports.

### First Audit

- Support a simple documented path for a single PR.
- Keep new non-core lanes informational by default.
- Preserve head-SHA pinning and structured verdicts.
- For merge-authority lanes, clear stale terminal labels on new commits unless
  the latest trusted reviewer verdict is explicitly tied to the current head
  SHA.
- Keep stale-label requeue and redispatch behavior reusable through generated
  templates or `code-mower clear-stale`, not bespoke product-repo workflows.
- Treat repo-native check surfaces as first-class audit context: Code Mower
  should detect and run the repository's declared checks rather than substitute
  its own Python/Ruff policy for non-Python projects.

### Calibration And Reports

- Ship a starter calibration corpus.
- Persist raw run results locally.
- Generate reviewer metrics and lane promotion recommendations.
- Explain that promotion is advisory until the repo opts into merge authority.
- Keep Antigravity, Hermes, Grok Build, Jules, CodeRabbit CLI, and local-model
  lanes informational until corpus evidence supports selective triggers or
  merge authority.
- Keep Cursor BugBot informational and manual until enabled-output examples are
  captured, parsed, and adjudicated.
- Treat bounded lens-smoke runs as evidence only after the raw artifacts or
  summarized reviewer runs are promoted into the corpus. Temporary `/tmp`
  artifacts are useful debugging output, not durable product evidence.
- Treat unavailable-provider bypasses as operational incidents, not reviewer
  approvals. They require an explicit PR note and must not count as PASS
  evidence in calibration metrics.

### Authoring Intelligence

- Define `authoring_runs.jsonl`.
- Render a first delivery report from manually supplied authoring-run entries.
- Connect authoring runs to reviewer outcomes in value reports.
- Normalize `run_role`/`purpose`, task contract identity, provider/tool/model,
  lens, context pack, worktree/branch, PR, elapsed time, intervention count,
  blocker iterations, checks, merge result, post-merge health, and known cost.
- Keep builder-experiment results source-free by default so they can feed local
  reports and optional cloud events without sharing raw code artifacts.
- Review builder output through diff plus task contract, not raw builder
  transcript.

### Cloud-Ready Export

- `code-mower cloud export` creates a local benchmark bundle.
- No upload is required in v1.0.
- If upload is present, it must be explicit, dry-run-first, opt-in, and
  metadata-only by default.
- Bundle excludes source code, raw diffs, raw transcripts, auth output, and
  secrets by default.
- Bundle vocabulary uses traces, spans, scores, datasets, and experiments so a
  later cloud service can aggregate without changing the local schema.

## Not Required For v1.0

- Hosted dashboards as a dependency for local value.
- Automatic cloud upload.
- Cloud account creation or login for local install, local audit, local value
  reports, or local export.
- GitLab or Bitbucket support.
- Live workflow mutation.
- Paid-lane auto-triggering.
- Universal merge authority for new reviewers.
- Fully automated authoring-run capture.
- Agent Communication Protocol/A2A support.
- Agent Client Protocol merge-gating support.

## Release Gate

v1.0 is ready when the generated standalone package passes the easy-mode smoke
script from a clean Python >=3.11 environment, and a new public repo plus a new
private repo can complete this sequence from a clean machine:

1. install package
2. run `code-mower init --easy`
3. run generated smoke tests
4. run `code-mower doctor --v05`
5. run `code-mower next-steps --profile recommended`
6. run at least one local/CLI audit lane in dry-run or PR-comment mode
7. generate a value report
8. export a local cloud benchmark bundle
9. run the product-repo wrapper rehearsal from at least one existing product
   repo with `CODE_MOWER_STANDALONE_PATH=/path/to/code-mower`
10. confirm `doctor --github` reports no recent Actions billing or spending
    limit blocks before treating branch protection as an autonomous merge gate
11. confirm a private-repo install path can fetch the standalone package in CI,
    or explicitly document that v1.0 requires a public Code Mower source or
    package-index install for CI usage
12. confirm provider-unavailable bypass docs are followed when a promoted lane
    cannot run, and that the failed run is excluded from reviewer-quality
    metrics

The generated package includes `scripts/smoke_easy_mode.py` to exercise the
core v1.0 path in a throwaway toy repository:

```bash
python scripts/smoke_easy_mode.py --json
```

The first public experience should answer, within about 30 minutes: "Which AI
reviewers are giving me useful signal on this codebase, and what should I run
next?"

## Required Docs

- `docs/current-state-and-roadmap.md`: current OSS/cloud state, v0.5 target,
  v1.0 direction, and near-term roadmap.
- `docs/github-setup.md`: GitHub auth, public/private repos, fork PRs, branch
  protection, token fallbacks, and the v1.0 GitHub-only scope.
- `docs/provider-matrix.md`: provider class, trigger, cost, source exposure,
  private repo requirements, and merge-authority posture.
- `docs/package-customization.md`: prompt lenses, context packs, and team rules.
- `docs/lane-promotion-policy.md`: evidence required before a lane gates merge.
- `docs/cloud-benchmarking.md`: local sanitized export first, upload later.

## Post-v1.0 Provider Expansion

After easy mode is reliable:

1. Calibrate Antigravity CLI as the forward Google CLI lane and treat Gemini
   CLI as legacy compatibility for public/consumer setups.
2. Continue Hermes CLI calibration as an informational doctrine-lens comparator.
   The first proof produced real signal plus context/parser gaps; rerun with
   context packs before deciding whether its ACP surface is worth a
   provider-neutral bridge.
3. Add Grok Build API as an OpenAI-compatible reviewer profile, then evaluate
   the CLI path separately.
4. Add Jules as a hosted async reviewer/builder lane by reusing the hosted
   bridge pattern.
5. Expand local Qwen/Gemma/DeepSeek calibration for the no- or low-cost OSS
   benchmark floor.
6. Explore Agent Client Protocol only after provider runtime, context-pack,
   parser, artifact, and doctor checks are boring.
