# Code Mower v1.0.5 Release Notes

Code Mower v1.0.5 adds repeatable release qualification across provider
environments. It keeps the v1.0.4 gate semantics, Python 3.12+ requirement,
supervised-pilot posture, and metadata-only privacy boundary.

Install the pinned package:

```bash
export CODE_MOWER_PYTHON="$(command -v python3.12)"
pipx install --python "$CODE_MOWER_PYTHON" code-mower==1.0.5
code-mower --version
```

Hosted agents and CI boxes can use:

```bash
uv tool install --python 3.12 code-mower==1.0.5
code-mower --version
```

## What Is New

- `code-mower release qualify` installs an exact package in an isolated
  environment and emits a closed `code_mower.adoptionResult.v1` document.
- `code-mower release campaign` coordinates Claude, Codex, Antigravity, Muse,
  Cursor/Grok Bot, and Devin. It is a dry run unless `--apply` is supplied.
- Campaign state is resumable and idempotent. Local adapters and hosted
  dispatches fail closed when credentials, configuration, trusted authors, or
  matching result evidence are unavailable.
- Board shows campaign and provider state, elapsed time, operational outcome,
  and the next action without requiring GitHub to remain reachable.
- The optional `adoption_run` cloud event lets CodeMower.com compare release
  installation and operational results by provider and environment.

Qualification answers whether a release installs and operates in an
environment. It does not compare builder quality, calibrate reviewer findings,
justify lane promotion, or measure model cost. Use builder experiments and the
lane promotion policy for those decisions.

## Qualify A Release

Preview without installing anything:

```bash
code-mower release qualify \
  --release-tag v1.0.5 \
  --package-spec code-mower==1.0.5 \
  --output adoption-result.json
```

Add `--execute` after reviewing the plan. To coordinate several provider
environments, use `code-mower release campaign`; providers without configured
adapters or hosted credentials remain explicitly unavailable rather than being
reported as passing.

## Recommended Update

For an existing pipx install:

```bash
export CODE_MOWER_PYTHON="$(command -v python3.12)"
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" code-mower==1.0.5
code-mower --version
code-mower board list
```

For hosted agents using uv:

```bash
uv tool install --python 3.12 --reinstall --refresh-package code-mower code-mower==1.0.5
code-mower --version
```

Restart a Board that still serves an older package before inspecting campaign
cards. For existing repositories, continue to review `migration setup-drift`
output in a pull request before applying generated changes.

## Quality And Privacy Proof

The campaign implementation passed the complete test suite plus focused
campaign, cloud-contract, Board, packaging, and documentation rehearsals.
Independent Codex and Claude audits found and corrected concurrency,
idempotency, cross-platform import, campaign-identity, configuration, and
truthful-status defects before merge.

Cloud upload remains opt-in and metadata-only. Adoption events contain safe
identifiers, coarse environment classes, categorical outcomes, counts, and
timings. They exclude source, raw diffs, prompts, transcripts, issue body text,
raw stdout/stderr, authentication output, local paths, and secrets.
