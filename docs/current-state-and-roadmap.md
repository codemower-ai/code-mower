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

## Current Source Candidate And Published Baseline

The current source candidate is `v1.4.1`, with target install spec
`code-mower==1.4.1`. Publication and installed-package qualification are pending
[#915](https://github.com/codemower-ai/code-mower/issues/915).

The published v1.4.0 baseline requires Python 3.12 or newer. It provides:

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
| Team interaction | CLI, GitHub, local Board, and optional CodeMower.com metadata views; Slack has an authenticated bounded ingress foundation with no worker delivery |

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
- credentials and secret values; and
- private Coworker evidence and account bindings.

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
  Devin roles in v1.4.0.
- Graphify remains an optional bounded provider with no default dependency, and
  Slack is an ingress foundation only: v1.4.0 delivers no Slack worker results.
- Provider cost fields remain unknown when the provider does not return them.
- A successful release campaign proves installation and operational transport,
  not builder quality or reviewer promotion readiness.
- Auto-discovered calibration cases are proposals that require human
  adjudication.
- Broad unattended rollout and uncalibrated merge gates are outside the current
  product posture.

## Near-Term Roadmap

`v1.4.0` is released and its published artifacts are immutable. The agreed
sequence from the stabilization epic is:

1. Accepted on main: all seven `v1.4.0` stabilization implementation children
   plus [#974](https://github.com/codemower-ai/code-mower/issues/974) evidence
   verification. #963 is accepted through the #990/#991/#992 replacement stages
   and final #997 integration, not the unaccepted #989 draft.
2. Ship those fixes together with Graphify as `v1.4.1`
   ([#915](https://github.com/codemower-ai/code-mower/issues/915)).
3. Ship Board as `v1.4.2`. Board work is underway;
   [#935](https://github.com/codemower-ai/code-mower/issues/935) is complete.
4. Supervised Slack remains planned for `v1.5.0`; its runtime work is deferred
   until the sequence above is complete.

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
stabilization changes awaiting the next package. Effective-authority rendering
([#955](https://github.com/codemower-ai/code-mower/issues/955)) is accepted through #988;
neither is part of the immutable `v1.4.0` artifact.

Each step below is an independently gated epic rather than one cross-cutting
implementation PR.

### 1. Accepted `v1.4.0` Stabilization ([#979](https://github.com/codemower-ai/code-mower/issues/979))

Seven accepted main-only implementation units plus one evidence verification:

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

#962 runs before #963 where handoff and provenance files overlap. #974 is
evidence-only verification of existing hosted aggregate freshness; a confirmed
hosted defect becomes a separately recorded implementation child and its own
hosted PR rather than an assumed fix.

### 2. Graphify Repository Context — `v1.4.1` ([#902](https://github.com/codemower-ai/code-mower/issues/902) / release [#915](https://github.com/codemower-ai/code-mower/issues/915))

Graphify is a repository-context provider beside Coworker, not a participant.
Its runtime source is accepted on main: the
[evaluation record](graphify-evaluation.md) closed
[#876](https://github.com/codemower-ai/code-mower/issues/876) with an adopt
decision, and `code-mower context-graph`, described in the
[lifecycle record](context-graph-lifecycle.md), closed
[#913](https://github.com/codemower-ai/code-mower/issues/913). The query/packet implementation
([#914](https://github.com/codemower-ai/code-mower/issues/914), accepted through
#982) consumes
a pinned structured JSON contract and generates bounded impact, dependency,
symbol, and related-test packets in one shape for Claude, Codex, and Devin.
Release #915 prepares that accepted source and stabilization baseline for the
package. The release-specific comparative scorecard, installed artifacts,
publication, campaign, Board and fresh aggregate evidence remain pending.

Installation stays opt-in, no command requires an index to exist, and Code
Mower owns refresh policy rather than parsing human-oriented MCP prose.

### 3. Board Clarity And Session Visibility — `v1.4.2` ([#945](https://github.com/codemower-ai/code-mower/issues/945) / release [#952](https://github.com/codemower-ai/code-mower/issues/952))

Board implementation is underway rather than unstarted.
[#935](https://github.com/codemower-ai/code-mower/issues/935) is complete and
merged with [#973](https://github.com/codemower-ai/code-mower/issues/973);
[#956](https://github.com/codemower-ai/code-mower/issues/956) and
[#957](https://github.com/codemower-ai/code-mower/issues/957) are drafts behind
main that need refreshing before review. Remaining work is presentation and
producers, persistent Board services
([#961](https://github.com/codemower-ai/code-mower/issues/961)), and integrated
qualification ([#951](https://github.com/codemower-ai/code-mower/issues/951)),
then the release PR #952. #961 is required before #951 and #952, and #951
consumes #975, #955, #962, #963, and #976 through its integration dependencies.

Board is a read model over one closed local observation model. Missing or stale
evidence stays explicitly unknown or last-observed; Board never infers runtime
activity from a label, provider name, PID, PR author, lease, or command-line
prose.

### 4. Supervised Slack Task And Status Interaction — `v1.5.0` ([#903](https://github.com/codemower-ai/code-mower/issues/903) / release [#923](https://github.com/codemower-ai/code-mower/issues/923))

Slack is an interaction channel, not an orchestrator. A real qualified Codex or
Claude supervisor controls bounded hosted work: missing supervisor readiness
blocks dispatch, and selecting a provider never promotes its role. Ingress
foundations [#916](https://github.com/codemower-ai/code-mower/issues/916) and
[#917](https://github.com/codemower-ai/code-mower/issues/917) are merged.
Remaining work is OAuth, the qualified-supervisor adapter
([#977](https://github.com/codemower-ai/code-mower/issues/977)), durable
interactions, the bridge, paired telemetry, setup
([#922](https://github.com/codemower-ai/code-mower/issues/922)), and release
acceptance #923. Slack consumes the durable session lifecycle and event surface
rather than scraping terminal or Board output, and carries no raw private
context or private reviewer findings.

This runtime work is deferred until the sequence above is complete. Board
readiness gates only Slack's end-to-end canary and final acceptance in #923; it
does not block independent Slack OAuth, inbox, interaction, bridge, setup, or
documentation work.

## Delivery Order

1. Complete `v1.4.0` stabilization on main: the seven #979 implementation PRs
   plus the #974 evidence verification.
2. Ship those main-only fixes together with Graphify as `v1.4.1` through #915,
   after #914.
3. Ship Board as `v1.4.2` through #952, after #961 and #951.
4. Merge the supervised Slack runtime last, accepted in #923 for `v1.5.0`.

Elapsed time, implementation difficulty, or an open draft PR never changes this
release order. An explicit evidence-backed Graphify deferral recorded in
#915/#902 may satisfy that one dependency. Merged post-`v1.4.0` fixes, including
#935/#973, count as on main until a later published package is verified to
contain them.

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
