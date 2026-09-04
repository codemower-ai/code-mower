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
- An executed `pass`/`pass_with_warnings` result must report `ending_version`
  exactly equal to `normalized_version`; planned/incomplete results may leave
  it empty

## Release Campaigns

The `code-mower release campaign` command coordinates multi-provider qualification across Claude, Codex, Antigravity, Muse, Cursor/Grok Bot, and Devin:

```bash
code-mower release campaign \
  --release-tag v1.0.0 \
  --package-spec code-mower==1.0.0 \
  --qualification-context cold_install
```

- **Dry-run by default:** Omit `--apply` for a safe preview. Add `--apply` for live local execution, GitHub comment dispatch, or paid runs.
- **Provider diversity:** Tracks Claude, Codex, Antigravity, Muse, Cursor/Grok Bot, and Devin. Missing tools, tokens, or adapters degrade gracefully to `unavailable` without failing the campaign. Each provider may appear at most once: `--providers` is canonicalized before any participant is built, so naming the same provider twice -- directly, or through two aliases of one lane such as `cursor` and `grok_bot` -- is rejected with an explicit error instead of creating two participants that share a single idempotency key and result path (which would let one provider's evidence count twice).
- **Idempotent resume:** Pass `--resume` to re-poll running providers or advance queued participants without duplicating dispatch or re-invoking an adapter that already completed. Once a provider's applied dispatch/adapter has been attempted (even if it failed or its outcome was uncertain), ordinary resume never repeats it automatically -- pass `--retry-provider <provider>` to explicitly retry that one provider. `--retry-provider` is rejected unless the named provider is already part of the campaign.
- **Never reinitialized:** An existing campaign is never replaced by a fresh queued one. Repeating the same invocation (same `--release-tag` or `--campaign-id`, with or without `--resume`) advances the stored campaign under resume semantics, so a repeated `--apply` never reruns a local adapter, reposts a hosted dispatch, or discards recorded provider state and evidence. The explicit `create` action fails when that campaign already exists, and `resume`/`dispatch` fail when it does not -- neither falls through to creating one. Creation arguments that describe a different campaign (`--package-spec`, `--providers`, `--qualification-context`, `--starting-version`, or a `--campaign-id`/`--release-tag` pair that disagree) are rejected explicitly rather than silently ignored. `--repo-slug` is the one field an existing campaign can still be *completed* with: a campaign created without a repository slug has nowhere to dispatch, so supplying `--repo-slug` on a later `resume`/`dispatch` fills the empty stored value and persists it before any hosted dispatch uses it. A `--repo-slug` that disagrees with a non-empty stored slug is rejected like any other identity change -- an in-flight campaign is never repointed at a different repository.
- **Bounded status lookups:** `--status` with an explicit `--campaign-id` or `--release-tag` reports that campaign or exits non-zero with a bounded "no campaign found" message -- it never falls back to an unrelated campaign's data. Only an unqualified `--status` (no identifier) reports the most recently updated campaign.
- **Local resilience:** Campaign state is stored in `.code-mower/campaigns/`. Local status remains fully readable during GitHub or provider network outages.
- **Board visibility:** Active campaigns surface directly on Code Mower Board with release, provider, environment, elapsed time, state, and actionable next steps.
- **Never fabricated:** A provider can only reach `complete` when its own adapter command actually ran (argv only, never a shell) and produced a result file that passes closed-schema validation and matches the campaign's provider, release tag, and (for GitHub comment results) idempotency key. The idempotency key itself is derived from the campaign id, provider, release tag, qualification context, and starting version, so an upgrade campaign from one starting version can never be satisfied by a same-tag dispatch from a different starting version. For GitHub comment results, the embedded adoption result's own `qualification_context` and `starting_version` are also checked directly against the campaign's expected values -- this holds even if a wrapper's idempotency key were copied or generated incorrectly. Code Mower never runs its own local qualification and relabels the result as another provider's.

### Provider Adapter Setup (one-time, per provider)

Local CLI providers (`local_cli` driver: Claude, Codex, Antigravity, Muse) only run automatically if a `campaign_adapter_argv` is configured for that provider's lane. None of the shipped providers ship with one configured by default -- a provider without a configured adapter is intentionally `unavailable`/manual rather than faking a result.

**Adopters: configure this per-repo in `code-mower.yml` at the repository root.** This is the recommended setup and does not require editing installed Python:

```yaml
lanes:
  muse_cli:                      # canonical lane_id, or a provider alias (e.g. "muse")
    provider_config:
      campaign_adapter_argv:
        - "{command}"
        - qualify
        - --release-tag
        - "{release_tag}"
        - --package-spec
        - "{package_spec}"
        - --output
        - "{output}"
      campaign_adapter_timeout_seconds: 60
```

Supported placeholders: `{command}` (resolved binary), `{release_tag}`, `{package_spec}`, `{qualification_context}`, `{starting_version}`, `{output}`, `{repo_path}`. The adapter command must write a `code_mower.adoptionResult.v1` JSON document to the `{output}` path whose `provider`, `release_tag`, `qualification_context`, and `starting_version` fields all match the campaign's -- a cold-install result cannot complete an upgrade campaign, and an upgrade result must match the campaign's exact starting version. Anything else (extra fields, mismatched identity, no file, non-zero exit, or a timeout) leaves the provider `unavailable`/`blocked` with a bounded error code -- never a fabricated pass. A local drop-in result file and `--record-result` are bound the same way. Install the provider's CLI binary on PATH and verify local authentication.

An explicit `--retry-provider` never accepts a pre-existing result file for that provider -- the stale file is removed before the new attempt runs, so a retry can only be satisfied by fresh evidence.

This overlays only `campaign_adapter_argv` and `campaign_adapter_timeout_seconds` onto the matching lane's built-in `provider_config`; every other key in `code-mower.yml` is ignored for this purpose, so it cannot widen the general config contract. A missing `code-mower.yml`, a missing `lanes` key, or a lane with no matching entry is treated as no override. An existing repo config that fails to load, or is structurally malformed (a non-mapping `lanes`, lane, or `provider_config` entry), degrades to the safe `adapter_configuration_invalid` error code and an actionable status message, never a crash or a leaked template/traceback. `campaign_adapter_argv` must be a YAML list of non-empty scalar tokens (no shell strings; the adapter still runs with `shell=False`).

Code Mower maintainers shipping a built-in adapter for a provider instead add `campaign_adapter_argv` (and optionally `campaign_adapter_timeout_seconds`) directly to the provider's `provider_config` in `src/code_mower/provider_registry.py`, using the same placeholders and contract described above. Most adopters do not need to touch this file.

Hosted / SaaS providers (`hosted_bridge`/`saas_event` driver: Devin, Cursor BugBot) dispatch via a GitHub issue comment instead of a local adapter:

- Configure authentication tokens (`DEVIN_AUDIT_LABEL_TOKEN`, `CURSOR_BUGBOT_AUDIT_LABEL_TOKEN`, `GITHUB_TOKEN`) and supply `--issue <number>` plus `--repo-slug <OWNER/REPO>` (at creation, or on the `resume`/`dispatch` that first needs it). Without both, the hosted provider stays `unavailable` and no comment is posted.
- The dispatch comment states exactly what will be accepted. For an upgrade campaign it carries the campaign's exact `starting_version` in both the machine-readable `code_mower.releaseCampaignDispatch.v1` marker and the human-facing instructions, so a remote runner never has to guess which starting version to qualify from. Cold-install (and `unknown`) campaigns have no starting version and omit the field. An upgrade campaign whose stored `starting_version` is missing is never dispatched at all: the provider stays `unavailable` with the bounded `campaign_identity_incomplete` error code and no comment is posted.
- The provider's reply comment must embed a `CODE_MOWER_ADOPTION_RESULT` marker wrapping schema `code_mower.releaseCampaignResult.v1` with `campaign_id`, `provider`, `release_tag`, and `idempotency_key` matching the original dispatch, plus a validated `adoption_result`. A bare or unbound result is ignored so a stale or unrelated comment can never be replayed as evidence. The embedded `adoption_result`'s own `qualification_context` and `starting_version` must also match the campaign's exactly, independent of the wrapper's idempotency key -- a cold-install result cannot complete an upgrade campaign, and an upgrade result from one starting version cannot complete a same-tag upgrade campaign from a different starting version.
- These identity fields are visible in the public dispatch comment, so binding alone does not prove authorship -- anyone could reply with a matching marker. A result marker is only ever accepted from a GitHub comment author present in the lane's `provider_config.bot_authors` list (and, if configured, the comma-separated login list in the environment variable named by `provider_config.bot_authors_env`). A lane with no trusted authors configured trusts nobody; an untrusted or spoofed author's comment is ignored and the provider keeps running.

### Per-Release Operation

Any provider without a working adapter, credentials, or a bound remote result stays `unavailable`/manual. Record its result explicitly once qualification has actually happened elsewhere:

```bash
code-mower release campaign --release-tag v1.0.0 \
  --record-result path/to/adoption-result.json \
  --record-provider codex
```

The recorded file is validated against the same closed schema as automated results.
