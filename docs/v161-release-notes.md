# Code Mower v1.6.1 Release Notes

v1.6.1 is a focused reliability and authorization patch for self-hosted audit
runners and the GitHub merge gate. It does not expand the v1.6.0 product,
privacy, Slack, Board, Graphify, provider-authority, or cloud-data boundaries.

## What changed

- **Runner checks use the service environment.**
  `code-mower doctor --runner --runner-env-file FILE` loads one permission-checked
  private runner environment over the ambient process environment, then runs
  the existing runner checks without serializing token values.
- **Claude recovery no longer reuses a revoked setup token.**
  The clean bounce probe removes stale Claude token and expiry overrides. Real
  audit processes still receive an explicitly configured durable token.
- **Unattended merge requires explicit authorization.**
  A green gate publishes its status normally but requests auto-merge only when
  the pull request currently has the configured `code-mower:auto-merge` label.
  The label is fetched again immediately before the head-pinned merge request.
- **Authorization reads the full label set.**
  The final label lookup is paginated, so an authorized PR with more than one
  page of labels does not fail closed merely because the authorization label is
  outside the first page.
- **Runner restart diagnosis respects platform precision.**
  macOS process start times are reported only to whole seconds. A listener and
  environment edit in the same reported second are no longer misclassified as
  stale; older listeners still fail closed.

## Upgrade

After publication, upgrade one stable tool environment and verify it:

```bash
uv tool install --python 3.12 --reinstall --refresh-package code-mower \
  code-mower==1.6.1
code-mower --version
code-mower doctor --adoption --repo OWNER/REPO --concise
```

Existing repositories should preview setup drift before applying generated
workflow updates. Auto-merge remains off until the repository owner adds the
configured label to a PR.

## Qualification boundary

The release is built once from the reviewed release PR merge SHA and qualified
as the exact retained wheel and sdist. Acceptance covers Python 3.12 through
3.14, fresh install, v1.6.0 upgrade and disposable rollback, exact private
runner-environment checks, Claude and Codex auth smoke probes, label-authorized
and unlabeled merge behavior, independent exact-head review, publication
without rebuilding, and canonical reinstall. See the
[v1.6.1 qualification contract](v161-qualification.md).
