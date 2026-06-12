# Code Mower Repo Strategy

Code Mower is a public Apache-2.0 OSS core. The repo strategy is to keep the
installable core useful on its own while private reference/product repos keep
validating real-world velocity, quality, and migration behavior without leaking
private product history into public docs.

## Repositories

- `code-mower`: public home for the installable Code Mower core.
- Private product/reference repos: continue validating Code Mower against real
  development, but public docs should not require access to them or name them as
  dependencies.
- Private commercial service repo: hosted benchmarking, reporting, ingestion,
  billing, and enterprise controls.

Public-facing docs should describe Code Mower as extracted from a production
multi-repo development workflow, not as a generated view of any specific private
product repo.

## Migration Principles

- Protect product development velocity. Code Mower migration work must not block
  feature, platform, deployment, or customer-impacting work.
- Keep product repo wrappers stable until the standalone package has CI, smoke
  tests, and real usage evidence.
- Move implementation ownership into the standalone repo in slices, with pinned
  product-repo consumption after each slice proves clean.
- Run standalone and repo-local implementations in shadow mode before changing
  defaults.
- Keep commercialization plans, hosted service internals, and private benchmark
  data outside the public OSS repo.

## Extraction Phases

1. Keep hardening easy-mode install, doctor, calibration, and package smoke
   tests until they are boring from a clean public checkout.
2. Maintain standalone CI and release packaging so the standalone repo validates
   itself without relying on private reference repos.
3. Add compatibility wrappers in reference/product repos that can call the standalone
   package when `CODE_MOWER_USE_STANDALONE=1`.
4. Run shadow comparisons between repo-local tools and standalone tools on real
   Code Mower PRs.
5. Flip product repos to use pinned standalone releases by default.
6. Remove duplicated tool implementation files from product repos after at least
   one clean release cycle.

## Wrapper Rehearsal

Before a product repo flips any default, run the package against that repo and
compare safe read-only commands with the repo-local tools:

```bash
code-mower migration wrapper-rehearsal \
  --repo-path /path/to/product-repo \
  --json
```

This command compares provider listing, prompt validation, and calibration
evidence generation when a local calibration corpus is available. A pass means
the repo is ready for standalone shadow mode. It does not mean the repo-local
tools can be deleted yet.

After at least one pinned standalone release is available in a product repo,
render the mirror-removal plan:

```bash
code-mower migration mirror-removal-plan \
  --repo-path /path/to/product-repo \
  --shadow-cycles 1 \
  --standalone-default-cycles 0 \
  --json
```

The plan inventories mirrored files and reports whether the repo has completed
enough clean shadow cycles to flip product wrappers to standalone by default.
Deleting mirrors requires a later clean standalone-default cycle. The plan is
deliberately conservative: inventory is not deletion approval, and product
feature work should not depend on mirror removal.

The expected migration order is:

1. standalone command matches repo-local read-only commands
2. product workflow wrappers can call the standalone command under
   `CODE_MOWER_USE_STANDALONE=1`
3. issue-comment and SaaS reviewer labeler workflows call the standalone
   wrapper entrypoints (`tools/code_mower trailer-comment-labeler ...` and
   `tools/code_mower saas-reviewer-labeler ...`) instead of importing mirrored
   Python files directly
4. setup/bootstrap workflow helpers call `tools/code_mower bootstrap ...`
   instead of `python3 tools/code_mower_bootstrap.py ...`
5. one release cycle runs standalone in shadow mode
6. pinned standalone release becomes the default
7. mirrored implementation files are removed from product repos

As of `v0.1.0-alpha.17`, the private reference/product repos have proved the
standalone checkout path, the standalone-default wrapper path, and the
mirror-removal path. Their product wrappers prefer the pinned standalone
command, workflows call `tools/code_mower` entrypoints, and mirrored
implementation files can be absent while `migration mirror-removal-plan` reports
`mirrors_removed`.

That does not make mirror deletion automatic for every user repo. The migration
contract remains one repo at a time:

1. pin a known Code Mower version
2. run `migration wrapper-rehearsal`
3. run `migration package-install-rehearsal` with the pinned package spec
4. run at least one clean standalone-default cycle
5. render `migration mirror-removal-plan --shadow-cycles 1
   --standalone-default-cycles 1`
6. remove mirrored implementation files only when the plan reports no blockers

Now that `code-mower` is public, GitHub Actions can fetch public source over
HTTPS without a deploy key. Deploy keys and fine-grained tokens are still useful
when a team pins a private fork, consumes a private package index, or runs
against private reference branches.

Keep thin migration support files in the product repos while removing mirrors:
`tools/code_mower`, `tools/code_mower_standalone_shadow.sh`, and
`tools/code_mower_standalone_pin.env` are wrappers/config, not duplicated
implementation. Remove implementation mirrors only after workflows no longer
call files such as `tools/trailer_comment_labeler.py` or
`tools/saas_reviewer_labeler.py` directly. Product shell shims such as
`tools/run_codex_audit_pr.sh` and `tools/run_claude_audit_pr.sh` may stay, but
they should call `tools/code_mower codex-audit` and
`tools/code_mower claude-audit` rather than importing local Python modules.
Workflows that need the Code Mower
Python environment should call `tools/code_mower bootstrap --print-python`
instead of importing `tools/code_mower_bootstrap.py` directly.

## Non-Goals

- Do not immediately remove repo-local tools from a user repo before wrapper
  rehearsal, package-install rehearsal, and a clean standalone-default cycle.
- Do not make high-velocity feature work depend on Code Mower migration.
- Do not publish private product history, product-specific secrets, or commercial
  roadmap details in the OSS repo.
