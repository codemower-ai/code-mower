# Demo Calibration Example

This example shows what Code Mower is trying to measure before you connect it
to your own repository history.

It is deliberately small and synthetic:

- one known-clean pull request that should not produce blockers;
- one known-blocked pull request with a real expected theme;
- three reviewer lanes with different outcomes; and
- a sample value report that turns those outcomes into lane recommendations.

The point is not that these numbers are universal. The point is that Code Mower
should help you build this same evidence loop for your product:

1. Choose known-clean and known-blocked pull requests.
2. Run reviewers and lenses on those changes.
3. Adjudicate which findings were useful, noisy, or missed.
4. Produce a value report.
5. Promote lanes only when the evidence supports it.

## Files

- `calibration-corpus.json` is a tiny example corpus.
- `reviewer-metrics.json` is the metrics shape produced from adjudicated runs.
- `lane-policy.json` shows how those metrics become recommended lane posture.
- `reviewer-value-report.md` is the human-readable output a team can discuss.

## Try The Shape Locally

From the repository root:

```bash
code-mower calibration value-report examples/demo-calibration/calibration-corpus.json \
  --output /tmp/code-mower-demo-value-report.md
```

That command proves the report generation path. In a real pilot, replace the
demo corpus with pull requests from your own repository and save the generated
report into your team docs or CI artifacts.

## Privacy Boundary

This example contains no source code, raw diffs, raw transcripts, secrets, auth
output, local machine paths, or product-specific private repository names.
That is the same boundary Code Mower Cloud uses by default for opt-in metadata
sharing.
