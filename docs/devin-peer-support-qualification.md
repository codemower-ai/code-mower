# Devin peer-support qualification

Decision record for [#911](https://github.com/codemower-ai/code-mower/issues/911),
part of [#901](https://github.com/codemower-ai/code-mower/issues/901). It
collects end-to-end evidence for Devin as a supervised builder and as an
informational reviewer across lifecycle, context, delivery, review, recovery,
setup, and cost. It states exactly what is proven and nothing more.

Devin is the named builder for this qualification PR. The trusted orchestrator
owns private-canary execution, cloud upload, scorecard updates, independent
reviews, and merge.

## What this qualification separates

| Distinction | Kept separate as |
| --- | --- |
| Local CLI builder evidence vs. hosted API builder evidence | Five bounded local Devin CLI samples in one table; two hosted v3 work orders in a second table. They are never combined into one aggregate. |
| Transport and lifecycle parity vs. live model quality | Synthetic reviewer controls and the offline synthetic protected-path context canary prove parsing, schema, lifecycle, and severity normalization. They do not measure live Devin reviewer accuracy, live private-context retrieval, or context relevance. |
| Informational Devin review adapters vs. merge-authority reviewers | Both `devin_cli` and `devin_api_v3` report `merge_authority=false`. Codex audit and Claude audit remain the only merge-authority lanes. |
| Public event wall time vs. active provider time | Local rows report active builder seconds. Hosted rows report public wall time from PR creation to merge; active provider time is unavailable. |
| Known ACU/cost values vs. unavailable values | Caps and observed ACU are recorded where the transport returned them. Tokens, monetary cost, and local ACU are reported as unavailable, never as zero. |

Nothing in this document claims a measured productivity lift, broad model
accuracy, or Devin reviewer promotion. Both Devin review transports remain
informational unless a separate calibration and policy change earns promotion.
The Claude + Codex first-run default is unchanged.

## Evidence baseline

- Dispatched from a `main` containing #904 through #909 plus the recovery
  corrections for issues #936, #938, #941, and #943 (merged as PRs #937,
  #939, #942, and #944). Round 1 incorporated `origin/main` at
  `cd26b9a569d3d765622db4804b0db65380dc155d`, which includes the Graphify
  PR #926 merged after the original dispatch.
- [#932](https://github.com/codemower-ai/code-mower/pull/932) is a verified
  hosted Devin builder delivery at `619638849b32308046342e0d839c58f80ca99edd`,
  merged as `da475b64a7696b5780b78d54aa61dbfbbc9d1e41`.
- [#940](https://github.com/codemower-ai/code-mower/pull/940) /
  [#910](https://github.com/codemower-ai/code-mower/issues/910) merged through
  dual exact-head review and a green gate before this work order was
  dispatched. Its hosted fix rounds are supervised delivery evidence,
  including every stale PASS and BLOCKED cycle; they are not a first-pass
  success.
- The five local Devin deliveries in
  [#659](https://github.com/codemower-ai/code-mower/issues/659) remain the
  fixed builder baseline and are not replaced or combined with hosted samples.

## Local CLI builder deliveries (fixed baseline)

Local Devin CLI 3000.6.14, model `adaptive`, selected explicitly by the
runner. The local transport has no ACU metric; tokens and cost were
unavailable rather than zero. Time is active builder time. The task-class
descriptions in this table repeat already-public issue and PR titles; they are
allowed public documentation but are never cloud metadata.

| Sample | Task | Final PR head | Merge commit | Active time | Intervention | Review/fix rounds | Accepted findings | Result |
| --- | --- | --- | --- | ---: | --- | --- | --- | --- |
| 1 | [#740](https://github.com/codemower-ai/code-mower/issues/740) / [PR #759](https://github.com/codemower-ai/code-mower/pull/759), package-install command construction | `5a39e2464d12dcf66ccdeac4bb62cb6ed974387f` | `e5b946baa3215b17f29284e40f2602183427adc9` | 893s | One metadata-only PR-linkage correction; code unchanged | No CI retry; no peer-audit fix round; final Codex and Claude PASS | None | Gate passed; merged |
| 2 | [#741](https://github.com/codemower-ai/code-mower/issues/741) / [PR #760](https://github.com/codemower-ai/code-mower/pull/760), campaign rejection diagnostics | `425d58185f0106c558b22a30b0c9e3a2ddaea371` | `9f877a272c163b05047d99b01d34d72586cf274d` | 1,064s | No owner intervention | One CI correction and two peer-audit fix rounds; final Codex and Claude PASS | Two P2, accepted-fixed | Gate passed; merged |
| 3 | [#739](https://github.com/codemower-ai/code-mower/issues/739) / [PR #761](https://github.com/codemower-ai/code-mower/pull/761), structured-output schema compatibility | `e4918bbeaa54ff9545f4736944048294cfeeeddf` | `9b3beed328b017e45d7e50fe544c7f0d689f61b0` | 415s | No owner intervention | No CI retry; two peer-audit fix rounds; final Codex and Claude PASS | One P1 and one P2, accepted-fixed | Gate passed; merged |
| 4 | [#753](https://github.com/codemower-ai/code-mower/issues/753) / [PR #762](https://github.com/codemower-ai/code-mower/pull/762), strict-shell optional provider flags | `bb21322aa6233ca7cd2df0f28c09c05556cc8272` | `7ce48742819bc213475aec707adf87f602066182` | 550s | Orchestrator opened the PR from the unchanged Devin head after provider-local TLS/keyring failure | No CI retry; no code/audit fix round; final Codex and Claude PASS | None | Gate passed; merged |
| 5 | [#763](https://github.com/codemower-ai/code-mower/issues/763) / [PR #764](https://github.com/codemower-ai/code-mower/pull/764), issue-linked delivery and cold prompts | `66e30adf56c5b376120c8c931e401f7cebea753e` | `140136be4480a3ca19c4cf618f0b34b30242b371` | 701s | None | No CI retry; no audit fix round; final Codex and Claude PASS | None | Gate passed; merged |

Aggregate: 3,623 active seconds; median 701 seconds; approximately 725 seconds
mean; four accepted blocking findings (one P1, three P2) across four audit fix
rounds; one CI correction; two orchestration interventions; zero owner
interventions. ACU and cost: unavailable for every row. Every sample merged
only after an exact-head Codex PASS, Claude PASS, and green gate. These are
five small real issues in one repository, not a productivity or general
model-quality estimate.

## Hosted API builder deliveries and recovery

Hosted Devin v3 work orders through `code-mower devin work-order`. Time is
public wall time from PR creation to merge; active provider time is
unavailable. Monetary cost is unavailable for both rows.

| Work order | ACU cap | Observed ACU | Public wall time | Active time | Work-order round | Exact-head audit cycles | Intervention | Verified PR / head | Recovery result |
| --- | ---: | ---: | ---: | --- | ---: | --- | --- | --- | --- |
| [#932](https://github.com/codemower-ai/code-mower/pull/932) | 5 | 0.0 returned | 24,253s | Unavailable | 10 | Codex: four BLOCKED, two PASS (first PASS invalidated by a supplemental P2); final head same-head Codex and Claude PASS | Trusted-orchestrator intervention: supervised; orchestrator-side authorization/provenance, work-item binding, capability migration, collection, and bot-login corrections; no owner intervention recorded | `619638849b32308046342e0d839c58f80ca99edd`, merged `da475b64a7696b5780b78d54aa61dbfbbc9d1e41` | Recovered; no duplicate paid create |
| [#940](https://github.com/codemower-ai/code-mower/pull/940) / [#910](https://github.com/codemower-ai/code-mower/issues/910) | 4 | 0.0 returned | 14,184s | Unavailable | 14 | Codex: seven BLOCKED carrying twelve accepted P2; six intermediate Claude PASS became stale; final head same-head Codex PASS and Claude PASS with one nonblocking P3 advisory | Trusted-orchestrator intervention: supervised; fix rounds routed through the same work order plus six P2-level pre-audit corrections by the orchestrator; no owner intervention recorded | `965d002cc0c42519de8729e1e66ef0496ff3f0ac`, merged `d09e523895dd12abfa5775bd088f7d687d45d6bc` | Recovered; no duplicate paid create |

A returned observed ACU of `0.0` is recorded as the value the transport
returned, not as a measured cost of zero. Both are supervised hosted transport
and recovery evidence, not first-pass deliveries.

### #932 chronology

1. Hosted work order created with a 5 ACU cap; one paid create for the whole
   lifecycle.
2. Structured-completion `waiting_for_user` observation: the completion
   object arrived while the session still reported `waiting_for_user`, and
   collection was corrected to recognize it (#942).
3. Authorization and packet-provenance fixes.
4. Tracker-neutral work-item binding.
5. Legacy hosted-capability migration.
6. Post-merge collection and verification against the merged head.
7. Exact terminal bot-login matching for the `devin-ai-integration[bot]`
   author binding.
8. Stale-round recovery after a stale work-order completion was observed.
9. Formal Codex exact-head sequence: four BLOCKED verdicts and two PASS
   verdicts; a supplemental P2 invalidated the first PASS.
10. Final head `619638849b32308046342e0d839c58f80ca99edd` received same-head
    Codex and Claude PASS; merged as
    `da475b64a7696b5780b78d54aa61dbfbbc9d1e41`.
11. Verified round-10 collection bound the author, issue, PR, repository, and
    exact head; zero duplicate paid creates.

### #940 / #910 chronology

1. Hosted work order created with a 4 ACU cap in one session.
2. Seven exact-head Codex BLOCKED cycles carrying twelve accepted P2 findings.
   Six intermediate Claude PASS verdicts became stale when later independent
   review found blockers on changed heads; a stale PASS is not carried
   forward.
3. Accepted fixes covered executable and effective-lane resolution, profile
   pinning and global/profile transport consistency, credential-path privacy,
   observer posture, capability and lifecycle wording, generated-init
   installation semantics, and custom-lane behavior.
4. The trusted orchestrator found and corrected six additional P2-level issues
   during local pre-audit; these are separate from the twelve formal Codex
   findings.
5. Recovery and fix rounds reused the original paid create; no duplicate
   session creation.
6. Final head `965d002cc0c42519de8729e1e66ef0496ff3f0ac` received same-head
   Codex PASS and Claude PASS. The final Claude PASS carried one nonblocking
   P3 advisory; its disposition was recorded as advisory, not accepted as a
   blocker, and the PASS stands unchanged. `code-mower/gate`, the aggregate package job,
   and the Python 3.12, 3.13, and 3.14 package matrices passed; merged as
   `d09e523895dd12abfa5775bd088f7d687d45d6bc`.
7. Final verified collection was round 14 and bound the expected author,
   issue, PR, repository, and exact head.

On that final head, the focused setup, capability, next-step, remote-session,
work-order, review, participant, init, doctor, and privacy suites passed 226
tests plus 244 subtests; the full suite passed 3,228 tests, skipped 11, and
passed 2,121 subtests; Ruff, compilation, privacy, and diff checks were clean.

## Reviewer contract controls

`tests/test_devin_review.py::DevinReviewTests::test_calibration_transport_parity`
adjudicates four checked-in synthetic controls through both `devin_cli` and
`devin_api_v3`:

| Control | Truth | Required result | `devin_cli` | `devin_api_v3` |
| --- | --- | --- | --- | --- |
| `clean-empty` | Known clean, no findings | PASS | PASS | PASS |
| `clean-advisory` | Known clean, one P3 advisory | PASS | PASS | PASS |
| `blocked-auth` | Known blocked, one P1 authorization omission | BLOCKED | BLOCKED | BLOCKED |
| `blocked-null` | Known blocked, one P2 null dereference while the provider declares PASS | BLOCKED | BLOCKED | BLOCKED |

Contract agreement: 4/4 for each transport on the exact qualification head
(one focused test, four subtests, both transports). Both adapters report
`merge_authority=false`. The hosted control also confirmed preview-by-default
dispatch, a 1 ACU explicit limit, required structured output, and no summary
text in public metadata.

These are synthetic contract controls. They prove parser, schema, lifecycle,
and severity normalization parity between the two transports. They do not
measure live Devin reviewer accuracy or false-positive rate, which remain
unqualified, and they are not fitness evidence for promotion.

## Trusted-orchestrator context canary

The trusted orchestrator completed an offline synthetic protected-path canary
on `26135c62f191b171a5b88d0234eb82c306ece169` using protected temporary
state, fake authorization/retrieval/remote/GitHub seams, and zero external
provider calls. It exercises the protected code path only. It does not prove
live authenticated private-context retrieval and it does not measure context
relevance; both remain unqualified. Only these metadata outcomes are
published; the builder did not fetch, print, persist, or reconstruct any
private context, and no live private-context evidence is inferred here.

| Case | Sanitized outcome |
| --- | --- |
| Required context available | Running; policy `required`; dispatch `delivered`; one fake provider mutation; public metadata redacted; five protected state files checked |
| Required context unavailable | `UNKNOWN`; paused; reason `context_unavailable`; zero provider mutations; no work-order reservation |
| Explicit refresh and stale rejection | Two synthetic retrievals; packet identity and attachment revision changed; stale delivery rejected; replaced private feedback removed |
| Private-feedback return | Attachment succeeded; feedback returned through the guided path; session advanced to `reviewed`; public metadata redacted |
| No-context compatibility | Running; policy `none`; dispatch `omitted`; one fake provider mutation; legacy input shape preserved |

The corresponding focused packet, delivery, guided-session, and Devin
work-order suites passed 44 tests.

## Setup and package qualification

Setup posture on the qualification head:

- A clean no-Devin install preserves the Claude + Codex defaults, selects no
  Devin transport, and recommends the `codex` and `claude_audit` lanes.
- Opted-in local, hosted, both, unavailable, custom-command, custom-profile,
  and observer-posture cases are covered by the exact-head setup and doctor
  suites and produce consistent setup output, session guidance, doctor output,
  and remediation. Devin remains explicit opt-in.
- An isolated upgrade rehearsal extracted the v1.3.1 template, previewed and
  applied the current initializer, left source configuration unchanged, kept
  the generated configuration valid, and wrote 36 generated files without
  requiring Devin setup.

Package qualification: fresh wheel installs on Python 3.12, 3.13, and 3.14;
sdist and wheel Twine and package-content checks; base installation without
optional dependencies; easy-mode and fresh-clone setup; first-user package
rehearsal; release readiness; privacy, workflow, and package guards. These run
in the GitHub package matrix and `code-mower/gate` on the exact PR head, and
their result is recorded in the PR's status checks rather than copied here.

## Board and cloud evidence (pending, trusted orchestrator)

Exact-head Board inspection, cloud dry-run inspection, and the metadata-only
upload are trusted-orchestrator steps. They were pending when this document
was written and were not performed by the builder. When performed, Board and
cloud records for this qualification may contain only allowlisted provider,
transport, state, reason, timing, round, PR/head, ACU/cost, and validation
metadata; the public task-class descriptions above are documentation, not
cloud metadata. The cloud dry run is inspected before any upload.

## Limitations

- Local versus hosted: the local rows report active seconds and the hosted
  rows report public wall time; they are not comparable and are not combined.
- Provider-version drift: local samples used Devin CLI 3000.6.14 with model
  `adaptive`; hosted samples used the v3 API. Later provider versions may
  behave differently.
- Single-repository sampling: all evidence is self-dogfood from one
  repository under one orchestrator, with five local and two hosted samples.
- Incomplete cost visibility: tokens and monetary cost are unavailable
  everywhere; local ACU does not exist; hosted observed ACU is the returned
  `0.0`, not an audited spend.
- No measured productivity lift: no baseline comparison was performed.
- Live private-context retrieval unqualified: the canary is offline and
  synthetic; no live authenticated retrieval was exercised.
- Unmeasured context relevance: the canary proves protected-path lifecycle
  and privacy behavior, not the usefulness of delivered context.
- Informational reviewer authority: live Devin reviewer accuracy and
  false-positive rate are unqualified; both transports stay informational.

## Recommendation posture

Devin is supported as a supervised builder through both the local CLI and the
hosted v3 work-order transport, with recovery proven under stale-completion,
merge-boundary, and bot-login conditions. Devin review remains informational
on both transports. Reviewer merge authority stays with Codex audit and Claude
audit under the [lane promotion policy](lane-promotion-policy.md). The
adoption recommendation for #901 is recorded by the trusted orchestrator after
merge.
