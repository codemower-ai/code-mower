# v1.6.1 immutable candidate and publication runbook

The release PR performs no tag or package publication. Qualification consumes
the exact retained candidate built from its merge SHA. Index commands select
`code-mower==1.6.1` only after publication.

## 0. Prove the release entry gates

The v1.6.0 entry gates remain closed: Board clarity #1063, audit-history
hardening #1104, and hosted capability tracking #978 are complete. Recheck the
hosted production capability before candidate dispatch. Require the exact accepted contract identity
already qualified for v1.6.0. A stale response,
`accepting: false`, or a changed schema, version, or digest reopens the gate.

Confirm #1119 and #1120 are ancestors of the prospective release head. These
are the only intended runtime changes in v1.6.1.

## 1. Review and merge the release PR

Require one writer, an independent exact-head audit with no P0, P1, or P2
findings, Python 3.12 through 3.14 CI, documentation lifecycle and rendering
checks, package projection and wheel rehearsal, release-integrity checks, and
the authoritative gate. Confirm #1119 and #1120 are ancestors, then bind the
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
branch mismatch and any rerun:

```bash
gh workflow run release-candidate.yml --repo "$REPO" --ref main \
  -f expected_sha="$RELEASE_SHA" -f release_pr="$RELEASE_PR"
```

Bind the first successful attempt, download it, and verify it:

```bash
CANDIDATE_RUN_ID=REPLACE_WITH_VERIFIED_RUN_ID
CANDIDATE_DIR="$PWD/v161-candidate-$CANDIDATE_RUN_ID"
gh run download "$CANDIDATE_RUN_ID" --repo "$REPO" \
  --name code-mower-candidate --dir "$CANDIDATE_DIR"
python scripts/release_candidate.py verify --dist "$CANDIDATE_DIR" \
  --source-sha "$RELEASE_SHA" --require-candidate
```

`candidate.json` and `rehearsal.json` must bind the release PR, source SHA,
artifact digests, inventories, and every installed-wheel rehearsal. Publication
downloads those retained bytes and does not rebuild them.

## 3. Qualify the exact candidate

Use disposable environments and the retained wheel for:

- a fresh install without uv or pipx using Python 3.12;
- an upgrade from v1.6.0 with state preservation and disposable rollback;
- the checkout-free remote observer and safe init paths;
- the exact private runner-env doctor with redacted Codex and Claude probes;
- revoked-token bounce isolation and real-audit token preservation;
- unlabeled refusal, label-authorized merge, paginated-label, and moved-head
  gate fixtures;
- Board, Graphify, basic Slack lifecycle, metadata-only privacy, and
  capability-gated lifecycle-summary regression checks; and
- two independent installation or upgrade passes in clean environments.

## 4. Run one bounded private Slack telemetry canary

Revalidate the accepted v1.6.0 bounded private Slack telemetry canary and local
versus hosted reconciliation. Because v1.6.1 changes only runner diagnostics
and merge authorization, do not create paid provider work unless comparison
with v1.6.0 finds an unexpected provider, privacy, cloud, or service-contract
change. If new provider work becomes necessary, obtain a numeric task and
aggregate campaign ACU cap first, and record provider exit, authorized usage,
and settled usage as separate facts. Record only public identifiers and
sanitized outcomes.

## 5. Soak and independent adoption

Keep candidate bytes unchanged for at least 24 hours after the final source
change. During the interval, obtain the two independent installation or upgrade
passes required above. A source or packaged-document change restarts
qualification. If an
emergency patch requires a shorter soak, record the reason, risk, independent
evidence, and rollback plan on #1121 before publication.

## 6. Owner decision, unchanged tag, and publication

After qualification, recheck the release PR, independent review, CI,
authoritative gate, candidate digests, adoption passes, and current PyPI state.

### Tag and publish unchanged bytes

Tag the release merge SHA and run no-publish verification first:

```bash
git fetch origin "$RELEASE_SHA"
test "$(gh pr view "$RELEASE_PR" --repo "$REPO" --json mergeCommit --jq '.mergeCommit.oid')" = "$RELEASE_SHA"
git tag -a v1.6.1 "$RELEASE_SHA" -m 'Code Mower v1.6.1'
git push origin refs/tags/v1.6.1
test "$(git rev-list -n 1 v1.6.1)" = "$RELEASE_SHA"
gh workflow run release.yml --repo "$REPO" --ref v1.6.1 \
  -f expected_sha="$RELEASE_SHA" -f candidate_run_id="$CANDIDATE_RUN_ID" \
  -f publish_testpypi=false -f publish_pypi=false
```

Verify the tag, SHA, candidate identity, digests, inventories, and rehearsal.
Then publish the same candidate:

```bash
gh workflow run release.yml --repo "$REPO" --ref v1.6.1 \
  -f expected_sha="$RELEASE_SHA" -f candidate_run_id="$CANDIDATE_RUN_ID" \
  -f publish_testpypi=false -f publish_pypi=true
```

Keep release-event publish variables explicitly false and bind
`CODE_MOWER_CANDIDATE_RUN_ID` to the accepted run. Create the GitHub Release
with the retained wheel and sdist plus immutable release notes. Require the
release-event run to consume the same candidate and skip both publication jobs.
Compare downloaded GitHub and canonical PyPI assets byte-for-byte.

## 7. Independent canonical reinstall and closeout

Download `code-mower==1.6.1` from canonical PyPI without cache or extra indexes.
Verify artifact digests, `code-mower --version`, installed metadata, safe init,
runner doctor, Board status, Graphify, and basic Slack. Record the release SHA,
candidate, soak decision, adoption passes, publication and release-event run
IDs, artifact digests, and reinstall evidence on #1121 and the GitHub Release.
Close #1121 and the v1.6.1 milestone only after the public package and release
evidence agree.
