# Optional Coworker connections

Coworker supplies organizational evidence; it does not become an orchestrator,
builder, reviewer, or tracker. Ordinary Claude/Codex setup requires no connection
and installs no MCP or credential-store dependency.

The connection lifecycle is available with the optional extra:

```sh
python -m pip install 'code-mower[coworker]'
code-mower context connect coworker --connection example-context
code-mower context verify --connection example-context --json
code-mower context status --connection example-context --json
code-mower context disconnect --connection example-context --json
```

Connect prompts for the intended account, workspace identifier from the Coworker
URL, approved repositories, and recipient roles (for example `codex:builder` and
`claude:reviewer`). It opens the provider's OAuth sign-in with PKCE. Use the browser
profile for that account. Signed identity claims must match the selected account
and workspace before Code Mower saves a usable connection. An unrelated host MCP
connection is never imported. Coworker's currently qualified `mcp:tools` grant is
broad; Code Mower's connection commands perform no organizational tool calls.

Use `--no-browser` to open the transient sign-in URL yourself. Keep that URL local.
For a noninteractive launcher, `--spec-stdin` accepts a private JSON object with
`principal`, `workspace`, `repositories` (list), and `recipients` (list). Send it
through stdin, not command arguments or a tracked file. The browser approval is
still required. Do not publish real accounts, workspace identifiers, private
repository names, or operator-specific connection aliases.

Credentials are stored only in macOS Keychain or Linux Secret Service. The store
must be available and unlocked; there is no plaintext fallback. Private connection
metadata defaults to `~/.local/share/code-mower/context`, with a directory mode of
0700 and files of 0600. An explicit `--state-dir` must be an absolute private
directory outside Git repositories and contain no symlink components. Windows
connections are not supported yet; normal Code Mower workflows are unaffected.

`status` is offline and redacted. `authorization: unchecked` is never permission to
reuse cached evidence. `verify` performs an online refresh and validates the
refreshed signature, issuer, audience, client, principal, workspace, and expiry.
Concurrent refreshes serialize per connection and save the rotated credential
pair in one vault item. Search and memory entitlement remain `unverified` until
the separate retrieval path has actually verified them; a login is not a memory
capability check.

The qualified provider invalidates the renewable grant when the refresh token is
revoked. Already-issued access tokens may remain usable until expiry. Consequently
dependent fetch and replay operations must refresh online before granting access.
A failed refresh invalidates the local connection generation and prior cached
evidence. Use `verify` to explicitly retry after service recovery, or reconnect if
the grant was revoked. Reconnecting, including after failed authentication,
requires `disconnect` followed by `connect`. This also applies when changing
accounts or destinations; the new login creates a new connection generation.

Disconnect first disables local authorization, then attempts refresh-token
revocation and deletes the local credential. `remote_revocation: unknown` reports
a remote failure without restoring local access. `credential_cleanup:
needs_attention` means the OS vault could not delete its item: unlock the vault
and retry disconnect. Disconnect cannot recall evidence already delivered to an
authorized recipient.

## Bounded retrieval

Prepare a private request using the selected repository and authoritative work
item. This example contains synthetic values; actual query text and work-item
identities belong in a private file outside the repository:

```json
{
  "repository": "owner/repo",
  "work_item": "EXAMPLE-1",
  "recipient": "codex:builder",
  "query": "Tracking number behavior for unfamiliar carriers",
  "source": "jira",
  "policy": {
    "schema": "code_mower.contextPolicy.v1",
    "connection": "example-context",
    "policy_version": "v1",
    "required": true
  }
}
```

```sh
code-mower context fetch --connection example-context --request-stdin --json < /path/to/private-request.json
```

The policy uses the shared provider-neutral contract. Defaults allow three read
requests, two discovery pages, five evidence records, 20,000 text bytes per record,
80,000 total text bytes, a 262,144-byte packet, and 30 seconds. The read-request
budget includes tool-discovery pages and one search. OAuth has its own fixed
request cap and shares the fetch deadline; MCP session bookkeeping has a separate
bounded allowance. There is no automatic search pagination or redispatch. The
optional source filter is passed to the qualified search tool as data; omitted
source allows the provider's normal search across the selected account's sources.

Only `om2_search` in fast mode is called, with raw documents disabled. The adapter
checks the qualified input schema. Tool descriptions and annotations cannot
authorize a different operation. Sampling, elicitation, generic agent calls,
resource URLs, and writes are not supported by this retrieval path.

Each record retains its source-row citation and title. A provider `date` is kept
as `source_date`, without assuming it means modification time. Attribute evidence
has `unknown` confidence: similarity scores do not establish truth. Unresolved
entities, warnings, additional unreturned results, and text truncation produce
explicit partial evidence. No source URL or revision is invented.

The command returns redacted counts, an opaque local packet handle, and observed
request/page/byte/time usage. Provider cost remains `null` when unavailable. Text,
citations, query, hashes, and account bindings stay in the private store. Optional
failure returns `optional_unavailable`; required failure returns
`required_unavailable` and a nonzero exit status. Neither outcome silently selects
another account.

Repeating a successful request reauthorizes online and reuses the same packet.
Changing only an approved participant role does not retrieve different evidence.
Failures, crashes, stale packets, or material changes require an explicit
`--refresh`; it replaces the prior packet and invalidates its handle. A saved
packet is never permission to replay offline. `load_authorized` checks the
current connection, account/workspace, repository, work item, recipient, policy
version, expiry, and integrity before delivery.

At most 16 packet records are retained per connection; older records are evicted.
Disconnect disables authorization first and deletes the connection's cached
packets, including incomplete attempts. `packet_cleanup: needs_attention` means
private-file cleanup failed; resolve the local storage problem and retry
disconnect. Deletion cannot recall evidence already delivered to a recipient.

The [provider contract](context-provider-contract.md) records the qualified SDK,
identity, read operations, and revocation behavior. Integration into participant
prompts and review gating is the separate delivery capability; fetching a packet
alone does not make an existing review context-aware.
