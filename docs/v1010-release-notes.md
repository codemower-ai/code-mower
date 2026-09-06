# Code Mower v1.0.10 Release Notes

Code Mower v1.0.10 makes local builder lanes deliverable and recoverable,
simplifies the first run, and publishes the 20-sample provider calibration
scorecard. It preserves the supervised-pilot operating model, Python 3.12+
requirement, and metadata-only privacy boundary.

Install the pinned package:

```bash
export CODE_MOWER_PYTHON="$(command -v python3.12)"
pipx install --python "$CODE_MOWER_PYTHON" code-mower==1.0.10
code-mower --version
```

Hosted agents and CI boxes can use:

```bash
uv tool install --python 3.12 code-mower==1.0.10
code-mower --version
```

## What Is New

- `code-mower lane-delivery` gives the local builder runner a provider-neutral
  delivery and recovery contract. Delivery is classified from a validated
  issue/PR/head transition or a bounded `no_change`/`owner_action` outcome the
  runner validates itself. Providers run in their own process group that is
  terminated and reaped on timeout, interruption, and overflow, and an explicit
  orchestrator handoff is the only way to write a pull request branch owned by
  another lane (#751, PR #754).
- A supervised provider's own exit ends the run even when a background
  descendant still holds its stdout open, so a provider that finished is no
  longer reported as a timeout (#751, PR #754).
- First-run defaults and guidance are simpler. `init --easy` and `next-steps`
  present one recommended path with executable commands instead of a wide
  provider menu (#755, PR #757).
- `code-mower participants` and host-led session briefs make reviewer and
  builder selection explicit for a session instead of implied by configuration
  (#756, PR #758).
- The local Devin CLI lane is now first class: a provider contract, a maintained
  release-qualification adapter, a supervised builder lane with self-hosted
  runner support, and an informational reviewer lane (#746, PRs #747, #748,
  #749, #750).
- [Provider Calibration Scorecard](provider-calibration-scorecard.md) publishes
  the 20-sample builder corpus: five bounded real issues each for Cursor, Muse,
  Antigravity, and Devin, with comparable aggregates, limitations, and role
  recommendations (#765, PR #766).

## What Is Fixed

- Package-install rehearsal omits empty optional pip flags instead of passing an
  empty argument to `pip install` (#740, PR #759).
- Bounded hosted campaign result rejection reasons are surfaced, so a rejected
  result is distinguishable from a missing one (#741, PR #760).
- The Codex campaign adapter compiles an API-compatible structured-output
  schema, which the API previously rejected before generation (#739, PR #761).
- The Mac lane runner accepts empty optional provider extra flags under `set -u`
  instead of aborting the run (#753, PR #762).
- Mac lane runner builder prompts require issue-linked pull request delivery, so
  a run is judged from the pull request's GitHub closing-issue reference rather
  than a bare mention (#763, PR #764).

## Provider Posture

The scorecard does not change reviewer authority. Codex audit and Claude audit
remain the established reviewer lanes with merge authority; Gitar stays
informational corroboration. Cursor, Muse, Antigravity, and Devin are supervised
builders and informational reviewers unless repository-specific evidence
promotes them under the [lane promotion policy](lane-promotion-policy.md).
Builder delivery success never grants reviewer merge authority.

## Run A Qualification Campaign

Qualify one provider environment directly when a full campaign is unnecessary:

```bash
code-mower release qualify \
  --release-tag v1.0.10 \
  --package-spec code-mower==1.0.10 \
  --output adoption-result.json \
  --execute
```

Preview a campaign with established local providers required and experimental or
hosted providers informational:

```bash
code-mower release campaign create \
  --release-tag v1.0.10 \
  --package-spec code-mower==1.0.10 \
  --providers claude,codex,antigravity,muse,cursor_cloud_agent,devin \
  --required-providers claude,codex \
  --repo-slug OWNER/REPO
```

Apply only after the preview and adoption doctor are clean:

```bash
code-mower doctor --adoption --repo OWNER/REPO
code-mower release campaign dispatch \
  --release-tag v1.0.10 \
  --required-providers claude,codex \
  --apply \
  --repo-slug OWNER/REPO \
  --issue ISSUE_NUMBER
code-mower release campaign watch --release-tag v1.0.10
```

Preview the closed cloud bundle, then upload it explicitly:

```bash
code-mower release campaign upload --release-tag v1.0.10 --json
code-mower release campaign upload --release-tag v1.0.10 --yes --json
```

Qualification writes the closed `code_mower.adoptionResult.v1` artifact.
Campaign upload converts terminal results into additive `adoption_run` events.
It does not upload source, raw diffs, prompts, transcripts, issue body text, raw
provider output, authentication output, local paths, or secrets.

## Recommended Update

For an existing pipx install:

```bash
export CODE_MOWER_PYTHON="$(command -v python3.12)"
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" code-mower==1.0.10
code-mower --version
code-mower board list
```

For hosted agents using uv:

```bash
uv tool install --python 3.12 --reinstall --refresh-package code-mower code-mower==1.0.10
code-mower --version
```

`--refresh-package` takes a package name as its value, so the first
`code-mower` is the cache-refresh target and `code-mower==1.0.10` is the single
package argument.

Restart a Board that still serves an older package. For existing repositories,
review `migration setup-drift` output in a pull request before applying any
generated changes.

## Quality And Privacy Proof

The implementation PRs passed package CI on Python 3.12, 3.13, and 3.14 and
exact-head Codex and Claude audits with the author lane excluded. The audits
found and corrected delivery-classification, process-cleanup, handoff-authority,
reviewer-selection, and campaign-diagnostic defects before merge.

Cloud sharing remains opt-in and dry-run first. The default release campaign
works locally without a CodeMower.com account.
