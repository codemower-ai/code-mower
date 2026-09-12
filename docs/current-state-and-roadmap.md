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

## Current Public Release

The current package-index release baseline is `v1.3.1`, with pinned package
install spec `code-mower==1.3.1`. Release evidence is recorded on the GitHub
release and in the first-user install rehearsal.

Version 1.3.1 requires Python 3.12 or newer. It provides:

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

The v1.3.1 guided context workflow derives repository, work item, selected
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
| Recognized session hosts | Devin, Grok Bot, Antigravity, Muse, and custom identities; explicit handoff/provider transport required |
| First-class local builders | Codex and Claude; maintained Devin builder lane is opt-in |
| Hosted builders | Explicit provider-specific dispatch and provenance; no implicit trust or merge authority |
| Merge-eligible reviewers | Codex and Claude after repository setup and calibration |
| Informational reviewers | Devin CLI and other optional providers until their evidence supports promotion |
| Organizational context | Optional Coworker packets for approved Claude/Codex orchestrator, builder, and reviewer roles |
| Repository context graph | Provider-neutral packet extension and offline scope/freshness checks exist; Graphify is adopted as an optional bounded provider but not yet installed or shipped |
| Work tracking | GitHub Issues by default; Jira Cloud optional, bounded, and dry-run-first for writes |
| Team interaction | CLI, GitHub, local Board, and optional CodeMower.com metadata views; no Slack ingress yet |

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
- Private Coworker delivery is limited to explicitly approved Claude and Codex
  roles in v1.3.1.
- Graphify and Slack are not included in v1.3.1.
- Provider cost fields remain unknown when the provider does not return them.
- A successful release campaign proves installation and operational transport,
  not builder quality or reviewer promotion readiness.
- Auto-discovered calibration cases are proposals that require human
  adjudication.
- Broad unattended rollout and uncalibrated merge gates are outside the current
  product posture.

## Near-Term Roadmap

The next three capabilities should ship as independently gated epics rather
than one cross-cutting implementation PR.

### 1. Devin Peer Support

Make Devin implement the same user-facing participant lifecycle as Claude and
Codex while keeping local CLI and hosted API mechanics inside separate
transports. The target includes:

- session dispatch, progress, messaging, cancellation, result collection, and
  recovery;
- qualification as a host/orchestrator;
- Coworker and later Graphify context delivery;
- structured current-head reviewer output; and
- a clean/blocked calibration campaign before any reviewer promotion.

The maintained local builder and hosted Sessions API work are the starting
point. Reviewer authority remains evidence-based.

### 2. Graphify Repository Context

Treat Graphify as a repository-context provider beside Coworker, not as a
participant. Start local and code-only:

- build the optional local provider against the conditions in the
  [evaluation record](graphify-evaluation.md), which closes
  [issue #876](https://github.com/codemower-ai/code-mower/issues/876) with an
  adopt decision;
- add a provider registry and multiple context attachments per session;
- build and refresh graphs with commit/freshness validation;
- consume a pinned structured JSON contract;
- generate bounded impact, dependency, symbol, and related-test packets; and
- deliver the same packet shape to Claude, Codex, and Devin.

Code Mower should own refresh policy and should not rely permanently on parsing
human-oriented MCP prose.

### 3. Slack Task And Status Interaction

Treat Slack as an interaction channel, not an orchestrator. A Slack-started
session uses the project or channel's configured default orchestrator unless the
request supplies an explicit one. The first local integration should use Socket
Mode and provide:

- allowlisted workspace, channel, user, and repository mappings;
- idempotent task creation and Slack-thread-to-session binding;
- redacted progress and completion updates;
- clarification questions with reply-to-resume behavior;
- cancellation and restart reconciliation; and
- no raw private context or private reviewer findings in Slack.

Slack should consume the durable session lifecycle and event surface introduced
for Devin rather than scrape terminal or Board output.

## Delivery Order

1. Ship Devin lifecycle, host, context-recipient, and reviewer parity first.
2. The bounded Graphify evaluation is complete and adopted; ship the optional
   local context provider after the shared context registry is stable, against
   the conditions in the evaluation record. Installation stays opt-in and no
   command requires an index to exist.
3. Define Slack's command and identity contract in parallel, but merge its
   worker only after session lifecycle and recovery are stable.

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
