# Code Mower Authoring Intelligence

Code Mower should measure the whole AI-assisted delivery loop, not only the
review comments at the end of a pull request.

The product goal is:

> Code Mower is the fastest way to build a peer-programmer and reviewer system
> around the best AI coding agents. It helps teams drive from plan to merge at
> maximum safe velocity while preserving code quality, architecture, and
> deployment confidence. It also turns your real codebase into a
> quality-and-velocity benchmark, measuring which AI builders and reviewers
> deliver the best quality, speed, and cost results for your actual product.

Reviewer calibration answers "who caught the bug?" Authoring intelligence
answers "which builder plus reviewer loop shipped the best result for this
codebase?"

## Authoring Run

An authoring run is one bounded development attempt from agreed direction to
one or more pull requests.

Starter fields:

```json
{
  "schema": "code_mower.authoringRun.v1",
  "run_id": "2026-06-09-codemower-oss-easy-mode",
  "run_role": "implement",
  "repo": "owner/repo",
  "task_class": "package-extraction",
  "task_contract_hash": "sha256:example",
  "builder": {
    "provider": "codex",
    "tool": "codex-desktop",
    "model": "unknown"
  },
  "started_at": "2026-06-09T10:00:00Z",
  "ended_at": "2026-06-09T11:00:00Z",
  "pull_requests": [
    {"repo": "owner/repo", "number": 123, "head_sha": "abc123"}
  ],
  "branch": "codex/oss-easy-mode",
  "worktree": "/tmp/code-mower-runs/oss-easy-mode",
  "user_interventions": 0,
  "blocker_iterations": 0,
  "tests_added": 0,
  "tests_run": ["code-mower"],
  "review_lanes": ["codex-audit", "claude-audit", "gitar"],
  "merge_result": "merged",
  "post_merge_health": "verified",
  "cost": {
    "known_usd": null,
    "notes": "cost capture is provider-dependent"
  }
}
```

The first version can be a JSONL artifact written by a human or agent. Later
versions can collect timestamps, PR ids, review iterations, and post-merge
health automatically.

Use `run_role` or `purpose` consistently across authoring and reviewer events:
`implement`, `review`, `calibrate`, `release`, and `explore` are enough for the
first measurement loop.

## Delivery Report

A delivery report summarizes an authoring run in engineering terms:

- functionality shipped
- PRs and merge commits
- elapsed wall time
- user interventions
- audit blockers found and resolved
- tests and validation run
- post-merge CI and deployment health
- known spend and latency

This is the artifact that supports "how long can an AI coding session keep
driving productively?" It should distinguish true blockers from infra noise,
and it should count a run as successful only after merge and post-merge
verification.

## Builder Plus Reviewer Value

Measure combinations, not only individual tools:

- builder provider and model
- review lanes used
- blocker iterations per merged PR
- useful findings per authored PR
- false-positive interruptions
- elapsed time from plan to merge
- cost per merged feature
- post-merge failures or rollbacks

This lets a team compare loops such as:

- Codex authoring plus Codex and Claude audits
- Claude authoring plus Codex audit
- Gemini authoring plus Code Mower reviewer set
- local model authoring plus hosted review gates

The results are observational. They are still valuable because they are measured
on the team's real codebase, tests, architecture, and deployment pipeline.

## Privacy

Authoring intelligence should be shareable without source code by default.

Do not include:

- source code
- raw diffs
- raw model transcripts
- raw stdout/stderr
- auth output
- secrets or token-shaped strings

Use task classes, line-count buckets, durations, disposition counts, and merge
health summaries first. Make raw artifacts local-only unless the user
explicitly opts into sharing them.

## First Implementation Steps

The first package surface is `code-mower builder-experiment`. It produces a
deterministic plan for bounded authoring runs and a report from captured run
results.

```bash
code-mower builder-experiment plan builder-experiment.json --json
code-mower builder-experiment report builder-experiment.json \
  --runs builder-results.json \
  --output builder-experiment-report.md
```

The planning layer in [planning-work-orders.md](planning-work-orders.md) now
adds the missing upstream contract:

1. `code-mower project-context init` creates editable architecture, CI/CD,
   hosting, design-system, quality-bar, and agent-team doctrine files.
2. `code-mower context add --external ...` records external docs as
   metadata-only local manifests by default.
3. `code-mower plan from-issue ...` turns issue text into a local plan.
4. `code-mower work-order draft ...` creates an implementation contract with
   role/lens sections.
5. `code-mower work-order critique-plan ...` creates prompt packets for other
   agents to improve the plan before implementation.
6. `code-mower work-order builder-experiment ...` seeds a builder experiment
   from that same contract.

This keeps the authoring loop measurable without making Code Mower a mandatory
agent orchestrator. The work order is the contract; the builder experiment is
the measurement scaffold; the audit protocol remains the merge gate.

Next implementation steps:

1. Add a thin authoring-run capture wrapper that writes builder result JSON.
2. Add cost fields for builder sessions where provider output exposes spend.
3. Feed builder reports into the cloud benchmark bundle.
4. Compare builder plus reviewer loops by task class and context pack.
5. Use verified results to choose repo defaults for high-velocity development.
