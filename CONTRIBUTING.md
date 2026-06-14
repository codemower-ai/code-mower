# Contributing

Thanks for helping make Code Mower boringly reliable.

## Development Setup

Use Python 3.11 or newer. Python 3.12 is the preferred local and CI runtime.

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e . ruff
```

## Local Checks

Run the focused checks before opening a pull request:

```bash
.venv/bin/python scripts/privacy_scan.py
.venv/bin/python -m ruff check .
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m compileall -q src scripts
.venv/bin/python scripts/smoke_easy_mode.py --code-mower-bin .venv/bin/code-mower --json
```

For packaging changes, also run:

```bash
.venv/bin/python scripts/fresh_clone_rehearsal.py --repo-url . --ref HEAD --python python3.12 --json
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
