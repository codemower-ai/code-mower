# Code Mower v1.0.1 Effectiveness Assessment

This assessment summarizes the dogfood evidence available while preparing
v1.0.1 on 2026-09-03. It is useful operating data, not a universal benchmark:
most quantitative evidence comes from Code Mower developing Code Mower, with
smaller dashboard and adoption-host samples. Use it to decide what to pilot
next, not to claim that any provider should be merge-gating in a new repository
without local calibration.

## Evidence Window

| Source | Window | Signal |
| --- | --- | --- |
| Code Mower OSS merged PRs | From the v0.6.0-beta.3 public package baseline on 2026-09-02 01:16 UTC through PR #633 on 2026-09-03 14:20 UTC | 67 merged OSS PRs in roughly 37 hours of wall-clock release-train time. |
| Code Mower OSS v1.0.1 loop | Local Board/spend window from 2026-09-02 21:42 UTC through 2026-09-03 14:06 UTC | 416 Board events, 163 reviewer runs, 47 blocked audit verdicts, 9 recorded reviewer catches, 2 fix rounds, 0 recorded owner interventions, and USD 51.61 of Claude-reported reviewer spend. |
| CodeMower.com dashboard | Since the v0.6.0-beta.3 baseline | 4 dashboard PRs merged, including Board snapshots, full-window portfolio counts, supervised Board snapshots, and the v1.0.1 productivity dashboard. |
| CodeMower.com local spend sample | Local dashboard checkout | 2 reviewer-spend rows: 1 PASS and 1 BLOCKED from Claude, with USD 0.55 recorded cost and 184 seconds of reviewer wall time. |
| Private reference/product repos | Available local artifacts | Adoption reports and qualitative install feedback exist, but the local checkouts available to this session did not contain Board history or reviewer-spend artifacts suitable for quantitative aggregation. |

## What Worked

- The small-PR lane model sustained high throughput. The OSS release train
  landed 67 PRs from the v0.6.0-beta.3 baseline through v1.0.1 prep, with 4
  additional dashboard PRs in the paired CodeMower.com repo.
- Peer review caught real issues before merge. The local v1.0.1 window records
  9 reviewer catches and 47 blocked audit verdicts; recent examples included
  stale timestamp handling, widened admin overrides, dashboard data-contract
  compatibility, and Board stop-payload redaction.
- Board and `lanes status` lowered orchestration friction. Operators could see
  stale audits, owner actions, active Board listeners, version drift, and
  productivity summaries without reconstructing state from GitHub tabs.
- The privacy boundary held. The data used here came from metadata-only Board
  events, reviewer-spend rows, GitHub PR metadata, and explicit dogfood uploads.
  It did not require source, raw diffs, transcripts, issue body text, raw
  stdout/stderr, auth output, local paths, or secrets.
- Diverse external agents produced useful adoption feedback even when they were
  not yet calibrated build or review lanes. Claude Code, Cursor/Grok Bot,
  Antigravity, and Devin all exposed install, doctor, hosted-agent, Board, and
  upgrade rough edges that became v0.9.x and v1.0 fixes.

## Provider Readiness

| Provider or surface | Observed role | Assessment |
| --- | --- | --- |
| Codex | Primary builder and reviewer in the OSS loop | Strong throughput and small-PR discipline. Codex review produced useful blocks, but cost data is incomplete where CLI telemetry is unavailable. |
| Claude Code | Primary peer reviewer and occasional orchestrator feedback source | High-volume reviewer signal with recorded spend and latency. The reviewer was useful enough for current Code Mower dogfood, but promotion in a new repo still needs that repo's calibration evidence. |
| Gitar | Advisory reviewer | Useful as a third signal and caught at least one concrete v1.0.1 issue. Keep informational until quota behavior, label freshness, and calibrated outcome data are broader. |
| Cursor/Grok Bot | Hosted builder/adoption rehearsal | Strong external install and hosted-orchestrator feedback. Needs more Code Mower-recorded builder runs before comparing merge quality or cost. |
| Antigravity | Hosted/local adoption reviewer candidate | Useful for install and integration feedback. Treat Gemini and Antigravity as distinct provider identities, and keep Antigravity informational until CLI/SDK behavior is calibrated. |
| Devin | Hosted adoption rehearsal | Valuable cold-environment feedback, especially around uv installs and hosted-agent expectations. Needs more live lane runs before promotion. |
| Muse | Experimental local CLI lane | The provider exists for calibration-only experiments. No merge-gating recommendation yet. |

## Limits

- The strongest quantitative sample is self-dogfood, so it may overstate
  throughput for teams without an experienced orchestrator.
- Reviewer spend is incomplete for providers that do not expose cost or token
  summaries in structured output.
- Private reference/product repo and hosted-agent install reports are useful
  adoption signal, but the local artifacts available here are not enough to
  compute apples-to-apples PR velocity or defect-catch rates.
- The current supervised-pilot posture still assumes a human or trusted
  orchestrator makes promotion and exception decisions.

## Next Measurement Work

- Add current-productivity uploads as a routine release task for Code Mower,
  CodeMower.com, and private reference/product repos.
- Record builder-run sidecars for Cursor/Grok Bot, Antigravity, Devin, and Muse
  whenever they work on Code Mower issues, so provider scorecards compare
  builder outcomes as well as reviewer verdicts.
- Add post-merge health and regression labels to distinguish true reviewer
  catches from conservative or stale blocks.
- Make CodeMower.com the long-running dashboard for productivity, quality, and
  cost trends while keeping the local Board as the immediate operator surface.
