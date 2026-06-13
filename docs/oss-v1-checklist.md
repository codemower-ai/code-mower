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

## Current Alpha Baseline

The current public-release baseline is `v0.1.0-alpha.25` of the standalone
package. It has proved:

- non-editable package-install rehearsal in a clean venv;
- fresh toy-repo easy-mode rehearsal from the installed package;
- `code-mower init --easy` smoke behavior;
- `doctor --easy --probe-runtime` provider probes for configured local CLIs;
- product-wrapper rehearsal with zero mismatches against the repo-local mirror;
- pinned standalone consumption from both private reference/product repos;
- private-repo standalone checkout shape through a read-only deploy-key shadow
  workflow;
- public-source standalone checkout from the public GitHub repository;
- calibration context-pack injection for Gemini, Antigravity, and Hermes CLI
  fan-out commands, including a dry-run proof on a known-problematic
  solver-runtime lens case;
- standalone labeler and bootstrap entrypoints for migrating product workflows
  away from mirrored Python files.
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
  compatibility shims without mirrored Python implementation files.
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

It has not yet proved:

- public package installation from PyPI or another package index;
- broad private-repo standalone checkout across arbitrary organizations and
  token policies;
- mirror deletion across arbitrary user repositories;
- broader spend-bearing context-pack lens runs beyond the first solver-runtime
  Gemini proof;
- a large enough reviewer/lens corpus for new merge gates.

## Easy Mode Flow

```bash
pipx install code-mower
code-mower init --easy
code-mower init --easy --apply --output-dir .code-mower.generated
code-mower doctor --easy --probe-runtime
code-mower next-steps --profile recommended
code-mower migration wrapper-rehearsal --repo-path /path/to/product-repo --json
code-mower migration package-install-rehearsal --package-spec code-mower --repo-path /path/to/product-repo --json
code-mower audit pr 123
code-mower calibration value-report templates/calibration-corpus.json
python scripts/smoke_easy_mode.py --json
```

The bundled starter corpus is for proving the first report path. It should not
be confused with Code Mower's richer reference corpus or a user's
product-specific benchmark corpus.
`reviewer-value-report.example.md` is the expected report for that starter
corpus before users add real reviewer runs and human dispositions.

`init --easy` is a safe alias for the recommended profile. It should render a
dry-run by default. `--apply` writes generated output to a reviewable directory
and still must not mutate live workflows, create labels, or trigger paid lanes.

## Release Plan

v1.0 should ship in four ordered slices:

1. **Install and easy-mode setup.** Package the CLI, render a starter config,
   and make `doctor --easy` produce actionable remediation without mutating a
   repository.
2. **GitHub readiness.** Document and check public/private repo behavior,
   workflow-token limits, branch protection, provider app access, and fork PR
   safety. GitHub is the only supported SCM for v1.0.
3. **First audit and value report.** Run the default reviewer set on one PR,
   persist local results, and generate a reviewer value report from a starter
   corpus.
4. **Cloud-ready export.** Produce a sanitized local benchmark bundle that can
   later feed an opt-in cloud service, without uploading in v1.0.

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

### Cloud-Ready Export

- `code-mower cloud export` creates a local benchmark bundle.
- No upload in v1.0.
- Bundle excludes source code, raw diffs, raw transcripts, auth output, and
  secrets by default.
- Bundle vocabulary uses traces, spans, scores, datasets, and experiments so a
  later cloud service can aggregate without changing the local schema.

## Not Required For v1.0

- Hosted dashboards.
- Automatic cloud upload.
- Cloud account creation or login.
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
4. run `code-mower doctor --easy --probe-runtime --github`
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
