# Provider Diversity Provenance Documentation

This document tracks the tool/model/version provenance metadata for v1.0 provider diversity hardening fixtures. All provider fixtures maintain the metadata-only privacy boundary: no source code, raw diffs, transcripts, issue body text, raw stdout/stderr, auth output, local paths by default, or secrets in uploads.

## Provider Fixture Provenance

### Codex (OpenAI/Cursor)
- **Provider**: codex
- **Driver**: local_cli
- **Schema**: codeMower.codexAudit.v1
- **Result Source**: trailer_comment
- **Model Source**: env (CODE_MOWER_CODEX_MODEL, CODEX_MODEL, OPENAI_MODEL)
- **Version Detection**: CLI version probe (`codex --version`)
- **Merge Authority**: true
- **Spend Policy**: paid
- **Status**: merge-gating lane with structured output

### Claude (Anthropic)
- **Provider**: claude
- **Driver**: local_cli
- **Schema**: codeMower.claudeAudit.v1
- **Result Source**: trailer_comment
- **Model Source**: env (CLAUDE_AUDIT_MODEL), default "sonnet"
- **Version Detection**: CLI version probe with JSON output
- **Merge Authority**: true (selective-trigger candidate per lane-promotion-policy.md)
- **Spend Policy**: included
- **Status**: structured audit lane with plan-conformance lens support

### Gitar
- **Provider**: gitar
- **Driver**: saas_event
- **Schema**: codeMower.gitarAudit.v1 (fixture schema)
- **Result Source**: issue_comment
- **Model Source**: vendor_hidden
- **Version Detection**: vendor_hidden
- **Merge Authority**: false (informational, keep where already approved)
- **Spend Policy**: included
- **Bot Authors**: gitar-ai[bot], gitar-bot, gitar-bot[bot]
- **Status**: informational adapter-based lane, opt-in required

### Cursor BugBot
- **Provider**: cursor_bugbot
- **Driver**: saas_event
- **Schema**: codeMower.cursorBugbotAudit.v1 (fixture schema)
- **Result Source**: issue_comment
- **Model Source**: vendor_hidden
- **Version Detection**: vendor_hidden
- **Merge Authority**: false (informational)
- **Spend Policy**: paid
- **Bot Authors**: cursor[bot], cursor
- **Trigger Comments**: "bugbot run", "@cursor review"
- **Status**: manual informational lane, calibration-only until output shape captured

### Antigravity CLI
- **Provider**: antigravity
- **Driver**: local_cli
- **Schema**: codeMower.antigravityAudit.v1 (fixture schema, reuses Gemini structure)
- **Result Source**: trailer_comment
- **Model Source**: env (ANTIGRAVITY_MODEL, CODE_MOWER_ANTIGRAVITY_MODEL)
- **Version Detection**: CLI version probe (`agy --version` or `antigravity --version`)
- **Merge Authority**: false (informational)
- **Spend Policy**: included
- **Auth**: Local OAuth via `agy` login/install, requires ANTIGRAVITY_CLI_USE_AMBIENT_HOME=1
- **Prompt Lenses**: base-audit
- **Status**: informational forward Google CLI research lane, not merge authority until calibrated

### Devin
- **Provider**: devin
- **Driver**: hosted_bridge
- **Schema**: codeMower.devinAudit.v1 (fixture schema)
- **Result Source**: trailer_comment
- **Model Source**: vendor_hidden
- **Version Detection**: vendor_hidden
- **Merge Authority**: true (enabled_by_default=false, manual trigger)
- **Spend Policy**: paid
- **Status**: paid-optional hosted bridge lane, manual trigger only

### Muse CLI (Meta)
- **Provider**: muse
- **Driver**: local_cli
- **Schema**: codeMower.museAudit.v1 (fixture schema)
- **Result Source**: trailer_comment
- **Model Source**: env (CODE_MOWER_MUSE_MODEL, MUSE_MODEL, META_MUSE_MODEL)
- **Version Detection**: CLI version probe (`muse --version`)
- **Merge Authority**: false (informational)
- **Spend Policy**: included
- **Auth**: `muse login`, META_API_KEY, or META_API_KEY_FILE; requires MUSE_CLI_USE_AMBIENT_HOME=1
- **Prompt Lenses**: base-audit
- **Prompt Transport**: jsonl_prompt_file
- **Status**: experimental Muse Code lane, not merge authority until calibrated for blocker catch rate, false positives, cost, and latency

### Grok Build (xAI/Cursor)
- **Provider**: grok_build
- **Driver**: local_cli
- **Schema**: codeMower.grokBuildAudit.v1 (fixture schema)
- **Result Source**: trailer_comment
- **Model Source**: env (CODE_MOWER_GROK_MODEL, GROK_MODEL, XAI_MODEL)
- **Version Detection**: CLI version probe with JSON output
- **Merge Authority**: false (informational)
- **Spend Policy**: included
- **Auth**: `grok login --device-code` or XAI_API_KEY; requires GROK_BUILD_USE_AMBIENT_HOME=1
- **Prompt Lenses**: base-audit
- **Prompt Transport**: prompt_file
- **Status**: informational Grok Build lane, not merge authority until calibrated

## Fixture Contract Structure

All provider fixtures include four test cases:
1. **pass**: Clean verdict with no findings
2. **blocked**: Blocking verdict with P1 findings
3. **placeholder**: Blocked verdict with placeholder data for guardrail testing
4. **malformed_extra_key**: Malformed verdict with unsupported extra keys for schema validation

## Metadata-Only Privacy Boundary

All fixtures and telemetry events strictly preserve the metadata-only boundary:
- ✅ Included: provider name, model identifier, tool version, verdict, severity counts, duration, head SHA, lane ID, PR number, repo slug
- ❌ Excluded: source code, raw diffs, transcripts, issue body text, raw stdout/stderr, auth output, local absolute paths, secrets, account identifiers, session IDs

## Evidence-Based Promotion Policy

No provider is promoted solely because it is available. All promotion decisions follow `docs/lane-promotion-policy.md`:
- At least 10 adjudicated findings across at least 5 PRs
- Fresh calibration run artifacts with raw output preserved
- At least 2 known-clean PRs with no blocking false positives
- At least 2 known-blocked or seeded-bug PRs where the lane catches real issues
- Useful-rate above 0.60 for general lanes, or above 0.75 for selective lanes
- Precision above 0.70 on blocker-labeled findings
- `code-mower doctor --probe-runtime` passes for required CLIs/tokens

## Version and Runtime Provenance

Provider lanes track tool version and model provenance where available:
- **CLI lanes**: Use `detect_local_cli_version()` for version probes
- **Hosted/SaaS lanes**: Accept `vendor_hidden` or `missing` version states
- **Model source**: Prioritized from env vars, profiles, defaults, or marked as `vendor_hidden` for hosted providers
- **Runtime environment**: Coarse labels (github-actions, ci, local) without exposing local paths

## Next Steps

1. Run calibration evidence collection with the expanded provider set
2. Generate value reports from combined fixture + live run evidence
3. Update lane-promotion-policy.md with adjudicated findings from diverse providers
4. Convert install/review feedback into small hardening PRs per work order guidance
