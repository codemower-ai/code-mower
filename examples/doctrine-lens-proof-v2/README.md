# Doctrine Lens Proof v2

This example is a metadata-only calibration corpus for testing whether reviewer
doctrine lenses move outcomes more or less than provider/runtime choice.

It is designed to answer the question:

> Are lens effects larger or smaller than provider effects?

## Definitions

- **Lens effect:** for the same provider, compare `base-audit` against a
  doctrine lens such as `generic-programming` or
  `context-driven-quality`.
- **Provider effect:** for the same lens, compare outcome spread across
  providers such as Claude, Gemini, Antigravity, and Hermes.
- **Effective catch rate:** known-blocker catches divided by all known-blocker
  runs in the cell. Input gaps and infra errors count against the cell because
  they are real operational outcomes.
- **Evaluable catch rate:** known-blocker catches divided only by runs where
  the reviewer produced an evaluable pass/block result.

The effective metric is the default answer because Code Mower cares about the
whole lane outcome, not only the idealized subset where every provider received
enough usable context and completed cleanly.

## Regenerate

From the repository root:

```bash
code-mower calibration effect-report \
  examples/doctrine-lens-proof-v2/calibration-corpus.json \
  --output docs/doctrine-lens-proof-v2.md
```

For local source checkouts without an installed console script:

```bash
scripts/dev-python -m code_mower.cli calibration effect-report \
  examples/doctrine-lens-proof-v2/calibration-corpus.json \
  --output docs/doctrine-lens-proof-v2.md
```

The corpus intentionally stores reviewer dispositions and timing metadata, not
source code, diffs, or raw reviewer transcripts.
