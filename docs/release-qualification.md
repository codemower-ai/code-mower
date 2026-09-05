# Release Qualification

The `code-mower release qualify` command runs local release qualification with a stable adoption-result schema.

Release qualification proves that an exact package can be installed and can
exercise Code Mower's operational surfaces in a provider environment. It does
not compare builder quality, calibrate reviewer findings, justify lane
promotion, or measure model cost. Use [Builder Experiments](builder-experiments.md)
and the [Lane Promotion Policy](lane-promotion-policy.md) for those decisions.

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

To qualify an exact TestPyPI candidate before it is announced or marked
current on production PyPI, add `--package-source testpypi`:

```bash
code-mower release qualify \
  --release-tag v1.0.0 \
  --package-spec code-mower==1.0.0 \
  --output result.json \
  --qualification-context cold_install \
  --package-source testpypi \
  --execute
```

`--package-source` is a closed vocabulary: `pypi` (the default) or `testpypi`.
It never accepts an arbitrary index URL. Pip does not prioritize
`--index-url` over `--extra-index-url`: a single install command naming both
the canonical TestPyPI simple index and production PyPI cannot prove which
one actually supplied the candidate, since an identical version already on
production PyPI could silently satisfy it instead. `testpypi` therefore runs
a closed two-stage install: it first downloads the exact candidate artifact
with the canonical TestPyPI simple index (`https://test.pypi.org/simple/`)
as the *only* configured index and `--no-deps`, verifies exactly one
artifact came back and that its filename names the requested package
identity and version -- failing closed on zero, multiple, malformed, or
mismatched artifacts -- and only then installs that verified local artifact
file, resolving its dependencies (which are not part of this release
candidate) from production PyPI (`https://pypi.org/simple/`). Both steps run
pip in isolated mode with ambient pip index variables and configuration
disabled, so a workstation's extra indexes cannot widen either source. Omit the flag,
or pass `--package-source pypi` explicitly, for the production-PyPI default,
which applies no index override at all. TestPyPI installs reuse the same
bounded pip-install retry behavior as any other package-index install (see
[Cache Bypass And Propagation Triage](pypi-release.md#cache-bypass-and-propagation-triage)).

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
- Executed results are timestamp-bounded: `timestamp_utc` must not predate
  `2020-01-01T00:00:00Z` and must not be more than 300 seconds in the future
  (deterministic clock-skew tolerance). Planned previews are exempt
- Step ids use the built-in taxonomy (`board`, `doctor`, `lanes_status`,
  `overhead`, `package_install`) or an explicit namespaced provider extension
  `<namespace>__<name>` with both halves safe identifiers; arbitrary
  unnamespaced ids are rejected
- All timings are finite and non-negative, and the step `elapsed_seconds`
  values must sum to within 1.0 second of the total. The built-in runner records
  setup and serialization time outside named checks as the metadata-only
  `overhead` step; the tolerance covers only rounding.
- Owner-action counts agree with the outcome: a step with status `pass` must
  report `owner_action_count: 0`, and outcome `pass` requires zero owner
  actions overall

## Release Campaigns

The `code-mower release campaign` command coordinates multi-provider qualification across Claude, Codex, Antigravity, Muse, Cursor Cloud Agent, and Devin:

```bash
code-mower release campaign \
  --release-tag v1.0.0 \
  --package-spec code-mower==1.0.0 \
  --qualification-context cold_install
```

### TestPyPI candidates

Add `--package-source testpypi` to qualify an exact TestPyPI candidate before
it is announced or marked current on production PyPI:

```bash
code-mower release campaign \
  --release-tag v1.0.0 \
  --package-spec code-mower==1.0.0 \
  --qualification-context cold_install \
  --package-source testpypi
```

`--package-source` is closed to `pypi` (the default) or `testpypi` -- never an
arbitrary index URL or credential. It is bound into the campaign the same way
`--package-spec` and `--qualification-context` are:

- it is part of every provider's idempotency key, so a dispatch/result pair
  for one source can never satisfy a same-tag campaign from a different
  source;
- local adapters receive it (as `{package_source}`) and construct their
  install commands against the canonical TestPyPI simple index
  (`https://test.pypi.org/simple/`) with production PyPI
  (`https://pypi.org/simple/`) as a dependency-only extra index;
- a hosted dispatch comment states the source (and, for `testpypi`, both
  canonical index URLs) in its machine-readable marker and human-facing
  instructions, so a remote provider never has to guess which index to
  install from; a reply's own declared source must match, or it is ignored
  like any other unbound comment;
- resuming or dispatching an existing campaign with a different
  `--package-source` is rejected as an identity conflict, exactly like a
  conflicting `--qualification-context`.

A campaign created without `--package-source` -- including one stored before
this field existed -- reads back as `pypi`, the default production behavior.
TestPyPI installs reuse the same bounded pip-install retry behavior as any
other package-index install; see
[Cache Bypass And Propagation Triage](pypi-release.md#cache-bypass-and-propagation-triage).

- **Dry-run by default, applied once and for all:** Omit `--apply` for a safe preview. Add `--apply` for live local execution, GitHub comment dispatch, or paid runs. `applied` is a *monotonic* transition: a dry-run campaign becomes applied the first time it is dispatched with `--apply`, and nothing moves it back. A later `resume` or `--status` poll that simply omits `--apply` is not a claim that the dispatches and attempts already made never happened, so it leaves the campaign (and each dispatched provider's `dispatch_mode`) applied -- in stored state, in the rendered text, and on the Board. Previously such a poll relabelled real evidence as a dry-run preview and regressed the aggregate status to "run with --apply to dispatch providers" for providers that had already been dispatched. A poll still never dispatches anything: only `--apply` does that. The aggregate headline of a preview is held to the same standard as the individual providers it summarizes: `queued` / "run with --apply to dispatch providers" is only reported while at least one provider is genuinely dispatchable. When every provider that is not already complete is `unavailable` -- for a missing `--issue`, a missing `--repo-slug`, missing credentials, or an unconfigured adapter alike -- the campaign reports `unavailable` and the actionable "configure prerequisites for unavailable providers: ..." next action instead of pointing at an `--apply` run that could dispatch nothing. A mixed preview stays `queued`, and its detail line still counts the queued and unavailable providers separately.
- **Provider diversity:** Tracks Claude, Codex, Antigravity, Muse, Cursor Cloud Agent, and Devin. Missing tools, tokens, or adapters -- and a `code-mower.yml` that configures one lane under two spellings -- degrade gracefully to `unavailable` without failing the campaign. Each provider may appear at most once: `--providers` is canonicalized before any participant is built, so naming the same provider twice -- directly, or through two aliases of one lane such as `cursor` and `cursor_cloud_agent` -- is rejected with an explicit error instead of creating two participants that share a single idempotency key and result path (which would let one provider's evidence count twice).
- **Idempotent resume:** Pass `--resume` to re-poll running providers or advance queued participants without duplicating dispatch or re-invoking an adapter that already completed. Once a provider's applied dispatch/adapter has been attempted (even if it failed or its outcome was uncertain), ordinary resume never repeats it automatically -- pass `--retry-provider <provider>` to explicitly retry that one provider. `--retry-provider` is rejected unless the named provider is already part of the campaign.
- **A hosted dispatch is checkpointed as pollable before it is posted:** Posting the dispatch comment is an external side effect that the campaign cannot undo and cannot re-observe, so everything a later resume needs is persisted *first*: the attempt (`attempted_at`), the `running` state, the issue the comment is addressed to (in `dispatch_ref`), the applied dispatch mode, and the matching campaign status and next action. A process killed anywhere around the post therefore leaves a campaign that an ordinary `--resume` polls to a conclusion against the original issue -- accepting the trusted, identity-bound result if the comment did get posted and answered -- and that never reposts on its own. Previously the campaign recorded only `attempted_at` and stayed `queued` until the post returned, which resume neither polled (not running) nor redispatched (already attempted): the provider stalled until an explicit retry posted a second comment for a dispatch that may well have succeeded. The checkpoint never claims the post succeeded -- `dispatched_at` is stamped and `dispatch_ref` is replaced with the returned dispatch metadata only when the post returns successfully; a dispatch that fails in-process records the usual `github_dispatch_failed` unavailable result. If nothing was ever posted, resume simply keeps polling and finds nothing, and dispatching again still requires an explicit `--retry-provider` (which, being explicit, may post a second comment). A retry that cannot dispatch for a prerequisite reason -- no `--issue`, missing credentials -- leaves an outstanding dispatch `running` and pollable rather than demoting it to `unavailable`, since refusing to dispatch reveals nothing about the comment already posted.
- **Never reinitialized:** An existing campaign is never replaced by a fresh queued one. Repeating the same invocation (same `--release-tag` or `--campaign-id`, with or without `--resume`) advances the stored campaign under resume semantics, so a repeated `--apply` never reruns a local adapter, reposts a hosted dispatch, or discards recorded provider state and evidence. The explicit `create` action fails when that campaign already exists, and `resume`/`dispatch` fail when it does not -- neither falls through to creating one. A request that asks for two actions at once is refused rather than resolved to one of them: an action may be spelled with the equivalent legacy flag (`status` with `--status`, `resume`/`dispatch` with `--resume`), but combining an action with a flag naming a *different* action -- `create --resume` above all -- exits non-zero with a bounded conflict message before any campaign lookup, directory creation, lock, state write, adapter run, dispatch, or poll, so the rejected request leaves nothing behind. Previously `create --resume` reached the command body with both intents live and was answered by whichever branch tested its flag first, reporting "no existing campaign to resume" for an explicit `create`. Creation arguments that describe a different campaign (`--package-spec`, `--providers`, `--qualification-context`, `--starting-version`, `--package-source`, or a `--campaign-id`/`--release-tag` pair that disagree) are rejected explicitly rather than silently ignored. `--qualification-context` and `--package-source` are each compared whenever supplied, including an explicit `--qualification-context cold_install` or `--package-source pypi` against a stored campaign with the same value: neither flag has a default value of its own, so an omitted flag (which advances the stored campaign under its own context/source, and creates a `cold_install`/`pypi` campaign when there is none) is distinguishable from an explicitly requested default, and an explicit default is never silently ignored. A campaign created without `--package-source` (or an explicit `pypi`) stays `pypi`; resuming it with `--package-source testpypi` is rejected as an identity conflict, not a resume, exactly like a conflicting `--qualification-context`. A campaign stored before this field existed reads back as `pypi`, its documented default. `--repo-slug` is the one field an existing campaign can still be *completed* with: a campaign created without a repository slug has nowhere to dispatch, so supplying `--repo-slug` on a later `resume`/`dispatch` fills the empty stored value and persists it before any hosted dispatch uses it. A `--repo-slug` that disagrees with a non-empty stored slug is rejected like any other identity change -- an in-flight campaign is never repointed at a different repository.
- **Serialized mutations, lock-free reads:** Every *mutating* `code-mower release campaign` invocation -- create, implicit create/advance, `resume`, `dispatch`, `--record-result`, `--retry-provider`, and the repository-slug fill -- takes an exclusive OS lock on the campaign directory and holds it across the whole sequence: reading stored state, claiming a provider attempt by stamping `attempted_at`, invoking a local adapter or posting a hosted dispatch, and writing the campaign back. Two such commands launched at the same time therefore run one after the other, and the second one reloads *after* the first has finished, so it sees the recorded `attempted_at` and declines the repeat exactly as a sequential resume would -- concurrency can never duplicate a local adapter run or a paid/hosted dispatch. The lock lives in `.code-mower/campaigns/.campaigns.lock`; because it is an OS lock on an open file descriptor, it is released automatically when a command exits, crashes, or is killed, so there is no stale-lock file to clean up and a dead run never blocks the next one. The lock is taken through one shared portable helper (`code_mower.file_locks`) used by the Board event store as well: POSIX `flock` where `fcntl` exists, and a `msvcrt` byte-range lock on Windows, retried on a bounded sleeping schedule rather than a spin. Both backends release on descriptor close, and neither module imports `fcntl` unconditionally any more, so the package imports on Windows. An invalid `--campaign-id` is rejected before the lock is taken, so it creates no directory, no lock file, and no campaign state. Reads are deliberately *not* serialized: `--status` and the Board projection take no lock at all, so they answer against a read-only campaign directory and complete while a long applied run holds the lock. That is safe because status is *strictly* read-only: `--status` (or the `status` action) combined with a mutating intent -- `--retry-provider`, `--record-result`, `--apply`, `--resume`, or a conflicting non-status action -- is rejected with a bounded non-zero error before any lock, mutation, poll, or dispatch. A mutation is never executed under a read-only spelling (which would also run it outside the serialization contract), and never silently dropped while the command reports success, as `--status --retry-provider` previously did. Campaign files are published with a single atomic rename from a per-write temporary file, so a lock-free read never observes a half-written or blended campaign.
- **One-to-one campaign ids:** A campaign id is its storage key: it is used verbatim as the stem of `.code-mower/campaigns/<campaign-id>.json`, with no substitution. `--campaign-id` accepts only lowercase ASCII letters, digits, `.`, `_`, and `-`, starting with a letter or digit, up to 64 characters; anything else is rejected before any lookup or save with a bounded error and no traceback. Lowercase-only keeps ids stable on case-insensitive volumes -- APFS on macOS is case-insensitive by default -- where `Campaign-A` and `campaign-a` would otherwise be one file holding two campaigns' state. The leading letter-or-digit rule excludes `.`, `..`, and every dotfile spelling (including the `.tmp.` write-staging prefix and the directory lock file), and the alphabet excludes path separators, so an id can neither traverse out of the campaign directory nor address internal storage. The generated default, `campaign-<release-tag>`, always satisfies this contract. Previously ids were sanitized to fit a filename, which made `campaign/a` and `campaign_a` collide on one file so naming either could advance and overwrite the other.
- **Bounded status lookups:** `--status` with an explicit `--campaign-id` or `--release-tag` reports that campaign or exits non-zero with a bounded "no campaign found" message -- it never falls back to an unrelated campaign's data. Only an unqualified `--status` (no identifier) reports the most recently updated campaign.
- **Campaign ids resolve by campaign id:** An explicit `--campaign-id` reads only `.code-mower/campaigns/<campaign-id>.json`, and accepts it only when the `campaign_id` stored *inside* it is exactly the id that was asked for. There is no directory scan and no fallback, so naming an id that no campaign is stored under reports "no campaign found" rather than resolving to some other campaign that merely carries that text as its *release tag* -- which status would have reported, and which `resume`/`dispatch` would have advanced and paid for. A file whose name and stored id disagree (hand-edited, copied, or restored from elsewhere) is likewise not treated as that campaign.
- **Release tags resolve by release tag:** A campaign id is a storage key; a release tag is not. Whenever `--release-tag` alone identifies the campaign -- for `status`, `resume`, `dispatch`, or an implicit advance -- the lookup matches only campaigns whose stored `release_tag` field is exactly that tag, and ignores filenames entirely. A tag that also happens to be a well-formed campaign id (`v1.0.0` is one) therefore cannot select a campaign someone stored under that text as a custom `--campaign-id` for a *different* release. If more than one campaign carries the tag (possible only via custom ids), the request is rejected as ambiguous with a bounded error naming at most a few ids and asking for `--campaign-id`, rather than resolved to whichever file the directory happened to list first. Supplying both `--campaign-id` and `--release-tag` still requires both fields to match. Because creation resolves by tag, the id a new campaign would be created under is checked against the campaign directory first: an id already occupied by a stored file is refused instead of overwritten.
- **Local resilience:** Campaign files stay in `.code-mower/campaigns/` of the checkout that wrote them, or in an explicit `--campaigns-dir`. Writes never copy, merge, or rewrite another checkout's campaign files merely to discover them. A metadata-only user-level index under `$CODE_MOWER_STATE_DIR/campaign-discovery` (default `~/.cache/code-mower/campaign-discovery`) records repository identity, campaign ids, timestamps, and the directory needed to reopen storage. `status`, `watch`, `upload`, and Board then resolve the same campaign set from another worktree or clone of that repository without an explicit path. `--campaigns-dir` remains authoritative for that invocation. If more than one campaign matches a tag or the same campaign id is stored in more than one directory, the command fails closed and names only safe campaign ids -- never local paths. A missing or malformed index is ignored and the current checkout's repo-local files remain readable. Status against a read-only campaign directory still writes nothing to campaign files. The index is not uploaded.
- **Board visibility:** Active campaigns surface directly on Code Mower Board with release, provider, environment, elapsed time, state, and actionable next steps. In-flight local CLI adapters checkpointed with attempted_at while still persisted queued render as running on Board only while plausibly live within their effective adapter timeout; once that bounded window is exceeded without completion evidence, Board projects an actionable queued state with retry guidance rather than running indefinitely.
- **Adoption doctor:** Run `code-mower doctor --adoption` to validate release campaign readiness across configured providers, adapters, credentials, authentication, writable storage, and Board visibility before dispatching.
- **Bound to this campaign's package:** A campaign is created from an exact package-index spec, and every result it accepts -- a local drop-in file, an adapter's own output, a trusted GitHub comment, or `--record-result` -- must report the normalized package identity derived from *that spec*. A result for a different distribution is refused through the same bounded `adapter_result_invalid` path as any other schema failure, and never enters campaign state. Because the expected identity comes from the campaign's own spec rather than a hard-coded package name, a campaign for a package other than Code Mower is neither refused at creation nor satisfiable by a Code Mower result.
- **Never fabricated:** A provider can only reach `complete` when its own adapter command actually ran (argv only, never a shell) and produced a result file that passes closed-schema validation and matches the campaign's provider, release tag, package identity, and (for GitHub comment results) idempotency key. The idempotency key itself is derived from the campaign id, provider, release tag, qualification context, and starting version, so an upgrade campaign from one starting version can never be satisfied by a same-tag dispatch from a different starting version. For GitHub comment results, the embedded adoption result's own `qualification_context` and `starting_version` are also checked directly against the campaign's expected values -- this holds even if a wrapper's idempotency key were copied or generated incorrectly. Code Mower never runs its own local qualification and relabels the result as another provider's.

### Provider Adapter Setup (one-time, per provider)

Local CLI providers (`local_cli` driver) only run automatically if a `campaign_adapter_argv` is configured for that provider's lane. Four lanes ship a maintained adapter (`src/code_mower/campaign_adapters.py`, invoked as `{python} -m code_mower.campaign_adapters ...` with `shell=False`); every other local CLI lane (e.g. aider) ships none and stays `unavailable`/manual rather than faking a result.

| Lane | Adapter provider | Verified CLI surface |
|---|---|---|
| `codex` | `codex` | `codex exec` with stdin (`-`), `--ephemeral`, `--approve-for-me`, a Code Mower-owned root-deny/workspace-write profile, keyring-only auth, `--skip-git-repo-check`, `--json`, `--output-schema`, `--output-last-message`, `-C` (codex-cli 0.147.0) |
| `claude_audit` | `claude` | `claude --print` with stdin, `--output-format json`, strict OS sandbox and PyPI allowlist, `--json-schema` (Claude Code 2.1.258) |
| `antigravity_cli` | `antigravity` | `agy --print` with a prompt file, `--sandbox`, noninteractive permission approval, `--new-project`, `--add-dir`, `--print-timeout` |
| `muse_cli` | `muse` | `muse exec` with `--json`, `--prompt-file`, `--workspace` (Muse Code 1.0.3) |

Newer CLIs keep working while the flags exist; a removed flag fails closed. Prompts travel on stdin or a prompt file, never through a shell. Provider children receive an allowlisted environment with home/config locations for their stored login, but no ambient GitHub, Code Mower cloud, or provider API keys; Muse's explicit API key travels on stdin only. Provider stdout/stderr are parsed transiently and never persisted: only a closed, validated `code_mower.adoptionResult.v1` document is written to `{output}`. Codex gets outbound network access inside its ephemeral workspace-write sandbox for package downloads; its CLI does not provide a domain allowlist here. Claude requires its OS sandbox, disables the unsandboxed escape hatch, denies Bash access to the operator's home directory, and allows package-download network access only to PyPI. Antigravity runs with its CLI sandbox retained, `--new-project` passed on every campaign run for project boundary isolation (strictly excluding continue or resume semantics: `--continue`, `-c`, `--conversation`, `--resume`, `-i`, `--prompt-interactive`), and noninteractive permission prompts auto-approved inside that sandbox; this is required because headless `agy --print` cannot answer command prompts. Before writing prompt input or invoking provider work, the adapter checks installed `agy` CLI capability via `--help` for `--new-project` support and fails closed if unsupported. Antigravity/Muse refuse without their ambient-home opt-in (`ANTIGRAVITY_CLI_USE_AMBIENT_HOME` / `MUSE_CLI_USE_AMBIENT_HOME`) or a provider key, mirroring the audit wrappers.

Timeout model: each maintained lane sets `campaign_adapter_timeout_seconds: 900`. The campaign passes the outer timeout minus `ADAPTER_INNER_TIMEOUT_MARGIN_SECONDS` (30s) as the adapter's `{adapter_timeout}`, so the adapter's own provider budget always fires first. `{python}` resolves to the running interpreter (launching the adapter); `{target_python}` to the deterministically resolved Python 3.12+ runtime binary passed to `--python-bin`; `{command}` to the installed provider CLI (the campaign refuses to run when it is missing).

**Adopters: override or disable per-repo in `code-mower.yml`.** `campaign_adapter_argv` replaces the maintained template wholesale (same placeholders plus `{python}`, `{target_python}`, and `{adapter_timeout}`); `campaign_adapter_enabled: false` disables the lane's adapter so the provider degrades to `unavailable`/manual. Only `campaign_adapter_argv`, `campaign_adapter_timeout_seconds`, and `campaign_adapter_enabled` are read here.

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

Supported placeholders: `{command}` (resolved binary), `{release_tag}`, `{package_spec}`, `{qualification_context}`, `{starting_version}`, `{package_source}`, `{output}`, `{repo_path}`, `{python}` (running interpreter), `{target_python}` (resolved Python 3.12+ binary), `{target_runtime}`, `{adapter_timeout}`. The maintained adapters pass `{package_source}` straight through to `code_mower.campaign_adapters`, which tells the provider CLI which closed index to install the candidate from (see [TestPyPI candidates](#testpypi-candidates) below); the adoption-result schema itself carries no separate source field, since it does not change which package or version a result is bound to. The adapter command must write a `code_mower.adoptionResult.v1` JSON document to the `{output}` path whose `provider` and `executor` both match the invoked provider, and whose `release_tag`, `qualification_context`, and `starting_version` fields all match the campaign's -- a cold-install result cannot complete an upgrade campaign, and an upgrade result must match the campaign's exact starting version. Anything else (extra fields, mismatched identity, no file, non-zero exit, or a timeout) leaves the provider `unavailable`/`blocked` with a bounded error code -- never a fabricated pass. A local drop-in result file and `--record-result` are bound the same way. Install the provider's CLI binary on PATH and verify local authentication.

Codex campaign runs use an isolated `CODEX_HOME` at `~/.config/code-mower/provider-homes/codex` (override with `CODE_MOWER_CODEX_CAMPAIGN_HOME`). Code Mower creates its non-secret restricted config automatically and refuses a readable `auth.json`. Authenticate that home once with `CODEX_HOME="$HOME/.config/code-mower/provider-homes/codex" codex login --device-auth -c 'cli_auth_credentials_store="keyring"' --enable secret_auth_storage`; the explicit login flags make Codex store that home-specific credential in the OS keyring even before Code Mower has created the config file. The adapter preserves the real OS `HOME` only so the platform keyring can locate the user's login keychain; Codex configuration and state remain isolated under `CODEX_HOME`, ambient token variables are removed, and the root-deny policy lets the agent write only its disposable workspace. Network remains available for package installation. A previous result file is removed before every adapter attempt, and a failed run never leaves stale evidence for a caller to accept.

An explicit `--retry-provider` never accepts a pre-existing result file for that provider -- the stale file is removed before the new attempt runs, so a retry can only be satisfied by fresh evidence. A retry advances only the retried provider: every other participant keeps its recorded state, evidence, and attempt, dispatch, and completion timestamps (aggregate campaign fields still recompute), and newly arrived evidence for them waits for the next ordinary resume. The superseded attempt leaves one bounded metadata-only summary per retry (`attempt_history`, most recent 5: timestamps, state, outcome, error code, and elapsed time -- never results, output, paths, or secrets). Retained entries are rebuilt from those allowed scalar fields on every retry, so a malformed or hand-edited stored history is sanitized (unknown/nested fields dropped, malformed entries discarded) rather than copied verbatim.

### Campaign Authentication & Runtime Readiness

An installed CLI and a valid argv contract do not prove the isolated home the adapter runs under is authenticated: without this check, a campaign is dispatched and only then fails with a generic adapter error, after paid work has started.

- **Codex isolated auth probe**: Where Codex exposes `codex login status` (20s budget), doctor probes the isolated home. Authenticated passes; confirmed logged-out produces an actionable warning and removes Codex from `ready_providers`. Timeouts or probe errors degrade safely to a skip.
- **Antigravity & Muse ambient-home opt-ins**: Antigravity and Muse require trusted ambient-home opt-ins (`ANTIGRAVITY_CLI_USE_AMBIENT_HOME=1`, `MUSE_CLI_USE_AMBIENT_HOME=1` or `META_API_KEY`/`META_API_KEY_FILE`). Doctor models these requirements directly: missing opt-ins produce an actionable warning and prevent the provider from being reported `campaign-ready`.
- **Antigravity campaign isolation & --new-project capability**: Every maintained Antigravity campaign invocation includes `--new-project` and excludes continue/resume flags (`--continue`, `-c`, `--conversation`, `--resume`, `-i`, `--prompt-interactive`) to guarantee session isolation inside a fresh project boundary. Both the adapter and doctor campaign readiness call the same bounded capability check (`agy --help`) before writing prompts, invoking provider work, or declaring Antigravity ready. When `--new-project` is unsupported, doctor excludes Antigravity from `ready_providers` with bounded actionable metadata and remediation instructing to upgrade to a version whose `--help` exposes `--new-project` (without claiming an unverified minimum version). Probe output, prompts, paths, and secrets are strictly excluded from evidence.
- **Structured-result capability**: Doctor distinguishes executable/auth readiness from structured-result capability using a bounded offline fixture (zero token spend, zero network). `doctor.campaign.readiness` detail breaks down `command`, `auth`, and `structured_result` per provider without leaking paths or command output.
- **Deterministic Python 3.12+ runtime resolution**: Before invoking local provider adapters, the campaign runner resolves a supported Python 3.12+ executable (probing `CODE_MOWER_PYTHON`, running interpreter, and versioned `python3.12+` binaries on PATH) and passes exact `--python-bin` and `--target-runtime` arguments. Providers must not pick ambient `python3`. If no supported runtime exists, the dispatch fails closed with `python_runtime_unavailable` and actionable remediation. Result validators enforce `runtime_class >= python_3.12`.

Probe stdout/stderr is never persisted: only bounded status tokens, registered error codes, and non-content output shape reach doctor JSON, so account names, tokens, credential contents, and local paths cannot leak. `hosted-builders` and `orchestrator-only` postures already skip local adapter checks, so no probe runs there. Set `CODE_MOWER_CAMPAIGN_AUTH_PROBE=0` to leave every local adapter capability-only for a doctor run.

This overlays only `campaign_adapter_argv` and `campaign_adapter_timeout_seconds` onto the matching lane's built-in `provider_config`; every other key in `code-mower.yml` is ignored for this purpose, so it cannot widen the general config contract. A missing `code-mower.yml`, a missing `lanes` key, or a lane with no matching entry is treated as no override. Configure each lane under **exactly one** spelling: `muse` and `muse_cli` (or `claude` and `claude_code`) name one lane with one adapter command, so a config that declares more than one of them is *ambiguous*, not merely redundant -- the entries may carry two different `campaign_adapter_argv` values and there is no correct choice between two adapter commands. That is rejected with the bounded `adapter_configuration_invalid` error code and a detail naming the conflicting spellings (drawn only from the built-in alias table, so the message never echoes config text), rather than silently running whichever spelling happened to be looked up first. An existing repo config that fails to load, or is structurally malformed (a non-mapping `lanes`, lane, or `provider_config` entry), degrades to the safe `adapter_configuration_invalid` error code and an actionable status message, never a crash or a leaked template/traceback. `campaign_adapter_argv` must be a YAML list of non-empty scalar tokens (no shell strings; the adapter still runs with `shell=False`).

Code Mower maintainers shipping a built-in adapter for a provider instead add `campaign_adapter_argv` (and optionally `campaign_adapter_timeout_seconds`) directly to the provider's `provider_config` in `src/code_mower/provider_registry.py`, using the same placeholders and contract described above. Most adopters do not need to touch this file.

Hosted / SaaS providers (`hosted_bridge`/`saas_event` driver: Devin, Cursor Cloud Agent) dispatch via a GitHub issue comment instead of a local adapter. Every hosted dispatch follows a closed five-check profile -- auth (dispatch token), installation (the provider App answers campaign comments, acknowledged via the lane's `campaign_transport_ready_env`), trigger (the provider's real builder trigger text), trusted responder (`bot_authors` allowlist), and result return (bounded response wait). Doctor reports each check independently, and a dry-run with an unverified App transport or result-return path reports the provider `unavailable` (`hosted_transport_unverified`) with the exact remediation instead of previewing it queued. Only an explicit `--apply` dispatches; silence past the deadline becomes `hosted_response_timeout` evidence, and only an explicit `--retry-provider` may dispatch again -- paid work is never retried automatically:

- Configure authentication tokens (`DEVIN_AUDIT_LABEL_TOKEN`, `CURSOR_BUGBOT_AUDIT_LABEL_TOKEN`, `GITHUB_TOKEN`) and supply `--issue <number>` plus `--repo-slug <OWNER/REPO>` (at creation, or on the `resume`/`dispatch` that first needs it). Without both, the hosted provider stays `unavailable` and no comment is posted. The dry-run preview judges this prerequisite exactly as `--apply` does: a hosted provider with valid credentials but no issue number previews as `unavailable` with the bounded `missing_issue_number` error code and a next action naming `--issue`, rather than as queued and ready to dispatch.
- The dispatch comment states exactly what will be accepted. For an upgrade campaign it carries the campaign's exact `starting_version` in both the machine-readable `code_mower.releaseCampaignDispatch.v1` marker and the human-facing instructions, so a remote runner never has to guess which starting version to qualify from. Cold-install (and `unknown`) campaigns have no starting version and omit the field. An upgrade campaign whose stored `starting_version` is missing is never dispatched at all: the provider stays `unavailable` with the bounded `campaign_identity_incomplete` error code and no comment is posted.
- The provider's reply comment must embed a `CODE_MOWER_ADOPTION_RESULT` marker as a single-line HTML comment on a line of its own (`<!-- CODE_MOWER_ADOPTION_RESULT: {...} -->`), wrapping schema `code_mower.releaseCampaignResult.v1` with `campaign_id`, `provider`, `release_tag`, and `idempotency_key` matching the original dispatch, plus a validated `adoption_result`. A bare or unbound result is ignored so a stale or unrelated comment can never be replayed as evidence. The embedded `adoption_result`'s own `qualification_context` and `starting_version` must also match the campaign's exactly, independent of the wrapper's idempotency key -- a cold-install result cannot complete an upgrade campaign, and an upgrade result from one starting version cannot complete a same-tag upgrade campaign from a different starting version. The marker line is matched end to end and its JSON is captured through the object's own final brace, so a literal `-->` inside a permitted string value cannot truncate an otherwise valid trusted result; a marker whose JSON is genuinely malformed is still ignored (fail-closed), never guessed at.
- These identity fields are visible in the public dispatch comment, so binding alone does not prove authorship -- anyone could reply with a matching marker. A result marker is only ever accepted from a GitHub comment author present in the lane's `provider_config.bot_authors` list (and, if configured, the comma-separated login list in the environment variable named by `provider_config.bot_authors_env`). A lane with no trusted authors configured trusts nobody; an untrusted or spoofed author's comment is ignored and the provider keeps running.

#### Cursor Cloud Agent Setup

Cursor Cloud Agent is a hosted async builder using the `hosted_bridge` driver.

**Prerequisites:**
- GitHub App authorization for Cursor in your repository
- `CURSOR_CLOUD_AGENT_AUDIT_LABEL_TOKEN` (or `GITHUB_TOKEN` as fallback) for applying audit labels
- `GITHUB_TOKEN` for posting dispatch comments
- After verifying that the installed App answers campaign issue comments, set
  `CODE_MOWER_CURSOR_CLOUD_AGENT_CAMPAIGN_TRANSPORT_READY=1`. Without it, doctor and
  Board report the transport as unverified, but an explicit `--apply` may still
  dispatch it under the response deadline below. Token presence alone proves
  comment permission, not that the App supports this transport.

**Trusted authors (default):**
- `cursor[bot]`
- `cursor`

**Environment override:**
Set `CURSOR_CLOUD_AGENT_BOT_AUTHORS` to a comma-separated list of additional trusted GitHub logins. This extends (does not replace) the default trusted authors, allowing self-hosted or alternative Cursor integrations to be trusted.

**Trigger comments (real builder contract):**
- `@cursor`

The `@cursor` mention is the dispatch-lanes builder trigger (see
`docs/lanes/cursor.md`). Never use BugBot/reviewer trigger text (`bugbot run`,
`@cursor review`) for release qualification.

**Role and capability:**
- `role: builder`
- `capability: work_order_execution`
- Can execute work orders and package-install campaigns

**Response timeout:**
- 3600 seconds (1 hour)

**Campaign dispatch:**
The campaign posts a GitHub issue comment with schema `code_mower.releaseCampaignDispatch.v1` containing:
- `campaign_id`
- `provider`: `cursor_cloud_agent`
- `release_tag`
- `package_spec`
- `qualification_context` (cold_install/upgrade/unknown)
- `starting_version` (for upgrade campaigns only)
- `idempotency_key`

Cursor Cloud Agent replies with a comment containing `<!-- CODE_MOWER_ADOPTION_RESULT: {...} -->` wrapping schema `code_mower.releaseCampaignResult.v1`. The embedded `adoption_result` must match the campaign's provider, release tag, package identity, qualification context, and (for upgrades) starting version.

**Trusted authors:**
- `cursor[bot]`, `cursor` (registry defaults)
- Override via `CURSOR_CLOUD_AGENT_BOT_AUTHORS` environment variable

**Example dispatch:**
```bash
code-mower release campaign \
  --release-tag v1.0.0 \
  --package-spec code-mower==1.0.0 \
  --providers cursor_cloud_agent \
  --issue 123 \
  --repo-slug owner/repo \
  --apply
```

**Aliases:**
- `cursor` → `cursor_cloud_agent`
- `cursor_cloud_agent` → `cursor_cloud_agent`

**Note:** Cursor Cloud Agent is an opt-in paid provider (`enabled_by_default: false`, `trigger_policy: manual`, `spend_policy: paid`). It must be explicitly requested via `--providers cursor_cloud_agent` or `--providers cursor`.

#### Cursor BugBot / Grok Bot Setup

Cursor BugBot and Grok Bot are **review-only** hosted providers using the `saas_event` driver. They provide code review and cannot execute package-install qualification campaigns.

**Prerequisites:**
- `CURSOR_BUGBOT_AUDIT_LABEL_TOKEN` (or `GITHUB_TOKEN` as fallback) for applying audit labels
- `GITHUB_TOKEN` for posting review trigger comments

**Trigger comments** (for review, not campaigns):
- `bugbot run`
- `@cursor review`

**Trusted authors:**
- `cursor[bot]`, `cursor` (registry defaults)
- Override via `CURSOR_BUGBOT_BOT_AUTHORS` environment variable

**Aliases:**
- `cursor_bugbot` → `cursor_bugbot`
- `cursor_grok_bot` → `cursor_bugbot`
- `grok_bot` → `cursor_bugbot`

**Note:** Cursor BugBot is an opt-in paid review provider. It is review-only with `role: reviewer` and `capability: code_review`. It cannot participate in release qualification campaigns.

#### Historical Note

Before v1.0.8, `cursor_bugbot` was used for both builder and review capabilities. As of v1.0.8, `cursor_cloud_agent` is the builder identity and `cursor_bugbot` is review-only.

#### Devin Setup

Devin is a hosted paid provider using the `hosted_bridge` driver.

**Prerequisites:**
- Devin GitHub App authorization in your repository
- `DEVIN_AUDIT_LABEL_TOKEN` (or `GITHUB_TOKEN` as fallback) for applying audit labels
- `GITHUB_TOKEN` for posting dispatch comments
- After verifying that the installed App answers campaign issue comments, set
  `CODE_MOWER_DEVIN_CAMPAIGN_TRANSPORT_READY=1`. Without it, doctor and the
  campaign dry-run report the transport as unverified (`unavailable` with the
  exact remediation) before any dispatch; an explicit `--apply` may still
  dispatch it under the response deadline below. Token presence alone does not
  prove that the App supports this transport.

Successful hosted dispatches carry a one-hour response deadline. A result from
a trusted author still wins when it arrives before the deadline. Silence after
the deadline becomes `hosted_response_timeout` with manual-result or explicit
`--retry-provider` guidance; Code Mower never redispatches paid work by itself.
Campaigns created before this field existed receive a fresh full window on
their first poll after upgrade, so the migration never retroactively times out
an older paid run.

**Trusted authors (default):**
- `devin-ai-integration[bot]`
- `devin-ai-integration`

**Environment override:**
Set `DEVIN_BOT_AUTHORS` to a comma-separated list of additional trusted GitHub logins. This extends (does not replace) the default trusted authors, allowing self-hosted or alternative Devin integrations to be trusted.

**Trigger comments:**
- `@devin run`
- `devin run`

**Example dispatch:**
```bash
code-mower release campaign \
  --release-tag v1.0.0 \
  --package-spec code-mower==1.0.0 \
  --providers devin \
  --issue 123 \
  --repo-slug owner/repo \
  --apply
```

**Note:** Devin is an opt-in paid provider (`enabled_by_default: false`, `trigger_policy: manual`, `spend_policy: paid`). It must be explicitly requested via `--providers devin` and is not included in the default provider set.

### Per-Release Operation

The normal release qualification sequence consists of three steps: **dispatch**, **watch**, and **upload**.

#### 1. Dispatch

Create and dispatch qualification tasks to automated local adapters and hosted providers:

```bash
code-mower release campaign dispatch --release-tag v1.0.0 --apply --repo-slug OWNER/REPO --issue 123
```

Any provider without an automated adapter, credentials, or a bound remote result stays `unavailable`/manual. Record its verified result explicitly once qualification has occurred:

```bash
code-mower release campaign --release-tag v1.0.0 \
  --record-result path/to/adoption-result.json \
  --record-provider codex
```

The recorded file is validated against the same closed schema as automated results.

#### 2. Watch

Follow campaign progression through a bounded watch operation:

```bash
code-mower release campaign watch --release-tag v1.0.0
```

Configure custom polling intervals or bounded timeouts as needed, or request machine-readable JSON:

```bash
code-mower release campaign watch --release-tag v1.0.0 --interval 5 --timeout 300
code-mower release campaign watch --release-tag v1.0.0 --json
```

- **Bounded polling:** Polls the stored campaign at a configurable positive interval (`--interval`, default `10.0`s) and bounded duration (`--timeout`, default `600.0`s). Both must be positive numbers. `--interval` is valid only for `watch`. `--timeout` is valid only for `watch` (bounded duration) and `upload` (request timeout); supplying either option to an action where it would be silently ignored is rejected with a bounded error without mutating campaign state.
- **Distinct stopping states:** Stops distinctly with appropriate exit codes for:
  - `complete` (exit 0): All providers have reached passing qualification states.
  - `owner_action` (exit 1): Running work has finished but campaign requires owner intervention (e.g. missing credentials, unconfigured adapter, or pending manual recording).
  - `blocked` (exit 1): One or more providers recorded failing qualification results.
  - `timeout` (exit 1): The bounded timeout expired while providers were still in-flight.
  - `interrupt` (exit 130): Interrupted via signal or SIGINT/Ctrl+C.
  - `remote_unavailable` (exit 1): GitHub API or remote service encountered an outage during polling.
  - `invalid_campaign` (exit 1): Campaign is missing, malformed, ambiguous, or invalid arguments were supplied.
- **Clean output & no-change suppression:** Text mode prints the initial campaign state, real state transitions (ticks without state changes are suppressed), and a final result line with actionable retry guidance.
- **Stable JSON contract:** `--json` mode emits one stable metadata-only final summary adhering to `code_mower.releaseCampaignWatch.v1` containing campaign metadata, duration metrics, transition records, provider statuses, and retry guidance.
- **Safe polling:** Watching never executes adapters, posts dispatches, retries providers, or uploads, but it may persist newly observed remote provider transitions through the existing poll path. It takes the campaign directory lock only during each individual poll read/update rather than holding it continuously, preserving Board readability and directory accessibility.

#### 3. Upload

Once providers have completed, publish the campaign's evidence to Code Mower
Cloud. Preview first; the preview and the upload use the same event set:

```bash
code-mower release campaign upload --release-tag v1.0.0 --json
code-mower release campaign upload --release-tag v1.0.0 --yes --json
```

- **Preview by default:** without `--yes` nothing leaves the machine. The
  preview reports the exact events, event ids, and counts the `--yes` run will
  upload, so it can be inspected first.
- **Terminal evidence only:** every provider in a terminal state (`complete`
  or `blocked`) with a stored result is revalidated against the closed
  adoption-result schema and rebound to this campaign's provider, release tag,
  package identity, qualification context, and starting version before it is
  converted to one metadata-only `adoption_run` event -- passing and failing
  terminal results alike, so `fail` (and schema-valid `incomplete`) evidence
  reaches `adoption_run` analytics instead of being skipped. The terminal
  state must also agree with the bound result outcome (`complete` carries
  only passing outcomes; `blocked` only failing/incomplete ones, exactly as
  the state machine stores them): a contradictory pair can only come from
  corrupted or hand-edited storage and is *rejected* with the bounded
  `adoption_result_state_mismatch` reason, never published. Queued, running,
  and unavailable providers are counted as skipped, never fabricated, as is a
  `blocked` provider whose adapter never produced a result. A terminal provider
  whose stored result is present but missing or no longer valid is *rejected*:
  the upload stops with a bounded error naming the provider and a safe reason
  code, rather than publishing a partial event set or repairing the result.
- **Idempotent:** each event id is derived from the result's own content, so
  repeating an upload republishes the same events instead of duplicating them.
  A failed post can simply be re-run.
- **Metadata-only:** report text, source, diffs, prompts, transcripts, issue
  bodies, raw output, auth output, local paths, and secrets are never uploaded,
  and missing model, token, and cost data stays unavailable rather than
  zero-filled. See the [Cloud Data Contract](cloud-data-contract.md).
- **Read-only:** upload never dispatches, retries, records, or advances a
  campaign, so it cannot be combined with `--apply`, `--resume`,
  `--retry-provider`, `--record-result`, or `--status`. It takes no campaign
  directory lock and writes no campaign state.

Token, endpoint, and identity resolution are the shared cloud ones:
`--token-env`, `--token-file`, `--token-dir`, `--install-id`, `--team-id`,
`--endpoint`, and `--timeout` (bounded request timeout, default 20.0s; valid only for `upload` and `watch`) behave as they do for `code-mower cloud upload`,
including `code-mower cloud setup` profiles. Uploading one result file at a time
with `code-mower cloud dogfood --event adoption_run=path/to/result.json`
still works and produces the same event ids.
