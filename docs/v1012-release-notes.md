# Code Mower v1.0.12 Release Notes

Code Mower v1.0.12 is a corrective release-qualification patch. It completes the
macOS Claude fix that v1.0.11 started: the maintained provider qualification
prompt no longer contradicts the disposable workspace Code Mower already
creates, and it states the result timing contract explicitly. It preserves the
supervised-pilot operating model, Python 3.12+ requirement, provider posture,
and metadata-only privacy boundary.

Install the pinned package:

```bash
export CODE_MOWER_PYTHON="$(command -v python3.12)"
pipx install --python "$CODE_MOWER_PYTHON" code-mower==1.0.12
code-mower --version
```

Hosted agents and CI boxes can use:

```bash
uv tool install --python 3.12 code-mower==1.0.12
code-mower --version
```

## Why This Patch Exists

v1.0.11 was published before the final real macOS Claude campaign proof ran.
That proof exposed the remaining prompt ambiguity described below. Because a
published version is immutable, the fix ships as v1.0.12 rather than as a
correction to v1.0.11.

## What Is Fixed

- The release-qualification prompt keeps the agent in the disposable workspace
  Code Mower already created. The old wording told the agent to qualify "in a
  disposable environment you create" and to do all work "inside a fresh
  temporary directory", which conflicts with the maintained macOS Claude strict
  sandbox: the allowed workspace is the one Code Mower prepared, so an agent
  that followed the prompt literally could push its work outside the sandbox's
  allowed workspace and fail the run for a reason unrelated to the release. The
  prompt now says the workspace already exists and that the agent must not
  create or change into another temporary directory (#769, PR #775).
- The qualification prompt states the `code_mower.adoptionResult.v1` timing
  contract that a result's total `elapsed_seconds` is the sum of its step
  `elapsed_seconds` values, within one second for rounding, so a provider does
  not report a total that contradicts its own steps (#769, PR #775).

## What Is Unchanged

Every other qualification instruction is intact. The agent still must not read
or modify an existing checkout, home directory, credential file, or
secret-bearing environment variable, and must not print secrets, tokens, file
paths, commands it ran, or raw logs in its final answer.

The macOS Claude sandbox posture from v1.0.11 is unchanged: `sandbox.enabled`,
`failIfUnavailable`, the disabled unsandboxed escape hatch
(`allowUnsandboxedCommands: false`), the home read/write denials, the closed
package-index domain allowlist, and pip's TLS-verifying legacy (certifi)
certificate path with no inherited pip configuration. See
[macOS Claude sandbox certificate path](release-qualification.md#macos-claude-sandbox-certificate-path).

The closed `code_mower.adoptionResult.v1` schema, outcome derivation from step
statuses, the supervised-pilot gate semantics, and the metadata-only upload
boundary are unchanged. Linux Claude runs and every other provider keep their
existing prompt and certificate behavior.

## Provider Posture

This release does not change reviewer authority. Codex audit and Claude audit
remain the established reviewer lanes with merge authority; Gitar stays
informational corroboration. Devin remains an opt-in paid lane
(`enabled_by_default: false`, `trigger_policy: manual`, `spend_policy: paid`)
and an informational reviewer unless repository-specific evidence promotes it
under the [lane promotion policy](lane-promotion-policy.md).

## Run A Qualification Campaign

Qualify one provider environment directly when a full campaign is unnecessary:

```bash
code-mower release qualify \
  --release-tag v1.0.12 \
  --package-spec code-mower==1.0.12 \
  --output adoption-result.json \
  --execute
```

Preview a campaign with established local providers required and experimental or
hosted providers informational:

```bash
code-mower release campaign create \
  --release-tag v1.0.12 \
  --package-spec code-mower==1.0.12 \
  --providers claude,codex,antigravity,muse,cursor_cloud_agent,devin \
  --required-providers claude,codex \
  --repo-slug OWNER/REPO
```

Apply only after the preview and adoption doctor are clean:

```bash
code-mower doctor --adoption --repo OWNER/REPO
code-mower release campaign dispatch \
  --release-tag v1.0.12 \
  --required-providers claude,codex \
  --apply \
  --repo-slug OWNER/REPO \
  --issue ISSUE_NUMBER
code-mower release campaign watch --release-tag v1.0.12
```

A hosted Devin attempt needs `--repo-slug OWNER/REPO` and its API environment;
`--issue` is optional for that lane. See
[Devin Setup](release-qualification.md#devin-setup).

Preview the closed cloud bundle, then upload it explicitly:

```bash
code-mower release campaign upload --release-tag v1.0.12 --json
code-mower release campaign upload --release-tag v1.0.12 --yes --json
```

Qualification writes the closed `code_mower.adoptionResult.v1` artifact.
Campaign upload converts terminal results into additive `adoption_run` events.
It does not upload source, raw diffs, prompts, transcripts, issue body text, raw
provider output, authentication output, local paths, or secrets.

## Recommended Update

For an existing pipx install:

```bash
export CODE_MOWER_PYTHON="$(command -v python3.12)"
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" code-mower==1.0.12
code-mower --version
code-mower board list
```

For hosted agents using uv:

```bash
uv tool install --python 3.12 --reinstall --refresh-package code-mower code-mower==1.0.12
code-mower --version
```

`--refresh-package` takes a package name as its value, so the first
`code-mower` is the cache-refresh target and `code-mower==1.0.12` is the single
package argument.

Restart a Board that still serves an older package. For existing repositories,
review `migration setup-drift` output in a pull request before applying any
generated changes.

## Quality And Privacy Proof

The implementation PR passed package CI on Python 3.12, 3.13, and 3.14 and an
exact-head peer audit with the author lane excluded. The change was found by a
real macOS Claude release campaign rather than by review alone.

Cloud sharing remains opt-in and dry-run first. The default release campaign
works locally without a CodeMower.com account.
