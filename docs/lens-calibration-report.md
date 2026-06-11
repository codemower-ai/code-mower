# Code Mower Lens Calibration Report

This report records the first evidence baseline for Code Mower prompt lenses.
It is deliberately conservative: the new lenses are available and package-tested,
but they are not merge-gating signals until calibration shows that they catch
useful findings without adding noise.

## Current Corpus

The starter corpus now includes 18 real PR outcomes:

- 14 existing Code Mower and CubeSnap calibration items.
- 4 known-clean prompt-lens calibration controls:
  - `jeffhuber/cube-two-view-debugger#479` at `e0c909068dc59891f8711fbfadd1de56c6fd7e81`
  - `jeffhuber/cube-snap#377` at `ebc0a99d36d4974e72fc15a6bdb79972903ba5c1`
  - `jeffhuber/cube-two-view-debugger#481` at `a80c6c45d4fd33f7e35ffe65fb116299f8c701f4`
  - `jeffhuber/cube-snap#380` at `c45ce1c00c37a97a44fc35f0efeaf32a4fad9393`

The first two PRs added the `generic-programming`, `context-driven-quality`,
`security-threat-model`, and `operability` lenses, mirrored package coverage,
and passed Codex, Claude, Gitar, CodeRabbit, and post-merge verification. The
second two PRs added Gemini fan-out calibration over clean controls, including
Gemini CLI, CodeRabbit CLI, Qwen, Gemma, and the Gemini base/risk/ops lenses.
They are useful as clean controls for the lens infrastructure and reviewer
stability. They are not by themselves enough to prove that any specific lens
catches more bugs.

## Generated Value Snapshot

`docs/reviewer-value-report.md` was regenerated from the corpus:

- Corpus items: 18
- Adjudicated findings: 70
- Reviewer runs: 100

Generated policy classes from the current evidence:

| Reviewer | Generated class | Recommended role |
| --- | --- | --- |
| `codex-audit` | `merge_gate_candidate` | `merge_gate_eligible` |
| `gitar` | `merge_gate_candidate` | `merge_gate_eligible` |
| `claude-audit` | `selective_trigger_candidate` | `selective_trigger` |
| `gemini-cli` | `selective_trigger_candidate` | `selective_trigger` |
| `coderabbit-hosted` | `selective_trigger_candidate` | `selective_trigger` |
| `gemini-base-audit` | `selective_trigger_candidate` | `selective_trigger` |
| `gemini-security-threat-model` | `informational` | `informational` |
| `gemini-operability` | `selective_trigger_candidate` | `selective_trigger` |
| `coderabbit-cli` | `informational` | `informational` |
| `hermes-base-audit` | `informational` | `informational` |
| `hermes-context-driven-quality` | `informational` | `informational` |
| `hermes-generic-programming` | `informational` | `informational` |
| `qwen3-coder-next-lmstudio` | `informational` | `informational` |
| `gemma4-ollama` | `informational` | `informational` |

This generated classification is evidence, not an automatic repository policy.
Repository merge bars still require explicit opt-in and the normal Code Mower
audit protocol.

## Lens Policy

For now, the four experimental lens definitions stay calibration-only:

- `generic-programming`: architecture/API/abstraction review. Use on design,
  algorithm, and reusable-surface PRs while measuring whether it finds real
  design defects beyond base audit.
- `context-driven-quality`: risk, test strategy, and user-impact review. Use on
  feature, QA, release-readiness, and behavioral PRs while measuring useful-rate
  and noise.
- `security-threat-model`: auth, authorization, entitlement, billing, secret,
  debug-upload, storage, and trust-boundary review. Use as a selective trigger
  candidate for security-sensitive changes.
- `operability`: deployment, runtime, observability, rollback, debug tooling,
  and failure-recovery review. Use as a selective trigger candidate for
  production-readiness changes.

Promotion requires adjudicated findings, not just clean passes. A lens can move
from calibration-only to selective trigger only after it has:

- at least two known-clean zero-blocker controls in its target class;
- at least one known-blocked or seeded-bug catch in its target class;
- useful-rate above the selective-trigger threshold;
- no recurring infra, auth, parser, or stale-head failures; and
- a clear trigger rule tied to `review_class`, changed paths, or context packs.

No lens should become a general merge gate until it has broad multi-class
evidence and shows value beyond the base audit lane.

## Next Experiment

Code Mower now has first-class Gemini CLI fan-out for the production-risk
lenses and the doctrine lenses, plus Hermes Agent doctrine fan-out for
`base-audit`, `generic-programming`, and `context-driven-quality`. These arms
are explicit-run only so normal lane-only calibration runs do not accidentally
multiply provider spend.

```bash
code-mower calibration run templates/calibration-corpus.json \
  --lanes gemini-cli \
  --arms gemini-risk-ops-lens-fanout \
  --repo-path-map OWNER/REPO#PR@HEAD=/path/to/pr-worktree \
  --allow-historical-head \
  --output-dir .code-mower/lens-fanout \
  --results-dir .code-mower/lens-fanout-results
```

Run the doctrine fan-out when the experiment needs to compare a base audit
against `generic-programming` and `context-driven-quality` on the same model:

```bash
code-mower calibration run templates/calibration-corpus.json \
  --lanes gemini-cli \
  --arms gemini-doctrine-lens-fanout \
  --repo-path-map OWNER/REPO#PR@HEAD=/path/to/pr-worktree \
  --allow-historical-head \
  --output-dir .code-mower/doctrine-lens-fanout \
  --results-dir .code-mower/doctrine-lens-fanout-results
```

Hermes can run the same doctrine fan-out as an informational comparator once
local Hermes auth is configured and the ambient-home trust opt-in is set:

```bash
HERMES_CLI_USE_AMBIENT_HOME=1 \
code-mower calibration run templates/calibration-corpus.json \
  --lanes hermes-cli \
  --arms hermes-doctrine-lens-fanout \
  --repo-path-map OWNER/REPO#PR@HEAD=/path/to/pr-worktree \
  --allow-historical-head \
  --output-dir .code-mower/hermes-doctrine-lens-fanout \
  --results-dir .code-mower/hermes-doctrine-lens-fanout-results
```

Hermes now has the same two known-blocked and two known-clean doctrine proof
cases used for the Claude/Gemini comparison. Treat its results as
informational: the first run showed real catch potential, but also a
known-clean false blocker, one parse failure, and multiple audit-input gaps.

The next definitive lens turn should run the explicit Gemini and Hermes doctrine
arms against the same known-blocked PRs and known-clean controls. That gives
Code Mower same-model, different-lens evidence plus a topology comparison
without mixing live PR state into the measurement.

## Alpha.3 Bounded Gemini Doctrine Smoke

After `v0.1.0-alpha.3`, a bounded smoke run tested the Gemini doctrine fan-out
against one known-clean control and one known-blocked historical PR:

- `jeffhuber/cube-two-view-debugger#455`
  (`363d054881863d45eee13e6dc6d076cec667f9b6`), known clean after prior
  reviewer approvals and post-merge verification.
- `jeffhuber/cube-snap#347`
  (`0683a90fb349a16a698d92f982b8f1abfab2398b`), known blocked by auth/history
  defects.

Results:

| Lens reviewer | Clean control | Known-blocked PR | Signal |
| --- | --- | --- | --- |
| `gemini-base-audit` | pass, 0 findings | setup failure on retry | clean control behaved correctly, but the blocked retry had provider/network instability |
| `gemini-generic-programming` | pass, 0 findings | blocked, 2 findings | caught the replay-suppression/history-loss issue and produced one extra lower-severity concern |
| `gemini-context-driven-quality` | pass, 0 findings | blocked, 3 findings | caught the replay-suppression/history-loss issue and added testability/RLS-oriented concerns |

The run is promising but not promotion-grade. It suggests doctrine alone can
change useful signal without producing false blockers on the clean control, but
the sample is too small and the base retry had an infrastructure failure. Keep
the doctrine lenses informational until the same experiment is promoted into the
durable corpus with adjudicated dispositions and more controls.

## First Fan-Out Result

The first real fan-out ran `gemini-base-audit`,
`gemini-security-threat-model`, and `gemini-operability` against:

- `jeffhuber/cube-snap#347`
  (`0683a90fb349a16a698d92f982b8f1abfab2398b`), a known-blocked auth/history
  PR with three expected findings.
- `jeffhuber/cube-snap#377`
  (`ebc0a99d36d4974e72fc15a6bdb79972903ba5c1`), a known-clean prompt-lens
  control.

Results:

| Lens reviewer | Blocked PR | Clean control | Adjudicated signal |
| --- | --- | --- | --- |
| `gemini-base-audit` | blocked, 2 findings | pass, 0 findings | true-positive replay suppression and migration quota/retention findings |
| `gemini-security-threat-model` | blocked, 2 findings | pass, 0 findings | true-positive replay suppression and migration quota/retention findings; also flagged unbounded solution length as an added abuse surface |
| `gemini-operability` | blocked, 2 findings | pass, 0 findings | true-positive replay suppression and migration quota/retention findings |

None of the three variants caught the solve-history write-timing finding in
`src/SolveStage.tsx`. The security lens produced the strongest differentiated
signal on this small sample because it framed the migration defect as a server
trust-boundary and abuse-surface issue. The operability lens produced a useful
production-risk framing, but did not yet show a distinct catch beyond the base
audit.

Decision from this slice: keep both `security-threat-model` and `operability`
as calibration-only. `security-threat-model` should be the next selective-trigger
candidate to test on auth, entitlement, billing, secrets, storage, and debug
upload PRs. `operability` should get more production-readiness controls before
promotion.

## Expanded Clean-Control Fan-Out

The next real fan-out added two clean controls:

- `jeffhuber/cube-two-view-debugger#481`
  (`a80c6c45d4fd33f7e35ffe65fb116299f8c701f4`)
- `jeffhuber/cube-snap#380`
  (`c45ce1c00c37a97a44fc35f0efeaf32a4fad9393`)

Results:

| Reviewer | CTVD #481 | cube-snap #380 | Decision |
| --- | --- | --- | --- |
| `gemini-cli` | pass | pass | Strongest new selective-trigger candidate, especially for auth/history or high-risk diffs. |
| `gemini-base-audit` | pass | pass | Eligible as an explicit fan-out baseline, not a general gate. |
| `gemini-security-threat-model` | infra/parse error | pass | Keep informational until reliability stabilizes. |
| `gemini-operability` | pass | pass | Eligible for the next selective-trigger trial on production-readiness changes. |
| `coderabbit-cli` | pass after minor suggestions were normalized as non-blocking | pass | Keep informational; useful convenience lane but not enough catch-rate evidence. |
| `qwen3-coder-next-lmstudio` | blocked known-clean | blocked known-clean | Keep informational due false-positive/noise rate. |
| `gemma4-ollama` | pass | pass | Keep informational because it still lacks useful finding evidence and missed the known-blocked head. |

Decision from the expanded slice: do not promote any new lens to merge-gating.
Use Gemini CLI and Gemini `operability` as the next narrow selective-trigger
experiment; keep `security-threat-model` and local LLM profiles informational
until reliability and precision improve.

## Doctrine Lens Fan-Out Result

The next same-model doctrine fan-out ran `gemini-base-audit`,
`gemini-generic-programming`, and `gemini-context-driven-quality` against the
same `cube-snap#347` known-blocked auth/history PR and the same `cube-snap#377`
known-clean prompt-lens control.

The first attempt exposed useful runner evidence: the repo-default `python3`
was Python 3.8 with a broken certificate chain, causing GitHub API lookups to
fail before Gemini ran. Rerunning with the repo Python 3.12 venv produced clean
JSON summaries for all six commands.

Results:

| Doctrine reviewer | Blocked PR | Clean control | Run signal |
| --- | --- | --- | --- |
| `gemini-base-audit` | blocked, 1 finding, 1 expected match | pass, 0 findings | caught the known auth/history blocker without a clean-control false positive |
| `gemini-generic-programming` | blocked, 1 finding, 1 expected match | pass, 0 findings | caught the same known blocker, but did not yet show differentiated signal beyond the base lens |
| `gemini-context-driven-quality` | blocked, 2 findings, 1 expected match | pass, 0 findings | caught the known blocker and produced one extra finding, making it the strongest doctrine-lens candidate from this tiny slice |

Decision from this slice: the doctrine-lens hypothesis is now testable with real
Code Mower evidence, and the first result is promising: same model, different
review doctrine produced useful independent reviewer records without clean-head
false positives. Keep `generic-programming` and `context-driven-quality`
informational until they have adjudicated finding dispositions and at least one
more known-clean control. The next definitive turn should add two or three more
known-blocked/known-clean pairs and compare overlap between base, risk/ops, and
doctrine lenses.

## Claude And Gemini Doctrine Proof

The next focused doctrine proof expanded the matrix to 24 runs:

- 2 known-blocked PRs:
  - `jeffhuber/cube-snap#347`
    (`0683a90fb349a16a698d92f982b8f1abfab2398b`)
  - `jeffhuber/cube-snap#390`
    (`2f7807300c2fe7118e48ff0c6271d2edba11166b`)
- 2 known-clean controls:
  - `jeffhuber/cube-snap#377`
    (`ebc0a99d36d4974e72fc15a6bdb79972903ba5c1`)
  - `jeffhuber/cube-snap#380`
    (`c45ce1c00c37a97a44fc35f0efeaf32a4fad9393`)
- 2 providers: Claude and Gemini CLI.
- 3 lenses: base audit, `generic-programming`, and
  `context-driven-quality`.

Results:

| Reviewer family | Lens | Known-blocked useful catches | Known-clean false blockers | Harness/input gaps |
| --- | --- | ---: | ---: | ---: |
| Claude | base audit | 1/2 | 0/2 | 0 |
| Claude | `generic-programming` | 2/2 | 0/2 | 0 |
| Claude | `context-driven-quality` | 2/2 | 0/2 | 0 |
| Gemini CLI | base audit | 1/2 | 0/2 | 1 |
| Gemini CLI | `generic-programming` | 1/2 | 0/2 | 1 |
| Gemini CLI | `context-driven-quality` | 1/2 | 0/2 | 1 |

This is not yet enough evidence to promote a doctrine lens, but it is enough to
answer the first reviewer-side hypothesis: doctrine alone changed useful signal
on the same code and same provider. Claude's doctrine lenses caught both
known-blocked heads while the base audit only caught one. Gemini also remained
clean on known-clean controls, but the second blocked head exposed a harness
limitation: multiple Gemini runs blocked because the audit input was incomplete
or too truncated to safely verify the target behavior.

Two harness changes came directly out of this proof:

- `audit_input_insufficient` is now a first-class run status. It means the lane
  surfaced a real review limitation, not a code defect and not an infra failure.
  Value reports count it separately from known-blocked misses and known-clean
  false blockers.
- Historical calibration mode is explicit for Gemini CLI archived-head runs.
  `--historical-calibration` marks the run as non-merge-authority evidence and
  lets the local checkout differ from the current PR head while preserving the
  stale-head guard for normal live reviews.

Decision from this slice: continue treating doctrine lenses as calibration-only,
but keep running them on known-blocked and known-clean PR pairs. The next proof
should add context packs or larger diff budgets for the PRs that produced
`audit_input_insufficient`, then compare whether the same lane catches the code
defect once it can see the right files.

## Hermes Doctrine Proof

The first Hermes doctrine proof used the same four-head shape as the
Claude/Gemini doctrine proof:

- 2 known-blocked PRs:
  - `jeffhuber/cube-snap#347`
    (`0683a90fb349a16a698d92f982b8f1abfab2398b`)
  - `jeffhuber/cube-snap#390`
    (`2f7807300c2fe7118e48ff0c6271d2edba11166b`)
- 2 known-clean controls:
  - `jeffhuber/cube-snap#377`
    (`ebc0a99d36d4974e72fc15a6bdb79972903ba5c1`)
  - `jeffhuber/cube-snap#380`
    (`c45ce1c00c37a97a44fc35f0efeaf32a4fad9393`)
- 1 provider: Hermes CLI.
- 3 lenses: base audit, `generic-programming`, and
  `context-driven-quality`.

Results:

| Hermes reviewer | Known-blocked useful catches | Known-clean false blockers | Harness/input gaps | Infra/parse failures |
| --- | ---: | ---: | ---: | ---: |
| `hermes-base-audit` | 1/2 | 1/2 | 1 | 0 |
| `hermes-generic-programming` | 0/2 | 0/2 | 1 | 1 |
| `hermes-context-driven-quality` | 0/2 | 0/2 | 1 | 0 |

Qualitative signal:

- `hermes-base-audit` caught two expected #347 themes: replay-suppression
  leakage and the missing/unauditable retention-quota migration surface.
- `hermes-generic-programming` and `hermes-context-driven-quality` correctly
  classified #347 as audit-input-insufficient rather than pretending the
  supplied diff was enough.
- On #390, both `hermes-base-audit` and
  `hermes-context-driven-quality` identified that the supplied diff omitted the
  solver-cache implementation, which is useful harness evidence. They did not
  catch the known Tighten cancellation blocker.
- `hermes-base-audit` produced a blocking false positive on the #380 clean
  control by treating report/corpus consistency issues as merge-blocking. The
  other two Hermes lenses passed that same clean head but still produced
  non-blocking suggestions.
- One `hermes-generic-programming` run produced a parse/infra error on #390.

Decision from this slice: Hermes stays informational/manual. It is promising as
a same-provider doctrine comparator, but it needs richer context, parser
stabilization, and human dispositions before any selective-trigger discussion.
The next Hermes experiment should rerun #390 with a context pack or larger diff
budget that includes the omitted solver-cache implementation, then compare
whether the lens catches the known cancellation blocker when the audit input is
sufficient.

## Alpha.6 Context-Pack Harness Proof

`v0.1.0-alpha.6` adds context-pack injection to calibration fan-out. The runner
can now:

- load a context-pack manifest with `--context-pack-manifest`;
- read each corpus item's `context_packs` ids;
- compute changed files from the mapped historical checkout;
- materialize only the selected bounded context files; and
- pass the generated context text into Gemini, Antigravity, and Hermes CLI
  prompts with `--context-pack-file`.

A dry-run against the known-blocked `cube-snap#390` head
(`2f7807300c2fe7118e48ff0c6271d2edba11166b`) proved the harness behavior for
the `ios-solver-runtime` pack:

| Proof item | Result |
| --- | --- |
| Corpus item | `jeffhuber/cube-snap#390` |
| Arm | `gemini-doctrine-lens-fanout` |
| Commands planned | 3 |
| Commands with `--context-pack-file` | 3/3 |
| Context pack | `ios-solver-runtime` |
| Context text size | 141112 bytes |

This is a harness proof, not a new reviewer-value result. The runtime used for
this alpha.6 implementation did not have `GEMINI_API_KEY` available, so the
spend-bearing Gemini rerun is intentionally left as a one-command follow-up
rather than fabricating evidence. The next measurement should run the same
command without `--dry-run`, then fold the resulting
`calibration-run-results.json` into `docs/reviewer-value-report.md`.

Example:

```bash
code-mower calibration run tools/calibration_corpus.json \
  --lanes gemini-cli \
  --arms gemini-doctrine-lens-fanout \
  --repo-path-map jeffhuber/cube-snap#390@2f7807300c2fe7118e48ff0c6271d2edba11166b=/tmp/cube-snap-pr-390 \
  --context-pack-manifest tools/context_packs.example.json \
  --allow-historical-head \
  --results-dir .code-mower/context-pack-lens-results \
  --json
```
