# Architecture

Code Mower is a local-first CLI plus generated GitHub support files. It helps a
team set up AI reviewer lanes, run diagnostics, calibrate those lanes against
known PR outcomes, and optionally upload sanitized metadata to CodeMower.com.

## Core Concepts

- **Profile:** a named setup posture, such as easy-mode local/manual lanes.
- **Provider:** an adapter for a reviewer or coding system such as Codex,
  Claude, Gitar, Antigravity/Gemini, Hermes, CodeRabbit, Cursor BugBot, Qodo,
  Greptile, Devin, or a local LLM.
- **Lane:** a provider plus trigger policy, prompt/lens, and merge posture.
- **Lens:** a review doctrine that changes what a reviewer looks for, without
  changing the underlying provider.
- **Context pack:** a bounded set of surrounding files that can be supplied to
  reviewers when a diff alone is insufficient.
- **Calibration corpus:** known-clean, known-blocked, or subtle-risk PRs used
  to measure reviewer usefulness.
- **Value report:** a local report that compares useful findings, false
  positives, cost, latency, and lane recommendations.
- **Cloud bundle:** an inspectable metadata-only export that can optionally be
  uploaded to CodeMower.com.

## Package Layout

```text
src/code_mower/
  cli.py                         command routing
  init.py                        easy-mode generated setup
  doctor.py                      runtime, provider, GitHub, cost, cloud checks
  provider_registry.py           provider metadata and posture
  prompts.py                     lane prompt loading
  reviewer_metrics.py            reviewer value/report calculations
  cloud.py                       export, doctor, upload, dogfood commands
  migration.py                   package install and mirror-removal rehearsals
  *_audit_pr.py                  provider-specific audit runners
  adapters/                      hosted/SaaS adapter helpers
  lane_configs/                  provider lane declarations
  templates/                     generated config, workflows, prompts, support
tests/                           unit and release-hygiene tests
scripts/                         smoke, privacy, fresh-clone, Python wrapper
docs/                            public setup, privacy, roadmap, release docs
```

The package intentionally keeps provider-specific behavior in adapters and lane
configs. Generic orchestration should not know provider-specific auth quirks
unless they are part of the declared provider contract.

## First-Run Flow

```mermaid
flowchart TD
  A["Install Code Mower"] --> B["code-mower init --easy"]
  B --> C["Generate .code-mower.generated"]
  C --> D["code-mower doctor --v05"]
  D --> E["Run local/manual audits"]
  E --> F["Build calibration corpus"]
  F --> G["Generate reviewer value report"]
  G --> H{"Opt into cloud?"}
  H -->|No| I["Use local reports"]
  H -->|Yes| J["Export metadata-only bundle"]
  J --> K["Dry-run upload"]
  K --> L["Upload with team token"]
```

## Provider And Lane Posture

Code Mower starts conservative:

- local structured audits first;
- hosted reviewers informational until calibrated;
- no recurring schedules by default;
- no merge-gating lane until that repo's data supports it; and
- no cloud upload unless explicitly configured.

Provider integrations should expose setup docs, auth/runtime doctor checks,
source/diff exposure posture, local/hosted/manual/automatic posture, and
cost/latency fields when available.

## Generated Product Support

`code-mower init --easy --apply` writes generated support files into a target
directory. Product repositories should treat generated files as reviewable
configuration and thin wrappers, not as a fork of the implementation.

The long-term rule is: product repos consume a pinned package version and keep
only product-specific config/support files.

## Cloud Boundary

The OSS package can export and upload a cloud bundle, but the hosted service is
optional. Default bundles exclude source code, raw diffs, raw model
transcripts, raw stdout/stderr, auth output, and secrets.

See `docs/cloud-data-contract.md` for the public upload contract.

## Release Hygiene

Before a public alpha promotion, run:

```bash
scripts/dev-python -m venv .venv
.venv/bin/python -m pip install -e . ruff
.venv/bin/python -m ruff check .
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m pytest -q
.venv/bin/python scripts/privacy_scan.py
.venv/bin/python scripts/smoke_easy_mode.py --code-mower-bin .venv/bin/code-mower --json
.venv/bin/python scripts/fresh_clone_rehearsal.py --repo-url . --ref HEAD --python .venv/bin/python --json
git diff --check
```

`scripts/dev-python` is the preferred source-checkout Python entrypoint. It
refuses stale or old Python interpreters so release work does not accidentally
run under an unsafe ambient `python3`.
