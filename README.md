# Code Mower

Code Mower is the fastest way to build a peer-programmer and reviewer system around the best AI coding agents.

It helps teams drive from plan to merge at maximum safe velocity while preserving code quality, architecture, and deployment confidence. It also turns your real codebase into a quality-and-velocity benchmark, measuring which AI builders and reviewers deliver the best quality, speed, and cost results for your actual product.

The Code Mower open-source core is licensed under Apache-2.0. Hosted benchmarking and reporting, managed integrations, private telemetry and benchmark data products, enterprise controls, and support are commercial surfaces unless licensed otherwise.

Code Mower is extracted from a production multi-repo development workflow and packaged as a standalone OSS tool. Start with `code-mower init --easy`, then run `code-mower doctor --v05` to verify local CLIs, tokens, provider catalog coverage, GitHub setup, optional cloud-token setup, Actions cost traps, and runtime probes. The current early-adopter path is documented in `docs/try-in-10-minutes.md` and `docs/quickstart.md`.

## Try It In 10 Minutes

```bash
python3.12 --version
pipx install --python python3.12 "git+https://github.com/codemower-ai/code-mower.git@v0.5.0-alpha.4"
code-mower --version
code-mower init --easy
code-mower doctor --v05 --json
```

Start with one GitHub repository, keep reviewer lanes manual, and inspect all
generated output before enabling cloud upload or paid reviewers. The short path
is [docs/try-in-10-minutes.md](docs/try-in-10-minutes.md); the fuller setup is
[docs/quickstart.md](docs/quickstart.md).

For source checkout development and release rehearsal, use
`scripts/dev-python` to create the local virtualenv. It resolves a Python
3.12+ interpreter and refuses stale or old system Python shims before any
release script runs.

For existing repos that still carry product-local Code Mower tools, run `code-mower migration wrapper-rehearsal --repo-path /path/to/repo --json` before flipping to a pinned standalone package. The rehearsal compares safe read-only commands and gives a low-risk path away from mirrored maintenance.

For opt-in dogfooding, run `code-mower cloud dogfood --json` to create a
metadata-only benchmark bundle and dry-run the upload path. Passing `--yes`
uploads to [https://codemower.com/api/ingest](https://codemower.com/api/ingest)
only when a team ingest token is configured.

Before removing mirrors, prove the package-installed path in a clean venv:

```bash
code-mower migration package-install-rehearsal \
  --package-spec code-mower \
  --repo-path /path/to/product-repo \
  --json
```

Use a local path or git URL for `--package-spec` during alpha testing. This
rehearsal installs Code Mower non-editably, creates a fresh toy repo, runs
easy-mode init/doctor/next-steps/calibration starter checks, and optionally
compares a product repo against the installed package.

For current state, v0.5 early-adopter, v1.0 readiness, and migration guidance,
see `docs/current-state-and-roadmap.md`, `docs/try-in-10-minutes.md`, `docs/quickstart.md`,
`docs/early-adopter-v05.md`,
`docs/oss-v1-checklist.md`, `docs/repo-strategy.md`,
`docs/mirror-removal-runbook.md`, `docs/github-setup.md`,
`docs/provider-matrix.md`, `docs/cloud-sharing.md`,
`docs/cloud-contributor-runbook.md`, `docs/cloud-benchmarking.md`,
`docs/privacy-threat-model.md`, `docs/commercial-boundary.md`, and
`docs/public-release-checklist.md`.
