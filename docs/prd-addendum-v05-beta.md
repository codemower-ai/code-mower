# Code Mower PRD Addendum: v0.5 Beta Learnings

This addendum records product learnings from the v0.5 beta path. It is not a
replacement for the full product requirements; it is the lightweight update we
want before widening early-adopter access.

## Installed Package Is The Release Gate

The public experience is the installed package, not the editable source
checkout. Release candidates must prove the package-installed path in a clean
environment before they are treated as shareable.

`v0.5.0-beta.3` dogfood found a real installed-package gap: the Codex audit
schema-structuring phase inherited a repository check that only made sense from
an editable source checkout. `v0.5.0-beta.8` fixed that by making the
installed-package transport path explicit and then validating it from the public
tag. `v0.5.0-beta.18` keeps that installed-package gate and carries the same
provenance discipline into stale-audit, catch-up, and dashboard trust work.

Going forward, package-install rehearsal is release-gating, not optional polish.

## Dashboard Trust Depends On Provenance

CodeMower.com should never blur different kinds of evidence:

- routine dogfood/current uploads prove connectivity and recent repo activity;
- historical catch-up imports provide workflow history and onboarding context;
- reviewer-run and calibration events provide provider/lens quality signal.

The dashboard should label those categories separately and avoid presenting
workflow history as reviewer-value evidence.

## Dogfood Is Not Historical Backfill

Dogfood workflows upload current repository metadata and optional current
shareable reports. They do not reconstruct older pull requests, older reviewer
comments, or historical benchmark outcomes.

Historical backfill is a separate, explicit mode:

- `code-mower cloud catch-up` for sanitized GitHub Actions history; and
- `code-mower cloud reviewer-runs` or `repo-sync --mode reviewer-runs` for
  existing local verdict artifacts.

Future richer catch-up should remain dry-run-first, idempotent, and
metadata-only by default.
