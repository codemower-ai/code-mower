# Code Mower v1.0.7 Release Notes

Code Mower v1.0.7 hardens the multi-provider release-qualification loop using
findings from its first applied v1.0.6 campaign. It preserves supervised-pilot
gate semantics, the Python 3.12+ requirement, and the metadata-only privacy
boundary.

Install the pinned package:

```bash
export CODE_MOWER_PYTHON="$(command -v python3.12)"
pipx install --python "$CODE_MOWER_PYTHON" code-mower==1.0.7
code-mower --version
```

Hosted agents and CI boxes can use:

```bash
uv tool install --python 3.12 code-mower==1.0.7
code-mower --version
```

## What Is New

- Isolated Codex campaigns preserve macOS keychain access while keeping Codex
  configuration and state in an isolated `CODEX_HOME` (#696, PR #697).
- Board shows checkpointed local adapters as running only inside their effective
  timeout, then gives coherent stale retry guidance at card, campaign, and
  top-level scope without rewriting campaign state (#698, PR #702).
- Adoption doctor probes isolated local-provider auth when a safe bounded probe
  is available, distinguishes known logged-out from indeterminate states, and
  does not expose auth output or local paths (#699, PR #704).
- Adoption results enforce bounded timestamps, a stable step taxonomy,
  explicit overhead, coherent timing totals, and pass/owner-action consistency
  for comparable evidence (#700, PR #703).
- Hosted Cursor/Grok Bot and Devin campaigns expose transport verification and
  response deadlines, then require explicit manual fallback or retry instead of
  duplicating paid work when a provider stays silent (#701, PR #705).

## Run A Campaign

For one provider environment, produce the closed local result directly:

```bash
code-mower release qualify \
  --release-tag v1.0.7 \
  --package-spec code-mower==1.0.7 \
  --output adoption-result.json \
  --execute
```

The command emits a `code_mower.adoptionResult.v1` artifact. Campaign upload
converts completed results into additive, metadata-only `adoption_run` events;
it does not upload source, diffs, prompts, transcripts, or raw provider output.

Check readiness and preview first:

```bash
code-mower doctor --adoption --repo OWNER/REPO
code-mower release campaign \
  --release-tag v1.0.7 \
  --package-spec code-mower==1.0.7 \
  --repo-slug OWNER/REPO
```

Apply only after reviewing the preview. Hosted providers also require the issue
that receives their trusted dispatch/result comments.

```bash
code-mower release campaign dispatch \
  --release-tag v1.0.7 \
  --apply \
  --repo-slug OWNER/REPO \
  --issue ISSUE_NUMBER
code-mower release campaign watch --release-tag v1.0.7
```

Preview the cloud bundle, then upload it explicitly:

```bash
code-mower release campaign upload --release-tag v1.0.7 --json
code-mower release campaign upload --release-tag v1.0.7 --yes --json
```

## Recommended Update

For an existing pipx install:

```bash
export CODE_MOWER_PYTHON="$(command -v python3.12)"
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" code-mower==1.0.7
code-mower --version
code-mower board list
```

For hosted agents using uv:

```bash
uv tool install --python 3.12 --reinstall --refresh-package code-mower code-mower==1.0.7
code-mower --version
```

Restart any Board that still serves an older package. Existing repositories
should continue to review `migration setup-drift` output in a pull request
before applying generated changes.

## Quality And Privacy Proof

The hardening PRs passed package CI on Python 3.12, 3.13, and 3.14, Gitar, and
exact-head peer audits. The audits found and corrected timestamp, readiness,
timeout, retry, campaign-summary, validation, and auth-isolation defects before
merge.

Campaign upload remains opt-in and metadata-only. It excludes source, raw diffs,
prompts, transcripts, issue body text, raw stdout/stderr, authentication output,
local paths, and secrets.
