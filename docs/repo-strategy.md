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
3. one release cycle runs standalone in shadow mode
4. pinned standalone release becomes the default
5. mirrored implementation files are removed from product repos

As of `v0.1.0-alpha.6`, the CubeSnap product repos have proved the private
standalone checkout path and are in the standalone-default phase: product
wrappers prefer the pinned standalone command and keep `CODE_MOWER_USE_LOCAL=1`
as the explicit repo-local fallback. The next migration PR should update one
product repo at a time to the alpha.6 pin, run
`migration wrapper-rehearsal`, then render
`migration mirror-removal-plan --shadow-cycles 1 --standalone-default-cycles 1`.
Even if the plan reports `ready_to_remove_mirrors`, mirror deletion should wait
for a dedicated follow-up PR with no CubeSnap feature work mixed in.

## Non-Goals

- Do not immediately remove repo-local tools from active product/reference repos.
- Do not make high-velocity feature work depend on Code Mower migration.
- Do not publish private product history, product-specific secrets, or commercial
  roadmap details in the OSS repo.
