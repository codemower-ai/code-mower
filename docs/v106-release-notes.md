# Code Mower v1.0.6 Release Notes

Code Mower v1.0.6 makes multi-provider release qualification operable from one
local campaign. It keeps the v1.0.5 supervised-pilot gate semantics, Python
3.12+ requirement, and metadata-only privacy boundary.

Install the pinned package:

```bash
export CODE_MOWER_PYTHON="$(command -v python3.12)"
pipx install --python "$CODE_MOWER_PYTHON" code-mower==1.0.6
code-mower --version
```

Hosted agents and CI boxes can use:

```bash
uv tool install --python 3.12 code-mower==1.0.6
code-mower --version
```

## What Is New

- Codex, Claude, Antigravity, and Muse have maintained local qualification
  adapters with bounded execution and closed result validation.
- Cursor/Grok Bot and Devin have hosted dispatch profiles with persisted
  idempotency markers, trusted result polling, and explicit retry behavior.
- `release campaign watch` follows all providers until completion, timeout, or
  a real owner action and remains useful during temporary provider outages.
- `release campaign upload` renders the exact metadata-only bundle by default;
  `--yes` is required for network upload.
- `doctor --adoption` validates the six campaign providers plus storage, cloud
  profile, Board visibility, and the command to preview a campaign.
- `code-mower release qualify` still emits the closed
  `code_mower.adoptionResult.v1` artifact; campaign upload carries those results
  as additive `adoption_run` metadata.

## Run A Campaign

Check readiness and preview first:

```bash
code-mower doctor --adoption --repo OWNER/REPO
code-mower release campaign \
  --release-tag v1.0.6 \
  --package-spec code-mower==1.0.6 \
  --repo-slug OWNER/REPO
```

Apply only after reviewing the preview. Hosted providers also require the issue
that receives their trusted dispatch/result comments.

```bash
code-mower release campaign dispatch \
  --release-tag v1.0.6 \
  --apply \
  --repo-slug OWNER/REPO \
  --issue ISSUE_NUMBER
code-mower release campaign watch --release-tag v1.0.6
```

Preview the cloud bundle, then upload it explicitly:

```bash
code-mower release campaign upload --release-tag v1.0.6 --json
code-mower release campaign upload --release-tag v1.0.6 --yes --json
```

## Recommended Update

For an existing pipx install:

```bash
export CODE_MOWER_PYTHON="$(command -v python3.12)"
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" code-mower==1.0.6
code-mower --version
code-mower board list
```

For hosted agents using uv:

```bash
uv tool install --python 3.12 --reinstall --refresh-package code-mower code-mower==1.0.6
code-mower --version
```

Restart any Board that still serves an older package. Existing repositories
should continue to review `migration setup-drift` output in a pull request
before applying generated changes.

## Quality And Privacy Proof

Every behavior slice passed the complete test suite, package CI on Python 3.12,
3.13, and 3.14, Gitar, and exact-head Codex and Claude merge-authority audits.
Those audits found and corrected concurrency, idempotency, storage, timeout,
identity, configuration, and truthful-readiness defects before merge.

Campaign upload remains opt-in and metadata-only. It excludes source, raw diffs,
prompts, transcripts, issue body text, raw stdout/stderr, authentication output,
local paths, and secrets.
