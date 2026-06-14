# Code Mower Builder Experiments

Code Mower builder experiments measure the authoring side of the AI delivery
loop: which builder, prompt doctrine, and reviewer set delivers the best merged
result on a real codebase.

Reviewer calibration answers which reviewers catch useful defects. Builder
experiments answer which peer-programmer loop can safely carry a task from
agreed direction to merged, verified code with the least waste.

The important distinction for v1.0: this is a measurement harness, not a
requirement to adopt a full orchestrator. Manual Codex, Claude, Devin, or other
agent sessions can write the same result artifacts as an automated runner.

## What To Measure

A builder experiment should record:

- run role or purpose, usually `implement`
- task contract, task class, and repository
- builder provider, tool, model, and prompt lenses
- context packs used
- branch and worktree identity
- pull request URL or number when created
- elapsed wall time
- user interventions
- audit blocker iterations
- tests and checks run
- merge result and post-merge health
- known cost where the provider exposes it

The first implementation is intentionally harness-only. It plans runs and
reports results, but it does not run autonomous authoring itself. That keeps the
measurement surface useful while preserving the normal Code Mower merge bar.

## Spec Example

```json
{
  "version": 1,
  "name": "starter-builder-loop",
  "description": "Compare two builder loops on one small package task.",
  "tasks": [
    {
      "task_id": "package-doctor-check",
      "run_role": "implement",
      "repo": "owner/repo",
      "base_ref": "origin/main",
      "task_class": "package-runtime",
      "prompt": "Add one provider/auth check to code-mower doctor.",
      "success_criteria": [
        "focused tests pass",
        "full Code Mower tests pass",
        "post-merge health is verified"
      ],
      "context_packs": ["package-runtime"],
      "review_classes": ["package-runtime"]
    }
  ],
  "builders": [
    {
      "builder_id": "codex-base",
      "provider": "codex",
      "tool": "codex-desktop",
      "prompt_lenses": ["base-audit"],
      "cost_policy": "provider-dependent"
    },
    {
      "builder_id": "codex-generic-programming",
      "provider": "codex",
      "tool": "codex-desktop",
      "prompt_lenses": ["generic-programming"],
      "cost_policy": "provider-dependent"
    }
  ],
  "metrics": [
    "elapsed_seconds",
    "cost_usd",
    "user_interventions",
    "audit_blockers",
    "resolved_blockers",
    "tests_passed",
    "post_merge_health"
  ]
}
```

Plan the experiment:

```bash
code-mower builder-experiment plan tools/builder_experiment.example.json --json
```

Write a plan artifact:

```bash
code-mower builder-experiment plan tools/builder_experiment.example.json \
  --output .code-mower/builder-experiment-plan.json \
  --json
```

## Run Result Example

Builder run results can be written by an agent or by a thin wrapper around the
authoring session:

```json
{
  "runs": [
    {
      "run_id": "starter-builder-loop-abc123-package-doctor-check-codex-base-r1",
      "run_role": "implement",
      "status": "verified",
      "branch": "codex/package-doctor-check",
      "worktree": "/tmp/code-mower-runs/package-doctor-check",
      "pull_request": "https://github.com/owner/repo/pull/123",
      "elapsed_seconds": 3600,
      "cost_usd": 4.25,
      "user_interventions": 1,
      "audit_blockers": 2,
      "resolved_blockers": 2,
      "post_merge_health": "verified"
    }
  ]
}
```

Generate the report:

```bash
code-mower builder-experiment report builder-experiment.json \
  --runs builder-results.json \
  --output builder-experiment-report.md
```

## Guardrails

- Use a fresh worktree for every builder run.
- Keep reviewer output hidden until the builder declares the run complete.
- Do not share patches between experiment arms.
- Review through diff plus task contract. Do not require reviewers to inspect
  the builder's raw transcript.
- Record interventions honestly; they are part of velocity measurement.
- Merge only through the normal Code Mower audit protocol.
- Count a delivery as successful only after post-merge health is verified.
- Keep source code, raw diffs, transcripts, stdout/stderr, auth output, and
  secrets out of cloud-bound result artifacts by default.

## Orchestrator Adapter Posture

Systems that manage agent sessions, worktrees, policies, or sub-agents can be
valuable future adapters. They should feed Code Mower's measurement contract
instead of replacing it. A good adapter reports task contract, branch/worktree,
provider, lens, elapsed time, spend, audit iterations, and merge health while
leaving source and credentials local.

Until the manual builder-experiment path is useful, orchestrator integrations
should stay experimental and disabled by default.

## How This Connects To Lenses

Lens calibration should come first because it is cheaper and already executable
through reviewer lanes. Builder experiments come next: once a lens shows useful
review signal, use the same doctrine as a builder prompt lens and measure
whether it improves delivery quality, speed, or cost.
