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
- `package_identity`: Normalized (PEP 503) package name the result qualifies
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
- Package must be an exact index spec: `<name>==<version>`, e.g. `code-mower==1.0.0`.
  The package identity is derived from that spec and normalized the way a
  package index normalizes it, so `Code_Mower`, `code.mower`, and `code-mower`
  are one identity. Paths, URLs, VCS specs, and inexact requirements are
  rejected: they name no single package a result could be bound to
- `code-mower release qualify` (the built-in runner) additionally accepts only
  the `code-mower` package, because it installs the distribution into a clean
  virtualenv and reads the installed version back through the `code-mower`
  console script. Campaigns are not limited this way -- see below
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

- **Dry-run by default, applied once and for all:** Omit `--apply` for a safe preview. Add `--apply` for live local execution, GitHub comment dispatch, or paid runs. `applied` is a *monotonic* transition: a dry-run campaign becomes applied the first time it is dispatched with `--apply`, and nothing moves it back. A later `resume` or `--status` poll that simply omits `--apply` is not a claim that the dispatches and attempts already made never happened, so it leaves the campaign (and each dispatched provider's `dispatch_mode`) applied -- in stored state, in the rendered text, and on the Board. Previously such a poll relabelled real evidence as a dry-run preview and regressed the aggregate status to "run with --apply to dispatch providers" for providers that had already been dispatched. A poll still never dispatches anything: only `--apply` does that. The aggregate headline of a preview is held to the same standard as the individual providers it summarizes: `queued` / "run with --apply to dispatch providers" is only reported while at least one provider is genuinely dispatchable. When every provider that is not already complete is `unavailable` -- for a missing `--issue`, a missing `--repo-slug`, missing credentials, or an unconfigured adapter alike -- the campaign reports `unavailable` and the actionable "configure prerequisites for unavailable providers: ..." next action instead of pointing at an `--apply` run that could dispatch nothing. A mixed preview stays `queued`, and its detail line still counts the queued and unavailable providers separately.
- **Provider diversity:** Tracks Claude, Codex, Antigravity, Muse, Cursor/Grok Bot, and Devin. Missing tools, tokens, or adapters -- and a `code-mower.yml` that configures one lane under two spellings -- degrade gracefully to `unavailable` without failing the campaign. Each provider may appear at most once: `--providers` is canonicalized before any participant is built, so naming the same provider twice -- directly, or through two aliases of one lane such as `cursor` and `grok_bot` -- is rejected with an explicit error instead of creating two participants that share a single idempotency key and result path (which would let one provider's evidence count twice).
- **Idempotent resume:** Pass `--resume` to re-poll running providers or advance queued participants without duplicating dispatch or re-invoking an adapter that already completed. Once a provider's applied dispatch/adapter has been attempted (even if it failed or its outcome was uncertain), ordinary resume never repeats it automatically -- pass `--retry-provider <provider>` to explicitly retry that one provider. `--retry-provider` is rejected unless the named provider is already part of the campaign.
- **Never reinitialized:** An existing campaign is never replaced by a fresh queued one. Repeating the same invocation (same `--release-tag` or `--campaign-id`, with or without `--resume`) advances the stored campaign under resume semantics, so a repeated `--apply` never reruns a local adapter, reposts a hosted dispatch, or discards recorded provider state and evidence. The explicit `create` action fails when that campaign already exists, and `resume`/`dispatch` fail when it does not -- neither falls through to creating one. Creation arguments that describe a different campaign (`--package-spec`, `--providers`, `--qualification-context`, `--starting-version`, or a `--campaign-id`/`--release-tag` pair that disagree) are rejected explicitly rather than silently ignored. `--qualification-context` is compared whenever it is supplied, including an explicit `--qualification-context cold_install` against a stored upgrade campaign: the flag has no default value of its own, so an omitted flag (which advances the stored campaign under its own context, and creates a `cold_install` campaign when there is none) is distinguishable from an explicitly requested `cold_install` and the latter is never silently ignored. `--repo-slug` is the one field an existing campaign can still be *completed* with: a campaign created without a repository slug has nowhere to dispatch, so supplying `--repo-slug` on a later `resume`/`dispatch` fills the empty stored value and persists it before any hosted dispatch uses it. A `--repo-slug` that disagrees with a non-empty stored slug is rejected like any other identity change -- an in-flight campaign is never repointed at a different repository.
- **Serialized mutations, lock-free reads:** Every *mutating* `code-mower release campaign` invocation -- create, implicit create/advance, `resume`, `dispatch`, `--record-result`, `--retry-provider`, and the repository-slug fill -- takes an exclusive OS lock on the campaign directory and holds it across the whole sequence: reading stored state, claiming a provider attempt by stamping `attempted_at`, invoking a local adapter or posting a hosted dispatch, and writing the campaign back. Two such commands launched at the same time therefore run one after the other, and the second one reloads *after* the first has finished, so it sees the recorded `attempted_at` and declines the repeat exactly as a sequential resume would -- concurrency can never duplicate a local adapter run or a paid/hosted dispatch. The lock lives in `.code-mower/campaigns/.campaigns.lock`; because it is an OS lock on an open file descriptor, it is released automatically when a command exits, crashes, or is killed, so there is no stale-lock file to clean up and a dead run never blocks the next one. The lock is taken through one shared portable helper (`code_mower.file_locks`) used by the Board event store as well: POSIX `flock` where `fcntl` exists, and a `msvcrt` byte-range lock on Windows, retried on a bounded sleeping schedule rather than a spin. Both backends release on descriptor close, and neither module imports `fcntl` unconditionally any more, so the package imports on Windows. An invalid `--campaign-id` is rejected before the lock is taken, so it creates no directory, no lock file, and no campaign state. Reads are deliberately *not* serialized: `--status` and the Board projection take no lock at all, so they answer against a read-only campaign directory and complete while a long applied run holds the lock. That is safe because status is *strictly* read-only: `--status` (or the `status` action) combined with a mutating intent -- `--retry-provider`, `--record-result`, `--apply`, `--resume`, or a conflicting non-status action -- is rejected with a bounded non-zero error before any lock, mutation, poll, or dispatch. A mutation is never executed under a read-only spelling (which would also run it outside the serialization contract), and never silently dropped while the command reports success, as `--status --retry-provider` previously did. Campaign files are published with a single atomic rename from a per-write temporary file, so a lock-free read never observes a half-written or blended campaign.
- **One-to-one campaign ids:** A campaign id is its storage key: it is used verbatim as the stem of `.code-mower/campaigns/<campaign-id>.json`, with no substitution. `--campaign-id` accepts only lowercase ASCII letters, digits, `.`, `_`, and `-`, starting with a letter or digit, up to 64 characters; anything else is rejected before any lookup or save with a bounded error and no traceback. Lowercase-only keeps ids stable on case-insensitive volumes -- APFS on macOS is case-insensitive by default -- where `Campaign-A` and `campaign-a` would otherwise be one file holding two campaigns' state. The leading letter-or-digit rule excludes `.`, `..`, and every dotfile spelling (including the `.tmp.` write-staging prefix and the directory lock file), and the alphabet excludes path separators, so an id can neither traverse out of the campaign directory nor address internal storage. The generated default, `campaign-<release-tag>`, always satisfies this contract. Previously ids were sanitized to fit a filename, which made `campaign/a` and `campaign_a` collide on one file so naming either could advance and overwrite the other.
- **Bounded status lookups:** `--status` with an explicit `--campaign-id` or `--release-tag` reports that campaign or exits non-zero with a bounded "no campaign found" message -- it never falls back to an unrelated campaign's data. Only an unqualified `--status` (no identifier) reports the most recently updated campaign.
- **Campaign ids resolve by campaign id:** An explicit `--campaign-id` reads only `.code-mower/campaigns/<campaign-id>.json`, and accepts it only when the `campaign_id` stored *inside* it is exactly the id that was asked for. There is no directory scan and no fallback, so naming an id that no campaign is stored under reports "no campaign found" rather than resolving to some other campaign that merely carries that text as its *release tag* -- which status would have reported, and which `resume`/`dispatch` would have advanced and paid for. A file whose name and stored id disagree (hand-edited, copied, or restored from elsewhere) is likewise not treated as that campaign.
- **Release tags resolve by release tag:** A campaign id is a storage key; a release tag is not. Whenever `--release-tag` alone identifies the campaign -- for `status`, `resume`, `dispatch`, or an implicit advance -- the lookup matches only campaigns whose stored `release_tag` field is exactly that tag, and ignores filenames entirely. A tag that also happens to be a well-formed campaign id (`v1.0.0` is one) therefore cannot select a campaign someone stored under that text as a custom `--campaign-id` for a *different* release. If more than one campaign carries the tag (possible only via custom ids), the request is rejected as ambiguous with a bounded error naming at most a few ids and asking for `--campaign-id`, rather than resolved to whichever file the directory happened to list first. Supplying both `--campaign-id` and `--release-tag` still requires both fields to match. Because creation resolves by tag, the id a new campaign would be created under is checked against the campaign directory first: an id already occupied by a stored file is refused instead of overwritten.
- **Local resilience:** Campaign state is stored in `.code-mower/campaigns/`. Local status remains fully readable during GitHub or provider network outages.
- **Board visibility:** Active campaigns surface directly on Code Mower Board with release, provider, environment, elapsed time, state, and actionable next steps.
- **Bound to this campaign's package:** A campaign is created from an exact package-index spec, and every result it accepts -- a local drop-in file, an adapter's own output, a trusted GitHub comment, or `--record-result` -- must report the normalized package identity derived from *that spec*. A result for a different distribution is refused through the same bounded `adapter_result_invalid` path as any other schema failure, and never enters campaign state. Because the expected identity comes from the campaign's own spec rather than a hard-coded package name, a campaign for a package other than Code Mower is neither refused at creation nor satisfiable by a Code Mower result.
- **Never fabricated:** A provider can only reach `complete` when its own adapter command actually ran (argv only, never a shell) and produced a result file that passes closed-schema validation and matches the campaign's provider, release tag, package identity, and (for GitHub comment results) idempotency key. The idempotency key itself is derived from the campaign id, provider, release tag, qualification context, and starting version, so an upgrade campaign from one starting version can never be satisfied by a same-tag dispatch from a different starting version. For GitHub comment results, the embedded adoption result's own `qualification_context` and `starting_version` are also checked directly against the campaign's expected values -- this holds even if a wrapper's idempotency key were copied or generated incorrectly. Code Mower never runs its own local qualification and relabels the result as another provider's.

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

`campaign_adapter_timeout_seconds` must be a positive integer number of seconds, written as one: a fractional value such as `1.9` is rejected with the bounded `adapter_configuration_invalid` error rather than truncated to a shorter budget than was configured, as are `0`, negatives, non-finite numbers, and booleans. Omitting it uses the built-in default.

Supported placeholders: `{command}` (resolved binary), `{release_tag}`, `{package_spec}`, `{qualification_context}`, `{starting_version}`, `{output}`, `{repo_path}`. The adapter command must write a `code_mower.adoptionResult.v1` JSON document to the `{output}` path whose `provider`, `release_tag`, `qualification_context`, and `starting_version` fields all match the campaign's -- a cold-install result cannot complete an upgrade campaign, and an upgrade result must match the campaign's exact starting version. Anything else (extra fields, mismatched identity, no file, non-zero exit, or a timeout) leaves the provider `unavailable`/`blocked` with a bounded error code -- never a fabricated pass. A local drop-in result file and `--record-result` are bound the same way. Install the provider's CLI binary on PATH and verify local authentication.

An explicit `--retry-provider` never accepts a pre-existing result file for that provider -- the stale file is removed before the new attempt runs, so a retry can only be satisfied by fresh evidence.

This overlays only `campaign_adapter_argv` and `campaign_adapter_timeout_seconds` onto the matching lane's built-in `provider_config`; every other key in `code-mower.yml` is ignored for this purpose, so it cannot widen the general config contract. A missing `code-mower.yml`, a missing `lanes` key, or a lane with no matching entry is treated as no override. Configure each lane under **exactly one** spelling: `muse` and `muse_cli` (or `claude` and `claude_code`) name one lane with one adapter command, so a config that declares more than one of them is *ambiguous*, not merely redundant -- the entries may carry two different `campaign_adapter_argv` values and there is no correct choice between two adapter commands. That is rejected with the bounded `adapter_configuration_invalid` error code and a detail naming the conflicting spellings (drawn only from the built-in alias table, so the message never echoes config text), rather than silently running whichever spelling happened to be looked up first. An existing repo config that fails to load, or is structurally malformed (a non-mapping `lanes`, lane, or `provider_config` entry), degrades to the safe `adapter_configuration_invalid` error code and an actionable status message, never a crash or a leaked template/traceback. `campaign_adapter_argv` must be a YAML list of non-empty scalar tokens (no shell strings; the adapter still runs with `shell=False`).

Code Mower maintainers shipping a built-in adapter for a provider instead add `campaign_adapter_argv` (and optionally `campaign_adapter_timeout_seconds`) directly to the provider's `provider_config` in `src/code_mower/provider_registry.py`, using the same placeholders and contract described above. Most adopters do not need to touch this file.

Hosted / SaaS providers (`hosted_bridge`/`saas_event` driver: Devin, Cursor BugBot) dispatch via a GitHub issue comment instead of a local adapter:

- Configure authentication tokens (`DEVIN_AUDIT_LABEL_TOKEN`, `CURSOR_BUGBOT_AUDIT_LABEL_TOKEN`, `GITHUB_TOKEN`) and supply `--issue <number>` plus `--repo-slug <OWNER/REPO>` (at creation, or on the `resume`/`dispatch` that first needs it). Without both, the hosted provider stays `unavailable` and no comment is posted. The dry-run preview judges this prerequisite exactly as `--apply` does: a hosted provider with valid credentials but no issue number previews as `unavailable` with the bounded `missing_issue_number` error code and a next action naming `--issue`, rather than as queued and ready to dispatch.
- The dispatch comment states exactly what will be accepted. For an upgrade campaign it carries the campaign's exact `starting_version` in both the machine-readable `code_mower.releaseCampaignDispatch.v1` marker and the human-facing instructions, so a remote runner never has to guess which starting version to qualify from. Cold-install (and `unknown`) campaigns have no starting version and omit the field. An upgrade campaign whose stored `starting_version` is missing is never dispatched at all: the provider stays `unavailable` with the bounded `campaign_identity_incomplete` error code and no comment is posted.
- The provider's reply comment must embed a `CODE_MOWER_ADOPTION_RESULT` marker as a single-line HTML comment on a line of its own (`<!-- CODE_MOWER_ADOPTION_RESULT: {...} -->`), wrapping schema `code_mower.releaseCampaignResult.v1` with `campaign_id`, `provider`, `release_tag`, and `idempotency_key` matching the original dispatch, plus a validated `adoption_result`. A bare or unbound result is ignored so a stale or unrelated comment can never be replayed as evidence. The embedded `adoption_result`'s own `qualification_context` and `starting_version` must also match the campaign's exactly, independent of the wrapper's idempotency key -- a cold-install result cannot complete an upgrade campaign, and an upgrade result from one starting version cannot complete a same-tag upgrade campaign from a different starting version. The marker line is matched end to end and its JSON is captured through the object's own final brace, so a literal `-->` inside a permitted string value cannot truncate an otherwise valid trusted result; a marker whose JSON is genuinely malformed is still ignored (fail-closed), never guessed at.
- These identity fields are visible in the public dispatch comment, so binding alone does not prove authorship -- anyone could reply with a matching marker. A result marker is only ever accepted from a GitHub comment author present in the lane's `provider_config.bot_authors` list (and, if configured, the comma-separated login list in the environment variable named by `provider_config.bot_authors_env`). A lane with no trusted authors configured trusts nobody; an untrusted or spoofed author's comment is ignored and the provider keeps running.

### Per-Release Operation

Any provider without a working adapter, credentials, or a bound remote result stays `unavailable`/manual. Record its result explicitly once qualification has actually happened elsewhere:

```bash
code-mower release campaign --release-tag v1.0.0 \
  --record-result path/to/adoption-result.json \
  --record-provider codex
```

The recorded file is validated against the same closed schema as automated results.
