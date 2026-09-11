# Context provider contract for v1.3

Status: architecture accepted for offline implementation; Coworker protocol and
account qualification pending. Tracks [#869](https://github.com/codemower-ai/code-mower/issues/869)
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

Public documentation checked on 2026-09-11 is a provider claim, not evidence that
the selected account has a capability. No authenticated canary has run.

| Capability | Public documentation | Observed contract / remaining verification |
| --- | --- | --- |
| Endpoint and transport | The MCP page displays `mcp.coworker.ai`. | That hostname did not resolve from the qualification host. The supported setup URL, path, and transport remain unverified. |
| Authentication | Existing Coworker login/SSO and customer admin enablement are described. | Verify discovery, registration, login, refresh, expiry, and revocation with the selected connection. |
| Identity and source scope | Queries are described as permission-aware. | Establish authoritative principal/workspace metadata and source scope; an email entered by an operator is not proof. |
| Search | The MCP page describes search/retrieval and also advertises dedicated agents. | Discover exact tool names and input/output schemas. Approve individual read operations; exclude generic agent invocation and writes. |
| Organizational memory | The OM2 page lists Enterprise availability. | Verify memory entitlement independently of MCP/search access. |
| Citations and completeness | No sufficient tool schema was found in the reviewed pages. | Verify source identifiers, timestamps, citations, pagination, truncation, and errors. |
| Usage | Account-specific request and billing limits were not established. | Record observed usage or unknown; do not infer free retrieval. |

Sources: [Coworker MCP](https://coworker.ai/mcp) and
[organizational memory](https://coworker.ai/organizational-memory).

The unauthenticated discovery attempt stopped on DNS resolution failure before
any provider authentication or data retrieval. This is a local observation,
not a claim that the service is unavailable to every customer. Do not derive a
production endpoint from the displayed hostname or substitute another account.

## MCP client choice

Use the official [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
as the first implementation candidate, installed through an optional extra with
a reviewed version range. Its current stable line is v2, with standard client
transports; the v1 maintenance API must not be assumed to be the current API.
Use the maintained client/auth mechanisms rather than hand-written OAuth or
copying another host's credential cache. Do not install the SDK's CLI extra
unless a demonstrated runtime need requires it.

The final version pin and auth integration depend on the live contract probe.
Validate compatibility with Code Mower's supported Python versions and the
provider's transport before adding the optional dependency. No dependency or
credential implementation is introduced by this ADR.

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

## Bounded qualification and remaining owner action

1. Obtain the customer-admin-supported MCP setup URL and enablement instructions.
   Keep account details local; never paste a token or password into an issue.
2. Authenticate the intended local connection and establish trusted principal
   and workspace verification. If the provider lacks such a mechanism, document
   the limitation and keep the connection unusable pending a supported solution.
3. Inspect the actual schemas and select a read-only allowlist. Run one relevant
   search and at most one cited retrieval, with at most five documents, 20 KB of
   normalized text, and a 30-second retrieval deadline. Discovery/auth requests
   are separately bounded and recorded. Stop on unsupported schemas or errors.
4. Test refresh/revocation and both permitted and rejected recipient/scope cases.
   Record sanitized schemas and metadata only. Report search and memory results
   separately, with observed cost or unknown.

Step C1 remains open until this evidence exists. C2 can proceed with clearly
synthetic organization and repository-graph fixtures while access is pending.
Production C3/C4 tool names, endpoints, and identity claims must follow verified
provider behavior rather than the synthetic fixtures.

## Later Graphify candidate

Use a discriminated connection kind: remote organization or local repository.
The shared evidence contract preserves extracted/inferred/unknown confidence,
source revision, and graph build time. A consumer can distinguish matching,
stale, and unknown revision binding. Local repository context does not require
an OAuth principal or workspace.

The [Graphify candidate](https://github.com/codemower-ai/code-mower/issues/876)
is later v1.3.x work. Synthetic graph fixtures prove only the extension point;
they do not establish Graphify compatibility or make it a v1.3.0 dependency.
