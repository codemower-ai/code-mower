# Operational acceptance evidence

`code-mower release evidence` reads a closed local observation record and reports
implementation completion, writer quiescence, reviewed head, published-package
inclusion, usage settlement, stored metadata, and aggregate visibility separately.
It performs no provider, GitHub, package, or cloud operations. Its result grants no
review or merge authority and is not a cloud event.

The operator or a maintained adapter records facts from the actual source. This
reader validates types, bindings, coverage, and freshness; it does not authenticate
an arbitrary file's claims or replace independent audits, provider reads, published
artifact inspection, or an authenticated hosted view. Keep the record and its
private evidence outside the repository and out of cloud bundles.

## Run the supported report

```bash
code-mower release evidence --input acceptance.json --json
code-mower release evidence --input acceptance.json \
  --require implementation --require provider_quiescence \
  --require reviewed_head --require published_package \
  --require ingestion_storage --require aggregate_visibility
code-mower doctor --operational-evidence acceptance.json --json
```

The first command reports all observations; exit zero means the record was valid,
not that every check passed. Repeat `--require` for the acceptance conditions of
the current operation. A required non-passing observation exits 1. An invalid,
missing, oversized, symlinked, or non-regular input exits 2 with one closed
`operational_evidence_invalid` diagnostic. The reader bounds the file to 64 KiB.
Doctor includes the same independent checks in its usual report; unknown evidence
is a warning, not green. `doctor --strict` also rejects warnings.

Unknown billing need not prevent release indefinitely. Require `usage_settlement`
only when settled billing itself is the acceptance condition; preserve unavailable
amounts explicitly in the release scorecard. Never convert an unsettled observed
zero to settled zero dollars.

## Local record contract

The top-level object has exactly these fields:

- `schema`: `code_mower.operationalEvidence.v1`.
- `binding`: a lowercase 64-character digest of the exact private delivery binding.
  Every observation repeats this binding; do not use a provider reference or path.
- `head`: the exact lowercase 40-character implementation/review commit.
- `release_commit`: the exact lowercase 40-character release commit, or null while
  package inclusion remains unverified.
- `observations`: any subset of the seven observation objects below. Missing
  objects remain unavailable; they are never filled from another object's status.

Every supplied observation has `source`, `observed_at` (an ISO timestamp with an
explicit UTC offset, not in the future), `coverage` (`complete`, `partial`, or
`unavailable`), and `binding`, plus all fields in its row. Unknown fields, duplicate
JSON keys, invalid types, non-finite/negative amounts, and mismatched bindings fail
closed. The record contains no arbitrary narrative fields.

| Observation | Source | Additional fields |
| --- | --- | --- |
| `implementation` | `work_order` or `local_delivery` | `head`; `state`: `active`, `complete`, `failed`, `user_cancelled_before_delivery`, or `unknown` |
| `provider` | `provider_read` or `local_supervisor` | `state`: `active`, `exited`, `suspended`, or `unknown`; `cancellation`: `not_requested`, `requested`, `accepted`, or `unknown` |
| `review` | `code_mower_audit` | `head`; `eligible` boolean; `verdict`, `ci`, and authoritative `gate`, each `pass`, `blocked`, or `unknown` |
| `package` | `published_package_inspection` | `head`, `release_commit`, `artifact_sha256`, `contains_head` and `published` booleans |
| `usage` | `provider_usage` or `provider_billing` | `authorized_acu_cap`, `observed_acu`, `settled_acu`, `settled_usd`, each a nonnegative finite number or null |
| `ingestion` | `cloud_receipt` | `manifest_sha256`, `stored` boolean, nonnegative integer `accepted_events` and `reports` |
| `aggregate` | `authenticated_view` | matching `manifest_sha256`; `state`: `fresh`, `stale`, `failed`, or `unknown`; `visible` boolean |

All head values must match the top-level head. Package commit must match the
explicit top-level release commit; its SHA-256 identifies the inspected artifact.
`contains_head` records the independently verified inclusion of that implementation
in the published artifact, not a guess from a version string. A main-branch merge
alone leaves package evidence missing. Hashes use lowercase hexadecimal.

Provider quiescence and aggregate visibility require complete source coverage and
an observation no more than 300 seconds old. A failed refresh preserves a dated
last observation; it cannot be relabeled fresh. All other observations retain their
source time/coverage as historical evidence bound to the immutable delivery/head.
A fresh source cannot hide a stale or missing source.

Use raw provider/supervisor writer evidence for `provider.state`. Logical work-order
completion and structured-output readiness do not establish writer exit. A recent
suspension may establish quiescence for the existing takeover policy; it does not
claim termination or implementation success. Cancellation accepted is still
separate from observed exit. An owner cancellation before delivery remains distinct
from a completed failed implementation.

Settled amounts require `source=provider_billing` and complete coverage; a usage
snapshot can supply only observed usage. Record authorized caps independently of
usage. Never derive dollars from an ACU cap or fabricate a rate or settlement.

A stored receipt passes metadata storage only when it records positive accepted
event count, complete coverage, and zero reports. Fresh aggregate acceptance also
requires an independently observed authenticated view, the same manifest, and a
passing storage check. An HTTP success or `aggregateRefreshStatus=stale` receipt
is insufficient. Investigate the existing receipt/view; do not resubmit an already
accepted bundle to manufacture a freshness result. New Slack/cloud fields remain
owned by their separate versioned contract; this local record adds none.

## Correctly keyed recovery operations

Use a short stable request key and a separate prose input file. The supported
remote-session CLI uses `--request` for message/cancel idempotency and `--input-file`
for message prose; inspect its subcommand help for the exact session binding.
Exact PR-bound trusted work orders use the maintained embedding API documented in
[Devin work orders](devin-work-orders.md). They are not an invented work-order CLI.

Missing, blank, or overlong request keys fail input validation before a remote
mutation. Earlier malformed requests do not establish that a completed provider
session refuses a correctly formed message or cancellation. Reconcile uncertain
outcomes under the same private binding and key; never automatically create a new
session or spend a new recovery allowance.

## Local audit publication evidence

For Claude/Codex workflow publication, record the source local-audit run ID,
attempt, matching lane job and completed reviewer-seal step, as well as the
local artifact's canonical
metadata digest, PR/head, publisher run URL, created comment ID and terminal run
conclusion. The public comment contains only allowlisted verdict metadata and
existing lane/run trailers. A successful receipt job binds the exact metadata
digest and comment ID; the existing gate and labeler verify that receipt through
the Actions API. Neither a rendered trailer alone nor dispatch acceptance is a
publication success.

Check that the completed publisher wakes the matching labeler, moves `needs-*`
to the correct `*-audit-done` or `*-audit-blocked` label, and dispatches the gate
for the current head. Retain failed/neutral reservations: they prevent ambiguous
retries from becoming a second signal. If a head moved, request a fresh audit at
the new head. If delivery timed out, inspect the existing run rather than blindly
reposting. No source, diff, prompt, transcript, local path, private repository name
or raw provider output belongs in public evidence. See the
[publication operations contract](local-audit-runner.md#verified-workflow-publication).
