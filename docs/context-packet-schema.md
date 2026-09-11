# Optional context packet contracts

Implementation step [C2](https://github.com/codemower-ai/code-mower/issues/870)
in [epic #868](https://github.com/codemower-ai/code-mower/issues/868).
These contracts underpin the optional [connection](context-connections.md),
retrieval, and [participant delivery](context-delivery.md) paths. A packet alone
does not select a review input: attach it through the delivery command.

`code_mower.context_contract` uses only the Python standard library. Existing
configs and external file manifests are unchanged when context is absent.

## Shared policy

The optional `context` block is closed to unknown fields. A generic logical
connection reference maps to an operator-controlled private connection; it must
not name an account or contain a personal alias.

```yaml
context:
  schema: code_mower.contextPolicy.v1
  connection: example-context
  policy_version: v1
  required: false
  max_documents: 5
  max_document_bytes: 20000
  max_text_bytes: 80000
  max_packet_bytes: 262144
  max_requests: 3
  max_pages: 2
  timeout_seconds: 30
  max_age_seconds: 3600
```

All limits have these defaults and hard ceilings in the validator. Integer
strings from Code Mower's YAML parser are accepted; booleans are not integers.
Packet loading enforces file/document/text limits and age. A future retrieval
adapter must enforce requests, pages, and elapsed time during retrieval.
Changing recipient or account policy must change the authorization generation;
changing shared policy must change `policy_version` and invalidate old packets.

## Private connection envelope

`code_mower.contextConnection.v1` requires capability version `1`, a logical
`connection`, `provider`, `kind`, authorization `generation`, `state`, `identity`,
allowed `repositories` and `recipients`, `expires_at`, and `capabilities`.
Capabilities separately declare boolean `search`, `memory`, and
`revision_binding` support. Unsupported schema/capability versions fail closed.

- `kind: organization` has identity fields `principal`, `workspace`, `endpoint`.
- `kind: repository` has only `repository_root`, an absolute local path. It does
  not require a SaaS principal, tenant, or OAuth credential.

Only a trusted runtime authorization callback may provide this envelope. Its
`verified` state asserts that the connection adapter has freshly verified the
selected authorization, not that JSON validation established identity. Provider
search output, PR files, and saved packets are not valid authorization callbacks.
The connection lifecycle step must implement authoritative identity verification,
refresh/revocation, endpoint validation, and secure credential storage.

## Packet and manifest extension

`code_mower.contextPacket.v1` contains:

| Field | Meaning |
| --- | --- |
| `capability_version` | Required integer `1`. |
| `provider`, `kind` | Must equal the trusted connection. |
| `retrieved_at` | Timezone-aware retrieval timestamp. |
| `source_revision`, `source_built_at` | Nullable revision and build timestamp; unknown remains explicit. |
| `completeness`, `truncated` | `complete`, `partial`, or `unknown`; truncated evidence cannot be complete. |
| `documents` | Bounded list of text with citations and `extracted`, `inferred`, or `unknown` confidence. |
| `binding` | Private connection/identity/generation, repository/work item, policy version, recipients, and expiry. |

Each citation has bounded `source` and `title` strings. A source is a locator for
evidence, not a URL to fetch automatically. This contract does not establish
that the source is correct, safe to render as a link, or independently verified.
Confidence and revision binding remain separate: inferred evidence can come
from a matching revision; extracted evidence can be stale. Unknown revision is
not silently treated as current. Required completeness/freshness decisions
belong to the later delivery step.

The existing `code_mower.externalContextManifest.v1` accepts an additive
`provider_packets` list, with at most 16 references. Each reference contains a
private-store-relative `path` and SHA-256 of the exact packet file bytes.
`context add --external` preserves these references but does not read them or
convert them to trusted previews. No public/cloud reporting should serialize
this private extension. Old manifests without it retain their original shape.

The trusted runtime supplies the private store root, reference/hash, policy,
request scope, and live authorization callback to `load_packet`. A hash supplied
by a PR is not trusted just because it matches a file. Descriptors resolve paths
beneath the private root without following symlinks; directories and files must
belong to the operator and allow no group/other access. FIFO/device inputs,
traversal, invalid JSON, duplicate keys, oversized bytes, and changed hashes fail
closed. Platforms without secure descriptor-relative opens are unsupported by
this initial storage reader.

Load again before each delivery/replay. The loader verifies authorization before
reading, then checks connection identity/generation, repository, work item,
policy, recipients, expiry, and content integrity. It returns an immutable byte
snapshot; extracting a private payload creates a new copy. This snapshot is not
a reusable authorization token. The live connection callback must reject a
revoked/disconnected account even if the packet's TTL has not elapsed.

## Evidence and reporting boundary

Private packets contain untrusted evidence, never project doctrine or executable
instructions. The delivery step must frame that distinction while preserving
the existing trusted-base audit policy, sandbox, and ambient-MCP restrictions.
It must give approved participants the same packet identity, invalidate review
for changed material context, and treat unavailable required inputs as
incomplete/UNKNOWN. The [delivery layer](context-delivery.md) enforces those
requirements; this validator alone does not dispatch an agent or approve review.

`ValidatedPacket.shareable_summary()` returns only the fixed summary schema,
kind, document count, completeness, truncation, and revision state. It omits
provider names, private aliases, identities, source links, text, paths, work-item
details, and digests. Do not export `private_payload()` or a connection mapping.

The organization and local-graph fixtures in `tests/test_context_contract.py`
are entirely synthetic. They validate the extension point without importing
Graphify, assuming a Coworker tool schema, or making network requests.
