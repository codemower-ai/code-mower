# Code Structure Roadmap

Code Mower started as extracted tooling from real product repositories. That
gave it useful battle scars, but the public OSS package should increasingly
look like an intentionally designed product rather than a bundle of scripts.

This document tracks the structure hardening path.

## Current Shape

The package is usable and has public release gates, but several modules are too
large for comfortable contributor onboarding:

| Module | Approximate Lines | Current Responsibility |
| --- | ---: | --- |
| `code_mower_calibration.py` | 3,150 | corpus parsing, evidence, metrics glue, policy, value reports |
| `doctor.py` | 2,550 | runtime checks, provider probes, GitHub diagnostics, cloud checks |
| `codex_audit_pr.py` | 1,920 | Codex audit wrapper, diff prep, subprocess isolation, verdict posting |
| `package.py` | 1,710 | extraction package generation, template copying, manifest validation |
| `cloud.py` | 1,500 | cloud export, upload, setup, token handling, API payloads |
| `migration.py` | 1,160 | wrapper migration, rehearsal, mirror-removal planning |
| `claude_audit_pr.py` | 1,130 | Claude audit wrapper, budget handling, verdict posting |

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

## Structural Progress

The first hardening slice keeps the public API CLI-first while introducing
tested internal seams:

- `code_mower.calibration` now owns corpus parsing helpers, artifact identity,
  evidence disposition constants, metric normalization, and lane-promotion
  thresholds. `code_mower_calibration.py` remains the backwards-compatible
  command adapter.
- `code_mower.doctor_checks` now owns doctor result models and the named check
  groups: runtime, GitHub, providers, cloud, and output.
- `code_mower.provider_runners` now owns shared GitHub token resolution for
  stdin-safe audit wrappers and local CLI lanes.
- `code_mower.cloud_client` now owns cloud endpoint probing plus bundle schema
  and privacy metadata. `cloud.py` remains the CLI adapter for export, doctor,
  setup, and upload.

These are intentionally package seams, not a full rewrite. The next slices can
move larger chunks of implementation behind those seams without breaking
existing commands.

## Recommended Refactor Order

1. **Calibration package split**
   - Move corpus parsing and truth models to `code_mower/calibration/corpus.py`
     and related modules.
   - Move evidence/metrics/policy math to separate modules.
   - Keep pulling value-report rendering into a smaller reporting module once
     the current metrics path has more tests.
   - Keep `code_mower_calibration.py` as a thin backwards-compatible command
     adapter until v1.0.

2. **Doctor check registry**
   - Split checks into registry-backed modules for runtime, GitHub, providers,
     cloud, and output.
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
   - Split metadata bundle creation from network upload.
   - Keep source/diff/transcript exclusion rules near the bundle schema.
   - Expose a small `create_bundle(...)` primitive for tests and future UI
     integrations.

5. **Package/migration boundary**
   - Separate template rendering from migration policy.
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
- `ruff`, privacy scan, unit tests, easy-mode smoke, and fresh-clone rehearsal
  stay green after each structural slice.
