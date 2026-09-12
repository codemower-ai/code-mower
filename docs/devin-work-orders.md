# Trusted work orders for hosted Devin

`code_mower.devin_work_orders.DevinWorkOrders` is a packaged, stdlib-only library
seam for dispatchers that already authenticate Code Mower work orders. It uses the
existing `RemoteSessions`, `DevinProvider`, organization-scoped v3 client, private
`ContextStore`, work-order manifest schema, and provider transport identity.
It does not change campaign dispatch, release policy, review authority, or lane CLI
capability declarations. No new lifecycle or automatic polling daemon is introduced.

The caller must first apply its trusted issue-author/work-order-comment policy and
acquire the repository's existing builder lease. A work-order manifest is not proof
of authorization. Pass the approved Markdown body explicitly; the library does not
open manifest paths, load context files, or fetch an untrusted issue body. Repository,
issue, branch, base, and expected GitHub author ID/login are dispatcher policy inputs.

```python
from code_mower.devin_sessions import DevinClient
from code_mower.devin_work_orders import DevinWorkOrders, WorkOrder
from code_mower.github_builder_evidence import GitHubBuilderEvidence

# Inputs below come from the authenticated dispatcher, not a provider response.
order = WorkOrder.from_manifest(
    approved_manifest, approved_markdown,
    repository="owner/repo", issue=907, branch="devin/907", base="main",
    author_id=expected_github_user_id, author_login=expected_github_login,
    acu_limit=5,
)
builder = DevinWorkOrders.hosted(
    private_state_root,
    DevinClient(organization_id, devin_service_user_key),
    GitHubBuilderEvidence(github_read_token),
)
preview = builder.run("dispatch", order)  # no filesystem or network operations
# An embedding CLI must map explicit --apply to apply=True. Never default it on.
metadata = builder.run("dispatch", order, apply=apply_from_cli)
metadata = builder.run("status", order)
metadata = builder.run("collect", order, apply=apply_from_cli)
```

Use a stable, private, operator-owned state root outside any checkout, with no
symlink components. All dispatchers must share the same roots. The library reserves
both repository/issue and repository/branch durably. A changed body, author, branch,
account, or cap cannot silently replace an existing binding. Reservations are not
released automatically, including after cancellation. Do not delete state to retry
an uncertain dispatch: that defeats paid-create reconciliation and writer ownership.
Provider branch restrictions are instructions, not a GitHub authorization mechanism;
use restricted repository credentials and the existing dispatcher lease as well.

Dispatch requests contain exactly one repository, a 1–100 ACU session cap, the
approved order, branch/write policy, and `code_mower.builderCompletion.v1`. The existing
v3 client adds a unique `cm-<uuid>` tag and fsyncs its create checkpoint before the paid
POST. Lost responses use `status` and bounded existing reconciliation, never a second
create. No match, multiple matches, or incomplete enumeration requires inspection.
The cap applies to the same session across clarification and fix rounds.

Completion has exactly `schema`, `round`, `repository`, `issue`, `pr_number`, and
`head_sha`. It is collected in the remote session's private artifact. Provider PR URLs,
messages, prose, extra fields, and provider assertions of verification are not evidence.
A missing result remains unavailable; malformed or stale results fail closed.

The GitHub adapter makes one query for at most two PRs on the exact head branch,
including closed/merged PRs, then two fresh reads of the claimed PR. Each call has a
30-second deadline, a 512 KiB response bound, no redirects, no retries, and no automatic
pagination. It requests metadata only. Incomplete candidate or closing-issue pages,
multiple candidates, extra issue links, wrong repository, forks, wrong author numeric
ID/login, wrong base/head branch, non-open PRs, and any disagreement about the exact
40-character head SHA fail closed. Later rounds must retain the original PR number.
`GitHub` can instead be implemented by an embedding client under the documented bounded
protocol; it must query GitHub independently, never normalize provider assertions into
observations. The built-in adapter's `runner(query, variables, headers)` and the Devin
client's `api_runner` support entirely offline tests.

Only a successful, freshly verified `collect` returns `verified_pr`. `status` never
replays cached PR evidence. Returned builder evidence includes the maintained provider
identity, repository/issue/PR number, trusted numeric author ID, exact SHA, round, cap,
and observed ACU, and explicitly has no merge authority. The ACU observation comes from
[Devin's organization session consumption endpoint](https://docs.devin.ai/api-reference/v3/consumption/organizations-consumption-daily-sessions),
which requires `ManageBilling`. A billing-read failure withholds completion evidence;
status and cancellation remain usable. Observed consumption is a billing snapshot,
not a promised final invoice or a value supplied by the completion object.

Clarification and fix rounds use existing remote messages:

```python
builder.run("clarify", order, request="clarification-1", prose=approved_answer,
            apply=apply_from_cli)
builder.run("fix", order, request="audit-round-1", prose=approved_findings,
            reviewed_head=exact_reviewed_sha, apply=apply_from_cli)
builder.run("cancel", order, request="cancel-1", apply=apply_from_cli)
```

A fix requires a previously verified completion and fresh GitHub verification of the
reviewed SHA. New messages invalidate private collected output and advance the completion
round before delivery; an earlier round cannot become new builder evidence. Request keys
are stable across retries, bounded to 100 rounds, and cannot be reused for different
input. A lost message/cancel response follows the remote-session inspection and explicit
`acknowledge_delivered=True` flow, with the same command, request and original input;
there is no automatic resend. Cancellation never releases the branch for another writer.

Only returned allowlisted metadata may reach Board/cloud/telemetry consumers. Never emit
order objects, prompts, state records, private artifacts, adapter responses, or credentials.
The library neither logs nor prints them. GitHub evidence is a point-in-time observation:
review/merge consumers must independently recheck the exact head immediately before their
action. No cross-service atomic snapshot is possible. Provider behavior, billing permissions,
and a real paid session remain for separately authorized, bounded live acceptance after merge.
