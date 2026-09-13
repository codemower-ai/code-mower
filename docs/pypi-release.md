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
irreversible step binds its inputs and asserts them before it runs: the exact
merge commit OID, the tag target, the workflow run identity and job posture, and
the artifact filenames and digests. Every check below is an assertion that exits
nonzero on mismatch; printed JSON alone is not evidence. Replace each
`REPLACE_WITH_...` value with the exact observed value, and keep tokens, profile
paths, private repository paths, and provider prose out of recorded output.

### 1. Bind the immutable release commit from the merged release PR

`origin/main` is mutable and may already carry later commits, so the release
commit is the release pull request's own merge commit OID.

```bash
REPO="codemower-ai/code-mower"
RELEASE_PR="REPLACE_WITH_RELEASE_PR_NUMBER"
test "$(gh pr view "$RELEASE_PR" --repo "$REPO" --json state --jq '.state')" = "MERGED"
RELEASE_SHA="$(gh pr view "$RELEASE_PR" --repo "$REPO" \
  --json mergeCommit --jq '.mergeCommit.oid')"
printf '%s\n' "$RELEASE_SHA" | grep -Eq '^[0-9a-f]{40}$'
git fetch origin "$RELEASE_SHA"
test "$(git cat-file -t "$RELEASE_SHA")" = "commit"
```

A working checkout can retain dirty or untracked files, so the release source is
a fresh clone bound to that commit and machine-asserted clean before anything is
built or installed from it.

```bash
RELEASE_CHECKOUT="$(mktemp -d /tmp/code-mower-v140-release-src.XXXXXX)/code-mower"
git clone --no-checkout "https://github.com/$REPO.git" "$RELEASE_CHECKOUT"
git -C "$RELEASE_CHECKOUT" fetch origin "$RELEASE_SHA"
git -C "$RELEASE_CHECKOUT" checkout --detach "$RELEASE_SHA"
test "$(git -C "$RELEASE_CHECKOUT" rev-parse HEAD)" = "$RELEASE_SHA"
test -z "$(git -C "$RELEASE_CHECKOUT" status --porcelain --untracked-files=all)"
```

### 2. Run release readiness from a fresh source environment at that commit

The ambient `code-mower` executable is still the previous release, so readiness
runs from a clean virtual environment built out of the clean `RELEASE_CHECKOUT`
clone of `RELEASE_SHA`.

```bash
RELEASE_ENV="$(mktemp -d /tmp/code-mower-v140-release-env.XXXXXX)"
python3.12 -m venv "$RELEASE_ENV/venv"
RELEASE_PYTHON="$RELEASE_ENV/venv/bin/python"
env -u PIP_INDEX_URL -u PIP_EXTRA_INDEX_URL -u PIP_FIND_LINKS \
  PIP_CONFIG_FILE=/dev/null "$RELEASE_PYTHON" -m pip install --no-cache-dir \
  --index-url https://pypi.org/simple/ "$RELEASE_CHECKOUT"
RELEASE_CLI="$RELEASE_ENV/venv/bin/code-mower"
test "$("$RELEASE_CLI" --version)" = "code-mower 1.4.0"
(cd "$RELEASE_CHECKOUT" && "$RELEASE_CLI" migration release-readiness --json) \
  >"$RELEASE_ENV/readiness.json"
READINESS_JSON="$RELEASE_ENV/readiness.json" "$RELEASE_PYTHON" - <<'PY'
import json
import os

report = json.loads(open(os.environ["READINESS_JSON"], encoding="utf-8").read())
checks = {row["id"]: row["status"] for row in report["checks"]}
required = [
    "package-version-consistency",
    "committed-package-manifest-version",
    "committed-package-manifest-matches-generated",
    "post-merge-release-runbook-ordered",
    "post-merge-release-runbook-asserted",
]
missing = [check for check in required if checks.get(check) != "pass"]
failing = sorted(check for check, status in checks.items() if status == "fail")
if missing or failing:
    raise SystemExit(f"release readiness is not ready: {missing or failing}")
print(json.dumps({"checks": len(checks), "required_pass": required}))
PY
```

### 3. Create and verify the annotated `v1.4.0` tag on that exact commit

```bash
git tag -a v1.4.0 "$RELEASE_SHA" -m "Code Mower v1.4.0"
git push origin refs/tags/v1.4.0
test "$(git rev-list -n 1 v1.4.0)" = "$RELEASE_SHA"
test "$(git ls-remote origin 'refs/tags/v1.4.0^{}' | awk '{print $1}')" = "$RELEASE_SHA"
```

### 4. Install the workflow-run assertion helper

Every workflow run below is asserted with this helper: workflow identity,
triggering event, exact head SHA, `success` conclusion, successful
`build-distributions` and `verify-distributions` jobs, and the exact posture of
both publish jobs. A run whose only reported jobs are skipped publish jobs fails.
A job that is expected to skip must be reported skipped or be absent from the
run; a job that is expected to publish must report `success`.

```bash
cat >"$RELEASE_ENV/assert_release_run.py" <<'PY'
"""Assert one release workflow run's identity, head, conclusion, and job posture."""

import json
import os
import subprocess
import sys

EXPECTED_WORKFLOW = "Code Mower Release"
BUILD_JOBS = ("build-distributions", "verify-distributions")
SKIPPED = {"skipped", "absent"}


def run_view(repo: str, run_id: str) -> dict:
    completed = subprocess.run(
        [
            "gh", "run", "view", run_id, "--repo", repo, "--json",
            "databaseId,workflowName,headSha,event,status,conclusion,url,jobs",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def job_posture(run: dict, job_name: str) -> str:
    for job in run.get("jobs") or []:
        if job.get("name") == job_name:
            return str(job.get("conclusion") or job.get("status") or "unknown")
    return "absent"


def main() -> None:
    repo, run_id, event, head_sha, testpypi, pypi = sys.argv[1:7]
    run = run_view(repo, run_id)
    problems = []
    if str(run.get("databaseId")) != run_id:
        problems.append("run id does not match the inspected run")
    if run.get("workflowName") != EXPECTED_WORKFLOW:
        problems.append("run belongs to another workflow")
    if run.get("event") != event:
        problems.append(f"event is {run.get('event')}, not {event}")
    if run.get("headSha") != head_sha:
        problems.append("run head is not the exact release commit")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        problems.append("run did not complete successfully")
    for job_name in BUILD_JOBS:
        actual = job_posture(run, job_name)
        if actual != "success":
            problems.append(f"{job_name} is {actual}, expected success")
    for job_name, expected in (
        ("publish-testpypi", testpypi),
        ("publish-pypi", pypi),
    ):
        actual = job_posture(run, job_name)
        if expected == "skipped" and actual not in SKIPPED:
            problems.append(f"{job_name} is {actual}, expected skipped")
        if expected == "success" and actual != "success":
            problems.append(f"{job_name} is {actual}, expected success")
    if problems:
        raise SystemExit("; ".join(problems))
    print(json.dumps({
        "run_id": run_id,
        "event": event,
        "head_sha": head_sha,
        "build_distributions": job_posture(run, "build-distributions"),
        "verify_distributions": job_posture(run, "verify-distributions"),
        "publish_testpypi": job_posture(run, "publish-testpypi"),
        "publish_pypi": job_posture(run, "publish-pypi"),
        "url": run.get("url"),
    }))


main()
PY
```

### 5. Run `release.yml` with no publishing first

Both publish jobs must skip on this run.

```bash
gh workflow run release.yml --repo "$REPO" --ref v1.4.0 \
  -f publish_testpypi=false -f publish_pypi=false
NO_PUBLISH_RUN_ID="REPLACE_WITH_EXACT_RUN_ID"
gh run watch "$NO_PUBLISH_RUN_ID" --repo "$REPO" --exit-status
"$RELEASE_PYTHON" "$RELEASE_ENV/assert_release_run.py" "$REPO" \
  "$NO_PUBLISH_RUN_ID" workflow_dispatch "$RELEASE_SHA" skipped skipped
```

### 6. Publish TestPyPI only, then rehearse the exact candidate from TestPyPI

TestPyPI must publish while production PyPI skips. pip does not prefer
`--index-url` over `--extra-index-url`, so the candidate artifacts are fetched
from TestPyPI alone, with no cache, no dependency resolution, and no ambient pip
configuration; their exact filenames and digests are bound before the rehearsal,
which then installs those local files. Dependencies resolve separately from
canonical PyPI.

```bash
gh workflow run release.yml --repo "$REPO" --ref v1.4.0 \
  -f publish_testpypi=true -f publish_pypi=false
TESTPYPI_RUN_ID="REPLACE_WITH_EXACT_RUN_ID"
gh run watch "$TESTPYPI_RUN_ID" --repo "$REPO" --exit-status
"$RELEASE_PYTHON" "$RELEASE_ENV/assert_release_run.py" "$REPO" \
  "$TESTPYPI_RUN_ID" workflow_dispatch "$RELEASE_SHA" success skipped

TESTPYPI_DIST_DIR="$(mktemp -d /tmp/code-mower-v140-testpypi-dist.XXXXXX)"
env -u PIP_INDEX_URL -u PIP_EXTRA_INDEX_URL -u PIP_FIND_LINKS \
  PIP_CONFIG_FILE=/dev/null python3.12 -m pip download code-mower==1.4.0 \
  --no-cache-dir --no-deps --only-binary :all: \
  --index-url https://test.pypi.org/simple/ --dest "$TESTPYPI_DIST_DIR"
env -u PIP_INDEX_URL -u PIP_EXTRA_INDEX_URL -u PIP_FIND_LINKS \
  PIP_CONFIG_FILE=/dev/null python3.12 -m pip download code-mower==1.4.0 \
  --no-cache-dir --no-deps --no-binary :all: \
  --index-url https://test.pypi.org/simple/ --dest "$TESTPYPI_DIST_DIR"
TESTPYPI_DIST_DIR="$TESTPYPI_DIST_DIR" "$RELEASE_PYTHON" - <<'PY'
import hashlib
import json
import os
from pathlib import Path

files = sorted(
    path for path in Path(os.environ["TESTPYPI_DIST_DIR"]).iterdir() if path.is_file()
)
digests = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
wheels = [name for name in digests if name == "code_mower-1.4.0-py3-none-any.whl"]
sdists = [name for name in digests if name == "code_mower-1.4.0.tar.gz"]
if len(digests) != 2 or len(wheels) != 1 or len(sdists) != 1:
    raise SystemExit(f"TestPyPI candidate artifact set is unexpected: {sorted(digests)}")
print(json.dumps({"source": "testpypi", "artifacts": digests}, sort_keys=True))
PY
TESTPYPI_WHEEL="$TESTPYPI_DIST_DIR/code_mower-1.4.0-py3-none-any.whl"
test -f "$TESTPYPI_WHEEL"
TESTPYPI_WORK_DIR="$(mktemp -d /tmp/code-mower-v140-testpypi-rehearsal.XXXXXX)"
env -u PIP_INDEX_URL -u PIP_EXTRA_INDEX_URL PIP_CONFIG_FILE=/dev/null \
  "$RELEASE_CLI" migration package-install-rehearsal \
  --package-spec "$TESTPYPI_WHEEL" \
  --python "$(command -v python3.12)" \
  --work-dir "$TESTPYPI_WORK_DIR" \
  --pip-index-url https://pypi.org/simple/ \
  --pip-no-cache --upgrade-pip --json
```

The rehearsal installs the exact TestPyPI file, so production PyPI cannot satisfy
this step; only its dependencies come from canonical PyPI.

### 7. Publish production PyPI only, then rehearse the published package

Production PyPI must publish while TestPyPI skips, and the rehearsal must reach
canonical `https://pypi.org/simple/` explicitly with no cache, so no ambient
`pip.conf`, `PIP_INDEX_URL`, or mirror can satisfy a production-labelled gate.

```bash
gh workflow run release.yml --repo "$REPO" --ref v1.4.0 \
  -f publish_testpypi=false -f publish_pypi=true
PYPI_RUN_ID="REPLACE_WITH_EXACT_RUN_ID"
gh run watch "$PYPI_RUN_ID" --repo "$REPO" --exit-status
"$RELEASE_PYTHON" "$RELEASE_ENV/assert_release_run.py" "$REPO" \
  "$PYPI_RUN_ID" workflow_dispatch "$RELEASE_SHA" skipped success

PYPI_WORK_DIR="$(mktemp -d /tmp/code-mower-v140-pypi-rehearsal.XXXXXX)"
env -u PIP_INDEX_URL -u PIP_EXTRA_INDEX_URL -u PIP_FIND_LINKS \
  PIP_CONFIG_FILE=/dev/null "$RELEASE_CLI" migration package-install-rehearsal \
  --package-spec code-mower==1.4.0 \
  --python "$(command -v python3.12)" \
  --work-dir "$PYPI_WORK_DIR" \
  --pip-index-url https://pypi.org/simple/ \
  --allow-package-index --pip-no-cache --upgrade-pip --json
```

### 8. Download the exact workflow artifact

```bash
PROD_DIST_DIR="$(mktemp -d /tmp/code-mower-v140-prod-dist.XXXXXX)"
gh run download "$PYPI_RUN_ID" --repo "$REPO" \
  --name code-mower-dist --dir "$PROD_DIST_DIR"
sha256sum "$PROD_DIST_DIR"/*
```

### 9. Compare SHA-256 digests with the files downloaded from canonical PyPI

```bash
PYPI_DOWNLOAD_DIR="$(mktemp -d /tmp/code-mower-v140-pypi-download.XXXXXX)"
env -u PIP_INDEX_URL -u PIP_EXTRA_INDEX_URL -u PIP_FIND_LINKS \
  PIP_CONFIG_FILE=/dev/null python3.12 -m pip download code-mower==1.4.0 \
  --no-cache-dir --no-deps --no-binary :all: \
  --index-url https://pypi.org/simple/ --dest "$PYPI_DOWNLOAD_DIR"
env -u PIP_INDEX_URL -u PIP_EXTRA_INDEX_URL -u PIP_FIND_LINKS \
  PIP_CONFIG_FILE=/dev/null python3.12 -m pip download code-mower==1.4.0 \
  --no-cache-dir --no-deps --only-binary :all: \
  --index-url https://pypi.org/simple/ --dest "$PYPI_DOWNLOAD_DIR"
PROD_DIST_DIR="$PROD_DIST_DIR" PYPI_DOWNLOAD_DIR="$PYPI_DOWNLOAD_DIR" \
  "$RELEASE_PYTHON" - <<'PY'
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
expected = {"code_mower-1.4.0-py3-none-any.whl", "code_mower-1.4.0.tar.gz"}
if set(workflow) != expected or set(published) != expected:
    raise SystemExit("workflow and PyPI artifact sets differ")
if any(workflow[name] != published[name] for name in workflow):
    raise SystemExit("workflow and PyPI SHA-256 values differ")
print(json.dumps({"artifact_count": len(workflow), "sha256_match": True}))
PY
```

Only continue when the artifact set and every digest match. A mismatch is a
release blocker: do not attach unverified files.

### 10. Assert the publish variables are off before creating the Release

The published Release triggers one `release`-event run whose publish jobs are
gated on repository variables. Assert they cannot republish before the
irreversible release creation, not afterwards.

`vars.CODE_MOWER_TESTPYPI_PUBLISH` and `vars.CODE_MOWER_PYPI_PUBLISH` resolve
through organization scope when the repository does not define them, so an
absent repository variable is not a false value. Each variable must exist at
repository scope and equal `false`, which is also what overrides an inherited
organization value. Anything else -- absent, `true`, or unparseable -- fails
closed.

```bash
REPO="$REPO" "$RELEASE_PYTHON" - <<'PY'
import json
import os
import subprocess

repo = os.environ["REPO"]
REQUIRED_VARIABLES = ("CODE_MOWER_TESTPYPI_PUBLISH", "CODE_MOWER_PYPI_PUBLISH")


def repository_variable(name: str) -> str | None:
    """Read one repository-scoped variable, never an inherited organization one."""
    completed = subprocess.run(
        ["gh", "api", f"repos/{repo}/actions/variables/{name}"],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return None
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    value = payload.get("value")
    return value if isinstance(value, str) else None


values = {name: repository_variable(name) for name in REQUIRED_VARIABLES}
blocked = sorted(
    name
    for name, value in values.items()
    if value is None or value.strip().lower() != "false"
)
if blocked:
    raise SystemExit(
        "repository-scope publish variables must exist and equal false: " f"{blocked}"
    )
print(json.dumps({"republish_repository_variables_false": sorted(values)}))
PY
```

### 11. Create the GitHub Release with those exact assets and verify them

An existing `v1.4.0` release is never clobbered: inspect it first and stop
unless its tag and its exact asset set and digests already match
`PROD_DIST_DIR`. Install the asset assertion first. It downloads the Release's
own assets and requires the exact filename set and every SHA-256 value to equal
`PROD_DIST_DIR`, with exactly one wheel and one sdist:

```bash
cat >"$RELEASE_ENV/assert_release_assets.py" <<'PY'
"""Assert the GitHub Release tag and its downloaded assets match PROD_DIST_DIR."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

EXPECTED = {"code_mower-1.4.0-py3-none-any.whl", "code_mower-1.4.0.tar.gz"}


def digests(directory: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.iterdir())
        if path.is_file()
    }


def main() -> None:
    mode = sys.argv[1]
    repo = os.environ["REPO"]
    release_sha = os.environ["RELEASE_SHA"]
    local = digests(Path(os.environ["PROD_DIST_DIR"]))
    view = json.loads(subprocess.run(
        ["gh", "release", "view", "v1.4.0", "--repo", repo, "--json",
         "tagName,isDraft,isPrerelease,assets"],
        check=True, capture_output=True, text=True,
    ).stdout)
    problems = []
    if view.get("tagName") != "v1.4.0":
        problems.append("release tag is not v1.4.0")
    if view.get("isDraft") or view.get("isPrerelease"):
        problems.append("release is a draft or prerelease")
    tag_target = subprocess.run(
        ["git", "rev-list", "-n", "1", "v1.4.0"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    if tag_target != release_sha:
        problems.append("release tag does not target the exact release commit")
    if set(local) != EXPECTED:
        problems.append(f"local artifact set is unexpected: {sorted(local)}")
    asset_names = {asset["name"] for asset in view.get("assets") or []}
    if asset_names != set(local):
        problems.append(f"release asset set differs: {sorted(asset_names)}")
    with tempfile.TemporaryDirectory() as scratch:
        target = Path(scratch)
        subprocess.run(
            ["gh", "release", "download", "v1.4.0", "--repo", repo,
             "--dir", str(target)],
            check=True, capture_output=True, text=True,
        )
        downloaded = digests(target)
    if downloaded != local:
        problems.append("release asset SHA-256 values differ from PROD_DIST_DIR")
    if problems:
        raise SystemExit(f"{mode} release assets are not acceptable: {problems}")
    print(json.dumps({
        "mode": mode,
        "assets": sorted(local),
        "sha256_match": True,
    }, sort_keys=True))


main()
PY
```

Assets are downloaded into a private empty scratch directory, so nothing is
overwritten anywhere, and a `v1.4.0` release whose assets differ stops the
runbook for inspection.

```bash
if gh release view v1.4.0 --repo "$REPO" >/dev/null 2>&1; then
  REPO="$REPO" PROD_DIST_DIR="$PROD_DIST_DIR" RELEASE_SHA="$RELEASE_SHA" \
    "$RELEASE_PYTHON" "$RELEASE_ENV/assert_release_assets.py" existing
else
  gh release create v1.4.0 "$PROD_DIST_DIR"/* --repo "$REPO" \
    --verify-tag --title "Code Mower v1.4.0" \
    --notes-file docs/v140-release-notes.md --latest --fail-on-no-commits
fi
REPO="$REPO" PROD_DIST_DIR="$PROD_DIST_DIR" RELEASE_SHA="$RELEASE_SHA" \
  "$RELEASE_PYTHON" "$RELEASE_ENV/assert_release_assets.py" created
gh release view v1.4.0 --repo "$REPO" \
  --json tagName,targetCommitish,isDraft,isPrerelease,publishedAt,url,assets
```

### 12. Assert the `release`-event run published nothing

```bash
RELEASE_EVENT_RUN_ID="REPLACE_WITH_EXACT_RELEASE_EVENT_RUN_ID"
gh run watch "$RELEASE_EVENT_RUN_ID" --repo "$REPO" --exit-status
"$RELEASE_PYTHON" "$RELEASE_ENV/assert_release_run.py" "$REPO" \
  "$RELEASE_EVENT_RUN_ID" release "$RELEASE_SHA" skipped skipped
```

### 13. Install locally and require hosted Devin readiness

`code-mower doctor --easy --devin` reports the unselected posture and can exit 0
with `skip`, so it does not prove readiness. Select hosted Devin explicitly with
a supported generated configuration, then require the hosted checks to pass for
the exact `codemower-ai/code-mower` scope. `provider.devin.permissions` is
reported, never probed: the doctor cannot read the account's own permission
settings, so `skip` is accepted only when the account owner separately confirms
them. Supply that confirmation privately as `confirmed`; any other value, an
unset variable, or any other check status fails closed.

```bash
CODE_MOWER_PYTHON="$(command -v python3.12)"
test -n "$CODE_MOWER_PYTHON"
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" \
  'code-mower[coworker]==1.4.0'
test "$(code-mower --version)" = "code-mower 1.4.0"

DEVIN_PROVIDER_PROFILE="REPLACE_WITH_PROTECTED_PROFILE_SELECTOR"
DEVIN_PERMISSIONS_OWNER_CONFIRMED="REPLACE_WITH_OWNER_CONFIRMATION"
DEVIN_DOCTOR_DIR="$(mktemp -d /tmp/code-mower-v140-devin-doctor.XXXXXX)"
code-mower init code-mower.yml --profile recommended \
  --set-transport devin=devin_api_v3 --apply --output-dir "$DEVIN_DOCTOR_DIR"
code-mower doctor "$DEVIN_DOCTOR_DIR/code-mower.yml" --profile recommended \
  --devin --repo codemower-ai/code-mower \
  --provider-profile "$DEVIN_PROVIDER_PROFILE" --json >"$DEVIN_DOCTOR_DIR/doctor.json"
DEVIN_DOCTOR_JSON="$DEVIN_DOCTOR_DIR/doctor.json" \
  DEVIN_PERMISSIONS_OWNER_CONFIRMED="$DEVIN_PERMISSIONS_OWNER_CONFIRMED" \
  "$RELEASE_PYTHON" - <<'PY'
import json
import os

report = json.loads(open(os.environ["DEVIN_DOCTOR_JSON"], encoding="utf-8").read())
checks = {
    row["name"]: row["status"]
    for row in report.get("checks", [])
    if row["name"].startswith("provider.devin.")
}
required = (
    "provider.devin.selection",
    "provider.devin.capabilities",
    "provider.devin.hosted_credentials",
    "provider.devin.repository_scope",
    "provider.devin.lifecycle",
)
blocked = [name for name in required if checks.get(name) != "pass"]
if blocked:
    raise SystemExit(f"hosted Devin readiness is blocked: {blocked}")
permissions = checks.get("provider.devin.permissions")
owner_confirmed = os.environ.get("DEVIN_PERMISSIONS_OWNER_CONFIRMED", "").strip().lower()
if permissions == "skip" and owner_confirmed != "confirmed":
    raise SystemExit(
        "Devin permissions are reported skip and the account owner has not "
        "separately confirmed them"
    )
if permissions not in {"pass", "skip"}:
    raise SystemExit(f"Devin permission check is {permissions!r}")
print(json.dumps({
    "devin_transport": "hosted",
    "required_pass": list(required),
    "permissions": permissions,
    "owner_confirmed": permissions == "pass" or owner_confirmed == "confirmed",
}))
PY
```

Keep the generated directory, profile selector, credential values, organization
identifier, and repository inventory out of recorded evidence.

### 14. Run the required Claude + Codex + Devin campaign

The campaign is a gate, so its watch and status output is saved and asserted:
the campaign must finish `complete`, the selected and required provider sets
must be exactly Claude, Codex, and Devin, every required lane must hold a
passing adoption result, and the Devin lane must report the verified hosted
bridge transport (`devin_api_v3`, the only hosted Code Mower Devin transport,
already selected explicitly in step 13). The protected profile is named on
watch and status too, so a protected or ambiguous profile stays selected.

```bash
CAMPAIGN_DIR="$(mktemp -d /tmp/code-mower-v140-campaign.XXXXXX)"
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
  --apply --json >"$CAMPAIGN_DIR/create.json"
code-mower release campaign watch --release-tag v1.4.0 \
  --provider-profile "$DEVIN_PROVIDER_PROFILE" \
  --interval 10 --timeout 3600 --json >"$CAMPAIGN_DIR/watch.json"
code-mower release campaign status --release-tag v1.4.0 \
  --provider-profile "$DEVIN_PROVIDER_PROFILE" --json >"$CAMPAIGN_DIR/status.json"
CAMPAIGN_DIR="$CAMPAIGN_DIR" "$RELEASE_PYTHON" - <<'PY'
import json
import os
from pathlib import Path

campaign_dir = Path(os.environ["CAMPAIGN_DIR"])
REQUIRED_PROVIDERS = {"claude", "codex", "devin"}
PASSING_OUTCOMES = {"pass", "pass_with_warnings"}
CAMPAIGN_SCHEMA = "code_mower.releaseCampaign.v1"
WATCH_SCHEMA = "code_mower.releaseCampaignWatch.v1"
ADOPTION_RESULT_SCHEMA = "code_mower.adoptionResult.v1"
CAMPAIGN_ID = "campaign-v1.4.0"
RELEASE_TAG = "v1.4.0"
PACKAGE_IDENTITY = "code-mower"
VERSION = "1.4.0"


def load(name: str) -> dict:
    return json.loads((campaign_dir / name).read_text(encoding="utf-8"))


watch = load("watch.json")
status = load("status.json")
problems = []
if watch.get("schema") != WATCH_SCHEMA or watch.get("mode") != "release-campaign-watch":
    problems.append(f"watch schema/mode is {watch.get('schema')!r}/{watch.get('mode')!r}")
watch_identity = {
    "campaign_id": CAMPAIGN_ID,
    "release_tag": RELEASE_TAG,
    "package_identity": PACKAGE_IDENTITY,
    "qualification_context": "cold_install",
}
for key, expected in watch_identity.items():
    if watch.get(key) != expected:
        problems.append(f"watch {key} is {watch.get(key)!r}, expected {expected!r}")
if watch.get("status") != "complete" or watch.get("stop_reason") != "complete":
    problems.append(
        f"watch stopped as {watch.get('stop_reason')!r} with status {watch.get('status')!r}"
    )
watch_lanes = {
    str(row.get("provider") or ""): row
    for row in watch.get("providers") or []
    if isinstance(row, dict)
}
if set(watch_lanes) != REQUIRED_PROVIDERS:
    problems.append(f"watch provider set is {sorted(watch_lanes)}")
for name in sorted(REQUIRED_PROVIDERS & set(watch_lanes)):
    row = watch_lanes[name]
    if row.get("posture") != "required" or row.get("state") != "complete" or row.get("error"):
        problems.append(
            f"watch {name} summary is {row.get('posture')!r}/{row.get('state')!r}"
        )
if status.get("schema") != CAMPAIGN_SCHEMA:
    problems.append(f"campaign schema is {status.get('schema')!r}")
status_identity = {
    "campaign_id": CAMPAIGN_ID,
    "release_tag": RELEASE_TAG,
    "package_identity": PACKAGE_IDENTITY,
    "package_spec": f"{PACKAGE_IDENTITY}=={VERSION}",
    "normalized_version": VERSION,
    "qualification_context": "cold_install",
    "package_source": "pypi",
    "repo_slug": "codemower-ai/code-mower",
}
for key, expected in status_identity.items():
    if status.get(key) != expected:
        problems.append(f"campaign {key} is {status.get(key)!r}, expected {expected!r}")
if status.get("status") != "complete":
    problems.append(f"campaign status is {status.get('status')!r}, not complete")
if status.get("dry_run") is not False:
    problems.append(f"campaign dry_run is {status.get('dry_run')!r}, expected False")
if status.get("provider_posture_configured") is not True:
    problems.append("campaign provider posture was not explicitly configured")
lanes = {
    str(row.get("provider") or ""): row
    for row in status.get("providers") or []
    if isinstance(row, dict)
}
if set(lanes) != REQUIRED_PROVIDERS:
    problems.append(f"campaign provider set is {sorted(lanes)}")
required = {name for name, row in lanes.items() if row.get("posture") == "required"}
if required != REQUIRED_PROVIDERS:
    problems.append(f"required provider set is {sorted(required)}")
for name in sorted(REQUIRED_PROVIDERS & set(lanes)):
    lane = lanes[name]
    if lane.get("state") != "complete":
        problems.append(f"{name} lane state is {lane.get('state')!r}")
    if lane.get("dispatch_mode") != "applied" or lane.get("error"):
        problems.append(f"{name} lane dispatch is {lane.get('dispatch_mode')!r}")
    result = lane.get("adoption_result")
    result = result if isinstance(result, dict) else {}
    if result.get("schema") != ADOPTION_RESULT_SCHEMA:
        problems.append(f"{name} lane result schema is {result.get('schema')!r}")
    if (
        result.get("release_tag") != RELEASE_TAG
        or result.get("package_identity") != PACKAGE_IDENTITY
        or result.get("normalized_version") != VERSION
    ):
        problems.append(f"{name} lane result is not bound to {RELEASE_TAG}")
    outcome = result.get("outcome")
    if outcome not in PASSING_OUTCOMES:
        problems.append(f"{name} lane result outcome is {outcome!r}")
devin = lanes.get("devin") or {}
devin_ref = devin.get("dispatch_ref")
devin_ref = devin_ref if isinstance(devin_ref, dict) else {}
if devin.get("driver") != "hosted_bridge" or devin.get("transport_verified") is not True:
    problems.append("Devin lane did not verify the hosted bridge transport")
if devin_ref.get("transport_kind") != "devin_api_v3":
    problems.append(f"Devin transport kind is {devin_ref.get('transport_kind')!r}")
if problems:
    raise SystemExit(f"release qualification campaign is not a pass: {problems}")
print(json.dumps({
    "campaign": "complete",
    "required_providers": sorted(REQUIRED_PROVIDERS),
    "devin_transport": "hosted_bridge",
}))
PY
```

All three provider results must pass, and Devin's result must identify the
hosted transport before peer support is claimed. Keep the profile selector,
credentials, and result prose out of recorded evidence.

### 15. Restart the three Boards from the release, waiting on each stop

The port 5332 Board must serve the exact v1.4.0 release checkout because its
pre-release repository path is stale. Assert that checkout first, then stop each
Board and wait through the bounded Board inventory until its listener is gone
before starting the replacement, so no start races a dying listener on a fixed
port.

```bash
CODE_MOWER_RELEASE_CHECKOUT="REPLACE_WITH_EXACT_V140_CHECKOUT"
BOARD_5342_REPO="REUSE_PRIVATE_INVENTORIED_SLUG"
BOARD_5342_REPO_PATH="REUSE_PRIVATE_INVENTORIED_PATH"
BOARD_5344_REPO="REUSE_PRIVATE_INVENTORIED_SLUG"
BOARD_5344_REPO_PATH="REUSE_PRIVATE_INVENTORIED_PATH"
test "$(git -C "$CODE_MOWER_RELEASE_CHECKOUT" rev-parse HEAD)" = "$RELEASE_SHA"
test "$(git -C "$CODE_MOWER_RELEASE_CHECKOUT" rev-list -n 1 v1.4.0)" = "$RELEASE_SHA"

cat >"$RELEASE_ENV/board_wait.py" <<'PY'
"""Bounded waits on the Board inventory: gone after a stop, serving after a start.

Serving mode takes `PORT=REPO` arguments and requires each port to serve exactly
its expected repository as well as healthy 1.4.0 serving/installed versions, so a
Board that came back on the wrong repository cannot satisfy another port's gate.
Only ports are printed; the expected slugs stay in the private arguments.
"""

import json
import subprocess
import sys
import time

DEADLINE_SECONDS = 120
INTERVAL_SECONDS = 3


def inventory() -> list[dict]:
    completed = subprocess.run(
        ["code-mower", "board", "list", "--json"],
        check=True, capture_output=True, text=True,
    )
    payload = json.loads(completed.stdout)
    return [row for row in payload.get("boards") or [] if isinstance(row, dict)]


def row_for(port: int) -> dict | None:
    for row in inventory():
        if int(row.get("port") or 0) == port:
            return row
    return None


def serving(row: dict | None, expected_repo: str) -> bool:
    return bool(
        row is not None
        and expected_repo
        and row.get("repo") == expected_repo
        and row.get("health") == "ok"
        and row.get("serving_version") == "1.4.0"
        and row.get("installed_version") == "1.4.0"
    )


def main() -> None:
    mode = sys.argv[1]
    expected: dict[int, str] = {}
    for value in sys.argv[2:]:
        port, _, repo = value.partition("=")
        expected[int(port)] = repo
    if mode == "serving" and not all(expected.values()):
        raise SystemExit("serving mode requires PORT=REPO for every port")
    deadline = time.monotonic() + DEADLINE_SECONDS
    pending = list(expected)
    while pending and time.monotonic() < deadline:
        remaining = []
        for port in pending:
            row = row_for(port)
            if mode == "gone" and row is None:
                continue
            if mode == "serving" and serving(row, expected[port]):
                continue
            remaining.append(port)
        pending = remaining
        if pending:
            time.sleep(INTERVAL_SECONDS)
    if pending:
        raise SystemExit(f"ports still not {mode} within {DEADLINE_SECONDS}s: {pending}")
    print(json.dumps({"mode": mode, "ports": sorted(expected)}))


main()
PY

code-mower board list --json
for BOARD_PORT in 5332 5342 5344; do
  code-mower board stop --port "$BOARD_PORT" --yes --json
  "$RELEASE_PYTHON" "$RELEASE_ENV/board_wait.py" gone "$BOARD_PORT"
done

nohup code-mower board serve --repo codemower-ai/code-mower \
  --repo-path "$CODE_MOWER_RELEASE_CHECKOUT" --host 127.0.0.1 \
  --port 5332 --record-events >/tmp/code-mower-board-5332.log 2>&1 &
nohup code-mower board serve --repo "$BOARD_5342_REPO" \
  --repo-path "$BOARD_5342_REPO_PATH" --host 127.0.0.1 \
  --port 5342 --record-events >/tmp/code-mower-board-5342.log 2>&1 &
nohup code-mower board serve --repo "$BOARD_5344_REPO" \
  --repo-path "$BOARD_5344_REPO_PATH" --host 127.0.0.1 \
  --port 5344 --record-events >/tmp/code-mower-board-5344.log 2>&1 &
"$RELEASE_PYTHON" "$RELEASE_ENV/board_wait.py" serving \
  "5332=codemower-ai/code-mower" "5342=$BOARD_5342_REPO" "5344=$BOARD_5344_REPO"

BOARD_DOCTOR_DIR="$(mktemp -d /tmp/code-mower-v140-board-doctor.XXXXXX)"
code-mower board doctor --repo codemower-ai/code-mower \
  --repo-path "$CODE_MOWER_RELEASE_CHECKOUT" --json >"$BOARD_DOCTOR_DIR/5332.json"
code-mower board doctor --repo "$BOARD_5342_REPO" \
  --repo-path "$BOARD_5342_REPO_PATH" --json >"$BOARD_DOCTOR_DIR/5342.json"
code-mower board doctor --repo "$BOARD_5344_REPO" \
  --repo-path "$BOARD_5344_REPO_PATH" --json >"$BOARD_DOCTOR_DIR/5344.json"
BOARD_DOCTOR_DIR="$BOARD_DOCTOR_DIR" \
  BOARD_5332_REPO="codemower-ai/code-mower" \
  BOARD_5342_REPO="$BOARD_5342_REPO" BOARD_5344_REPO="$BOARD_5344_REPO" \
  "$RELEASE_PYTHON" - <<'PY'
import json
import os
from pathlib import Path

BOARD_DOCTOR_SCHEMA = "code_mower.boardDoctor.v1"
EXPECTED_CHECK_IDS = {
    "repo.path",
    "github.remote",
    "gate.health",
    "store.events",
    "owner.queue",
    "agent.adapters",
    "spend.timeline",
}
doctor_dir = Path(os.environ["BOARD_DOCTOR_DIR"])
problems = []
for port in ("5332", "5342", "5344"):
    expected_repo = os.environ[f"BOARD_{port}_REPO"]
    report = json.loads((doctor_dir / f"{port}.json").read_text(encoding="utf-8"))
    if report.get("schema") != BOARD_DOCTOR_SCHEMA:
        problems.append(f"board {port} doctor schema is {report.get('schema')!r}")
    if report.get("repo") != expected_repo:
        problems.append(f"board {port} doctor reports another repository")
    if report.get("status") != "pass":
        problems.append(f"board {port} doctor status is {report.get('status')!r}")
    checks = {
        str(row.get("id") or ""): str(row.get("status") or "")
        for row in report.get("checks") or []
        if isinstance(row, dict)
    }
    if not EXPECTED_CHECK_IDS or not EXPECTED_CHECK_IDS <= set(checks):
        problems.append(
            f"board {port} doctor is missing {sorted(EXPECTED_CHECK_IDS - set(checks))}"
        )
    failing = sorted(name for name, value in checks.items() if value != "pass")
    if failing:
        problems.append(f"board {port} doctor checks are not pass: {failing}")
if problems:
    raise SystemExit(f"restarted Board doctors are not all pass: {problems}")
print(json.dumps({"board_doctors_pass": ["5332", "5342", "5344"]}))
PY
```

`code-mower board doctor` exits zero for `warn`, so each report is parsed and
required to carry the `code_mower.boardDoctor.v1` schema, the expected
repository, a top-level `pass`, the full expected check inventory, and a `pass`
on every individual check; printing the JSON is not the gate.
Do not use raw process kills or Board reset, and never copy private repository
slugs or paths into public evidence.

### 16. Dry-run, inspect, then upload metadata-only cloud evidence

Both uploads are gates: the preview and the applied result are saved and
parsed. A preview must be metadata-only, carry zero reports, require explicit
application, and report the event identifiers and counts it would send; the
applied upload must be accepted by the service and carry exactly the previewed
identifiers and counts.

```bash
CLOUD_DIR="$(mktemp -d /tmp/code-mower-v140-cloud.XXXXXX)"
code-mower cloud doctor --install-id codex-code-mower --probe-service --json \
  >"$CLOUD_DIR/doctor.json"
code-mower release campaign upload --release-tag v1.4.0 \
  --install-id codex-code-mower --team-id jeff-internal --json \
  >"$CLOUD_DIR/campaign-preview.json"
code-mower release campaign upload --release-tag v1.4.0 \
  --install-id codex-code-mower --team-id jeff-internal --yes --json \
  >"$CLOUD_DIR/campaign-applied.json"
CLOUD_DIR="$CLOUD_DIR" "$RELEASE_PYTHON" - <<'PY'
import json
import os
from pathlib import Path

cloud_dir = Path(os.environ["CLOUD_DIR"])


def load(name: str) -> dict:
    return json.loads((cloud_dir / name).read_text(encoding="utf-8"))


CAMPAIGN_UPLOAD_SCHEMA = "code_mower.releaseCampaignUpload.v1"
REQUIRED_PROVIDERS = ["claude", "codex", "devin"]
EXPECTED_POSTURES = {name: "required" for name in REQUIRED_PROVIDERS}
EXPECTED_COUNTS = {
    "providers": 3,
    "complete": 3,
    "skipped": 0,
    "accepted": 3,
    "rejected": 0,
    "events": 3,
}
preview = load("campaign-preview.json")
applied = load("campaign-applied.json")
preview_upload = preview.get("upload") or {}
applied_upload = applied.get("upload") or {}
problems = []
for name, payload in (("preview", preview), ("applied", applied)):
    if payload.get("schema") != CAMPAIGN_UPLOAD_SCHEMA:
        problems.append(f"{name} schema is {payload.get('schema')!r}")
    if payload.get("mode") != "release-campaign-upload":
        problems.append(f"{name} mode is {payload.get('mode')!r}")
    if (
        payload.get("campaign_id") != "campaign-v1.4.0"
        or payload.get("release_tag") != "v1.4.0"
        or payload.get("package_identity") != "code-mower"
        or payload.get("qualification_context") != "cold_install"
    ):
        problems.append(f"{name} campaign identity is not the v1.4.0 campaign")
    if payload.get("provider_postures") != EXPECTED_POSTURES:
        problems.append(f"{name} provider postures are {payload.get('provider_postures')!r}")
    if payload.get("counts") != EXPECTED_COUNTS:
        problems.append(f"{name} counts are {payload.get('counts')!r}")
    if sorted(payload.get("accepted_providers") or []) != REQUIRED_PROVIDERS:
        problems.append(f"{name} accepted providers are {payload.get('accepted_providers')!r}")
    if payload.get("skipped_providers") or payload.get("rejected_providers"):
        problems.append(f"{name} skipped or rejected a provider")
    ids = [str(value) for value in payload.get("event_ids") or []]
    if len(ids) != 3 or len(set(ids)) != 3 or not all(ids):
        problems.append(f"{name} does not carry three unique event identifiers")
if preview_upload.get("event_types") != {"adoption_run": 3}:
    problems.append(f"preview event types are {preview_upload.get('event_types')!r}")
if preview_upload.get("would_upload") is not False:
    problems.append("preview payload would upload without --yes")
if preview.get("status") != "dry_run" or preview.get("would_upload") is not False:
    problems.append(f"preview status is {preview.get('status')!r}")
if preview.get("requires_yes") is not True or preview_upload.get("requires_yes") is not True:
    problems.append("preview does not require explicit application")
if preview.get("upload_mode") != "metadata_only":
    problems.append(f"preview upload mode is {preview.get('upload_mode')!r}")
if preview_upload.get("upload_mode") != "metadata_only":
    problems.append(f"preview payload mode is {preview_upload.get('upload_mode')!r}")
if preview_upload.get("report_count") != 0:
    problems.append(f"preview carries {preview_upload.get('report_count')!r} reports")
preview_events = [str(value) for value in preview.get("event_ids") or []]
if not preview_events or preview_upload.get("event_count") != len(preview_events):
    problems.append("preview event identifiers and count disagree")
if applied.get("status") != "uploaded" or applied.get("would_upload") is not True:
    problems.append(f"applied status is {applied.get('status')!r}")
if applied.get("requires_yes") is not False:
    problems.append("applied result is still a preview")
if applied.get("upload_mode") != "metadata_only":
    problems.append(f"applied upload mode is {applied.get('upload_mode')!r}")
if applied_upload.get("mode") != "cloud-upload":
    problems.append(f"applied upload mode is {applied_upload.get('mode')!r}")
if not 200 <= int(applied_upload.get("status") or 0) < 300:
    problems.append(f"applied upload was not accepted: {applied_upload.get('status')!r}")
if [str(value) for value in applied.get("event_ids") or []] != preview_events:
    problems.append("applied event identifiers differ from the preview")
if applied.get("counts") != preview.get("counts"):
    problems.append("applied counts differ from the preview")
if problems:
    raise SystemExit(f"campaign metadata upload is not a verified gate: {problems}")
print(json.dumps({
    "campaign_upload": "accepted",
    "event_count": len(preview_events),
    "reports": 0,
}))
PY

BOARD_SNAPSHOT_DIR="$(mktemp -d /tmp/code-mower-v140-board-snapshot.XXXXXX)"
code-mower cloud board-snapshot \
  --repo-path "$CODE_MOWER_RELEASE_CHECKOUT" \
  --repo-slug codemower-ai/code-mower \
  --output-dir "$BOARD_SNAPSHOT_DIR" \
  --install-id codex-code-mower --team-id jeff-internal --json \
  >"$CLOUD_DIR/board-snapshot.json"
code-mower cloud upload "$BOARD_SNAPSHOT_DIR" \
  --install-id codex-code-mower --dry-run --json \
  >"$CLOUD_DIR/board-preview.json"
code-mower cloud upload "$BOARD_SNAPSHOT_DIR" \
  --install-id codex-code-mower --yes --json \
  >"$CLOUD_DIR/board-applied.json"
CLOUD_DIR="$CLOUD_DIR" BOARD_SNAPSHOT_DIR="$BOARD_SNAPSHOT_DIR" "$RELEASE_PYTHON" - <<'PY'
import json
import os
from pathlib import Path

cloud_dir = Path(os.environ["CLOUD_DIR"])
bundle_dir = Path(os.environ["BOARD_SNAPSHOT_DIR"])


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


BUNDLE_SCHEMA = "code_mower.cloudBenchmarkBundle.v1"
EVENT_SCHEMA = "code_mower.benchmarkEvent.v1"
SNAPSHOT_SCHEMA = "code_mower.cloudBoardSnapshot.v1"
snapshot = load(cloud_dir / "board-snapshot.json")
preview = load(cloud_dir / "board-preview.json")
applied = load(cloud_dir / "board-applied.json")
manifest = load(bundle_dir / "code-mower-cloud-bundle.json")
export = snapshot.get("export") or {}
events = [row for row in manifest.get("events") or [] if isinstance(row, dict)]
event_types = sorted({str(row.get("event_type") or "") for row in events})
problems = []
if snapshot.get("mode") != "cloud-board-snapshot" or snapshot.get("status") != "dry_run":
    problems.append(
        f"board snapshot is {snapshot.get('mode')!r}/{snapshot.get('status')!r}"
    )
if snapshot.get("repo_slug") != "codemower-ai/code-mower":
    problems.append("board snapshot is not bound to the release repository")
if snapshot.get("event_count") != 1:
    problems.append(f"board snapshot carries {snapshot.get('event_count')!r} events")
if export.get("event_types") != {"board_snapshot": 1} or export.get("included_reports"):
    problems.append(f"board export carries {export.get('event_types')!r}")
if manifest.get("schema") != BUNDLE_SCHEMA:
    problems.append(f"board bundle schema is {manifest.get('schema')!r}")
if event_types != ["board_snapshot"] or len(events) != 1:
    problems.append(f"board bundle carries {event_types} events")
if manifest.get("included_reports"):
    problems.append("board bundle carries report content")
event = events[0] if events else {}
dimensions = event.get("dimensions")
dimensions = dimensions if isinstance(dimensions, dict) else {}
if event.get("schema") != EVENT_SCHEMA or not str(event.get("event_id") or ""):
    problems.append(f"board event schema/id is {event.get('schema')!r}")
if dimensions.get("snapshot_schema") != SNAPSHOT_SCHEMA:
    problems.append(f"board event snapshot schema is {dimensions.get('snapshot_schema')!r}")
if preview.get("mode") != "cloud-upload-dry-run" or preview.get("would_upload") is not False:
    problems.append(f"board preview mode is {preview.get('mode')!r}")
if preview.get("requires_yes") is not True:
    problems.append("board preview does not require explicit application")
if preview.get("upload_mode") != "metadata_only":
    problems.append(f"board preview upload mode is {preview.get('upload_mode')!r}")
if preview.get("report_count") != 0:
    problems.append(f"board preview carries {preview.get('report_count')!r} reports")
if preview.get("event_count") != len(events):
    problems.append("board preview event count differs from the bundle")
if applied.get("mode") != "cloud-upload":
    problems.append(f"board applied mode is {applied.get('mode')!r}")
if not 200 <= int(applied.get("status") or 0) < 300:
    problems.append(f"board upload was not accepted: {applied.get('status')!r}")
if problems:
    raise SystemExit(f"board snapshot upload is not a verified gate: {problems}")
print(json.dumps({
    "board_upload": "accepted",
    "event_types": event_types,
    "reports": 0,
}))
PY
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
