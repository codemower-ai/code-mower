# Post-v0.8 Effectiveness Assessment

This assessment summarizes the dogfood run that completed the v0.7 cold
adoption polish and v0.8 native Board epics on 2026-09-02.

It is operational evidence, not a universal benchmark. The strongest claims
come from Code Mower running on itself; other providers remain calibration
candidates until more repositories and more adjudicated findings are captured.

## What Is Working

- **Small-PR throughput is real.** The v0.7/v0.8 queue landed as many small
  PRs with one issue per PR, normal CI, peer review, and `code-mower/gate`
  before merge. This is the strongest proof that Code Mower can raise velocity
  without turning review into an informal chat transcript.
- **The merge gate caught process drift.** Author-never-gates, current-head
  audit labels, stale-audit cleanup, and explicit owner/gate state kept the
  loop honest while PRs moved quickly.
- **The adoption story is much clearer.** A cold user now has a Python 3.12+
  contract, pipx/uv/install guidance, hosted-builder and orchestrator-only
  doctor postures, non-expiring token support, redacted `lanes status`, and a
  local browser Board.
- **Visibility no longer depends on a separate hook-based viewer.** The native
  Board provides a read-only local view of PR lanes, checks, owner queue, local
  history, spend/latency, verdicts, and optional metadata-only agent cards.
- **The cloud boundary held.** Dogfood, reviewer-run, and Board-snapshot uploads
  remained explicit and metadata-only: no source, diffs, transcripts, issue
  bodies, raw stdout/stderr, auth output, local paths by default, or secrets.

## What Should Be Better

- **Keep release verification routine.** The public package baseline is
  `v0.9.3-beta.1` / `code-mower==0.9.3b1`; each beta should continue to prove
  the PyPI install path, first-user rehearsal, local Board, and metadata-only
  cloud checks before it is announced.
- **Use more builders in live dogfood.** The v0.7/v0.8 implementation PRs were
  overwhelmingly Codex-authored. That was fast, but it does not yet compare
  Claude Code, Cursor/Grok Bot, Devin, or Antigravity as builders on equal
  footing.
- **Keep Gitar advisory until automation is smoother.** Gitar produced useful
  review signal, including a real duplicate-read finding in the Board doctor
  PR, but label/state automation still needs more proof before it should gate.
- **Turn Board MVP into operator muscle memory.** `lanes status`, `board serve`,
  `board record`, `board doctor`, and cloud Board snapshots work; the next
  adoption step is using them on every active repo so status and cost data are
  complete without heroics.
- **Rehearse a cold repo again after release.** Run the package install path,
  `init --easy`, `doctor --adoption`, `lanes status`, `board serve`, and one
  tiny audited PR on a repo that is neither Code Mower nor a reference repo.

## Peer Builder And Reviewer Assessment

| Lane | Observed role | Current assessment |
| --- | --- | --- |
| Codex builder | Primary v0.7/v0.8 builder | Excellent throughput and small-PR discipline in this repo. Needs comparison against other builders before claiming broad builder superiority. |
| Claude audit | Merge-authority reviewer | Strong default peer reviewer. Local spend data shows 84 captured runs across 54 PRs, with 62 PASS, 21 BLOCKED, and 1 UNKNOWN; median captured runtime was about 88 seconds. Keep as a first-class gate where the CLI and runner are healthy. |
| Codex audit | Peer reviewer, mostly historical in this slice | Strong earlier signal on integration seams, parser edge cases, credential handling, and runner safety. It should continue as the peer lane for Claude-authored or hosted-builder PRs; it is excluded from gating Codex-authored PRs by design. |
| Gitar | SaaS advisory reviewer | Useful third signal and found at least one concrete v0.8 issue before merge. Keep informational until label freshness, quota behavior, and calibration evidence are broader. |
| Antigravity CLI | Google-family research reviewer | Promising informational lane. The supplemental lens proof caught useful auth/history signal and stayed mostly quiet on clean controls, but context insufficiency and high-latency profiles keep it out of merge authority. |
| Gemini CLI | Legacy Google-family comparison lane | Useful historical/calibration signal, but keep separate from Antigravity. Prefer Antigravity for new Google CLI work while preserving Gemini evidence as its own bucket. |
| Cursor/Grok Bot | Hosted builder candidate | Good candidate for hosted-builder dogfood and lineage capture. Current evidence is stronger on docs/install feedback than on merged Code Mower implementation PRs. |
| Devin | Hosted builder/reviewer candidate | Explicitly opt-in only. No current Code Mower dogfood basis for promotion. Record builder provenance first, then require independent reviewer evidence. |

## Recommendation

Code Mower is ready for supervised early adopters who want a manual
reviewer-gate pilot, adoption diagnostics, and local visibility before
promotion. It is not yet ready to be marketed as an unattended autonomous merge
system for arbitrary teams.

The next release should keep running a cold-repo adoption rehearsal from the
published package and deliberately route a few small follow-up issues through
non-Codex builders and informational reviewers so promotion decisions are based
on measured evidence rather than enthusiasm.
