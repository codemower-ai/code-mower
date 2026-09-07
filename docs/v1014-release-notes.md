# Code Mower v1.0.14 Release Notes

Code Mower v1.0.14 is a corrective release-qualification patch. Hosted Devin
polling now accepts a valid closed structured result even while the session
still reports an in-progress status, so a finished informational Devin attempt
is no longer recorded as owner-blocked. It preserves the supervised-pilot
operating model, Python 3.12+ requirement, provider posture, and metadata-only
privacy boundary.

Install the pinned package:

```bash
export CODE_MOWER_PYTHON="$(command -v python3.12)"
pipx install --python "$CODE_MOWER_PYTHON" code-mower==1.0.14
code-mower --version
```

Hosted agents and CI boxes can use:

```bash
uv tool install --python 3.12 code-mower==1.0.14
code-mower --version
```

## Why This Patch Exists

The hosted Devin lane is informational, but its result was being discarded for
a reason unrelated to the release. Devin can return the requested structured
output while the session still reports `running` with status detail
`waiting_for_user`. The previous polling order checked that owner-input detail
before it looked at the payload, so a session that had already produced a valid
`code_mower.adoptionResult.v1` result was recorded as owner-blocked and the
campaign asked the owner to answer a session that was effectively done.
Because a published version is immutable, the fix ships as v1.0.14 rather than
as a correction to v1.0.13.

## What Is Fixed

- Hosted Devin polling now evaluates snapshots in explicit precedence order:
  terminal API failure statuses first, then `waiting_for_approval`, then a
  valid structured result, then `waiting_for_user` owner action, and only then
  the terminal-without-result rejection. A completed Devin session that
  returned its structured result while still reporting `running` /
  `waiting_for_user` is therefore accepted as complete (PR #783).
- [Release Qualification](release-qualification.md) records this precedence, so
  an operator watching a campaign can tell an accepted late result apart from a
  session that genuinely needs owner input.

## What Is Unchanged

Explicit terminal failures still win over any structured output. When the API
reports `error` or `suspended`, or a status detail of `error`,
`usage_limit_exceeded`, `out_of_credits`, `out_of_quota`,
`no_quota_allocation`, `payment_declined`, `org_usage_limit_exceeded`, or
`total_session_limit_exceeded`, the attempt is still `devin_session_failed`
and no structured output is read from it.

Approval gates still win over any structured output. A session whose status
detail is `waiting_for_approval` is still reported as
`devin_waiting_for_owner` so the owner decides before Code Mower treats
anything as finished. Only ordinary `waiting_for_user` now loses to a result
that is already present.

Result acceptance is not loosened. A structured payload is still bound to this
campaign's release tag, package identity, qualification context, and the closed
`code_mower.adoptionResult.v1` schema; a payload that does not match is still
recorded as `hosted_result_rejected` rather than accepted. A terminal session
that produced no structured output at all is still rejected the same way.

The rest of the hosted Devin transport is intact: bounded polling with the
one-hour response deadline owned by campaign watch, `hosted_response_timeout`
when nothing arrives in time, retries only from terminal sessions with bounded
attempt history preserved, and polling outages that leave the paid attempt
running instead of duplicating it.

Devin remains an opt-in paid lane (`enabled_by_default: false`,
`trigger_policy: manual`, `spend_policy: paid`). This release changes no
reviewer authority, no supervised-pilot gate semantics, no provider posture,
and no privacy boundary.

## Provider Posture

Codex audit and Claude audit remain the established reviewer lanes with merge
authority; Gitar stays informational corroboration. Hosted Devin remains an
informational reviewer unless repository-specific evidence promotes it under
the [lane promotion policy](lane-promotion-policy.md). Release campaigns keep
Claude and Codex required, with Antigravity, Muse, Cursor Cloud Agent, hosted
Devin, and Devin CLI informational.

## Run A Qualification Campaign

Qualify one provider environment directly when a full campaign is unnecessary:

```bash
code-mower release qualify \
  --release-tag v1.0.14 \
  --package-spec code-mower==1.0.14 \
  --output adoption-result.json \
  --execute
```

Preview a campaign with established local providers required and experimental or
hosted providers informational:

```bash
code-mower release campaign create \
  --release-tag v1.0.14 \
  --package-spec code-mower==1.0.14 \
  --providers claude,codex,antigravity,muse,cursor_cloud_agent,devin \
  --required-providers claude,codex \
  --repo-slug OWNER/REPO
```

Apply only after the preview and adoption doctor are clean:

```bash
code-mower doctor --adoption --repo OWNER/REPO
code-mower release campaign dispatch \
  --release-tag v1.0.14 \
  --required-providers claude,codex \
  --apply \
  --repo-slug OWNER/REPO \
  --issue ISSUE_NUMBER
code-mower release campaign watch --release-tag v1.0.14
```

A hosted Devin attempt needs `--repo-slug OWNER/REPO` and its API environment;
`--issue` is optional for that lane. See
[Devin Setup](release-qualification.md#devin-setup).

Preview the closed cloud bundle, then upload it explicitly:

```bash
code-mower release campaign upload --release-tag v1.0.14 --json
code-mower release campaign upload --release-tag v1.0.14 --yes --json
```

Qualification writes the closed `code_mower.adoptionResult.v1` artifact.
Campaign upload converts terminal results into additive `adoption_run` events.
It does not upload source, raw diffs, prompts, transcripts, issue body text, raw
provider output, authentication output, local paths, or secrets.

## Recommended Update

For an existing pipx install:

```bash
export CODE_MOWER_PYTHON="$(command -v python3.12)"
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" code-mower==1.0.14
code-mower --version
code-mower board list
```

For hosted agents using uv:

```bash
uv tool install --python 3.12 --reinstall --refresh-package code-mower code-mower==1.0.14
code-mower --version
```

`--refresh-package` takes a package name as its value, so the first
`code-mower` is the cache-refresh target and `code-mower==1.0.14` is the single
package argument.

Restart a Board that still serves an older package. For existing repositories,
review `migration setup-drift` output in a pull request before applying any
generated changes.

## Quality And Privacy Proof

The implementation PR passed package CI on Python 3.12, 3.13, and 3.14 and an
exact-head peer audit with the author lane excluded. The behavior was found by
a real hosted Devin campaign attempt rather than by review alone.

Cloud sharing remains opt-in and dry-run first. The default release campaign
works locally without a CodeMower.com account.
