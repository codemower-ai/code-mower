# Code Mower

Code Mower is the fastest way to build a peer-programmer and reviewer system around the best AI coding agents.

It helps teams drive from plan to merge at maximum safe velocity while preserving code quality, architecture, and deployment confidence. It also turns your real codebase into a quality-and-velocity benchmark, measuring which AI builders and reviewers deliver the best quality, speed, and cost results for your actual product.

The Code Mower open-source core is licensed under Apache-2.0. Hosted benchmarking and reporting, managed integrations, private telemetry and benchmark data products, enterprise controls, and support are commercial surfaces unless licensed otherwise.

Code Mower is extracted from a production multi-repo development workflow and packaged as a standalone OSS tool. Start with `code-mower init --easy`, then run `code-mower doctor --easy` to verify local CLIs, tokens, provider catalog coverage, and runtime probes.

For existing repos that still carry product-local Code Mower tools, run `code-mower migration wrapper-rehearsal --repo-path /path/to/repo --json` before flipping to a pinned standalone package. The rehearsal compares safe read-only commands and gives a low-risk path away from mirrored maintenance.

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

For v1.0 readiness and migration guidance, see `docs/oss-v1-checklist.md`, `docs/repo-strategy.md`, `docs/mirror-removal-runbook.md`, `docs/github-setup.md`, `docs/provider-matrix.md`, `docs/commercial-boundary.md`, and `docs/public-release-checklist.md`.
