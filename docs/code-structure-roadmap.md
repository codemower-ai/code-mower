# Code Structure Roadmap

Code Mower began as tooling extracted from product repositories. The package is
now the source of truth, but several orchestration modules have grown as the
product added sessions, context, provider campaigns, Jira, and Board support.
This page records the current contributor-facing structure work. Runtime
behavior is described in [Architecture](architecture.md).

## Current Shape

The CLI is the primary public API. Internal package seams already isolate much
of the domain behavior:

- `calibration/` owns corpora, evidence, metrics, policy, and reports;
- `doctor_checks/` owns runtime, provider, GitHub, tracker, cloud, and privacy
  diagnostics;
- `provider_runners/` owns shared checkout, process, verdict, and GitHub
  mechanics;
- `cloud_client/` owns bundle validation, redaction, upload, and cloud setup;
- `providers/` and `lane_configs/` own provider metadata and lane declarations;
  and
- package and migration helpers own generated files, setup drift, rehearsals,
  and mirror-removal support.

Some root modules are still large. Approximate sizes at v1.3.1 are useful as
orientation, not as an API promise:

| Module | Lines | Main responsibility |
| --- | ---: | --- |
| `release_campaigns.py` | 8,100 | persistent multi-provider qualification campaigns |
| `init.py` | 3,450 | setup planning, participant selection, generated package materialization |
| `codex_audit_pr.py` | 2,700 | Codex audit transport and verdict lifecycle |
| `board.py` | 2,500 | local Board snapshots, history, process state, and server |
| `jira_mutations.py` | 2,450 | bounded Jira write planning and execution |
| `claude_audit_pr.py` | 2,050 | Claude audit transport and verdict lifecycle |
| `work_orders.py` | 1,850 | plan-to-delivery contracts and metadata |
| `jira_cloud.py` | 1,750 | Jira reads, mapping, and readiness |
| `campaign_adapters.py` | 1,600 | provider-specific qualification dispatch and recovery |
| `devin_cli_audit_pr.py` | 1,450 | local Devin audit transport |

Large modules are an onboarding and change-isolation risk. Their size alone is
not a reason for a broad rewrite; extraction should follow tested domain seams
and preserve the CLI and artifact contracts.

## Public API Direction

Keep the stable user surface CLI-first:

```text
code-mower init --easy
code-mower doctor --adoption --repo OWNER/REPO
code-mower session ...
code-mower lanes status --repo OWNER/REPO
code-mower calibration ...
code-mower cloud ...
```

`cli.py` uses a command registry so parsing and dispatch have one source of
truth. Importable Python APIs should be introduced only for concrete embedders
and kept smaller than the corresponding CLI surface.

## Recommended Refactor Order

1. **Release campaigns.** Split storage and locking, campaign state changes,
   provider dispatch, watch/recovery, qualification evaluation, and cloud event
   rendering. Keep one compatibility command adapter and preserve stored
   campaign formats.
2. **Setup generation.** Separate participant/profile decisions from repository
   inspection and file rendering. This makes first-run changes easier to test
   without loading every generated template.
3. **Audit runners.** Move repeated PR-head validation, isolated checkout,
   subprocess lifecycle, verdict validation, comment posting, and cleanup into
   shared runner primitives. Provider modules should mainly describe auth,
   command construction, output parsing, and provider-specific limits.
4. **Board.** Separate snapshot collection, history/event persistence, process
   liveness, presentation models, and HTTP serving.
5. **Jira.** Keep read models, write planning, mutation execution, and recovery
   distinct. Preserve dry-run-first writes and idempotency contracts.
6. **Work orders and sessions.** Share participant, task, delivery, and
   provenance types where they already describe the same concept. Avoid a new
   umbrella abstraction until it removes a demonstrated mismatch.

## Provider Consistency Rule

Every provider should map to the same role-level lifecycle where the product
supports it: readiness, dispatch, progress, clarification, cancellation,
result, recovery, and provenance. Provider adapters may implement that
lifecycle through a local CLI, hosted API, GitHub app, or manual handoff.

Do not force vendor mechanics into a false common denominator. Keep OAuth,
sandbox flags, session identifiers, rate limits, webhook behavior, and output
parsers in the provider adapter. Promote a provider to a peer role only after
the relevant lifecycle is qualified.

## Completion Criteria For Each Slice

A structural change is complete when:

- the user-facing CLI and artifact schema remain compatible or have an explicit
  migration;
- new seams have focused behavior tests;
- privacy and current-head review guarantees remain enforced;
- the full unit, Ruff, privacy, easy-mode, and package rehearsal checks pass;
  and
- this roadmap and [Architecture](architecture.md) still describe the resulting
  layout accurately.

Prefer bounded extractions tied to product work. They are easier to review and
less likely to destabilize a provider or release path than a package-wide
rewrite.
