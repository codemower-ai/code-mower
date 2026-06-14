# Calibration Policy

Use this lens when a PR changes reviewer calibration, lane promotion policy,
value reporting, calibration corpora, spend/latency accounting, or reviewer
disposition tooling.

Focus on:

- Whether evidence is head-bound, reproducible, and tied to known-clean or
  known-blocked outcomes.
- Whether generated policy separates routine merge gates, selective triggers,
  informational lanes, and research lanes.
- Whether reported precision, useful-rate, catch/miss, spend, and latency
  metrics are derived from adjudicated evidence rather than reviewer claims.
- Whether raw reviewer artifacts are durable but shareable reports avoid
  leaking tokens, account state, local paths, or unbounded terminal output.
- Whether context packs and prompt lenses are treated as customization
  surfaces, not hidden provider-specific behavior.

Return a terse clean pass when the change only updates evidence or docs and the
metrics/policy derivation remains internally consistent.
