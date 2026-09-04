# Release Qualification

The `code-mower release qualify` command runs local release qualification with a stable adoption-result schema.

## Usage

```bash
code-mower release qualify \
  --release-tag v1.0.0 \
  --package-spec code-mower==1.0.0 \
  --output result.json \
  --qualification-context cold_install \
  --execute
```

Omit `--execute` for a non-mutating preview. Preview results use
`execution_state: planned` and `outcome: incomplete`; they are not evidence that
the package qualified. Runs use the current directory by default. Specify
`--repo-path` to qualify a different checkout.

To rehearse an upgrade in one isolated environment, pass
`--qualification-context upgrade --starting-version <installed-version>`.

## Schema

`code_mower.adoptionResult.v1` includes:

- `schema`: Schema version
- `timestamp_utc`: UTC timestamp
- `release_tag`: Exact tag
- `package_identity`: Package name (code-mower)
- `normalized_version`: Normalized version
- `qualification_context`: Context (cold_install/upgrade/unknown)
- `starting_version`: Exact preinstalled version for upgrade runs
- `ending_version`: Version after rehearsal
- `provider`, `executor`: Safe identifiers
- `host_class`, `runtime_class`: Environment classification
- `elapsed_seconds`: Total time
- `execution_state`: planned/executed
- `outcome`: incomplete/pass/pass_with_warnings/fail
- `steps`: List with id, status, elapsed_seconds, warning_count, owner_action_count

No local paths, secrets, commands, or raw output.

## Validation

- Tag must match `v<major>.<minor>.<patch>[-<stage>.<num>]`
- Package must be exact index spec: `code-mower==1.0.0`
- Tag and spec versions must match
- Provider/executor must be safe identifiers: `^[a-z][a-z0-9_]{0,31}$`
- Context must be: cold_install, upgrade, or unknown
- Upgrade runs require a normalized `starting_version` lower than the target

## Release Campaigns

The `code-mower release campaign` command coordinates multi-provider qualification across Claude, Codex, Antigravity, Muse, Cursor/Grok Bot, and Devin:

```bash
code-mower release campaign \
  --release-tag v1.0.0 \
  --package-spec code-mower==1.0.0 \
  --qualification-context cold_install
```

- **Dry-run by default:** Omit `--apply` for a safe preview. Add `--apply` for live local execution, GitHub comment dispatch, or paid runs.
- **Provider diversity:** Tracks Claude, Codex, Antigravity, Muse, Cursor/Grok Bot, and Devin. Missing tools or tokens degrade gracefully to `unavailable` or `manual` without failing the campaign.
- **Idempotent resume:** Pass `--resume` to re-poll running providers or advance queued participants without duplicating dispatch.
- **Local resilience:** Campaign state is stored in `.code-mower/campaigns/`. Local status remains fully readable during GitHub or provider network outages.
- **Board visibility:** Active campaigns surface directly on Code Mower Board with release, provider, environment, elapsed time, state, and actionable next steps.

### Provider Adapter Setup

- **Local CLI (Claude, Codex, Antigravity, Muse, Grok):** Install the provider CLI binary (`claude`, `codex`, `agy`, `muse`, or `grok`) on PATH and verify local authentication.
- **Hosted / SaaS (Devin, Cursor BugBot):** Configure authentication tokens (`DEVIN_AUDIT_LABEL_TOKEN`, `CURSOR_BUGBOT_AUDIT_LABEL_TOKEN`, `GITHUB_TOKEN`) and supply `--issue <number>` for comment dispatch.
