# v1.5.1 immutable candidate and publication runbook

The release PR performs no tag or package publication. Qualification consumes
the exact retained candidate built from its merge SHA. Index commands select
`code-mower==1.5.1` only after publication. Slack telemetry remains deferred to v1.6.0.

## 1. Review and merge the release PR

Require one writer, independent exact-head audit with no P0/P1/P2 findings,
Python 3.12–3.14 CI, containment, Board qualification, wheel rehearsal,
release-integrity checks, and the authoritative gate. Confirm every required
implementation PR in [the qualification contract](v151-qualification.md) is an
ancestor. Bind the merged PR and SHA:

```bash
set -euo pipefail
REPO=codemower-ai/code-mower
RELEASE_PR=REPLACE_WITH_RELEASE_PR
test "$(gh pr view "$RELEASE_PR" --repo "$REPO" --json state --jq '.state')" = MERGED
RELEASE_SHA="$(gh pr view "$RELEASE_PR" --repo "$REPO" --json mergeCommit --jq '.mergeCommit.oid')"
[[ "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]]
```

## 2. Build and retain the merge-SHA candidate once

Dispatch while `main` still equals the release SHA. The workflow rejects a
branch mismatch and any rerun.

```bash
gh workflow run release-candidate.yml --repo "$REPO" --ref main \
  -f expected_sha="$RELEASE_SHA" -f release_pr="$RELEASE_PR"
```

Inspect the workflow inputs and first-attempt result, bind its exact run ID, and
download the retained artifacts:

```bash
CANDIDATE_RUN_ID=REPLACE_WITH_VERIFIED_RUN_ID
CANDIDATE_DIR="$PWD/v151-candidate-$CANDIDATE_RUN_ID"
gh run download "$CANDIDATE_RUN_ID" --repo "$REPO" \
  --name code-mower-candidate --dir "$CANDIDATE_DIR"
python scripts/release_candidate.py verify --dist "$CANDIDATE_DIR" \
  --source-sha "$RELEASE_SHA" --require-candidate
```

`candidate.json` and `rehearsal.json` must bind the release PR, source SHA,
wheel digest, sdist digest, complete inventories, and every required installed
wheel rehearsal. Retain this pair. Publication downloads it and does not rebuild.
A source or packaged-doc change invalidates it.

## 3. Qualify the exact candidate

Use disposable environments and the retained wheel for:

- a fresh install without uv or pipx using the documented Python bootstrap;
- an upgrade from v1.5.0 with state preservation;
- checkout-free remote observer doctor output with no local path;
- safe init with an existing root configuration and unique generated targets;
- transient and persistent Board lifecycle plus version/status presentation;
- Graphify build, status, connection, and query-reader compatibility;
- the basic Slack lifecycle, without adding telemetry or richer UX; and
- single-lane and multi-lane audit publication, including writer-lane
  self-exclusion and exact `source_job_id` binding.

Record only public identifiers and sanitized outcomes. Keep source, logs,
credentials, Slack content, graph content, transcripts, and local paths private.

## 4. Observe the bounded hosted Board canary

Use the owner-authorized numeric aggregate campaign ACU cap and one bounded
provider create. Reconcile an uncertain create before any retry. Record every
reservation and create attempt, provider completion and provider exit, the
closed Board projection, privacy outcome, authorized usage, settled usage, and
any remaining billing uncertainty separately. Do not infer provider exit from
logical Code Mower completion.

Upload only the approved metadata-only evidence to codemower.com. Record the
upload receipt, then use a fresh dashboard view in an authenticated session to verify the new
evidence independently. An accepted HTTP response with a stale aggregate is
not the dashboard observation.

## 5. Owner decision, unchanged tag, and publication

After the exact candidate and hosted observation pass, recheck the release PR,
review, CI, gate, candidate digests, codemower.com evidence, and current PyPI
state. Tag the release merge SHA and run a no-publish verification first:

```bash
git fetch origin "$RELEASE_SHA"
test "$(gh pr view "$RELEASE_PR" --repo "$REPO" --json mergeCommit --jq '.mergeCommit.oid')" = "$RELEASE_SHA"
git tag -a v1.5.1 "$RELEASE_SHA" -m 'Code Mower v1.5.1'
git push origin refs/tags/v1.5.1
test "$(git rev-list -n 1 v1.5.1)" = "$RELEASE_SHA"
gh workflow run release.yml --repo "$REPO" --ref v1.5.1 \
  -f expected_sha="$RELEASE_SHA" -f candidate_run_id="$CANDIDATE_RUN_ID" \
  -f publish_testpypi=false -f publish_pypi=false
```

Verify the selected run's tag, SHA, candidate identity, digests, inventories,
and rehearsal. Then publish the same candidate:

```bash
gh workflow run release.yml --repo "$REPO" --ref v1.5.1 \
  -f expected_sha="$RELEASE_SHA" -f candidate_run_id="$CANDIDATE_RUN_ID" \
  -f publish_testpypi=false -f publish_pypi=true
```

Keep release-event publish variables explicitly false and bind
`CODE_MOWER_CANDIDATE_RUN_ID` to the accepted run. Create the GitHub Release
with the two files from `CANDIDATE_DIR`, the immutable release notes, and only
sanitized observed identifiers. Require the release-event verification run to
consume the same candidate and skip both publication jobs. Compare downloaded
GitHub assets byte-for-byte with the retained pair.

## 6. Independent canonical reinstall and closeout

Download `code-mower==1.5.1` from canonical PyPI without cache or extra indexes.
Verify the wheel and sdist digests, `code-mower --version`, installed metadata,
remote observer, safe init, Board, Graphify, and basic Slack behavior. Never
downgrade live v2 claims during rollback; use disposable state for rollback
checks.

Record the release SHA, candidate/publication/release-event run IDs, artifact
digests, canonical reinstall, hosted canary, and codemower.com dashboard result
on #1056 and the GitHub Release. Close #951 and #945 only after the Board canary
is observed, then close #1050 and #1056 when every required item is complete.
