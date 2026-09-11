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
the grant was revoked. Changing accounts or destinations requires disconnecting
first, followed by a new login and a new connection generation.

Disconnect first disables local authorization, then attempts refresh-token
revocation and deletes the local credential. `remote_revocation: unknown` reports
a remote failure without restoring local access. `credential_cleanup:
needs_attention` means the OS vault could not delete its item: unlock the vault
and retry disconnect. Disconnect cannot recall evidence already delivered to an
authorized recipient.

The [provider contract](context-provider-contract.md) records the qualified SDK,
identity, read operations, and revocation behavior. Packet retrieval and delivery
are separate capabilities; the lifecycle alone does not claim they are available.
