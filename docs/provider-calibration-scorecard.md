# Provider Calibration Scorecard

Decision output for the 20-sample builder calibration corpus tracked in
[#659](https://github.com/codemower-ai/code-mower/issues/659): five bounded
real issues each for Cursor, Muse, Antigravity, and Devin. This scorecard
resolves the publication task in
[#765](https://github.com/codemower-ai/code-mower/issues/765).

## Methodology

Each provider completed five bounded real backlog issues, balanced across
task classes where practical: test/fixture hardening, CLI text/JSON
consistency, documentation/adoption corrections, privacy/redaction
guardrails, and provider/configuration validation. No synthetic or
duplicate quota-filler work was created.

Every sample followed the same controlled protocol: the task contract and
acceptance criteria were frozen before builder selection; the builder
worked a fresh branch under an explicit identity; peer findings were
withheld until the first implementation was declared complete; and the
final head required exact-head Codex and Claude audits (author lane
excluded) plus the required gate before merge. Findings carry one
disposition: accepted-fixed, owner-decided, false-positive, duplicate, or
infrastructure. Cloud uploads were metadata-only after dry-run inspection.

Two timing concepts are kept separate throughout. Active builder time is
time the builder spent producing the delivery. Wall time includes inactive
waits, review latency, and orchestrator takeover, and is not comparable to
active time. Fields the ledger does not contain (timing, model, tokens,
cost) are reported as unavailable, never zero, and never inferred.
Reviewer assignment was constant but task mix was not, so providers are
not ranked by raw BLOCKED rate.

## Source Samples

| Provider | Issue | PR | Task class |
| --- | --- | --- | --- |
| Cursor | [#657](https://github.com/codemower-ai/code-mower/issues/657) | [#662](https://github.com/codemower-ai/code-mower/pull/662) | Installation/documentation audit |
| Cursor | [#669](https://github.com/codemower-ai/code-mower/issues/669) | [#672](https://github.com/codemower-ai/code-mower/pull/672) | Adoption-result contract and qualification runner |
| Cursor | [#681](https://github.com/codemower-ai/code-mower/issues/681) | [#715](https://github.com/codemower-ai/code-mower/pull/715) | Audit-verdict transport for metadata findings |
| Cursor | [#717](https://github.com/codemower-ai/code-mower/issues/717) | [#722](https://github.com/codemower-ai/code-mower/pull/722) | Provider identity and capability split |
| Cursor | [#727](https://github.com/codemower-ai/code-mower/issues/727) | [#732](https://github.com/codemower-ai/code-mower/pull/732) | Package-install failure taxonomy and remediation |
| Muse | [#656](https://github.com/codemower-ai/code-mower/issues/656) | [#660](https://github.com/codemower-ai/code-mower/pull/660) | Package runtime and adoption diagnostics |
| Muse | [#664](https://github.com/codemower-ai/code-mower/issues/664) | [#665](https://github.com/codemower-ai/code-mower/pull/665) | Workflow security and template consistency |
| Muse | [#666](https://github.com/codemower-ai/code-mower/issues/666) | [#667](https://github.com/codemower-ai/code-mower/pull/667) | Gate reliability and exact-head auto-merge |
| Muse | [#711](https://github.com/codemower-ai/code-mower/issues/711) | [#716](https://github.com/codemower-ai/code-mower/pull/716) | Campaign retry evidence and metadata sanitization |
| Muse | [#718](https://github.com/codemower-ai/code-mower/issues/718) | [#725](https://github.com/codemower-ai/code-mower/pull/725) | Hosted transport and paid-dispatch readiness |
| Antigravity | [#654](https://github.com/codemower-ai/code-mower/issues/654) | [#661](https://github.com/codemower-ai/code-mower/pull/661) | Workflow security and regression tests |
| Antigravity | [#710](https://github.com/codemower-ai/code-mower/issues/710) | [#719](https://github.com/codemower-ai/code-mower/pull/719) | Runtime selection and readiness handling |
| Antigravity | [#721](https://github.com/codemower-ai/code-mower/issues/721) | [#724](https://github.com/codemower-ai/code-mower/pull/724) | Provider session isolation fix |
| Antigravity | [#728](https://github.com/codemower-ai/code-mower/issues/728) | [#733](https://github.com/codemower-ai/code-mower/pull/733) | Board concurrency and recovery guardrail |
| Antigravity | [#730](https://github.com/codemower-ai/code-mower/issues/730) | [#734](https://github.com/codemower-ai/code-mower/pull/734) | Campaign provider posture and completion semantics |
| Devin | [#740](https://github.com/codemower-ai/code-mower/issues/740) | [#759](https://github.com/codemower-ai/code-mower/pull/759) | Package-install command construction |
| Devin | [#741](https://github.com/codemower-ai/code-mower/issues/741) | [#760](https://github.com/codemower-ai/code-mower/pull/760) | Campaign result rejection diagnostics |
| Devin | [#739](https://github.com/codemower-ai/code-mower/issues/739) | [#761](https://github.com/codemower-ai/code-mower/pull/761) | Structured-output schema compatibility |
| Devin | [#753](https://github.com/codemower-ai/code-mower/issues/753) | [#762](https://github.com/codemower-ai/code-mower/pull/762) | Strict-shell provider flag handling |
| Devin | [#763](https://github.com/codemower-ai/code-mower/issues/763) | [#764](https://github.com/codemower-ai/code-mower/pull/764) | Issue-linked delivery contract and cold prompts |

Infrastructure-only evidence (not delivery samples): one Devin dispatch
received no acknowledgment inside the bounded wait, and one Grok-built
change whose blocked-audit fix was misrouted to Cursor is retained as
provider-routing evidence. Release-qualification runs are operational
compatibility, not bounded deliveries.

## Comparable Aggregates

| Provider | Done | Ledger timing | Accepted blockers | Fix rounds | CI corrections | Interventions (orchestrator / owner) | Provenance |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Cursor | 5/5 | Dispatch-to-final wall 859s to ~36,580s where reported; the top end includes a long inactive wait plus takeover and is not active time; 1 sample untimed | Minimum 17 severity-labeled (1 P1, 16 P2) plus 2 accepted pre-audit corrections; 1 sample undisclosed | 2 to 13 fix/intervention rounds per recorded sample; 9 exact-head rounds on the hardest | Minimum 1 | 6 to 13 orchestrator interventions on the two hardest; 0 owner where recorded | Model, tokens, and cost unavailable |
| Muse | 5/5 | Authoring/active 390.7s + 56.0s fix, ~720s, and ~960s where reported; 2 samples untimed | 1 P1, 1 P1/P2, and 2 P2 severity-labeled, plus an uncounted defect set across 7 audit rounds in one sample and 1 orchestration fix in another | 0 to 7 audit-fix rounds per sample | 0 recorded | 1 to 4 orchestrator interventions per recorded sample; 0 owner where recorded | `muse-spark-1.3-contributor` on 3 samples; CLI 1.0.2 and 1.0.3-R2198.1; cost unavailable |
| Antigravity | 5/5 | 747s CLI-reported (835s dispatch-to-PR) and 5,742s resumed-conversation on 2 samples; 3 samples untimed | Minimum 13 accepted blocking across 2 samples; 1 false positive with regression proof; 2 samples clean at first round; 1 sample undisclosed | 0 to 9 exact-head rounds per recorded sample | 0 recorded | 1 to 5 orchestrator interventions per recorded sample | CLI 1.1.27 on 2 samples with 755,233 and 3,947,268 reported total tokens; model otherwise unavailable; cost unavailable |
| Devin | 5/5 | 3,623 active seconds total; 701s median; ~725s mean | 4 accepted blockers (1 P1, 3 P2) | 4 audit fix rounds | 1 CI correction | 2 orchestration interventions; 0 owner interventions | Local Devin CLI 3000.6.14 with model adaptive selected explicitly by the runner; tokens and cost unavailable |

## Throughput With Quality

All 20 samples merged only after exact-head Codex and Claude PASS
verdicts and a green required gate; none merged on a stale or unaudited
head. Completion alone does not differentiate the providers: review
burden does. The hardest Cursor, Antigravity, and Muse samples still
merged clean, but only after 5 to 13 fix/intervention rounds each, while
three Devin and two Antigravity samples passed first-round review with
zero findings.

Peer review prevented material defects across the corpus, including a
response schema that would have been rejected before generation, a
missing fork guard in active workflows, misclassified rejection and
timeout diagnostics, line-zero blocking misclassification, reviewer-only
providers admitted to campaign dispatch, stale retry state with
cross-provider mutation, and contradictory terminal evidence. That catch
list is the quality half of throughput: the builder lanes produced, and
the reviewer lanes filtered.

## Limitations

The corpus is pilot-scale: five samples per provider supports ranges and
minimums, not formal confidence intervals. Task mix differs by provider,
so fix-round and finding counts describe review burden on different work,
not a provider leaderboard. Timing is not apples-to-apples: Devin
reports active seconds, Cursor reports dispatch-to-final wall including
waits and takeover, and Antigravity mixes CLI-reported and
resumed-conversation totals. Cost and token coverage is partial at best.
Finding-level detail is unavailable for three completed samples beyond
their pass-and-merge record. All evidence is self-dogfood from one
repository under one orchestrator, and may not generalize.

## Recommendations

Builder role: all four providers are viable supervised builders with
explicit identity and single-writer branches. Devin through the local
runner showed the lowest orchestration burden here. Cursor hosted
delivers but needs takeover budget on hard tasks. Antigravity is capable
yet token-heavy and review-dependent; keep it manual. Muse is solid,
with review catching real defects on its harder samples.

Reviewer role: no change. Codex audit and Claude audit remain the
merge-authority lanes; Gitar stays informational corroboration. Builder
delivery success grants no reviewer authority to any of the four.

Orchestrator role: freeze the contract before selecting the builder;
withhold findings until first implementation; record active time
separately from wall time and count manual-session setup as
orchestration overhead; require exact-head audits excluding the author
lane; upload metadata only after dry-run inspection; never synthesize
quota-filler work.

## Promotion Posture

This evidence supports continued supervised use of all four providers as
builders. It promotes no provider to reviewer merge authority: builder
success never promotes reviewer merge authority. Any reviewer promotion
still requires the clean/blocked calibration evidence in the
[lane promotion policy](lane-promotion-policy.md).
