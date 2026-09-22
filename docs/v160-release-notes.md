# Code Mower v1.6.0 Release Notes

v1.6.0 improves operational clarity and adds the minimum metadata-only
telemetry needed to observe work requested through the basic private-workspace
Slack control surface. It does not add Slack-to-Board links, rich interactive
cards, per-user analytics, orchestration authority, or a new background
service.

## What changed

- **Doctor results are consistent and scoped.** Human-readable warnings now
  agree with their JSON state, hosted-only posture does not appear in an
  ordinary local adoption check, and remediation names the command that can
  establish the missing evidence (#1064 / #1099).
- **Managed Board replacement is atomic and truthful.** Replacement captures
  the prior definition, applies the new one once, and reconciles the actual
  definition, supervisor, process, listener, repository, arguments, and version.
  It reports success only when the new binding is proved, reports rollback only
  when the previous binding is proved, and otherwise returns one recovery action
  (#1082 / #1100, hardened by #1103).
- **Board inventory identifies the process the current CLI is operating.**
  `board list --repo` includes only listeners whose identity verifies the
  repository. Board inventory and lane status expose invoking, serving,
  installed, managed-service, and restart state consistently. Stale managed
  services and verified transient Boards receive exact restart or promotion
  commands (#1063 / #1109).
- **Unmanaged pull requests remain observable.** An ordinary pull request with
  no Code Mower provenance is neutral `unmanaged`; the Board and lane status
  preserve its readable pull-request and gate state. A visible malformed Code
  Mower claim remains actionable and fail-closed (#1083 / #1097).
- **Adoption diagnostics are share-safe by default.** Concise, advanced, and
  JSON adoption reports omit private local paths and identifiers unless the
  operator explicitly selects the local-only view (#1084 / #1102).
- **The Slack control surface has a closed lifecycle summary.** The additive
  `code_mower.controlSurfaceSessionSummary.v1` event carries bounded state,
  lifecycle reason, operation counts, owner-action class, pull-request presence,
  terminal duration, and normalized usage availability. It carries no command
  or message prose, answers, source, diffs, prompts, transcripts, response URLs,
  Slack identities, credentials, private paths, graph data, or raw provider
  output (#921 / #1098).
- **Emission remains capability-gated.** A client emits only after the hosted
  service advertises the exact contract version and fixture-manifest digest.
  It emits the first observation and meaningful transitions while suppressing
  timestamp-only polling and changing nonterminal elapsed-time or usage samples.
  Old clients remain compatible and an unrecognized hosted capability fails
  closed.
- **Cross-repository cost coverage is isolated and attributable.** Shared
  builder and reviewer ledgers separate explicitly different repositories, and
  a pre-PR builder record links only when its branch exactly matches one fetched
  pull request. Missing, ambiguous, malformed, and same-repository unattributable
  evidence remains fail-closed. The change adds no upload fields and exports no
  subscription access, elapsed time, token counts, source, diffs, prompts,
  transcripts, issue bodies, raw output, credentials, or local paths
  (#1106 / #1108).
- **Audit-comment history is bounded and complete.** Local audits, hosted
  labelers, lane status, gate health, and the generated gate share one
  payload-aware reader. It reduces page size and restarts safely, proves a
  stable terminal page, and fails closed on oversized items, exhausted budgets,
  omissions, duplicate or changed IDs, and incomplete history. Lineage controls
  are recognized only as exact standalone HTML comments outside fenced
  Markdown; explanatory prose is ignored while malformed controls from trusted
  authorities remain fail-closed (#1104 / #1107).

## Release entry boundary

All release entry prerequisites are complete. Board and audit-history work
merged through #1063 / #1109 and #1104 / #1107. Hosted PR #542 merged at
`bcddaa25c633f2dcf8fa2077d6ecb8004c1d8f88`, and production deployment
`6581697672` completed its two-step rollout and production deployment
`dpl_HxhK4CHrYPkCCzjxS9rGjSuBG8C8` is Ready. The authenticated health response
advertises capability schema
`code_mower.controlSurfaceSessionSummaryCapability.v1`, summary schema
`code_mower.controlSurfaceSessionSummary.v1`, capability version `1`, fixture
manifest SHA-256
`9e87c52812a49a1c72d0e0d2448661a3ef17cb8539ca8e029c6669ea9738d62e`,
and `accepting: true`. The 79/79 migration ledger digest is
`dec1a7338629e50e9edc5a927295736e4d74563775dd9af29bd38f8d3a234cdd`.
A sanitized metadata-only probe was accepted exactly once as upload
`650c9bb7-2d51-4b1e-877a-7f2bdf54c174` and remained visible only in its
`jeff-internal` tenant projection. Public evidence is #978 comment
`5770184083`.

After this release PR merges, #1105 may build the one immutable candidate. The
hosted acceptance probe does not replace the retained-candidate Slack canary;
that candidate still needs the bounded private Slack canary,
local-versus-hosted reconciliation, privacy and tenant checks, clean install,
v1.5.2 upgrade, rollback, Graphify and Board rehearsals, a
24-hour soak, two independent installation passes, exact-head release audits,
publication, and canonical reinstall. Observed results belong on #1105 and the
GitHub Release, not in this source document.

## Upgrade

Before publication, use only the retained candidate wheel selected by #1105.
After publication, install one stable tool environment and confirm its identity:

```bash
uv tool install --python 3.12 --reinstall --refresh-package code-mower \
  code-mower==1.6.0
code-mower --version
code-mower doctor --adoption --repo OWNER/REPO --concise
```

Existing repositories should preview `code-mower migration setup-drift` before
applying generated files. Follow [Install And Bootstrap](install.md) and the
[v1.6.0 qualification contract](v160-qualification.md).
