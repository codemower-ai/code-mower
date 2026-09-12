# Optional organizational context setup

Start with the ordinary Claude + Codex installation. Coworker is optional and is
never a participant or an implicit account inherited from your host tool. A
future repository-context provider, such as Graphify, can use the same policy,
packet and readiness contracts with its own capability and authorization checks.
Graphify is not installed or required by this feature.

## Select a connection

Use a generic alias suitable for the repository. Keep account email, workspace,
credentials and destination permissions in the separate private connection.

```sh
code-mower init --easy --context-connection example-context --dry-run
code-mower init --easy --context-connection example-context --apply
python -m pip install 'code-mower[coworker]'
code-mower context connect coworker --connection example-context
code-mower context doctor --connection example-context --online --json
```

The init preview changes only the selected context policy; it does not sign in,
search, add participants or change reviewer authority. Context is optional by
default. Select `--context-required` if dependent work must pause when context is
unavailable, `--context-optional` to restore optional behavior, or
`--without-context` to remove the repository selection. Removing that selection
does not disconnect the private account or erase already-declared PR inputs.
Use [disconnect](context-connections.md) for credential and packet cleanup.

Connect asks for the intended account and workspace, then opens OAuth. A browser
already signed into another account cannot silently become the selected account:
Code Mower verifies signed identity against your explicit selection.

## Use the connection in a session

The normal path does not require packet handles or private request files. Start
the session with a work item and use the saved session file for each phase:

```sh
code-mower session start --repo OWNER/REPO --host codex --work-item EXAMPLE-123
code-mower session context prepare .code-mower/sessions/SESSION.json
code-mower session context deliver .code-mower/sessions/SESSION.json
code-mower session context attach .code-mower/sessions/SESSION.json --pr 42
code-mower session context feedback .code-mower/sessions/SESSION.json \
  --reviewer claude
```

Use `--host claude` when Claude starts the session. The host is the implicit
orchestrator and default builder. `prepare --builder NAME` supports a selected
Claude/Codex handoff. Each delivery and private-feedback read reauthorizes
online; use `prepare --refresh` only when intentionally replacing evidence or
retrying a failed retrieval. `session context status SESSION` shows redacted
progress and the next safe action.

The explicit commands in [Context Delivery](context-delivery.md) remain the
expert interface for automation that manages its own private request files,
packet handles, recipients and revisions.

## Diagnose readiness

`code-mower context doctor --connection example-context --json` reads private
local metadata only. It does not read credentials, load the MCP SDK, refresh a
grant or search. General `code-mower doctor` includes the same readiness vocabulary
when a policy is configured. Use `--context-online` there, or `--online` on context
doctor, to deliberately authorize online. Online verification refreshes the grant
with a bounded timeout; it never searches organizational memory.

| Readiness | Meaning and next step |
| --- | --- |
| `not_configured` | Continue the ordinary workflow. |
| `unchecked` | Run explicit online context doctor before using the connection. |
| `identity_unverified` | Connect the selected account. |
| `unauthorized` | Verify account/destination permissions; disconnect and reconnect if the grant changed. |
| `unavailable` | Check private storage and the optional package installation. |
| `stale` | Run explicit online verification to renew expired authorization. |
| `incomplete` | Identity is verified; perform an explicit bounded fetch to establish retrieval capability. |
| `ready` | Online identity and previously qualified retrieval capability are available. Fetch/delivery still reauthorizes. |

`dependent_work: paused` and `owner_action` distinguish a required dependency from
an optional outage. Optional failure leaves the ordinary code workflow usable;
required context doctor returns a nonzero exit status until ready. A `ready`
connection does not prove that a particular packet or PR input is still current.

For deliberate identity inspection on your local terminal only:

```sh
code-mower context identity --connection example-context --local-only
```

This prints the saved account and workspace, not credentials. It refuses
redirection and `--json`; it is not current online authorization. Do not paste the
terminal output into shared reports or public issues.

## Recover a missing or expired PR input

If the gate stays pending after a context packet expires, refresh the bounded
request with `context fetch --refresh`, attach the new packet, then rerun review.
Changing context requires a new review even when the code head is unchanged.

An optional outage can instead be explicitly acknowledged:

```sh
code-mower context attach --unavailable --connection example-context \
  --host codex --request-stdin < /path/to/private-unavailable-request.json
```

The private request has `repository`, `work_item`, `policy` and `pr`, as in the
[attachment example](context-delivery.md), with no `packet` field. This declares a
new metadata-only input revision and marks the gate pending. A fresh code-only
review must acknowledge that revision. Existing reviews do not carry forward.
If either the selected policy or trusted repository policy requires context, the
input is `required_unavailable` and review cannot pass. This command does not read
credentials, call a provider or silently select another account.

## Shared reporting and retention

The `code_mower.contextReadiness.v1` schema allowlists only configuration/required
booleans, readiness/authorization enums, dependent-work and owner-action flags,
and fixed diagnostic/action strings. Account/workspace names, aliases, local
paths, query text, source links, provider prose, credentials and packet/content
fingerprints are excluded. Session briefs use this same metadata vocabulary.
Board polling does not gain provider access or new context collection; cloud
export/upload remains an explicit existing operation, with no new event fields.
Context readiness is not a cloud report/event type in this release; keep these
reports local or share their redacted output deliberately.

The private store retains at most sixteen packet records per connection and eight
attached input records per packet. Refresh and eviction invalidate old bindings
and remove associated private review feedback; disconnect disables local access
first and attempts revocation and cleanup. None of those operations can recall
text already delivered to an authorized recipient. See the
[connection lifecycle](context-connections.md) and [review protocol](context-delivery.md).
