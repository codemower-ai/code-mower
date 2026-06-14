# Security Policy

Code Mower coordinates local CLIs, GitHub workflows, optional hosted reviewers,
and optional cloud metadata upload. Treat it as automation that can read code,
run configured tools, and post back to GitHub when explicitly configured.

## Supported Versions

Security fixes target the latest published alpha tag and `main` until a stable
release line exists.

## Reporting A Vulnerability

Please do not open a public issue with exploit details, credentials, private
repository URLs, raw audit prompts, raw reviewer outputs, proprietary code, or
secrets.

Report security concerns privately through GitHub private vulnerability
reporting when available, or contact the maintainers out of band.

Good reports include:

- affected version or commit;
- command or workflow used;
- whether the issue involves local CLI execution, GitHub permissions, provider
  output, cloud export/upload, generated files, or hosted dashboard behavior;
- the smallest sanitized reproduction you can provide;
- whether source code, diffs, transcripts, tokens, provider auth output, or
  private keys may have been exposed; and
- suggested severity if you have one.

## Security Boundaries

- Code Mower's local CLI wrappers can send prompts, diffs, and selected context
  files to the provider behind the configured CLI.
- SaaS reviewer lanes can expose pull request diffs and repository context to
  the configured GitHub App or hosted reviewer.
- Local model lanes expose code to the configured endpoint; local endpoints can
  keep code local, remote endpoints cannot.
- Cloud bundle/export commands are opt-in and produce inspectable artifacts
  before upload.
- Default cloud export and upload paths must not include source code, raw
  diffs, raw model transcripts, raw stdout/stderr, auth probe output, or
  secrets.
- Generated GitHub workflows should use least-privilege tokens and should keep
  paid or hosted lanes manual or explicitly labeled until calibrated.

If you find a default path that leaks source, raw diffs, raw transcripts, auth
output, or secrets, treat it as a security issue.

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
