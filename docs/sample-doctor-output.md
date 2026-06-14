# Sample Doctor Output

This page shows the kind of signal `code-mower doctor --v05` is meant to
produce before you enable reviewer lanes.

The exact checks depend on your repository, GitHub auth, provider CLIs, and
optional cloud-token setup. This sample is sanitized and intentionally generic.

## Human-Readable Shape

```text
$ code-mower doctor --v05
PASS  config.validate                  config validates
PASS  provider_templates.load          provider templates load
PASS  profile.select                   selected profile recommended: codex, claude_audit, gitar
PASS  provider_templates.coverage      provider templates cover selected lanes
PASS  runtime.python                   Python 3.12 satisfies Code Mower's >=3.11 requirement
PASS  runtime.pytest                   pytest import is available for product-side test wrappers
PASS  runtime.github_auth              GitHub CLI auth probe succeeded
PASS  runtime.ripgrep                  rg found

WARN  env.tokens codex                 missing token env vars: CODEX_AUDIT_LABEL_TOKEN, GITHUB_TOKEN
PASS  runtime.local_cli codex          codex found
PASS  runtime.local_cli.probe codex    codex probe succeeded

WARN  env.tokens claude_audit          missing token env vars: CLAUDE_AUDIT_LABEL_TOKEN, GITHUB_TOKEN
PASS  runtime.local_cli claude_audit   claude found
PASS  runtime.local_cli.probe claude   claude auth smoke probe succeeded

WARN  env.tokens gitar                 missing token env vars: GITAR_AUDIT_LABEL_TOKEN, GITHUB_TOKEN
SKIP  runtime.probe gitar              SaaS event lanes do not have a local runtime probe yet

PASS  github.cli                       gh found for GitHub setup checks
WARN  github.repo.metadata             could not read GitHub repository metadata for owner/repo
WARN  github.provider.private_repo     could not determine repository visibility for SaaS lanes
PASS  cloud.token                      optional Code Mower Cloud token file is configured

Summary: warn
Checks: 20
Failures: 0
Warnings: 5
```

## What To Do With It

Treat the doctor output as setup guidance, not as a magical quality score.

- `PASS` means the check found enough evidence to continue.
- `WARN` means the lane or setup path might work, but you should inspect the
  remediation before relying on it.
- `FAIL` means a required first-run condition is missing.
- `SKIP` means the check is not applicable or cannot be probed locally.

`--strict` turns warnings into a harder gate for CI/bootstrap jobs. For a first
local run, start without `--strict`.

## JSON Mode

Use JSON mode for support, automation, or CI:

```bash
code-mower doctor --v05 --json > code-mower-doctor.json
```

The output includes:

- top-level status and summary counts;
- one object per check;
- lane-specific remediation when available;
- token-safe cloud setup diagnostics; and
- GitHub/private-repo cost and permission hints.

The JSON output is designed to avoid raw auth output and token values. It can
still mention repository slugs, workflow names, provider names, and local setup
state, so inspect it before sharing publicly.

## Why This Matters

Code Mower's job is not only to run reviewers. It should also tell you when a
reviewer lane is expensive, unauthenticated, uncalibrated, too noisy, or
dangerous to make merge-gating.

The first successful install should answer:

- Which reviewer lanes are available on this machine?
- Which lanes need tokens or GitHub app setup?
- Which workflows could spend private-repo Actions minutes?
- Is optional cloud upload configured, and is it still dry-run safe?
- What should I fix before promoting any lane beyond manual/informational?
