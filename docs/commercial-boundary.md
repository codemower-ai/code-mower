# Code Mower Commercial Boundary

The Code Mower OSS core should be useful on its own. The commercial business can
grow around hosted benchmarking, managed integrations, private aggregate data,
and enterprise controls without hiding local developer value.

## OSS Core

The Apache-2.0 core includes:

- local CLI and package scaffolding
- local audit orchestration and label semantics
- provider templates, lane configs, prompt lenses, and context packs
- local calibration corpus support and reviewer value reports
- local doctor/setup checks
- cloud export schemas and opt-in bundle creation
- docs, templates, examples, and smoke tests

The OSS core should let a team install Code Mower, configure providers, run
audits, run calibration, and produce local quality, speed, and cost reports
without a hosted account.

## Commercial Surfaces

Commercial/private surfaces include:

- hosted benchmark ingestion and reporting
- cloud dashboards and comparison views
- private aggregate benchmark datasets
- managed provider integrations and hosted runners
- billing, subscriptions, and usage metering
- enterprise policy, audit, admin, SSO, and retention controls
- support, SLAs, and implementation services
- pricing, go-to-market plans, and internal business strategy

These belong in a private commercial repo, not in the public OSS repo.

## Data Boundary

- Local-first by default.
- No cloud upload by default.
- Cloud benchmarking export must be explicit, inspectable, and opt-in.
- Public docs should describe the export schema, privacy model, and user
  controls, not private backend implementation details.
- Export bundles should avoid secrets and should make code/context inclusion
  intentional.

## Trust Posture

The public repo should be boringly clear about what runs locally, what calls
provider CLIs/APIs, what can be uploaded, and what requires a hosted account.
That transparency is part of the product: teams should be able to adopt Code
Mower before they trust the commercial service.
