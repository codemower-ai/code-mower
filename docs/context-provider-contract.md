# Context provider contract for v1.3

Status: architecture accepted; live Coworker login, identity, OM2 search, and
headless refresh qualified on 2026-09-11. Revocation testing remains pending. Tracks [#869](https://github.com/codemower-ai/code-mower/issues/869)
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
| Revocation | Discovery advertises `/oauth/revoke`; live revocation has not run. | Keep revocation behavior unqualified. Local disconnect must invalidate packet generations immediately; JWT expiry alone does not prove continuing authorization. |
| Usage | The server reported search processing time, but no price or charge. | Cost remains unknown. No broader search, pagination, automatic retry, or extra source retrieval was performed. |

The probe made one `individual_context` call and one `om2_search` call, with
30-second read deadlines. The search returned 445 bytes of result text and
source identifiers/titles; no source documents were fetched. Authentication,
JWKS retrieval, initialization, and tool discovery were separate from those
data calls. A listing exposed 71 tools, including writes, so production code
must select a small explicit allowlist rather than trust `readOnlyHint` alone.
No Jira or other connected-source mutation was performed.

The selected search variant returned Attributes; the advertised SemanticUnit
variant and `om2_source_trace` are not live-qualified. The initial adapter should
support the verified search result shape and reject unsupported variants until
separately tested. Never pass an Attribute ID to a SemanticUnit-only tool.

The [synthetic contract fixture](../tests/fixtures/coworker_mcp_contract.json)
records stripped input schemas and an invented response with the observed
shape. It is not an export of private content and does not establish live
capabilities by itself.

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

## Proposed commands

These are design targets, not commands available in v1.2.2:

```text
code-mower context connect coworker --connection example-context
code-mower context doctor --connection example-context
code-mower context fetch --connection example-context --work-order PATH
code-mower context disconnect --connection example-context
```

Connection selection is independent of participant selection. Setup can select
an existing private connection without adding an account login to ordinary
Claude/Codex onboarding. Doctor must distinguish identity verification, search
availability, memory entitlement, and recipient authorization.

## Remaining qualification

The endpoint, consent, signed identity, result shape, and headless refresh
checks above replace the earlier setup-details blocker. The operator approved
the intended local account connection. That authorization and account identity
are deliberately not copied into public fixtures or configuration.

Live revocation testing awaits explicit approval because it can invalidate the
temporary probe connection and require signing in again. C1 remains open until
that result or an explicit qualification exception is recorded. Do not claim
that remote revocation is immediate based on a successful revocation HTTP
response alone: test whether the old credential is actually rejected.

C3 must test local disconnect, generation changes, wrong-account rejection,
expiry, and unavailable refresh. C4 must retain the observed partial status and
citations under strict request/document/byte/time limits. Existing C2 tests
cover structural scope/recipient rejection only; they are not live provider
authorization evidence. Production code must follow the verified contract
rather than hypothetical output shapes in tool descriptions.

## Later Graphify candidate

Use a discriminated connection kind: remote organization or local repository.
The shared evidence contract preserves extracted/inferred/unknown confidence,
source revision, and graph build time. A consumer can distinguish matching,
stale, and unknown revision binding. Local repository context does not require
an OAuth principal or workspace.

The [Graphify candidate](https://github.com/codemower-ai/code-mower/issues/876)
is later v1.3.x work. Synthetic graph fixtures prove only the extension point;
they do not establish Graphify compatibility or make it a v1.3.0 dependency.
