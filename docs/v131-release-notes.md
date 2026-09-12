# Code Mower v1.3.1 Release Notes

Code Mower v1.3.1 makes optional organizational context part of the normal
session workflow. A Claude or Codex host can start from a work item, retrieve
one bounded packet, deliver it to the selected builder, attach it to a pull
request, and retrieve an independent reviewer's private findings without
copying packet handles, revision identifiers, or private request files.

## Guided context workflow

Start a session with a work item, then use its saved session file throughout:

```sh
code-mower session start --repo OWNER/REPO --host codex --work-item EXAMPLE-123
code-mower session context prepare .code-mower/sessions/SESSION.json
code-mower session context deliver .code-mower/sessions/SESSION.json
code-mower session context attach .code-mower/sessions/SESSION.json --pr 42
code-mower session context feedback .code-mower/sessions/SESSION.json \
  --reviewer claude
```

The host remains the implicit orchestrator and default builder. The session
derives repository, work item, selected connection and policy from trusted
configuration and protected state. Prepare fetches once and creates the
context-aware work order. Delivery and feedback still perform fresh online
authorization. Explicit `--refresh` is required before replacing retrieval
input or retrying a failed search.

`session context status` reports fixed redacted progress states and the next
safe action. It never prints queries, account or workspace identity, packet
handles, revisions, local paths, citations, findings, or provider prose.

## Recovery and review binding

The guided path saves its intended attachment revision before the first GitHub
write. A restarted process can reconcile an accepted comment, retry an
explicitly checked uncertain write with the same revision, or pause before an
unsafe replacement. A changed code head or refreshed packet retires stale local
bindings and requires a new independent review. A builder's own reviewer lane
cannot satisfy that review.

The lower-level `context fetch`, `context deliver`, `context attach`, and
`context feedback` commands remain available for automation that deliberately
manages private request files and identifiers.

## Qualification

The release qualification exercises Codex-hosted and Claude-hosted sessions
through fresh store instances at every phase. Both use the same synthetic
bounded retrieval, cross-host builder and reviewer roles, attachment,
independent context-aware review, private feedback, and redaction assertions.
Mutation-boundary tests cover prepare, attachment and feedback recovery.

The qualification is provider-free and makes no productivity claim. A live
product-repository pilot remains owned by its existing Code Mower lane so this
release work does not compete for the same repository or private account state.
See [v1.3.1 Guided Context Qualification](v131-guided-context-qualification.md).

Graphify is still an anticipated provider behind the existing provider-neutral
packet contract. It is not installed or qualified by v1.3.1.

## Install or upgrade

```bash
CODE_MOWER_PYTHON="$(command -v python3.12)"
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" \
  'code-mower[coworker]==1.3.1'
code-mower --version
```

Expected output: `code-mower 1.3.1`. The base package remains usable without
the Coworker extra or any context connection.

The privacy boundary is unchanged. Cloud upload does not collect organization
context, identities, queries, source text, citations, findings, credentials, or
packet fingerprints.
