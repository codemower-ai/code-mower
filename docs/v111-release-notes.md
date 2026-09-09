# Code Mower v1.1.1 Release Notes

Code Mower v1.1.1 hardens hosted release qualification after v1.1.0. It makes
GitHub authentication, campaign issue binding, and trusted result-author
posture survive the normal dispatch, watch, and retry cycle, and makes failed
remote package installs return bounded, actionable classifications.

Install or upgrade the pinned package:

```bash
export CODE_MOWER_PYTHON="$(command -v python3.12)"
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" code-mower==1.1.1
code-mower --version
```

Hosted agents and CI boxes can use:

```bash
uv tool install --python 3.12 --reinstall --refresh-package code-mower code-mower==1.1.1
code-mower --version
```

## Authenticated GitHub And Durable Issue Binding

GitHub-comment qualification transports now reuse authenticated `gh` access
when no configured token environment variable is present. Authentication
probe output and credentials are never printed or stored in campaign state.
Explicit token environment variables retain precedence, and Devin's API
credential handling is unchanged.

Campaigns persist their GitHub issue number and reuse it for dispatch, resume,
watch, retry, and result discovery. An older campaign without an issue can be
bound once; a conflicting later issue is rejected before network writes or
campaign mutation. This fixes issue #817 in PR #824.

## Persistent Trusted Result Authors

Campaign creation accepts provider-scoped trusted result-author additions.
Provider names and GitHub logins are normalized and validated, then stored as
immutable campaign posture. Subsequent dispatch and result discovery reuse
that posture without requiring an environment override on every command.
Built-in authors and existing environment additions remain supported.

Trusted-author login values stay out of Board and cloud uploads. Exact result
markers, campaign identity, and author binding still fail closed. This fixes
issue #818 in PR #823; see the safe creation example in
[Release Qualification](release-qualification.md).

## Actionable Remote Install Failures

Remote qualification results must include an existing closed `failure_reason`
when `package_install` fails. Provider instructions explain the categories
and reserve `unknown` for failures that cannot be classified. Invalid or
missing reasons are rejected before the remote result counts as evidence.
This fixes issue #819 in PR #822.

## Upgrade Qualification And Privacy

The v1.1.1 upgrade campaign starts from v1.1.0 and selects Claude and Codex as
required providers, with Antigravity, Muse, Cursor Cloud Agent, and Devin
informational. Required failures block qualification; informational failures
remain visible. Campaign evidence is recorded on release issue #820.

The closed `code_mower.adoptionResult.v1` contract remains authoritative.
Operators inspect `code-mower release campaign upload` in dry-run mode before
explicitly uploading additive `adoption_run` metadata to CodeMower.com. Source,
raw diffs, prompts, transcripts, raw provider output, authentication output,
local paths, secrets, and trusted-author login values are excluded.

GitHub remains authoritative for pull requests, checks, and merge gates.
Code Mower remains a supervised-pilot release: qualification demonstrates
operational adoption, not reviewer quality or lane-promotion readiness. The
separately tracked live Jira write canary is outside this patch release.
