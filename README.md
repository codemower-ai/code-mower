# Code Mower

Code Mower adds a supervised operating layer around AI coding agents. It helps
teams give one builder ownership of a change, obtain independent reviews on the
current pull-request head, recover stalled work, and measure which builder and
reviewer combinations are useful on their own codebase.

The current release is supervised-pilot, bring-your-own-agent-loop software.
It is not a drop-in unattended merge gate. Humans still own credentials,
repository policy, reviewer promotion, and exceptional decisions.

The current package-index release baseline is `v1.4.2`, with pinned package
install spec `code-mower==1.4.2`. Release evidence is recorded on the GitHub
release and in the first-user install rehearsal. v1.4.2 was published from
release commit `55339bf1acf76d33be5937e80bdaad772e0b2bf5` under the annotated
`v1.4.2` tag; see the
[v1.4.2 release](https://github.com/codemower-ai/code-mower/releases/tag/v1.4.2)
and the [v1.4.2 qualification record](https://github.com/codemower-ai/code-mower/blob/main/docs/v142-qualification.md). The published
`v1.4.0` and `v1.4.1` artifacts remain unchanged. Every install command below
targets the published release.

One qualification boundary stays open and is not claimed by v1.4.2: the bounded
hosted Devin canary tracked by
[#951](https://github.com/codemower-ai/code-mower/issues/951), which needs an
explicit owner authorization before it can run.

Documentation on `main` follows the source on `main`. To read the guide exactly as
v1.4.2 shipped it, use the
[`v1.4.2` guide](https://github.com/codemower-ai/code-mower/blob/v1.4.2/docs/try-in-10-minutes.md);
the pages on `main` are the maintained current versions.

## What Code Mower Adds

You can run Claude Code, Codex, Devin, or another coding agent directly. Code
Mower supplies the shared workflow around them:

- one orchestrator and one writer per branch;
- provider-neutral work orders, handoffs, status, and recovery;
- independent, structured reviews bound to the current PR head;
- repository-specific calibration before a reviewer gains merge authority;
- local reports for quality, latency, cost, interventions, and outcomes;
- setup diagnostics for provider auth, GitHub permissions, and private-repo
  Actions cost; and
- optional, explicitly authorized organizational context and metadata sharing.

The default setup is deliberately small: Claude Code and Codex. Additional
participants are opt-in and keep their own execution, privacy, cost, and
promotion requirements.

## Start Here

Code Mower requires Python 3.12 or newer. A laptop or workstation should use
one stable `pipx` installation:

```bash
python3.12 --version
export CODE_MOWER_PYTHON="$(command -v python3.12)"
pipx install --python "$CODE_MOWER_PYTHON" code-mower==1.4.2
command -v code-mower
code-mower --version
```

`command -v code-mower` should print the path you expect and `code-mower
--version` should print `code-mower 1.4.2` before you point Code Mower at a
repository. If you do not have pipx, install it from the
[official pipx installation guide](https://pipx.pypa.io/stable/installation/).

Hosted agents and CI machines can use `uv tool install` after installing uv
from the [official uv installation guide](https://docs.astral.sh/uv/getting-started/installation/);
contributors should use `scripts/dev-python` and an editable virtual
environment. The [Install And Bootstrap](https://github.com/codemower-ai/code-mower/blob/main/docs/install.md) guide gives the exact
cold-install, upgrade, optional Coworker, and contributor commands.

From the repository you want to pilot:

```bash
code-mower init --easy
code-mower init --easy --apply --output-dir .code-mower.generated
code-mower doctor --adoption --repo OWNER/REPO
code-mower lanes status --repo OWNER/REPO
```

`init --easy` previews the Claude + Codex setup and changes nothing. `--apply`
writes a reviewable generated tree; it does not copy those files into your
repository, start a provider, enable auto-merge, or upload data. Review and
edit the generated configuration before opening the setup PR.

To inspect representative output first, use the
[synthetic calibration example](https://github.com/codemower-ai/code-mower/blob/main/examples/demo-calibration/README.md) and
[Board demo](https://github.com/codemower-ai/code-mower/blob/main/examples/board-demo/README.md).

Follow [Try Code Mower In 10 Minutes](https://github.com/codemower-ai/code-mower/blob/main/docs/try-in-10-minutes.md) to run Codex
and Claude manually against that first PR. Automation tokens, recurring
dispatch, branch-protection changes, and auto-merge are not prerequisites for
the manual reviewer-gate pilot.

After the first peer-review loop works, follow
[Build Loop In 30 Minutes](https://github.com/codemower-ai/code-mower/blob/main/docs/build-loop-in-30-minutes.md) to add automated
builder dispatch and the stricter promoted-pilot repository settings.

## Participants And Sessions

Choose participants interactively or explicitly:

```bash
code-mower init --interactive
code-mower init --with claude,codex,devin
```

Both commands preview by default. Add `--apply` only when you are ready to
write the generated setup.

The agent that starts a session is the implicit orchestrator:

```bash
code-mower session start \
  --repo OWNER/REPO \
  --with claude,codex,devin \
  --host codex
```

`session start` saves an operating brief and takes the local single-orchestrator
lease. It does not launch every selected product. The host uses the execution
paths available for each participant and records explicit handoffs where Code
Mower has no automatic transport. Later, from anywhere in the checkout,
`code-mower session show --current` finds the brief the live lease names and
`code-mower session lease show` reports the lease itself; neither changes state.
The default is 12 hours. A later process can renew or release the same session
ID with `session lease renew --session-id SESSION_ID` or `session lease release
--session-id SESSION_ID`; stop its writers before release. Use `session start
--dry-run` or `--no-lease` for read-only work.

Codex, Claude Code, and Cursor are qualified for the shared session, telemetry,
lease, and Jira-authority contract in v1.4.2. Devin, Grok Bot, Antigravity,
Muse, and custom hosts are recognized for briefs and provenance, while their
execution remains an explicit handoff or provider-specific transport. See
[Participants And Sessions](https://github.com/codemower-ai/code-mower/blob/main/docs/sessions.md) and the
[Provider Matrix](https://github.com/codemower-ai/code-mower/blob/main/docs/provider-matrix.md).

## Optional Organizational Context

The base installation works without an organizational-memory provider. The
optional Coworker integration gives approved Claude, Codex, and Devin roles the same
bounded, cited evidence through an explicitly selected private account. Account
identity and credentials stay outside the repository.

After installing the optional dependencies and connecting an approved account,
the guided path carries one work item through retrieval, builder delivery,
current-head attachment, independent review, and private feedback:

```bash
code-mower session start --repo OWNER/REPO --host codex --work-item EXAMPLE-123
code-mower session context prepare .code-mower/sessions/SESSION.json
code-mower session context deliver .code-mower/sessions/SESSION.json
code-mower session context attach .code-mower/sessions/SESSION.json --pr 42
code-mower session context feedback .code-mower/sessions/SESSION.json \
  --reviewer claude
```

Every delivery and feedback read reauthorizes online. Private evidence and
private review findings must not be copied into tracked files or public logs.
Start with [Optional Organizational Context Setup](https://github.com/codemower-ai/code-mower/blob/main/docs/context-setup.md).

## Roles

- **Orchestrator:** the hosting agent by default; coordinates the work item,
  assignments, evidence, reviews, recovery, and owner actions.
- **Builder:** the only writer for its PR branch.
- **Reviewer:** an independent current-head audit. A builder's own review does
  not satisfy the peer-review requirement.
- **Merge authority:** repository policy. Selecting a participant never grants
  it.
- **Context provider:** supplies approved evidence; it cannot build, review,
  orchestrate, write to the tracker, or merge.

Code Mower records builder provenance for Claude Code, Codex, Cursor-style
hosted builders, Devin, and other authoring lanes. Its generated templates now
support the supervised issue-to-merge loop end to end, with provider-specific
adapters handling product differences.

## What Calibration Does And Does Not Prove

The generated starter corpus proves that the command and report paths work. It
does not prove that a reviewer should gate merges.

Bootstrap a draft from recent repository history:

```bash
code-mower calibration auto-discover \
  --repo OWNER/REPO \
  --last-n 20 \
  --output .code-mower/draft-calibration-corpus.json
```

Review every proposed disposition. Promote a lane only after known-clean and
known-blocked evidence satisfies the
[lane promotion policy](https://github.com/codemower-ai/code-mower/blob/main/docs/lane-promotion-policy.md).

## Local Status And Board

`lanes status` is the concise terminal view. The optional Board serves the same
redacted metadata on loopback:

```bash
code-mower lanes status --repo OWNER/REPO
code-mower productivity report --repo OWNER/REPO
code-mower board serve --repo OWNER/REPO
```

Plain `board serve` is read-only. Add `--record-events` to append throttled,
metadata-only local history:

```bash
code-mower board serve --repo OWNER/REPO --record-events
code-mower board record --repo OWNER/REPO
code-mower board events
code-mower board doctor --repo OWNER/REPO
code-mower board reset --repo OWNER/REPO --yes
```

The Board includes the owner queue, reviewer verdict history and spend/latency
when those local inputs exist. It binds to loopback only. Local paths are
redacted by default. The Board does not upload data.

To keep one Board running across logout and reboot, install it as a persistent
local service. macOS is the supported platform, through launchd; every other
platform refuses rather than calling a transient process a service:

```bash
code-mower board service render --repo OWNER/REPO --repo-path . --port 5332
code-mower board service install --repo OWNER/REPO --repo-path . --port 5332
code-mower board service status --json
code-mower board service restart --repo OWNER/REPO --repo-path . --port 5332
code-mower board service remove --repo OWNER/REPO --yes
```

`render` prints the exact definition before anything is applied. `board stop
--repo OWNER/REPO` resolves one exact known binding and refuses an ambiguous,
duplicate, or contradicting selection, and it refuses a port a keepalive-managed
service would immediately reclaim. See
[Board Service Lifecycle](https://github.com/codemower-ai/code-mower/blob/main/docs/board-service-lifecycle.md) for the serving gate,
delayed health, and the fail-closed refusals.

## Optional Cloud Sharing

The OSS package works without CodeMower.com. Cloud sharing is an explicit,
dry-run-first path for sanitized metadata and selected reports:

```bash
code-mower cloud dogfood --json
code-mower cloud board-snapshot --repo-slug OWNER/REPO --json
```

Neither command uploads without `--yes`. Default bundles exclude source code,
raw diffs, model transcripts, raw stdout/stderr, auth output, issue body text,
local secret values, and secrets. See [Cloud Sharing](https://github.com/codemower-ai/code-mower/blob/main/docs/cloud-sharing.md)
and the [Cloud Data Contract](https://github.com/codemower-ai/code-mower/blob/main/docs/cloud-data-contract.md).

## Current Capabilities And Limits

| Area | v1.4.2 posture |
| --- | --- |
| Default builders and reviewers | Claude Code + Codex |
| Session hosts | Codex, Claude Code, and Cursor qualified; other identities recognized but require explicit handoff/provider transport |
| Devin | Maintained local builder lane and an exact PR-bound hosted work-order library seam (`code_mower.devin_work_orders`, no packaged CLI command); local review remains informational and Devin is not yet a qualified peer orchestrator |
| Organizational context | Optional Coworker delivery to approved Claude/Codex/Devin roles |
| Work trackers | GitHub Issues by default; Jira Cloud is optional and guarded |
| Forge and merge gate | GitHub |
| Cloud | Optional metadata/report upload; no upload by default |
| Graphify | Optional bounded local repository-graph provider behind the packet contract; no default dependency and no network access for the provider |
| Slack | Command and authenticated bounded ingress foundation only; no Slack worker delivery, results, or orchestration authority |

GitLab, Bitbucket, broad unattended rollout, uncalibrated merge gates, Devin
peer-orchestrator/reviewer parity, a hosted work-order CLI, a required Graphify
dependency, and Slack worker delivery are outside v1.4.2. The current priorities
and boundaries are recorded in
[Current State And Roadmap](https://github.com/codemower-ai/code-mower/blob/main/docs/current-state-and-roadmap.md).

## Optional Repository Context Graph

Graphify shipped in v1.4.1 as an optional local repository-graph provider and
remains available in v1.4.2. It is separately installed into an operator-owned
environment, explicitly activated, and outside the base dependency set: a
default Claude + Codex install adds no Graphify dependency, no indexer, no
background service, and no watcher.

```bash
code-mower init --graphify
code-mower context-graph doctor
code-mower context-graph status --json
```

`init --graphify` only renders acquisition and pin guidance; it installs and
indexes nothing. Start with
[Optional Graphify Setup](https://github.com/codemower-ai/code-mower/blob/main/docs/graphify-setup.md) for the acquisition and
ramp-up flow, [Local Repository Graph Lifecycle](https://github.com/codemower-ai/code-mower/blob/main/docs/context-graph-lifecycle.md)
for what a build is allowed to see and where its state lives, and
[Bounded Queries And Context Packets](https://github.com/codemower-ai/code-mower/blob/main/docs/context-graph-queries.md) for the
four questions and the packet contract.

The published v1.4.2 package contains that originally shipped integration.
Further real-pilot compatibility fixes -- a bounded provider-manifest reader,
`doc_ref` exclusions, and JavaScript/TypeScript test-convention recognition --
merged to `main` after the release and are intended for the next appropriate
release. The accepted `0.9.58` provider pin is unchanged, and a generation built
before that release has to be rebuilt explicitly; upgrading does not repair one.
See [Optional Graphify Setup](https://github.com/codemower-ai/code-mower/blob/main/docs/graphify-setup.md#published-v142-versus-current-main).

## Documentation

### Install And First Use

- [Install And Bootstrap](https://github.com/codemower-ai/code-mower/blob/main/docs/install.md)
- [Try Code Mower In 10 Minutes](https://github.com/codemower-ai/code-mower/blob/main/docs/try-in-10-minutes.md)
- [Upgrade An Existing Repository](https://github.com/codemower-ai/code-mower/blob/main/docs/upgrade-existing-repo.md)
- [Quickstart Reference](https://github.com/codemower-ai/code-mower/blob/main/docs/quickstart.md)
- [Troubleshooting](https://github.com/codemower-ai/code-mower/blob/main/docs/troubleshooting.md)
- [First Run Transcript](https://github.com/codemower-ai/code-mower/blob/main/docs/first-run-transcript.md) (v1.4.0 illustrative shape, not the current v1.4.2 pin)

### Local Board And Repository Context

- [Board Data Contract](https://github.com/codemower-ai/code-mower/blob/main/docs/board-data-contract.md)
- [Board Service Lifecycle](https://github.com/codemower-ai/code-mower/blob/main/docs/board-service-lifecycle.md)
- [Board Demo Rehearsal](https://github.com/codemower-ai/code-mower/blob/main/examples/board-demo/README.md)
- [Optional Graphify Setup](https://github.com/codemower-ai/code-mower/blob/main/docs/graphify-setup.md)
- [Local Repository Graph Lifecycle](https://github.com/codemower-ai/code-mower/blob/main/docs/context-graph-lifecycle.md)
- [Bounded Queries And Context Packets](https://github.com/codemower-ai/code-mower/blob/main/docs/context-graph-queries.md)
- [Graphify Evaluation Record](https://github.com/codemower-ai/code-mower/blob/main/docs/graphify-evaluation.md) (historical decision, 2026-09-12)

### Sessions, Builders, And Reviewers

- [Participants And Sessions](https://github.com/codemower-ai/code-mower/blob/main/docs/sessions.md)
- [Build Loop In 30 Minutes](https://github.com/codemower-ai/code-mower/blob/main/docs/build-loop-in-30-minutes.md)
- [Build Loop Operations](https://github.com/codemower-ai/code-mower/blob/main/docs/build-loop.md)
- [Planning And Work Orders](https://github.com/codemower-ai/code-mower/blob/main/docs/planning-work-orders.md)
- [Builder Experiments](https://github.com/codemower-ai/code-mower/blob/main/docs/builder-experiments.md)
- [Orchestrator Prompt Pack](https://github.com/codemower-ai/code-mower/blob/main/docs/orchestrator-prompt-pack.md)
- [Provider Matrix](https://github.com/codemower-ai/code-mower/blob/main/docs/provider-matrix.md)
- [Provider Calibration Scorecard](https://github.com/codemower-ai/code-mower/blob/main/docs/provider-calibration-scorecard.md)
- [Devin Peer-Support Qualification](https://github.com/codemower-ai/code-mower/blob/main/docs/devin-peer-support-qualification.md)
- [Lane Standing Instructions](https://github.com/codemower-ai/code-mower/blob/main/docs/lanes/README.md)
- [Codex Lane](https://github.com/codemower-ai/code-mower/blob/main/docs/lanes/codex.md)
- [Claude Lane](https://github.com/codemower-ai/code-mower/blob/main/docs/lanes/claude.md)
- [Cursor Lane](https://github.com/codemower-ai/code-mower/blob/main/docs/lanes/cursor.md)
- [Devin Lane](https://github.com/codemower-ai/code-mower/blob/main/docs/lanes/devin.md)
- [Self-Hosted Mac Runner](https://github.com/codemower-ai/code-mower/blob/main/docs/self-hosted-mac-runner.md)
- [Local Audit Runner](https://github.com/codemower-ai/code-mower/blob/main/docs/local-audit-runner.md)

### Context And Trackers

- [Optional Organizational Context Setup](https://github.com/codemower-ai/code-mower/blob/main/docs/context-setup.md)
- [Coworker Connections](https://github.com/codemower-ai/code-mower/blob/main/docs/context-connections.md)
- [Context Delivery And Private Review](https://github.com/codemower-ai/code-mower/blob/main/docs/context-delivery.md)
- [Context Provider Contract](https://github.com/codemower-ai/code-mower/blob/main/docs/context-provider-contract.md)
- [Context Packet Schema](https://github.com/codemower-ai/code-mower/blob/main/docs/context-packet-schema.md)
- [Jira Cloud Setup](https://github.com/codemower-ai/code-mower/blob/main/docs/jira-cloud-setup.md)
- [Jira Adoption Rehearsal](https://github.com/codemower-ai/code-mower/blob/main/docs/jira-adoption-rehearsal.md)
- [Work Tracker Data Contract](https://github.com/codemower-ai/code-mower/blob/main/docs/tracker-data-contract.md)

### Trust, Operations, And Project Records

- [Architecture](https://github.com/codemower-ai/code-mower/blob/main/docs/architecture.md)
- [Current State And Roadmap](https://github.com/codemower-ai/code-mower/blob/main/docs/current-state-and-roadmap.md)
- [Lane Promotion Policy](https://github.com/codemower-ai/code-mower/blob/main/docs/lane-promotion-policy.md)
- [Privacy And Threat Model](https://github.com/codemower-ai/code-mower/blob/main/docs/privacy-threat-model.md)
- [Cloud Data Contract](https://github.com/codemower-ai/code-mower/blob/main/docs/cloud-data-contract.md)
- [Release Qualification](https://github.com/codemower-ai/code-mower/blob/main/docs/release-qualification.md)
- [Public Release Checklist](https://github.com/codemower-ai/code-mower/blob/main/docs/public-release-checklist.md)
- [v1.4.2 Release Notes](https://github.com/codemower-ai/code-mower/blob/main/docs/v142-release-notes.md)
- [v1.4.2 Qualification Record](https://github.com/codemower-ai/code-mower/blob/main/docs/v142-qualification.md)
- [Release History And Archived Plans](https://github.com/codemower-ai/code-mower/blob/main/docs/release-history.md)
- [Changelog](https://github.com/codemower-ai/code-mower/blob/main/CHANGELOG.md)
- [Contributing](https://github.com/codemower-ai/code-mower/blob/main/CONTRIBUTING.md)
- [Support](https://github.com/codemower-ai/code-mower/blob/main/SUPPORT.md)
- [Security Policy](https://github.com/codemower-ai/code-mower/blob/main/SECURITY.md)
- [Code of Conduct](https://github.com/codemower-ai/code-mower/blob/main/CODE_OF_CONDUCT.md)

## License

The Code Mower open-source core is licensed under Apache-2.0. Hosted reporting,
managed integrations, private telemetry and benchmark products, enterprise
controls, and support are separate commercial surfaces unless licensed
otherwise.
