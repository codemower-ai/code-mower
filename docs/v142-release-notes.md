# Code Mower v1.4.2 Release Notes

Status: source candidate prepared for #952. This document is not a publication,
installed-package qualification, comparative scorecard or freshness claim.
v1.4.0 and v1.4.1 tags, assets, release notes and their historical runbooks
remain immutable.

## Board clarity included in the source candidate

This candidate packages the accepted #945/#900 Board clarity work already on
`main`: the work-first Now/Timeline/Releases/Health views (#1000), the
provider-neutral remote lifecycle observations (#1002), managed persistent
Board services with stale-keepalive rejection during release restart (#1001),
exact local work observations (#999) and the qualified independent head-bound
evidence and session-visibility composition (#951, merged via #1003). It adds
no cloud event fields; Slack-specific and hosted-cloud mappings remain #921.

## What this source PR prepares

- Version, changelog, release notes and current docs updated for v1.4.2.
- Package/release qualification contracts and the post-merge runbook moved
  forward to bind the exact v1.4.2 release commit, tag and artifacts.
- Cold-install and 1.4.1-to-1.4.2 upgrade rehearsal coverage, installed-
  version/Board doctor and multi-service restart verification guidance for
  the Board processes named in #952, pending exact-inventory reconciliation.
- Dry-run-first allowlisted metadata upload guidance; no new field is
  required for release, and no upload is applied by this source change.

## Remaining boundaries carried over from v1.4.1

Repository-aware `board stop --repo` landed via #961 and is exercised by
#951's local qualification; installed-version agreement across the two
observed local Board processes named in #952 (port 5332,
`codemower-ai/code-mower`, plus one additional private-repository port) is
verified as part of the post-merge runbook, not by this
source PR, and each port's managed-versus-transient posture is classified
from `code-mower board service status` rather than assumed. Isolated
non-keyring Codex campaign authentication remains #983.
No new paid hosted Devin session is authorized by this release procedure.

The privacy boundary is unchanged. Upload only the maintained metadata
allowlist, never credentials, source, diffs, prompts, transcripts, private
paths, task prose, graphs, queries, citations or raw provider output. Stored
receipts and fresh authenticated aggregate visibility require separate
release-specific evidence.

## #951 status carried into this release

#951's merged code evidence (head-bound evidence composition, deterministic
regression cases, sanitized qualification scorecard) is accepted on `main`.
Its bounded hosted Devin canary is still pending; this release PR prepares
and checks the canary contract but does not claim the hosted result or close
#951.

## Acceptance still required

The orchestrator owns the canonical full suite, independent exact-head Codex
audit, CI/gate, merge, annotated tag, no-publish build, publication and all
installed-package/campaign/Board/cloud acceptance. Bind actual wheel/sdist
filenames, digests, inspected contents and installed behavior to the reviewed
release commit. Source inclusion alone does not prove published inclusion.
Follow the [v1.4.2 evidence matrix](v142-qualification.md) and
[current runbook](pypi-release.md); keep #952 open until every criterion
passes, and keep #951 open until its hosted Devin canary is observed.
