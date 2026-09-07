# Code Mower v1.0.11 Release Notes

Code Mower v1.0.11 is a release-qualification patch. It moves canonical hosted
Devin campaigns onto the bounded, pollable Devin Sessions API v3 transport, and
it lets macOS Claude release qualification cold-install an exact PyPI release
again without weakening the maintained strict sandbox. It preserves the
supervised-pilot operating model, Python 3.12+ requirement, and metadata-only
privacy boundary.

Install the pinned package:

```bash
export CODE_MOWER_PYTHON="$(command -v python3.12)"
pipx install --python "$CODE_MOWER_PYTHON" code-mower==1.0.11
code-mower --version
```

Hosted agents and CI boxes can use:

```bash
uv tool install --python 3.12 code-mower==1.0.11
code-mower --version
```

## What Is New

- Canonical hosted Devin release campaigns dispatch through the Devin Sessions
  API v3 instead of a GitHub issue comment. The API call is the execution
  trigger: it needs a service-user `DEVIN_API_KEY` with the `UseDevinSessions`
  and `ViewOrgSessions` organization permissions, the opaque `org-*`
  `DEVIN_ORG_ID`, and the exact `OWNER/REPO` target acknowledged in
  `CODE_MOWER_DEVIN_REPOSITORIES`. Matching is against the full slug, so a
  same-name personal fork does not satisfy an organization repository target.
  Credentials are read but never printed or persisted (#770, PR #771).
- A hosted Devin dispatch is bounded and pollable rather than a wait on a bot
  comment. A resume (`--resume` or `watch`) polls the stored session id and
  never creates another paid session; `--retry-provider devin --apply` creates a
  new session only after the prior session is known terminal or its one-hour
  response deadline has expired, so an active or owner-blocked session is polled
  but never duplicated. Accepted retries preserve bounded attempt history
  (#770, PR #771).
- `--issue` is now optional audit evidence for hosted Devin. When supplied, Code
  Mower records the existing campaign marker on the issue; it does not post
  `@devin run` and does not depend on a bot reply. Cursor Cloud Agent keeps the
  issue-comment transport and its own five-check profile (#770, PR #771).
- An informational Devin attempt that stays active but cannot be completed can
  be closed out with `release campaign dispose`, which records a terminal,
  metadata-only disposition without inventing a result or contacting Devin again
  (#770, PR #771).

## What Is Fixed

- macOS Claude release campaigns can cold-install an exact PyPI release again
  inside the maintained strict sandbox. Claude Code's macOS sandbox denies the
  Security.framework call pip's default platform trust store makes
  (`OSStatus -26276`), so a macOS Claude qualification prompt now runs pip with
  its TLS-verifying legacy (certifi) certificate path and no inherited pip
  configuration. Certificate verification stays enabled -- no trusted-host
  option, no disabled TLS, and no unsandboxed command -- and the sandbox, domain
  allowlist, home denials, and disabled escape hatch are unchanged. Linux Claude
  runs and every other provider keep pip's default certificate path. A
  certificate failure that survives this path always classifies as a `network`
  package-install failure, never `sandbox_permission` and never `package_index`;
  a non-certificate index response such as a 404 still classifies as
  `package_index` (#769, PR #772). See
  [macOS Claude sandbox certificate path](release-qualification.md#macos-claude-sandbox-certificate-path).

## Provider Posture

This release does not change reviewer authority. Codex audit and Claude audit
remain the established reviewer lanes with merge authority; Gitar stays
informational corroboration. Devin remains an opt-in paid lane
(`enabled_by_default: false`, `trigger_policy: manual`, `spend_policy: paid`)
and an informational reviewer unless repository-specific evidence promotes it
under the [lane promotion policy](lane-promotion-policy.md). A successful hosted
dispatch is transport evidence, not builder-quality or reviewer-promotion
evidence.

## Run A Qualification Campaign

Qualify one provider environment directly when a full campaign is unnecessary:

```bash
code-mower release qualify \
  --release-tag v1.0.11 \
  --package-spec code-mower==1.0.11 \
  --output adoption-result.json \
  --execute
```

Preview a campaign with established local providers required and experimental or
hosted providers informational:

```bash
code-mower release campaign create \
  --release-tag v1.0.11 \
  --package-spec code-mower==1.0.11 \
  --providers claude,codex,antigravity,muse,cursor_cloud_agent,devin \
  --required-providers claude,codex \
  --repo-slug OWNER/REPO
```

Apply only after the preview and adoption doctor are clean:

```bash
code-mower doctor --adoption --repo OWNER/REPO
code-mower release campaign dispatch \
  --release-tag v1.0.11 \
  --required-providers claude,codex \
  --apply \
  --repo-slug OWNER/REPO \
  --issue ISSUE_NUMBER
code-mower release campaign watch --release-tag v1.0.11
```

A hosted Devin attempt needs `--repo-slug OWNER/REPO` and its API environment;
`--issue` is optional for that lane. See
[Devin Setup](release-qualification.md#devin-setup).

Preview the closed cloud bundle, then upload it explicitly:

```bash
code-mower release campaign upload --release-tag v1.0.11 --json
code-mower release campaign upload --release-tag v1.0.11 --yes --json
```

Qualification writes the closed `code_mower.adoptionResult.v1` artifact.
Campaign upload converts terminal results into additive `adoption_run` events.
It does not upload source, raw diffs, prompts, transcripts, issue body text, raw
provider output, authentication output, local paths, or secrets.

## Recommended Update

For an existing pipx install:

```bash
export CODE_MOWER_PYTHON="$(command -v python3.12)"
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" code-mower==1.0.11
code-mower --version
code-mower board list
```

For hosted agents using uv:

```bash
uv tool install --python 3.12 --reinstall --refresh-package code-mower code-mower==1.0.11
code-mower --version
```

`--refresh-package` takes a package name as its value, so the first
`code-mower` is the cache-refresh target and `code-mower==1.0.11` is the single
package argument.

Restart a Board that still serves an older package. For existing repositories,
review `migration setup-drift` output in a pull request before applying any
generated changes.

## Quality And Privacy Proof

The implementation PRs passed package CI on Python 3.12, 3.13, and 3.14 and
exact-head peer audits with the author lane excluded. The audits found and
corrected duplicate-session, repository-scope, and package-install failure
classification defects before merge.

Cloud sharing remains opt-in and dry-run first. The default release campaign
works locally without a CodeMower.com account.
