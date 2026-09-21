# Control-surface session summaries

This supporting contract describes the metadata-only lifecycle summaries used
by the optional [Slack integration](slack-setup.md). Installation and Slack
administration remain in the Slack guide. General upload behavior remains in
the [cloud data contract](cloud-data-contract.md), and local work presentation
remains in the [Board data contract](board-data-contract.md).

## Boundary

`code_mower.controlSurfaceSessionSummary.v1` is a closed specialization of the
existing `code_mower.benchmarkEvent.v1` envelope. It reports one meaningful
session observation rather than a command, message, or transcript stream. The
packaged schema and fixtures are the normative machine-readable contract:

- `control_surface_session_summary.schema.json`;
- `control_surface_session_summary.accepted.json`;
- `control_surface_session_summary.rejected.json`;
- `control_surface_session_summary.expectations.json`;
- `control_surface_session_summary.fixture-manifest.json`.

The summary allows an opaque Code Mower correlation key, repository slug,
categorical provider, lifecycle state and outcome, observation time, operation
counts, bounded owner action, optional pull-request metadata, and already
available elapsed-time or Devin ACU measurements. The producer hashes its
local logical session into a 32-character key before building the event. It
never forwards a Slack identity or provider session reference.

The closed validator rejects unknown root, dimension, and metric fields. Task
text, messages, answers, prompts, response URLs, Slack identities, source,
diffs, transcripts, tokens, paths, context or graph data, provider references,
and raw output cannot enter this event.

## Local Board projection

Slack-requested work uses the existing public remote-session lifecycle and
local Board adapter. The adapter maps `pending`, `running`, owner-waiting,
completion, failure, suspension, termination, archival, and uncertainty into
the same Board phases used by other remote work. This is local observation;
it does not require cloud access and grants no dispatch or merge authority.

## Hosted capability gate

Production emission stays disabled until the hosted service advertises this
exact closed object:

```json
{
  "accepting": true,
  "capability_version": 1,
  "fixture_manifest_sha256": "<sha256 of the exact packaged fixture manifest bytes>",
  "schema": "code_mower.controlSurfaceSessionSummaryCapability.v1",
  "summary_schema": "code_mower.controlSurfaceSessionSummary.v1"
}
```

Every field must match, and unknown fields fail closed. A missing capability,
version mismatch, digest mismatch, or `accepting: false` leaves operation local
only. Client rollback stops new emission without rewriting accepted rows.
The client reads this object from
`GET /api/health` at `capabilities.control_surface_session_summary`; the cloud
doctor exposes an accepted exact match in its service-check detail.

After acceptance, the producer emits the first observation and then only a
meaningful lifecycle, operation-count, owner-action, or pull-request change.
Timestamp-only changes and changing elapsed time or usage on a nonterminal
session do not create another event. Terminal elapsed-time or usage changes
remain meaningful reconciliation evidence.

The hosted service must vendor the five packaged resources byte-for-byte,
scope repository identity through authenticated tenant policy, and implement
the retention, export, deletion, tenant-isolation, and aggregate-reconciliation
expectations in the packaged expectations fixture.

## Contract changes

The fixture manifest hashes exact UTF-8 file bytes. It does not hash itself;
the hosted capability advertises the SHA-256 of the manifest bytes separately.
After qualification, changing the schema or any fixture requires a new schema
or manifest version. Do not refresh hashes in place to accept changed meaning.
