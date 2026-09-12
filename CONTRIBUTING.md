# Contributing

Thanks for helping make Code Mower boringly reliable.

## Development Setup

Use Python 3.12 or newer. CI exercises Python 3.12, 3.13, and 3.14.

```bash
scripts/dev-python --version
scripts/dev-python -m venv .venv
.venv/bin/python -m pip install -e ".[test]"
```

The checked-in wrapper selects Python 3.12 or newer and rejects stale system
shims. Upgrade pip only for a deliberate package-index or release rehearsal;
normal contributor setup should remain reproducible and offline-friendly.

## Local Checks

Run the focused checks before opening a pull request. These checks should stay
local-source and offline-friendly by default; they must not depend on PyPI,
TestPyPI, live package-index propagation, or external provider network calls.

```bash
.venv/bin/python scripts/privacy_scan.py
.venv/bin/python -m ruff check .
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m compileall -q src scripts
.venv/bin/python scripts/smoke_easy_mode.py --code-mower-bin .venv/bin/code-mower --json
```

For packaging changes, also run:

```bash
.venv/bin/python scripts/fresh_clone_rehearsal.py --repo-url "$(pwd)" --ref HEAD --python python3.12 --json
```

Package-index rehearsals are release/integration checks, not default unit
tests. Use the local checkout package spec for ordinary CI and PR work. Add
`--allow-package-index` only when deliberately validating a published
TestPyPI/PyPI candidate or release:

```bash
.venv/bin/python -m code_mower.migration package-install-rehearsal \
  --package-spec . \
  --json
```

## Privacy And Examples

Public examples should use `owner/repo`, `owner/other-repo`, or intentionally
published toy repositories. Do not add:

- personal email addresses, account names, or home-directory paths;
- private repository slugs;
- raw auth probe output;
- raw reviewer stdout/stderr containing private source;
- API keys, private keys, tokens, or token-like sample values.

Calibration evidence should be summarized or anonymized unless the repository
owner has intentionally published the underlying corpus.

## Pull Request Shape

Keep changes focused. Prefer small PRs that improve one of:

- easy-mode install and doctor reliability;
- provider setup clarity;
- calibration evidence quality;
- privacy/security guardrails;
- package migration from repo-local mirrors; or
- reviewer value reporting.

Markdown bodies are data, not shell syntax. Use `--body-file`, stdin, or API
payloads for GitHub comments and PR bodies instead of inline double-quoted
Markdown.
