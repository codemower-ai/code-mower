# PyPI Release Runbook

Code Mower beta users currently install from a tagged GitHub release. The
release workflow already builds source and wheel distributions; package-index
publishing should be enabled in this order so first-user installs become:

```bash
pipx install code-mower
```

## Current Status

- GitHub Release workflow builds distributions on every published release.
- The release workflow downloads the uploaded distributions and runs
  `twine check` before any optional PyPI publish job can start.
- TestPyPI publishing is gated behind the `testpypi` GitHub environment and
  the `CODE_MOWER_TESTPYPI_PUBLISH` repository variable or manual
  `workflow_dispatch` input.
- Production PyPI publishing is gated behind the `pypi` GitHub environment and
  the `CODE_MOWER_PYPI_PUBLISH` repository variable or manual
  `workflow_dispatch` input.
- Trusted publishing must be configured before using this for public users.
- GitHub-tag install remains the documented beta path until a TestPyPI install
  rehearsal passes.

## One-Time TestPyPI Setup

1. Create or verify a project on [https://test.pypi.org](https://test.pypi.org)
   named `code-mower`.
2. Configure trusted publishing for
   [https://github.com/codemower-ai/code-mower](https://github.com/codemower-ai/code-mower):
   - owner: `codemower-ai`
   - repository: `code-mower`
   - workflow: `release.yml`
   - environment: `testpypi`
3. Add a `testpypi` GitHub environment at
   [https://github.com/codemower-ai/code-mower/settings/environments](https://github.com/codemower-ai/code-mower/settings/environments).
4. Keep the `CODE_MOWER_TESTPYPI_PUBLISH` repository variable unset or `false`
   for normal releases. Use manual `workflow_dispatch` with
   `publish_testpypi=true` when rehearsing a package-index release candidate.
5. Keep the production `pypi` environment separate.

## One-Time Production PyPI Setup

1. Create or claim the project on [https://pypi.org](https://pypi.org).
2. Configure trusted publishing for the same repository and workflow:
   - owner: `codemower-ai`
   - repository: `code-mower`
   - workflow: `release.yml`
   - environment: `pypi`
3. Keep the production `pypi` GitHub environment protected until at least one
   TestPyPI release has been installed in a fresh repo.
4. Keep the `CODE_MOWER_PYPI_PUBLISH` repository variable unset or `false`
   until production PyPI trusted publishing has passed a deliberate release
   gate. Prefer manual `workflow_dispatch` with `publish_pypi=true` for the
   first production publish.

## Workflow Dispatch Matrix

Use [https://github.com/codemower-ai/code-mower/actions/workflows/release.yml](https://github.com/codemower-ai/code-mower/actions/workflows/release.yml)
for manual release rehearsals:

| `publish_testpypi` | `publish_pypi` | Expected behavior |
| --- | --- | --- |
| `false` | `false` | Build, upload, download, and verify distributions only. |
| `true` | `false` | Build, verify, then publish to TestPyPI using the `testpypi` environment. |
| `false` | `true` | Build, verify, then publish to production PyPI using the `pypi` environment. Use only after TestPyPI passes. |
| `true` | `true` | Avoid this for normal releases; publish to TestPyPI and PyPI as separate, auditable runs. |

## Release Verification

Every GitHub release run should leave `build-distributions` and
`verify-distributions` green. The `verify-distributions` job exercises the
same artifact download path used by the optional PyPI publish job, then runs
`twine check dist/*` without publishing anything.

Before any package-index promotion, run the static release-readiness check from
the repository root:

```bash
code-mower migration release-readiness --json
```

It verifies the package version, current alpha tag references, release workflow
shape, TestPyPI/PyPI gates, trusted-publishing permissions, and the package-index
install rehearsal docs. Treat a failure as a release blocker. The JSON also
includes `setup_urls` for the GitHub environments, release workflow, PyPI
project pages, and trusted-publishing setup pages:

- [GitHub environments](https://github.com/codemower-ai/code-mower/settings/environments)
- [Release workflow](https://github.com/codemower-ai/code-mower/actions/workflows/release.yml)
- [TestPyPI trusted publishers](https://test.pypi.org/manage/project/code-mower/settings/publishing/)
- [PyPI trusted publishers](https://pypi.org/manage/project/code-mower/settings/publishing/)

Before publishing to TestPyPI, run the release workflow once with both publish
inputs set to `false` and confirm `build-distributions` and
`verify-distributions` are green.

Before switching docs to package-index install:

```bash
python3.12 -m venv /tmp/code-mower-pypi-smoke
/tmp/code-mower-pypi-smoke/bin/python -m pip install --upgrade pip
/tmp/code-mower-pypi-smoke/bin/python -m pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ code-mower==0.5.0b4
/tmp/code-mower-pypi-smoke/bin/code-mower --version
```

Then run the release-gate first-user rehearsal. For TestPyPI, pass the package
spec that points at the candidate distribution; for a GitHub tag, pass the tag
URL:

```bash
code-mower migration package-install-rehearsal \
  --package-spec code-mower==0.5.0b4 \
  --pip-index-url https://test.pypi.org/simple/ \
  --pip-extra-index-url https://pypi.org/simple/ \
  --python "$(command -v python3.12)" \
  --json
```

See [First-User Install Rehearsal](first-user-install-rehearsal.md) for the full
artifact contract. If you need to debug a step manually, the equivalent toy-repo
flow is:

```bash
mkdir /tmp/code-mower-toy && cd /tmp/code-mower-toy
git init
git config user.email code-mower-smoke@example.com
git config user.name "Code Mower Smoke"
printf '# Toy Repo\n' > README.md
git add README.md && git commit -m 'Initial commit'
/tmp/code-mower-pypi-smoke/bin/code-mower init --easy --apply --output-dir .code-mower.generated
bash .code-mower.generated/smoke-tests.sh
/tmp/code-mower-pypi-smoke/bin/code-mower doctor --preflight --json
/tmp/code-mower-pypi-smoke/bin/code-mower cloud dogfood --repo-slug example/toy-repo --endpoint http://localhost:3000/api/ingest --json
```

Promotion criteria:

- `code-mower --version` reports the intended version.
- The generated smoke tests pass.
- `doctor --preflight` has no failures.
- `cloud dogfood` stays dry-run by default and does not require a production
  token against a local endpoint.
- Public docs still describe privacy boundaries and do not imply cloud upload is
  required.

## When To Switch The README

Only change the primary README install command from GitHub-tag install to
`pipx install code-mower` after:

- TestPyPI install has passed.
- Production PyPI trusted publishing has passed.
- `pipx install code-mower` has been tested in a clean shell.
- A fresh toy repo completes `init --easy`, generated smoke tests,
  `doctor --preflight`, a starter value report, and cloud dogfood dry run.
