# Code Mower Mirror-Removal Runbook

Use this runbook when a product repository already carries repo-local Code
Mower tool mirrors and you want to migrate to the standalone package without
blocking product velocity.

## States

### Shadow

The product repo keeps local mirrored implementation files, but also carries:

- `tools/code_mower`
- `tools/code_mower_standalone_shadow.sh`
- `tools/code_mower_standalone_pin.env`

Run:

```bash
code-mower migration wrapper-rehearsal --repo-path /path/to/product-repo --json
```

Expected result: `status: pass` and `mismatch_count: 0`.

### Standalone Default

The product wrapper prefers the pinned standalone checkout/package. The local
mirror remains only as an explicit fallback for emergencies or private-repo
Actions limitations.

Run:

```bash
code-mower migration package-install-rehearsal \
  --package-spec "git+https://github.com/codemower-ai/code-mower.git@v0.5.0-alpha.44" \
  --repo-path /path/to/product-repo \
  --json
```

During alpha testing, use a local path or git URL for `--package-spec`.

### Mirrors Removed

The product repo has removed mirrored implementation files such as:

- `tools/code_mower_*.py`
- `tools/*_audit_pr.py`
- `tools/*_labeler.py`
- `tools/lane_prompts/*.md`
- `tools/CODE_MOWER*.md`

The thin wrapper and pin files stay in place until the package is consumed
through a boring public install path.

For new repos or follow-up cleanup after mirror removal, generate the support
files instead of hand-copying them:

```bash
code-mower init --easy --apply --output-dir .code-mower.generated
```

Review `.code-mower.generated/tools/code_mower*`,
`.code-mower.generated/tools/run_*_audit_pr.sh`, and
`.code-mower.generated/tools/safe_gh_comment.py`, then copy only the support
files your repository actually needs. Before copying
`code_mower_standalone_pin.env`, replace the placeholder repository URL and ref
with a reviewed Code Mower package source.

For private standalone repositories, keep package-install rehearsal on the same
authenticated path as checkout. Use the workflow deploy key, leave
`CODE_MOWER_STANDALONE_REPO_URL` set to the SSH URL, prefer an explicit
`CODE_MOWER_STANDALONE_REF` workflow override when present, and otherwise read
only `CODE_MOWER_STANDALONE_REF` from the pin file when building a pip package
spec. Do not `source` the entire pin file in the workflow after configuring an
SSH URL; doing so can replace the deploy-key URL with an unauthenticated HTTPS
URL.
Use `CODE_MOWER_STANDALONE_PACKAGE_REPO_URL` for the pip-installable package
source, for example `git+ssh://git@github.com/OWNER/code-mower.git`.
For public standalone repositories, a `git+https://...@REF` package spec is
fine.

Run:

```bash
code-mower migration mirror-removal-plan \
  --repo-path /path/to/product-repo \
  --shadow-cycles 1 \
  --standalone-default-cycles 1 \
  --json
```

Expected result after deletion: `status: mirrors_removed`, empty blockers, and
`mirrors_absent: true`.

## Current Reference Status

The standalone-default and mirror-removal paths have now been exercised in the
private reference/product repos. The successful end state is:

- product repo keeps thin support files such as `tools/code_mower`,
  `tools/code_mower_standalone_shadow.sh`, and
  `tools/code_mower_standalone_pin.env`;
- product shell shims and support helpers may remain for operator muscle
  memory or repo-specific bridges, but they delegate to package commands or
  stay intentionally product-specific;
- workflows invoke package-backed entrypoints instead of importing local Python
  mirrors;
- mirrored implementation files and package-owned Code Mower docs are removed
  from the product repo; and
- post-merge CI/deploy checks pass after deletion.

Use that as confidence, not as permission to skip the runbook. New repos should
still move through shadow, standalone-default, and mirrors-removed states in
separate commits or PRs when product velocity matters.

## Workflow Entry Points

Before deleting mirrors, workflows should call package-backed commands:

```bash
tools/code_mower trailer-comment-labeler --lane codex
tools/code_mower trailer-comment-labeler --lane claude
tools/code_mower saas-reviewer-labeler --adapter gitar
tools/code_mower bootstrap --print-python
```

Inspect supported aliases with:

```bash
code-mower migration runner-aliases
```

The package provides standalone local reviewer commands for Codex, Claude,
Gemini, Antigravity, Hermes, CodeRabbit CLI, and local LLM lanes. Product repos
may keep thin shell wrappers such as `tools/run_codex_audit_pr.sh` and
`tools/run_claude_audit_pr.sh` for token handling and operator muscle memory,
but those wrappers should delegate to `tools/code_mower codex-audit` and
`tools/code_mower claude-audit` instead of importing mirrored Python files.

`mirror-removal-plan --json` reports two support groups:

- `support_files`: core wrapper/pin/shadow files required for standalone
  package delegation;
- `product_support_files`: repo-specific helpers that can remain after mirror
  deletion, such as audit shell shims, `devin_audit_bridge.py`,
  `safe_gh_comment.py`, `request_review.py`, or local environment helpers.

Do not delete product support files merely because they mention Code Mower.
Delete only implementation mirrors that the standalone package now owns, or
rewrite the support helper first so it delegates to `tools/code_mower`.

## Reference Pilot Learning

The first private reference pilot removed the implementation mirror after a
clean standalone default cycle, but kept product support files. Two reviewer
findings improved the migration shape:

- blind-review artifact workflows should validate synthetic plans with
  `tools/code_mower blind-review plan ...` before materializing held/released
  artifacts; and
- workflow tests should prove both hold and release call sites use
  `tools/code_mower blind-review artifacts`, not just that the command appears
  somewhere in the YAML.

Carry those checks into any future product-repo migration.

## Recovery

If a workflow still calls a deleted local file:

1. Restore product velocity first by switching that workflow step to
   `tools/code_mower ...` when an equivalent package entrypoint exists.
2. If it depends on legacy Codex/Claude shell-wrapper behavior, restore the
   thin product wrapper in a small rollback PR, then make that wrapper delegate
   to the package command before retrying mirror deletion.
3. Run `code-mower migration runner-aliases --legacy <script>` to confirm
   whether the package owns that command or the product repo still does.
4. Re-run `code-mower migration mirror-removal-plan` and require zero workflow
   mirrored-file references before retrying deletion.

Keep mirror removal separate from feature work. A failed migration should never
block normal product development.

## Provider-Unavailable During Migration

Provider runtime failures are not reviewer findings. If a promoted lane cannot
run because the local CLI is unauthenticated, rate-limited, or otherwise
unavailable, do not mark it as passed.

The safe bypass pattern is:

1. run a minimal provider sanity prompt or version/auth check;
2. leave a PR comment naming the provider, head SHA, failure class, and current
   merge evidence;
3. remove the stale `needs-*-audit` label only if an authorized maintainer or
   repo policy allows an unavailable-provider bypass;
4. exclude the failed run from reviewer-quality calibration metrics; and
5. fix provider auth before relying on that lane again.

This keeps delivery moving without teaching Code Mower that "provider broken"
means "reviewer approved."
