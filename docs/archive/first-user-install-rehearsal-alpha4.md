# v0.5.0-alpha.4 First-User Install Rehearsal

This is the recorded release-gate shape for `v0.5.0-alpha.4`. It proves the
source checkout can install, run easy mode, generate a starter value report, and
create a metadata-only cloud dry-run bundle from a fresh toy repository.

The exact temporary paths and cache output vary by machine; the important signal
is the command sequence and `status: pass` results.

## Local Source Install

```bash
scripts/dev-python -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e . ruff build
.venv/bin/code-mower --version
```

Expected output:

```text
code-mower 0.5.0a4
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
Ran 54 tests ... OK
Successfully built code_mower-0.5.0a4.tar.gz and code_mower-0.5.0a4-py3-none-any.whl
```

`python -m build` currently emits non-blocking warnings about the deprecated
`project.license` table form and broad YAML include patterns. The alpha.4 build
also verifies bytecode caches are excluded from the wheel even after
`compileall` runs.

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

After committing the alpha.4 changes, run:

```bash
.venv/bin/python scripts/fresh_clone_rehearsal.py \
  --repo-url /absolute/path/to/code-mower \
  --ref HEAD \
  --python .venv/bin/python \
  --work-dir /tmp/code-mower-alpha4-fresh-clone \
  --json
```

Expected summary:

```json
{
  "mode": "code-mower-fresh-clone-rehearsal",
  "status": "pass"
}
```

Observed alpha.4 summary:

```text
Successfully installed PyYAML-6.0.3 code-mower-0.5.0a4
No broken requirements found.
code-mower 0.5.0a4
status: pass
```
