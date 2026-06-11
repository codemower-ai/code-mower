# Code Mower Calibration Pilot

This pilot measures reviewer value before changing merge policy.

## Goals

- Compare finding overlap across model families, prompt lenses, and repeated runs.
- Keep local and CLI reviewers informational until they have adjudicated evidence.
- Turn bakeoff outputs into disposition templates, reviewer metrics, and overlap reports.
- Preserve blind-review independence: no reviewer should see another reviewer output before its own run finishes.

## Recommended Pilot

Start with a small corpus of five known pull requests before spending on a
larger matrix. Mix easy, medium, and hard PRs, and include seeded-bug entries
when possible so catch rate is measurable without relying only on judgment
calls. For historical heads, include `base_ref` in the corpus entry so generated
Gemini CLI, CodeRabbit CLI, and local LLM commands compare the intended diff.
Add `review_class` and `context_packs` when a PR exercises a narrow specialty
such as `auth-history`, `calibration-policy`, `package-runtime`,
`backend-debug-upload`, or `web-debug-upload`; the policy report uses those
fields to recommend selective triggers instead of blanket merge gates.

Run:

```bash
code-mower calibration plan templates/calibration-corpus.json --replicates 2 --json
```

For a pinned PR head SHA:

```bash
gh pr view 123 --repo owner/repo --json headRefOid --jq .headRefOid
```

The generated plan contains nine arms:

- `topology-baseline`: different model families with the base audit lens.
- `same-provider-lenses`: the same provider with different prompt lenses.
- `same-provider-doctrine-lenses`: the same provider with the base audit lens
  compared against generic-programming and context-driven-quality doctrine
  lenses. Use this to test whether materially different review vocabulary
  produces useful disagreement beyond same-model repeated-run noise.
- `same-provider-risk-ops-lenses`: the same provider with the base audit lens
  compared against security-threat-model and operability lenses. Use this to
  test whether specialized production-risk lenses catch useful issues without
  becoming noisy global gates.
- `gemini-risk-ops-lens-fanout`: explicit-run Gemini CLI fan-out for
  `base-audit`, `security-threat-model`, and `operability`. This arm is skipped
  by default during `calibration run` so normal lane-only runs do not
  accidentally increase Gemini spend.
- `gemini-doctrine-lens-fanout`: explicit-run Gemini CLI fan-out for
  `base-audit`, `generic-programming`, and `context-driven-quality`.
- `hermes-doctrine-lens-fanout`: explicit-run Hermes Agent fan-out for
  `base-audit`, `generic-programming`, and `context-driven-quality`. Hermes is
  manual and informational until it has adjudicated calibration evidence.
- `same-provider-control`: the same provider and same prompt lens, repeated as a noise floor.
- `local-cli-models`: Qwen, Gemma, Gemini CLI, Antigravity CLI, Hermes CLI,
  CodeRabbit CLI, and similar informational reviewers.

The doctrine-lens arm currently includes:

- `claude-base-audit`: baseline structured audit doctrine.
- `claude-generic-programming`: base audit plus a generic-programming lens
  grounded in Alexander Stepanov's concepts, laws, minimal requirements,
  regularity, value semantics, and complexity contracts.
- `claude-context-driven-quality`: base audit plus a context-driven quality
  lens grounded in Cem Kaner, James Bach, and Michael Bolton's context-driven
  testing vocabulary: stakeholder value, risk, oracles, checking vs testing,
  coverage models, and testability.

The risk/ops-lens arm currently includes:

- `claude-base-audit`: baseline structured audit doctrine.
- `claude-security-threat-model`: base audit plus a security lens focused on
  assets, actors, trust boundaries, STRIDE-style threats, authorization,
  secrets, and disclosure surfaces.
- `claude-operability`: base audit plus an operability lens focused on failure
  modes, detection, diagnosis, recovery, rollout, rollback, environment
  assumptions, and operator action.

Interpret this arm with the control arm. A doctrine lens is interesting only if
it finds useful, adjudicated issues that the base repeated-run control does not
regularly find. Compare it with `topology-baseline` to decide whether lens
variation inside one model is enough, or whether different model families still
produce more valuable independence.

## Collection Flow

1. Run every reviewer against the exact same PR head SHA.
2. Store reviewer output under the plan's run directory and preserve
   `calibration-run-results.json`.
3. Keep outputs hidden until every reviewer in the arm finishes.
4. Build calibration summaries and write disposition templates.
5. Fill dispositions with `true_positive`, `false_positive`, `useful`, `noise`, or `unknown`.
6. Generate reviewer metrics and overlap reports.

Useful commands:

```bash
code-mower doctor code-mower.yml --profile cli_research --probe-runtime --json
code-mower context-packs templates/context-packs.example.json --json
code-mower calibration run templates/calibration-corpus.json --lanes antigravity-cli,gemini-cli,hermes-cli,coderabbit-cli,local-llm --repo-path-map owner/repo#123@HEAD_SHA=/path/to/pr-worktree --context-pack-manifest templates/context-packs.example.json --results-dir .code-mower/calibration-results --json
code-mower calibration run templates/calibration-corpus.json --lanes gemini-cli --arms gemini-risk-ops-lens-fanout --repo-path-map owner/repo#123@HEAD_SHA=/path/to/pr-worktree --context-pack-manifest templates/context-packs.example.json --results-dir .code-mower/lens-fanout-results --json
code-mower calibration run templates/calibration-corpus.json --lanes hermes-cli --arms hermes-doctrine-lens-fanout --repo-path-map owner/repo#123@HEAD_SHA=/path/to/pr-worktree --context-pack-manifest templates/context-packs.example.json --results-dir .code-mower/hermes-lens-fanout-results --json
code-mower calibration value-report templates/calibration-corpus.json --runs .code-mower/calibration-results/calibration-run-results.json --output docs/reviewer-value-report.md
code-mower local-llm calibrate .code-mower/calibration/pr-123/local-llm/summary.json --write-disposition-template .code-mower/calibration/pr-123/dispositions.json
code-mower calibration evidence templates/calibration-corpus.json --json
code-mower reviewer-metrics .code-mower/calibration/pr-123/calibration.json --spend templates/reviewer-spend.example.json --json
code-mower calibration overlap .code-mower/calibration/pr-123/calibration.json --json
code-mower calibration policy .code-mower/calibration/reviewer-metrics.json --json
```

Pass one `--repo-path-map` flag per mapped worktree. Do not combine multiple
repo mappings into a comma-separated value; the parser treats each flag value
as one mapping.

For a merged or historical PR head, create a detached worktree at the archived
head and compare it to the recorded base or merge-base:

```bash
git worktree add --detach /tmp/pr-123-blocked HEAD_SHA
code-mower gemini-cli --repo owner/repo --pr 123 \
  --repo-path /tmp/pr-123-blocked \
  --base-ref BASE_SHA \
  --expected-head-sha HEAD_SHA \
  --allow-historical-head \
  --context-pack-file .code-mower/context-packs/pr-123/context-pack.txt \
  --output-dir .code-mower/calibration/pr-123/gemini-cli \
  --json
code-mower antigravity-cli --repo owner/repo --pr 123 \
  --repo-path /tmp/pr-123-blocked \
  --base-ref BASE_SHA \
  --expected-head-sha HEAD_SHA \
  --allow-historical-head \
  --context-pack-file .code-mower/context-packs/pr-123/context-pack.txt \
  --output-dir .code-mower/calibration/pr-123/antigravity-cli \
  --json
code-mower hermes-cli --repo owner/repo --pr 123 \
  --repo-path /tmp/pr-123-blocked \
  --base-ref BASE_SHA \
  --expected-head-sha HEAD_SHA \
  --allow-historical-head \
  --historical-calibration \
  --context-pack-file .code-mower/context-packs/pr-123/context-pack.txt \
  --output-dir .code-mower/calibration/pr-123/hermes-cli \
  --json
code-mower local-llm bakeoff --repo owner/repo --pr 123 \
  --profiles qwen3-coder-next-lmstudio,gemma4-ollama \
  --repo-path /tmp/pr-123-blocked \
  --base-ref BASE_SHA \
  --expected-head-sha HEAD_SHA \
  --allow-historical-head \
  --output-dir .code-mower/calibration/pr-123/local-llm \
  --json
code-mower coderabbit-cli --repo owner/repo --pr 123 \
  --repo-path /tmp/pr-123-blocked \
  --base-ref BASE_SHA \
  --expected-head-sha HEAD_SHA \
  --allow-historical-head \
  --output-dir .code-mower/calibration/pr-123/coderabbit-cli \
  --json
```

Gemini CLI auth can be supplied without putting secrets in the repository:

```bash
code-mower init auth gemini --from-stdin --print-shell
export GEMINI_API_KEY_FILE=/path/to/gemini.env
```

The key file may contain either a raw API key or a shell-style assignment such as
`export GEMINI_API_KEY=...`. The init helper writes a raw key file with private
permissions and prints only the shell export, not the secret.

Antigravity CLI uses the local `agy` authentication state created by
`agy install`/login. Verify it with:

```bash
agy -p "Reply with exactly: ok"
```

Code Mower sends Antigravity a short `--print` instruction that reads the full
audit prompt from a sandboxed workspace file, because `agy` does not accept
Gemini CLI's `--output-format json` flag or stdin prompt contract. Current
Antigravity OAuth state lives under the operator's normal home directory, so
Code Mower requires an explicit trust opt-in before inheriting it:

```bash
export ANTIGRAVITY_CLI_USE_AMBIENT_HOME=1
```

Use that only in trusted local environments. The lane remains manual and
informational until Antigravity exposes a stronger noninteractive auth/sandbox
contract or calibration data justifies promotion.
After installation, confirm `agy --version` and the smoke prompt both work, then
run `code-mower doctor --profile cli_research --probe-runtime`.

Hermes Agent can be calibrated as a manual local CLI lane after local setup:

```bash
hermes setup
hermes --oneshot "Reply with exactly: ok"
export HERMES_CLI_USE_AMBIENT_HOME=1
```

The Hermes wrapper writes the full audit prompt to an isolated temp workspace
and runs `hermes --ignore-user-config --ignore-rules --toolsets "" --oneshot
@code-mower-hermes-prompt.txt`. Hermes expands that `@` context reference before
dispatching the one-shot message, so the full diff is not placed in process
argv. Use it only in trusted local calibration environments until Hermes has
stable parser behavior, richer context-pack coverage, and enough adjudicated
clean and blocked corpus evidence for selective-trigger consideration.

If a product-repo compatibility wrapper completes a structured audit but the
final GitHub comment POST fails, replay the saved public comment artifact
instead of rerunning the model:

```bash
tools/run_codex_audit_pr.sh --repost-verdict-artifact /path/to/verdict.json
tools/run_claude_audit_pr.sh --repost-verdict-artifact /path/to/verdict.json
```

Artifacts are written before posting under
`CODE_MOWER_VERDICT_ARTIFACT_DIR` when set, otherwise under
`~/.cache/cube-agent-audits/verdicts`.

Seeded-bug entries should include:

- `source` beginning with `seeded-bug-`;
- `expected_findings` with the path and concise expected bug summary;
- `base_ref` and `head_sha` so the exact diff is reproducible;
- `review_class` so selective-trigger evidence can be grouped by PR type;
- optional `context_packs` ids for the surrounding files a lane may need; and
- notes that distinguish the seeded defect from unrelated observations.

CodeRabbit CLI reviews a local checkout, so the worktree passed with
`--repo-path` must already be checked out at the pinned PR head and clean unless
the run is explicitly exploratory with `--allow-dirty`. Prefer a detached
temporary PR-head worktree plus the PR base commit as `--base-ref`, especially
after the PR has merged and `main` has moved.

Record clean-run evidence separately from findings:

- `reviewer_evidence` is for adjudicated findings.
- `reviewer_runs` is for whole-review outcomes such as a known-clean PR that
  produced zero findings, a rate-limit response, or a failed setup probe.

The policy command uses both signals. A reviewer needs useful findings and
known-clean zero-blocker runs on distinct PR heads before it can be considered
for promotion. It also needs known-blocked or seeded-bug catches; a quiet pass on
a known-blocked head is reported as `known_blocked_missed_runs`. Infrastructure
outcomes such as `error`, `failure`, and `rate_limited` are reported as
`infra_error_runs`; they affect reliability judgment without being counted as
blocking false positives.

Doctor auth probes redact raw output content in JSON reports. If a reviewer or
setup command emits account state or token-shaped strings, keep the raw
stdout/stderr files local and share the structured manifest/value report first.

## Promotion Rule

Treat this as evidence, not merge authority. Promote a reviewer or lens only after it shows useful findings on real PRs, acceptable noise, and reasonable cost or latency. A reviewer that mostly repeats CI or repeats another lane should stay informational or be narrowed to a specialty lens.
