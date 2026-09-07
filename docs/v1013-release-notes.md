# Code Mower v1.0.13 Release Notes

Code Mower v1.0.13 is a corrective release-qualification patch. Automatic
campaign runtime selection is now sandbox-safe: when `CODE_MOWER_PYTHON` is not
set, Code Mower prefers a supported versioned Python on `PATH` outside the user
home before falling back to its own running interpreter. It preserves the
supervised-pilot operating model, Python 3.12+ requirement, provider posture,
and metadata-only privacy boundary.

Install the pinned package:

```bash
export CODE_MOWER_PYTHON="$(command -v python3.12)"
pipx install --python "$CODE_MOWER_PYTHON" code-mower==1.0.13
code-mower --version
```

Hosted agents and CI boxes can use:

```bash
uv tool install --python 3.12 code-mower==1.0.13
code-mower --version
```

## Why This Patch Exists

v1.0.12 was published before its real campaign exposed the remaining runtime
selection problem. On a Mac where Code Mower itself is installed with pipx, the
campaign runner's automatic resolution could pick the pipx-contained
interpreter under the user home. Claude's maintained strict macOS sandbox
denies reads under that home tree, so the qualification run failed for a reason
unrelated to the release even though a readable Homebrew Python was on `PATH`.
Because a published version is immutable, the fix ships as v1.0.13 rather than
as a correction to v1.0.12.

## What Is Fixed

- Automatic Python 3.12+ runtime resolution now prefers versioned `PATH`
  runtimes outside the user home before falling back to Code Mower's running
  interpreter or another home-contained runtime. A pipx- or uv-installed CLI
  therefore hands the provider an interpreter that remains readable inside
  Claude's strict macOS sandbox, and a maintained Claude campaign no longer
  needs `CODE_MOWER_PYTHON` set by hand (#778, PR #779).
- Hosted environments that have no runtime outside the home are unchanged: a
  home-contained interpreter is still a supported fallback, so the dispatch
  keeps working where the running interpreter is the only supported Python
  (#778, PR #779).

## What Is Unchanged

An explicit `CODE_MOWER_PYTHON` remains authoritative. When it is set, Code
Mower resolves exactly that interpreter and does not substitute a different
one.

The rest of the deterministic runtime contract is intact. The campaign runner
still passes exact `--python-bin` and `--target-runtime` arguments so providers
cannot pick an ambient `python3`, still fails closed with
`python_runtime_unavailable` and actionable remediation when no supported
runtime exists, and result validators still enforce
`runtime_class >= python_3.12`.

The macOS Claude sandbox posture is unchanged: `sandbox.enabled`,
`failIfUnavailable`, the disabled unsandboxed escape hatch
(`allowUnsandboxedCommands: false`), the home read/write denials, the closed
package-index domain allowlist, and pip's TLS-verifying legacy (certifi)
certificate path with no inherited pip configuration. See
[macOS Claude sandbox certificate path](release-qualification.md#macos-claude-sandbox-certificate-path).

The qualification prompt from v1.0.12 is unchanged, including the disposable
workspace wording and the `code_mower.adoptionResult.v1` timing contract. The
agent still must not read or modify an existing checkout, home directory,
credential file, or secret-bearing environment variable, and must not print
secrets, tokens, file paths, commands it ran, or raw logs in its final answer.

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
  --release-tag v1.0.13 \
  --package-spec code-mower==1.0.13 \
  --output adoption-result.json \
  --execute
```

Preview a campaign with established local providers required and experimental or
hosted providers informational:

```bash
code-mower release campaign create \
  --release-tag v1.0.13 \
  --package-spec code-mower==1.0.13 \
  --providers claude,codex,antigravity,muse,cursor_cloud_agent,devin \
  --required-providers claude,codex \
  --repo-slug OWNER/REPO
```

Apply only after the preview and adoption doctor are clean:

```bash
code-mower doctor --adoption --repo OWNER/REPO
code-mower release campaign dispatch \
  --release-tag v1.0.13 \
  --required-providers claude,codex \
  --apply \
  --repo-slug OWNER/REPO \
  --issue ISSUE_NUMBER
code-mower release campaign watch --release-tag v1.0.13
```

A hosted Devin attempt needs `--repo-slug OWNER/REPO` and its API environment;
`--issue` is optional for that lane. See
[Devin Setup](release-qualification.md#devin-setup).

Preview the closed cloud bundle, then upload it explicitly:

```bash
code-mower release campaign upload --release-tag v1.0.13 --json
code-mower release campaign upload --release-tag v1.0.13 --yes --json
```

Qualification writes the closed `code_mower.adoptionResult.v1` artifact.
Campaign upload converts terminal results into additive `adoption_run` events.
It does not upload source, raw diffs, prompts, transcripts, issue body text, raw
provider output, authentication output, local paths, or secrets.

## Recommended Update

For an existing pipx install:

```bash
export CODE_MOWER_PYTHON="$(command -v python3.12)"
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" code-mower==1.0.13
code-mower --version
code-mower board list
```

For hosted agents using uv:

```bash
uv tool install --python 3.12 --reinstall --refresh-package code-mower code-mower==1.0.13
code-mower --version
```

`--refresh-package` takes a package name as its value, so the first
`code-mower` is the cache-refresh target and `code-mower==1.0.13` is the single
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
