# Code Mower Provider Matrix

Code Mower separates lane semantics from provider mechanics. v1.0 should ship a
small default path and make every optional provider's cost, privacy, and merge
authority clear before it runs.

## Default v1.0 Profile

| Lane | Driver | Default role | Merge authority | Notes |
|---|---|---|---|---|
| Codex audit | `local_cli` | structured peer audit | eligible | Requires local Codex auth and GitHub label/comment access. |
| Claude audit | `local_cli` | structured peer audit | eligible | Normal Claude lane is `*-audit`; prose `*-review` is manual. |
| Gitar audit | `saas_event` | advisory third signal | no | Useful informational SaaS signal in the reference repos. |

Everything else is opt-in until calibrated on the user's codebase.

## Provider Classes

| Class | Examples | Private repo support | Source exposure | v1.0 posture |
|---|---|---|---|---|
| Local CLI | Codex, Claude Code, Antigravity CLI, Hermes CLI, Aider, CodeRabbit CLI | Yes, if local auth can read the repo | Usually sent to the provider behind the CLI unless provider is local-only | Codex/Claude default; others informational |
| API/local model | Qwen, Gemma, DeepSeek, Grok-compatible endpoints | Yes | Sent to configured endpoint; local endpoints can keep code local | Informational calibration |
| SaaS reviewer | Gitar, CodeRabbit hosted, Cursor BugBot, Greptile, Qodo | Yes, if GitHub App and plan allow | Provider sees PR diff/source context | Manual or informational until calibrated |
| Hosted async | Devin, Jules | Yes, if app/API is authorized | Provider may clone or modify repo | Opt-in, paid, explicit trust boundary |
| Protocol bridge | ACP-backed CLIs | Depends on underlying agent | Depends on underlying agent | Research only until runtime stabilizes |

## Lane Details

| Lane id | Provider | Trigger | Cost policy | Private repo requirement | v1.0 merge role |
|---|---|---|---|---|---|
| `codex` | Codex CLI | Code Mower label/wrapper | included/provider account | local checkout plus GitHub token | merge-gating eligible |
| `claude_audit` | Claude Code | Code Mower label/wrapper | included/provider account | local checkout plus GitHub token | merge-gating eligible |
| `claude_review` | Claude Code/manual | human request | none | local/user context | advisory only |
| `gitar` | Gitar | GitHub event/comment after opt-in label | included/provider plan | GitHub App enabled for repo | informational |
| `greptile` | Greptile | GitHub review/check | paid | GitHub App enabled for repo | informational |
| `qodo` | Qodo | manual opt-in comment/event | paid | GitHub App enabled for repo | informational |
| `cursor_bugbot` | Cursor BugBot | `bugbot run` or `@cursor review` | paid/Cursor usage | Cursor GitHub App and BugBot repo enablement | informational |
| `devin` | Devin | hosted bridge | paid | Devin GitHub integration authorized | optional merge-gating only after explicit policy |
| `local_llm` | OpenAI-compatible endpoint | local runner | local or endpoint cost | endpoint receives selected code context | informational |
| `aider` | Aider CLI | local runner | local/provider account | local checkout plus model auth | informational |
| `gemini_cli` | Gemini CLI compatibility | local runner | provider account | local checkout plus API/auth | legacy informational |
| `antigravity_cli` | Antigravity CLI | local runner | provider account | local checkout plus local auth | informational |
| `hermes_cli` | Hermes Agent | local runner | provider account | local checkout plus local auth | informational |
| `coderabbit_cli` | CodeRabbit CLI | local runner | provider account | local checkout plus CLI auth | informational |
| `acp_bridge` | ACP-compatible agent | local runner | provider-dependent | depends on agent | research |

## Google CLI Posture

Google announced that Gemini CLI stops serving individual free/Pro/Ultra
requests on June 18, 2026, while enterprise/API-key paths continue. Treat
`gemini_cli` as a legacy compatibility and historical-comparison lane. New
Google CLI calibration work should target `antigravity_cli` once the local
`agy` runtime, auth, prompt transport, and parser behavior are stable enough for
repeatable non-gating runs.

Reference:
<https://developers.googleblog.com/an-important-update-transitioning-gemini-cli-to-antigravity-cli/>

## Benchmark Provenance Setup

Code Mower accepts operational dogfood without exact model names, but strong
provider comparisons need each reviewer event to identify the tool, version,
provider, model, and lens that produced it. Prefer the Code-Mower-specific
aliases when a vendor has multiple CLI surfaces, because those aliases prevent
one lane from accidentally inheriting another lane's model label.

| Provider lane | Preferred model env | Alternate model envs | Notes |
|---|---|---|---|
| Codex audit | `CODE_MOWER_CODEX_MODEL` | `CODEX_MODEL`, `OPENAI_MODEL` | Set to the model/profile you want benchmarked, for example `gpt-5`. |
| Claude audit | `CLAUDE_AUDIT_MODEL` | none | Mirrors the structured audit wrapper model. |
| Gemini CLI | `CODE_MOWER_GEMINI_MODEL` | `GEMINI_MODEL`, `GOOGLE_GENAI_MODEL` | Legacy compatibility lane; prefer Antigravity for new Google CLI work. |
| Antigravity CLI | `CODE_MOWER_ANTIGRAVITY_MODEL` | `ANTIGRAVITY_MODEL` | Deliberately does not inherit Gemini model env vars. |
| Hermes CLI | `CODE_MOWER_HERMES_MODEL` | `HERMES_INFERENCE_MODEL`, `HERMES_MODEL` | Use the deployed inference model id. |
| Aider | `CODE_MOWER_AIDER_MODEL` | `AIDER_MODEL`, `AIDER_CHAT_MODEL` | Informational until calibrated. |
| CodeRabbit CLI | `CODE_MOWER_CODERABBIT_MODEL` | `CODERABBIT_MODEL` | Informational/manual lane. |
| ACP bridge | `CODE_MOWER_ACP_MODEL` | none | Research lane until runtimes stabilize. |
| Local LLM | `CODE_MOWER_LOCAL_LLM_MODEL` | `LOCAL_LLM_MODEL` | Use the local endpoint/model id you configured. |

These values are metadata, not secrets. Never store API keys, prompts, raw
diffs, transcripts, auth output, or secret-like values in model/version fields.
If model provenance is missing, `code-mower cloud doctor` reports the relevant
provider env names and CodeMower.com shows provider-specific provenance fixes.

## Cursor BugBot Policy

Cursor BugBot is useful to test as a convenience reviewer, but it should not be
merge-gating in v1.0.

Recommended setup:

- Cursor dashboard trigger mode: Manual Only
- Review draft PRs: Off
- Autofix: Off
- Incremental review: Off during calibration
- Versioned repo guidance: `.cursor/BUGBOT.md`
- Code Mower lane: `cursor_bugbot`, informational, manual, paid

The observed disabled-repository response is:

```text
Skipping Bugbot: Bugbot is disabled for this repository.
```

That proves GitHub comments and Cursor's bot response path work, but it does
not prove the repo is enabled for BugBot reviews. Capture real enabled-output
examples before promoting the adapter beyond setup diagnostics.

## Private Repo Actions Cost Policy

Private GitHub repos can spend Actions minutes even on small metadata
workflows. The v1.0 posture is:

- no recurring cron for optional hosted reviewers
- issue-comment labelers must have job-level prefilters before checkout
- paid/manual lanes require an explicit lane label or manual trigger
- informational lanes must not be branch-protection requirements
- external apps may still spend provider credits or post comments according to
  their dashboards, but Code Mower should not spend Actions minutes parsing
  them unless the PR opted into that lane

## Promotion Policy

A lane can move from informational to selective trigger or merge authority only
after calibration answers:

- Did it catch known blockers?
- Did it stay quiet on known-clean controls?
- Were findings actionable and accepted?
- How much did it cost?
- How long did it take?
- Did it leak or expose source beyond the repo's trust policy?
- Did it produce stable parseable output?

Default promotions:

- Codex audit: eligible for merge authority after local setup.
- Claude audit: eligible for merge authority after local setup.
- Claude doctor probe: use the provider-configured JSON sentinel probe with a
  cheap explicit model and budget cap (`--model sonnet`, `--max-budget-usd
  0.25`) instead of relying on the local Claude CLI's default model. Auth
  status fields are configured through `doctor_probe_auth_status_fields` so
  providers can use their own JSON field names without leaking raw output.
- Claude inherited-env failures: if `claude auth status` says logged in but
  real prompt probes return `401` inside a long-lived shell or app, run
  `code-mower claude-bounce`. It retries the real prompt with known stale
  Claude/Anthropic auth override variables removed and can write a sourceable
  unset snippet without deleting local Claude credentials.
- Gitar: informational until local corpus evidence justifies selective use.
- Cursor BugBot and other SaaS/manual lanes: informational until calibrated.
- Local/private model lanes: informational until false-positive rates are low.

## OSS v1.0 Rule

Easy mode should never surprise users with provider spend or source exposure.
Every non-default lane must say:

- how it is triggered
- whether it is local or hosted
- whether it can see private code
- what secret or app access it needs
- whether it can affect merge readiness
- how to turn it off

See `docs/privacy-threat-model.md` for the data minimization and cloud export
rules that apply across providers.
