# PyPI Release Runbook

Code Mower users install from PyPI. The release workflow builds source and
wheel distributions, verifies them with `twine check`, and can publish to
TestPyPI or production PyPI through trusted publishing.

```bash
CODE_MOWER_PYTHON="$(command -v python3.12)"
pipx install --python "$CODE_MOWER_PYTHON" code-mower==1.4.0
```

## Current Status

- GitHub Release workflow builds distributions on every published release.
- The release workflow downloads the uploaded distributions and runs
  `twine check` before any optional PyPI publish job can start.
- TestPyPI publishing is gated behind the `testpypi` GitHub environment.
  Manual `workflow_dispatch` rehearsals publish only when
  `publish_testpypi=true`; published GitHub releases publish only when the
  `CODE_MOWER_TESTPYPI_PUBLISH` repository variable is `true`.
- Production PyPI publishing is gated behind the `pypi` GitHub environment.
  Manual `workflow_dispatch` rehearsals publish only when `publish_pypi=true`;
  published GitHub releases publish only when the `CODE_MOWER_PYPI_PUBLISH`
  repository variable is `true`.
- Trusted publishing is configured for TestPyPI and production PyPI.
- GitHub-tag install remains a fallback for release debugging, not the primary
  early-adopter path.

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
   unless a published GitHub release should automatically publish to TestPyPI.
   Manual `workflow_dispatch` runs ignore this variable and require
   `publish_testpypi=true`.
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
   gate. Manual `workflow_dispatch` runs ignore this variable and require
   `publish_pypi=true`, which is the preferred first production publish path.

## Workflow Dispatch Matrix

Use [https://github.com/codemower-ai/code-mower/actions/workflows/release.yml](https://github.com/codemower-ai/code-mower/actions/workflows/release.yml)
for manual release rehearsals:

| `publish_testpypi` | `publish_pypi` | Expected behavior |
| --- | --- | --- |
| `false` | `false` | Build, upload, download, and verify distributions only. |
| `true` | `false` | Build, verify, then publish to TestPyPI using the `testpypi` environment. |
| `false` | `true` | Build, verify, then publish to production PyPI using the `pypi` environment. Use only after the no-publish verification run is green; run TestPyPI first for trusted-publishing setup changes or risky packaging changes. |
| `true` | `true` | Avoid this for normal releases; publish to TestPyPI and PyPI as separate, auditable runs. |

Manual dispatch inputs are the only publish controls for manual runs. Repository
variables are intentionally scoped to `release` events so a dry-run dispatch with
both inputs set to `false` cannot publish just because a repository variable was
left enabled.

## Release Verification

Every GitHub release run should leave `build-distributions` and
`verify-distributions` green. The `verify-distributions` job exercises the
same artifact download path used by the optional PyPI publish job, then runs
`twine check dist/*` without publishing anything.

For stable releases, keep release metadata simple: the newest GitHub release
should be the `/releases/latest` result, and exact-version installs should
resolve from PyPI.

```bash
gh release view v1.4.0 \
  --repo codemower-ai/code-mower \
  --json tagName,isPrerelease
gh api repos/codemower-ai/code-mower/releases/latest \
  --jq '{tag_name,prerelease}'
```

Historical note: `DEC-427-LATEST` records the 2026-08-23 owner decision in
[PR #427](https://github.com/codemower-ai/code-mower/pull/427#issuecomment-5388373205):
beta.52 through v0.9.4 were published as **regular releases** (prerelease flag
off), not prerelease-flagged releases, so GitHub's `/releases/latest` endpoint
resolved for early adopters, automation, and package-index release checks. A
prerelease-flagged release cannot be returned by that endpoint. For v1.0.0 and
newer stable releases, the title, notes, README, and PyPI version should all
say release while preserving the supervised-pilot caveat.

Before any package-index promotion, run the static release-readiness check from
the repository root:

```bash
code-mower migration release-readiness --json
```

It verifies the package version, current release tag references, release workflow
shape, TestPyPI/PyPI gates, trusted-publishing permissions, and the package-index
install rehearsal docs. Treat a failure as a release blocker. The JSON also
includes `setup_urls` for the GitHub environments, release workflow, PyPI
project pages, and trusted-publishing setup pages:

- [GitHub environments](https://github.com/codemower-ai/code-mower/settings/environments)
- [Release workflow](https://github.com/codemower-ai/code-mower/actions/workflows/release.yml)
- [TestPyPI trusted publishers](https://test.pypi.org/manage/project/code-mower/settings/publishing/)
- [PyPI trusted publishers](https://pypi.org/manage/project/code-mower/settings/publishing/)

Before publishing to TestPyPI or PyPI, run the release workflow once with both
publish inputs set to `false` and confirm `build-distributions` and
  `verify-distributions` are green. TestPyPI remains useful for first-time
  trusted-publishing setup or risky packaging changes; routine publishing
  can go from the green no-publish verification run to production PyPI.

## v1.4.0 Post-Merge Release Runbook

Run these steps in this order after the release pull request merges. Every
irreversible step binds its inputs first: the exact merge head, the tag target,
the workflow run ID, and the artifact filenames and digests. Replace each
`REPLACE_WITH_...` value with the exact observed value, and keep tokens, profile
paths, private repository paths, and provider prose out of recorded output.

### 1. Bind the immutable merge head

```bash
REPO="codemower-ai/code-mower"
git fetch origin main --tags
RELEASE_SHA="$(git rev-parse origin/main)"
git checkout "$RELEASE_SHA"
test "$(git rev-parse HEAD)" = "$RELEASE_SHA"
code-mower migration release-readiness --json
```

### 2. Create and verify the annotated `v1.4.0` tag

```bash
git tag -a v1.4.0 "$RELEASE_SHA" -m "Code Mower v1.4.0"
git push origin refs/tags/v1.4.0
test "$(git rev-list -n 1 v1.4.0)" = "$RELEASE_SHA"
test "$(git ls-remote origin 'refs/tags/v1.4.0^{}' | awk '{print $1}')" = "$RELEASE_SHA"
```

### 3. Run `release.yml` with no publishing first

```bash
gh workflow run release.yml --repo "$REPO" --ref v1.4.0 \
  -f publish_testpypi=false -f publish_pypi=false
NO_PUBLISH_RUN_ID="REPLACE_WITH_EXACT_RUN_ID"
gh run watch "$NO_PUBLISH_RUN_ID" --repo "$REPO" --exit-status
gh run view "$NO_PUBLISH_RUN_ID" --repo "$REPO" \
  --json databaseId,headSha,event,status,conclusion,url
```

### 4. Publish TestPyPI only, then rehearse it

```bash
gh workflow run release.yml --repo "$REPO" --ref v1.4.0 \
  -f publish_testpypi=true -f publish_pypi=false
TESTPYPI_RUN_ID="REPLACE_WITH_EXACT_RUN_ID"
gh run watch "$TESTPYPI_RUN_ID" --repo "$REPO" --exit-status
TESTPYPI_WORK_DIR="$(mktemp -d /tmp/code-mower-v140-testpypi-rehearsal.XXXXXX)"
code-mower migration package-install-rehearsal \
  --package-spec code-mower==1.4.0 \
  --python "$(command -v python3.12)" \
  --work-dir "$TESTPYPI_WORK_DIR" \
  --pip-index-url https://test.pypi.org/simple/ \
  --pip-extra-index-url https://pypi.org/simple/ \
  --allow-package-index --upgrade-pip --json
```

### 5. Publish production PyPI only, then rehearse it

```bash
gh workflow run release.yml --repo "$REPO" --ref v1.4.0 \
  -f publish_testpypi=false -f publish_pypi=true
PYPI_RUN_ID="REPLACE_WITH_EXACT_RUN_ID"
gh run watch "$PYPI_RUN_ID" --repo "$REPO" --exit-status
PYPI_WORK_DIR="$(mktemp -d /tmp/code-mower-v140-pypi-rehearsal.XXXXXX)"
code-mower migration package-install-rehearsal \
  --package-spec code-mower==1.4.0 \
  --python "$(command -v python3.12)" \
  --work-dir "$PYPI_WORK_DIR" \
  --allow-package-index --upgrade-pip --json
```

### 6. Download the exact workflow artifact

```bash
PROD_DIST_DIR="$(mktemp -d /tmp/code-mower-v140-prod-dist.XXXXXX)"
gh run download "$PYPI_RUN_ID" --repo "$REPO" \
  --name code-mower-dist --dir "$PROD_DIST_DIR"
ls -1 "$PROD_DIST_DIR"
sha256sum "$PROD_DIST_DIR"/*
```

### 7. Compare SHA-256 digests with the files downloaded from PyPI

```bash
PYPI_DOWNLOAD_DIR="$(mktemp -d /tmp/code-mower-v140-pypi-download.XXXXXX)"
python3.12 -m pip download code-mower==1.4.0 --no-deps --no-binary :all: \
  --dest "$PYPI_DOWNLOAD_DIR"
python3.12 -m pip download code-mower==1.4.0 --no-deps --only-binary :all: \
  --dest "$PYPI_DOWNLOAD_DIR"
sha256sum "$PYPI_DOWNLOAD_DIR"/*
PROD_DIST_DIR="$PROD_DIST_DIR" PYPI_DOWNLOAD_DIR="$PYPI_DOWNLOAD_DIR" python3.12 - <<'PY'
import hashlib
import json
import os
from pathlib import Path


def digests(directory):
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(Path(directory).iterdir())
        if path.is_file()
    }


workflow = digests(os.environ["PROD_DIST_DIR"])
published = digests(os.environ["PYPI_DOWNLOAD_DIR"])
if len(workflow) != 2 or set(workflow) != set(published):
    raise SystemExit("workflow and PyPI artifact sets differ")
if any(workflow[name] != published[name] for name in workflow):
    raise SystemExit("workflow and PyPI SHA-256 values differ")
print(json.dumps({"artifact_count": len(workflow), "sha256_match": True}))
PY
```

Only continue when the artifact set and every digest match. A mismatch is a
release blocker: do not attach unverified files.

### 8. Create the GitHub Release with those exact assets

```bash
gh release create v1.4.0 "$PROD_DIST_DIR"/* --repo "$REPO" \
  --verify-tag --title "Code Mower v1.4.0" \
  --notes-file docs/v140-release-notes.md --latest --fail-on-no-commits
gh release view v1.4.0 --repo "$REPO" \
  --json tagName,targetCommitish,isDraft,isPrerelease,publishedAt,url,assets
RELEASE_EVENT_RUN_ID="REPLACE_WITH_EXACT_RELEASE_EVENT_RUN_ID"
gh run watch "$RELEASE_EVENT_RUN_ID" --repo "$REPO" --exit-status
gh run view "$RELEASE_EVENT_RUN_ID" --repo "$REPO" \
  --json databaseId,headSha,event,status,conclusion,url
```

Use `gh release upload v1.4.0 "$PROD_DIST_DIR"/* --repo "$REPO" --clobber` when
the release already exists. The publish jobs of the `release`-event run must stay
skipped; it must not publish again.

### 9. Install locally and verify Devin readiness

```bash
CODE_MOWER_PYTHON="$(command -v python3.12)"
test -n "$CODE_MOWER_PYTHON"
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" \
  'code-mower[coworker]==1.4.0'
code-mower --version
DEVIN_PROVIDER_PROFILE="REPLACE_WITH_PROTECTED_PROFILE_SELECTOR"
code-mower doctor --easy --devin \
  --provider-profile "$DEVIN_PROVIDER_PROFILE" --json
```

### 10. Run the required Claude + Codex + Devin campaign

```bash
RELEASE_PR="REPLACE_WITH_RELEASE_PR_NUMBER"
code-mower release campaign create \
  --release-tag v1.4.0 \
  --package-spec code-mower==1.4.0 \
  --providers claude,codex,devin \
  --required-providers claude,codex,devin \
  --qualification-context cold_install \
  --package-source pypi \
  --repo-slug codemower-ai/code-mower \
  --issue 912 --release-pr "$RELEASE_PR" \
  --provider-profile "$DEVIN_PROVIDER_PROFILE" \
  --apply --json
code-mower release campaign watch --release-tag v1.4.0 \
  --interval 10 --timeout 3600 --json
code-mower release campaign status --release-tag v1.4.0 --json
```

All three provider results must pass, and Devin's result must identify the
hosted transport before peer support is claimed.

### 11. Restart the three Boards from the release

```bash
CODE_MOWER_RELEASE_CHECKOUT="REPLACE_WITH_EXACT_V140_CHECKOUT"
BOARD_5342_REPO="REUSE_PRIVATE_INVENTORIED_SLUG"
BOARD_5342_REPO_PATH="REUSE_PRIVATE_INVENTORIED_PATH"
BOARD_5344_REPO="REUSE_PRIVATE_INVENTORIED_SLUG"
BOARD_5344_REPO_PATH="REUSE_PRIVATE_INVENTORIED_PATH"
code-mower board list --json
code-mower board stop --port 5332 --yes --json
code-mower board stop --port 5342 --yes --json
code-mower board stop --port 5344 --yes --json
nohup code-mower board serve --repo codemower-ai/code-mower \
  --repo-path "$CODE_MOWER_RELEASE_CHECKOUT" --host 127.0.0.1 \
  --port 5332 --record-events >/tmp/code-mower-board-5332.log 2>&1 &
nohup code-mower board serve --repo "$BOARD_5342_REPO" \
  --repo-path "$BOARD_5342_REPO_PATH" --host 127.0.0.1 \
  --port 5342 --record-events >/tmp/code-mower-board-5342.log 2>&1 &
nohup code-mower board serve --repo "$BOARD_5344_REPO" \
  --repo-path "$BOARD_5344_REPO_PATH" --host 127.0.0.1 \
  --port 5344 --record-events >/tmp/code-mower-board-5344.log 2>&1 &
code-mower board list --json
code-mower board doctor --repo codemower-ai/code-mower \
  --repo-path "$CODE_MOWER_RELEASE_CHECKOUT" --json
```

Each inventory row must report serving and installed version `1.4.0`. The port
5332 Board must be restarted from the exact v1.4.0 release checkout because its
pre-release repository path is stale. Run `code-mower board doctor` for the other
two Boards with their privately inventoried slugs and paths. Do not use raw
process kills or Board reset, and do not copy private repository slugs or paths
into public evidence.

### 12. Dry-run, inspect, then upload metadata-only cloud evidence

```bash
code-mower cloud doctor --install-id codex-code-mower --probe-service --json
code-mower release campaign upload --release-tag v1.4.0 \
  --install-id codex-code-mower --team-id jeff-internal --json
code-mower release campaign upload --release-tag v1.4.0 \
  --install-id codex-code-mower --team-id jeff-internal --yes --json
BOARD_SNAPSHOT_DIR="$(mktemp -d /tmp/code-mower-v140-board-snapshot.XXXXXX)"
code-mower cloud board-snapshot \
  --repo-path "$CODE_MOWER_RELEASE_CHECKOUT" \
  --repo-slug codemower-ai/code-mower \
  --output-dir "$BOARD_SNAPSHOT_DIR" \
  --install-id codex-code-mower --team-id jeff-internal --json
code-mower cloud upload "$BOARD_SNAPSHOT_DIR" \
  --install-id codex-code-mower --dry-run --json
code-mower cloud upload "$BOARD_SNAPSHOT_DIR" \
  --install-id codex-code-mower --yes --json
```

Record accepted event identifiers and counts only, never report prose, profile
paths, tokens, or local configuration.

## Cache Bypass And Propagation Triage

Use cache-bypassing exact-version installs when validating a just-published
release. That keeps stale local wheels from looking like a successful release
and keeps PyPI propagation delays from looking like source regressions.

For pipx:

```bash
python3.12 --version
export CODE_MOWER_PYTHON="$(command -v python3.12)"
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" code-mower==1.4.0
code-mower --version
```

For uv:

```bash
uv python install 3.12
uv tool install --python 3.12 --reinstall --refresh-package code-mower code-mower==1.4.0
code-mower --version
```

Before the candidate is available on TestPyPI or PyPI, validate the local wheel
from the release checkout:

```bash
scripts/dev-python -m build
export CODE_MOWER_PYTHON="$(command -v python3.12)"
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" dist/code_mower-*.whl
uv tool install --python 3.12 --reinstall dist/code_mower-*.whl
```

If an exact-version install fails within a few minutes of publication, retry
with the cache-bypass command. The package-install rehearsal does this for
package-index specs after `--allow-package-index`: it passes
`pip --no-cache-dir` and retries the install three times by default. Repeated
"no matching distribution" errors, HTTP/index errors, or TestPyPI/PyPI timeouts
after those attempts are package-index or network propagation until the
uploaded artifact is visible and installable. A command that installs
successfully but reports the wrong `code-mower --version`, fails to start, or
fails the first-user rehearsal is a release blocker.

For production PyPI verification:

```bash
python3.12 -m venv /tmp/code-mower-pypi-smoke
/tmp/code-mower-pypi-smoke/bin/python -m pip install --upgrade pip
/tmp/code-mower-pypi-smoke/bin/python -m pip install code-mower==1.4.0
/tmp/code-mower-pypi-smoke/bin/code-mower --version
```

Then run the release-gate first-user rehearsal against the same package:

```bash
code-mower migration package-install-rehearsal \
  --package-spec code-mower==1.4.0 \
  --allow-package-index \
  --upgrade-pip \
  --python "$(command -v python3.12)" \
  --json
```

For a TestPyPI candidate, add:

```bash
  --pip-index-url https://test.pypi.org/simple/ \
  --pip-extra-index-url https://pypi.org/simple/
```

`code-mower release qualify` and `code-mower release campaign` accept the
equivalent closed `--package-source testpypi` flag (default: `pypi`) to
qualify the same TestPyPI candidate before it is announced or marked current
on production PyPI -- see
[Release Qualification](release-qualification.md#testpypi-candidates):

```bash
code-mower release qualify \
  --release-tag v1.4.0 \
  --package-spec code-mower==1.4.0 \
  --output result.json \
  --package-source testpypi \
  --execute
```

See [First-User Install Rehearsal](first-user-install-rehearsal.md) for the full
artifact contract. If you need to debug a step manually, the equivalent toy-repo
flow is:

Code Mower-created scratch repositories use the non-personal Git identity
`Code Mower Scratch <code-mower-scratch@example.com>`.

```bash
mkdir /tmp/code-mower-toy && cd /tmp/code-mower-toy
git init
git config user.email code-mower-scratch@example.com
git config user.name "Code Mower Scratch"
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

## README Install Command Policy

The primary README command stays on the exact current release so an adopter,
an agent, and the release rehearsal all install the same artifact:

```bash
CODE_MOWER_PYTHON="$(command -v python3.12)"
pipx install --python "$CODE_MOWER_PYTHON" code-mower==1.4.0
```

An unpinned `pipx install code-mower` may be mentioned as a convenience only
after each release verifies that:

- TestPyPI install has passed.
- Production PyPI trusted publishing has passed.
- `pipx install code-mower` has been tested in a clean shell.
- A fresh toy repo completes `init --easy`, generated smoke tests,
  `doctor --preflight`, a starter value report, and cloud dogfood dry run.

The pinned command remains the canonical copy-paste path even after those
checks pass; bump it with every release.
