# Code Mower v1.3.0 Release Notes

Code Mower v1.3.0 adds optional organizational context through Coworker. Claude
and Codex can receive the same bounded, cited evidence from an explicitly
selected account, and reviews become stale when that evidence changes.

The default remains Claude + Codex with no Coworker dependency or login. The
calling host remains the orchestrator; a context provider gains no builder,
reviewer, tracker-write or merge authority. The supervised-pilot posture remains.

## Optional Coworker context

- Select a generic connection reference during init. Keep the actual account,
  workspace, credentials and destination permissions in private local storage.
- Authenticate through OAuth and verify signed account/workspace identity.
  Credentials use macOS Keychain or Linux Secret Service, without a plaintext
  fallback or import from the host's unrelated MCP account.
- Fetch a bounded read-only packet with citations, completeness and expiry.
  Every retrieval and recipient replay reauthorizes online.
- Deliver the same packet to approved participants. Private detailed findings
  stay local; public review comments contain only allowlisted verdict metadata.
- Bind review validity to both code head and context input revision. A changed
  required-context policy is read from trusted repository configuration on every
  gate run; old code-only reviews cannot silently satisfy it.
- Diagnose readiness offline by default, with explicit online verification.
  Optional outages can be acknowledged as a new input requiring a fresh code-only
  review; required context pauses dependent work.

See the [setup guide](context-setup.md), [connection lifecycle](context-connections.md)
and [review delivery protocol](context-delivery.md).

## Qualification and limits

The [two-case qualification](v130-context-qualification.md) demonstrated live
account-bound delivery to both host roles and fresh authorization across process
restarts. It found useful prior art in one case and mostly adjacent material in
the other. Neither case justified changing the existing patch solely from the
retrieved evidence. No time-saving or general productivity claim is made.

Packets may be partial, and a citation from memory does not establish current
source truth. Required context cannot be reviewed offline or by a runner without
the selected authorization. The gate is event-driven, not a continuous revocation
monitor. Revoking a refresh grant prevents subsequent Code Mower use; already-issued
bearer tokens outside Code Mower may persist until expiry.

Graphify is anticipated by the provider-neutral packet contract and a synthetic
local-graph fixture. Its production adapter is a separate v1.3.x candidate.

## Install or upgrade

```bash
CODE_MOWER_PYTHON="$(command -v python3.12)"
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" code-mower==1.3.0
code-mower --version
```

For optional Coworker support, use the same version with the extra:

```bash
PIP_NO_CACHE_DIR=1 pipx install --force --python "$CODE_MOWER_PYTHON" 'code-mower[coworker]==1.3.0'
code-mower init --easy --context-connection example-context --dry-run
```

Expected version output: `code-mower 1.3.0`. Inspect the init preview and apply the
selected policy, then connect the intended account as described in the setup
guide. Existing repositories should review generated support-file/workflow drift
when adopting context-aware gates. Restart long-running Boards after upgrading.

## Privacy

The privacy boundary is unchanged. Cloud upload remains optional and does not
collect organization context, account/workspace identity, queries, source text,
private citations, prompts, transcripts, credentials or packet fingerprints.
Default readiness output uses fixed redacted metadata. Identity inspection is an
explicit local-terminal operation. Disconnect disables access before remote
revocation and private packet/feedback cleanup; it cannot recall already-delivered
text from a recipient.
