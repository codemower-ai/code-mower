# Code Mower v1.4.0 Release Notes

Code Mower v1.4.0 turns Devin into an optional peer participant with a durable
remote work-order lifecycle, adopts Graphify as a bounded optional local
repository-graph provider, and lands the Slack command and ingress foundation.
Claude Code + Codex remain the first-run default builders and reviewers, and
Devin, Coworker, Graphify, and Slack all stay opt-in.

## Devin peer participant and lifecycle

Devin can now be selected as an optional participant with one setup and
readiness path for both the local CLI and the hosted v3 API:

```sh
code-mower doctor --easy --devin --json
```

The release adds a provider-neutral remote session lifecycle, a reusable Devin
v3 session client, trusted hosted work orders, and normalized local and hosted
review evidence. Work-order collection verifies the exact round, pull-request
number, linked issue, author, repository, branch, head SHA, and base branch
before it publishes evidence. Results survive process restarts and the merge
boundary, and a running session that already holds a current-round result is
recognized without weakening that verification.

Devin's local review stays informational and Devin is not a qualified peer
orchestrator or merge-eligible reviewer in v1.4.0. Structured provider output,
prompts, transcripts, diffs, and credentials stay in protected local state and
are never returned through the work-order surface.

### Release hardening: stale completions never look complete

A provider can resume work while its API still returns the previous round's
structured output. Code Mower already rejected that stale completion, cleared
verified pull-request evidence, and required a fresh collection, but the same
response could still project `session.state=complete`, which can make an
orchestrator stop polling an active fix round.

When a persisted completion rejection exists and the remote projection reports
`complete`, the returned logical session projection is now `state: running`,
`reason: result_not_ready`, `next_action: status`. The authoritative rejection
block is unchanged (`state: rejected`, a bounded reason such as
`stale_completion`, and `next_action: collect_after_provider_update`), the
durable remote record and shared result precedence are untouched, and a later
valid exact-round collection clears the rejection and returns verified
pull-request evidence.

## Bounded optional Graphify provider

Graphify is adopted as an optional, bounded local provider behind the existing
provider-neutral packet contract. The local repository graph is pinned to an
exact artifact digest, bound to a full commit and tree revision, kept in private
state outside every checkout, denied network access, and fails closed on stale,
partial, or unknown revisions. There is no default dependency, background
service, subscription, or mandatory indexing step, and synthetic fixtures prove
the extension point rather than Graphify compatibility.

## Slack command and ingress foundation

Slack has an authenticated, bounded ingress seam with a documented command and
identity contract: request signature verification, tenant and actor resolution,
durable idempotent receipt, and allowlisted redacted responses. Slack is a
foundation for later worker delivery, not a completed Slack integration: it is
not a builder, reviewer, participant, scheduler, or orchestrator, it carries no
repository or merge authority, and it delivers no worker results in v1.4.0.

## Release hygiene

The committed `code-mower-package-manifest.json` is a current package surface.
Its `package.version` now tracks the release, and both the release-hygiene suite
and `code-mower migration release-readiness` fail when it disagrees with
`pyproject.toml` or `src/code_mower/__init__.py`.

## Install or upgrade

```bash
CODE_MOWER_PYTHON="$(command -v python3.12)"
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" \
  'code-mower[coworker]==1.4.0'
code-mower --version
```

Expected output: `code-mower 1.4.0`. The base package remains usable without the
Coworker extra, a context connection, Devin, or Slack.

The privacy boundary is unchanged. Cloud upload does not collect organization
context, identities, queries, source text, citations, findings, credentials,
packet fingerprints, provider prose, or Slack message content.

## Post-merge release steps

The orchestrator owns every step after this pull request merges: binding the
annotated `v1.4.0` tag to the exact release merge commit, the no-publish
`release.yml` rehearsal at `--ref v1.4.0`, TestPyPI publication and rehearsal,
production PyPI publication and rehearsal, SHA-256 comparison of the verified
workflow wheel and sdist against PyPI before they are attached to the GitHub
v1.4.0 Release, local installation and Devin readiness verification, the
published-package qualification campaign, Board restart and verification from an
exact v1.4.0 checkout, and the allowlisted CodeMower.com metadata upload. The
expected evidence is each immutable workflow run ID with its exact head, the
rehearsal JSON, the digest comparison, the campaign result per provider, the
Board inventory showing version `1.4.0`, and accepted cloud event identifiers
and counts without report prose.
