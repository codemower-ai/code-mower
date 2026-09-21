# v1.5.2 qualification contract and evidence matrix

This document defines the immutable acceptance contract for #1078. Observed
results belong on #1078, the release pull request, and the GitHub Release. Do
not edit qualified source to insert later results or private data.

## Identity

| Item | Required identity |
| --- | --- |
| Release source | The release PR's actual `mergeCommit.oid` after independent exact-head review, CI, and the authoritative gate |
| Candidate | First successful attempt of `Code Mower Immutable Candidate`, dispatched on `main` while `GITHUB_SHA` equals that merge SHA |
| Artifacts | Retained `code_mower-1.5.2-py3-none-any.whl`, `code_mower-1.5.2.tar.gz`, `candidate.json`, and `rehearsal.json` |
| Publication | Annotated `v1.5.2` tag, retained candidate run, production publication run, non-publishing release-event run, and byte-identical GitHub assets |
| Installed release | Canonical PyPI download whose version and SHA-256 match the accepted candidate |

## Required observations

| Boundary | Acceptance |
| --- | --- |
| Source | #1074, #1075, #1076, #1077, and #1080 are ancestors; release text is final; privacy, documentation lifecycle, package inventory, Python 3.12–3.14, wheel rehearsal, independent exact-head review, and gate pass |
| Documentation | Every Markdown file is classified; canonical subjects are unique; frozen hashes, local links, generated index, current-release regions, and supporting-document pin rules pass |
| Package templates | Every declared source exists; a fresh package projects the canonical template tree byte-for-byte; retired mirrors and embedded fallbacks are absent |
| Fresh install | The exact wheel installs in a disposable Python 3.12 environment with no product state or Slack service created by default and reports `code-mower 1.5.2` |
| Upgrade and rollback | A canonical v1.5.1 wheel upgrades to the exact candidate and can be restored in disposable state while preserving synthetic operator state |
| Graphify | The installed reader accepts the qualified synthetic generation, excludes `doc_ref` from code, and returns bounded completeness/readiness results |
| Slack | Offline setup, manifest permissions, disabled-state reporting, and no-live-readiness rules pass. Slack telemetry remains deferred to v1.6.0 |
| Publish and reinstall | A no-publish run installs the manifest parser dependencies and verifies the unchanged candidate before publication; publication does not rebuild; GitHub assets match byte-for-byte; release-event publication jobs stay disabled; and a clean canonical reinstall matches the release identity |

The runtime and hosted-service boundary is unchanged from v1.5.1. A new paid
provider canary, Slack reinstall, or metadata-only cloud upload is unnecessary
unless the candidate comparison shows a runtime, dependency, authority,
privacy, or service-contract change. If that equivalence check fails, stop and
qualify the affected boundary before publication.

Raw source, diffs, prompts, transcripts, provider output, credentials, local
paths, private Slack content, private graph data, and private Board/session state
remain local.

Follow [the v1.5.2 runbook](v152-release-runbook.md). Any source or packaged
document change after candidate creation invalidates the candidate. A failed,
missing, expired, rerun, or ambiguous candidate is a stop and does not authorize
a replacement build without newly reviewed release source.
