# Try Code Mower In 10 Minutes

This page is the short route through the maintained setup guides. Exact install
commands live in [Install And Bootstrap](install.md), and the complete first
repository workflow lives in [Code Mower Quickstart](quickstart.md). Keeping
those procedures in one place prevents release pins, authentication steps, and
setup behavior from drifting.

1. Complete the install guide and verify `command -v code-mower` plus
   `code-mower --version`.
2. Authenticate GitHub as described in the quickstart.
3. Preview, generate, and review the default Claude + Codex setup:

   ```bash
   code-mower init --easy
   code-mower init --easy --apply --output-dir .code-mower.generated
   code-mower doctor --adoption --repo OWNER/REPO --json --share-safe
   ```

4. Open the setup pull request and request independent Claude and Codex audits
   against its current head. Follow the exact commands and token boundaries in
   the quickstart.
5. Inspect the result:

   ```bash
   code-mower lanes status --repo OWNER/REPO
   code-mower productivity report --repo OWNER/REPO
   code-mower next-steps --repo OWNER/REPO --pr PR_NUMBER
   ```

`init --easy` is a preview. `--apply` writes a generated tree for review; it
does not copy files into the repository, launch providers, enable auto-merge,
or upload data. Existing integrations should follow
[Upgrade An Existing Repository](upgrade-existing-repo.md) before generating a
new tree.

After the manual reviewer gate is useful, continue with
[Build Loop In 30 Minutes](build-loop-in-30-minutes.md).
