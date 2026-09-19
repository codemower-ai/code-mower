# Code Mower v1.5.0 Release Notes

v1.5.0 adds the basic supervised private-workspace Slack boundary and the
Graphify compatibility changes below. The install identity is
`code-mower==1.5.0`. Claude + Codex remain the default. Default dependencies are
only PyYAML and packaging: no Slack SDK, login, network setup, service, Graphify
installation or provider dispatch is added by installing Code Mower.

## Included behavior

- Explicit `code-mower slack setup --manifest slack-app.json --yes` creates the
  hosted manifest privately and exclusively. `slack doctor` and `doctor --slack`
  provide bounded, redacted diagnostics. An offline snapshot cannot establish
  live readiness or authorize dispatch (#1024).
- Supervisor v2 retains the original claim, lease, provider binding and
  cumulative caps across checkpointed clarification/fix requests (#1017).
  The v1 contract remains frozen. Public code is the contract/setup surface;
  hosted administration, credentials and deployment remain separately owned.
- Exact-head audit publication is reliable across repository dispatch, with
  bounded refusal diagnostics (#1025 / #1026 / #1030).
- Explicit headless Codex campaign file authentication, clearer setup-drift
  operands, concise adoption doctor guidance, bounded Board startup observation
  and canonical local-lane writer identities carry forward the accepted fixes
  listed in the 1.5.0 CHANGELOG.

## Graphify compatibility and upgrade

PR #1007 contributes four behaviors:

1. A separate bounded **16 MiB provider inventory** reader, retaining the
   **256 KiB generation-manifest** bound and existing hash/coverage checks.
   Oversized provider inventories refuse publication explicitly.
2. `doc_ref` nodes are declared non-code exclusions; their incident edges cannot
   become code query results or citations. Unknown types still fail validation.
3. Related-test queries recognize JavaScript/TypeScript `.test`, `.spec` and
   `__tests__` conventions and follow import relationships. Import evidence
   does not establish execution coverage.
4. Acquisition guidance covers language parser extras, supported runtime
   ownership and the single-worker option without weakening containment.

Merged PR #1031 (#1029) additionally makes build/refresh/status/connection-status
readiness agree with the installed query reader. Compatibility diagnostics name
the upgrade/rebuild action without disclosing graph content or local paths.
Generation completeness and query completeness are separate: a bounded partial
answer remains available and discloses its limits; it does not make a complete
generation incomplete.

The separate provider pin remains `graphifyy==0.9.58`. Upgrading Code Mower does
not rewrite existing graphs. Inspect `code-mower context-graph status --json`
and explicitly `code-mower context-graph refresh` affected/partial generations
(for example, a refused oversized inventory or inputs skipped for missing
parsers). A usable unaffected generation needs no rebuild merely because its
query reached a traversal limit. See [Graphify setup](graphify-setup.md).

## Qualification and boundaries

The [qualification contract](v150-qualification.md) separates pre-merge rehearsal,
the immutable merge-SHA candidate, private acceptance (#918), one accepted
completion and one accepted confirmed-cancellation outcome (#920), with every
attempt and reservation count-preserved, and publication/reinstall (#923).
The final candidate normally supplies the paid canary bytes. For this v1.5.0
closeout, the contract also permits a machine-verified carry-forward from a
retained ancestor candidate only when every wheel member outside the closed
audit/release/documentation set is byte-identical, both immutable audit receipts
replay successfully, and the exact final wheel repeats private no-provider
acceptance. Any Slack, supervisor, provider, CLI, persistence, state, dependency
or entry-point difference requires newly authorized canaries.
Only explicitly observed outcomes count. Sanitized outcomes belong on #923 and
the GitHub Release rather than in the qualified source.
These notes claim no paid or live hosted result. Canaries require numeric task
and aggregate authorization.

Slack scope is one private workspace, one private unshared channel, authorized
repository aliases, private replies, start/status/answer/confirmed cancellation,
qualified Codex supervision and bounded hosted Devin execution. Registration is
not qualification; acknowledgement is not provider exit or settled billing.
Slack telemetry/Board/cloud links and richer UX remain v1.5.1. No Slack Connect,
public channel, unrestricted execution or peer-orchestrator Devin is implied.

Disposable package rollback to exact 1.4.2 is an installation rehearsal only.
**Never downgrade live durable v2 state.** Disable admission, reconcile original
work and confirmed exits, preserve claims/receipts/reservations, and restore only
a reviewed compatible deployment through its owner-controlled rollback. Follow
[Slack upgrade, disable, rollback and uninstall](slack-setup.md#troubleshooting-upgrade-and-removal).

No private graph, query, source, task prose, credentials, mappings, provider
output or adoption evidence belongs in public artifacts. Historical v1.4.x
release notes, qualification records and published artifacts remain unchanged.

The immutable v1.5.0 candidate must be built from the final reviewed release
source after every required qualification ancestor, including #1043 and the
final closeout change. Publication attaches that retained artifact pair to the
GitHub Release, verifies the non-publishing release-event run and assets, and
removes the temporary candidate-run repository variable.
