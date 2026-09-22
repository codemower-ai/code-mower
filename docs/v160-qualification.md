# v1.6.0 qualification contract and evidence matrix

This document defines the immutable acceptance contract for #1105. Observed
results belong on #1105, the release pull request, and the GitHub Release. Do
not edit qualified source to insert later results, credentials, private data,
or operational transcripts.

## Entry gates

Candidate construction is refused until the final release commit contains all
of the following and each dependency has its own accepted exact-head evidence:

| Gate | Required state |
| --- | --- |
| Audit history | #1104 merged: bounded payload-aware comment pagination and exact recognition of reserved lineage controls |
| Hosted consumer | CodeMower.com #978 deployed before client emission; `/api/health` advertises contract commit `99b657ae9822a689b46d21c41695a4cfb28a177d` and fixture-manifest SHA-256 `9e87c52812a49a1c72d0e0d2448661a3ef17cb8539ca8e029c6669ea9738d62e` |
| Source scope | #1063/#1109, #1064/#1099, #1082/#1100/#1103, #1083/#1097, #1084/#1102, #921/#1098, and #1106/#1108 are ancestors of the release commit |
| Release text | The v1.6 epic, release notes, roadmap, package identity, and current documentation agree on final scope |

The first immutable-candidate workflow run before all entry gates hold is
invalid evidence and must not be reused.

## Identity

| Item | Required identity |
| --- | --- |
| Release source | The release PR's actual `mergeCommit.oid` after exact-head review, complete CI, and the authoritative gate |
| Candidate | First successful attempt of `Code Mower Immutable Candidate`, dispatched on `main` while `GITHUB_SHA` equals that merge SHA |
| Artifacts | Retained `code_mower-1.6.0-py3-none-any.whl`, `code_mower-1.6.0.tar.gz`, `candidate.json`, and `rehearsal.json` |
| Telemetry contract | OSS contract commit and fixture-manifest digest named in the entry-gate table, accepted byte-for-byte by the deployed consumer |
| Publication | Annotated `v1.6.0` tag, retained candidate run, production publication run, non-publishing release-event run, and byte-identical GitHub assets |
| Installed release | Canonical PyPI download whose version and SHA-256 match the accepted candidate |

## Required observations

| Boundary | Acceptance |
| --- | --- |
| Source | Every entry gate above is complete; Python 3.12–3.14 CI, release-integrity, privacy, documentation, package, Graphify, Board, Slack, cloud capability, independent exact-head review, and the authoritative gate pass |
| Candidate | One merge-SHA wheel/sdist pair is built once, retained, verified, and reused without rebuilding; a changed source or packaged document invalidates it |
| Board | Repository filters, invoking and serving versions, stale-service classification, atomic replacement/rollback, local lifecycle-summary projection, and exact recovery guidance agree |
| Audit history | Histories exceeding the former private-context payload limit remain complete and bounded; ordinary prose does not become control data; malformed real controls, mutation, truncation, duplication, or budget exhaustion fail closed |
| Hosted capability | The deployed authenticated health response advertises the exact accepted contract identity before emission; older or mismatched consumers cause the client to emit nothing |
| Slack telemetry canary | One owner-authorized private lifecycle exercises start/status/answer/cancel or terminal completion as applicable; only meaningful summaries cross the boundary; local Board state reconciles with authenticated hosted totals and freshness |
| Privacy and isolation | No task/message prose, answers, source, diffs, prompts, transcripts, response URLs, Slack identities, credentials, private paths, graph/context data, or raw provider output is uploaded; wrong tenant/repository access is denied |
| Retention and control | Hosted retention, export, deletion, capability disablement, and rollback are verified; disabling either client emission or hosted acceptance stops new telemetry independently |
| Installation | Exact wheel fresh install, v1.5.2 upgrade with synthetic state, disposable rollback to the digest-verified v1.5.2 wheel, uninstall preservation, basic Slack, Graphify, Board service, and public-package paths pass |
| Soak and adoption | The unchanged candidate soaks for at least 24 hours and receives two independent install or upgrade passes |
| Publish and reinstall | Publication reuses the retained bytes; GitHub and PyPI assets and checksums agree; the release-event run does not republish; a clean canonical reinstall matches release identity and behavior |

Raw source, diffs, prompts, transcripts, provider output, credentials, private
Slack content, private graph data, private Board/session state, and tenant
mappings remain local or in their authorized protected store. Public evidence
contains only immutable public identifiers and sanitized outcomes.

Follow [the v1.6.0 runbook](v160-release-runbook.md). A failed, missing,
expired, rerun, ambiguous, pre-entry-gate, or superseded candidate is a stop.
It does not authorize a replacement build without newly reviewed release source.
