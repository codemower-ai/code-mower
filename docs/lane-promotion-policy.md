# Code Mower Lane Promotion Policy

Code Mower promotes reviewers from experiment to merge signal only when the
evidence supports that move. This keeps the system fast without turning every
new model or CLI into a gate.

Generate the current evidence-based recommendation with:

```bash
code-mower calibration evidence tools/calibration_corpus.json --json > reviewer-evidence.json
code-mower reviewer-metrics reviewer-evidence.json --json > reviewer-metrics.json
code-mower calibration policy reviewer-metrics.json --json
```

Collect fresh non-gating calibration runs with:

```bash
code-mower doctor code-mower.yml --profile cli_research --probe-runtime --json
code-mower context-packs tools/context_packs.example.json --json
code-mower calibration run tools/calibration_corpus.json \
  --lanes antigravity-cli,gemini-cli,hermes-cli,coderabbit-cli,local-llm \
  --repo-path-map owner/repo#123@HEAD_SHA=/path/to/clean/pr-worktree \
  --results-dir .code-mower/calibration-results \
  --json
code-mower calibration value-report tools/calibration_corpus.json \
  --runs .code-mower/calibration-results/calibration-run-results.json \
  --output tools/CODE_MOWER_REVIEWER_VALUE_REPORT.md
```

The runner writes raw command, stdout, stderr, per-lane summaries, and a
`calibration-run-results.json` manifest. Treat raw stdout/stderr files as local
debugging artifacts; share the manifest and value report by default. Doctor auth
probes redact output content in JSON reports so account state does not leak into
calibration evidence. Treat that manifest as the durable evidence input; do not
rely on terminal scrollback.
When a corpus item includes `base_ref`, the generated local CLI commands carry
it through automatically. Use that for archived or seeded-bug heads so every
reviewer sees the same intended diff.
When a corpus item includes `review_class` and `context_packs`, the generated
policy report can recommend a narrow selective trigger instead of treating the
lane as a general merge gate.

The generated policy is advisory. Repository-specific merge bars still require
explicit configuration and the usual Code Mower audit protocol.

## Lane Classes

### Merge Gate

A merge-gating lane may block or satisfy the repository merge bar.

Requirements:

- structured output with a schema or trusted trailer;
- head-SHA pinning before and after the run;
- low false-positive and noise rates on adjudicated findings;
- evidence on both known-clean and known-blocked PRs;
- stable runtime through `code-mower doctor`; and
- clear ownership for fixing or dismissing blockers.

### Selective Trigger

A selective lane runs only for matching PR classes, such as packaging changes,
database migrations, docs/design changes, or high-risk diffs.

Requirements:

- a documented trigger rule;
- a prompt lens or context pack that explains the lane's specialty;
- better useful-rate in that class than in the default class; and
- no merge authority outside the trigger scope.

### Informational

An informational lane can produce review artifacts and calibration data but
does not affect merge authority.

Use this for:

- local LLM profiles during calibration;
- Antigravity CLI, Gemini CLI, Hermes CLI, and CodeRabbit CLI until their
  output is adjudicated;
- one-off reviewer experiments;
- expensive lanes before spend/value is understood; and
- lanes with known setup, auth, parse, or latency instability.

### Research

Research lanes are design spikes with no production expectations. They should
have no automatic labels, no merge authority, and no requirement to run on every
PR.

## Promotion Thresholds

Use these as starting thresholds, not permanent law:

- at least 10 adjudicated findings across at least 5 PRs;
- fresh `calibration run` artifacts for the lane, with raw output preserved;
- at least 2 known-clean PRs with no blocking false positives;
- at least 2 known-blocked or seeded-bug PRs where the lane catches real issues;
- useful-rate above 0.60 for general lanes, or above 0.75 for narrow selective
  lanes;
- precision above 0.70 on blocker-labeled findings;
- duplicate rate low enough to justify latency and spend; and
- `code-mower doctor --probe-runtime` passes for required CLIs/tokens, with no
  recurring auth, parser, stale-head, or environment failures.

Infrastructure failures are tracked separately from false positives. A
rate-limit, timeout, setup error, or tool failure should reduce confidence in a
lane's reliability, but it is not the same as a reviewer incorrectly blocking a
known-clean PR.

## Fresh-Run Workflow

1. Run `code-mower doctor` for the profile you plan to calibrate. Use
   `--probe-runtime` for local CLI lanes so missing auth, stale installs, or
   broken commands fail before the corpus starts.
   Run the CLI through the Code Mower bootstrap interpreter or another explicit
   Python 3.11+ runtime; a bare `python3` can resolve to an older local Python
   with a broken certificate store and turn a reviewer batch into an
   infrastructure failure.
2. Run `code-mower calibration run` with a narrow `--lanes` list. Start with one
   lane and one or two PRs before widening the batch.
3. Keep `--repo-path-map` entries pointed at clean, head-pinned worktrees for
   lanes that need local checkout context, especially CodeRabbit CLI. Use
   `owner/repo#PR=PATH`, `owner/repo@HEAD=PATH`, or
   `owner/repo#PR@HEAD=PATH` when the same repository appears more than once in
   the corpus. If the corpus item has `context_packs`, render the matching pack
   manifest before running expensive lanes and materialize only the packs that
   lane needs.
4. Adjudicate findings into true positive, useful, false positive, noise, or
   unknown. Unknowns are allowed, but they do not justify promotion.
5. Generate `calibration value-report --runs ...` and update lane policy from
   the combined historical corpus plus fresh-run evidence.

## Current Policy From Starter Evidence

Current generated value report:

- Corpus items: 18
- Adjudicated findings: 70
- Reviewer runs: 100
- Clean lens controls: CTVD #479/#481 and cube-snap #377/#380
- First Gemini risk/ops fan-out: cube-snap #347 known-blocked plus cube-snap
  #377 known-clean control
- Expanded clean-control fan-out: CTVD #481 and cube-snap #380 through Gemini
  CLI, CodeRabbit CLI, Qwen, Gemma, and Gemini base/risk/ops lenses.
- First Hermes doctrine proof: cube-snap #347 and #390 known-blocked heads plus
  cube-snap #377 and #380 known-clean controls through Hermes base,
  generic-programming, and context-driven-quality lenses.

- `codex-audit`: keep as a structured merge-gating lane where repo policy opts
  in. Current evidence is strongest on integration, parser, runner, credential,
  calibration-policy, and token-safety issues.
- `claude-audit`: keep as the normal structured Claude lane. The generated
  policy currently treats it as a selective-trigger candidate because it has
  strong useful-rate and clean controls but fewer than 10 adjudicated findings.
  Use `claude-review` only for explicit advisory prose review.
- `gitar`: keep where already approved, especially for platform/runtime hazards.
  Current starter-corpus evidence ranks it as a merge-gate candidate under the
  generated heuristics, with strong useful-rate and enough adjudicated findings.
  Keep repo-specific merge authority explicit rather than promoting it globally
  by default.
- `coderabbit-hosted`: selective-trigger candidate. Hosted CodeRabbit has clean
  pass evidence and useful findings, but the corpus still needs more
  adjudicated findings before treating it as a general gate.
- `qwen3-coder-next-lmstudio`: informational. It caught one known-blocked
  auth/history issue and produced useful setup notes, but it also blocked the
  fresh known-clean lens controls with false positives and noisy findings. Keep
  it out of merge authority until clean-control precision improves.
- `gemma4-ollama`: informational. It now has several clean PASS outcomes, but
  missed the known-blocked auth/history head and still has no useful finding
  evidence.
- `gemini-cli`: selective-trigger candidate from generated metrics. It has clean
  PASS evidence, two known-blocked catches, durable `GEMINI_API_KEY_FILE`
  support, and a local auth initializer. Start with narrow auth/history or
  high-risk selective triggers; do not make it a general merge gate yet.
- `gemini-base-audit`: selective-trigger candidate for auth/history-like
  calibration work, but only as an explicit fan-out arm while evidence is small.
- `gemini-operability`: selective-trigger candidate for production-readiness and
  auth/history work. It has clean-control evidence and a known-blocked catch,
  but needs more PR classes before routine use.
- `gemini-security-threat-model`: informational despite useful known-blocked
  signal, because the expanded CTVD clean-control fan-out produced a parse/setup
  failure. Stabilize reliability before selective triggering.
- `hermes-cli`: informational and manual. The wrapper can run Hermes Agent
  one-shot with prompt lenses and historical calibration mode. The first
  12-run doctrine proof caught useful #347 signal, but also produced a
  known-clean blocker, one parse/infra failure, and multiple
  audit-input-insufficient outcomes. Require `code-mower doctor --profile
  cli_research --probe-runtime` plus explicit `HERMES_CLI_USE_AMBIENT_HOME=1`
  in trusted local environments before using it. Do not promote until context
  packs or larger diff budgets eliminate the #390 input gap and human
  dispositions exist for the findings.
- `coderabbit-cli`: informational until its head-bound local artifact summaries
  are calibrated separately from the existing hosted signal. The runner now
  treats low-severity CLI suggestions as non-blocking calibration evidence, but
  the corpus still records a known-blocked miss and a preserved rate-limit
  artifact.
- `generic-programming`, `context-driven-quality`, `security-threat-model`,
  and `operability` lenses: calibration-only as lens doctrine. The Gemini
  `operability` lane is eligible for the next selective-trigger trial, while
  `security-threat-model` needs reliability cleanup first. Hermes doctrine
  lenses remain informational because the first proof showed context and parser
  gaps. No lens should become a merge gate without broader clean and blocked
  evidence.
- `acp_bridge`: research until a real adapter and protocol runner exist.

The value report now includes cost per run, seconds per run, cost per useful
finding, known-blocked catch/miss counts, generated policy classification, and a
recommended role. Use those columns to decide whether a lane is worth running
routinely, running only under a selective trigger, or leaving as a manual
research tool.

## Demotion Rules

Demote or narrow a lane when:

- false positives repeatedly block known-clean PRs;
- findings mostly duplicate CI or another cheaper reviewer;
- setup failures are common enough to slow merges;
- spend increases without useful findings; or
- the lane cannot preserve blind-review independence.
