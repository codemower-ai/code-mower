# Code Mower v1.5.1 Release Notes

v1.5.1 is a focused reliability release based on five independent v1.5.0
adoption passes. It keeps the v1.5.0 product boundary: Claude and Codex remain
the default, Graphify stays optional, and Slack remains the basic private
workspace control surface. Slack telemetry, hosted aggregation, Board links in
Slack, and richer Slack UX remain deferred to v1.6.0.

## What changed

- **Hosted installation is explicit.** The docs provide a Python-based uv
  bootstrap for minimal machines, distinguish a remote observer from a local
  operator, and state the evidence and role boundaries for dry runs.
- **Tests are host-independent.** Release workflow subprocess tests use the
  interpreter running the suite and disable interactive Git credential prompts.
- **Audit publication is lane-exact.** Reviewer seals bind to the exact source
  job, matrix lane, run, and attempt. A Codex lane can self-exclude while a
  Claude lane in the same workflow publishes independently.
- **Remote observers no longer need a checkout.** `code-mower doctor --adoption
  --orchestrator-only --repo OWNER/REPO` uses a labeled packaged-starter plan,
  keeps irrelevant local checks quiet, and redacts local paths from its
  share-oriented result.
- **Initialization protects repository policy.** `code-mower init --easy`
  detects an existing root `code-mower.yml`, refuses an implicit packaged
  starter beside it, prints exact recovery commands, and emits each selected
  lane configuration once.
- **Status is more precise.** Missing optional lineage policy no longer hides a
  readable PR or green gate. Terminal and Board views distinguish optional from
  unreadable lineage, render empty recent workflows as `none`, and show
  `Serving version: …` in the primary Board header.

## Upgrade

Install or upgrade one stable tool environment, then confirm its identity:

```bash
uv tool install --python 3.12 --reinstall --refresh-package code-mower \
  code-mower==1.5.1
code-mower --version
code-mower doctor --adoption --repo OWNER/REPO --concise
```

A laptop may use `pipx` instead; a hosted machine with neither tool can follow
[Install And Bootstrap](install.md). Existing repositories should preview
`code-mower migration setup-drift` and `code-mower init --easy` before applying
generated files.

## Qualification boundary

The release is built once from the reviewed release PR merge SHA and qualified
as the exact retained wheel and sdist. Acceptance covers fresh hosted install,
v1.5.0 upgrade, remote observer, safe init, transient and persistent Board,
Graphify, basic Slack, single-lane and multi-lane audit publication, one bounded
hosted Board canary, metadata-only codemower.com upload, canonical PyPI install,
and GitHub Release identity. The immutable contract is
[v1.5.1 qualification](v151-qualification.md).
