# Code Mower

Code Mower helps teams set up AI peer-programmer and reviewer lanes on real
GitHub pull requests, then measure which builders and reviewers are useful on
their actual codebase.

The short version:

- create safe, manual-first reviewer lanes for Codex, Claude, Gitar, and other
  AI review tools;
- run setup diagnostics before a lane can surprise you with spend, source
  exposure, or GitHub Actions churn;
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

`code-mower doctor --preflight` is the first useful command. It checks your
runtime, GitHub setup, provider CLIs, token posture, optional cloud setup, and
private-repo Actions cost traps. `--preflight` is the friendly name for the
versioned v0.5 first-run preset, so `doctor --v05` remains equivalent for
scripts.

Example, shortened:

```text
$ code-mower doctor --preflight
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

## See The Value Shape First

If you want to understand the product before installing anything, start with
the checked-in demo calibration package:

- [examples/demo-calibration/README.md](examples/demo-calibration/README.md)
- [examples/demo-calibration/reviewer-value-report.md](examples/demo-calibration/reviewer-value-report.md)
- [docs/first-user-demo-transcript.md](docs/first-user-demo-transcript.md)

The example is intentionally tiny and synthetic: one known-clean control, one
known-blocked control, and three reviewer lanes. It shows the decision Code
Mower is built to support: which AI reviewers are useful, noisy, expensive,
fast, or eligible for stronger merge policy on your actual codebase.

## Try It First

Code Mower currently targets Python 3.11+; Python 3.12 is recommended.

```bash
python3.12 --version
pipx install --python python3.12 code-mower==0.5.0b27
code-mower --version
```

`0.5.0b27` is a beta release. Until Code Mower publishes a stable `1.0`
line, use the explicit beta version above or allow prereleases with:

```bash
pipx install --python python3.12 --pip-args="--pre" code-mower
```

From the repository you want to pilot:

```bash
code-mower init --easy
code-mower doctor --preflight
code-mower checks detect --json
```

When those look sane, write the generated setup to a reviewable folder and
produce the starter local report:

```bash
code-mower init --easy --apply --output-dir .code-mower.generated
code-mower calibration value-report .code-mower.generated/calibration-corpus.json \
  --output .code-mower/reviewer-value-report.md
```

The generated starter corpus proves the command path. To bootstrap a draft from
your repository history:

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
First-time command map: [docs/launch-command-surface.md](docs/launch-command-surface.md).

The current PyPI beta has been rehearsed end-to-end from a clean install:
[First-User Install Rehearsal](docs/first-user-install-rehearsal.md) records
the latest 10/10 public-package readiness proof for `code-mower==0.5.0b27`.

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

Provider details: [docs/provider-matrix.md](docs/provider-matrix.md).
Setup/auth fixes: [docs/troubleshooting.md](docs/troubleshooting.md).

## Road To v1.0

The v1.0 bar is not "every provider works." The v1.0 bar is that a fresh senior
engineer can:

1. install Code Mower in a clean repo;
2. understand the local/cloud trust boundary;
3. run `init --easy` and `doctor --preflight`;
4. detect and run the repo's native lint/test/build surface instead of assuming
   every project uses the same tools;
5. produce a local value report from known PR outcomes;
6. decide which lanes should stay informational, selective, or merge-gating;
   and
7. optionally upload sanitized metadata to CodeMower.com and see useful team
   dashboard signal.

Future builder/orchestrator experiments extend the same loop from "who reviews
best?" to "which AI builder plus reviewer loop ships best on this product?" See
[docs/builder-experiments.md](docs/builder-experiments.md) and
[docs/authoring-intelligence.md](docs/authoring-intelligence.md).

## Installation Status

The current public beta is `v0.5.0-beta.27` from
[codemower-ai/code-mower](https://github.com/codemower-ai/code-mower), published
as `code-mower==0.5.0b27` on [PyPI](https://pypi.org/project/code-mower/).
GitHub releases remain the auditable source for tags, build artifacts, and
release notes.

For source checkout development and release rehearsal, use:

```bash
scripts/dev-python
scripts/dev-python -m venv .venv
.venv/bin/python -m pip install -e ".[test]"
```

The wrapper resolves Python 3.12+ and refuses stale or old system Python shims.

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

- [Try Code Mower In 10 Minutes](docs/try-in-10-minutes.md)
- [Quickstart](docs/quickstart.md)
- [First Run Transcript](docs/first-run-transcript.md)
- [First-User Demo Transcript](docs/first-user-demo-transcript.md)
- [First-User Install Rehearsal](docs/first-user-install-rehearsal.md)
- [Launch Command Surface](docs/launch-command-surface.md)
- [Demo Calibration Example](examples/demo-calibration/README.md)
- [PyPI Release Runbook](docs/pypi-release.md)
- [Sample Doctor Output](docs/sample-doctor-output.md)
- [Architecture](docs/architecture.md)
- [Provider Matrix](docs/provider-matrix.md)
- [GitHub Setup](docs/github-setup.md)
- [Cloud Sharing](docs/cloud-sharing.md)
- [Cloud Data Contract](docs/cloud-data-contract.md)
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
