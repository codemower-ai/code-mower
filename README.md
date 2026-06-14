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

## Try It In 10 Minutes

Code Mower currently targets Python 3.11+; Python 3.12 is recommended.

```bash
python3.12 --version
pipx install --python python3.12 "git+https://github.com/codemower-ai/code-mower.git@v0.5.0-alpha.8"
code-mower --version
```

From the repository you want to pilot:

```bash
code-mower init --easy
code-mower init --easy --apply --output-dir .code-mower.generated
code-mower doctor --preflight --json
```

Then generate the starter local report:

```bash
code-mower calibration value-report .code-mower.generated/calibration-corpus.json \
  --output .code-mower/reviewer-value-report.md
```

The generated starter corpus proves the command path. Replace it with your own
known-clean and known-blocked PRs before using any lane as a merge gate.

Full walkthrough: [docs/try-in-10-minutes.md](docs/try-in-10-minutes.md).

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

Create an inspectable metadata-only bundle:

```bash
code-mower cloud export \
  --report value-report=.code-mower/reviewer-value-report.md \
  --output-dir .code-mower/cloud-benchmark-bundle \
  --anonymous \
  --json

code-mower cloud upload .code-mower/cloud-benchmark-bundle --dry-run --json
```

Nothing uploads unless you pass `--yes`.

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

After token setup, use the dogfood path for repeated metadata uploads:

```bash
source ~/.config/code-mower/tokens/your-laptop.env
code-mower cloud dogfood --json
code-mower cloud dogfood --yes --json
```

The first command previews the metadata locally. The second sends only sanitized
metadata and a `dogfood_upload` event so the dashboard can start showing repo,
provider/lens, cost, latency, and recommendation rows.

Cloud sharing details: [docs/cloud-sharing.md](docs/cloud-sharing.md).

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
4. produce a local value report from known PR outcomes;
5. decide which lanes should stay informational, selective, or merge-gating;
   and
6. optionally upload sanitized metadata to CodeMower.com and see useful team
   dashboard signal.

Future builder/orchestrator experiments extend the same loop from "who reviews
best?" to "which AI builder plus reviewer loop ships best on this product?" See
[docs/builder-experiments.md](docs/builder-experiments.md) and
[docs/authoring-intelligence.md](docs/authoring-intelligence.md).

## Installation Status

The current public alpha is `v0.5.0-alpha.8` from
[codemower-ai/code-mower](https://github.com/codemower-ai/code-mower). PyPI
distribution builds now run from GitHub Releases. Publishing to PyPI remains
off by default until trusted publishing is configured, so use the tagged GitHub
install command above for this alpha.

For source checkout development and release rehearsal, use:

```bash
scripts/dev-python
scripts/dev-python -m venv .venv
.venv/bin/python -m pip install -e . ruff
```

The wrapper resolves Python 3.12+ and refuses stale or old system Python shims.

## Known Limitations

- PyPI distribution builds exist, but publishing is gated until trusted
  publishing is configured; use the tagged GitHub install command. See
  [docs/pypi-release.md](docs/pypi-release.md) for the TestPyPI/PyPI
  activation path.
- GitHub is the primary supported forge. GitLab, Bitbucket, and ACP bridges are
  roadmap items.
- Hosted/SaaS reviewers start informational or manual until calibration data
  supports promotion.
- CodeMower.com currently provides private team dashboards; cohort benchmarks
  are roadmap work and should not be treated as live product value yet.
- Self-service cloud data deletion/export basics are live. Retention remains
  conservative and team-controlled while automated retention jobs are roadmap
  work.
- The CLI still exposes some advanced/operator commands. The early-adopter path
  should stay focused on `init`, `doctor`, local audits, value reports, and
  optional cloud export/upload.

## Docs Map

- [Try Code Mower In 10 Minutes](docs/try-in-10-minutes.md)
- [Quickstart](docs/quickstart.md)
- [First Run Transcript](docs/first-run-transcript.md)
- [First-User Install Rehearsal](docs/first-user-install-rehearsal.md)
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

## License

The Code Mower open-source core is licensed under Apache-2.0. Hosted
benchmarking and reporting, managed integrations, private telemetry and
benchmark data products, enterprise controls, and support are commercial
surfaces unless licensed otherwise.
