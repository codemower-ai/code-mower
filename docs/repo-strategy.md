# Code Mower Repo Strategy

Code Mower should become a public Apache-2.0 OSS core without exposing private
product repos or slowing ongoing product delivery.

## Repositories

- `code-mower`: standalone OSS candidate. This repo should become the public
  home for the installable Code Mower core when the public release checklist is
  complete.
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

1. Keep hardening the private standalone repo until easy-mode install, doctor,
   calibration, and package smoke tests are reliable.
2. Add standalone CI and release packaging so the standalone repo can validate
   itself without relying on private reference repos.
3. Add compatibility wrappers in the product repos that can call the standalone
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

As of `v0.1.0-alpha.10`, the CubeSnap product repos have proved the private
standalone checkout path and are in the standalone-default phase: product
wrappers prefer the pinned standalone command and keep `CODE_MOWER_USE_LOCAL=1`
as the explicit repo-local fallback. The next migration PR should update one
product repo at a time to the alpha.10+ pin, migrate workflow entrypoints to
`tools/code_mower`, run `migration wrapper-rehearsal`, run
`migration package-install-rehearsal` with the pinned package spec, then render
`migration mirror-removal-plan --shadow-cycles 1 --standalone-default-cycles 1`.

While the standalone Code Mower repository is private, GitHub Actions jobs that
run from product repos need either authenticated standalone checkout or an
intentional repo-local fallback. `CODE_MOWER_USE_LOCAL=1 tools/code_mower ...`
is the correct cost-safe/private-repo fallback for labeler workflows, but it is
also a formal mirror-removal blocker: those workflows still depend on the
repo-local mirrored scripts. Mirror deletion should wait for a dedicated
follow-up PR after the standalone package is public/package-installable or the
product workflows have authenticated standalone access.

Keep thin migration support files in the product repos while removing mirrors:
`tools/code_mower`, `tools/code_mower_standalone_shadow.sh`, and
`tools/code_mower_standalone_pin.env` are wrappers/config, not duplicated
implementation. Remove implementation mirrors only after workflows no longer
call files such as `tools/trailer_comment_labeler.py` or
`tools/saas_reviewer_labeler.py` directly. Workflows that need the Code Mower
Python environment should call `tools/code_mower bootstrap --print-python`
instead of importing `tools/code_mower_bootstrap.py` directly.

## Non-Goals

- Do not immediately remove repo-local tools from active product/reference repos.
- Do not make high-velocity feature work depend on Code Mower migration.
- Do not publish private product history, product-specific secrets, or commercial
  roadmap details in the OSS repo.
