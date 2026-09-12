# Context provider contract for v1.3

Status: architecture accepted; live Coworker login, identity, OM2 search, and
headless refresh, and refresh-token revocation qualified on 2026-09-11.
Access-token revocation alone did not immediately reject an existing token. Tracks [#869](https://github.com/codemower-ai/code-mower/issues/869)
in [epic #868](https://github.com/codemower-ai/code-mower/issues/868).
This document does not announce a working Coworker integration.

## Decision

Add optional context providers to the existing external-context manifest and
work-order pipeline. A provider supplies evidence to an orchestrator, builder,
and independent reviewer. It never becomes an agent lane and gains no tracker
mutation or merge authority. Claude and Codex remain the default participants;
the hosting tool remains the implicit orchestrator.

Separate four responsibilities:

1. Shared repository policy selects a generic connection reference and limits.
   A private local connection store holds the endpoint, verified identity,
   credential reference, approved recipients, and authorization lifecycle.
2. A narrow adapter verifies access and performs bounded, allowlisted reads.
   Its capabilities distinguish organization search from organizational memory.
3. A versioned private packet preserves evidence, citations, retrieval time,
   completeness, integrity, and optional source revision/build time. Its envelope
   binds the connection, repository, work item, policy version, and recipients.
4. Existing consumers receive the same authorized packet through a trusted
   runtime input channel. Base-ref project doctrine remains separate. Retrieved
   text is untrusted evidence, including any instructions embedded in it.

Existing configs and file-preview manifests remain valid. Default installation
does not acquire a provider dependency or require another login. A local graph
adapter can later use the packet contract without invented SaaS credentials.

## Coworker verification ledger

The authenticated customer setup UI identifies `https://odin.coworker.ai/mcp`
for generic remote HTTP clients and standard MCP OAuth. A small authorized
probe used a dedicated local OAuth registration. All identities, credentials,
raw responses, and source identifiers remain private. The following are
observations for that connection, not entitlement promises for every account.

| Capability | Observed contract | Implementation consequence |
| --- | --- | --- |
| Endpoint and transport | Streamable HTTP initialization and one unpaginated tool listing succeeded at `https://odin.coworker.ai/mcp`; bare GET returns 405. | Do not diagnose the bare GET as failed authentication or use the earlier unresolved marketing hostname. |
| Authentication | OAuth public-client registration, authorization code with PKCE S256, scope `mcp:tools`, and a loopback redirect worked. The consent page offers broad act-as-user access. | Explain the grant and require explicit local consent; OAuth scope alone cannot enforce read-only use. |
| Identity | RS256 JWT verified against the advertised JWKS. Signed `email`, `network`, `sub`, and `client_id` bind the selected identity, workspace, and registration. Issuer and audience both equal the server origin, `https://odin.coworker.ai`. | Pin algorithm, issuer, audience, and trusted JWKS origin; check expiry, local expected identity/workspace, and client binding. Never accept an unsigned decode or an LLM answer as identity proof. |
| Source access | `individual_context` succeeded; its result includes prose describing connected and unconnected sources. | Treat that prose as informational. Actual read success is separate from source availability and from the signed identity binding. |
| Organizational memory | One source-scoped `om2_search`, `top_k=3`, `search_mode=fast`, and raw fallback disabled returned three Attribute results. | Memory access worked for this connection. Probe it separately from source search; never infer general entitlement from a listed tool. |
| Result envelope | Tool metadata has no output schema. The actual result used JSON inside a text content block, with outer `result` and `compaction` objects; `structuredContent` was null. | Parse this observed envelope strictly and bound bytes. A changed or unsupported envelope is an explicit error, not an empty success. |
| Citations and coverage | Attribute results include `id`, `kind`, `text`, `date`, `source_row_id`, `doc_title`, and `similarity`. The response was `partial`, `has_more=true`, and entity resolution was `unresolved`. | Preserve the returned source identifier and title as provenance. Do not invent a source URL, `doc_key`, revision, or SemanticUnit ID; similarity is not confidence. Ranked search is not an exhaustive source inventory. |
| Refresh | A forced-expiry probe used the SDK refresh flow without browser interaction. Both access and refresh tokens rotated, and the refreshed JWT retained the verified identity/workspace. | Persist absolute expiry and the new token pair atomically. Reverify refreshed identity before use. |
| Revocation | Revoking the temporary access token returned 200, but it could still initialize MCP. Revoking the temporary refresh token returned 200; a subsequent SDK refresh received 400 and cleared its in-memory credentials. | Require a successful online refresh before each retrieval or replay. On failure, invalidate local authorization and packet generations. Neither JWT validity nor a revocation HTTP 200 proves ongoing or withdrawn access. |
| Usage | The server reported search processing time, but no price or charge. | Cost remains unknown. No broader search, pagination, automatic retry, or extra source retrieval was performed. |

The probe made one `individual_context` call and one `om2_search` call, with
30-second read deadlines. The search returned 445 bytes of result text and
source identifiers/titles; no source documents were fetched. Authentication,
JWKS retrieval, initialization, and tool discovery were separate from those
data calls. A listing exposed 71 tools, including writes, so production code
must select a small explicit allowlist rather than trust `readOnlyHint` alone.
No Jira or other connected-source mutation was performed.

The initial search returned only Attributes. A subsequent bounded C4 work-item
search returned one `SemanticUnit` and two `Attribute` records, each with the
same seven fields listed above. The adapter accepts these two observed record
kinds, preserves the kind as `source_kind`, and keeps confidence unknown for
both. Other result kinds and `om2_source_trace` remain unqualified. Never pass
an Attribute ID to a SemanticUnit-only tool.

The [synthetic contract fixture](../tests/fixtures/coworker_mcp_contract.json)
records stripped input schemas and an invented response with the observed
shape. It is not an export of private content and does not establish live
capabilities by itself.

The [mixed-record fixture](../tests/fixtures/coworker_mcp_mixed_records.json)
uses invented evidence to cover the additionally observed C4 response shape.

Discovery sources: [protected resource metadata](https://odin.coworker.ai/.well-known/oauth-protected-resource/mcp),
[authorization server metadata](https://odin.coworker.ai/.well-known/oauth-authorization-server),
and [JWKS](https://odin.coworker.ai/oauth/jwks). The public
[Coworker MCP](https://coworker.ai/mcp) and
[organizational memory](https://coworker.ai/organizational-memory) pages remain
marketing claims, separate from these observations.

## MCP client choice

Use the official [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
as the first implementation candidate, installed through an optional extra with
a reviewed version range. Its current stable line is v2, with standard client
transports; the v1 maintenance API must not be assumed to be the current API.
Use the maintained client/auth mechanisms rather than hand-written OAuth or
copying another host's credential cache. Do not install the SDK's CLI extra
unless a demonstrated runtime need requires it.

The live probe used SDK 2.2.0. Its `OAuthClientProvider` handles registration,
PKCE, token exchange, and refresh. After a process restart, the adapter must
restore absolute token expiry and the validated OAuth metadata: the SDK's
storage protocol only reloads tokens/client information, and otherwise its
refresh fallback points to `/token` rather than this provider's advertised
`/oauth/token`. The probe seeded that context and exercised the SDK refresh
flow; it did not implement a second OAuth protocol stack.

C3 must cover that small compatibility boundary with offline tests and pin the
reviewed SDK range. Validate Python 3.12–3.14 before adding the optional extra.
Do not persist raw relative `expires_in` as a fresh lifetime on every restart.
No dependency or credential implementation is introduced by this ADR.

## Private connection and packet rules

- The connection store is operator-controlled and outside tracked repositories.
  Provider endpoints, principals, tenant identifiers, credentials, and private
  aliases never enter shared policy, examples, PR comments, or cloud events.
- Authentication must establish the selected principal and workspace from
  trusted metadata. A provider response, query filter, prompt, or local alias
  cannot establish that binding. Unverified identity blocks retrieval.
- Authorize retrieval and every replay. Bind packets to the connection's
  authorization generation, current policy, repository/work item, destination,
  and expiry. Revocation, disconnect, or policy/account changes invalidate reuse.
  Packet TTL and content hashes alone do not establish authorization.
- Credentials stay with the adapter. Models receive only approved evidence.
  Source/content hashes also remain local unless a later cloud contract
  explicitly allows them. Shareable summaries use an allowlist of coarse status
  and count fields, not a redaction pass over arbitrary provider payloads.
- Enforce request, page, document, byte, and time bounds. Never follow arbitrary
  provider URLs or launch broad organization crawls. No automatic paid redispatch.
- Missing optional context leaves the ordinary workflow usable. Required context
  that cannot be validated produces an explicit incomplete/UNKNOWN outcome.
  Changed material context must invalidate prior review input even at the same
  PR head. Do not weaken existing base-ref, path, or audit sandbox protections.

The local connection lifecycle and delivery implementations must enforce these
rules; a synthetic fixture or structurally valid JSON is not authorization.

## Implemented commands

The qualified connection and delivery path is available in v1.3.1. See the
[setup guide](context-setup.md) for installation and explicit account selection:

```text
code-mower context connect coworker --connection example-context
code-mower context doctor --connection example-context
code-mower context fetch --connection example-context --request-stdin --json
code-mower context disconnect --connection example-context
```

Connection selection is independent of participant selection. Setup can select
an existing private connection without adding an account login to ordinary
Claude/Codex onboarding. Doctor must distinguish identity verification, search
availability, memory entitlement, and recipient authorization.

## Qualification boundary and implementation requirements

C1 establishes the endpoint, explicit local consent, signed identity, observed
search shape, headless refresh, and refresh-token revocation contract. The
operator explicitly authorized invalidating the temporary qualification token.
The provider accepted revocation of the refresh token and then rejected its
use with HTTP 400; the SDK cleared the in-memory credentials. The old access
token's earlier successful initialization after access-token revocation shows
why offline JWT verification is insufficient for packet replay authorization.

The connection runtime forces an online SDK refresh before every context retrieval or replay,
under a per-connection lock, and requires a successful token response with a
newly verified principal/workspace/client binding. A failed refresh invalidates
the local generation and cached evidence. Do not fall back to the old token,
a stored token file, another account, or another connection. A bare MCP GET
returns 405 even without credentials; it cannot prove authentication success.

Disconnect invalidates local state and packet access first, then revokes the
connection's refresh credential through the advertised endpoint. It must report
remote revocation failures without restoring local access. An already-issued
bearer token outside Code Mower may remain usable until its expiry; do not
promise immediate global access-token revocation. Preserve other connections.

The regression suite tests local disconnect, concurrent refresh, generation changes,
wrong-account rejection, expiry, and unavailable refresh. The retrieval adapter retains the
observed partial status and citations under strict request/document/byte/time
limits. Existing C2 tests cover structural scope/recipient rejection only;
they are not live provider authorization evidence. The [v1.3 qualification scorecard](v130-context-qualification.md) distinguishes
live delivery evidence from frozen reference assessment. Public artifacts contain no
private account identities, source text, source IDs, or credentials.

## Later Graphify candidate

Use a discriminated connection kind: remote organization or local repository.
The shared evidence contract preserves extracted/inferred/unknown confidence,
source revision, and graph build time. A consumer can distinguish matching,
stale, and unknown revision binding. Local repository context does not require
an OAuth principal or workspace.

Local repository evidence needs one rule the generic packet schema cannot
express: a citation must stay inside the indexed checkout. `context_graph`
parses repository-relative citations with optional line spans, rejects absolute
paths, parent traversal, and the indexer's own cache directories, and scores how
many line claims still resolve. Stale or unknown revision binding fails that
quality gate even when every citation resolves.

The [Graphify candidate](https://github.com/codemower-ai/code-mower/issues/876)
is **adopted as an optional, bounded local provider** behind this contract; see
the [evaluation record](graphify-evaluation.md) for the decision, the pinned
package record, and the conditions an implementing change must meet. Nothing is
installed or required yet. Synthetic graph fixtures prove only the extension
point; they do not establish Graphify compatibility or make it a v1.3.1
dependency.
