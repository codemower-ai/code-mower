# Code Mower

Code Mower helps teams set up AI peer-programmer and reviewer lanes on real
GitHub pull requests, then measure which builders and reviewers are useful on
their actual codebase.

Code Mower is beta, bring-your-own-agent-loop software for teams willing to
calibrate reviewers. It is not a drop-in autonomous merge gate.

The short version:

- create safe, manual-first reviewer lanes for Codex, Claude, Gitar, and other
  AI review tools;
- run setup diagnostics before a lane can surprise you with spend, source
  exposure, or GitHub Actions churn;
- turn issue text, external docs, and project doctrine into local work orders
  before an agent starts coding;
- generate local reviewer value reports from known-clean and known-blocked PRs;
  and
- optionally share sanitized metadata with [CodeMower.com](https://codemower.com)
  for private team dashboards today and aggregate benchmarks as that dataset
  becomes useful.

Code Mower is local-first. The OSS tool works without the hosted service.
Default cloud bundles exclude source code, raw diffs, raw model transcripts,
raw stdout/stderr, auth output, and secrets.

## Design Principles

Code Mower should feel like an engineering tool, not a demo harness:

- **Local first:** install, diagnose, audit, and report without a hosted
  account.
- **Manual first:** new reviewer lanes start explicit and observable before
  they can affect merge policy.
- **Evidence first:** promote lanes from real calibration data on your
  repository, not from generic benchmark claims.
- **Privacy first:** cloud sharing is opt-in metadata by default, with source,
  diffs, transcripts, auth output, and secrets excluded.
- **Composable by design:** providers, lenses, context packs, calibration, and
  cloud upload stay separate so teams can adopt only the parts they trust.

## What It Looks Like

`code-mower doctor --adoption --repo OWNER/REPO` is the first useful command
for a real repository. It checks your runtime, GitHub setup, provider CLIs,
token posture, optional cloud setup, private-repo Actions cost traps, and
first-run adoption gaps. `--preflight` remains the compatibility preset for
older scripts; `--adoption` adds explicit repo targeting and setup guidance.
Use `--hosted-builders` or `--orchestrator-only` when the current machine is
observing/coordinating lanes and will not run local Codex or Claude wrappers.

Example, shortened:

```text
$ code-mower doctor --adoption --repo OWNER/REPO
PASS  config.validate             config validates
PASS  profile.select              selected profile: codex, claude_audit, gitar
PASS  runtime.python              Python 3.12 satisfies Code Mower requirements
PASS  runtime.github_auth         GitHub CLI auth probe succeeded
PASS  runtime.local_cli codex     codex found
PASS  runtime.local_cli claude    claude auth smoke probe succeeded
WARN  env.tokens codex            missing CODEX_AUDIT_LABEL_TOKEN or GITHUB_TOKEN
WARN  github.actions_cost         private repo has high-frequency metadata workflows
PASS  cloud.token                 optional Code Mower Cloud token file is configured

Summary: warn, 20 checks, 0 failures, 5 warnings
Next: fix token warnings, keep paid lanes manual, then generate a value report.
```

The warnings are the point: Code Mower should make setup, cost, and trust
boundaries visible before you promote any reviewer lane.

See a fuller static transcript: [docs/first-run-transcript.md](docs/first-run-transcript.md).

For an existing repository that already has Code Mower generated workflows or
wrappers, run `code-mower migration setup-drift --repo-path .` before copying a
fresh `.code-mower.generated` tree. It is read-only and reports path-level
classifications (`same`, `differs`, `new`, `repo-only`,
`missing-from-output`) without file contents or diffs.
Use [Upgrade An Existing Repository](docs/upgrade-existing-repo.md) for the
reviewed PR flow, repo-only file handling, builder identity checks, and
wrapper/pin drift checks.

After `init` and `doctor`, use `code-mower lanes status --repo OWNER/REPO` to
see active builder/reviewer lanes, gate/check state, recent Code Mower
workflows, local board/process hints when present, and the next operator
action. Text and JSON output redact local cwd paths by default; use
`--show-local-paths` only for local debugging. Local Board discovery is
best-effort across common macOS and Linux listener tools; when a host restricts
listener inventory, remote PR/check status still reports normally.

For the same redacted metadata in a local browser view, run
`code-mower board serve --repo OWNER/REPO` and open the printed localhost URL.
Plain `board serve` is read-only. If the default Board port is busy, the CLI
uses a nearby free loopback port and prints the URL; an explicit `--port` stays
strict and reports a friendly conflict. To build local history while the browser
view is open, run `code-mower board serve --repo OWNER/REPO --record-events`; it
records at most one snapshot every 60 seconds by default. The Board header shows
the Code Mower version serving the page and flags when a restart is needed after
an upgrade. To append one snapshot without serving the browser view, run
`code-mower board record --repo OWNER/REPO` from the repository checkout; it
writes metadata-only snapshots to `.code-mower/board/events.jsonl` with default
14-day and 500-event retention. `code-mower board events` prints recent stored
events without calling GitHub. `code-mower board doctor --repo OWNER/REPO`
checks Board inputs and local history without exposing local paths by default.
`code-mower board reset --repo OWNER/REPO --yes` clears only the local Board
history file. The Board also shows an owner queue, and it
summarizes reviewer verdict history and spend/latency from local board events
plus `.code-mower/reviewer-spend.json` when those files exist. Local agents can
publish opt-in status cards by writing metadata-only JSON files to
`.code-mower/board/agents/*.json`; the Board redacts local paths by default and
does not upload those cards.
The local JSON contracts are documented in
[Board Data Contract](docs/board-data-contract.md).
When a team wants the same operator picture on CodeMower.com, run
`code-mower cloud board-snapshot --repo-slug OWNER/REPO --json` to inspect a
zero-report, metadata-only mirror event, then add `--yes` only after review.

## See The Value Shape First

If you want to understand the product before installing anything, start with
the checked-in demo calibration package:

- [examples/demo-calibration/README.md](examples/demo-calibration/README.md)
- [examples/board-demo/README.md](examples/board-demo/README.md)
- [examples/demo-calibration/reviewer-value-report.md](examples/demo-calibration/reviewer-value-report.md)
- [docs/first-user-demo-transcript.md](docs/first-user-demo-transcript.md)
- [docs/post-v08-effectiveness-assessment.md](docs/post-v08-effectiveness-assessment.md)

The example is intentionally tiny and synthetic: one known-clean control, one
known-blocked control, and three reviewer lanes. It shows the decision Code
Mower is built to support: which AI reviewers are useful, noisy, expensive,
fast, or eligible for stronger merge policy on your actual codebase.

## Start Here

Choose one path. Each path has one guide and gets to one visible outcome.
Cold adopters should start with the reviewer gate, then add builders, then
measure builder experiments; provider experiments come after that base loop is
observable.

Install first from the [Install And Bootstrap](docs/install.md) matrix: pipx for
laptops, uv tool installs for hosted agents or CI boxes, and an editable venv
for Code Mower contributors. All paths require Python 3.12 or newer. For
upgrades, record `command -v code-mower` and `code-mower --version` before and
after reinstalling, especially when switching between pipx and uv.

| Path | Use When | Route | Guide |
| --- | --- | --- | --- |
| A. Reviewer gate in 10 minutes | You want one audited PR before recurring workflows or builder dispatch. | Install, run `init --easy`, run `doctor --adoption --repo OWNER/REPO`, open a small setup PR, run Codex and Claude audits, then merge manually when the audit evidence is clean. | [Try Code Mower In 10 Minutes](docs/try-in-10-minutes.md) |
| B. Build loop in 30 minutes | You want builders plus an orchestrator pattern after the reviewer gate works. | Complete path A, then add the automation token, require `code-mower/gate` from Any source, enable repository auto-merge, prove the self-hosted Mac lane runner with `doctor --runner`, run `init --builders`, and dispatch the first issue. | [Build Loop In 30 Minutes](docs/build-loop-in-30-minutes.md) |
| C. Builder experiment | You want to compare authoring loops before trusting them broadly. | Use a work order or experiment spec, run `code-mower builder-experiment run` around an explicit command, then review the source-free `authoringRun` artifact and normal audit evidence. | [Builder Experiments](docs/builder-experiments.md) |

The current package-index announcement entry point is the tagged
[Try Code Mower In 10 Minutes](https://github.com/codemower-ai/code-mower/blob/v0.9.2-beta.1/docs/try-in-10-minutes.md)
guide. The v0.9 beta includes the completed native Board work plus the adoption
and upgrade hardening from recent install rehearsals; v0.9.2 keeps the path
simple by removing the retired third-party observe bridge, softening
dispatch-token expiry metadata when the secret is present, and making
stale-audit waits name the runner/dispatcher requeue path. Package-only users
can start from the public package rather than a source checkout.

## What Calibration Does And Does Not Prove

The generated starter corpus proves that the commands run and the report path
works. It does not prove that a reviewer should gate merges. Promote reviewer
lanes only after repository-specific known-clean and known-blocked evidence
meets the [lane promotion policy](docs/lane-promotion-policy.md).

To bootstrap a draft from your repository history:

```bash
code-mower calibration auto-discover \
  --repo OWNER/REPO \
  --last-n 20 \
  --output .code-mower/draft-calibration-corpus.json
```

Auto-discovery uses recent merged PR metadata, structured audit trailers, and
review-request signals to propose known-clean and known-blocked cases. Review
every disposition before using it for lane promotion or merge policy.

Full walkthrough: [docs/try-in-10-minutes.md](docs/try-in-10-minutes.md).
Existing-repo upgrade flow:
[docs/upgrade-existing-repo.md](docs/upgrade-existing-repo.md).
First-time command map: [docs/launch-command-surface.md](docs/launch-command-surface.md).
Provider-contract baseline for the next release train:
[docs/v06-truth-baseline.md](docs/v06-truth-baseline.md).

For release verification,
[First-User Install Rehearsal](docs/first-user-install-rehearsal.md) records
the package-index procedure for `v0.9.2-beta.1` / `code-mower==0.9.2b1`. The
GitHub release records the workflow and rehearsal evidence for the exact tag.

## Optional: Plan Before Coding

For larger changes, use GitHub Issues and local work orders as the planning
surface, then keep pull requests focused on code review and merge readiness:

```bash
code-mower project-context init --project-name "My Product"
code-mower context add --external ~/Downloads/product-requirements.md
code-mower plan from-github-issue OWNER/REPO#123 \
  --output .code-mower/work-orders/billing-settings-plan.md
code-mower work-order draft \
  --issue-plan .code-mower/work-orders/billing-settings-plan.md \
  --output .code-mower/work-orders/billing-settings.md
```

The GitHub issue remains the source of truth. The local plan and work order are
derived working artifacts, and `work-order draft` also writes a metadata-only
`*.cloud-event.json` sidecar that can tie later builder/reviewer evidence back
to the issue on CodeMower.com. That sidecar excludes source, raw diffs, raw
transcripts, stdout/stderr, auth output, secrets, and issue body text. External
docs are recorded as metadata manifests unless you explicitly ask for bounded
previews. See [docs/planning-work-orders.md](docs/planning-work-orders.md).

After a PR exists, attach source-free delivery metadata to the same sidecar:

```bash
code-mower work-order attach-delivery \
  .code-mower/work-orders/billing-settings.cloud-event.json \
  --pr OWNER/REPO#124 \
  --from-github
```

If a hosted builder such as Grok Bot or Cursor Cloud Agents produced the PR,
record source-free builder provenance too:

```bash
code-mower builder record \
  --provider grok_bot \
  --executor cursor_cloud_agent \
  --work-order .code-mower/work-orders/billing-settings.md \
  --pr OWNER/REPO#124
```

That lets CodeMower.com show `issue -> plan -> work order -> builder run -> PR
-> reviewer checks -> merge` lineage without receiving source, diffs,
transcripts, prompts, or issue body text. See
[docs/builders-grok-cursor.md](docs/builders-grok-cursor.md).

## Roles

Code Mower records builder provenance for Claude Code, Codex, Cursor-style
hosted builders, and similar authoring lanes, then runs reviewer lanes against
the resulting pull requests. The orchestrator is a workflow convention: issue,
optional work order, single-writer branch, reviewer lanes, and fix rounds. Code
Mower's templates now support that loop end to end; humans still own
credentials, branch protection, calibration, and owner decisions.

The reference adoption shape is Claude Code as orchestrator; Claude Code,
Codex, and Cursor as builder lanes; Claude Code and Codex as reviewer lanes;
and Gitar or Antigravity as informational reviewer signal until local
calibration says otherwise.

## Why Not Just Run Codex Or Claude Yourself?

You should, at first. Code Mower is not a replacement for a good local agent or
reviewer CLI.

Code Mower adds the operating layer around those tools:

- consistent reviewer lanes on real pull requests;
- setup checks for auth, Python, GitHub permissions, private-repo Actions cost,
  provider CLIs, and cloud-token posture;
- calibration against known-clean and known-blocked PRs instead of vibes;
- evidence-gated lane promotion: informational, selective, or merge-gating;
- spend/latency/usefulness reporting across providers and lenses; and
- privacy boundaries for optional metadata sharing.

The goal is to learn which AI builders and reviewers are worth trusting on your
actual codebase, at what cost, and under which merge policy.

## Optional Cloud Sharing

Code Mower Cloud currently provides private team dashboards from opt-in
metadata. Cross-team cohort benchmarking is a roadmap feature that becomes
valuable only as enough teams contribute sanitized data. The local OSS path
stays useful without the hosted service.

The cloud value loop is:

1. run local Code Mower reports;
2. inspect the metadata-only bundle or dogfood preview;
3. upload only with `--yes`; and
4. use [CodeMower.com](https://codemower.com) to see repo rollups, provider/lens
   signal, cost/latency, noisy lanes, next-lane recommendations, and
   token-safe evidence/detail pages over time.

Start with a dry run:

```bash
code-mower cloud dogfood --json
```

Nothing uploads unless you pass `--yes`.
For the hosted Board mirror specifically,
`code-mower cloud board-snapshot --repo-slug OWNER/REPO --json` exports one
metadata-only event and zero reports; add `--yes` to upload it after inspection.

Current dogfood uploads, historical catch-up imports, and calibrated
reviewer/lens evidence are intentionally separate. Imported GitHub Actions
history can prove activity and upload health; it should not be treated as
reviewer-quality evidence until it is calibrated against known-clean and
known-blocked cases.

To connect to [CodeMower.com](https://codemower.com), sign in at
[https://codemower.com/login](https://codemower.com/login), create or receive a
team token, then run:

```bash
code-mower cloud setup \
  --token-stdin \
  --team-id "YOUR_TEAM_SLUG" \
  --install-id "your-laptop" \
  --out ~/.config/code-mower/tokens/your-laptop.env
```

`cloud setup` stores a private `0600` token env file and records it as the
current local cloud profile. Future `cloud doctor`, `cloud upload`, `cloud
dogfood`, `cloud reviewer-runs`, and `cloud repo-sync` commands can load that
profile automatically; pass `--install-id` or `--token-file` when a machine has
multiple stored profiles.

Cloud sharing details, historical catch-up, and repo-sync commands live in
[docs/cloud-sharing.md](docs/cloud-sharing.md).

## Provider Posture

The first recommended lanes are local/manual:

| Lane | Default role | Merge posture |
| --- | --- | --- |
| Codex audit | structured local peer audit | merge-gating eligible after setup |
| Claude audit | structured local peer audit | merge-gating eligible after setup |
| Gitar | advisory third signal | informational until calibrated |

Everything else starts manual or informational until your own calibration data
proves it is useful: Antigravity/Gemini, Hermes, CodeRabbit CLI, Cursor BugBot,
Qodo, Greptile, Devin, local LLMs, and future ACP bridges.

Gemini CLI and Antigravity are distinct lane ids even though both are Google
surfaces and may use Gemini model infrastructure. Keep their auth, model
provenance, calibration evidence, and release notes separate.

Provider details: [docs/provider-matrix.md](docs/provider-matrix.md).
Setup/auth fixes: [docs/troubleshooting.md](docs/troubleshooting.md).

## Road To v1.0

The v1.0 bar is not "every provider works." The v1.0 bar is that a fresh senior
engineer can:

1. install Code Mower in a clean repo;
2. understand the local/cloud trust boundary;
3. run `init --easy` and `doctor --adoption --repo OWNER/REPO`;
4. run `lanes status --repo OWNER/REPO` to see active lanes and gate state;
5. run `board serve --repo OWNER/REPO` when you want the same state in a local
   browser view;
6. detect and run the repo's native lint/test/build surface instead of assuming
   every project uses the same tools;
7. produce a local value report from known PR outcomes;
8. decide which lanes should stay informational, selective, or merge-gating;
   and
9. optionally upload sanitized metadata to CodeMower.com and see useful team
   dashboard signal.

Build-loop templates extend the same loop from "who reviews best?" to "which AI
builder plus reviewer loop ships best on this product?" Start with
[docs/build-loop-in-30-minutes.md](docs/build-loop-in-30-minutes.md), then use
[docs/builder-experiments.md](docs/builder-experiments.md) and
[docs/authoring-intelligence.md](docs/authoring-intelligence.md) for deeper
measurement work.

## Installation Status

The current package-index beta baseline is `v0.9.2-beta.1`, with pinned package
install spec `code-mower==0.9.2b1`. Release evidence is recorded on the GitHub
release and in the first-user install rehearsal. The public repository is
[codemower-ai/code-mower](https://github.com/codemower-ai/code-mower), and
GitHub releases remain the auditable source for tags, build artifacts, and
release notes.

For source checkout development and release rehearsal, use:

```bash
scripts/dev-python
scripts/dev-python -m venv .venv
.venv/bin/python -m pip install -e ".[test]"
```

The wrapper resolves Python 3.12+ and refuses stale or old system Python shims.
Do not run the CLI by hand-wiring source import paths; install the editable venv
first so local work exercises the same package entrypoint users install.

## Known Limitations

- PyPI distribution publishing is active through trusted publishing; see
  [docs/pypi-release.md](docs/pypi-release.md) for the release and
  verification runbook.
- GitHub is the primary supported forge. GitLab, Bitbucket, and ACP bridges are
  roadmap items.
- Hosted/SaaS reviewers start informational or manual until calibration data
  supports promotion.
- `calibration auto-discover` is a bootstrap tool, not an adjudicator. It
  proposes a draft corpus from PR history; humans still confirm the ground truth.
- CodeMower.com currently provides private team dashboards; cohort benchmarks
  are roadmap work and should not be treated as live product value yet.
- Self-service cloud data deletion/export basics are live. Retention remains
  conservative and team-controlled while automated retention jobs are roadmap
  work.
- Advanced/provider/operator commands remain available behind
  `code-mower --help-all`. The default help path stays focused on `init`,
  `doctor`, calibration, value reports, and optional cloud export/upload.

## Docs Map

- [Install And Bootstrap](docs/install.md)
- [Upgrade An Existing Repository](docs/upgrade-existing-repo.md)
- [Try Code Mower In 10 Minutes](docs/try-in-10-minutes.md)
- [Build Loop In 30 Minutes](docs/build-loop-in-30-minutes.md)
- [Quickstart Reference](docs/quickstart.md)
- [Orchestrator Prompt Pack](docs/orchestrator-prompt-pack.md)
- [Planning And Work Orders](docs/planning-work-orders.md)
- [Builder Providers: Grok And Cursor](docs/builders-grok-cursor.md)
- [Build Loop Operations](docs/build-loop.md)
- [Self-Hosted Mac Runner](docs/self-hosted-mac-runner.md)
- [Local Audit Runner](docs/local-audit-runner.md)
- [Lane Standing Instructions](docs/lanes/README.md)
- [Codex Lane Standing Instructions](docs/lanes/codex.md)
- [Claude Lane Standing Instructions](docs/lanes/claude.md)
- [Cursor Hosted Lane Standing Instructions](docs/lanes/cursor.md)
- [Provider Matrix](docs/provider-matrix.md)
- [GitHub Setup](docs/github-setup.md)
- [First Run Transcript](docs/first-run-transcript.md)
- [First-User Demo Transcript](docs/first-user-demo-transcript.md)
- [First-User Install Rehearsal](docs/first-user-install-rehearsal.md)
- [Launch Command Surface](docs/launch-command-surface.md)
- [v0.6 Truth Baseline](docs/v06-truth-baseline.md)
- [v0.6 Release Notes](docs/v06-release-notes.md)
- [v0.8 Release Notes](docs/v08-release-notes.md)
- [v0.9 Release Notes](docs/v09-release-notes.md)
- [Post-v0.8 Effectiveness Assessment](docs/post-v08-effectiveness-assessment.md)
- [Demo Calibration Example](examples/demo-calibration/README.md)
- [Board Demo Rehearsal](examples/board-demo/README.md)
- [PyPI Release Runbook](docs/pypi-release.md)
- [Sample Doctor Output](docs/sample-doctor-output.md)
- [Architecture](docs/architecture.md)
- [Lane Promotion Policy](docs/lane-promotion-policy.md)
- [Cloud Sharing](docs/cloud-sharing.md)
- [Cloud Data Contract](docs/cloud-data-contract.md)
- [Board Data Contract](docs/board-data-contract.md)
- [Privacy And Threat Model](docs/privacy-threat-model.md)
- [Current State And Roadmap](docs/current-state-and-roadmap.md)
- [Public Release Checklist](docs/public-release-checklist.md)
- [Changelog](CHANGELOG.md)
- [Contributing](CONTRIBUTING.md)
- [Support](SUPPORT.md)
- [Security Policy](SECURITY.md)
- [Code of Conduct](CODE_OF_CONDUCT.md)

## License

The Code Mower open-source core is licensed under Apache-2.0. Hosted
benchmarking and reporting, managed integrations, private telemetry and
benchmark data products, enterprise controls, and support are commercial
surfaces unless licensed otherwise.
