# Code Mower v1.0.3 Release Notes

Code Mower v1.0.3 is the confidence-polish release for experienced senior
engineers adopting the v1 supervised pilot. It keeps the v1.0.2 merge-gate
semantics and metadata-only privacy boundary while making Board operation,
configuration diagnostics, and the public repository substantially cleaner.

Install the pinned package:

```bash
export CODE_MOWER_PYTHON="$(command -v python3.12)"
pipx install --python "$CODE_MOWER_PYTHON" code-mower==1.0.3
code-mower --version
```

Hosted agents and CI boxes can use:

```bash
uv tool install --python 3.12 code-mower==1.0.3
code-mower --version
```

## What Is New

- Board `/api/status` uses a thread-safe stale-while-refresh cache. Cold starts
  return an immediate metadata-only warming response; fresh and stale snapshots
  remain responsive while one background refresh performs slower GitHub and
  process discovery.
- Board refresh retries use bounded backoff, browser polling follows the
  remaining cache TTL, and event recording uses successful snapshot generations
  so stale responses are not duplicated and completed refreshes are not lost.
- `init` and setup-drift output distinguish packaged starter configuration from
  an explicitly selected repository config, including cross-checkout drift
  inspection.
- Productivity reports describe local evidence as empty, partial, or complete;
  reviewer-spend and cloud evidence remain optional enhancements rather than
  prerequisites for local readiness.
- Dead local agent cards are visibly stale. The confirmed
  `code-mower board stop --prune-stale-agents --yes` path removes only stale
  Code Mower adapter metadata and reports partial or failed cleanup accurately.
- Board stop and pruning defend against PID reuse, symlink traversal, malformed
  PID values, silent file caps, and exception text that could contain local or
  authentication details.
- The public starter configuration, contributor documentation, support and
  security guidance, repository metadata, and identity-variable flow were
  reviewed so a public checkout does not depend on maintainer-specific values.

## Recommended Update

For an existing pipx install:

```bash
export CODE_MOWER_PYTHON="$(command -v python3.12)"
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" code-mower==1.0.3
code-mower --version
code-mower board list
```

For hosted agents using uv:

```bash
uv tool install --python 3.12 --reinstall --refresh-package code-mower code-mower==1.0.3
code-mower --version
```

Restart a Board that still serves an older package:

```bash
code-mower board list
code-mower board stop --port PORT --yes
code-mower board serve --repo OWNER/REPO --record-events
```

## Quality And Privacy Proof

The v1.0.3 train used Claude Code and Muse as builders with Codex orchestration,
plus independent Codex, Claude, and Gitar review. Peer audits blocked concrete
cache, polling, recording, deletion-safety, status-contract, and config-identity
defects before merge. The release PR records the final tests, lint, generated
workflow checks, package build, release-readiness, fresh-checkout rehearsal,
package-index verification, and metadata-only CodeMower.com upload.

Uploads remain opt-in and metadata-only. Code Mower does not upload raw diffs,
source, transcripts, issue body text, raw stdout/stderr, authentication output,
local paths, or secrets.
