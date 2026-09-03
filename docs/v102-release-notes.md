# Code Mower v1.0.2 Release Notes

Code Mower v1.0.2 is a small adoption and release-smoke hardening patch on top
of the v1.0 supervised autonomous pilot. It preserves the v1.0.1 privacy
boundary, dashboard data contracts, and merge-gate semantics.

Install the pinned package:

```bash
export CODE_MOWER_PYTHON="$(command -v python3.12)"
pipx install --python "$CODE_MOWER_PYTHON" code-mower==1.0.2
code-mower --version
```

Hosted agents and CI boxes can use:

```bash
uv tool install --python 3.12 code-mower==1.0.2
code-mower --version
```

## What Is New

- Package-index install rehearsals now bypass pip cache and retry exact-version
  package-index installs after `--allow-package-index`, which reduces false
  blockers while PyPI or TestPyPI finishes propagating a fresh release.
- `doctor` and `init` give clearer cwd/config diagnostics when run from a temp
  directory or a checkout without the expected `code-mower.yml`.
- Board inventory distinguishes transient unresponsive listeners from legacy
  or pre-identity Board listeners, and labels the latter as restart
  recommended.
- Empty productivity reports now point to both continuous Board history capture
  and the lighter one-shot `code-mower board record --repo OWNER/REPO` path.
- Quickstart treats `uv tool install` as the first-class hosted-agent install
  route, while keeping pipx as the laptop/workstation path.
- `code-mower controller run --dry-run` is accepted as an explicit alias for
  the default dry-run controller mode, which helps agent permission systems
  classify the command as read-only.

## Recommended Update

For an existing v1.0.1 install:

```bash
export CODE_MOWER_PYTHON="$(command -v python3.12)"
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" code-mower==1.0.2
code-mower --version
code-mower board list
```

For hosted agents using uv:

```bash
uv tool install --python 3.12 --reinstall --refresh-package code-mower code-mower==1.0.2
code-mower --version
```

If a Board from an older install is still running, inspect it and stop the
chosen listener with:

```bash
code-mower board list
code-mower board stop --port PORT --yes
code-mower board serve --repo OWNER/REPO --record-events
```

## Release Proof

The release PR records the v1.0.2 release-readiness check, focused and full test
passes, Code Mower dogfood upload, GitHub release workflow run, production PyPI
publish, and exact package-install rehearsal. The release keeps uploads opt-in
and metadata-only: no source, raw diffs, transcripts, issue body text, raw
stdout/stderr, auth output, local secret values, or secrets.
