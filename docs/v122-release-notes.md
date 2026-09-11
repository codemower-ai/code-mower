# Code Mower v1.2.2 Release Notes

Code Mower v1.2.2 is a focused Jira lifecycle and Board correctness release.
It preserves the v1.2 supervised-pilot posture, GitHub merge-gate semantics,
provider contracts, and metadata-only privacy boundary.

## Jira ready-for-review lifecycle

`code-mower tracker pr-sync` can now map the `ready_for_review` PR milestone to
the Jira workflow status configured for that milestone, such as `Code Review`.
This lets Jira reflect when implementation has started and when the current PR
is ready for human review without an orchestrator guessing transition names.

The mutation path remains explicit and dry-run first. It discovers Jira's
available transitions, uses the configured status mapping, refuses ambiguous
or unavailable transitions, and is idempotent when the issue already has the
target status. No Jira issue description, comment, attachment, or source code
is added to cloud telemetry.

## Board current check state

GitHub can return multiple runs for the same check context, including a failed
run followed by a successful rerun on the same PR head. Board and
`code-mower lanes status` now identify repeated checks by context plus provider
or workflow and retain only the newest timestamped result in the live status
snapshot.

Superseded results remain available in historical local Board events. They do
not drive the current next action or owner queue. Distinct failing checks stay
visible and actionable. GitHub's current merge state and branch-protection
requirements remain authoritative and are reported separately; this change
does not weaken or reinterpret the repository's merge policy.

## Install or upgrade

With `uv`:

```bash
uv tool install --python 3.12 --reinstall --refresh-package code-mower code-mower==1.2.2
code-mower --version
```

With `pipx`:

```bash
CODE_MOWER_PYTHON="$(command -v python3.12)"
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" code-mower==1.2.2
code-mower --version
```

Expected version output: `code-mower 1.2.2`.

Restart any long-running Board after upgrading so its serving version matches
the installed package.

## Privacy

The privacy boundary is unchanged. Code Mower uploads only explicitly selected
metadata. It does not upload source, raw diffs, Jira issue bodies or comments,
attachments, prompts, transcripts, raw provider output, authentication output,
local paths, or secrets.
