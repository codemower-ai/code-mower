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

## Non-Goals

- Do not immediately remove repo-local tools from active product/reference repos.
- Do not make high-velocity feature work depend on Code Mower migration.
- Do not publish private product history, product-specific secrets, or commercial
  roadmap details in the OSS repo.
