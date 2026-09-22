# v1.6.0 immutable candidate and publication runbook

The release PR performs no tag, immutable build, production enablement, or
publication. Qualification consumes one exact retained candidate built from
the release PR merge SHA after every entry gate is complete.

## 0. Prove the release entry gates

Do not dispatch the candidate workflow until #1063 and #1104 are merged and the
CodeMower.com #978 deployment advertises the exact accepted contract identity from
the [qualification contract](v160-qualification.md). Confirm each merge is an
ancestor of the prospective release head, inspect the authenticated hosted
health response, and record only public deployment and contract identifiers.

Production client emission remains disabled during this check. A health response
that is unauthenticated, stale, missing either identity, or names another digest
does not satisfy the gate.

## 1. Review and merge the release PR

Require one writer, independent exact-head audit with no P0/P1/P2 findings,
Python 3.12–3.14 CI, documentation lifecycle and rendering checks, package
projection and wheel rehearsal, release-integrity checks, and the authoritative gate.
Confirm every required implementation is an ancestor, then bind the
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
branch mismatch and every rerun.

```bash
gh workflow run release-candidate.yml --repo "$REPO" --ref main \
  -f expected_sha="$RELEASE_SHA" -f release_pr="$RELEASE_PR"
```

Bind the first successful attempt and download its retained artifacts:

```bash
CANDIDATE_RUN_ID=REPLACE_WITH_VERIFIED_RUN_ID
CANDIDATE_DIR="$PWD/v160-candidate-$CANDIDATE_RUN_ID"
gh run download "$CANDIDATE_RUN_ID" --repo "$REPO" \
  --name code-mower-candidate --dir "$CANDIDATE_DIR"
python scripts/release_candidate.py verify --dist "$CANDIDATE_DIR" \
  --source-sha "$RELEASE_SHA" --require-candidate
```

`candidate.json` and `rehearsal.json` must bind the release PR, source SHA,
artifact digests, inventories, and every installed-wheel rehearsal. Retain this
pair. Publication downloads it and does not rebuild it.

## 3. Qualify the exact candidate

Use disposable environments and the retained wheel for:

- a fresh install without uv or pipx using Python 3.12;
- an upgrade from v1.5.2 with state preservation and disposable rollback to the
  manifest's digest-verified v1.5.2 wheel;
- the checkout-free remote observer and safe init paths;
- payload-aware long audit histories, explanatory marker-name prose, valid
  controls, malformed real controls, pagination mutation, and bounded refusal;
- Board repository filtering, invoking/serving version parity, stale-service
  guidance, atomic replacement/rollback, and lifecycle-summary projection;
- Graphify installed-reader compatibility and bounded synthetic queries;
- basic Slack lifecycle readiness without claiming a live canary; and
- capability-gated lifecycle-summary suppression, privacy, and tenant fixtures.

Record only public identifiers and sanitized outcomes. Keep source, logs,
credentials, Slack content, graph content, transcripts, mappings, and local
paths private.

## 4. Run one bounded private Slack telemetry canary

Obtain the owner's numeric task and aggregate campaign ACU cap before creating
paid provider work. Provider authorized usage and settled usage are separate facts.
Use the exact candidate and the deployed #978 consumer. Exercise a bounded
private lifecycle and reconcile:

- the local Board's current summary and transition count;
- authenticated hosted aggregate totals and freshness;
- suppression of timestamp-only polls and nonterminal elapsed/usage changes;
- metadata-only field inventory and tenant/repository isolation;
- retention, export, deletion, client disablement, hosted disablement, and
  rollback; and
- provider exit separately from logical completion and billing settlement.

Any contract mismatch, private-field appearance, unexplained count difference,
wrong-tenant visibility, unavailable rollback, or ambiguous provider exit stops
qualification. Never replace observed failure with an owner override.

## 5. Soak and independent adoption

Keep the candidate bytes unchanged for at least 24 hours after the accepted
canary. During that interval, obtain two independent installation or upgrade
passes in clean environments. Each pass must verify the artifact digest,
version, doctor, safe initialization, basic Slack readiness, Graphify, Board,
and uninstall or rollback behavior. A source or packaged-document change starts
qualification again with newly reviewed release source.

## 6. Owner decision, unchanged tag, and publication

After every qualification row passes, recheck the release PR, independent
reviews, CI, gate, candidate digests, hosted capability, canary, soak, adoption
passes, and current PyPI state. Tag the release merge SHA and run a no-publish
verification first:

```bash
git fetch origin "$RELEASE_SHA"
test "$(gh pr view "$RELEASE_PR" --repo "$REPO" --json mergeCommit --jq '.mergeCommit.oid')" = "$RELEASE_SHA"
git tag -a v1.6.0 "$RELEASE_SHA" -m 'Code Mower v1.6.0'
git push origin refs/tags/v1.6.0
test "$(git rev-list -n 1 v1.6.0)" = "$RELEASE_SHA"
gh workflow run release.yml --repo "$REPO" --ref v1.6.0 \
  -f expected_sha="$RELEASE_SHA" -f candidate_run_id="$CANDIDATE_RUN_ID" \
  -f publish_testpypi=false -f publish_pypi=false
```

Verify the tag, SHA, candidate identity, digests, inventories, and rehearsal.
Then publish the same candidate:

```bash
gh workflow run release.yml --repo "$REPO" --ref v1.6.0 \
  -f expected_sha="$RELEASE_SHA" -f candidate_run_id="$CANDIDATE_RUN_ID" \
  -f publish_testpypi=false -f publish_pypi=true
```

Keep release-event publish variables explicitly false and bind
`CODE_MOWER_CANDIDATE_RUN_ID` to the accepted run. Create the GitHub Release
from the retained wheel and sdist, immutable release notes, and sanitized public
evidence. Require the release-event run to consume the same candidate and skip
both publication jobs. Compare downloaded GitHub assets byte-for-byte.

## 7. Independent canonical reinstall and closeout

Download `code-mower==1.6.0` from canonical PyPI without cache or extra indexes.
Verify wheel and sdist digests, `code-mower --version`, installed metadata,
documentation inventory, safe init, Board status and service guidance,
Graphify, basic Slack, and capability-gated telemetry. Exercise rollback only
in disposable state.

Record the release SHA, candidate, canary, soak, independent adoption,
publication, release-event, artifact digest, and canonical reinstall evidence
on #1105 and the GitHub Release. Close #1105 and #1066 only after the public
package and release evidence agree.
