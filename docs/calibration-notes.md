# Code Mower Calibration Notes

These notes keep reviewer-quality context durable across runs. They are not a
merge policy by themselves; use them with `templates/calibration-corpus.json`,
calibration outputs, disposition templates, and `code-mower reviewer-metrics`.

## Current Intent

Code Mower should maximize safe development velocity by making review lanes
faster, more independent, and more measurable. The calibration work answers
three questions before changing merge authority:

- Which reviewers find real issues that other lanes miss?
- Which reviewers mostly duplicate CI, duplicate another lane, or produce noise?
- Which reviewers are worth their latency and spend for a given PR class?

## Lane Semantics

- `*-audit`: automated, structured, head-SHA-bound, schema-backed, and suitable
  for merge gates when the repo has opted into that lane.
- `*-review`: explicit advisory prose review. Code Mower should not fire this as
  routine merge machinery.
- Local and CLI research lanes: informational until enough calibrated evidence
  exists. This includes Antigravity CLI, Gemini CLI, Hermes CLI, local
  Qwen/Gemma profiles, Aider, and future provider experiments.
- Prompt lenses: review doctrine bundles. Use them to change what a lane looks
  for, not to stuff PR-specific context into the prompt.
- Doctrine lenses: prompt lenses that deliberately shift the same model's
  review vocabulary. The starter set includes `generic-programming`, grounded
  in Alexander Stepanov's generic-programming discipline, and
  `context-driven-quality`, grounded in context-driven testing ideas associated
  with Cem Kaner, James Bach, and Michael Bolton.
- Risk/operations lenses: prompt lenses for recurring production classes. The
  starter set includes `security-threat-model` for assets, actors, trust
  boundaries, authorization, secrets, and disclosure surfaces, plus
  `operability` for failure modes, deploy/runtime assumptions, diagnostics,
  rollback, and recovery.
- Context packs: bounded surrounding-file manifests. Use them when a lane needs
  context beyond the diff without bloating every audit.
- Review classes: corpus categories such as `auth-history`,
  `calibration-policy`, `backend-debug-upload`, or `web-debug-upload`. Use them
  to decide selective triggers from evidence instead of promoting a reviewer
  globally because it helped in one narrow area.

## Reviewer Observations So Far

These are working notes, not permanent rankings.

- Codex audit has been strong at integration seams, exit semantics, parser edge
  cases, credential leakage, and runner safety. It is useful as a structured
  correctness lane, especially for Code Mower itself.
- Claude audit has been strong at architectural invariants and protocol
  mismatches, especially when a wrapper relies on a CLI behavior that needs to
  be explicit. It should run through the structured audit lane for routine code.
- Gitar has been useful for concrete runtime and platform hazards, such as OS
  argument-length limits. Treat its findings seriously even when its label is
  configured as informational.
- CodeRabbit remains useful as a broad PR-review signal. Its CLI path now has a
  head-bound artifact capture runner and has found useful calibration-slice
  issues. Keep it informational until it has more clean-run, known-blocked, and
  reliability evidence; the first known-blocked auth/history run produced no
  findings.
- Gemini CLI is wired as an informational calibration runner. `code-mower
  doctor` accepts `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `GEMINI_API_KEY_FILE`, or
  `GOOGLE_API_KEY_FILE`, and `code-mower init auth gemini` creates a durable
  local key file without printing the secret. Gemini now has four clean PASS
  controls and two known-blocked catches, which makes it the strongest new
  selective-trigger candidate but still not a merge gate.
- Hermes CLI is wired as an informational calibration runner. It writes the
  full audit prompt to a temp workspace and invokes `hermes --ignore-user-config
  --ignore-rules --toolsets "" --oneshot @code-mower-hermes-prompt.txt`,
  avoiding full prompt payloads in argv through Hermes' single-query `@`
  context-reference expansion. It requires `HERMES_CLI_USE_AMBIENT_HOME=1`
  before inheriting local Hermes auth/session state. Its first 12-run doctrine
  proof found real #347 signal, but also produced one clean-control blocker,
  one parse/infra failure, and three audit-input-insufficient outcomes. Keep it
  informational/manual until richer context and parser stability improve.
- Qwen and Gemma local profiles are promising for cheap independence, but the
  first real corpus slice keeps both informational: Qwen caught useful issues,
  including one known-blocked auth/history issue, but over-blocked known-clean
  PRs; Gemma was quiet on clean controls but missed the known-blocked head.
- Hosted CodeRabbit is now a selective-trigger candidate in the starter value
  report, but not a general merge gate. It needs more adjudicated findings
  across PR classes before promotion.

## Current Evidence Anchors

Use the starter corpus in `templates/calibration-corpus.json` first. It includes:

- Known-clean Code Mower lane-semantics PRs.
- A known-blocked auth/history slice with expected findings for solve-history
  timing, replay suppression scope, and server-side quota enforcement.
- Known-clean-after-fixes Gemini CLI runner PRs with intermediate blocker
  evidence preserved under `reviewer_evidence`.
- Known-clean-after-fixes CodeRabbit CLI artifact PRs with intermediate
  reviewer findings under `reviewer_evidence` and final whole-review outcomes
  under `reviewer_runs`.
- Known-clean-after-fixes calibration-policy/package PRs #472/#368 with
  intermediate Codex findings for doctor auth probing, duplicate base refs,
  token-env validation, and historical local checkout reproducibility.
- Known-clean prompt-lens calibration controls #479/#377 with package coverage
  for `generic-programming`, `context-driven-quality`, `security-threat-model`,
  and `operability`.
- Known-clean expanded fan-out controls #481/#380 with Gemini CLI, CodeRabbit
  CLI, Qwen, Gemma, and Gemini base/risk/ops lane evidence.
- Hermes CLI support is present in the provider catalog, doctor, package
  skeleton, and calibration runner. The starter corpus now includes the first
  Hermes doctrine proof as whole-review run evidence; human-disposition finding
  evidence still needs to be added before comparing useful-rate.

See `docs/lens-calibration-report.md` for the current lens-specific
policy. The initial lens entries prove the lens package surface is clean; they
do not prove that a lens should be promoted until it catches adjudicated
findings.

Latest bounded lens smoke, 2026-06-11:

- Corpus shape: one known-clean control
  (`jeffhuber/cube-two-view-debugger#455`) and one known-blocked auth/history
  control (`jeffhuber/cube-snap#347`).
- Runner: `gemini-doctrine-lens-fanout` with `base-audit`,
  `generic-programming`, and `context-driven-quality`.
- Known-clean outcome: all three Gemini doctrine variants passed with zero
  findings.
- Known-blocked outcome: `generic-programming` and
  `context-driven-quality` both produced parseable blocked verdicts and caught
  the replay-suppression/history-loss issue. The quality lens also raised
  additional testability/RLS concerns that still need human disposition.
- Infrastructure outcome: the base-audit retry failed before producing a
  summary because of a URL/name-resolution error, and an earlier attempt showed
  Gemini selecting `GOOGLE_API_KEY` when both `GOOGLE_API_KEY` and
  `GEMINI_API_KEY` were present. Treat this as runtime hardening evidence, not
  reviewer-quality evidence.
- Policy outcome: doctrine lenses remain informational. The result makes the
  experiment credible, but promotion still requires more known-clean controls,
  more known-blocked or seeded-bug catches, and adjudicated dispositions.

Latest context-pack lens proof, 2026-06-11:

- Corpus shape: one known-blocked iOS solver performance control
  (`jeffhuber/cube-snap#390` at
  `2f7807300c2fe7118e48ff0c6271d2edba11166b`).
- Context: `ios-solver-runtime`, 141097 bytes of bounded surrounding files,
  including `CubeJSBridge.swift`.
- Runner: `gemini-doctrine-lens-fanout` with `base-audit`,
  `generic-programming`, and `context-driven-quality`.
- Outcome: base audit and context-driven-quality blocked with parseable JSON;
  generic-programming passed with one non-blocking finding.
- Evidence: all three missed the expected cancellation blocker, but base and
  context-driven-quality found real adjacent solver-runtime risks around zlib
  format compatibility and cache-contaminated tests.
- Policy outcome: context packs are worth keeping in the calibration path, and
  Gemini stays useful as an informational/selective candidate. Doctrine lenses
  remain calibration-only until expected-finding matches improve on more cases.

Summarize embedded historical evidence with:

```bash
code-mower calibration evidence templates/calibration-corpus.json --json
```

Fold that report into reviewer metrics with:

```bash
code-mower reviewer-metrics reviewer-evidence.json --json
```

Turn reviewer metrics into a promotion recommendation with:

```bash
code-mower calibration policy reviewer-metrics.json --json
```

Use `reviewer_evidence` for adjudicated findings and `reviewer_runs` for
whole-review outcomes. A zero-finding pass on a known-clean PR is useful
calibration evidence, but it is not the same thing as a useful finding.

Good future corpus additions:

- Seeded bug PRs where the expected finding is unambiguous.
- Large-diff PRs that exercise diff budgeting and context packs.
- Docs/design PRs that can compare `base-audit` against `docs-design`.
- Architecture/refactor PRs that can compare `base-audit` against
  `generic-programming`.
- Risk/testability/security PRs that can compare `base-audit` against
  `context-driven-quality`.
- Auth, subscription, debug-upload, and secret-handling PRs that can compare
  `base-audit` against `security-threat-model`.
- Deploy, Railway/Vercel/Xcode Cloud, CLI runtime, and diagnostic PRs that can
  compare `base-audit` against `operability`.

Seeded-bug entries should preserve the exact diff shape in data. Include
`head_sha`, `base_ref`, `expected_findings`, and any `context_packs` ids needed
to understand the bug. This lets `calibration plan` and `calibration run`
produce repeatable commands instead of relying on manual historical-head edits.

## Disposition Labels

Use the existing disposition vocabulary:

- `true_positive`: a real issue that should have blocked or changed the PR.
- `useful`: real and worth knowing, even if not strictly blocking.
- `false_positive`: incorrect or based on a misunderstanding.
- `noise`: technically true but low-signal, duplicative, or not worth action.
- `unknown`: not adjudicated yet.

Prefer disposition templates over memory. Every useful reviewer comparison needs
the finding text, reviewer lane, PR head SHA, and final human disposition.

## Metrics To Watch

- Catch rate on expected findings.
- Miss rate on known-blocked or seeded-bug heads.
- Precision on adjudicated findings.
- Useful-rate across all findings.
- Duplicate rate against other reviewers and CI.
- Time to result.
- Spend per useful finding.
- Cost per run and seconds per run.
- Cost and seconds per known-blocked catch.
- Failure rate from auth, CLI, model, or environment setup.
- Head-SHA correctness: no reviewer output should count if it reviewed a stale
  or moving head.

## Promotion Criteria

Promote an informational lane only after it shows:

- useful findings on multiple PR types;
- low false-positive and low noise rates;
- low duplication with existing gate lanes;
- stable setup through `code-mower doctor`;
- clear spend/latency value; and
- compatibility with blind-review independence.

A lane that is helpful only for a narrow class should become a lens or
selective trigger, not a universal merge gate.

## Operational Gotchas

- Do not leak `GITHUB_TOKEN`, `GH_TOKEN`, or other operator secrets into local
  model subprocesses.
- Do not persist raw GitHub auth probe output in shareable doctor reports.
  Account names, scopes, and host state are operationally useful but not needed
  in calibration artifacts. Prefer redacted booleans and coarse shape only; do
  not include previews or content-derived hashes.
- Treat Markdown PR, issue, review, and comment bodies as data. Never pass
  Markdown inline through double-quoted shell arguments such as `gh pr create
  --body "..."`, because backticks and `$()` can be shell-interpreted before
  GitHub receives the body. Use `--body-file`, `tools/safe_gh_comment.py
  --body-file`, stdin, or a JSON/API payload.
- Keep prompt delivery explicit and tested for each CLI. If a CLI reads prompt
  from stdin only under certain flags, verify that contract before relying on it.
- Keep Python controlled through the Code Mower bootstrap runner; do not let temp
  worktrees fall back to random system Python.
- The 2026-06-08 Gemini refresh proved why that Python guard matters: a bare
  `python3` on the Mac resolved to old Framework Python 3.8 with a broken TLS
  certificate store, causing GitHub API fetches to fail before Gemini ran. Use
  the repo bootstrap interpreter or an explicit Python 3.11+ runtime for
  calibration batches.
- `--repo-path-map` is a repeatable flag. Pass it once per repo or per
  repo/PR/head selector; do not comma-separate multiple mappings in one value.
- When using `GEMINI_API_KEY_FILE`, make sure ambient `GOOGLE_API_KEY` is unset
  or intentionally equivalent. The current Gemini CLI chooses `GOOGLE_API_KEY`
  when both variables are present, which can make a run use the wrong account or
  quota pool.
- Treat direct JSON, wrapped JSON, and malformed model output as separate parser
  cases. A parse failure should not look like a successful calibration run.
- Preserve head-SHA pinning before and after long-running reviews.
- For CodeRabbit CLI, run against a clean local worktree at the pinned PR head;
  otherwise the CLI may review different local changes than the corpus item.

## Next Calibration Steps

1. Add known-blocked or seeded-bug PRs so Antigravity CLI, Gemini CLI, Hermes
   CLI, CodeRabbit CLI, Qwen, and Gemma can be scored on catch rate and miss
   rate, not only known-clean behavior.
2. Write disposition templates for all new findings and adjudicate them.
3. Render `templates/context-packs.example.json` or a repo-local context-pack
   manifest before running expensive lanes that need surrounding files.
4. Generate `reviewer-metrics` with spend and timing where available.
5. Promote the best corpus entries into `templates/calibration-corpus.json`.
6. Use the report to decide which lanes stay informational, which get narrower
   triggers, and which deserve merge authority.

## Same-Provider Doctrine Lens Experiment

Use the `same-provider-doctrine-lenses` calibration arm to answer whether
different review doctrine inside one model produces useful disagreement beyond
the same model's noise floor.

Use `same-provider-risk-ops-lenses` for the same measurement pattern on
security and operability review classes.

Use `gemini-risk-ops-lens-fanout` when you want executable Gemini CLI evidence
for `base-audit`, `security-threat-model`, and `operability` on the same head.
The arm must be selected explicitly with `--arms` so ordinary lane-only runs do
not accidentally spend three Gemini calls per PR.

Use `hermes-doctrine-lens-fanout` when you want executable Hermes Agent evidence
for `base-audit`, `generic-programming`, and `context-driven-quality` on the
same head. This arm is also explicit-run only and should remain research-grade
until human dispositions exist.

Use `--context-pack-manifest` when a corpus item has `context_packs`, especially
after a previous run produced `audit_input_insufficient`. The alpha.6 harness
materializes only the selected packs from the mapped local checkout and passes a
generated `--context-pack-file` to Gemini, Antigravity, and Hermes CLI prompts.
That keeps routine prompts small while letting repeated blind spots get enough
surrounding code to become real reviewer evidence.

Run at least three comparison groups before drawing a conclusion:

- same model, same lens, repeated: estimates repeated-run variance;
- same model, different doctrine lenses: estimates lens-driven disagreement;
- same model, specialized risk/operations lenses: estimates whether narrow
  production-risk lenses create useful selective-trigger evidence;
- different model families with the same base lens: estimates topology-driven
  disagreement.

Treat the result as meaningful only after human adjudication. A lens that
produces more findings but mostly noise should stay informational. A lens that
catches useful findings in a narrow review class should become a selective
trigger or reviewer option for that class, not a global merge gate.

## First Live Local Bakeoff Signal

The first live Qwen/Gemma passes against known-clean Code Mower PRs were useful
negative-control runs. Qwen produced useful setup observations and caught one
known-blocked auth/history issue, but also over-blocked known-clean heads. Gemma
produced clean PASS outcomes but no useful findings yet and missed the
known-blocked auth/history head. Keep both local profiles informational until
they show better precision and catch rate on a broader corpus.

## First Live Gemini And CodeRabbit CLI Signal

Gemini CLI produced clean PASS outcomes on known-clean-after-fixes PRs once the
runner read an external `GEMINI_API_KEY_FILE`. That makes it a good
clean-control candidate.

CodeRabbit CLI produced useful findings on the calibration slice and can now be
captured as a head-bound local artifact. It needs more reliability evidence
because the corpus still preserves a rate-limit run and a known-blocked miss
even though the expanded clean-control runs now pass.

The expanded clean-control run showed that CodeRabbit CLI emits low-severity
minor suggestions as finding events. For calibration policy, those suggestions
should be preserved as raw evidence but should not automatically turn a
known-clean control into a blocking result. The runner now separates raw finding
count from blocking finding count for recognized non-blocking CodeRabbit
severities.

## First Known-Blocked Auth/History Signal

The first known-blocked auth/history calibration head was cube-snap PR #347 at
`0683a90fb349a16a698d92f982b8f1abfab2398b`. Gemini CLI caught the replay
suppression scope bug. Qwen caught a real solve-history concern but also
produced several noisy or incorrect blockers. CodeRabbit CLI and Gemma produced
no blocking findings, so both missed the known-blocked head. This is the first
evidence that catch-rate and miss-rate need to be first-class metrics beside
clean-pass behavior.

That run also justified historical-head support for calibration runners:
Gemini CLI, CodeRabbit CLI, and local LLM bakeoff can now review a detached
local checkout with `--repo-path`, `--base-ref`, and `--allow-historical-head`
instead of pretending a merged PR's current GitHub head is still the review
target.

## Second Gemini Known-Blocked Refresh

The 2026-06-08 refresh reran Gemini CLI against cube-snap PR #347 and the
known-clean web-debug PR #363 using detached historical worktrees. Gemini again
blocked #347 and matched one expected finding, while also raising server-side
storage/quota concerns; it passed #363 with no findings. The run adds another
known-blocked catch and another clean control, but it still does not make Gemini
a merge gate because the adjudicated finding count is small and latency was
roughly 81 seconds for the blocked head plus 184 seconds for the clean head.

The follow-up package/customization slice also ran Gemini CLI through
`calibration run` against CTVD PR #455. It passed a known-clean head in about 41
seconds with zero findings, adding a fourth clean-control run to the starter
corpus.

## Calibration-Policy Package Slice

The 2026-06-08 calibration-policy/package slice added CTVD PR #472 and
cube-snap PR #368 to the starter corpus. Both final heads were known-clean after
Codex caught real intermediate issues: GitHub auth could be treated as valid
just because `gh` existed, CodeRabbit CLI could receive duplicate `--base-ref`
arguments, token env variables needed runtime validation under
`--probe-runtime`, and historical Gemini/local-LLM commands needed explicit repo
paths when `base_ref` was present. Claude also raised a useful non-blocking
privacy point: auth-probe `output_preview` fields can expose account state in
shareable reports. Follow-up Codex audit caught that content-derived hashes can
also leak account-state correlation. The doctor now redacts auth-probe output
content and records only content-free shape diagnostics.

## Expanded Prompt-Lens Clean Controls

The 2026-06-09 expanded calibration slice added CTVD PR #481 and cube-snap
PR #380 as known-clean prompt-lens controls. The same heads were run through
Gemini CLI, CodeRabbit CLI, Qwen, Gemma, and the Gemini base/risk/ops fan-out.

What the slice showed:

- Gemini CLI passed both clean controls and remains the strongest new
  selective-trigger candidate, especially for auth/history and high-risk diffs.
- Gemini `operability` passed both clean controls and has enough early evidence
  for the next narrow production-readiness trigger experiment.
- Gemini `security-threat-model` passed the cube-snap clean control but hit a
  parse/setup failure on the CTVD control, so it stays informational until
  reliability improves.
- CodeRabbit CLI passed after minor suggestions were normalized as non-blocking
  calibration evidence. Keep it informational until it catches known-blocked or
  seeded-bug heads reliably.
- Qwen blocked both known-clean controls with false positives/noise. Keep it
  informational even though it has caught real issues elsewhere.
- Gemma passed the clean controls but still lacks useful finding evidence and
  missed the known-blocked auth/history head.

The current policy decision is deliberately conservative: no new merge gates.
Codex and Gitar remain merge-gate candidates where repo policy opts in. Gemini
CLI and Gemini `operability` can move to the next selective-trigger trial.
CodeRabbit CLI, Qwen, Gemma, and Gemini `security-threat-model` remain
informational calibration lanes.

## Google CLI Transition And Verdict Replay

The 2026-06-10 provider-reliability slice added Antigravity CLI as the forward
Google local-CLI lane while keeping Gemini CLI as a compatibility lane for the
evidence collected so far. The Antigravity lane reuses the Google CLI audit
contract, prompt-lens support, historical calibration mode, and local secret
file conventions, but it does not inherit Gemini's calibration evidence. Treat
Antigravity as informational until it has run the same known-blocked and
known-clean corpus heads.

The same slice also made Codex and Claude structured audit verdicts durable
before posting. If GitHub posting fails after the model run completes, replay
the saved verdict artifact instead of paying for or waiting on a duplicate
audit. This should reduce wasted reruns during flaky network sessions while
preserving head-bound, structured review evidence.

## Hermes Agent Lane

The 2026-06-10 Hermes slice added an informational `hermes-cli` lane, provider
template, package export, lane config, doctor visibility, and calibration
runner support. Hermes is useful to test because it exposes both a one-shot CLI
and ACP-related surfaces, but Code Mower should first stabilize the plain
one-shot lane and measure it in the existing corpus before promoting an ACP
bridge or merge authority.

The first Hermes calibration used two known-blocked PRs and two known-clean
controls with `base-audit`, `generic-programming`, and
`context-driven-quality`. It showed:

- `hermes-base-audit` caught useful #347 auth/history signal, but also blocked
  the #380 clean control.
- `hermes-generic-programming` and `hermes-context-driven-quality` frequently
  identified insufficient audit input instead of forcing a normal code verdict.
- #390 exposed the next context-pack need: the supplied diff omitted the
  solver-cache implementation, so Hermes could not catch the known Tighten
  cancellation blocker.
- One Hermes run failed JSON parsing and counts as an infra/parser failure.

Treat Hermes as a manual informational comparator until the next run proves it
can handle the same heads with sufficient context and stable parsing.
