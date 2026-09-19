# v1.5.0 immutable candidate and publication runbook

The release preparation PR performs no paid work, tag, package publication,
hosted deployment or live Slack mutation. Index install commands select
`code-mower==1.5.0` after publication; qualification before that uses the exact
local wheel. The [qualification document](v150-qualification.md) is the release
contract, while observed results belong on #923 and the GitHub Release. Never
treat a pre-merge wheel as the final candidate.

## 1. Review and merge the preparation PR

Require one writer, independent exact-head audit with no P0/P1/P2 findings,
normal CI, the authoritative gate, full tests, lint, privacy scan, package guards,
release identity and `migration release-readiness --json`. Dependencies #1007,
#1024, #1025, #1031 and #1037 must be ancestors. The release preparation PR
must also contain the final reviewed release notes, qualification contract and
publication instructions; a prior code-only or pre-#1037 head is not the final
documentation head. The owner-controlled merge process supplies the final
source SHA. Do not merge or bypass a gate as part of rehearsal.

In one operator shell, set the actual PR number, then bind it once:

```bash
set -euo pipefail
REPO=codemower-ai/code-mower
RELEASE_PR=REPLACE_WITH_PREPARATION_PR_NUMBER
test "$(gh pr view "$RELEASE_PR" --repo "$REPO" --json state --jq '.state')" = MERGED
RELEASE_SHA="$(gh pr view "$RELEASE_PR" --repo "$REPO" --json mergeCommit --jq '.mergeCommit.oid')"
[[ "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]]
```

## 2. Build and retain the merge-SHA candidate once

Dispatch while `main` still points at `RELEASE_SHA`: the workflow requires its
own `GITHUB_SHA` to equal that source SHA before checkout or build. It also
requires `GITHUB_RUN_ATTEMPT=1`; GitHub reruns are refused before any build.
If main has advanced, stop and resolve the release source through a newly
reviewed preparation PR; do not use an ancestor as a substitute workflow head.

```bash
gh workflow run release-candidate.yml --repo "$REPO" --ref main \
  -f expected_sha="$RELEASE_SHA" -f release_pr="$RELEASE_PR"
```

Bind `CANDIDATE_RUN_ID` to this exact dispatch after inspecting its inputs and
successful conclusion. Do not select the latest run by name alone. The workflow
checks the merged PR, exact clean source, version text, builds wheel/sdist once,
checks both inventories and runs disposable rehearsals against that wheel.
It uploads only artifacts, `candidate.json` and `rehearsal.json`, never raw logs
or private state. No tag is required. Retention is 90 days; retain the approved
pair securely for #918/#920/#923. Expiration is a stop, not permission to rebuild.

```bash
CANDIDATE_RUN_ID=REPLACE_WITH_VERIFIED_RUN_ID
CANDIDATE_DIR="$PWD/v150-candidate-$CANDIDATE_RUN_ID"
gh run download "$CANDIDATE_RUN_ID" --repo "$REPO" \
  --name code-mower-candidate --dir "$CANDIDATE_DIR"
python scripts/release_candidate.py verify --dist "$CANDIDATE_DIR" \
  --source-sha "$RELEASE_SHA" --require-candidate
```

Record PR/merge SHA, workflow run, both SHA-256 digests, inventory outcome and
the sanitized rehearsal result on #923 (with #1027 retained as preparation
history). `candidate.json` must name that merged PR and SHA. Subsequent
qualification/publication must consume the retained pair; never rerun the
candidate build or dispatch a second build for that SHA. If code or packaged
documentation changes, invalidate this candidate explicitly and repeat all
gates for a newly reviewed source. Do not tag or publish the invalidated bytes.

For **pre-merge local rehearsal only**, clone the reviewed head into a new clean
directory, install build/twine in a separate tools venv and use:

```bash
python scripts/release_candidate.py build --source "$EXACT_CHECKOUT" \
  --source-sha "$REHEARSAL_SHA" --dist "$REHEARSAL_DIST"
python scripts/rehearse_v150.py --dist "$REHEARSAL_DIST" \
  --source-sha "$REHEARSAL_SHA" --work-dir "$NEW_DISPOSABLE_DIR"
```

No `--release-pr` means `kind=rehearsal`; publication rejects it. Output/work
directories must be new, and build output must be outside the exact checkout.
Use Python 3.12+ from the selected runner PATH. The script installs only package
dependencies from canonical PyPI. Product smoke runs use installed modules with
network denial and no inherited provider credentials. Only read-only,
transport-disabled Git commands against the synthetic fixture are allowed as
product subprocesses. Default install,
explicit Slack setup, offline doctor, 1.4.2 upgrade, disposable rollback and
uninstall are checked. Disabled snapshots and local manifest removal are offline
evidence only, not live hosted disable/uninstall.

The same installed wheel publishes synthetic complete Graphify generations and
checks the real reader, status, connection and query paths: `doc_ref` is excluded
as non-code while search remains available; ambiguity alone yields a usable
partial answer from a complete generation; and an unknown type from a different
distribution at the reviewed version yields bounded `reader_incompatible`
diagnostics with no type, path or content leakage. These three named checks are
required in `rehearsal.json` before publication. No private graph/adoption data
or live extractor is used.

The normal CI `release wheel rehearsal` job builds the exact PR head as
`kind=rehearsal` and runs these checks before merge. Its sanitized evidence
artifact is not a release candidate and cannot be selected for publication.

## 3. Private acceptance consumes these exact bytes (#918)

Bind private installation/administration evidence to `RELEASE_SHA` and the wheel
digest, validate the host implementation lock, use fresh trusted-host probes,
and complete private disable/uninstall/state-preservation checks. Do not upload
private snapshots, bindings, logs, graphs or adoption evidence. Stop if the
installed package identity differs. Offline snapshots never prove live readiness.

## 4. Explicitly authorize and run only two canaries (#920)

After #918 passes, obtain the recorded numeric task and aggregate campaign ACU
caps, task count/expiry, runtime and review spend/round limits, clarification/fix
allowances and zero recovery creates. Run one completion and one confirmed
cancellation only. Observe actual builder/reviewer exit, independent exact-head
review and gate evidence. Preserve unsettled/unknown outcomes and full original
reservations. No elapsed time, credits or prior release authorization substitutes
for this decision. A failure blocks publication.

## 5. Owner decision, unchanged tag and publication (#923)

Only after #918 and #920 pass against the retained candidate does the owner
record the release decision on #923. Recheck candidate digests, the preparation
PR's merge SHA, independent review, CI and authoritative gate before tagging.
Do not modify release source to append qualification evidence.

```bash
git fetch origin "$RELEASE_SHA"
test "$(gh pr view "$RELEASE_PR" --repo "$REPO" --json mergeCommit --jq '.mergeCommit.oid')" = "$RELEASE_SHA"
git tag -a v1.5.0 "$RELEASE_SHA" -m 'Code Mower v1.5.0'
git push origin refs/tags/v1.5.0
test "$(git rev-list -n 1 v1.5.0)" = "$RELEASE_SHA"
test "$(git ls-remote origin 'refs/tags/v1.5.0^{}' | awk '{print $1}')" = "$RELEASE_SHA"
gh workflow run release.yml --repo "$REPO" --ref v1.5.0 \
  -f expected_sha="$RELEASE_SHA" -f candidate_run_id="$CANDIDATE_RUN_ID" \
  -f publish_testpypi=false -f publish_pypi=false
```

Verify the exact no-publish run's ref/SHA, identity/retrieval/verification success
and skipped publication jobs. The release workflow validates the candidate run
origin, exact run head SHA, first attempt, merged PR, source SHA, digest pair,
inventories and required rehearsal checks bound to the single verified wheel.
It **downloads the retained candidate; it does not rebuild**. Publication never
accepts a pre-merge rehearsal. If TestPyPI is required by the owner, dispatch the
same tag/SHA/run with only `publish_testpypi=true` and independently verify that
index's exact package and digests; never combine TestPyPI and PyPI indexes.

After inspecting the no-publish result and the owner decision:

```bash
gh workflow run release.yml --repo "$REPO" --ref v1.5.0 \
  -f expected_sha="$RELEASE_SHA" -f candidate_run_id="$CANDIDATE_RUN_ID" \
  -f publish_testpypi=false -f publish_pypi=true
```

Keep `CODE_MOWER_PYPI_PUBLISH` and `CODE_MOWER_TESTPYPI_PUBLISH` off so creating
the GitHub Release does not publish twice. For release-event verification, set
`CODE_MOWER_CANDIDATE_RUN_ID` to this same verified run before creating the release.
Keep the trusted `pypi`/`testpypi` environments and explicit owner release decision;
a passing offline rehearsal is not publication approval.

### Create and verify the GitHub Release

The GitHub Release is the mutable public record for observed qualification and
publication evidence. Prepare a release body that starts with the immutable
`docs/v150-release-notes.md` text, then adds only sanitized observations: links
to #923 and the public evidence comments, the exact release PR/merge SHA,
candidate and publication workflow run IDs, artifact digests, canary outcomes,
and canonical reinstall outcome. Do not put private observations or secrets in
the body.

Before release creation, assert both release-event publish switches are false
and set the temporary candidate lookup variable. An unset publish switch is not
accepted as evidence of false. Stop if a release already exists; never clobber
or silently edit one in this creation path.

```bash
set -euo pipefail
RELEASE_TAG=v1.5.0
RELEASE_TITLE='Code Mower v1.5.0'
GITHUB_RELEASE_NOTES=REPLACE_WITH_SANITIZED_RELEASE_BODY_FILE
RELEASE_ASSET_DIR="$CANDIDATE_DIR"

gh variable set CODE_MOWER_TESTPYPI_PUBLISH --repo "$REPO" --body false
gh variable set CODE_MOWER_PYPI_PUBLISH --repo "$REPO" --body false
gh variable set CODE_MOWER_CANDIDATE_RUN_ID --repo "$REPO" \
  --body "$CANDIDATE_RUN_ID"
test "$(gh variable get CODE_MOWER_TESTPYPI_PUBLISH --repo "$REPO" \
  --json value --jq .value)" = false
test "$(gh variable get CODE_MOWER_PYPI_PUBLISH --repo "$REPO" \
  --json value --jq .value)" = false
test "$(gh variable get CODE_MOWER_CANDIDATE_RUN_ID --repo "$REPO" \
  --json value --jq .value)" = "$CANDIDATE_RUN_ID"

if gh release view "$RELEASE_TAG" --repo "$REPO" >/dev/null 2>&1; then
  echo "release already exists; inspect it without mutation" >&2
  exit 1
fi
gh release create "$RELEASE_TAG" \
  "$RELEASE_ASSET_DIR/code_mower-1.5.0-py3-none-any.whl" \
  "$RELEASE_ASSET_DIR/code_mower-1.5.0.tar.gz" \
  --repo "$REPO" --verify-tag --latest --title "$RELEASE_TITLE" \
  --notes-file "$GITHUB_RELEASE_NOTES"
```

Bind `RELEASE_EVENT_RUN_ID` by inspecting the `release`-event run created by
that exact published release. Require its tag and resolved SHA to equal
`RELEASE_TAG` and `RELEASE_SHA`, require candidate retrieval and distribution
verification to pass, and require both publish jobs to be skipped. A manual
publish success does not substitute for this release-event check.

Download the published assets into a new directory and compare them byte for
byte with the retained candidate. Verify that the release is neither draft nor
prerelease and that the latest-release endpoint selects it:

```bash
RELEASE_DOWNLOAD_DIR="$(mktemp -d /tmp/code-mower-v150-release.XXXXXX)"
gh run watch "$RELEASE_EVENT_RUN_ID" --repo "$REPO" --exit-status
gh release view "$RELEASE_TAG" --repo "$REPO" \
  --json tagName,targetCommitish,isDraft,isPrerelease,assets,url
gh release download "$RELEASE_TAG" --repo "$REPO" \
  --dir "$RELEASE_DOWNLOAD_DIR" --pattern 'code_mower-1.5.0*'
cmp "$CANDIDATE_DIR/code_mower-1.5.0-py3-none-any.whl" \
  "$RELEASE_DOWNLOAD_DIR/code_mower-1.5.0-py3-none-any.whl"
cmp "$CANDIDATE_DIR/code_mower-1.5.0.tar.gz" \
  "$RELEASE_DOWNLOAD_DIR/code_mower-1.5.0.tar.gz"
test "$(gh api "repos/$REPO/releases/latest" --jq .tag_name)" = "$RELEASE_TAG"
```

Only after that run and asset verification succeed, remove the temporary
candidate lookup variable. Leave both release-event publish variables explicitly
`false`; they are durable fail-closed defaults, not temporary authorization.

```bash
gh variable delete CODE_MOWER_CANDIDATE_RUN_ID --repo "$REPO"
if gh variable get CODE_MOWER_CANDIDATE_RUN_ID --repo "$REPO" >/dev/null 2>&1; then
  echo "temporary candidate variable still exists" >&2
  exit 1
fi
test "$(gh variable get CODE_MOWER_TESTPYPI_PUBLISH --repo "$REPO" \
  --json value --jq .value)" = false
test "$(gh variable get CODE_MOWER_PYPI_PUBLISH --repo "$REPO" \
  --json value --jq .value)" = false
```

## 6. Independent canonical reinstall and release evidence

Download exact 1.5.0 from production PyPI without dependencies/config/cache into
a new directory and compare both canonical artifact digests with `candidate.json`
before installing. Do not rebuild from the sdist or substitute checkout modules.
Use a fresh venv or isolated uv/pipx home and record command/version/provenance,
`pip check`, fresh install, 1.4.2 upgrade and preserved state. Repeat the offline
Slack checks using the published wheel, and independently verify the private
host's installed implementation lock through #923. Inspect every existing Board
binding privately and restart only through its owned managed/transient lifecycle.

Confirm the GitHub Release assets still match the exact verified wheel/sdist,
record the candidate, publication and release-event runs and digests, then add
the sanitized reinstall outcome to #923 and the GitHub Release evidence. Link
#1027/#918/#920 as supporting history without copying private observations. Do
not claim a package is independently reinstalled from its build log alone. No
cloud upload is implicit.

## Live rollback boundary

The disposable 1.4.2 downgrade is **not** an operational rollback of durable v2
state. Disable admission, reconcile original work and confirmed provider exits,
retain claims/receipts/reservations, and restore only a reviewed compatible
runtime through its owner-controlled rollback. Never downgrade live v2 claims,
delete uncertainty or refund allowances. Re-enable only after fresh qualification
and an explicit owner decision. See [the Slack runbook](slack-setup.md).
