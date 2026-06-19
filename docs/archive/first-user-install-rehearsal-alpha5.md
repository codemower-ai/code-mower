# v0.5.0-alpha.5 First-User Install Rehearsal

This is the recorded release-gate shape for `v0.5.0-alpha.5`. It proves the
source checkout can install, expose the friendlier `doctor --preflight` path,
generate starter reports, and create a metadata-only cloud dry-run bundle from
a fresh toy repository.

The exact temporary paths and cache output vary by machine; the important signal
is the command sequence and `status: pass` results.

## Local Source Install

```bash
scripts/dev-python -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e . ruff build
.venv/bin/code-mower --version
```

Observed output:

```text
code-mower 0.5.0a5
```

## Release Gate

```bash
.venv/bin/python -m ruff check .
.venv/bin/python scripts/privacy_scan.py
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m compileall -q src scripts
.venv/bin/python -m build
```

Observed summary:

```text
All checks passed!
privacy scan passed
Ran 56 tests ... OK
Successfully built code_mower-0.5.0a5.tar.gz and code_mower-0.5.0a5-py3-none-any.whl
```

`python -m build` currently emits non-blocking warnings about the deprecated
`project.license` table form and broad YAML include patterns. The alpha.5 build
also verifies bytecode caches are excluded from the wheel even after
`compileall` runs.

## Preflight Doctor Smoke

```bash
.venv/bin/code-mower doctor --preflight --json
```

Observed summary:

```json
{
  "status": "warn",
  "summary": {
    "checks": 20,
    "failures": 0,
    "warnings": 6
  }
}
```

The warnings came from optional local setup context: missing audit label tokens,
the example `owner/example` repository, hosted Gitar visibility that cannot be
resolved from that example repository, and absent `pytest` in the standalone
environment. None are release blockers.

## Easy-Mode Smoke

```bash
.venv/bin/python scripts/smoke_easy_mode.py \
  --code-mower-bin .venv/bin/code-mower \
  --json
```

Observed summary:

```json
{
  "mode": "code-mower-easy-mode-smoke",
  "status": "pass"
}
```

The smoke covers:

- `code-mower providers list`
- `code-mower init --easy --apply`
- generated `smoke-tests.sh`
- `code-mower doctor --easy --json`
- wrapper rehearsal
- calibration plan/evidence/metrics/policy/value-report
- cloud export
- cloud upload dry run

## Fresh-Clone Rehearsal

After committing the alpha.5 changes, run:

```bash
.venv/bin/python scripts/fresh_clone_rehearsal.py \
  --repo-url /absolute/path/to/code-mower \
  --ref HEAD \
  --python .venv/bin/python \
  --work-dir /tmp/code-mower-alpha5-fresh-clone \
  --json
```

Expected summary:

```json
{
  "mode": "code-mower-fresh-clone-rehearsal",
  "status": "pass"
}
```

Observed alpha.5 summary:

```text
Successfully installed PyYAML-6.0.3 code-mower-0.5.0a5
No broken requirements found.
code-mower 0.5.0a5
status: pass
```
