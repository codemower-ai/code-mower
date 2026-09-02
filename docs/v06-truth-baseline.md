# Code Mower v0.6 Truth Baseline

Last verified: 2026-09-01 PT.

This note is the starting baseline for the v0.6 provider-contract hardening
queue. It records facts that later refactors should preserve, especially where
external reviewer feedback has been plausible but stale or too broad.

## Release Baseline

- The current v0.6 release target for the OSS package is `v0.6.0-beta.3`.
- The matching package-index spec for installation is `code-mower==0.6.0b3`.
- The beta line stays active while v0.6 proves adoption. Do not rename
  generated workflow files or break package-backed product-repo consumption
  while beta installs remain the adoption path.

## Provider Runner Facts

Code Mower already has shared provider-runner helpers under
`src/code_mower/provider_runners/` for comments, git/worktree handling,
GitHub PR metadata, subprocess execution, text/schema parsing, and verdict
artifacts. The v0.6 extraction goal is therefore incremental:

- keep existing CLI entrypoints stable;
- fixture-lock provider output behavior before moving logic;
- extract repeated Codex and Claude audit comment, verdict, and exit-code
  helpers into `provider_runners/`; and
- avoid a flag-day `BaseProviderRunner` rewrite of calibrated merge-gate code.

## Gemini And Antigravity

Gemini CLI and Antigravity are both Google surfaces, and Antigravity can use
Gemini model infrastructure. They are still distinct Code Mower runtimes.

| Runtime | Code Mower lane | Posture |
| --- | --- | --- |
| Gemini CLI | `gemini_cli` | Legacy compatibility and historical-comparison lane. |
| Antigravity CLI | `antigravity_cli` | Forward Google CLI research lane; manual and informational. |
| Antigravity SDK | Future `antigravity_sdk` | Optional research lane only after the real SDK surface is proven. |

Do not merge Gemini and Antigravity calibration records into one bucket. Record
the provider family, runtime, lane id, model provenance, prompt lenses, and
output parser separately because the harness, auth path, prompt transport,
state handling, and artifacts can change reviewer behavior even when the
underlying model family overlaps.

## Antigravity CLI Baseline

The current Antigravity CLI wrapper is intentionally conservative:

- command: `agy`, with `antigravity` accepted as an alternate command;
- lane id: `antigravity_cli`;
- labels: `needs-antigravity-cli-audit`,
  `antigravity-cli-audit-done`, and `antigravity-cli-audit-blocked`;
- model metadata env: `CODE_MOWER_ANTIGRAVITY_MODEL`, then
  `ANTIGRAVITY_MODEL`;
- no inheritance from Gemini model env vars; and
- local OAuth state requires explicit `ANTIGRAVITY_CLI_USE_AMBIENT_HOME=1` in a
  trusted local environment.

The ambient-home requirement is a consent boundary. v0.6 may add additional
explicit Antigravity auth modes, but it must not silently inherit local Google
or Gemini credentials.

## Antigravity SDK Research Posture

The `google-antigravity` package and `google.antigravity` import path are real
enough to justify a spike. As of the 2026-08-31 PT v0.6 probe,
`google-antigravity` 0.1.15 is an alpha package with a bundled local harness,
Gemini API or Vertex/ADC auth, response-schema support, read-only tool
defaults, and token usage metadata. SDK work should not replace the CLI lane
until Code Mower proves the exact API, auth, sandboxing, structured verdict,
usage-metadata, timeout, and failure semantics.

Treat SDK-based review as a separate optional lane with its own fixtures and
calibration records. If SDK events upload to CodeMower.com, update
`docs/cloud-data-contract.md` before the upload shape changes.

## v0.6 Scope

The v0.6 queue should make provider contracts boring:

1. clean up package/import execution paths;
2. add golden provider-output fixtures;
3. continue incremental `provider_runners/` extraction;
4. validate provider and cloud-event boundaries without adding heavy core
   runtime dependencies by default;
5. tighten static checks in low-noise stages;
6. harden Antigravity CLI as manual/informational; and
7. timebox Antigravity SDK and builder-experiment execution as follow-on
   research features.

## Non-Goals

- No automatic promotion of Antigravity, Gemini, hosted reviewers, or SDK lanes
  to merge authority.
- No broad automated agent orchestration inside Code Mower core before the
  provider contracts and builder-run metadata are stable.
- No runtime dependency expansion unless the trust and packaging trade-off is
  explicit.
- No privacy-boundary expansion.

## Privacy Boundary

The v0.6 work must keep the existing cloud boundary unchanged: metadata only.
Default uploads must not contain source code, raw diffs, model transcripts,
prompt history, reviewer stdout/stderr, issue body text, auth output, tokens,
or secret-like values.
