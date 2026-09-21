# Code Mower v1.5.2 Release Notes

v1.5.2 is a maintenance release that makes the repository's documentation,
release identity, and packaged setup files simpler to keep current. It retains
the v1.5.1 product boundary: Claude and Codex remain the default participants,
Graphify remains optional, and Slack remains the basic private-workspace control
surface. Slack telemetry, hosted aggregation, Board links in Slack, and richer
Slack UX remain planned for v1.6.0.

## What changed

- **Documentation has explicit ownership.** Every Markdown page is classified
  as canonical, supporting, frozen, or archived. CI rejects missing entries,
  duplicate canonical subjects, edits to frozen evidence, broken local links,
  and copied current package pins in supporting guides.
- **The maintained user journey is shorter.** The README and generated
  documentation index route readers through one install guide, quickstart,
  upgrade guide, Board runbook, and troubleshooting guide. Supporting pages
  link to those owners instead of repeating their commands.
- **Release facts have one source.** `release.yml` owns the current version,
  tag, package pin, Python matrix, release documents, and package inventory.
  Candidate construction, readiness checks, rendered documentation regions,
  and the installed-wheel rehearsal consume that manifest.
- **Package templates have one source.** `src/code_mower/templates/` is the only
  authored template tree. Consumer-facing projections are produced during
  package materialization, required source files fail clearly when absent, and
  a fresh-package parity check detects drift.

The changes remove thousands of mirrored lines and make routine release and
documentation updates deterministic. They do not add a service dependency,
change provider authority, expand cloud data, or broaden Slack access.

## Upgrade

Install or upgrade one stable tool environment, then confirm its identity:

```bash
uv tool install --python 3.12 --reinstall --refresh-package code-mower \
  code-mower==1.5.2
code-mower --version
code-mower doctor --adoption --repo OWNER/REPO --concise
```

A laptop may use `pipx`; a hosted machine with neither tool can follow
[Install And Bootstrap](install.md). Existing repositories should preview
`code-mower migration setup-drift` before applying generated files.

## Qualification boundary

The release is built once from the reviewed release PR merge SHA and qualified
as the exact retained wheel and sdist. Acceptance covers documentation
lifecycle validation, rendered release facts, package-template projection,
fresh install, v1.5.1 upgrade and disposable rollback, Python 3.12–3.14,
Graphify reader compatibility, basic offline Slack checks, exact-head review,
publication without rebuilding, and canonical reinstall. The immutable
contract is [v1.5.2 qualification](v152-qualification.md).
