# Local Board qualification (#951)

Builder: Code Mower Codex. One writer owns the issue branch. Independent Claude
review must name the exact current PR head; this qualification does not grant
merge authority, merge the PR, or execute a hosted work order.

This is a qualification of the local Board read model. The trusted work order
allows a maintained local execution path in tests and requires the hosted Devin
canary to remain an operator action. Slack/cloud mappings remain in #921;
#923's final canary and #974's cloud freshness qualification remain separate.

## Evidence composition

`LocalObservationInput` is the common input to `observations_payload` for local
and remote adapters. Its output still validates against the closed
`code_mower.boardObservation.v1` schema. No provider/session/context/Slack/
Graphify prose, source content, private paths, or provider references are added
to the record or uploaded in a scorecard.

- PR number, full head, session, work and worktree bind evidence independently.
  Old-head or old-observation review/CI/gate facts remain `stale` with their
  original head and source timestamp. They cannot establish current readiness.
- `review_from_audit_artifact` requires the resolved exact-target lineage and
  effective review authority from trusted base policy. Missing lineage, an old
  head, an identity-only decision, a contributor (including a former writer),
  an unqualified reviewer, and an informational lane cannot supply authoritative
  audit evidence. Callers must capture policy from the trusted base; Board does
  not fetch or promote policy from the work under review.
- `LocalPolicyObservation` supplies explicit, exactly bound branch-update and
  human-policy reasons. A passing audit does not imply a human review
  requirement or approval. Stale/unreachable policy requires a refresh instead
  of inventing a current owner decision.
- Review request, running review and verdict remain distinct. The gate
  publisher is execution evidence only; `code-mower/gate` is a separate verdict.
  Sampled CI cannot establish required CI. Merge readiness requires full CI,
  review, gate and merge evidence, with no blocking reasons or active/pending
  provider run.
- All simultaneous reasons are retained before the primary actor/action is
  chosen by `REASON_ROUTES`. Behind-main plus review-needed is one work row;
  branch updates, failed checks and audit remediation remain builder work.
- When a work record has no blocking route, the view offers a follow-up:
  refresh old evidence, observe an active implementation, confirm an assigned
  execution starts, request a missing exact-head review after implementation,
  or inspect remaining evidence. These are presentation guidance, not provider
  mutations, new authority, or an invented recorded blocking reason.
- Collapsed work rows expose next action/actor and each source's freshness and
  coverage. Full and partial measurements both show the observed denominator.
  Missing/unsettled cost remains unavailable; no cost, quality or productivity
  value is inferred from a run's phase, a zero ACU snapshot or a successful test.

## Reproducible scorecard

From a checkout with the test dependencies installed:

```bash
PYTHONPATH=src python scripts/qualify_board.py \
  --head-sha "$(git rev-parse HEAD)" --output .code-mower/board-scorecard.json
PYTHONPATH=src python tests/board_qualification_fixtures.py .code-mower/board-browser
# Playwright must be installed and its Chromium browser available.
node tests/board_qualification_browser.cjs .code-mower/board-browser
```

The `Board qualification` CI job runs these checks and uploads only
`scorecard.json`, `browser-scorecard.json`, and the two synthetic screenshots.
The normal `package` check depends on this job as well as the Python matrix.
Generated HTML, fixture records, process logs and private stores are not
artifacts. Each scorecard names its tested head; a new head requires new checks
and a new independent audit.

| Qualification surface | Coverage and limits |
| --- | --- |
| Closed producer/render matrix | 20 named scenarios in `board_qualification_fixtures.py`, validated by `test_board_qualification.py` and rendered by the shipped JavaScript |
| Fresh work / reviews | No PR, review requested, running, changes requested, implementation complete, behind plus review, simultaneous blockers |
| Independent authority | Publisher pass with gate pending, sampled CI, exact-head audit, GitHub merge state, explicit human policy; lineage and role admission regressions |
| Source/outcome matrix | Fresh, stale, unreachable, waiting, failed, cancelled, historical-running, unlinked and complete-coverage no-work |
| #976 distinctions | Implementation complete with provider active or unavailable; accepted cancellation while still running; observed cancellation; local interruption before delivery; unsettled usage excluded |
| Identity / restart | Re-read without rewriting timestamps, wrong-worktree refusal, multiple-worktree rendering, session resolver and managed-service delayed-health/exact-binding suites |
| Maintained local execution | Real bounded Python child through `supervise_process`, `classify_delivery`, `build_delivery_outcome_event`, real `session start`/resolver, and Board projection. GitHub delivery snapshots are synthetic. This is not a paid model-quality canary. |
| Desktop / phone | 1440×1000 and 390×844; first meaningful row, visible action/actor/sources, no horizontal overflow, Health → Now interaction, console checks and screenshots |
| Time | Suite wall time measured with a monotonic clock and 1/1 coverage. Automated first-row visibility must take under ten seconds. Human task-discovery time remains unmeasured. |
| Cost / productivity | Monetary cost unavailable; no settled provider usage observed by this run. No productivity claim. |
| Hosted Devin | Offline transport/contract evidence only. Live execution **not run**, coverage **unavailable** pending the checklist below. |

The local Mac lane sandbox places temporary directories inside a Git checkout,
which intentionally fails the private-store outside-Git guard, and cannot launch
Chrome. Report those runs as partial/blocked environment coverage, never as
passing provider/storage or browser qualification. CI supplies disposable
outside-Git storage and a browser. The `test_board_service` suite exercises the
maintained lifecycle provider with deterministic supervision fixtures; it is not
an installation or restart of the owner's live launchd service.

## Hosted Devin operator checklist (not executed by this builder)

1. Record a new owner-approved **campaign-wide ACU cap**, wall-clock deadline,
   task/round scope and stopping conditions on #951 or its PR. Do not reuse the
   consumed qualification create allowance. Record current applicable pricing
   and its uncertainty before approving paid work; a cap is not observed spend.
2. Select one bounded work order through the maintained `DevinWorkOrders` path
   with the reviewed manifest, exact repository/issue/branch/base/author
   binding, qualified builder role, and an explicit per-session `acu_limit`
   within the campaign cap. Review the dry-run plan before the separate explicit
   `apply=True`. See [the work-order embedding contract](devin-work-orders.md).
3. Preserve the durable create intent. If creation is uncertain, reconcile that
   same intent/checkpoint and inspect the provider before any further mutation.
   Never retry create blindly or open a replacement to bypass uncertainty.
4. Observe assignment, current GitHub PR/head, provider state and availability,
   completion/failure, cancellation acceptance and eventual observed exit
   separately. Feed `RemoteWorkObservation` to `hosted_work_input` with the
   exact round; do not infer writer exit from accepted implementation output.
5. Capture independent full-head review, required CI, gate publisher, gate
   verdict, GitHub merge state and explicit policy evidence. Run the existing
   exact-head contributor exclusion and effective authority decisions; the
   builder cannot supply its own independent audit. A changed head restarts
   evidence qualification.
6. Refresh, restart the observer and simulate an unavailable read. Retain only
   the closed previous observation with its original timestamps. Verify the
   same work projection and stale/unavailable cues against the fixture matrix;
   no retained observation establishes liveness. Independently verify the
   managed local Board's exact binding and delayed health after restart.
7. Record elapsed time, final outcome and observed ACU with their coverage and
   settlement state. Missing usage and unsettled zero snapshots remain unknown
   cost; do not sum them as zero or infer productivity. Stop within the approved
   campaign budget, accounting for all rounds and uncertain charges.
8. Publish only allowlisted state/count/coverage/head metadata and privacy-safe
   screenshots. Keep session/provider identifiers, prompts, transcripts,
   questions, context packets, Slack/Graphify content, source and paths private.
   Attach the exact-head independent audit and green CI/gate evidence. Mark live
   hosted coverage separately before #923 final acceptance; this document does
   not declare the Slack canary or cloud/release qualification passed.
