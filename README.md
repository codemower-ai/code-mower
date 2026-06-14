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
  for private team dashboards and aggregate benchmarks.

Code Mower is local-first. The OSS tool works without the hosted service.
Default cloud bundles exclude source code, raw diffs, raw model transcripts,
raw stdout/stderr, auth output, and secrets.

## What It Looks Like

`code-mower doctor --v05` is the first useful command. It checks your runtime,
GitHub setup, provider CLIs, token posture, optional cloud setup, and private-repo
Actions cost traps.

Example, shortened:

```text
$ code-mower doctor --v05
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
pipx install --python python3.12 "git+https://github.com/codemower-ai/code-mower.git@v0.5.0-alpha.3"
code-mower --version
```

From the repository you want to pilot:

```bash
code-mower init --easy
code-mower init --easy --apply --output-dir .code-mower.generated
code-mower doctor --v05 --json
```

Then generate the starter local report:

```bash
code-mower calibration value-report .code-mower.generated/calibration-corpus.json \
  --output .code-mower/reviewer-value-report.md
```

The generated starter corpus proves the command path. Replace it with your own
known-clean and known-blocked PRs before using any lane as a merge gate.

Full walkthrough: [docs/try-in-10-minutes.md](docs/try-in-10-minutes.md).

## Optional Cloud Sharing

Code Mower Cloud is for teams that want private dashboards and benchmark
comparison over time. The local OSS path stays useful without it.

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

## Installation Status

The current public alpha is `v0.5.0-alpha.3` from
[codemower-ai/code-mower](https://github.com/codemower-ai/code-mower). PyPI
publishing is on the release checklist; until then, use the tagged GitHub
install command above.

For source checkout development and release rehearsal, use:

```bash
scripts/dev-python
scripts/dev-python -m venv .venv
.venv/bin/python -m pip install -e . ruff
```

The wrapper resolves Python 3.12+ and refuses stale or old system Python shims.

## Known Limitations

- PyPI publishing is not live yet; use the tagged GitHub install command.
- GitHub is the primary supported forge. GitLab, Bitbucket, and ACP bridges are
  roadmap items.
- Hosted/SaaS reviewers start informational or manual until calibration data
  supports promotion.
- CodeMower.com cohort benchmarks are early. Local reports are useful now;
  aggregate comparison improves as more teams opt in.

## Docs Map

- [Try Code Mower In 10 Minutes](docs/try-in-10-minutes.md)
- [Quickstart](docs/quickstart.md)
- [First Run Transcript](docs/first-run-transcript.md)
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
