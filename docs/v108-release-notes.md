# Code Mower v1.0.8 Release Notes

Code Mower v1.0.8 closes the remaining trust gaps found while dogfooding the
multi-provider release-qualification campaign. It preserves supervised-pilot
gate semantics, the Python 3.12+ requirement, and the metadata-only privacy
boundary.

Install the pinned package:

```bash
export CODE_MOWER_PYTHON="$(command -v python3.12)"
pipx install --python "$CODE_MOWER_PYTHON" code-mower==1.0.8
code-mower --version
```

Hosted agents and CI boxes can use:

```bash
uv tool install --python 3.12 code-mower==1.0.8
code-mower --version
```

## What Is New

- Codex and Claude audit verdict transport preserves valid metadata-only P3
  findings with line 0 while still requiring actionable source lines for
  blocking P0/P1/P2 findings (#681, PR #715).
- Campaign retries retain ordered attempt chronology and terminal failure
  evidence without persisting provider output (#711, PR #716).
- Local adapters prove the requested Python 3.12+ runtime, truthful
  authentication readiness, and the expected closed result artifact (#710,
  PR #719).
- Campaign status, watch, upload, and Board find the same campaign after a
  checkout or worktree changes through a metadata-only user index (#712,
  PR #720).
- Release qualification can install the exact candidate from a closed
  TestPyPI source before production publication (#713, PR #723).
- Cursor Cloud Agent is the builder identity, distinct from Cursor BugBot and
  Grok Bot reviewer identities (#717, PR #722).
- Antigravity campaigns use explicit project identity and isolated execution
  rather than ambient IDE state (#721, PR #724).
- Hosted Cursor Cloud Agent and Devin profiles require verified dispatch and
  result transport through five explicit readiness checks (#718, PR #725).

## Run A Campaign

Qualify one provider environment against the exact release:

```bash
code-mower release qualify \
  --release-tag v1.0.8 \
  --package-spec code-mower==1.0.8 \
  --output adoption-result.json \
  --execute
```

Check the whole campaign posture and preview before applying:

```bash
code-mower doctor --adoption --repo OWNER/REPO
code-mower release campaign \
  --release-tag v1.0.8 \
  --package-spec code-mower==1.0.8 \
  --repo-slug OWNER/REPO
```

For a TestPyPI candidate, add `--package-source testpypi`. Applied hosted
providers also require the issue that receives their trusted dispatch and
result comments.

```bash
code-mower release campaign dispatch \
  --release-tag v1.0.8 \
  --apply \
  --repo-slug OWNER/REPO \
  --issue ISSUE_NUMBER
code-mower release campaign watch --release-tag v1.0.8
```

Preview the closed cloud bundle, then upload it explicitly:

```bash
code-mower release campaign upload --release-tag v1.0.8 --json
code-mower release campaign upload --release-tag v1.0.8 --yes --json
```

Qualification writes the closed `code_mower.adoptionResult.v1` artifact.
Campaign upload converts completed results into additive, metadata-only
`adoption_run` events. It does not upload source, raw diffs, prompts, transcripts,
issue body text, raw provider output, authentication output, local paths, or
secrets.

## Recommended Update

For an existing pipx install:

```bash
export CODE_MOWER_PYTHON="$(command -v python3.12)"
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" code-mower==1.0.8
code-mower --version
code-mower board list
```

For hosted agents using uv:

```bash
uv tool install --python 3.12 --reinstall --refresh-package code-mower code-mower==1.0.8
code-mower --version
```

Restart a Board that still serves an older package. For existing repositories,
review `migration setup-drift` output in a pull request before applying any
generated changes.

## Quality And Privacy Proof

The implementation PRs passed package CI on Python 3.12, 3.13, and 3.14,
Gitar, and exact-head peer audits. Four exact-head audit rounds on the hosted
transport work found and corrected readiness, remediation, and timeout defects
before merge.

Campaign upload remains opt-in and metadata-only. It excludes source, raw
diffs, prompts, transcripts, issue body text, raw stdout/stderr,
authentication output, local paths, and secrets.
