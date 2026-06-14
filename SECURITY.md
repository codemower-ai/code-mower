# Security Policy

Code Mower coordinates local CLIs, GitHub workflows, and optional hosted
reviewers. Treat it as automation that can read code and run configured tools.

## Supported Versions

Security fixes target the latest public alpha and the `main` branch until a
stable release line exists.

## Reporting A Vulnerability

Open a private security advisory or contact the maintainers through the
repository security channel. Do not file public issues that include credentials,
private repository URLs, raw audit prompts, or raw reviewer outputs containing
proprietary code.

Good reports include:

- affected version or commit;
- command or workflow used;
- whether the issue involves local CLI execution, GitHub permissions, provider
  output, cloud export, or generated files;
- the smallest sanitized reproduction you can provide; and
- whether any token, private key, or proprietary source text was exposed.

## Security Boundaries

- Code Mower's local CLI wrappers can send prompts, diffs, and selected context
  files to the provider behind the configured CLI.
- SaaS reviewer lanes can expose pull request diffs and repository context to
  the configured GitHub App or hosted reviewer.
- Local model lanes expose code to the configured endpoint; local endpoints can
  keep code local, remote endpoints cannot.
- Cloud bundle/export commands are opt-in and produce inspectable artifacts
  before upload.
- Generated GitHub workflows should use least-privilege tokens and should keep
  paid or hosted lanes manual or explicitly labeled until calibrated.

## Maintainer Release Checks

Before broad public release, maintainers should run:

```bash
python scripts/privacy_scan.py
python -m unittest discover -s tests
python -m compileall -q src scripts
python scripts/smoke_easy_mode.py --json
python scripts/fresh_clone_rehearsal.py --repo-url . --ref HEAD --python python3.12 --json
```

The public CI workflow includes a `Privacy scan` step that runs
`python scripts/privacy_scan.py`. That step exits non-zero when it finds
personal paths, private repo slugs, raw auth output patterns, or likely secrets,
and release/publish work must not proceed from a failing CI run.

For manual releases outside GitHub Actions, maintainers must run the same
command locally and inspect any changed calibration artifacts before tagging.
Only maintainers with release authority may override the scan, and overrides
must be documented in the release notes with the exact reason the finding is
safe to publish.
