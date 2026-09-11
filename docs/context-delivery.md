# Share optional evidence with Claude and Codex

An approved context packet can now accompany a work order and its independent
review. Both hosts use the same evidence renderer and authorization checks.
The calling host remains the orchestrator; a context provider gains no builder,
reviewer, tracker-write, or merge authority. Ordinary sessions without context
keep their existing behavior and do not load the optional Coworker SDK.

Start with a verified [private connection and bounded fetch](context-connections.md).
Fetch once for the work item, then reuse the returned opaque packet handle.
Every delivery verifies authorization online; it does not repeat the search.
Only Claude and Codex orchestrator, builder, and reviewer roles are supported
for private delivery in this release. Each role must be explicitly approved in
the private connection configuration.

## Work order and builder

`work-order draft --context-packet HANDLE` adds the opaque identity and a delivery
instruction to the existing work-order format. It adds no source text, account
binding, or new cloud telemetry fields.

Before a PR exists, the approved builder or orchestrator can receive the packet:

```sh
code-mower context deliver --packet HANDLE --connection example-context \
  --recipient codex:builder --request-stdin < /private/path/delivery-request.json
```

The private JSON request has exactly `repository`, `work_item`, and `policy`.
Use the same scope and policy as the original fetch. Evidence is written to
stdout for the approved participant's prompt. Keep it out of public terminal
logs, tracked files, PR descriptions, and shared artifacts.

## Attach evidence to independent review

After creating the PR, attach the packet to its current code head:

```sh
code-mower context attach --connection example-context --host codex \
  --repo-path /path/to/repository --base-ref origin/main \
  --request-stdin < /private/path/attachment-request.json
```

The private request has exactly `repository`, `work_item`, `policy`, `packet`
(the handle), and `pr` (a positive integer). `--host` may come from
`CODE_MOWER_HOST`. Use `origin/develop` when that is the trusted target branch.

The GitHub actor must already be a configured control authority: the trusted
base's `owner_surface.owner_login` or `decisions.authorities`, or the equivalent
trusted runtime setting. Configure the same authority for the gate and local
auditor. PR-supplied configuration cannot grant that authority. Updating existing
repositories requires the current generated gate workflow and support helpers.
If trusted repository policy requires context, both audit wrappers and the
generated gate reject a review without its first required input declaration.
If local policy discovery itself fails, ordinary audits remain usable; explicit
input revisions and known declarations still require context. The generated
gate independently enforces required policy and rejects a code-only PASS.

Attachment sets `code-mower/gate` pending and publishes a small control comment
containing only a random input revision, code head, required/available state,
and expiry. The packet hash, handle, account, alias, query, citations, and source
text stay local. A failed or uncertain publication leaves the local binding
unusable. Explicit attachment always creates a new review input revision.

Run the usual independent audit on the machine that holds the private connection:

```sh
code-mower claude-audit --repo owner/repo --pr 42 \
  --repo-paths owner/repo:/path/to/repository --base-ref origin/main \
  --context-revision REVISION
```

The corresponding `codex-audit` command uses the same options. The explicit
revision is optional: both wrappers discover current control declarations from
trusted authorities. It is useful when dispatching a specific work order.
Use `--context-state-dir` for a nondefault private store. A remote runner without
the selected authorization cannot review private context; it reports UNKNOWN.
Do not copy credentials or private packets into GitHub Actions to bypass this.

Both wrappers frame source material as evidence outside trusted project doctrine
and owner decisions. They preserve citations, uncertainty, and contradictions.
The model receives neither credentials nor account-binding metadata. Existing
sandbox and ambient-MCP restrictions remain in force.

## Findings, refresh, and failure

Before accepting the verdict, the wrapper checks the current code head, current
input revision, packet identity, expiry, and fresh online authorization again.
Missing required context, authorization failure, or changed input yields UNKNOWN.
An explicitly declared optional-unavailable input can receive an ordinary code
review, but that review must still name its input revision. Neither wrapper
silently drops selected private evidence to fit a truncated diff.

When private evidence was supplied, public review comments and saved verdict
artifacts contain only review status, severity counts, input metadata, and the
standard audit provenance. Model-authored findings stay in the protected local
binding because they may quote private sources. Raw CLI sidecars are disabled
for this path. Retrieve detailed findings for an approved participant with:

```sh
code-mower context feedback --revision REVISION --reviewer claude \
  --recipient codex:builder --repo-path /path/to/repository
```

This command checks the current PR input and authorization before printing
private findings. The analogous `context deliver --revision REVISION` prints
the evidence itself. Both have private stdout.

Changed material context requires an explicit fetch with `--refresh`, followed
by attachment and a new audit, even when code is unchanged. A changed code head
also requires a new attachment. The gate checks both identities; an old PASS
cannot satisfy a newly attached input. UNKNOWN from a later authorization check
supersedes a prior PASS on the same input. Context-bound verdict artifacts cannot
be reposted offline; run a fresh authorized audit.

Gate status is updated by GitHub events, not a continuous authorization monitor.
Do not treat a previously displayed green status as permission to replay expired
evidence. Recheck the selected input when resuming work. Deleting or manually
rewriting control comments is not a supported way to change the selected input;
use the context commands and review the new revision.

Refresh, eviction, and disconnect delete the packet's local delivery bindings
and private findings. At most eight bindings are retained per packet. Deletion
cannot recall evidence already sent to an approved model. Provider billing that
was not returned stays unknown; replay performs authorization, not another
organization search.

The synthetic local-repository graph fixture uses the common packet validation
and evidence renderer for all six roles without OAuth identity fields. This
preserves an extension point for later Graphify evaluation; it does not install
or qualify Graphify in v1.3.0.
