# Code Mower v1.1 Release Notes

Code Mower v1.1.0 adds optional Jira Cloud work tracking to the supervised
development loop. GitHub remains authoritative for pull requests, checks, and
merge gates. Repositories that do not configure a tracker keep the existing
GitHub-only behavior and generated workflows.

Install the pinned package:

```bash
export CODE_MOWER_PYTHON="$(command -v python3.12)"
pipx install --python "$CODE_MOWER_PYTHON" code-mower==1.1.0
code-mower --version
```

Hosted agents and CI boxes can use:

```bash
uv tool install --python 3.12 code-mower==1.1.0
code-mower --version
```

## Optional Jira Tracker

The provider-neutral `code_mower.trackerWorkItem.v1` contract separates work
tracking from builder, reviewer, and gate policy. Jira Cloud support includes:

- tenant- and project-scoped read-only issue discovery;
- controller queue, lane-status, and Board visibility;
- explicit pull-request association and bounded PR/gate milestones;
- local credential resolution, including macOS Keychain profiles; and
- init, adoption doctor, deterministic offline fixtures, and a live rehearsal
  runbook.

Start with the default dry run:

```bash
code-mower init --jira
code-mower doctor --adoption --repo OWNER/REPO
code-mower tracker status --repo OWNER/REPO
```

See [Jira Cloud Setup](jira-cloud-setup.md) and
[Jira Adoption Rehearsal](jira-adoption-rehearsal.md). Jira writes are disabled
by default. Enabling them in configuration is necessary but not sufficient:
every mutation command still plans by default and requires an explicit
`--apply` to write.

## Guarded Mutations

The closed mutation surface supports only claim, configured lifecycle
transitions, bounded templated comments, and one pull-request remote link. It
does not expose delete, attachment, arbitrary field update, or free-form
comment operations.

Before every write, Code Mower re-reads and validates the issue id, project,
permissions, and applicable transitions. Writes are attempted once; ambiguous
results fail closed instead of retrying into duplicate effects. Stable Jira
properties make supported comment and link operations replay-safe. Planning,
permission probes, and readiness checks report bounded metadata rather than
Jira issue prose.

## GitHub Remains Authoritative

Jira can supply work items and receive selected lifecycle milestones, but it
cannot override GitHub pull-request state, Code Mower audit verdicts, required
checks, or the merge gate. A Jira outage degrades Jira-backed queue visibility;
it does not reinterpret GitHub state or permit a merge.

PR-to-Jira synchronization accepts only explicit associations from trusted
GitHub actors. Association and milestone state are transactional so a failed
write is not reported as synchronized.

## Provider And Board Fixes

- Antigravity headless audits now use sandbox-compatible working paths while
  preserving their closed verdict contract.
- Board recognizes supervised Muse processes launched through both the stable
  and versioned Muse executables without exposing arguments or private paths.

## Release Qualification

The existing release-qualification contract remains available for Jira and
GitHub-only adopters. A single environment can emit the closed
`code_mower.adoptionResult.v1` artifact with:

```bash
code-mower release qualify \
  --release-tag v1.1.0 \
  --package-spec code-mower==1.1.0 \
  --output adoption-result.json \
  --execute
```

For several providers, use `code-mower release campaign` to create, dispatch,
and watch a bounded campaign. Upload remains a separate, explicit operation:
terminal results become additive `adoption_run` metadata only after a dry-run
preview and operator confirmation.

## Privacy Boundary

The v1.1 tracker contract remains metadata-only. Code Mower does not upload or
persist Jira issue summaries, descriptions, comments, attachments, source,
raw diffs, prompts, transcripts, raw provider output, authentication output,
local paths, or secrets. Jira identifiers and bounded lifecycle metadata are
included only where required to coordinate the configured tracker.

## Recommended Adoption

1. Install exactly `code-mower==1.1.0` and verify the version.
2. Keep the default GitHub tracker unless Jira is actually part of the team's
   operating model.
3. For Jira, run init and adoption doctor read-only before enabling mutations.
4. Exercise the offline Jira rehearsal, then the live read-only rehearsal.
5. Enable writes only after reviewing the closed operation set and required
   Jira permissions.
6. Keep reviewer lanes informational until repository-specific calibration
   satisfies the lane promotion policy.

The release was qualified across Python 3.12, 3.13, and 3.14 with generated
workflow linting, package and privacy checks, GitHub-only adoption rehearsal,
Jira read-only rehearsal, and an author-excluded peer audit. A live Jira write
rehearsal is intentionally not release-blocking unless the owner separately
authorizes a disposable issue.
