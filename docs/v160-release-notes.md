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
- **Unmanaged pull requests remain observable.** When no lineage policy is
  configured, the Board and lane status preserve readable pull-request and gate
  state while labeling lineage as optional. A configured lineage policy still
  fails closed (#1083 / #1097).
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

## Release entry boundary

The source preparation does not establish release acceptance. Issue #1105 may
build the one immutable candidate only after all three entry gates are complete:

1. #1063 merges Board inventory filters, invoking/serving version parity, stale
   service detection, and exact service guidance.
2. #1104 merges payload-aware audit-comment ingestion and reserved lineage
   control parsing.
3. CodeMower.com #978 deploys the backward-compatible consumer and advertises
   the exact accepted OSS contract identity.

After those gates merge, the retained candidate still needs the bounded private
Slack canary, local-versus-hosted reconciliation, privacy and tenant checks,
clean install, v1.5.2 upgrade, rollback, Graphify and Board rehearsals, a
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
