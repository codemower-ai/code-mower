# v1.5.1 qualification contract and evidence matrix

This document defines the immutable acceptance contract for #1056 under the
v1.5.1 epic #1050. Observed results belong on #1056, the public implementation
PRs, and the GitHub Release. Do not edit qualified source to insert later
results or private data.

## Identity

| Item | Required identity |
| --- | --- |
| Release source | The release PR's actual `mergeCommit.oid` after independent exact-head review, CI, and authoritative gate |
| Candidate | First successful attempt of `Code Mower Immutable Candidate`, dispatched on `main` while `GITHUB_SHA` equals that merge SHA |
| Artifacts | Retained `code_mower-1.5.1-py3-none-any.whl`, `code_mower-1.5.1.tar.gz`, `candidate.json`, and `rehearsal.json` |
| Publication | Annotated `v1.5.1` tag, retained candidate run, production publication run, non-publishing release-event run, and byte-identical GitHub assets |
| Installed release | Canonical PyPI download whose version and SHA-256 match the accepted candidate |

## Required observations

| Boundary | Acceptance |
| --- | --- |
| Source | #1051, #1052, #1058, #1059, #1057, and #1060 are ancestors; release text is final; privacy, package inventory, Python 3.12–3.14, containment, wheel rehearsal, Board qualification, exact-head independent review, and gate pass |
| Fresh hosted install | A minimal hosted environment without uv or pipx bootstraps through the documented Python path, installs the exact candidate, and reports `code-mower 1.5.1` |
| Upgrade | A verified v1.5.0 install upgrades to the exact candidate while preserving synthetic configuration and state |
| Remote observer | Checkout-free orchestrator-only doctor uses the packaged-starter observer plan, keeps local/provider/campaign checks quiet, and emits no local path |
| Safe init | Existing root policy is preserved; implicit packaged-starter apply refuses with exact recovery; generated lane targets are unique |
| Board | Transient and persistent lifecycle, version discoverability, optional/unavailable lineage, empty-workflow presentation, and closed privacy projection pass |
| Graphify | Installed reader accepts the qualified generation, excludes `doc_ref` from code, and returns bounded completeness/readiness results |
| Slack | Basic private-workspace setup, doctor, authorization, and lifecycle commands pass. Slack telemetry remains deferred to v1.6.0 |
| Audit publication | A single-lane result and a multi-lane result bind to their exact source job; an ineligible writer lane can self-exclude without blocking an eligible lane |
| Hosted Board canary | Exactly one bounded hosted Board canary records provider completion and exit, the closed Board projection, privacy boundary, aggregate campaign ACU cap, every create attempt, and uncertainty; authorized usage and settled usage are separate facts |
| Cloud evidence | Only allowlisted metadata is uploaded to codemower.com; a fresh authenticated dashboard observation verifies the accepted upload separately from its receipt |
| Publish/reinstall | The unchanged candidate is verified before tag, publication does not rebuild, GitHub assets match byte-for-byte, release-event publication jobs stay disabled, and a clean canonical reinstall matches the release identity |

Raw source, diffs, prompts, transcripts, provider output, credentials, local
paths, private Slack content, private graph data, and private Board/session state
remain local. A successful upload receipt does not prove aggregate freshness;
the fresh dashboard observation is a separate requirement.

Follow [the v1.5.1 runbook](v151-release-runbook.md). Any source or packaged
document change after candidate creation invalidates the candidate. A failed,
missing, expired, rerun, or ambiguous candidate is a stop and does not authorize
a replacement build without a newly reviewed release source.
