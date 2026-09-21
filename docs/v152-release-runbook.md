# v1.5.2 immutable candidate and publication runbook

The release PR performs no tag or package publication. Qualification consumes
the exact retained candidate built from its merge SHA. Index commands select
`code-mower==1.5.2` only after publication. Slack telemetry remains deferred to v1.6.0.

## 1. Review and merge the release PR

Require one writer, independent exact-head audit with no P0/P1/P2 findings,
Python 3.12–3.14 CI, documentation lifecycle and rendering checks, package
projection and wheel rehearsal, release-integrity checks, and the authoritative
gate. Confirm every required implementation PR in
[the qualification contract](v152-qualification.md) is an ancestor. Bind the
merged PR and SHA:

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
CANDIDATE_DIR="$PWD/v152-candidate-$CANDIDATE_RUN_ID"
gh run download "$CANDIDATE_RUN_ID" --repo "$REPO" \
  --name code-mower-candidate --dir "$CANDIDATE_DIR"
python scripts/release_candidate.py verify --dist "$CANDIDATE_DIR" \
  --source-sha "$RELEASE_SHA" --require-candidate
```

`candidate.json` and `rehearsal.json` must bind the release PR, source SHA,
wheel digest, sdist digest, complete inventories, and every required installed
wheel rehearsal. Retain this pair. Publication downloads it and does not rebuild.
A source or packaged-document change invalidates it.

## 3. Qualify the exact candidate

Use disposable environments and the retained wheel for:

- a fresh install without uv or pipx using Python 3.12;
- an upgrade from v1.5.1 with state preservation and disposable rollback;
- the checkout-free remote observer and safe init paths retained from v1.5.1;
- documentation lifecycle, link, generated-index, and rendered-region checks;
- fresh-package template projection and required-source checks;
- Graphify installed-reader compatibility and bounded synthetic queries;
- the basic Slack lifecycle offline, without adding telemetry or richer UX; and
- single-lane and multi-lane audit publication, including exact source-job
  binding, when evaluating the release PR.

Record only public identifiers and sanitized outcomes. Keep source, logs,
credentials, Slack content, graph content, transcripts, and local paths private.

## 4. Observe the bounded hosted Board canary

This maintenance release requires equivalence, not a new paid hosted canary.
Review the exact source and packaged-member delta from the immutable v1.5.1
tag to the v1.5.2 candidate. If changes are limited to documentation, release
machinery, and package-template projection and all offline checks pass, record
that the prior hosted evidence remains applicable.
Do not consume aggregate campaign ACU or create a provider session. The prior
record keeps provider exit, authorized usage, and settled usage as separate
facts.

If the comparison finds a runtime, dependency, authority, privacy, Slack, or
hosted-service change, stop. Obtain a separately authorized numeric aggregate
campaign ACU cap and qualify the affected boundary before continuing. A
metadata-only upload or fresh dashboard observation is required only when new
hosted evidence is created; an accepted upload never substitutes for that
fresh dashboard observation.

## 5. Owner decision, unchanged tag, and publication

After exact-candidate qualification passes, recheck the release PR, independent
review, CI, authoritative gate, candidate digests, and current PyPI state. Tag
the release merge SHA and run a no-publish verification first:

```bash
git fetch origin "$RELEASE_SHA"
test "$(gh pr view "$RELEASE_PR" --repo "$REPO" --json mergeCommit --jq '.mergeCommit.oid')" = "$RELEASE_SHA"
git tag -a v1.5.2 "$RELEASE_SHA" -m 'Code Mower v1.5.2'
git push origin refs/tags/v1.5.2
test "$(git rev-list -n 1 v1.5.2)" = "$RELEASE_SHA"
gh workflow run release.yml --repo "$REPO" --ref v1.5.2 \
  -f expected_sha="$RELEASE_SHA" -f candidate_run_id="$CANDIDATE_RUN_ID" \
  -f publish_testpypi=false -f publish_pypi=false
```

Verify the selected run's tag, SHA, candidate identity, digests, inventories,
and rehearsal. Then publish the same candidate:

```bash
gh workflow run release.yml --repo "$REPO" --ref v1.5.2 \
  -f expected_sha="$RELEASE_SHA" -f candidate_run_id="$CANDIDATE_RUN_ID" \
  -f publish_testpypi=false -f publish_pypi=true
```

Keep release-event publish variables explicitly false and bind
`CODE_MOWER_CANDIDATE_RUN_ID` to the accepted run. Create the GitHub Release
with the two files from `CANDIDATE_DIR`, the immutable release notes, and only
sanitized observed identifiers. Require the release-event verification run to
consume the same candidate and skip both publication jobs. Compare downloaded
GitHub assets byte-for-byte with the retained pair.

## 6. Independent canonical reinstall

Download `code-mower==1.5.2` from canonical PyPI without cache or extra indexes.
Verify the wheel and sdist digests, `code-mower --version`, installed metadata,
documentation inventory, safe init, Board status, Graphify, and basic Slack
offline behavior. Never downgrade live v2 claims during rollback; use
disposable state for rollback checks.

Record the release SHA, candidate, publication and release-event run IDs,
artifact digests, equivalence result, and canonical reinstall on #1078 and the
GitHub Release, then close #1078.
