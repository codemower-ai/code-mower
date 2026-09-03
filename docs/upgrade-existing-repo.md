# Upgrade An Existing Repository

Use this flow when a repository already has Code Mower generated workflows,
wrappers, labels, or support files. Keep the upgrade as one reviewed PR so
setup drift is visible before it can affect lanes.

## 1. Record The Current Install

Run these from the machine that will operate the repository:

```bash
command -v code-mower
code-mower --version
```

If multiple agents share the machine, decide which installer owns this command
before upgrading it. Use the install matrix in [Install And Bootstrap](install.md)
for pipx, uv, and contributor-checkout paths.

## 2. Generate Fresh Setup Output

From a clean repository checkout:

```bash
code-mower init --easy --apply --output-dir .code-mower.generated
```

Treat `.code-mower.generated` as review input. Do not copy it wholesale until
you have compared it with the existing repository files.

## 3. Inspect Setup Drift

Run the read-only drift report:

```bash
code-mower migration setup-drift --repo-path . --json
code-mower migration setup-drift --repo-path .
```

The report classifies paths only; it does not include source, diffs,
transcripts, issue body text, auth output, local secret values, or secrets.

- `same`: current file already matches the generated output.
- `differs`: review the file diff before copying the generated replacement.
- `new`: generated file does not exist in the repo yet.
- `missing-from-output`: existing Code Mower file is no longer generated.
- `repo-only`: file appears Code Mower-related but is intentionally outside the
  generator contract.

Do not delete `repo-only` or `missing-from-output` files automatically. They may
be product-specific shims, pinned wrappers, hand-written docs, or rollback
support. Keep, edit, or remove them only as an explicit review decision.

If `tools/code_mower_standalone_pin.env` exists, the JSON report includes a
`standalone_pin` block and the text report prints a concise standalone pin line.
A warning there means the checked-in standalone ref is missing, placeholder,
unreadable, or different from the currently running Code Mower package. Treat it
as an upgrade review item: decide whether the repo should keep its current
reviewed pin or move the pin in the same upgrade PR.

## 4. Copy Only Intended Files

Open a branch for the upgrade PR, then copy the generated files you intend to
adopt:

```bash
git switch -c chore/code-mower-upgrade
cp -R .code-mower.generated/. .
git status --short
```

Review the diff before committing. Preserve repository-specific edits in
`code-mower.yml`, local wrapper files, workflow permissions, and owner-surface
labels unless this upgrade intentionally changes them.

## 5. Check Builder And Reviewer Identity

Confirm the generated builder identity matches how agents actually open PRs:

- `builder:<lane>` labels are the strongest merge-gate signal.
- authenticated PR authors are trusted when configured in `builder_identity`.
- `builder_identity.branch_prefixes` can infer labels such as `builder:cursor`.
- PR-body trailers are useful for provenance experiments, but are not trusted
  as merge-authority author-exclusion input.

For audit comments, set trusted author repository variables such as
`CLAUDE_AUDIT_BOT_AUTHORS` and `CODEX_BOT_AUTHORS` to the GitHub logins that
may post manual pilot verdicts. `doctor --adoption --github` verifies only
variable names and presence status; it never prints variable values.

## 6. Check Wrapper And Pin Drift

If the product repository uses standalone support wrappers, inspect:

- `tools/code_mower`
- `tools/code_mower_standalone_shadow.sh`
- `tools/code_mower_standalone_pin.env`
- `tools/run_codex_audit_pr.sh`
- `tools/run_claude_audit_pr.sh`

The pin file should point at a reviewed Code Mower release, tag, or package
source. Product wrappers should delegate to the pinned standalone package unless
the repository is intentionally keeping a local fallback. For deeper migrations,
use [Mirror-Removal Runbook](mirror-removal-runbook.md).

## 7. Verify And Open The PR

Run:

```bash
bash .code-mower.generated/smoke-tests.sh
code-mower doctor --adoption --repo OWNER/REPO
code-mower lanes status --repo OWNER/REPO
```

Then commit the reviewed setup changes and open the upgrade PR. Run the usual
peer audits and merge only when the current head has clean audit evidence and
`code-mower/gate` is green for repositories where the gate is required.

After merge, record the installed path/version again:

```bash
command -v code-mower
code-mower --version
```
