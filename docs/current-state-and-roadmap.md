# Code Mower Current State And Roadmap

This page is the short source of truth for the public OSS package and its
near-term product direction. Release-specific history belongs in the
[changelog](../CHANGELOG.md) and [release-history index](release-history.md).

## Product Position

Code Mower is a supervised operating layer for teams using multiple AI coding
agents. It coordinates roles and evidence around the tools rather than hiding
provider differences:

- the hosting agent is the default orchestrator;
- one builder owns each branch;
- independent reviewers evaluate the current pull-request head;
- repository policy decides which reviews can affect merge readiness; and
- optional context providers supply evidence without gaining build, review,
  tracker-write, or merge authority.

The product is useful without CodeMower.com. Cloud sharing is optional and
dry-run-first.

## Current Source And Published Baseline

This source defines Code Mower `v1.6.0`, with package spec
`code-mower==1.6.0`. Confirm the release tag on GitHub Releases and the package
version on the selected index before using an index install command; source
version and publication state are separate facts. The latest published baseline
remains `v1.5.2` until the entry gates and qualification contract for #1105 are
complete. See the
[v1.6.0 release notes](https://github.com/codemower-ai/code-mower/blob/main/docs/v160-release-notes.md),
[qualification contract](https://github.com/codemower-ai/code-mower/blob/main/docs/v160-qualification.md),
and [candidate runbook](v160-release-runbook.md). The GitHub Release and #1105
will carry observed source SHA, artifact digests, canary, soak, publication,
and reinstall evidence after those observations exist.
Historical v1.4.x artifacts and qualification records remain unchanged.

`v1.4.0`, `v1.4.1` and `v1.4.2` have all shipped, and the v1.4.0 and v1.4.1
artifacts remain unchanged. The published baselines require Python 3.12 or
newer. Each provides:

- pipx, uv tool, and contributor installation paths;
- safe setup previews and selectable participants;
- a Claude + Codex default reviewer and builder profile;
- host-led session briefs with a local single-orchestrator lease;
- local and hosted builder provenance, delivery, and recovery contracts;
- current-head Codex and Claude audit lanes;
- a GitHub-first reviewer gate and optional guarded Jira Cloud tracker;
- local lane status, Board, productivity, calibration, and provider scorecards;
- release qualification and resumable provider campaigns; and
- optional Coworker organizational context through a protected local store.

The v1.4.0 guided context workflow derives repository, work item, selected
connection, policy, packet, builder, pull-request head, input revision, reviewer,
and feedback recipient from the session. Fetch, delivery, attachment, review,
and feedback retain explicit authorization, expiry, and refresh checks.

## Capability Matrix

| Capability | Current behavior |
| --- | --- |
| Default participants | Claude Code and Codex |
| Participant selection | `init --interactive` or `init --with` |
| Session orchestration | Host-led operating brief and local lease; the command does not launch every provider |
| Qualified session hosts | Codex, Claude Code, and Cursor |
| Other recognized participants | Grok Bot, Antigravity, and Muse retain existing host policy; Devin orchestration is ineligible under source role admission |
| First-class local builders | Codex and Claude; maintained Devin builder lane is opt-in |
| Hosted builders | Explicit provider-specific dispatch and provenance; no implicit trust or merge authority |
| Merge-eligible reviewers | Codex and Claude after repository setup and calibration |
| Informational reviewers | Devin CLI and other optional providers until their evidence supports promotion |
| Organizational context | Optional Coworker packets for approved Claude/Codex/Devin roles, subject to role admission |
| Repository context graph | Provider-neutral packet extension, offline scope/freshness checks, and a revision-bound local graph lifecycle; Graphify is an optional bounded provider with no default dependency |
| Work tracking | GitHub Issues by default; Jira Cloud optional, bounded, and dry-run-first for writes |
| Team interaction | CLI, GitHub, local Board, optional CodeMower.com metadata views, and optional private-workspace Slack through the bounded hosted bridge and qualified Codex supervisor |

Provider selection, execution transport, and review authority are separate.
For example, selecting Devin does not promote the Devin reviewer, and selecting
Coworker does not make it a participant. The detailed source of truth is the
[Provider Matrix](provider-matrix.md).

## Installation And First Use

The supported first-use sequence is:

1. Install one pinned Code Mower command using the path appropriate to the
   machine.
2. Preview `code-mower init --easy`.
3. Generate reviewable output with `--apply`.
4. Run `code-mower doctor --adoption --repo OWNER/REPO`.
5. Open a small setup PR and run the Codex and Claude audits manually.
6. Add automation tokens, recurring builder dispatch, and promoted merge policy
   only after the manual loop works.

Use [Install And Bootstrap](install.md) for exact commands and
[Try Code Mower In 10 Minutes](try-in-10-minutes.md) for the first audited PR.
Existing repositories should inspect setup drift before copying new generated
files; see [Upgrade An Existing Repository](upgrade-existing-repo.md).

Published-release evidence belongs on the corresponding GitHub release and
release PR. Maintainers can reproduce the package path with the
[First-User Install Rehearsal](first-user-install-rehearsal.md).

## Local And Cloud Boundaries

Local runners hold source code, diffs, worktrees, provider credentials, raw
provider output, and private context. Generated GitHub files coordinate labels,
comments, checks, and workflow entrypoints.

Default cloud bundles exclude:

- source code and raw diffs;
- model prompts and transcripts;
- raw stdout/stderr and auth output;
- issue body text;
- credentials and secret values;
- private Coworker evidence and account bindings;
- Slack command and modal prose, raw authenticated requests and routing
  identities; and
- private Slack member, repository and channel mappings.

CodeMower.com currently exposes a health endpoint, sign-in, private dashboards,
metadata ingestion, evidence/detail views, productivity summaries, provider
scorecards, and self-service metadata export/deletion. Dashboard routes require
sign-in. Cross-team cohort benchmarks and automated retention jobs remain
future hosted-service work.

## Known Limits

- Code Mower remains GitHub-first for pull requests, checks, and merge gates.
- `session start` prepares state and instructions; it is not a universal
  multi-provider process launcher.
- Devin has stronger builder support than reviewer or orchestrator support.
- Private Coworker delivery is limited to explicitly approved Claude, Codex, and
  Devin roles.
- Graphify's bounded provider foundation shipped in v1.4.0; its complete
  qualified integration, scorecard and query behavior shipped in v1.4.1. It
  remains optional and has no default dependency.
- Slack in v1.6.0 retains one private workspace with explicit member, repository
  and private-channel mappings plus private start, status, answer and
  confirmed-cancel interactions. Metadata-only lifecycle summaries remain
  disabled until the hosted service advertises the exact accepted contract.
  Slack-to-Board links, Slack Connect, public channels and richer Slack UX are
  deferred.
- Provider cost fields remain unknown when the provider does not return them.
- A successful release campaign proves installation and operational transport,
  not builder quality or reviewer promotion readiness.
- Auto-discovered calibration cases are proposals that require human
  adjudication.
- Broad unattended rollout and uncalibrated merge gates are outside the current
  product posture.

## Current Release And v1.6.0

`v1.5.2` is released and remains the current supported package. The `v1.6.0`
source line now contains the completed doctor taxonomy, atomic Board replacement,
identity-verified Board inventory and version guidance, neutral `unmanaged`
state for ordinary pull requests with no Code Mower provenance, share-safe
adoption diagnostics, cross-repository cost isolation, and the closed
metadata-only control-surface summary contract. Visible malformed Code Mower
claims remain actionable and fail-closed. The optional client
emitter remains fail-closed unless the hosted service advertises the exact
accepted contract identity.

The [v1.6.0 milestone](https://github.com/codemower-ai/code-mower/milestone/2),
[epic #1066](https://github.com/codemower-ai/code-mower/issues/1066), and
[release issue #1105](https://github.com/codemower-ai/code-mower/issues/1105)
are the live trackers. Board clarity #1063 is complete through merged PR #1109.
Two entry gates remain before the immutable candidate:

1. [#1104](https://github.com/codemower-ai/code-mower/issues/1104) — bounded,
   payload-aware audit comment ingestion and reserved control parsing; and
2. [CodeMower.com #978](https://github.com/codemower-ai/code-mower/issues/978)
   — deployed validation, tenant isolation, retention, export/deletion,
   aggregate counts, freshness, and exact capability advertisement.

After those gates complete, #1105 serializes the one immutable build, bounded
private Slack canary, local-versus-hosted reconciliation, 24-hour soak, two
independent installation passes, exact-head release audits, publication, and
canonical reinstall. None of those observations is claimed by this source
preparation.

## Near-Term Roadmap

`v1.4.0`, `v1.4.1` and `v1.4.2` are released and their published artifacts are
immutable. The agreed sequence from the stabilization epic ran as follows:

1. **Done.** All seven `v1.4.0` stabilization implementation children were
   accepted on main, plus
   [#974](https://github.com/codemower-ai/code-mower/issues/974) evidence
   verification. #963 was accepted through the #990/#991/#992 replacement stages
   and final PR #997 integration, not the unaccepted #989 draft.
2. **Done.** Graphify shipped together with those fixes as `v1.4.1`, closing
   release [#915](https://github.com/codemower-ai/code-mower/issues/915).
3. **Done.** Board shipped as `v1.4.2` from release commit `55339bf`, closing
   release [#952](https://github.com/codemower-ai/code-mower/issues/952). The
   included work is
   [#935](https://github.com/codemower-ai/code-mower/issues/935) and
   [#961](https://github.com/codemower-ai/code-mower/issues/961), delivered by
   PRs #956, #957, #999, #1000, #1001, #1002 and #1003. Issue
   [#951](https://github.com/codemower-ai/code-mower/issues/951)'s bounded
   hosted Devin canary was not claimed by this release; it later completed
   during v1.5.1 qualification and #951 is closed.
4. **Released.** Supervised Slack is the `v1.5.0` work
   ([#903](https://github.com/codemower-ai/code-mower/issues/903) /
   [#923](https://github.com/codemower-ai/code-mower/issues/923)). The
   preceding sequence and implementation are complete. Authoritative observed
   acceptance, publication and canonical reinstall evidence belongs on #923 and
   the GitHub Release rather than in this immutable source page.

Devin support is a bounded builder qualification only: a maintained local CLI
builder lane and the exact PR-bound hosted work-order library seam. Hosted
dispatch has no packaged CLI command. Devin review stays informational and
Devin is not a qualified peer orchestrator.

Source role-policy enforcement
([#975](https://github.com/codemower-ai/code-mower/issues/975)) separates role
qualification, transport capability, repository policy, and runtime readiness.
It rejects an unqualified Devin orchestrator before lease or session writes and
checks bounded hosted builder admission before new work. See
[Participant Qualification](participant-qualification.md). These are main-line
stabilization changes that shipped in `v1.4.1`. Effective-authority rendering
([#955](https://github.com/codemower-ai/code-mower/issues/955)) was accepted
through PR #988;
neither is part of the immutable `v1.4.0` artifact, and both are part of the
published `v1.4.1` artifact.

Each phase below was an independently gated epic rather than one cross-cutting
implementation PR. All four phases are release history.

### 1. Accepted `v1.4.0` Stabilization -- shipped in `v1.4.1` ([#979](https://github.com/codemower-ai/code-mower/issues/979))

Seven accepted implementation units plus one evidence verification, all merged
and carried into the published `v1.4.1` artifact:

- accurate advertised commands and live roadmap docs
  ([#965](https://github.com/codemower-ai/code-mower/issues/965));
- optional review defaults
  ([#967](https://github.com/codemower-ai/code-mower/issues/967));
- role-specific qualification and admission
  ([#975](https://github.com/codemower-ai/code-mower/issues/975));
- effective-authority and migration reporting
  ([#955](https://github.com/codemower-ai/code-mower/issues/955)), after #975;
- quiescent, capable, consistent takeover
  ([#962](https://github.com/codemower-ai/code-mower/issues/962));
- contributor lineage and reviewer exclusion
  ([#963](https://github.com/codemower-ai/code-mower/issues/963)), after #962
  and #975; and
- independent operational acceptance evidence
  ([#976](https://github.com/codemower-ai/code-mower/issues/976)).

#962 ran before #963 where handoff and provenance files overlapped. #974 was
evidence-only verification of existing hosted aggregate freshness; a confirmed
hosted defect would have become a separately recorded implementation child and
its own hosted PR rather than an assumed fix.

### 2. Graphify Repository Context -- foundation in `v1.4.0`, qualified integration in `v1.4.1` ([#902](https://github.com/codemower-ai/code-mower/issues/902) / release [#915](https://github.com/codemower-ai/code-mower/issues/915))

Graphify is a repository-context provider beside Coworker, not a participant.
The [evaluation record](graphify-evaluation.md) closed
[#876](https://github.com/codemower-ai/code-mower/issues/876) with an adopt
decision, and `code-mower context-graph`, described in the
[lifecycle record](context-graph-lifecycle.md), closed
[#913](https://github.com/codemower-ai/code-mower/issues/913). The query/packet
implementation ([#914](https://github.com/codemower-ai/code-mower/issues/914),
accepted through PR #982) consumes
a pinned structured JSON contract and generates bounded impact, dependency,
symbol, and related-test packets in one shape for Claude, Codex, and Devin.
Release #915 shipped that accepted source and stabilization baseline as
`v1.4.1`, completing the release-specific comparative scorecard, campaign,
Board, and fresh aggregate evidence as part of that closeout.

The published `v1.4.2` package carries that originally shipped integration
unchanged. Installation stays opt-in and outside the base dependency set, no
command requires an index to exist, and Code Mower owns refresh policy rather
than parsing human-oriented MCP prose. See
[Optional Graphify Setup](graphify-setup.md) for the current ramp-up flow.

Real-pilot compatibility fixes have since merged to `main` in
[PR #1007](https://github.com/codemower-ai/code-mower/pull/1007): a bounded
provider-manifest reader separate from the compact generation-manifest bound,
explicit refusal of an oversized provider manifest, `doc_ref` nodes as declared
non-code exclusions, and JavaScript/TypeScript test-convention and `imports`
recognition in `related_tests`. They are included in v1.5.0 together with #1031 readiness/query parity;
the historical `v1.4.2` package does not contain them. The
accepted `0.9.58` provider pin is unchanged. Because a published generation is
never rewritten in place, upgrading Code Mower repairs no generation already
built -- but only the generations those compatibility gaps actually affected
need rebuilding. A generation they left `partial`, most often an older partial
frontend generation, must be rebuilt explicitly with
`code-mower context-graph refresh`; one `code-mower context-graph status --json`
already reports usable does not.

### 3. Board Clarity And Session Visibility -- shipped in `v1.4.2` ([#945](https://github.com/codemower-ai/code-mower/issues/945) / release [#952](https://github.com/codemower-ai/code-mower/issues/952))

Board is complete at release commit `55339bf`.
[#935](https://github.com/codemower-ai/code-mower/issues/935) was completed by
[PR #973](https://github.com/codemower-ai/code-mower/pull/973). The work-first
Now/Timeline/Releases/Health views arrived in
[PR #1000](https://github.com/codemower-ai/code-mower/pull/1000), the frozen
provider-neutral observation contract and existing-data hierarchy in
[PR #956](https://github.com/codemower-ai/code-mower/pull/956) and
[PR #957](https://github.com/codemower-ai/code-mower/pull/957), exact local
work observations in
[PR #999](https://github.com/codemower-ai/code-mower/pull/999),
provider-neutral remote lifecycle observations in
[PR #1002](https://github.com/codemower-ai/code-mower/pull/1002), and
persistent Board services with stale-keepalive rejection during release restart
in [PR #1001](https://github.com/codemower-ai/code-mower/pull/1001), closing
issue [#961](https://github.com/codemower-ai/code-mower/issues/961).
Issue [#951](https://github.com/codemower-ai/code-mower/issues/951)'s
integrated qualification code -- independent head-bound evidence and
session-visibility composition -- landed in
[PR #1003](https://github.com/codemower-ai/code-mower/pull/1003).
Release [#952](https://github.com/codemower-ai/code-mower/issues/952) shipped
that baseline through
[PR #1006](https://github.com/codemower-ai/code-mower/pull/1006) and is closed.
It added no cloud field.

#951's bounded hosted Devin canary was not claimed by v1.4.2. It later completed
under the explicit v1.5.1 qualification cap, and #951 is closed.

Board is a read model over one closed local observation model. Missing or stale
evidence stays explicitly unknown or last-observed; Board never infers runtime
activity from a label, provider name, PID, PR author, lease, or command-line
prose. `code-mower board service` keeps one Board running as a supervised
launchd service on macOS and refuses every other platform; see
[Board Service Lifecycle](board-service-lifecycle.md).

### 4. Supervised Slack Task And Status Interaction -- source implementation complete for `v1.5.0` ([#903](https://github.com/codemower-ai/code-mower/issues/903) / release [#923](https://github.com/codemower-ai/code-mower/issues/923))

Slack is an interaction channel, not an orchestrator. A real qualified Codex or
other separately qualified supervisor controls bounded hosted work; v1.5.0's
accepted basic path uses Codex. Missing supervisor readiness
blocks dispatch, and selecting a provider never promotes its role. Ingress
foundations [#916](https://github.com/codemower-ai/code-mower/issues/916) and
[#917](https://github.com/codemower-ai/code-mower/issues/917) are merged and
shipped in `v1.4.0`.
The v1.5.0 public package includes the basic setup/doctor runbook (#1024),
qualified-supervisor v2 contract and checkpointed clarification/fix semantics.
The authorized private implementation provides OAuth, durable inbox/outbox,
policy bindings and the hosted supervisor bridge. #918 qualifies private
administration/readiness; #920 consumes the immutable candidate to obtain one
accepted completion and one accepted confirmed cancellation under an explicit
numeric cap while preserving every attempt and reservation; #923 records the
tag, publication and independent reinstall evidence.
The v1.6.0 source adds capability-gated, metadata-only lifecycle summaries to
the local Board and optional cloud emitter. Slack-to-Board links and rich UX
remain deferred. Slack consumes the durable lifecycle instead of scraping
terminal or Board output and carries no raw private context or private reviewer
findings.

The source implementation and qualification contract are complete. This
statement covers the basic v1.5.0 interaction boundary.
Consult #923 and the GitHub Release for the observed v1.5.0 lifecycle, canary,
publication and canonical reinstall state.

## Delivery Order

1. **Done.** `v1.4.0` stabilization completed on main: the seven #979
   implementation PRs plus the #974 evidence verification.
2. **Done.** Graphify shipped together with those fixes as `v1.4.1` through
   #915, after #914.
3. **Done.** Board shipped as `v1.4.2` through #952, after #961 and #951's
   merged local-evidence code.
4. **Done.** The supervised Slack runtime and release contract shipped in
   v1.5.0. #923 and the GitHub Release are the authoritative record of lifecycle
   evidence, capped canary outcomes, publication and reinstall.

Elapsed time, implementation difficulty, or an open draft PR never changes this
release order. Merged fixes count as on main until a later published package is
verified to contain them; #935/#973 and the phase-3 Board PRs are now verified
in the published `v1.4.2` artifact, while the merged Graphify compatibility
fixes in [PR #1007](https://github.com/codemower-ai/code-mower/pull/1007) are included in `v1.5.0`
together with the #1029 search-readiness check from merged PR #1031. That check makes `status` and
`connection-status` report `search` from the installed query reader, so a
current generation the reader cannot consume is reported as a reader mismatch
with an upgrade action rather than as searchable.

Each child issue should produce one reviewable PR with one branch writer,
independent current-head review, the normal gate, and package-level validation.
The roadmap issue and epics track the work; they should not become umbrella
implementation PRs.

## Documentation Ownership

Current OSS install, operation, privacy, and protocol documentation belongs in
this repository. Release-specific notes and completed planning records are
historical and must not be used as current installation instructions. Private
CodeMower.com deployment, OAuth, database, DNS, and service-secret procedures
belong in the hosted-service repository.

`README.md`, `docs/install.md`, `docs/try-in-10-minutes.md`,
`docs/upgrade-existing-repo.md`, `docs/quickstart.md`, and this page are the
maintained current-release entry points. Versioned release notes,
qualification contracts, runbooks, archived rehearsals, and transcripts retain
their original pins as historical evidence. When the two conflict, follow the
current-release entry points and file the mismatch as documentation drift.
