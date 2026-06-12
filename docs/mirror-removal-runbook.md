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
  --package-spec code-mower \
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

The package provides standalone local reviewer commands for Gemini,
Antigravity, Hermes, CodeRabbit CLI, and local LLM lanes. The historical
`tools/run_codex_audit_pr.sh` and `tools/run_claude_audit_pr.sh` shell wrappers
remain product-wrapper responsibilities until generic Codex/Claude authoring
runners are intentionally designed for the OSS package.

## Recovery

If a workflow still calls a deleted local file:

1. Restore product velocity first by switching that workflow step to
   `tools/code_mower ...` when an equivalent package entrypoint exists.
2. If it depends on legacy Codex/Claude product runner behavior, restore the
   thin product wrapper in a small rollback PR and keep the mirror-removal PR
   paused.
3. Run `code-mower migration runner-aliases --legacy <script>` to confirm
   whether the package owns that command or the product repo still does.
4. Re-run `code-mower migration mirror-removal-plan` and require zero workflow
   mirrored-file references before retrying deletion.

Keep mirror removal separate from feature work. A failed migration should never
block normal product development.
