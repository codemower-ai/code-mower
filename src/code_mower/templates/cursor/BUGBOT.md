# BugBot Review Guidance

BugBot is used as an informational Code Mower reviewer for manually triggered
PR checks. Focus on issues that CI and formatters usually cannot catch:
correctness bugs, security risks, data-loss risks, architecture regressions,
missing edge-case coverage, and user-visible behavior changes.

Do not duplicate CI. Avoid comments about formatting, lint, typecheck,
dependency install failures, or test failures unless the diff itself makes the
failure likely and the root cause is visible in the changed code. If there is
nothing material to add beyond CI, leave no findings.

Prefer high-signal findings:

- Explain the concrete failure mode and the affected path.
- Cite the changed code or nearby behavior that makes the issue plausible.
- Distinguish blockers from advisory cleanup.
- Avoid speculative rewrites or style preferences unless they prevent a real
  bug or maintenance hazard.

When reviewing Code Mower changes, preserve the lane model:

- `*-audit` lanes are structured, automated, head-bound review signals.
- `*-review` lanes are manual/advisory prose reviews.
- Uncalibrated provider lanes should remain informational until measured
  against known-clean and known-blocker PRs.
