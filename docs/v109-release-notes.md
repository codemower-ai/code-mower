# Code Mower v1.0.9 Release Notes

Code Mower v1.0.9 makes multi-provider release qualification more resilient
and more honest about which evidence decides completion. It preserves the
supervised-pilot operating model, Python 3.12+ requirement, and metadata-only
privacy boundary.

Install the pinned package:

```bash
export CODE_MOWER_PYTHON="$(command -v python3.12)"
pipx install --python "$CODE_MOWER_PYTHON" code-mower==1.0.9
code-mower --version
```

Hosted agents and CI boxes can use:

```bash
uv tool install --python 3.12 code-mower==1.0.9
code-mower --version
```

## What Is New

- Board detects dead or abandoned status-refresh workers, clears stale state,
  and enters bounded retry backoff instead of remaining permanently stuck
  (#728, PR #733).
- Package-install qualification records a closed failure phase, reason, and
  bounded remediation. Campaigns can explain whether installation failed at
  package resolution, package verification, or the first-user rehearsal
  without persisting raw stdout or stderr (#727, PR #732).
- Release campaigns accept `--required-providers`. Required providers determine
  success or failure; informational providers remain visible in status, watch,
  Board, dispatch markers, and upload evidence without blocking a completed
  required set (#730, PR #734).
- Campaigns that omit `--required-providers` retain the v1.0.8 all-required
  behavior and exact adoption-event identity. Explicit provider posture is an
  optional metadata dimension with its own deterministic event identity.

## Run A Qualification Campaign

Qualify one provider environment directly when a full campaign is unnecessary:

```bash
code-mower release qualify \
  --release-tag v1.0.9 \
  --package-spec code-mower==1.0.9 \
  --output adoption-result.json \
  --execute
```

Preview a campaign with established local providers required and experimental
or hosted providers informational:

```bash
code-mower release campaign create \
  --release-tag v1.0.9 \
  --package-spec code-mower==1.0.9 \
  --providers claude,codex,antigravity,muse,cursor_cloud_agent,devin \
  --required-providers claude,codex \
  --repo-slug OWNER/REPO
```

Apply only after the preview and adoption doctor are clean:

```bash
code-mower doctor --adoption --repo OWNER/REPO
code-mower release campaign dispatch \
  --release-tag v1.0.9 \
  --required-providers claude,codex \
  --apply \
  --repo-slug OWNER/REPO \
  --issue ISSUE_NUMBER
code-mower release campaign watch --release-tag v1.0.9
```

Preview the closed cloud bundle, then upload it explicitly:

```bash
code-mower release campaign upload --release-tag v1.0.9 --json
code-mower release campaign upload --release-tag v1.0.9 --yes --json
```

Qualification writes the closed `code_mower.adoptionResult.v1` artifact.
Campaign upload converts terminal results into additive `adoption_run` events.
It does not upload source, raw diffs, prompts, transcripts, issue body text, raw
provider output, authentication output, local paths, or secrets.

## Recommended Update

For an existing pipx install:

```bash
export CODE_MOWER_PYTHON="$(command -v python3.12)"
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" code-mower==1.0.9
code-mower --version
code-mower board list
```

For hosted agents using uv:

```bash
uv tool install --python 3.12 --reinstall --refresh-package code-mower code-mower==1.0.9
code-mower --version
```

Restart a Board that still serves an older package. For existing repositories,
review `migration setup-drift` output in a pull request before applying any
generated changes.

## Quality And Privacy Proof

The implementation PRs passed package CI on Python 3.12, 3.13, and 3.14,
Gitar, and exact-head Codex and Claude audits. The audits found and corrected
package-failure precedence, retry-boundary, event-identity, option-scope, and
campaign-state defects before merge.

Cloud sharing remains opt-in and dry-run first. The default release campaign
works locally without a CodeMower.com account.
