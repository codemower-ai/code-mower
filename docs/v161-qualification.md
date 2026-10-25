# v1.6.1 qualification contract and evidence matrix

This document defines the immutable acceptance contract for #1121. Observed
results belong on #1121, the release pull request, and the GitHub Release. Do
not edit qualified source to insert later results or private data.

## Identity

| Item | Required identity |
| --- | --- |
| Release source | The release PR actual `mergeCommit.oid` after independent exact-head review, CI, and the authoritative gate |
| Candidate | First successful attempt of `Code Mower Immutable Candidate`, dispatched on `main` while `GITHUB_SHA` equals that merge SHA |
| Artifacts | Retained `code_mower-1.6.1-py3-none-any.whl`, `code_mower-1.6.1.tar.gz`, `candidate.json`, and `rehearsal.json` |
| Publication | Annotated `v1.6.1` tag, retained candidate run, production publication run, non-publishing release-event run, and byte-identical GitHub assets |
| Installed release | Canonical PyPI download whose version and SHA-256 match the accepted candidate |

## Required observations

| Boundary | Acceptance |
| --- | --- |
| Source | #1119 and #1120 are ancestors; release text is final; privacy, documentation lifecycle, package inventory, Python 3.12 through 3.14, wheel rehearsal, independent exact-head review, and gate pass |
| Fresh install | The exact wheel installs in a disposable Python 3.12 environment, reports `code-mower 1.6.1`, and creates no product state or service by default |
| Upgrade and rollback | A canonical v1.6.0 wheel upgrades to the exact candidate and can be restored in disposable state while preserving synthetic operator state |
| Runner auth | A permission-checked private runner env is loaded without token output; real Codex and Claude auth smoke probes pass; revoked Claude overrides are excluded from the clean bounce probe but preserved for real audits |
| Merge authorization | An unlabeled green fixture remains status-only; an explicitly labeled exact-head PR queues one head-pinned squash merge; the authorization lookup is paginated and revalidated immediately before merge |
| Listener freshness | A listener older than its environment fails; a same-second process timestamp does not fail solely because the file mtime has subsecond precision |
| Unchanged product boundary | Board, Graphify, Slack, lifecycle telemetry, cloud schemas, provider authority, and privacy behavior remain equivalent to v1.6.0 |
| Publish and reinstall | No-publish verification consumes the retained candidate; publication does not rebuild; GitHub and PyPI assets match byte-for-byte; release-event publication jobs remain disabled; canonical reinstall matches release identity |

The candidate normally soaks for at least 24 hours and receives two independent
clean install or upgrade passes. A shortened emergency-patch soak is permitted
only if #1121 records the reason, risk, exact independent evidence, and rollback
plan before publication.

Raw source, diffs, prompts, transcripts, provider output, credentials, local
paths, private graph data, Slack content, and Board/session state remain local.

Follow [the v1.6.1 runbook](v161-release-runbook.md). Any source or packaged
document change after candidate creation invalidates the candidate. A failed,
missing, expired, rerun, or ambiguous candidate is a stop and does not authorize
a replacement build without newly reviewed release source.
