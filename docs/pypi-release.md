# PyPI Release Runbook

<!-- code-mower:release-facts:start -->
Code Mower users install from PyPI. For the current release, build the immutable
merge-SHA candidate first, qualify those retained bytes, then tag and publish the
unchanged SHA. The release workflow retrieves and verifies the candidate without
rebuilding. Follow the [v1.6.0 runbook](v160-release-runbook.md) and
[qualification contract](v160-qualification.md); observed evidence belongs on the release
issue and GitHub Release.

```bash
CODE_MOWER_PYTHON="$(command -v python3.12)"
pipx install --python "$CODE_MOWER_PYTHON" code-mower==1.6.0
```
<!-- code-mower:release-facts:end -->

<a id="v140-post-merge-release-runbook"></a>

The [historical v1.4.0 post-merge release runbook](v140-release-runbook.md#v140-post-merge-release-runbook)
is preserved unchanged.

## Immutable Release Text Gate (v1.5.0 onward)

Finalize the versioned public text **before creating the immutable tag**, in
the release preparation PR. The v1.4.1 tag permanently described itself as a
source candidate with publication pending #915 even after publication finished.
Editing `main` cannot repair that tagged README or the README embedded in its
package. Never rewrite a published tag to correct the wording.

1. Choose the exact release tag, such as `v1.6.0`. Set both the project version
   in `pyproject.toml` and `src/code_mower/__init__.py` to `1.6.0`.
2. Add exactly one matching `## 1.6.0` (or `## v1.6.0`) CHANGELOG heading as
   the first versioned entry; an `Unreleased` section may precede it. Describe
   what the release contains. A neutral heading such as `## 1.6.0 — release`
   works before publication and remains true afterward.
3. Set the README's opening source identity statement to
   `This source defines Code Mower v1.6.0, with package spec code-mower==1.6.0.`
   Markdown backticks and line wrapping are supported. Keep this statement
   before the first `##` heading and keep its tag and install spec exact.
   Follow it with the durable instruction to confirm the release tag on GitHub
   Releases and the package version on the selected index before using an index
   install command; source identity and publication state remain separate facts.
   Remove temporary promises from the introduction and the selected changelog
   entry: no `source candidate`, `publication pending`, or assertion that the
   release depends on an issue closing. Use the release issue and mutable
   GitHub Release evidence to track publication progress; do not claim a
   completed upload before it happens or bake a temporary upload status into
   the immutable package description.
4. Run the shared check against the intended tag while it is still a proposed
   name (the tag does not need to exist):

   ```bash
   .venv/bin/python src/code_mower/release_identity.py --tag v1.6.0
   .venv/bin/python -m code_mower.migration release-readiness --json
   ```

5. Obtain independent review on the exact final preparation PR head, green CI,
   and the authoritative Code Mower gate before merge. For v1.6.0, require
   #1063/#1109 and #1104/#1107 on that head, plus an exact accepted-contract
   health response and complete hosted acceptance evidence from the production
   CodeMower.com #978 deployment. Hosted PR #542 at
   `bcddaa25c633f2dcf8fa2077d6ecb8004c1d8f88` is an implementation prerequisite,
   not deployment evidence. Bind the actual merge SHA and
   build the candidate once. Complete #1105's bounded private Slack telemetry
   canary, local-versus-hosted reconciliation, 24-hour soak, two independent
   installation passes, and exact-candidate audits before the owner release
   decision, tag, or publication. Re-run identity on that exact checkout;
   publish the retained pair with the same SHA and candidate workflow run ID.

For v1.6.0 and later releases, let the final candidate soak for at least 24 hours
after its last source or packaged-document change and complete at least two
independent cold-install or upgrade passes during that window. A candidate
change restarts the clock. An emergency patch may shorten the soak only when
the release issue records the reason, risk, independent evidence, and rollback
plan before the owner publication decision.

Ordinary release-readiness CI invokes the same checker using the source
version's intended tag, so contradictions are reviewable before tagging. The
release workflow checks out the exact dispatched tag or published release tag
with full tag history, resolves lightweight and annotated tags to their commit,
and proves that checked-out HEAD equals that commit before checking the public
text. It exports the validated 40-character SHA to candidate retrieval,
which checks out that SHA and verifies the retained pair; event `github.sha`
is not used as source identity. Both TestPyPI and PyPI consume only that pair.
Manual dispatch additionally requires the resolved tag commit to equal the
supplied `expected_sha`; a branch dispatch or mismatched tag ref fails closed.

TestPyPI qualification may call its artifact a candidate in instruction
sections. Historical CHANGELOG entries and `Unreleased` are outside the active
entry's wording gate. Canonical prerelease tags `vX.Y.Z-alpha.N`,
`vX.Y.Z-beta.N`, and `vX.Y.Z-rc.N` bind to package versions `X.Y.ZaN`,
`X.Y.ZbN`, and `X.Y.ZrcN`; their README statement uses the existing beta or
release-candidate baseline and may describe a candidate. A final `vX.Y.Z` tag
always requires final-state text, including TestPyPI rehearsals and GitHub
releases marked prerelease. Neither the index nor that flag bypasses the gate.

The executed v1.4.2 commands below remain a historical record. Do not mechanically
substitute v1.6.0: its candidate-before-tag procedure is in the current runbook.

## Current Status

- GitHub Release workflow retrieves and verifies the qualified candidate on
  every published release; it does not rebuild the distributions.
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
RELEASE_VERSION="${RELEASE_VERSION:-1.6.0}"
RELEASE_TAG="v$RELEASE_VERSION"
gh release view "$RELEASE_TAG" \
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

## v1.4.2 Post-Merge Release Runbook

Complete the [candidate and installed-package evidence matrix](v142-qualification.md),
including #999/#1000/#1001/#1002/#1003 and the installed lineage replay, alongside these gates. Run
these steps in this order after the release pull request merges. Every
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
set -euo pipefail
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
set -euo pipefail
RELEASE_CHECKOUT="$(mktemp -d /tmp/code-mower-v142-release-src.XXXXXX)/code-mower"
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

Every pip-backed command in this runbook uses the same package-source
isolation: the outer environment drops `PIP_INDEX_URL`,
`PIP_EXTRA_INDEX_URL`, `PIP_FIND_LINKS`, and `PIP_NO_INDEX`, reads no pip
configuration file, names its index explicitly, and bypasses caches. Direct
pip commands add `--isolated` so no ambient environment or configuration can
reintroduce another package source.

```bash
set -euo pipefail
RELEASE_ENV="$(mktemp -d /tmp/code-mower-v142-release-env.XXXXXX)"
python3.12 -m venv "$RELEASE_ENV/venv"
RELEASE_PYTHON="$RELEASE_ENV/venv/bin/python"
env -u PIP_INDEX_URL -u PIP_EXTRA_INDEX_URL -u PIP_FIND_LINKS -u PIP_NO_INDEX \
  PIP_CONFIG_FILE=/dev/null "$RELEASE_PYTHON" -m pip --isolated install \
  --no-cache-dir --index-url https://pypi.org/simple/ "$RELEASE_CHECKOUT"
RELEASE_CLI="$RELEASE_ENV/venv/bin/code-mower"
test "$("$RELEASE_CLI" --version)" = "code-mower 1.4.2"
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

### 3. Create and verify the annotated `v1.4.2` tag on that exact commit

```bash
set -euo pipefail
git tag -a v1.4.2 "$RELEASE_SHA" -m "Code Mower v1.4.2"
git push origin refs/tags/v1.4.2
test "$(git rev-list -n 1 v1.4.2)" = "$RELEASE_SHA"
test "$(git ls-remote origin 'refs/tags/v1.4.2^{}' | awk '{print $1}')" = "$RELEASE_SHA"
```

### 4. Install the workflow-run assertion helper

Every workflow run below is asserted with this helper: workflow identity,
triggering event, exact head SHA, `success` conclusion, a successful
`release-identity` gate job, successful `build-distributions` and
`verify-distributions` jobs, and the exact posture of both publish jobs. A run
whose only reported jobs are skipped publish jobs fails.
A job that is expected to skip must be reported skipped or be absent from the
run; a job that is expected to publish must report `success`.

```bash
set -euo pipefail
cat >"$RELEASE_ENV/assert_release_run.py" <<'PY'
"""Assert one release workflow run's identity, head, conclusion, and job posture."""

import json
import os
import subprocess
import sys

EXPECTED_WORKFLOW = "Code Mower Release"
# release-identity is the workflow's fail-fast gate: it proves the dispatched
# ref is the v1.4.2 tag and github.sha equals the expected_sha input, and both
# build and publish jobs depend on it.
BUILD_JOBS = ("release-identity", "build-distributions", "verify-distributions")
SKIPPED = {"skipped", "absent"}


def run_view(repo: str, run_id: str) -> dict:
    completed = subprocess.run(
        [
            "gh", "run", "view", run_id, "--repo", repo, "--json",
            "databaseId,workflowName,headSha,headBranch,event,status,conclusion,url,jobs",
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
    repo, run_id, event, head_sha, head_branch, testpypi, pypi = sys.argv[1:8]
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
    # A commit can carry several tags, so the commit alone does not prove the
    # run was dispatched for the v1.4.2 tag.
    if run.get("headBranch") != head_branch:
        problems.append(f"head branch is {run.get('headBranch')}, not {head_branch}")
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
        "head_branch": head_branch,
        "release_identity": job_posture(run, "release-identity"),
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

Both publish jobs must skip on this run. Every dispatch below passes
`-f expected_sha="$RELEASE_SHA"`, and the workflow's first job,
`release-identity`, fails fast unless the dispatch ref is `refs/tags/v1.4.2` and
`github.sha` equals that exact 40-character commit. `build-distributions`,
`publish-testpypi`, and `publish-pypi` all depend on that job, so a missing,
malformed, or mismatched expected SHA cannot build or publish anything.

```bash
set -euo pipefail
gh workflow run release.yml --repo "$REPO" --ref v1.4.2 \
  -f publish_testpypi=false -f publish_pypi=false \
  -f expected_sha="$RELEASE_SHA"
NO_PUBLISH_RUN_ID="REPLACE_WITH_EXACT_RUN_ID"
gh run watch "$NO_PUBLISH_RUN_ID" --repo "$REPO" --exit-status
"$RELEASE_PYTHON" "$RELEASE_ENV/assert_release_run.py" "$REPO" \
  "$NO_PUBLISH_RUN_ID" workflow_dispatch "$RELEASE_SHA" v1.4.2 skipped skipped
```

### 6. Publish TestPyPI only, then rehearse the exact candidate from TestPyPI

TestPyPI must publish while production PyPI skips. pip does not prefer
`--index-url` over `--extra-index-url`, so the candidate artifacts are fetched
from TestPyPI alone, with no cache, no dependency resolution, and no ambient pip
configuration; their exact filenames and digests are bound before the rehearsal,
which then installs the local wheel. Dependencies resolve separately from
canonical PyPI.

The runtime candidate download is wheel-only (`--only-binary :all:`), so it
fails clearly when the universal wheel is missing and never builds from source.
The sdist is verified separately: even with `--no-deps`, pip prepares PEP 517
metadata for a source archive and would try to fetch the declared
`setuptools>=77` build requirement from the only configured index, which
TestPyPI does not carry. The build requirement is therefore installed first into
the disposable release environment from canonical PyPI, and the sdist download
then runs with that environment, `--no-binary :all:`, `--no-build-isolation`,
and `--check-build-dependencies`, so TestPyPI still supplies only `code-mower`
and a missing or wrong build backend fails closed instead of widening the
candidate source. Production PyPI is never added as an extra index.

```bash
set -euo pipefail
gh workflow run release.yml --repo "$REPO" --ref v1.4.2 \
  -f publish_testpypi=true -f publish_pypi=false \
  -f expected_sha="$RELEASE_SHA"
TESTPYPI_RUN_ID="REPLACE_WITH_EXACT_RUN_ID"
gh run watch "$TESTPYPI_RUN_ID" --repo "$REPO" --exit-status
"$RELEASE_PYTHON" "$RELEASE_ENV/assert_release_run.py" "$REPO" \
  "$TESTPYPI_RUN_ID" workflow_dispatch "$RELEASE_SHA" v1.4.2 success skipped

TESTPYPI_DIST_DIR="$(mktemp -d /tmp/code-mower-v142-testpypi-dist.XXXXXX)"
env -u PIP_INDEX_URL -u PIP_EXTRA_INDEX_URL -u PIP_FIND_LINKS -u PIP_NO_INDEX \
  PIP_CONFIG_FILE=/dev/null python3.12 -m pip --isolated download code-mower==1.4.2 \
  --no-cache-dir --no-deps --only-binary :all: \
  --index-url https://test.pypi.org/simple/ --dest "$TESTPYPI_DIST_DIR"
env -u PIP_INDEX_URL -u PIP_EXTRA_INDEX_URL -u PIP_FIND_LINKS -u PIP_NO_INDEX \
  PIP_CONFIG_FILE=/dev/null "$RELEASE_PYTHON" -m pip --isolated install \
  --no-cache-dir --index-url https://pypi.org/simple/ "setuptools>=77"
env -u PIP_INDEX_URL -u PIP_EXTRA_INDEX_URL -u PIP_FIND_LINKS -u PIP_NO_INDEX \
  PIP_CONFIG_FILE=/dev/null "$RELEASE_PYTHON" -m pip --isolated download code-mower==1.4.2 \
  --no-cache-dir --no-deps --no-binary :all: \
  --no-build-isolation --check-build-dependencies \
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
wheels = [name for name in digests if name == "code_mower-1.4.2-py3-none-any.whl"]
sdists = [name for name in digests if name == "code_mower-1.4.2.tar.gz"]
if len(digests) != 2 or len(wheels) != 1 or len(sdists) != 1:
    raise SystemExit(f"TestPyPI candidate artifact set is unexpected: {sorted(digests)}")
print(json.dumps({"source": "testpypi", "artifacts": digests}, sort_keys=True))
PY
TESTPYPI_WHEEL="$TESTPYPI_DIST_DIR/code_mower-1.4.2-py3-none-any.whl"
test -f "$TESTPYPI_WHEEL"
TESTPYPI_WORK_DIR="$(mktemp -d /tmp/code-mower-v142-testpypi-rehearsal.XXXXXX)"
env -u PIP_INDEX_URL -u PIP_EXTRA_INDEX_URL -u PIP_FIND_LINKS -u PIP_NO_INDEX \
  PIP_CONFIG_FILE=/dev/null \
  "$RELEASE_CLI" migration package-install-rehearsal \
  --package-spec "$TESTPYPI_WHEEL" \
  --python "$(command -v python3.12)" \
  --work-dir "$TESTPYPI_WORK_DIR" \
  --pip-index-url https://pypi.org/simple/ \
  --pip-no-cache --upgrade-pip --json
```

The rehearsal installs the exact TestPyPI wheel, so production PyPI cannot satisfy
this step; only its dependencies come from canonical PyPI. The outer
environment cleanup covers the rehearsal's own `--upgrade-pip` subprocess as
well as its install, so neither can inherit an ambient index, find-links
directory, offline flag, or pip configuration file.

### 7. Publish production PyPI only, then rehearse the published package

Production PyPI must publish while TestPyPI skips, and the rehearsal must reach
canonical `https://pypi.org/simple/` explicitly with no cache, so no ambient
`pip.conf`, `PIP_INDEX_URL`, or mirror can satisfy a production-labelled gate.

```bash
set -euo pipefail
gh workflow run release.yml --repo "$REPO" --ref v1.4.2 \
  -f publish_testpypi=false -f publish_pypi=true \
  -f expected_sha="$RELEASE_SHA"
PYPI_RUN_ID="REPLACE_WITH_EXACT_RUN_ID"
gh run watch "$PYPI_RUN_ID" --repo "$REPO" --exit-status
"$RELEASE_PYTHON" "$RELEASE_ENV/assert_release_run.py" "$REPO" \
  "$PYPI_RUN_ID" workflow_dispatch "$RELEASE_SHA" v1.4.2 skipped success

PYPI_WORK_DIR="$(mktemp -d /tmp/code-mower-v142-pypi-rehearsal.XXXXXX)"
env -u PIP_INDEX_URL -u PIP_EXTRA_INDEX_URL -u PIP_FIND_LINKS -u PIP_NO_INDEX \
  PIP_CONFIG_FILE=/dev/null "$RELEASE_CLI" migration package-install-rehearsal \
  --package-spec code-mower==1.4.2 \
  --python "$(command -v python3.12)" \
  --work-dir "$PYPI_WORK_DIR" \
  --pip-index-url https://pypi.org/simple/ \
  --allow-package-index --pip-no-cache --upgrade-pip --json
```

### 8. Download the exact workflow artifact

```bash
set -euo pipefail
PROD_DIST_DIR="$(mktemp -d /tmp/code-mower-v142-prod-dist.XXXXXX)"
gh run download "$PYPI_RUN_ID" --repo "$REPO" \
  --name code-mower-dist --dir "$PROD_DIST_DIR"
sha256sum "$PROD_DIST_DIR"/*
```

### 9. Compare SHA-256 digests with the files downloaded from canonical PyPI

```bash
set -euo pipefail
PYPI_DOWNLOAD_DIR="$(mktemp -d /tmp/code-mower-v142-pypi-download.XXXXXX)"
env -u PIP_INDEX_URL -u PIP_EXTRA_INDEX_URL -u PIP_FIND_LINKS -u PIP_NO_INDEX \
  PIP_CONFIG_FILE=/dev/null python3.12 -m pip --isolated download code-mower==1.4.2 \
  --no-cache-dir --no-deps --no-binary :all: \
  --index-url https://pypi.org/simple/ --dest "$PYPI_DOWNLOAD_DIR"
env -u PIP_INDEX_URL -u PIP_EXTRA_INDEX_URL -u PIP_FIND_LINKS -u PIP_NO_INDEX \
  PIP_CONFIG_FILE=/dev/null python3.12 -m pip --isolated download code-mower==1.4.2 \
  --no-cache-dir --no-deps --only-binary :all: \
  --index-url https://pypi.org/simple/ --dest "$PYPI_DOWNLOAD_DIR"
PYPI_VERIFIED_MAP="$RELEASE_ENV/pypi-verified-artifacts.json"
test ! -e "$PYPI_VERIFIED_MAP"
PROD_DIST_DIR="$PROD_DIST_DIR" PYPI_DOWNLOAD_DIR="$PYPI_DOWNLOAD_DIR" \
  PYPI_VERIFIED_MAP="$PYPI_VERIFIED_MAP" "$RELEASE_PYTHON" - <<'PY'
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
expected = {"code_mower-1.4.2-py3-none-any.whl", "code_mower-1.4.2.tar.gz"}
if set(workflow) != expected or set(published) != expected:
    raise SystemExit("workflow and PyPI artifact sets differ")
if any(workflow[name] != published[name] for name in workflow):
    raise SystemExit("workflow and PyPI SHA-256 values differ")
# This map is the immutable release artifact evidence: every later local and
# GitHub Release asset check compares against it, never against a freshly
# recomputed map of the mutable download directory.
Path(os.environ["PYPI_VERIFIED_MAP"]).write_text(
    json.dumps(workflow, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
print(json.dumps({"artifact_count": len(workflow), "sha256_match": True}))
PY
test -s "$PYPI_VERIFIED_MAP"
```

Only continue when the artifact set and every digest match. A mismatch is a
release blocker: do not attach unverified files. The saved
`pypi-verified-artifacts.json` map is written once, at the moment the workflow
artifacts are proven identical to canonical PyPI, and is treated as immutable
release evidence from then on.

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
set -euo pipefail
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

An existing `v1.4.2` release is never clobbered: inspect it first and stop
unless its tag and its exact asset set and digests already match the saved
PyPI-verified map. Install the asset assertion first. It compares the local
files and the Release's own downloaded assets against
`$PYPI_VERIFIED_MAP` -- not against a freshly recomputed `PROD_DIST_DIR` map --
and re-resolves the remote peeled `v1.4.2` tag to `$RELEASE_SHA` on every
invocation, including the `pre-create` invocation that runs immediately before
`gh release create`:

```bash
set -euo pipefail
cat >"$RELEASE_ENV/assert_release_assets.py" <<'PY'
"""Assert the GitHub Release tag and its downloaded assets match PROD_DIST_DIR."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

EXPECTED = {"code_mower-1.4.2-py3-none-any.whl", "code_mower-1.4.2.tar.gz"}
EXPECTED_TITLE = "Code Mower v1.4.2"
RELEASE_NOTES_RELPATH = "docs/v142-release-notes.md"


def digests(directory: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.iterdir())
        if path.is_file()
    }


def gh_json(args: list[str]) -> dict:
    return json.loads(subprocess.run(
        ["gh", *args], check=True, capture_output=True, text=True,
    ).stdout)


def remote_peeled_tag_sha(repo: str) -> str:
    """Resolve the remote v1.4.2 tag to the commit it currently peels to."""
    ref = gh_json(["api", f"repos/{repo}/git/ref/tags/v1.4.2"])
    target = ref.get("object") if isinstance(ref.get("object"), dict) else {}
    sha = str(target.get("sha") or "")
    if target.get("type") == "tag" and sha:
        annotated = gh_json(["api", f"repos/{repo}/git/tags/{sha}"])
        peeled = annotated.get("object") if isinstance(annotated.get("object"), dict) else {}
        sha = str(peeled.get("sha") or "")
    return sha


def main() -> None:
    mode = sys.argv[1]
    repo = os.environ["REPO"]
    release_sha = os.environ["RELEASE_SHA"]
    # The immutable map saved when the workflow artifacts were proven identical
    # to canonical PyPI. A file replaced in PROD_DIST_DIR afterwards cannot
    # become release evidence.
    verified = json.loads(
        Path(os.environ["PYPI_VERIFIED_MAP"]).read_text(encoding="utf-8")
    )
    local = digests(Path(os.environ["PROD_DIST_DIR"]))
    problems = []
    if not isinstance(verified, dict) or set(verified) != EXPECTED:
        raise SystemExit(f"{mode}: the PyPI-verified artifact map is not the release set")
    if any(not isinstance(value, str) or len(value) != 64 for value in verified.values()):
        raise SystemExit(f"{mode}: the PyPI-verified artifact map is malformed")
    if local != verified:
        problems.append("local artifacts differ from the PyPI-verified map")
    # Re-resolved on every invocation, so a tag moved after the earlier local
    # check cannot reach release creation or acceptance.
    if remote_peeled_tag_sha(repo) != release_sha:
        problems.append("remote v1.4.2 tag does not peel to the exact release commit")
    tag_target = subprocess.run(
        ["git", "rev-list", "-n", "1", "v1.4.2"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    if tag_target != release_sha:
        problems.append("release tag does not target the exact release commit")
    # The notes are read from the clean checkout of the exact release commit on
    # every invocation, so neither an ambient working copy nor notes edited
    # after the earlier checks can describe the published release.
    checkout = Path(os.environ["RELEASE_CHECKOUT"])
    checkout_head = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    if checkout_head != release_sha:
        problems.append("release checkout is not the exact release commit")
    checkout_status = subprocess.run(
        ["git", "-C", str(checkout), "status", "--porcelain", "--untracked-files=all"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    if checkout_status:
        problems.append("release checkout has uncommitted or untracked changes")
    notes_path = checkout / RELEASE_NOTES_RELPATH
    expected_notes = (
        notes_path.read_text(encoding="utf-8").strip() if notes_path.is_file() else ""
    )
    if not expected_notes:
        problems.append("release notes in the exact checkout are empty")
    if mode == "pre-create":
        if problems:
            raise SystemExit(f"{mode} release assets are not acceptable: {problems}")
        print(json.dumps({
            "mode": mode,
            "assets": sorted(verified),
            "sha256_match": True,
            "remote_tag_match": True,
            "checkout_match": True,
            "notes_present": True,
        }, sort_keys=True))
        return
    view = gh_json([
        "release", "view", "v1.4.2", "--repo", repo, "--json",
        "tagName,isDraft,isPrerelease,assets,body,name",
    ])
    if view.get("tagName") != "v1.4.2":
        problems.append("release tag is not v1.4.2")
    if str(view.get("body") or "").replace("\r\n", "\n").strip() != expected_notes:
        problems.append("release body does not match the exact checkout release notes")
    if view.get("name") != EXPECTED_TITLE:
        problems.append("release title is not the expected v1.4.2 title")
    if view.get("isDraft") or view.get("isPrerelease"):
        problems.append("release is a draft or prerelease")
    asset_names = {asset["name"] for asset in view.get("assets") or []}
    if asset_names != set(verified):
        problems.append(f"release asset set differs: {sorted(asset_names)}")
    with tempfile.TemporaryDirectory() as scratch:
        target = Path(scratch)
        subprocess.run(
            ["gh", "release", "download", "v1.4.2", "--repo", repo,
             "--dir", str(target)],
            check=True, capture_output=True, text=True,
        )
        downloaded = digests(target)
    if downloaded != verified:
        problems.append("release asset SHA-256 values differ from the PyPI-verified map")
    if problems:
        raise SystemExit(f"{mode} release assets are not acceptable: {problems}")
    print(json.dumps({
        "mode": mode,
        "assets": sorted(verified),
        "sha256_match": True,
        "remote_tag_match": True,
        "notes_match": True,
        "title_match": True,
    }, sort_keys=True))


main()
PY
```

Assets are downloaded into a private empty scratch directory, so nothing is
overwritten anywhere, and a `v1.4.2` release whose assets differ stops the
runbook for inspection.

```bash
set -euo pipefail
test "$(git -C "$RELEASE_CHECKOUT" rev-parse HEAD)" = "$RELEASE_SHA"
test -z "$(git -C "$RELEASE_CHECKOUT" status --porcelain --untracked-files=all)"
test -s "$RELEASE_CHECKOUT/docs/v142-release-notes.md"
test -s "$PYPI_VERIFIED_MAP"
if gh release view v1.4.2 --repo "$REPO" >/dev/null 2>&1; then
  REPO="$REPO" PROD_DIST_DIR="$PROD_DIST_DIR" RELEASE_SHA="$RELEASE_SHA" \
    PYPI_VERIFIED_MAP="$PYPI_VERIFIED_MAP" RELEASE_CHECKOUT="$RELEASE_CHECKOUT" \
    "$RELEASE_PYTHON" "$RELEASE_ENV/assert_release_assets.py" existing
else
  REPO="$REPO" PROD_DIST_DIR="$PROD_DIST_DIR" RELEASE_SHA="$RELEASE_SHA" \
    PYPI_VERIFIED_MAP="$PYPI_VERIFIED_MAP" RELEASE_CHECKOUT="$RELEASE_CHECKOUT" \
    "$RELEASE_PYTHON" "$RELEASE_ENV/assert_release_assets.py" pre-create
  gh release create v1.4.2 \
    "$PROD_DIST_DIR/code_mower-1.4.2-py3-none-any.whl" \
    "$PROD_DIST_DIR/code_mower-1.4.2.tar.gz" --repo "$REPO" \
    --verify-tag --title "Code Mower v1.4.2" \
    --notes-file "$RELEASE_CHECKOUT/docs/v142-release-notes.md" \
    --latest --fail-on-no-commits
fi
REPO="$REPO" PROD_DIST_DIR="$PROD_DIST_DIR" RELEASE_SHA="$RELEASE_SHA" \
  PYPI_VERIFIED_MAP="$PYPI_VERIFIED_MAP" RELEASE_CHECKOUT="$RELEASE_CHECKOUT" \
  "$RELEASE_PYTHON" "$RELEASE_ENV/assert_release_assets.py" created
gh release view v1.4.2 --repo "$REPO" \
  --json tagName,targetCommitish,isDraft,isPrerelease,publishedAt,url,assets
```

### 12. Assert the `release`-event run published nothing

```bash
set -euo pipefail
RELEASE_EVENT_RUN_ID="REPLACE_WITH_EXACT_RELEASE_EVENT_RUN_ID"
gh run watch "$RELEASE_EVENT_RUN_ID" --repo "$REPO" --exit-status
"$RELEASE_PYTHON" "$RELEASE_ENV/assert_release_run.py" "$REPO" \
  "$RELEASE_EVENT_RUN_ID" release "$RELEASE_SHA" v1.4.2 skipped skipped
```

### 13. Install the published package and inspect adoption readiness

The authorized v1.4.2 participant scope is Claude + Codex. Optional Graphify is
read-only context, never a campaign execution provider. Ordinary adoption does
not require campaign authentication. Quiet ordinary adoption does not prove
optional campaign readiness: inspect `doctor code-mower.yml --profile recommended --campaign` separately before step 14. If isolated campaign authentication is
unavailable, record the blocked qualification; do not use ambient credentials
or mark an unrun provider passed. Supported non-keyring Codex campaign auth
remains #983 before v1.5.0. No paid hosted Devin creation is authorized here;
a later hosted campaign needs a separate explicit owner allowance.

```bash
set -euo pipefail
CODE_MOWER_PYTHON="$(command -v python3.12)"
test -n "$CODE_MOWER_PYTHON"
env -u PIP_INDEX_URL -u PIP_EXTRA_INDEX_URL -u PIP_FIND_LINKS -u PIP_NO_INDEX \
  PIP_CONFIG_FILE=/dev/null pipx install --force --backend pip \
  --python "$CODE_MOWER_PYTHON" --index-url https://pypi.org/simple/ \
  --pip-args='--isolated --no-cache-dir' 'code-mower[coworker]==1.4.2'
test "$(code-mower --version)" = "code-mower 1.4.2"

code-mower doctor --adoption --repo codemower-ai/code-mower --json
```

### 14. Run the required Claude + Codex campaign

After explicit campaign readiness and the supervisor's release authorization,
require one complete passing result per selected provider, bound to this exact
release and cold-install context. Save and assert watch/status output; duplicate
or missing rows fail. This procedure grants no Devin orchestration authority.

```bash
set -euo pipefail
CAMPAIGN_DIR="$(mktemp -d /tmp/code-mower-v142-campaign.XXXXXX)"
code-mower release campaign create \
  --release-tag v1.4.2 \
  --package-spec code-mower==1.4.2 \
  --providers claude,codex \
  --required-providers claude,codex \
  --qualification-context cold_install \
  --package-source pypi \
  --repo-slug codemower-ai/code-mower \
  --issue 952 --release-pr "$RELEASE_PR" \
  --apply --json >"$CAMPAIGN_DIR/create.json"
code-mower release campaign watch --release-tag v1.4.2 \
  --interval 10 --timeout 3600 --json >"$CAMPAIGN_DIR/watch.json"
code-mower release campaign status --release-tag v1.4.2 \
  --json >"$CAMPAIGN_DIR/status.json"
CAMPAIGN_DIR="$CAMPAIGN_DIR" "$RELEASE_PYTHON" - <<'PY'
import json
import os
from pathlib import Path

campaign_dir = Path(os.environ["CAMPAIGN_DIR"])
REQUIRED_PROVIDERS = {"claude", "codex"}
PASSING_OUTCOMES = {"pass", "pass_with_warnings"}
CAMPAIGN_SCHEMA = "code_mower.releaseCampaign.v1"
WATCH_SCHEMA = "code_mower.releaseCampaignWatch.v1"
ADOPTION_RESULT_SCHEMA = "code_mower.adoptionResult.v1"
CAMPAIGN_ID = "campaign-v1.4.2"
RELEASE_TAG = "v1.4.2"
PACKAGE_IDENTITY = "code-mower"
VERSION = "1.4.2"


def load(name: str) -> dict:
    return json.loads((campaign_dir / name).read_text(encoding="utf-8"))


def exact_provider_rows(rows: object, label: str) -> tuple[dict | None, list[str]]:
    """Index provider rows only after the raw list holds each provider exactly once.

    Building a provider-keyed dictionary first would silently discard a
    duplicate row: a failing Devin lane followed by a passing Devin lane would
    read as one passing lane. The raw list is validated instead, so duplicate,
    unknown, missing, or malformed rows fail before any indexing happens.
    """

    if not isinstance(rows, list):
        return None, [f"{label} provider list is {type(rows).__name__}, not a list"]
    problems: list[str] = []
    indexed: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            problems.append(f"{label} provider row is malformed")
            continue
        name = row.get("provider")
        if not isinstance(name, str) or name not in REQUIRED_PROVIDERS:
            problems.append(f"{label} provider row identity is {name!r}")
            continue
        if name in indexed:
            problems.append(f"{label} provider {name!r} appears more than once")
            continue
        indexed[name] = row
    missing = sorted(REQUIRED_PROVIDERS - set(indexed))
    if missing:
        problems.append(f"{label} provider rows are missing {missing}")
    return (None if problems else indexed), problems


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
watch_lanes, watch_row_problems = exact_provider_rows(watch.get("providers"), "watch")
problems.extend(watch_row_problems)
if watch_lanes is not None and set(watch_lanes) != REQUIRED_PROVIDERS:
    problems.append(f"watch provider set is {sorted(watch_lanes)}")
for name in sorted(watch_lanes or {}):
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
lanes, lane_row_problems = exact_provider_rows(status.get("providers"), "campaign")
problems.extend(lane_row_problems)
lanes = lanes or {}
if set(lanes) != REQUIRED_PROVIDERS:
    problems.append(f"campaign provider set is {sorted(lanes)}")
required = {name for name, row in lanes.items() if row.get("posture") == "required"}
if required != REQUIRED_PROVIDERS:
    problems.append(f"required provider set is {sorted(required)}")
for name in sorted(lanes):
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
    if result.get("provider") != name:
        problems.append(f"{name} lane result provider is {result.get('provider')!r}")
    if result.get("qualification_context") != "cold_install":
        problems.append(
            f"{name} lane result context is {result.get('qualification_context')!r}"
        )
    outcome = result.get("outcome")
    if outcome not in PASSING_OUTCOMES:
        problems.append(f"{name} lane result outcome is {outcome!r}")
if problems:
    raise SystemExit(f"release qualification campaign is not a pass: {problems}")
print(json.dumps({
    "campaign": "complete",
    "required_providers": sorted(REQUIRED_PROVIDERS),
}))
PY
```

Both provider results must pass. Keep credentials and result prose in private
local evidence. Record known caps, unknown settlement, stored receipts and
observed aggregate freshness separately; zero observed usage is not billing.

### 15. Restart the reconciled Board inventory from the release, waiting on each stop

The port 5332 Board must serve the exact v1.4.2 release checkout because its
pre-release repository path is stale. Assert that checkout first, then stop each
Board and wait through the bounded Board inventory until its listener is gone
before starting the replacement, so no start races a dying listener on a fixed
port.

```bash
set -euo pipefail
BOARD_5333_REPO="REUSE_PRIVATE_INVENTORIED_SLUG"
BOARD_5333_REPO_PATH="REUSE_PRIVATE_INVENTORIED_PATH"
test "$(git -C "$RELEASE_CHECKOUT" rev-parse HEAD)" = "$RELEASE_SHA"
# The fresh clone predates the tag, so the published tag is fetched into it
# before its target is asserted against the release commit.
git -C "$RELEASE_CHECKOUT" fetch --no-tags origin "+refs/tags/v1.4.2:refs/tags/v1.4.2"
test "$(git -C "$RELEASE_CHECKOUT" rev-list -n 1 v1.4.2)" = "$RELEASE_SHA"

cat >"$RELEASE_ENV/assert_board_repo_paths.py" <<'PY'
"""Require every Board repository path to be the checkout of its paired slug.

A path paired with another repository's slug would serve that repository's
history under the wrong name, so each path's git origin is normalized and
compared. Only the port count is printed; slugs and paths stay in arguments.
"""

import json
import re
import subprocess
import sys

ORIGIN_PATTERN = re.compile(r"^(?:git@[^:]+:|(?:https?|ssh|git)://[^/]+/)(?P<slug>.+?)(?:\.git)?$")


def origin_slug(path: str) -> str:
    completed = subprocess.run(
        ["git", "-C", path, "config", "--get", "remote.origin.url"],
        check=True, capture_output=True, text=True,
    )
    match = ORIGIN_PATTERN.match(completed.stdout.strip())
    if match is None:
        raise SystemExit("a Board repository path has no recognizable git origin")
    return match.group("slug").strip().lower()


def main() -> None:
    pairs = []
    for value in sys.argv[1:]:
        slug, separator, path = value.partition("=")
        if not separator or not slug.strip() or not path.strip():
            raise SystemExit("each argument must be SLUG=PATH")
        pairs.append((slug.strip().lower(), path.strip()))
    if not pairs:
        raise SystemExit("no Board repository pairs supplied")
    for slug, path in pairs:
        if origin_slug(path) != slug:
            raise SystemExit("a Board repository path does not match its paired slug")
    print(json.dumps({"board_repo_paths": "slug_bound", "pair_count": len(pairs)}))


main()
PY
"$RELEASE_PYTHON" "$RELEASE_ENV/assert_board_repo_paths.py" \
  "codemower-ai/code-mower=$RELEASE_CHECKOUT" \
  "$BOARD_5333_REPO=$BOARD_5333_REPO_PATH"

cat >"$RELEASE_ENV/board_wait.py" <<'PY'
"""Bounded waits on the Board inventory: gone after a stop, serving after a start.

Serving mode takes `PORT=REPO` arguments and requires each port to serve exactly
its expected repository as well as healthy 1.4.2 serving/installed versions, so a
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
        and row.get("serving_version") == "1.4.2"
        and row.get("installed_version") == "1.4.2"
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

test "$(git -C "$RELEASE_CHECKOUT" rev-parse HEAD)" = "$RELEASE_SHA"
test -z "$(git -C "$RELEASE_CHECKOUT" status --porcelain --untracked-files=all)"

# Each port is restarted by its own observed posture, not a blind stop/serve.
# #961's managed services refuse `board stop` (status=managed_service, exit
# nonzero); replacing a managed service with a transient `nohup ... serve`
# would downgrade its supervision, so a managed port is restarted in place
# with `board service restart --replace` instead. `board service status`
# itself exits nonzero for every status except `ok`, so its raw exit code is
# ignored here and the captured JSON is classified explicitly instead.
cat >"$RELEASE_ENV/board_service_mode.py" <<'PY'
"""Classify one port's `board service status` payload, failing closed.

Only an exact `not_installed` with zero matching rows is transient. A
managed service stays managed through `delayed_health_failed` -- restart is
what heals a stale binding, not a reason to treat it as unmanaged. Anything
else (a wrong or missing schema, `unsupported_platform`, more than one
matching row, a non-object row, a row for another port, or malformed JSON)
fails the runbook instead of guessing a posture or raising AttributeError on
an unexpected shape.
"""

import json
import sys

BOARD_SERVICE_STATUS_SCHEMA = "code_mower.boardServiceStatus.v1"


def main() -> None:
    port = int(sys.argv[1])
    try:
        payload = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        raise SystemExit(f"port {port}: board service status did not return JSON")
    if not isinstance(payload, dict):
        raise SystemExit(f"port {port}: board service status payload is not an object")
    if payload.get("schema") != BOARD_SERVICE_STATUS_SCHEMA:
        raise SystemExit(f"port {port}: board service status schema is {payload.get('schema')!r}")
    status = payload.get("status")
    services = payload.get("services")
    if status == "not_installed" and services == []:
        print("transient")
        return
    if (
        status in ("ok", "delayed_health_failed")
        and isinstance(services, list)
        and len(services) == 1
        and isinstance(services[0], dict)
        and services[0].get("port") == port
    ):
        print("managed")
        return
    raise SystemExit(f"port {port}: board service status is not a classifiable posture: {payload!r}")


main()
PY

BOARD_5332_STATUS_JSON="$(code-mower board service status --port 5332 --json 2>/dev/null || true)"
BOARD_5333_STATUS_JSON="$(code-mower board service status --port 5333 --json 2>/dev/null || true)"
BOARD_5332_MODE="$(printf '%s' "$BOARD_5332_STATUS_JSON" | "$RELEASE_PYTHON" "$RELEASE_ENV/board_service_mode.py" 5332)"
BOARD_5333_MODE="$(printf '%s' "$BOARD_5333_STATUS_JSON" | "$RELEASE_PYTHON" "$RELEASE_ENV/board_service_mode.py" 5333)"

if [ "$BOARD_5332_MODE" = "managed" ]; then
  code-mower board service restart --repo codemower-ai/code-mower \
    --repo-path "$RELEASE_CHECKOUT" --port 5332 --replace --json
else
  # A stop selector needs both --repo and --port: a port-only selector could
  # stop a different repository's listener if the port was reused after
  # reconciliation moved between checking status and stopping it.
  code-mower board stop --repo codemower-ai/code-mower --port 5332 --yes --json
  "$RELEASE_PYTHON" "$RELEASE_ENV/board_wait.py" gone 5332
  nohup code-mower board serve --repo codemower-ai/code-mower \
    --repo-path "$RELEASE_CHECKOUT" --host 127.0.0.1 \
    --port 5332 --record-events >/tmp/code-mower-board-5332.log 2>&1 &
fi

if [ "$BOARD_5333_MODE" = "managed" ]; then
  code-mower board service restart --repo "$BOARD_5333_REPO" \
    --repo-path "$BOARD_5333_REPO_PATH" --port 5333 --replace --json
else
  code-mower board stop --repo "$BOARD_5333_REPO" --port 5333 --yes --json
  "$RELEASE_PYTHON" "$RELEASE_ENV/board_wait.py" gone 5333
  nohup code-mower board serve --repo "$BOARD_5333_REPO" \
    --repo-path "$BOARD_5333_REPO_PATH" --host 127.0.0.1 \
    --port 5333 --record-events >/tmp/code-mower-board-5333.log 2>&1 &
fi

"$RELEASE_PYTHON" "$RELEASE_ENV/board_wait.py" serving \
  "5332=codemower-ai/code-mower" "5333=$BOARD_5333_REPO"

# The restart must not silently change a port's supervision posture: a
# managed service stays managed, and a transient process is never left
# installed as a managed service it was not before.
BOARD_5332_STATUS_JSON_AFTER="$(code-mower board service status --port 5332 --json 2>/dev/null || true)"
BOARD_5333_STATUS_JSON_AFTER="$(code-mower board service status --port 5333 --json 2>/dev/null || true)"
BOARD_5332_MODE_AFTER="$(printf '%s' "$BOARD_5332_STATUS_JSON_AFTER" | "$RELEASE_PYTHON" "$RELEASE_ENV/board_service_mode.py" 5332)"
BOARD_5333_MODE_AFTER="$(printf '%s' "$BOARD_5333_STATUS_JSON_AFTER" | "$RELEASE_PYTHON" "$RELEASE_ENV/board_service_mode.py" 5333)"
test "$BOARD_5332_MODE_AFTER" = "$BOARD_5332_MODE"
test "$BOARD_5333_MODE_AFTER" = "$BOARD_5333_MODE"

BOARD_DOCTOR_DIR="$(mktemp -d /tmp/code-mower-v142-board-doctor.XXXXXX)"
code-mower board doctor --repo codemower-ai/code-mower \
  --repo-path "$RELEASE_CHECKOUT" --json >"$BOARD_DOCTOR_DIR/5332.json"
code-mower board doctor --repo "$BOARD_5333_REPO" \
  --repo-path "$BOARD_5333_REPO_PATH" --json >"$BOARD_DOCTOR_DIR/5333.json"
BOARD_DOCTOR_DIR="$BOARD_DOCTOR_DIR" \
  BOARD_5332_REPO="codemower-ai/code-mower" \
  BOARD_5333_REPO="$BOARD_5333_REPO" \
  "$RELEASE_PYTHON" - <<'PY'
import json
import os
from pathlib import Path

BOARD_DOCTOR_SCHEMA = "code_mower.boardDoctor.v1"
REQUIRED_PASS_CHECK_IDS = (
    "repo.path",
    "github.remote",
    "gate.health",
    "store.events",
    "agent.adapters",
    "spend.timeline",
)
OWNER_QUEUE_CHECK_ID = "owner.queue"
EXPECTED_CHECK_IDS = {*REQUIRED_PASS_CHECK_IDS, OWNER_QUEUE_CHECK_ID}
OWNER_QUEUE_STATUSES = {"pass", "warn"}


def exact_doctor_checks(rows: object, port: str) -> tuple[dict | None, list[str]]:
    """Index doctor checks only after the raw list holds each check exactly once.

    Indexing first would keep the last row for a repeated check id, so a failing
    check followed by a passing duplicate would read as healthy. Duplicate,
    unknown, missing, or malformed check rows fail before indexing.
    """

    if not isinstance(rows, list):
        return None, [f"board {port} doctor check list is not a list"]
    problems: list[str] = []
    indexed: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            problems.append(f"board {port} doctor check row is malformed")
            continue
        check_id = row.get("id")
        status = row.get("status")
        if not isinstance(check_id, str) or check_id not in EXPECTED_CHECK_IDS:
            problems.append(f"board {port} doctor check id is {check_id!r}")
            continue
        if not isinstance(status, str):
            problems.append(f"board {port} doctor check {check_id!r} status is malformed")
            continue
        if check_id in indexed:
            problems.append(f"board {port} doctor check {check_id!r} appears more than once")
            continue
        indexed[check_id] = status
    missing = sorted(EXPECTED_CHECK_IDS - set(indexed))
    if missing:
        problems.append(f"board {port} doctor is missing {missing}")
    return (None if problems else indexed), problems


doctor_dir = Path(os.environ["BOARD_DOCTOR_DIR"])
problems = []
for port in ("5332", "5333"):
    expected_repo = os.environ[f"BOARD_{port}_REPO"]
    report = json.loads((doctor_dir / f"{port}.json").read_text(encoding="utf-8"))
    if report.get("schema") != BOARD_DOCTOR_SCHEMA:
        problems.append(f"board {port} doctor schema is {report.get('schema')!r}")
    if report.get("repo") != expected_repo:
        problems.append(f"board {port} doctor reports another repository")
    checks, check_problems = exact_doctor_checks(report.get("checks"), port)
    problems.extend(check_problems)
    if checks is None:
        continue
    failing = sorted(name for name in REQUIRED_PASS_CHECK_IDS if checks[name] != "pass")
    if failing:
        problems.append(f"board {port} doctor checks are not pass: {failing}")
    owner_queue = checks[OWNER_QUEUE_CHECK_ID]
    if owner_queue not in OWNER_QUEUE_STATUSES:
        problems.append(f"board {port} owner queue check is {owner_queue!r}")
        continue
    if report.get("status") != owner_queue:
        problems.append(
            f"board {port} doctor status is {report.get('status')!r},"
            f" expected {owner_queue!r}"
        )
if problems:
    raise SystemExit(f"restarted Board doctors are not release-ready: {problems}")
print(json.dumps({"board_doctors_release_ready": ["5332", "5333"]}))
PY
```

`code-mower board doctor` exits zero for `warn`, so each report is parsed and
required to carry the `code_mower.boardDoctor.v1` schema, the expected
repository, and exactly one row for each expected check id; printing the JSON is
not the gate. `repo.path`, `github.remote`, `gate.health`, `store.events`,
`agent.adapters`, and `spend.timeline` must pass. Only `owner.queue` may be
`warn`, because a nonempty owner queue reports ordinary queued drafts, rebases,
stale pull requests, and owner work rather than a degraded Board; the top-level
status must then be exactly that `warn`, and exactly `pass` when the queue is
empty. Every other warning, failure, unknown status, or unexpected top-level
verdict blocks the release.
Do not use raw process kills or Board reset, and never copy private repository
slugs or paths into public evidence.

### 16. Dry-run, inspect, then upload metadata-only cloud evidence

Both uploads run in two phases, so "dry-run, inspect, then upload" is true of
the commands and not only of the prose: the preview is saved and fully
validated, its accepted verdict is required to exist, and only then may the
`--yes` mutation run. A preview must be metadata-only, carry zero reports,
require explicit application, target the probed service, and report the event
identifiers and counts it would send; the applied upload is validated
separately and must be accepted by the service, target the same endpoint, and
carry exactly the previewed identifiers and counts.

The cloud identifiers are account-specific, so they are supplied privately and
only ever passed as variables; their values are never printed or recorded. An
empty value or a forgotten `REPLACE_WITH_...` placeholder fails before the
probe, and the selected install profile is resolved privately so a conflicting
explicit team identity fails before any preview or application rather than
uploading under the wrong account. The service itself is probed and asserted
before either upload against a newly created empty bundle directory, so the
expected check inventory is exactly
`endpoint`, `service`, `token`, and the `bundle` warning for the bundle that is
exported later; the probe report is saved privately because it names the
endpoint and describes the token resolution. Every later producer re-resolves
the install profile on its own, so each preview and applied payload is required
to report the endpoint read back from that private probe report; the value is
compared, never printed.

```bash
set -euo pipefail
CLOUD_DIR="$(mktemp -d /tmp/code-mower-v142-cloud.XXXXXX)"
# Supply both privately, for example by sourcing a protected token env file.
# Never echo them and never write them into release evidence.
: "${CODE_MOWER_CLOUD_TEAM_ID:?private cloud team id is required}"
: "${CODE_MOWER_INSTALL_ID:?private cloud install id is required}"
test -n "$CODE_MOWER_CLOUD_TEAM_ID"
test -n "$CODE_MOWER_INSTALL_ID"
case "$CODE_MOWER_CLOUD_TEAM_ID" in REPLACE_WITH_*) exit 1 ;; esac
case "$CODE_MOWER_INSTALL_ID" in REPLACE_WITH_*) exit 1 ;; esac
env -u CODE_MOWER_CLOUD_TOKEN -u CODE_MOWER_CLOUD_ENDPOINT \
  CODE_MOWER_CLOUD_TEAM_ID="$CODE_MOWER_CLOUD_TEAM_ID" \
  CODE_MOWER_INSTALL_ID="$CODE_MOWER_INSTALL_ID" "$RELEASE_PYTHON" - \
  >"$CLOUD_DIR/identity.json" <<'PY'
import json
import os

from code_mower.cloud_client import DEFAULT_TOKEN_ENV, resolve_cloud_token

# The stored profile is resolved privately and only compared: the supplied
# identifiers must be exactly the ones the selected install profile holds, so
# an upload cannot silently target another install or team. The ambient cloud
# token and endpoint are excluded from this process, so the resolution has to
# come from the selected stored install profile rather than reflecting the very
# values being asserted. Only the verdict is printed.
install_id = os.environ["CODE_MOWER_INSTALL_ID"].strip()
team_id = os.environ["CODE_MOWER_CLOUD_TEAM_ID"].strip()
resolution = resolve_cloud_token(token_env=DEFAULT_TOKEN_ENV, install_id=install_id)
problems = []
if not install_id or install_id.startswith("REPLACE_WITH_"):
    problems.append("the private install identifier is empty or a placeholder")
if not team_id or team_id.startswith("REPLACE_WITH_"):
    problems.append("the private team identifier is empty or a placeholder")
if not resolution.has_token:
    problems.append("the selected install profile has no usable cloud token")
if resolution.source != "install_id":
    problems.append("the cloud token was not resolved from the selected install profile")
if not resolution.install_id or resolution.install_id.strip() != install_id:
    problems.append("the selected install profile stores a different install identity")
if not resolution.team_id or resolution.team_id.strip() != team_id:
    problems.append("the selected install profile stores a different team identity")
if problems:
    raise SystemExit(f"cloud identity is not bound to the selected profile: {problems}")
print(json.dumps({"cloud_identity": "bound", "source": resolution.source}))
PY
grep -q '"cloud_identity": "bound"' "$CLOUD_DIR/identity.json"
grep -q '"source": "install_id"' "$CLOUD_DIR/identity.json"
CLOUD_DOCTOR_BUNDLE_DIR="$(mktemp -d /tmp/code-mower-v142-cloud-doctor.XXXXXX)"
env -u CODE_MOWER_CLOUD_TOKEN -u CODE_MOWER_CLOUD_ENDPOINT \
  code-mower cloud doctor "$CLOUD_DOCTOR_BUNDLE_DIR" \
  --install-id "$CODE_MOWER_INSTALL_ID" \
  --probe-service --json >"$CLOUD_DIR/doctor.json"
CLOUD_DIR="$CLOUD_DIR" "$RELEASE_PYTHON" - <<'PY'
import json
import os
from pathlib import Path

# The empty bundle directory makes the inventory deterministic: endpoint,
# service, and token must pass, and the only tolerated condition is the bundle
# warning for the release bundle that is exported later in this step.
PASSING_CLOUD_CHECKS = ("endpoint", "service", "token")
EXPECTED_CLOUD_CHECKS = frozenset(PASSING_CLOUD_CHECKS) | {"bundle"}
report = json.loads(
    (Path(os.environ["CLOUD_DIR"]) / "doctor.json").read_text(encoding="utf-8")
)
if not isinstance(report, dict):
    raise SystemExit("cloud doctor output is not a report")
problems = []
if report.get("mode") != "cloud-doctor":
    problems.append(f"cloud doctor mode is {report.get('mode')!r}")
if report.get("status") != "pass":
    problems.append(f"cloud doctor status is {report.get('status')!r}")
if report.get("failures") != 0:
    problems.append(f"cloud doctor reports {report.get('failures')!r} failures")
rows = report.get("checks")
if not isinstance(rows, list):
    raise SystemExit("cloud doctor check list is not a list")
# Health is derived from the raw rows and only then compared with the aggregate
# fields, so falsified status/failures values cannot hide a degraded check.
statuses = {}
raw_failures = 0
for row in rows:
    if not isinstance(row, dict):
        problems.append("cloud doctor check row is malformed")
        continue
    name = row.get("name")
    status = row.get("status")
    if not isinstance(name, str) or not isinstance(status, str):
        problems.append("cloud doctor check identity is malformed")
        continue
    if status == "fail":
        raw_failures += 1
    if name in statuses:
        problems.append(f"cloud doctor check {name!r} appears more than once")
        continue
    statuses[name] = status
unexpected = sorted(set(statuses) - EXPECTED_CLOUD_CHECKS)
missing = sorted(EXPECTED_CLOUD_CHECKS - set(statuses))
if unexpected:
    problems.append(f"cloud doctor reported unexpected checks {unexpected}")
if missing:
    problems.append(f"cloud doctor is missing checks {missing}")
for name in PASSING_CLOUD_CHECKS:
    if statuses.get(name) != "pass":
        problems.append(f"cloud doctor {name} check is {statuses.get(name)!r}")
if statuses.get("bundle") != "warn":
    problems.append(f"cloud doctor bundle check is {statuses.get('bundle')!r}")
if raw_failures or raw_failures != report.get("failures"):
    problems.append(f"cloud doctor rows report {raw_failures} failures")
if problems:
    raise SystemExit(f"cloud service readiness is not a pass: {problems}")
print(json.dumps({"cloud_doctor": "pass", "checks": sorted(PASSING_CLOUD_CHECKS)}))
PY

env -u CODE_MOWER_CLOUD_TOKEN -u CODE_MOWER_CLOUD_ENDPOINT \
  code-mower release campaign upload --release-tag v1.4.2 \
  --install-id "$CODE_MOWER_INSTALL_ID" --team-id "$CODE_MOWER_CLOUD_TEAM_ID" --json \
  >"$CLOUD_DIR/campaign-preview.json"
CLOUD_DIR="$CLOUD_DIR" "$RELEASE_PYTHON" - \
  >"$CLOUD_DIR/campaign-preflight.json" <<'PY'
import json
import os
from pathlib import Path

cloud_dir = Path(os.environ["CLOUD_DIR"])
CAMPAIGN_UPLOAD_SCHEMA = "code_mower.releaseCampaignUpload.v1"
REQUIRED_PROVIDERS = ["claude", "codex"]
EXPECTED_POSTURES = {name: "required" for name in REQUIRED_PROVIDERS}
EXPECTED_COUNTS = {
    "providers": 2,
    "complete": 2,
    "skipped": 0,
    "accepted": 2,
    "rejected": 0,
    "events": 2,
}


def identity_problems(name: str, payload: dict) -> list:
    problems = []
    if payload.get("schema") != CAMPAIGN_UPLOAD_SCHEMA:
        problems.append(f"{name} schema is {payload.get('schema')!r}")
    if payload.get("mode") != "release-campaign-upload":
        problems.append(f"{name} mode is {payload.get('mode')!r}")
    if (
        payload.get("campaign_id") != "campaign-v1.4.2"
        or payload.get("release_tag") != "v1.4.2"
        or payload.get("package_identity") != "code-mower"
        or payload.get("qualification_context") != "cold_install"
    ):
        problems.append(f"{name} campaign identity is not the v1.4.2 campaign")
    if payload.get("provider_postures") != EXPECTED_POSTURES:
        problems.append(f"{name} provider postures are {payload.get('provider_postures')!r}")
    if payload.get("counts") != EXPECTED_COUNTS:
        problems.append(f"{name} counts are {payload.get('counts')!r}")
    if sorted(payload.get("accepted_providers") or []) != REQUIRED_PROVIDERS:
        problems.append(f"{name} accepted providers are {payload.get('accepted_providers')!r}")
    if payload.get("skipped_providers") or payload.get("rejected_providers"):
        problems.append(f"{name} skipped or rejected a provider")
    ids = [str(value) for value in payload.get("event_ids") or []]
    if len(ids) != 2 or len(set(ids)) != 2 or not all(ids):
        problems.append(f"{name} does not carry two unique event identifiers")
    return problems


# Nothing has been sent yet: the preview alone decides whether the applied
# upload may run at all, so it is validated before the --yes command exists.
preview = json.loads((cloud_dir / "campaign-preview.json").read_text(encoding="utf-8"))
preview_upload = preview.get("upload") or {}
# The endpoint the probe actually reached is read back from the private doctor
# report, so an upload that re-resolved a different install profile cannot be
# accepted. It is compared, never printed.
probed_endpoint = str(
    json.loads((cloud_dir / "doctor.json").read_text(encoding="utf-8")).get("endpoint")
    or ""
)
problems = identity_problems("preview", preview)
if not probed_endpoint:
    problems.append("the probed cloud endpoint was not recorded")
if preview_upload.get("endpoint") != probed_endpoint:
    problems.append("campaign upload preview does not target the probed service")
if preview_upload.get("event_types") != {"adoption_run": 2}:
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
if problems:
    raise SystemExit(f"campaign upload preview is not an acceptable payload: {problems}")
print(json.dumps({
    "campaign_preview": "accepted",
    "event_count": len(preview_events),
    "reports": 0,
}))
PY
grep -q '"campaign_preview": "accepted"' "$CLOUD_DIR/campaign-preflight.json"

env -u CODE_MOWER_CLOUD_TOKEN -u CODE_MOWER_CLOUD_ENDPOINT \
  code-mower release campaign upload --release-tag v1.4.2 \
  --install-id "$CODE_MOWER_INSTALL_ID" --team-id "$CODE_MOWER_CLOUD_TEAM_ID" --yes --json \
  >"$CLOUD_DIR/campaign-applied.json"
CLOUD_DIR="$CLOUD_DIR" "$RELEASE_PYTHON" - <<'PY'
import json
import os
from pathlib import Path

cloud_dir = Path(os.environ["CLOUD_DIR"])


def load(name: str) -> dict:
    return json.loads((cloud_dir / name).read_text(encoding="utf-8"))


CAMPAIGN_UPLOAD_SCHEMA = "code_mower.releaseCampaignUpload.v1"
REQUIRED_PROVIDERS = ["claude", "codex"]
EXPECTED_POSTURES = {name: "required" for name in REQUIRED_PROVIDERS}
EXPECTED_COUNTS = {
    "providers": 2,
    "complete": 2,
    "skipped": 0,
    "accepted": 2,
    "rejected": 0,
    "events": 2,
}


def identity_problems(name: str, payload: dict) -> list:
    problems = []
    if payload.get("schema") != CAMPAIGN_UPLOAD_SCHEMA:
        problems.append(f"{name} schema is {payload.get('schema')!r}")
    if payload.get("mode") != "release-campaign-upload":
        problems.append(f"{name} mode is {payload.get('mode')!r}")
    if (
        payload.get("campaign_id") != "campaign-v1.4.2"
        or payload.get("release_tag") != "v1.4.2"
        or payload.get("package_identity") != "code-mower"
        or payload.get("qualification_context") != "cold_install"
    ):
        problems.append(f"{name} campaign identity is not the v1.4.2 campaign")
    if payload.get("provider_postures") != EXPECTED_POSTURES:
        problems.append(f"{name} provider postures are {payload.get('provider_postures')!r}")
    if payload.get("counts") != EXPECTED_COUNTS:
        problems.append(f"{name} counts are {payload.get('counts')!r}")
    if sorted(payload.get("accepted_providers") or []) != REQUIRED_PROVIDERS:
        problems.append(f"{name} accepted providers are {payload.get('accepted_providers')!r}")
    if payload.get("skipped_providers") or payload.get("rejected_providers"):
        problems.append(f"{name} skipped or rejected a provider")
    ids = [str(value) for value in payload.get("event_ids") or []]
    if len(ids) != 2 or len(set(ids)) != 2 or not all(ids):
        problems.append(f"{name} does not carry two unique event identifiers")
    return problems


preview = load("campaign-preview.json")
applied = load("campaign-applied.json")
applied_upload = applied.get("upload") or {}
probed_endpoint = str(load("doctor.json").get("endpoint") or "")
preview_events = [str(value) for value in preview.get("event_ids") or []]
problems = identity_problems("applied", applied)
if not probed_endpoint or applied_upload.get("endpoint") != probed_endpoint:
    problems.append("campaign applied upload does not target the probed service")
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

BOARD_SNAPSHOT_DIR="$(mktemp -d /tmp/code-mower-v142-board-snapshot.XXXXXX)"
# The checkout is re-bound to the released commit immediately before the
# snapshot runs, and the producer is also told to require that exact commit and
# a clean worktree while it collects, so the emitted evidence names the source
# it actually read instead of relying on an earlier assertion.
test "$(git -C "$RELEASE_CHECKOUT" rev-parse HEAD)" = "$RELEASE_SHA"
test -z "$(git -C "$RELEASE_CHECKOUT" status --porcelain --untracked-files=all)"
env -u CODE_MOWER_CLOUD_TOKEN -u CODE_MOWER_CLOUD_ENDPOINT \
  code-mower cloud board-snapshot \
  --repo-path "$RELEASE_CHECKOUT" \
  --repo-slug codemower-ai/code-mower \
  --output-dir "$BOARD_SNAPSHOT_DIR" \
  --require-head-sha "$RELEASE_SHA" --require-clean \
  --install-id "$CODE_MOWER_INSTALL_ID" --team-id "$CODE_MOWER_CLOUD_TEAM_ID" --json \
  >"$CLOUD_DIR/board-snapshot.json"
sha256sum "$BOARD_SNAPSHOT_DIR/code-mower-cloud-bundle.json" \
  >"$CLOUD_DIR/board-bundle-before-preview.sha256"
env -u CODE_MOWER_CLOUD_TOKEN -u CODE_MOWER_CLOUD_ENDPOINT \
  code-mower cloud upload "$BOARD_SNAPSHOT_DIR" \
  --install-id "$CODE_MOWER_INSTALL_ID" --dry-run --json \
  >"$CLOUD_DIR/board-preview.json"
sha256sum "$BOARD_SNAPSHOT_DIR/code-mower-cloud-bundle.json" \
  >"$CLOUD_DIR/board-bundle-after-preview.sha256"
CLOUD_DIR="$CLOUD_DIR" BOARD_SNAPSHOT_DIR="$BOARD_SNAPSHOT_DIR" \
  RELEASE_SHA="$RELEASE_SHA" "$RELEASE_PYTHON" - \
  >"$CLOUD_DIR/board-preflight.json" <<'PY'
import hashlib
import json
import os
from pathlib import Path

cloud_dir = Path(os.environ["CLOUD_DIR"])
bundle_dir = Path(os.environ["BOARD_SNAPSHOT_DIR"])


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def digest_of(path: Path) -> str:
    return path.read_text(encoding="utf-8").split()[0]


BUNDLE_SCHEMA = "code_mower.cloudBenchmarkBundle.v1"
# The nested snapshot doctor runs without --probe-service, so its inventory is
# exactly these rows with that one expected skip.
SNAPSHOT_DOCTOR_PASSING = ("endpoint", "token", "bundle", "model-provenance")
SNAPSHOT_DOCTOR_CHECKS = frozenset(SNAPSHOT_DOCTOR_PASSING) | {"service"}
EVENT_SCHEMA = "code_mower.benchmarkEvent.v1"
SNAPSHOT_SCHEMA = "code_mower.cloudBoardSnapshot.v1"
UPLOAD_IDENTITY_SCHEMA = "code_mower.cloudUploadIdentity.v1"
release_sha = os.environ["RELEASE_SHA"].strip()
EXPECTED_REPO_SLUG = "codemower-ai/code-mower"
EXPECTED_EVENT_TYPES = {"board_snapshot": 1}
snapshot = load(cloud_dir / "board-snapshot.json")
preview = load(cloud_dir / "board-preview.json")
# The endpoint the probe actually reached is read back from the private doctor
# report, so a producer that re-resolved a different install profile cannot be
# accepted. It is compared, never printed.
probed_endpoint = str(load(cloud_dir / "doctor.json").get("endpoint") or "")
manifest_path = bundle_dir / "code-mower-cloud-bundle.json"
manifest = load(manifest_path)
export = snapshot.get("export") or {}
snapshot_preview = snapshot.get("upload") or {}
raw_events = manifest.get("events")
problems = []
# Malformed rows are reported instead of being filtered away, so a bundle that
# carries a valid event plus anything else cannot look like a single event.
if not isinstance(raw_events, list) or len(raw_events) != 1 or not isinstance(
    raw_events[0], dict
):
    problems.append("board bundle does not carry exactly one structured event")
    events = []
else:
    events = list(raw_events)
event_types = sorted({str(row.get("event_type") or "") for row in events})
event_type_counts = {}
for row in events:
    key = str(row.get("event_type") or "")
    event_type_counts[key] = event_type_counts.get(key, 0) + 1
if snapshot.get("mode") != "cloud-board-snapshot" or snapshot.get("status") != "dry_run":
    problems.append(
        f"board snapshot is {snapshot.get('mode')!r}/{snapshot.get('status')!r}"
    )
if snapshot.get("repo_slug") != EXPECTED_REPO_SLUG:
    problems.append("board snapshot is not bound to the release repository")
if snapshot.get("event_count") != 1:
    problems.append(f"board snapshot carries {snapshot.get('event_count')!r} events")
if export.get("event_types") != EXPECTED_EVENT_TYPES or export.get("included_reports"):
    problems.append(f"board export carries {export.get('event_types')!r}")
if manifest.get("schema") != BUNDLE_SCHEMA:
    problems.append(f"board bundle schema is {manifest.get('schema')!r}")
if manifest.get("repo_slug") != EXPECTED_REPO_SLUG:
    problems.append("board bundle is not bound to the release repository")
if event_types != ["board_snapshot"] or len(events) != 1:
    problems.append(f"board bundle carries {event_types} events")
if manifest.get("included_reports"):
    problems.append("board bundle carries report content")
event = events[0] if events else {}
dimensions = event.get("dimensions")
dimensions = dimensions if isinstance(dimensions, dict) else {}
if event.get("schema") != EVENT_SCHEMA or not str(event.get("event_id") or ""):
    problems.append(f"board event schema/id is {event.get('schema')!r}")
if event.get("repo_slug") != EXPECTED_REPO_SLUG:
    problems.append("board event is not bound to the release repository")
if dimensions.get("snapshot_schema") != SNAPSHOT_SCHEMA:
    problems.append(f"board event snapshot schema is {dimensions.get('snapshot_schema')!r}")
snapshot_doctor = snapshot.get("doctor")
if not isinstance(snapshot_doctor, dict):
    problems.append("board snapshot carries no doctor report")
    snapshot_doctor = {}
if snapshot_doctor.get("mode") != "cloud-doctor":
    problems.append(f"board snapshot doctor mode is {snapshot_doctor.get('mode')!r}")
if snapshot_doctor.get("status") != "pass":
    problems.append(f"board snapshot doctor status is {snapshot_doctor.get('status')!r}")
if snapshot_doctor.get("failures") != 0:
    problems.append(
        f"board snapshot doctor reports {snapshot_doctor.get('failures')!r} failures"
    )
if snapshot_doctor.get("endpoint") != probed_endpoint:
    problems.append("board snapshot doctor does not target the probed service")
doctor_rows = snapshot_doctor.get("checks")
if not isinstance(doctor_rows, list) or not doctor_rows:
    problems.append("board snapshot doctor check list is not a nonempty list")
    doctor_rows = []
doctor_statuses = {}
doctor_failures = 0
for row in doctor_rows:
    if not isinstance(row, dict):
        problems.append("board snapshot doctor check row is malformed")
        continue
    name = row.get("name")
    status = row.get("status")
    if not isinstance(name, str) or not isinstance(status, str):
        problems.append("board snapshot doctor check identity is malformed")
        continue
    if status == "fail":
        doctor_failures += 1
    if name in doctor_statuses:
        problems.append(f"board snapshot doctor check {name!r} appears more than once")
        continue
    doctor_statuses[name] = status
unexpected_doctor = sorted(set(doctor_statuses) - SNAPSHOT_DOCTOR_CHECKS)
missing_doctor = sorted(SNAPSHOT_DOCTOR_CHECKS - set(doctor_statuses))
if unexpected_doctor:
    problems.append(f"board snapshot doctor reported unexpected checks {unexpected_doctor}")
if missing_doctor:
    problems.append(f"board snapshot doctor is missing checks {missing_doctor}")
for name in SNAPSHOT_DOCTOR_PASSING:
    if doctor_statuses.get(name) != "pass":
        problems.append(
            f"board snapshot doctor {name} check is {doctor_statuses.get(name)!r}"
        )
if doctor_statuses.get("service") != "skip":
    problems.append(
        f"board snapshot doctor service check is {doctor_statuses.get('service')!r}"
    )
if doctor_failures or doctor_failures != snapshot_doctor.get("failures"):
    problems.append(f"board snapshot doctor rows report {doctor_failures} failures")
if preview.get("mode") != "cloud-upload-dry-run" or preview.get("would_upload") is not False:
    problems.append(f"board preview mode is {preview.get('mode')!r}")
if preview.get("requires_yes") is not True:
    problems.append("board preview does not require explicit application")
if preview.get("upload_mode") != "metadata_only":
    problems.append(f"board preview upload mode is {preview.get('upload_mode')!r}")
if preview.get("report_count") != 0:
    problems.append(f"board preview carries {preview.get('report_count')!r} reports")
if preview.get("event_count") != len(events) or preview.get("event_count") != 1:
    problems.append("board preview event count is not the single bundled event")
# Generic `cloud upload --dry-run` reports no event-type map, so the exact event
# types come from the manifest this preview describes, and that manifest is the
# one bound to the applied upload by digest.
if event_type_counts != EXPECTED_EVENT_TYPES:
    problems.append(f"board bundle event types are {event_type_counts}")
if not probed_endpoint:
    problems.append("the probed cloud endpoint was not recorded")
if snapshot_preview.get("endpoint") != probed_endpoint:
    problems.append("board snapshot preview does not target the probed service")
if preview.get("endpoint") != probed_endpoint:
    problems.append("board upload preview does not target the probed service")
# The manifest is hashed on both sides of the preview command, so a bundle that
# is replaced while the preview runs cannot become the previewed identity.
before_preview = digest_of(cloud_dir / "board-bundle-before-preview.sha256")
after_preview = digest_of(cloud_dir / "board-bundle-after-preview.sha256")
previewed_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
if before_preview != after_preview or previewed_digest != before_preview:
    problems.append("board bundle changed while the preview was generated")
# External hashes alone cannot see an A-B-A substitution inside a producer, so
# both producers also report the exact manifest bytes and events they acted on,
# and those reports must agree with each other and with the inspected event.
expected_identity = {
    "schema": UPLOAD_IDENTITY_SCHEMA,
    "manifest_sha256": previewed_digest,
    "event_count": 1,
    "event_ids": [str(event.get("event_id") or "")],
    "event_type_counts": dict(EXPECTED_EVENT_TYPES),
}
snapshot_identity = snapshot.get("manifest")
preview_identity = preview.get("manifest")
if snapshot_identity != expected_identity:
    problems.append("board snapshot does not report the inspected manifest identity")
if preview_identity != expected_identity:
    problems.append("board upload preview does not report the inspected manifest identity")
# The snapshot also names the checkout it read, so accepted Board evidence is
# owned by the producer rather than inferred from a separate assertion.
snapshot_git = snapshot.get("git")
snapshot_git = snapshot_git if isinstance(snapshot_git, dict) else {}
expected_git = {
    "available": True,
    "head_sha": release_sha,
    "clean": True,
    "dirty_entry_count": 0,
}
if snapshot_git != expected_git:
    problems.append("board snapshot was not collected from the exact clean release checkout")
if dimensions.get("source_git") != expected_git:
    problems.append("board event does not carry the release checkout provenance")
if problems:
    raise SystemExit(f"board snapshot preview is not an acceptable payload: {problems}")
print(json.dumps({
    "board_preview": "accepted",
    "event_types": event_types,
    "previewed_digest": previewed_digest,
    "previewed_identity": expected_identity,
    "reports": 0,
}))
PY
grep -q '"board_preview": "accepted"' "$CLOUD_DIR/board-preflight.json"

sha256sum "$BOARD_SNAPSHOT_DIR/code-mower-cloud-bundle.json" \
  >"$CLOUD_DIR/board-bundle-before-apply.sha256"
env -u CODE_MOWER_CLOUD_TOKEN -u CODE_MOWER_CLOUD_ENDPOINT \
  code-mower cloud upload "$BOARD_SNAPSHOT_DIR" \
  --install-id "$CODE_MOWER_INSTALL_ID" --yes --json \
  >"$CLOUD_DIR/board-applied.json"
sha256sum "$BOARD_SNAPSHOT_DIR/code-mower-cloud-bundle.json" \
  >"$CLOUD_DIR/board-bundle-after-apply.sha256"
CLOUD_DIR="$CLOUD_DIR" BOARD_SNAPSHOT_DIR="$BOARD_SNAPSHOT_DIR" "$RELEASE_PYTHON" - <<'PY'
import hashlib
import json
import os
from pathlib import Path

cloud_dir = Path(os.environ["CLOUD_DIR"])
bundle_dir = Path(os.environ["BOARD_SNAPSHOT_DIR"])


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def digest_of(path: Path) -> str:
    return path.read_text(encoding="utf-8").split()[0]


EXPECTED_EVENT_TYPES = {"board_snapshot": 1}
applied = load(cloud_dir / "board-applied.json")
probed_endpoint = str(load(cloud_dir / "doctor.json").get("endpoint") or "")
manifest_path = bundle_dir / "code-mower-cloud-bundle.json"
manifest = load(manifest_path)
raw_events = manifest.get("events")
events = list(raw_events) if isinstance(raw_events, list) else []
event_type_counts = {}
for row in events:
    if not isinstance(row, dict):
        event_type_counts = {}
        break
    key = str(row.get("event_type") or "")
    event_type_counts[key] = event_type_counts.get(key, 0) + 1
problems = []
if event_type_counts != EXPECTED_EVENT_TYPES:
    problems.append(f"board bundle event types are {event_type_counts}")
if applied.get("mode") != "cloud-upload":
    problems.append(f"board applied mode is {applied.get('mode')!r}")
if not 200 <= int(applied.get("status") or 0) < 300:
    problems.append(f"board upload was not accepted: {applied.get('status')!r}")
if not probed_endpoint or applied.get("endpoint") != probed_endpoint:
    problems.append("board applied upload does not target the probed service")
# The applied upload is also bound to the inspected bundle by external digests,
# so the manifest may not change between the accepted preview and the applied
# upload.
preflight = load(cloud_dir / "board-preflight.json")
previewed_digest = str(preflight.get("previewed_digest") or "")
previewed_identity = preflight.get("previewed_identity")
# The applied upload reports the exact manifest bytes and event identifiers it
# submitted, so a same-shape manifest swapped in before the mutation is caught
# by the producer itself rather than only by external hashes.
if not isinstance(previewed_identity, dict) or not previewed_identity:
    problems.append("the previewed board manifest identity was not retained")
elif applied.get("manifest") != previewed_identity:
    problems.append("board applied upload does not report the previewed manifest identity")
before_digest = digest_of(cloud_dir / "board-bundle-before-apply.sha256")
after_digest = digest_of(cloud_dir / "board-bundle-after-apply.sha256")
current_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
if not previewed_digest:
    problems.append("the previewed board bundle identity was not retained")
if (
    before_digest != after_digest
    or current_digest != before_digest
    or current_digest != previewed_digest
):
    problems.append("board bundle changed between the preview and the applied upload")
if problems:
    raise SystemExit(f"board snapshot upload is not a verified gate: {problems}")
print(json.dumps({
    "board_upload": "accepted",
    "event_types": sorted(event_type_counts),
    "reports": 0,
}))
PY
```

Record accepted event identifiers and counts only, never report prose, profile
paths, tokens, cloud team or install identifiers, endpoints, or local
configuration. The bundle manifest and its single event must name the release
repository, not only the top-level summary, so a truthful summary cannot cover
evidence gathered from another repository. The health of both `cloud doctor`
reports -- the standalone probe and the one nested in the Board snapshot -- is
derived from their raw check rows, so a falsified `status` or `failures` cannot
hide a degraded check. The nested report's inventory is exact: `endpoint`,
`token`, `bundle`, and `model-provenance` must pass, `service` must be the skip
this producer causes by not requesting `--probe-service`, and its endpoint must
equal the privately probed one. The Board snapshot and both generic upload
phases each report the exact manifest bytes and event identifiers they acted
on, and those producer-owned reports must be identical to one another, to the
inspected single `board_snapshot` event, and to the manifest digest, so a
same-shape manifest substituted around a phase cannot pass as the previewed
payload. The manifest is also hashed immediately before and immediately after
the dry-run preview and again around the applied upload, and every one of those
values must match the retained previewed identity. The snapshot additionally
carries the exact commit and clean state of the checkout it read, which must be
the release commit, and the command itself is required to fail unless that
checkout stays exactly that commit and clean while the snapshot is collected.

### 17. Rehearse the 1.4.1-to-1.4.2 upgrade in place, preserving existing state

Cold install alone does not prove upgrade safety: install the exact
digest-bound `v1.4.1` artifact, create representative state a real
installation would already hold, then upgrade in place to the exact
digest-verified `v1.4.2` artifact already downloaded in step 9 -- never a
fresh index re-resolution, which could silently install a different build
than the one this runbook verified -- and assert both the version
transition and that the preserved state survived untouched. This targets
headless Linux, so hashing uses `$RELEASE_PYTHON`'s own `hashlib`, not the
macOS-only `shasum`.

```bash
set -euo pipefail
CODE_MOWER_PYTHON="$(command -v python3.12)"
test -n "$CODE_MOWER_PYTHON"
UPGRADE_ENV="$(mktemp -d /tmp/code-mower-v142-upgrade-env.XXXXXX)"
"$CODE_MOWER_PYTHON" -m venv "$UPGRADE_ENV"

cat >"$RELEASE_ENV/sha256_of.py" <<'PY'
"""Print one file's SHA-256 digest, portable to headless Linux."""

import hashlib
import sys

print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())
PY

# Bind the exact v1.4.1 source wheel by digest, the same way step 9 already
# binds v1.4.2; the upgrade installs this exact downloaded file, not
# whatever the index resolves at rehearsal time.
V141_DOWNLOAD_DIR="$(mktemp -d /tmp/code-mower-v142-v141-download.XXXXXX)"
env -u PIP_INDEX_URL -u PIP_EXTRA_INDEX_URL -u PIP_FIND_LINKS -u PIP_NO_INDEX \
  PIP_CONFIG_FILE=/dev/null "$UPGRADE_ENV/bin/pip" download --no-cache-dir \
  --no-deps --index-url https://pypi.org/simple/ --dest "$V141_DOWNLOAD_DIR" \
  code-mower==1.4.1
V141_WHEEL="$V141_DOWNLOAD_DIR/code_mower-1.4.1-py3-none-any.whl"
test -s "$V141_WHEEL"
V141_WHEEL_SHA256="$("$RELEASE_PYTHON" "$RELEASE_ENV/sha256_of.py" "$V141_WHEEL")"
test -n "$V141_WHEEL_SHA256"

env -u PIP_INDEX_URL -u PIP_EXTRA_INDEX_URL -u PIP_FIND_LINKS -u PIP_NO_INDEX \
  PIP_CONFIG_FILE=/dev/null "$UPGRADE_ENV/bin/pip" install --no-cache-dir \
  --index-url https://pypi.org/simple/ "${V141_WHEEL}[coworker]"
test "$("$UPGRADE_ENV/bin/code-mower" --version)" = "code-mower 1.4.1"

UPGRADE_REPO="$(mktemp -d /tmp/code-mower-v142-upgrade-repo.XXXXXX)"
git -C "$UPGRADE_REPO" init -q
"$UPGRADE_ENV/bin/code-mower" init --packaged-starter --profile deep_review \
  --apply --output-dir "$UPGRADE_REPO/.code-mower.generated" \
  --skip-actionlint --skip-github-labels
PRESERVED_CONFIG_SHA256_BEFORE="$("$RELEASE_PYTHON" "$RELEASE_ENV/sha256_of.py" \
  "$UPGRADE_REPO/.code-mower.generated/code-mower.yml")"

# The exact wheel this runbook already digest-verified in step 9 -- not a
# fresh `code-mower==1.4.2` index resolution.
V142_WHEEL="$PYPI_DOWNLOAD_DIR/code_mower-1.4.2-py3-none-any.whl"
test -s "$V142_WHEEL"
env -u PIP_INDEX_URL -u PIP_EXTRA_INDEX_URL -u PIP_FIND_LINKS -u PIP_NO_INDEX \
  PIP_CONFIG_FILE=/dev/null "$UPGRADE_ENV/bin/pip" install --no-cache-dir \
  --index-url https://pypi.org/simple/ --upgrade "${V142_WHEEL}[coworker]"
test "$("$UPGRADE_ENV/bin/code-mower" --version)" = "code-mower 1.4.2"

PRESERVED_CONFIG_SHA256_AFTER="$("$RELEASE_PYTHON" "$RELEASE_ENV/sha256_of.py" \
  "$UPGRADE_REPO/.code-mower.generated/code-mower.yml")"
test "$PRESERVED_CONFIG_SHA256_AFTER" = "$PRESERVED_CONFIG_SHA256_BEFORE"

"$UPGRADE_ENV/bin/code-mower" doctor "$UPGRADE_REPO/.code-mower.generated/code-mower.yml" \
  --profile deep_review --json
```

Record `$V141_WHEEL_SHA256` and the `$PYPI_VERIFIED_MAP` entry for
`code_mower-1.4.2-py3-none-any.whl` (already bound in step 9) alongside this
rehearsal's outcome. A failed upgrade, a changed preserved-config digest, or
a version string that does not read exactly `code-mower 1.4.2` after the
upgrade fails this step; do not record upgrade coverage as passed on a
cold-install substitute or an index re-resolution that bypassed the verified
artifacts.

## Cache Bypass And Propagation Triage

Use cache-bypassing exact-version installs when validating a just-published
release. That keeps stale local wheels from looking like a successful release
and keeps PyPI propagation delays from looking like source regressions. Bind the
release once in the operator shell; update `RELEASE_VERSION` for the release
being verified instead of copying an older version pin through this reusable
section:

```bash
export RELEASE_VERSION="${RELEASE_VERSION:-1.6.0}"
export RELEASE_TAG="v$RELEASE_VERSION"
export RELEASE_SPEC="code-mower==$RELEASE_VERSION"
export RELEASE_WHEEL_STEM="code_mower-${RELEASE_VERSION}"
```

For pipx:

```bash
python3.12 --version
export CODE_MOWER_PYTHON="$(command -v python3.12)"
env -u PIP_INDEX_URL -u PIP_EXTRA_INDEX_URL -u PIP_FIND_LINKS -u PIP_NO_INDEX \
  PIP_CONFIG_FILE=/dev/null pipx install --force --backend pip \
  --python "$CODE_MOWER_PYTHON" --index-url https://pypi.org/simple/ \
  --pip-args='--isolated --no-cache-dir' "$RELEASE_SPEC"
code-mower --version
```

The environment cleanup matters as much as the cache flag: an ambient
`PIP_INDEX_URL`, `PIP_FIND_LINKS`, or `pip.conf` can otherwise supply the
"canonical" artifact from somewhere else entirely.

For uv:

```bash
uv python install 3.12
env -u UV_INDEX -u UV_DEFAULT_INDEX -u UV_INDEX_URL -u UV_EXTRA_INDEX_URL \
  -u UV_FIND_LINKS -u UV_NO_INDEX -u UV_OFFLINE \
  uv --no-config --no-cache tool install --python 3.12 --reinstall \
  --default-index https://pypi.org/simple/ "$RELEASE_SPEC"
code-mower --version
```

`--no-cache` is what bypasses the cache; `--refresh-package` only refreshes
resolution metadata. `--no-config` and the cleared `UV_*` variables keep a
project or user configuration from redirecting the index the same way an
ambient `pip.conf` can.

Before the candidate is available on TestPyPI or PyPI, validate the local wheel
from the release checkout:

```bash
scripts/dev-python -m build
export CODE_MOWER_PYTHON="$(command -v python3.12)"
env -u PIP_INDEX_URL -u PIP_EXTRA_INDEX_URL -u PIP_FIND_LINKS -u PIP_NO_INDEX \
  PIP_CONFIG_FILE=/dev/null pipx install --force --backend pip \
  --python "$CODE_MOWER_PYTHON" --index-url https://pypi.org/simple/ \
  --pip-args='--isolated --no-cache-dir' dist/code_mower-*.whl
env -u UV_INDEX -u UV_DEFAULT_INDEX -u UV_INDEX_URL -u UV_EXTRA_INDEX_URL \
  -u UV_FIND_LINKS -u UV_NO_INDEX -u UV_OFFLINE \
  uv --no-config --no-cache tool install --python 3.12 --reinstall \
  --default-index https://pypi.org/simple/ dist/code_mower-*.whl
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
env -u PIP_INDEX_URL -u PIP_EXTRA_INDEX_URL -u PIP_FIND_LINKS -u PIP_NO_INDEX \
  PIP_CONFIG_FILE=/dev/null /tmp/code-mower-pypi-smoke/bin/python -m pip --isolated \
  install --no-cache-dir --index-url https://pypi.org/simple/ --upgrade pip
env -u PIP_INDEX_URL -u PIP_EXTRA_INDEX_URL -u PIP_FIND_LINKS -u PIP_NO_INDEX \
  PIP_CONFIG_FILE=/dev/null /tmp/code-mower-pypi-smoke/bin/python -m pip --isolated \
  install --no-cache-dir --index-url https://pypi.org/simple/ "$RELEASE_SPEC"
/tmp/code-mower-pypi-smoke/bin/code-mower --version
```

Then run the release-gate first-user rehearsal against the same package:

```bash
env -u PIP_INDEX_URL -u PIP_EXTRA_INDEX_URL -u PIP_FIND_LINKS -u PIP_NO_INDEX \
  PIP_CONFIG_FILE=/dev/null code-mower migration package-install-rehearsal \
  --package-spec "$RELEASE_SPEC" \
  --allow-package-index \
  --pip-index-url https://pypi.org/simple/ \
  --upgrade-pip \
  --python "$(command -v python3.12)" \
  --json
```

Do not rehearse a TestPyPI candidate by adding production PyPI as an extra
index: pip gives the primary index no priority, so production PyPI can satisfy
`code-mower` and the run proves nothing about the candidate. Rehearse the
candidate using the immutable-candidate procedure for its release instead --
download the exact candidate wheel in an isolated, no-deps, TestPyPI-only step,
bind its filename and SHA-256, then rehearse that local wheel with
`--package-spec "/path/to/${RELEASE_WHEEL_STEM}-py3-none-any.whl"` while
dependencies resolve from canonical PyPI.

`code-mower release qualify` and `code-mower release campaign` accept the
equivalent closed `--package-source testpypi` flag (default: `pypi`) to
qualify the same TestPyPI candidate before it is announced or marked current
on production PyPI -- see
[Release Qualification](release-qualification.md#testpypi-candidates):

```bash
code-mower release qualify \
  --release-tag "$RELEASE_TAG" \
  --package-spec "$RELEASE_SPEC" \
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
RELEASE_VERSION="${RELEASE_VERSION:-1.6.0}"
RELEASE_SPEC="code-mower==$RELEASE_VERSION"
CODE_MOWER_PYTHON="$(command -v python3.12)"
pipx install --python "$CODE_MOWER_PYTHON" "$RELEASE_SPEC"
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
