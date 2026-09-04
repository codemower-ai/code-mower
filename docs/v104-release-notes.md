# Code Mower v1.0.4 Release Notes

Code Mower v1.0.4 is a post-announcement confidence release for experienced
engineers adopting the supervised pilot. It keeps the v1.0.3 gate semantics,
Python 3.12+ runtime contract, and metadata-only privacy boundary while making
offline verification, setup diagnostics, and token-bearing workflows safer and
more predictable.

Install the pinned package:

```bash
export CODE_MOWER_PYTHON="$(command -v python3.12)"
pipx install --python "$CODE_MOWER_PYTHON" code-mower==1.0.4
code-mower --version
```

Hosted agents and CI boxes can use:

```bash
uv tool install --python 3.12 code-mower==1.0.4
code-mower --version
```

## What Is New

- The standalone-wrapper reinstall regression fixture now uses a local,
  standard-library-only build backend. The test proves the offline path does
  not invoke pip or a package index, so contributor tests remain deterministic
  on a disconnected machine.
- Setup-drift text and JSON identify whether configuration came from the
  packaged starter or an explicit repository file. This makes cold-install and
  cross-checkout upgrade output easier to interpret.
- Hosted-agent doctor output emits the adoption posture hint once, before
  provider-specific CLI warnings, while preserving the individual remediation
  detail.
- Generated pull-request labeler and fix-round dispatch workflows that receive
  `DISPATCH_TOKEN` now run trusted default-branch definitions through
  `pull_request_target`. They retain the same-repository guard and do not check
  out pull-request code.
- The installation and documentation flow was re-audited at the release
  baseline. Python 3.12+, cold-install versus upgrade, hosted-builder posture,
  CLI guidance, public examples, and privacy claims remain consistent;
  172 local Markdown targets and anchors resolved successfully.

## Recommended Update

For an existing pipx install:

```bash
export CODE_MOWER_PYTHON="$(command -v python3.12)"
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" code-mower==1.0.4
code-mower --version
code-mower board list
```

For hosted agents using uv:

```bash
uv tool install --python 3.12 --reinstall --refresh-package code-mower code-mower==1.0.4
code-mower --version
```

Restart a Board that still serves an older package:

```bash
code-mower board list
code-mower board stop --port PORT --yes
code-mower board serve --repo OWNER/REPO --record-events
```

For an existing repository, run `code-mower migration setup-drift` and review
the generated changes in a pull request before applying them. Do not replace
customized workflows from a dry-run alone.

## Quality And Privacy Proof

The v1.0.4 train used Muse, Antigravity, and Cursor as builders with Codex
orchestration, plus independent Codex, Claude, and Gitar review. Accepted
findings corrected duplicate posture guidance, release-scope leakage, failed
release-hygiene tests, inaccurate evidence provenance, and documentation
quality issues before merge. Every behavior change has focused regression
coverage, and the release gate includes the full unit suite, Ruff, compileall,
privacy scanning, package build, release-readiness, and a clean install
rehearsal.

Uploads remain opt-in and metadata-only. Code Mower does not upload raw diffs,
source, transcripts, issue body text, raw stdout/stderr, authentication output,
local paths, or secrets.
