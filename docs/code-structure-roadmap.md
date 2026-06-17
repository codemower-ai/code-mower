# Code Structure Roadmap

Code Mower started as extracted tooling from real product repositories. That
gave it useful battle scars, but the public OSS package should increasingly
look like an intentionally designed product rather than a bundle of scripts.

This document tracks the structure hardening path.

## Current Shape

The package is usable and has public release gates. The first calibration split
is largely complete, and the doctor check registry now contains the first real
runtime/cloud check implementations. Several modules are still large enough to
slow contributor onboarding:

| Module | Approximate Lines | Current Responsibility |
| --- | ---: | --- |
| `codex_audit_pr.py` | 1,917 | Codex audit wrapper, diff prep, subprocess isolation, verdict posting |
| `local_llm_audit_pr.py` | 1,351 | local model audit wrapper, prompt setup, subprocess isolation |
| `claude_audit_pr.py` | 1,130 | Claude audit wrapper, budget handling, verdict posting |

The calibration adapter is no longer on this list: `code_mower_calibration.py`
is now roughly 600 lines and delegates most domain behavior to
`code_mower.calibration`.
`doctor.py` is no longer on this list either: it is now roughly 200 lines and
acts as a backwards-compatible CLI adapter around `code_mower.doctor_checks`.
`cloud.py` has also dropped off this list: it is now roughly 680 lines and acts
as a backwards-compatible CLI adapter around `code_mower.cloud_client`.
`package.py` has also dropped off this list: it is now roughly 670 lines and
acts as a materializer adapter around package manifest, content, static,
rendering, and path helper modules.
`migration.py` has also dropped off this list: package-install rehearsal and
first-user readiness scoring now live under `code_mower.migration_rehearsal`,
and mirror-removal planning plus runner aliases now live under
`code_mower.migration_mirror`. `migration.py` remains a smaller migration
CLI/compatibility adapter.
The GitHub doctor checks also now keep redacted `gh api` helpers in
`code_mower.doctor_checks.github_api` and Actions billing/cost probes in
`code_mower.doctor_checks.github_actions`, leaving `github.py` focused on
repository-level setup orchestration.

These are not urgent correctness problems. They are readability and evolution
risks: new contributors cannot quickly tell which functions are stable API,
which are command plumbing, and which are legacy compatibility paths.

## Public API Direction

The CLI should remain the primary user API:

```text
code-mower init --easy
code-mower doctor --preflight
code-mower calibration ...
code-mower cloud ...
```

Internally, command routing should have one source of truth. `cli.py` now uses
`COMMAND_HANDLERS` so adding a command means registering it once and testing the
registry. Keep moving command-specific argument parsing and orchestration into
small command modules instead of growing `cli.py`.

Python import APIs should stay conservative until v1.0. The stable importable
surface should eventually be small:

- configuration loading and validation;
- provider/lane registry inspection;
- calibration corpus and value-report primitives;
- cloud metadata bundle creation;
- doctor check primitives for embedders.

Everything else can remain CLI-first.

## Senior-Engineer Readability Goal

The structure goal for v1.0 is that a new contributor can answer four questions
quickly:

1. where command parsing ends and domain logic begins;
2. where provider-specific behavior belongs;
3. where privacy-sensitive cloud bundle decisions are enforced; and
4. which modules are stable seams versus compatibility adapters.

That is why the next refactors should prefer small packages with clear names
over broad rewrites. A module can stay large temporarily if it is behind a
tested seam and docs call out the planned split.

## Structural Progress

The first hardening slice keeps the public API CLI-first while introducing
tested internal seams:

- `code_mower.calibration` now owns corpus parsing helpers, artifact identity,
  evidence disposition constants, metric normalization, the built-in
  calibration arm catalog, reviewer run-status normalization,
  lane-promotion thresholds, lane-policy report construction, value-report
  rendering, command materialization, context-pack input materialization,
  run-result normalization, and calibration command execution.
  `code_mower_calibration.py` remains the backwards-compatible command
  adapter.
- `code_mower.doctor_checks` now owns doctor result models, named check groups,
  runtime/toolchain checks, optional cloud-token checks, GitHub/provider/Actions
  diagnostics, human-readable output rendering, first-run presets, and
  package-aware config/template path resolution. Provider diagnostics are split
  into token/env checks, local CLI discovery/probes, API-model probes, and a
  thin provider catalog/runtime orchestrator. GitHub API and Actions-cost
  internals are split into `doctor_checks.github_api` and
  `doctor_checks.github_actions`. It also owns doctor report orchestration
  through `code_mower.doctor_checks.runner`; `doctor.py` remains the
  backwards-compatible CLI adapter.
- `code_mower.provider_runners` now owns shared GitHub token resolution for
  stdin-safe audit wrappers and local CLI lanes.
- `code_mower.cloud_client` now owns cloud endpoint probing, cloud doctor
  diagnostics, bundle schema and privacy metadata, bundle materialization,
  dogfood report discovery, dry-run preview shape, upload payload construction,
  network posting, local cloud setup/token handling, structured event/repo
  helper logic, and dogfood/catch-up/reviewer-run/repo-sync orchestration.
  `cloud.py` remains the CLI adapter for export, doctor, setup, dogfood,
  repo-sync, and upload.
- `code_mower.package_manifest`, `code_mower.package_content`,
  `code_mower.package_static`, `code_mower.package_rendering`, and
  `code_mower.package_paths` now own package file manifests, generated content
  builders, static generated file bodies, YAML/provider-catalog rendering, CLI
  command inventory, and provider-template path resolution. `package.py`
  remains the materializer adapter for package plans and output writes.
- `code_mower.migration_rehearsal` now owns package-install rehearsal,
  first-user readiness scorecards, and clean-venv/toy-repo rehearsal helpers.
- `code_mower.migration_mirror` now owns mirror-removal planning, workflow
  dependency detection, local fallback detection, and runner-alias reporting.
  `migration.py` remains the compatibility adapter for migration subcommands,
  wrapper comparisons, release-readiness routing, mirror planning, and
  package-install orchestration.
- `builder-experiment` and authoring-intelligence docs establish the future
  `run_role`/`purpose` event shape without requiring a full orchestrator runtime
  before v1.0.

These are intentionally package seams, not a full rewrite. The next slices can
move larger chunks of implementation behind those seams without breaking
existing commands.

## Recommended Refactor Order

1. **Calibration package split**
   - Completed: corpus parsing, truth models, evidence constants,
     metric helpers, lane-policy math, experiment arms, run-status
     categorization, command materialization, context-pack inputs,
     run-result normalization, report rendering, and runner orchestration now
     live under `code_mower.calibration`.
   - Keep `code_mower_calibration.py` as the backwards-compatible CLI adapter
     until v1.0.
   - Next calibration cleanup should be small: reduce repeated import
     compatibility plumbing only if it clarifies contributor onboarding without
     breaking direct-script users.

2. **Doctor check registry**
   - Completed: result models, group registry, runtime/toolchain checks, and
     optional cloud-token checks now live under `code_mower.doctor_checks`.
   - GitHub repository diagnostics, provider token/env checks, local CLI
     discovery/probes, API-model probes, and Actions cost checks now also live
     behind that seam.
   - Next: move the remaining output/privacy checks behind the same registry
     seam.
   - Keep `doctor --preflight` behavior unchanged.
   - Add tests at the check-result level, not only command-output level.

3. **Provider runner base**
   - Extract shared audit-wrapper primitives from Codex, Claude, Gemini,
     Antigravity, Hermes, and local LLM wrappers:
     diff context, subprocess execution, token handling, verdict artifacts,
     and PR comment posting.
   - Provider modules should mostly describe provider-specific commands and
     output parsing.

4. **Cloud client package**
   - Completed: metadata bundle materialization is separate from network
     upload. Cloud setup/token handling, structured event/repo helpers, cloud
     doctor diagnostics, and dogfood/catch-up/reviewer-run/repo-sync
     orchestration now live under `code_mower.cloud_client`.
   - Keep source/diff/transcript exclusion rules near the bundle schema.
   - `build_cloud_bundle(...)` is the current small bundle primitive for tests
     and future UI integrations.
   - Next: reduce remaining import-compatibility plumbing only where it makes
     first-read comprehension better; the main cloud domain logic now has a
     tested package seam.

5. **Builder experiment primitives**
   - Normalize `run_role`/`purpose`, task contract identity, provider/lens,
     worktree/branch, PR, elapsed time, intervention counts, blocker
     iterations, checks, merge result, post-merge health, and known cost.
   - Keep these primitives source-free by default so they can feed local
     reports and optional cloud events.
   - Do not add a full orchestrator dependency until the metadata contract is
     useful from manual and semi-manual runs.

6. **Package/migration boundary**
   - Completed: package-install rehearsal and first-user readiness scoring now
     live under `code_mower.migration_rehearsal`.
   - Completed: mirror-removal planning and runner-alias reporting now live
     under `code_mower.migration_mirror`.
   - Next: keep product-repo wrapper support as command plumbing unless a
     concrete external API consumer needs import-level stability.
   - Product-repo wrapper support should read like a compatibility layer, not
     the center of the project.

## API Simplification Candidates

- Prefer one friendly first-run command: `doctor --preflight`.
- Keep versioned aliases such as `doctor --v05` for scripts, but docs should
  lead with the human name.
- Make `init --easy`, `doctor --preflight`, `calibration value-report`, and
  `cloud upload --dry-run` the golden path.
- Group experimental lanes behind clear names and docs rather than promoting
  every provider wrapper equally.
- Add `code-mower demo` or `code-mower first-run` only if it removes actual
  first-user friction; avoid another umbrella command until evidence supports it.

## Definition Of Done

Before v1.0, the codebase should satisfy:

- no core module above roughly 1,500 lines unless it is mostly data/templates;
- command registry remains tested as the CLI source of truth;
- calibration and doctor have small tested submodules;
- provider wrappers share common audit-runner primitives;
- public docs explain the package layout;
- builder-experiment metadata has a small source-free model before any
  orchestrator adapter is promoted;
- `ruff`, privacy scan, unit tests, easy-mode smoke, and fresh-clone rehearsal
  stay green after each structural slice.
