# Code Mower

Code Mower adds a supervised operating layer around AI coding agents. It helps
teams give one builder ownership of a change, obtain independent reviews on the
current pull-request head, recover stalled work, and measure which builder and
reviewer combinations are useful on their own codebase.

The current release is supervised-pilot, bring-your-own-agent-loop software.
It is not a drop-in unattended merge gate. Humans still own credentials,
repository policy, reviewer promotion, and exceptional decisions.

The current package-index release baseline is `v1.3.1`, with pinned package
install spec `code-mower==1.3.1`. Release evidence is recorded on the GitHub
release and in the first-user install rehearsal.

Documentation on `main` follows the source on `main`. When using the published
package, start with the
[`v1.3.1` guide](https://github.com/codemower-ai/code-mower/blob/v1.3.1/docs/try-in-10-minutes.md).

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
pipx install --python "$CODE_MOWER_PYTHON" code-mower==1.3.1
command -v code-mower
code-mower --version
```

Hosted agents and CI machines can use `uv tool install`; contributors should
use `scripts/dev-python` and an editable virtual environment. The
[Install And Bootstrap](docs/install.md) guide gives the exact cold-install,
upgrade, optional Coworker, and contributor commands.

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
[synthetic calibration example](examples/demo-calibration/README.md) and
[Board demo](examples/board-demo/README.md).

Follow [Try Code Mower In 10 Minutes](docs/try-in-10-minutes.md) to run Codex
and Claude manually against that first PR. Automation tokens, recurring
dispatch, branch-protection changes, and auto-merge are not prerequisites for
the manual reviewer-gate pilot.

After the first peer-review loop works, follow
[Build Loop In 30 Minutes](docs/build-loop-in-30-minutes.md) to add automated
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
Mower has no automatic transport.

Codex, Claude Code, and Cursor are qualified for the shared session, telemetry,
lease, and Jira-authority contract in v1.3.1. Devin, Grok Bot, Antigravity,
Muse, and custom hosts are recognized for briefs and provenance, while their
execution remains an explicit handoff or provider-specific transport. See
[Participants And Sessions](docs/sessions.md) and the
[Provider Matrix](docs/provider-matrix.md).

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
Start with [Optional Organizational Context Setup](docs/context-setup.md).

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
[lane promotion policy](docs/lane-promotion-policy.md).

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
when those local inputs exist. Local paths are redacted by default. The Board
does not upload data.

## Optional Cloud Sharing

The OSS package works without CodeMower.com. Cloud sharing is an explicit,
dry-run-first path for sanitized metadata and selected reports:

```bash
code-mower cloud dogfood --json
code-mower cloud board-snapshot --repo-slug OWNER/REPO --json
```

Neither command uploads without `--yes`. Default bundles exclude source code,
raw diffs, model transcripts, raw stdout/stderr, auth output, issue body text,
local secret values, and secrets. See [Cloud Sharing](docs/cloud-sharing.md)
and the [Cloud Data Contract](docs/cloud-data-contract.md).

## Current Capabilities And Limits

| Area | v1.3.1 posture |
| --- | --- |
| Default builders and reviewers | Claude Code + Codex |
| Session hosts | Codex, Claude Code, and Cursor qualified; other identities recognized but require explicit handoff/provider transport |
| Devin | Maintained local builder and hosted release-qualification transport; local review remains informational and Devin is not yet a qualified peer orchestrator |
| Organizational context | Optional Coworker delivery to approved Claude/Codex roles |
| Work trackers | GitHub Issues by default; Jira Cloud is optional and guarded |
| Forge and merge gate | GitHub |
| Cloud | Optional metadata/report upload; no upload by default |
| Graphify and Slack | Tracked future integrations; not included in v1.3.1 |

GitLab, Bitbucket, broad unattended rollout, uncalibrated merge gates, Devin
peer-orchestrator/reviewer parity, Graphify, and Slack task ingress are not
shipped in v1.3.1. The current priorities and boundaries are recorded in
[Current State And Roadmap](docs/current-state-and-roadmap.md).

## Documentation

### Install And First Use

- [Install And Bootstrap](docs/install.md)
- [Try Code Mower In 10 Minutes](docs/try-in-10-minutes.md)
- [Upgrade An Existing Repository](docs/upgrade-existing-repo.md)
- [Quickstart Reference](docs/quickstart.md)
- [Troubleshooting](docs/troubleshooting.md)
- [First Run Transcript](docs/first-run-transcript.md)

### Sessions, Builders, And Reviewers

- [Participants And Sessions](docs/sessions.md)
- [Build Loop In 30 Minutes](docs/build-loop-in-30-minutes.md)
- [Build Loop Operations](docs/build-loop.md)
- [Planning And Work Orders](docs/planning-work-orders.md)
- [Builder Experiments](docs/builder-experiments.md)
- [Orchestrator Prompt Pack](docs/orchestrator-prompt-pack.md)
- [Provider Matrix](docs/provider-matrix.md)
- [Provider Calibration Scorecard](docs/provider-calibration-scorecard.md)
- [Devin Peer-Support Qualification](docs/devin-peer-support-qualification.md)
- [Lane Standing Instructions](docs/lanes/README.md)
- [Codex Lane](docs/lanes/codex.md)
- [Claude Lane](docs/lanes/claude.md)
- [Cursor Lane](docs/lanes/cursor.md)
- [Devin Lane](docs/lanes/devin.md)
- [Self-Hosted Mac Runner](docs/self-hosted-mac-runner.md)
- [Local Audit Runner](docs/local-audit-runner.md)

### Context And Trackers

- [Optional Organizational Context Setup](docs/context-setup.md)
- [Coworker Connections](docs/context-connections.md)
- [Context Delivery And Private Review](docs/context-delivery.md)
- [Context Provider Contract](docs/context-provider-contract.md)
- [Context Packet Schema](docs/context-packet-schema.md)
- [Jira Cloud Setup](docs/jira-cloud-setup.md)
- [Jira Adoption Rehearsal](docs/jira-adoption-rehearsal.md)
- [Work Tracker Data Contract](docs/tracker-data-contract.md)

### Trust, Operations, And Project Records

- [Architecture](docs/architecture.md)
- [Current State And Roadmap](docs/current-state-and-roadmap.md)
- [Lane Promotion Policy](docs/lane-promotion-policy.md)
- [Privacy And Threat Model](docs/privacy-threat-model.md)
- [Board Data Contract](docs/board-data-contract.md)
- [Cloud Data Contract](docs/cloud-data-contract.md)
- [Release Qualification](docs/release-qualification.md)
- [Public Release Checklist](docs/public-release-checklist.md)
- [Release History And Archived Plans](docs/release-history.md)
- [Changelog](CHANGELOG.md)
- [Contributing](CONTRIBUTING.md)
- [Support](SUPPORT.md)
- [Security Policy](SECURITY.md)
- [Code of Conduct](CODE_OF_CONDUCT.md)

## License

The Code Mower open-source core is licensed under Apache-2.0. Hosted reporting,
managed integrations, private telemetry and benchmark products, enterprise
controls, and support are separate commercial surfaces unless licensed
otherwise.
