# Board Data Contract

The Code Mower Board is a local, read-only visibility surface. It shows the
same metadata snapshot printed by `code-mower lanes status --repo OWNER/REPO`
in a localhost browser view.

The board/status payload is local-only and is not uploaded by default. It is a
different contract from CodeMower.com cloud uploads.

## Schemas

`code_mower.laneStatus.v1` is the status snapshot produced by
`code-mower lanes status --repo OWNER/REPO`.
Repeated runs for the same check context and provider or workflow are collapsed
to the newest timestamped result in this current-state snapshot. Superseded
results remain available in prior local history events, but do not drive the
current next action or owner queue.

Each readable open PR includes a `lineage` posture. When no repository policy
was loaded, `status: optional` and `reason: lineage_policy_not_configured`
preserve the independently readable labels, checks, gate state, and PR next
action; `lineage.next_action` tells the operator to pass
`--config code-mower.yml` if they want lineage evaluated. When a configured
policy cannot read or validate the lineage metadata, `status: unavailable`
and `reason: lineage_unreadable` direct the operator to restore readable
metadata and rerun status. Neither posture is presented as verified lineage.

`code_mower.board.v1` is the local board wrapper added by
`code-mower board serve --repo OWNER/REPO`. It adds board display metadata and
embeds the lane-status snapshot unchanged.
The `board.version` block includes `serving_version`, `installed_version`, and
`restart_recommended` so operators can tell when a long-running Board server
should be restarted after a package upgrade.

`code_mower.boardEvent.v1` is the local history event emitted by
`code-mower board record --repo OWNER/REPO` and by
`code-mower board serve --repo OWNER/REPO --record-events`.

`code_mower.boardEventStore.v1` is the read response emitted by
`code-mower board events` and the board's `/api/events` endpoint.

`code_mower.boardRecord.v1` is the write acknowledgement emitted by
`code-mower board record --json`.

`code_mower.boardTimelines.v1` is the derived local timeline payload embedded in
the board's `/api/status` response. It summarizes local board events and
reviewer-spend rows for display only.

`code_mower.productivityReport.v1` is emitted by
`code-mower productivity report --repo OWNER/REPO`. It derives a local
effectiveness snapshot from Board history, reviewer-spend rows, and optional
metadata-only aggregate `productivity_summary` event files.

`code_mower.boardProductivity.v1` is the compact Board form embedded in the
board's `/api/status` response as `productivity`.

`code_mower.boardOwnerQueue.v1` is the derived local owner queue embedded in the
board's `/api/status` response. It summarizes PRs that need operator attention.

`code_mower.boardAgentAdapters.v1` is the local adapter-card payload embedded in
the board's `/api/status` response. It reads opt-in metadata files from
`.code-mower/board/agents/*.json` by default.

`code_mower.boardObservations.v1` is the local observation block embedded in the
board's `/api/status` response as `observations`. It carries validated
`code_mower.boardObservation.v1` records read from
`.code-mower/board/observations/*.json` by default, plus bounded diagnostics for
records the observation contract rejected and the file-level coverage of that
bounded read. A maintained local execution adapter can also supply one immutable
snapshot to the pure local producer during a refresh. Board validates and
consumes the returned record in memory through the same frozen contract; it
never writes an observation.

`code_mower.supervisedPilot.v1` is the local supervised-pilot payload embedded
as `supervised_pilot` in the board's `/api/status` response when
`code-mower.yml` is present. It is derived from the same controller policy
engine used by `code-mower controller run`.

`code_mower.cloudBoardSnapshot.v1` is the summarized cloud mirror event
dimension schema emitted only by the explicit
`code-mower cloud board-snapshot --repo-slug OWNER/REPO` command.

All board schemas are metadata-only. They must not contain source code, raw diffs,
transcripts, issue body text, raw stdout/stderr, auth output, browser history,
local secret values, or secrets.

## Local HTTP Endpoints

The loopback Board server exposes read-only JSON endpoints for the browser UI
and local diagnostics:

- GET `/api/status` returns `code_mower.board.v1`. Its `board.version` block
  includes `serving_version`, `installed_version`, and `restart_recommended`.
  When `restart_recommended` is true, stop and restart
  `code-mower board serve --repo OWNER/REPO` so the browser uses the newly
  installed package. The response also embeds `productivity` as
  `code_mower.boardProductivity.v1` and `observations` as
  `code_mower.boardObservations.v1`. The GitHub/local snapshot behind this
  response is served from a thread-safe stale-while-refresh cache bounded by
  `board.refresh_seconds`: a cold request returns a metadata-only
  warming payload immediately and starts one background refresh, a
  warm/stale request returns the latest completed snapshot immediately while
  at most one refresh runs, and cache health is exposed as `board.cache`
  (`state`, `generation`, `age_seconds`, `refresh_in_progress`,
  `retry_in_seconds`, and a summarized `last_error`). `board.cache.generation`
  is a monotonic count of completed snapshots: it is `0` while the cache is
  cold, increments by one after each refresh that completes successfully, and
  is left unchanged by a refresh that fails or a refresh thread that fails to
  start. It is a plain counter and identifies *which* completed snapshot the
  response carries, independently of whether that snapshot is still fresh. A refresh that fails — including a refresh thread
  that fails to start — arms a bounded retry backoff: the cache keeps answering
  immediately from its cold/stale metadata but starts nothing and reports
  `refresh_in_progress` false with `retry_in_seconds` set to the seconds left
  in the window, so a persistent GitHub or local failure cannot trigger a fresh
  expensive recomputation on every request. The first request after the window
  expires starts exactly one retry; the window doubles per consecutive failure
  from 5s up to 60s and is cleared by the first success (which also clears
  `last_error`). `retry_in_seconds` is null whenever no backoff is pending.
  Cold-start behavior is unchanged: the very first request still starts a
  refresh immediately. The Board browser UI paces itself off this metadata:
  every response schedules exactly one next `/api/status` load through a single
  timer, and there is no separate fixed interval running alongside it.
  - Whenever a completed snapshot is already on its way — `board.cache.state`
    is `cold` or `stale` with `board.cache.refresh_in_progress` true — the next
    load is ~750ms out (capped at 20 consecutive attempts), so the snapshot
    appears promptly instead of waiting a full `board.refresh_seconds`.
  - A `fresh` response schedules against its own remaining TTL, computed from
    the numeric `board.cache.ttl_seconds` and `age_seconds`, floored at 250ms
    and capped at the configured interval. Cache age starts when a background
    refresh completes, not when the page loaded, so a fixed browser interval
    drifted out of phase with the TTL: a tick could land just under the TTL,
    re-render the same snapshot, and only pick up the next one an interval
    later, updating roughly every two intervals.
  - Everything else uses the normal `board.refresh_seconds` interval: the
    fast-poll cap is reached, `cold` or `stale` with no refresh in flight (the
    refresh thread failed to start, or the retry backoff is pending), cache
    metadata that is absent or not a usable number, or a request that failed
    outright.

  Only a response that is no longer awaiting a refresh resets the fast-poll
  attempt budget; once the cap is reached the budget stays exhausted while the
  same refresh is still pending, so a refresh that never completes cannot
  restart the burst. This adds no extra GitHub fan-out: the fast poll only
  re-reads the cached endpoint and the server-side cache still allows one
  in-flight refresh.
- GET `/api/identity` returns `code_mower.boardIdentity.v1`, a lightweight
  repo/version/restart payload used by `code-mower board list`.
- GET `/api/events` returns `code_mower.boardEventStore.v1` from local
  `.code-mower/board/events.jsonl` history.

## `code_mower.laneStatus.v1`

Top-level fields:

- `schema`: always `code_mower.laneStatus.v1`.
- `repo`: the requested `OWNER/REPO` slug.
- `generated_at`: UTC timestamp for the snapshot.
- `remote`: best-effort GitHub state.
- `local_boards`: best-effort local Code Mower Board listener hints.
- `local_processes`: best-effort local lane process hints.
- `next_action`: concise operator action such as `fix BLOCKED audit`,
  `waiting for checks`, `ready for merge or auto-merge`,
  `remote unavailable; fix GitHub access`, or `no active lanes`. `no active
  lanes` is emitted only when GitHub PR/workflow state was available.

`remote.pull_requests[]` includes metadata useful to an operator:

- `number`, `title`, `url`, `branch`, `author`, `updated_at`, `is_draft`,
  `merge_state`, and `head_sha`.
- `labels`: grouped label names for `builder`, `dispatched`, `needs`, `done`,
  and `blocked` families.
- `checks`: check names and states, without raw logs.
- `stale`: whether gate/check evidence is older than the requested threshold.
- `next_action`: the PR-specific next action.
- `next_detail`: optional short operator guidance for stale audit/gate waits,
  such as checking the audit runner/dispatcher and requeueing a lane.
- `gate_rerun_command`: a paste-safe command only when rerunning the gate is the
  useful next step.

`remote.workflow_runs[]` includes recent Code Mower workflow metadata:

- `id`, `workflow`, `title`, `status`, `conclusion`, `event`, `branch`,
  `created_at`, `updated_at`, and `url`.

`remote.gate_health` includes:

- `available`: whether the local check could inspect gate state.
- `status`: summary status.
- `message`: concise human-readable summary.
- `alerts[]`: metadata-only stale or missing-gate alerts.

Local board and process sections are best-effort and non-fatal. Local cwd paths
are redacted by default as `[local path hidden]`; `--show-local-paths` may
include them for same-machine debugging only.

## `code_mower.board.v1`

The board wrapper adds:

- `board.schema`: always `code_mower.board.v1`.
- `board.mode`: `local_read_only` by default, or `local_recording` when
  `--record-events` is explicitly requested.
- `board.refresh_seconds`: browser refresh interval.
- `board.local_paths`: `redacted` by default, or `shown` when
  `--show-local-paths` is explicitly requested.
- `board.recording`: live local-history recording metadata. Recording is
  disabled by default. When `--record-events` is explicitly requested, this
  includes the configured interval and a safe status such as `pending`,
  `recorded`, `skipped`, or `error`. Recording identity is
  `board.cache.generation`, not cache freshness:
  - `pending` while no snapshot has completed yet (`generation` is `0`), with
    the message `waiting for first completed status snapshot`.
  - `recorded` the first time a request observes a completed generation that
    has not been recorded and the record interval is due. A browser polling
    slower than `board.refresh_seconds` can first see a generation only after
    it has already aged out, so a `stale` response records it too; gating on
    `state == "fresh"` silently dropped those generations from local history.
  - `skipped` with `snapshot already recorded` for a generation that was
    already written, including while a newer refresh is in flight. The
    recording lock makes this hold across concurrent requests, so a generation
    is written at most once.
  - `skipped` with `record interval not reached` when a new generation exists
    but the interval is not due. The generation stays eligible, so whichever
    generation is current once the interval elapses is recorded then.
  - `error` with `could not update local board event store` when the write
    fails. The failure consumes the interval and the generation exactly like a
    successful write, so a failing store is not retried on every request.

The browser UI fetches `/api/status` from a loopback-only HTTP server. It does
not mutate GitHub and does not upload payloads. Plain `board serve` does not
mutate local repository state. `board serve --record-events` is the explicit
local-only write mode for filling board history while the browser view is open.
When the default port is already in use, `board serve` falls forward to a nearby
free loopback port and prints the selected URL. An explicit `--port` stays
strict so scripts and bookmarks fail clearly instead of silently moving. The
conflict message points operators at `code-mower board list` and
`code-mower board stop --port PORT --yes` before choosing another port. The
printed URL is local to that machine or VM unless the operator creates a tunnel.

## `code_mower.supervisedPilot.v1`

When `code-mower.yml` is available, the Board adds a `supervised_pilot` block to
`/api/status`. The block is local-only and summarizes the controller's current
read-only decision:

- `schema`: always `code_mower.supervisedPilot.v1`.
- `enabled`: whether the Board could load and validate `code-mower.yml`.
- `cycle_state`: compact UI state such as `idle`, `dispatch`, `waiting`,
  `blocked`, `owner_action`, or `ready`.
- `controller_mode`: currently `dry_run` for Board display.
- `decision`: selected controller decision with PR or issue number, URL, branch,
  author login, short SHA prefix, gate status, reviewer outcomes, stop
  condition, owner action kind, and next-action text when present.
- `queue`: active lane counts, coarse queue metrics, and ready-issue errors.
- `active_prs`: the same open PR metadata already exposed through
  `remote.pull_requests[]`, narrowed for the supervised display.
- `active_issues`: ready issue references from safe labels only. This includes
  issue number, URL, author login, updated time, builder lane, assignment and
  dispatch booleans, owner-action boolean, and label names. It does not include
  issue titles or body text.

If the config is missing or invalid, `enabled` is false and `message` tells the
operator to add or validate `code-mower.yml`. If GitHub is unavailable but the
config is valid, the controller reports a safe owner-action state instead of
raising.

## Local Event Store

`code-mower board record --repo OWNER/REPO` appends one redacted status snapshot
to `.code-mower/board/events.jsonl` under the repository checkout unless
`--store-path` points somewhere else. `code-mower board serve --repo OWNER/REPO
--record-events` appends the same event shape while the board polls, throttled
to at most one stored snapshot every 60 seconds unless
`--record-interval-seconds` is set.

The default retention policy keeps 14 days and at most 500 events. Retention is
applied only by explicit write commands such as `board record` and
`board serve --record-events`; plain `board serve`, `board events`, and
`/api/events` are read-only.

Each stored `code_mower.boardEvent.v1` event includes:

- `type`: currently `status_snapshot`.
- `created_at`: UTC timestamp for the stored event.
- `repo`: the repository slug.
- `snapshot_schema`: the embedded status schema.
- `board_schema`: the embedded board schema.
- `summary`: counts and next-action metadata for fast timeline rendering.
- `snapshot`: the redacted board/status payload.

`code_mower.boardEventStore.v1` read responses include store availability,
recent events, total valid event count, malformed-line count, and a message when
no store exists yet. Malformed JSONL lines are skipped instead of failing the
board. Event store errors use safe generic messages and do not expose local
paths.

`code_mower.boardRecord.v1` write acknowledgements include `status`, the redacted
store path, the stored event, retained/pruned counts, and malformed-line count.

Persisted snapshots redact local cwd paths by default even if a debug view chose
to show them. Store paths are redacted in JSON output unless an operator
explicitly requests `--show-store-path`.

## Timelines

The Board embeds `code_mower.boardTimelines.v1` in `/api/status`. The timeline
payload is derived locally and is not written back into board event snapshots, so
history files do not recursively grow as the browser refreshes.

`timelines.verdicts.entries[]` is derived from local board event snapshots. Each
entry includes:

- `created_at`: event snapshot timestamp.
- `lane`: reviewer lane inferred from done or blocked audit labels.
- `pr_number`: pull request number.
- `head_sha_prefix`: first 12 characters of the PR head SHA.
- `verdict`: `PASS` for done labels or `BLOCKED` for blocked labels.
- `url`: HTTP(S) PR URL when present.

`timelines.spend` is derived from `.code-mower/reviewer-spend.json` unless
`--spend-path` points somewhere else. It includes:

- `available`: whether the spend file exists and was readable.
- `groups[]`: run counts, total and average wall seconds, total cost, and total
  tokens grouped by lane and verdict.
- `recent_runs[]`: recent metadata rows with lane, PR number, SHA prefix, model,
  wall seconds, cost, token count, and verdict.
- `skipped_rows`: malformed spend rows skipped while rendering.
- `filtered_rows`: spend rows for other repositories skipped while rendering.
- `message`: safe status text when no spend file exists or the file cannot be
  read.

Spend paths are redacted in JSON output. Malformed spend files and local read
errors use generic safe messages without embedding local paths.

## Productivity

`code-mower productivity report --repo OWNER/REPO` emits
`code_mower.productivityReport.v1`. The Board embeds the display subset as
`code_mower.boardProductivity.v1` in `/api/status`.

The report uses only metadata already visible to Code Mower:

- local Board history from `.code-mower/board/events.jsonl`;
- reviewer spend rows from `.code-mower/reviewer-spend.json`; and
- optional aggregate `productivity_summary` cloud-event files passed with
  `--cloud-event PATH`.

Top-level report fields include:

- `schema`, `repo`, `generated_at`, and `status`;
- `source`: redacted availability/counts for Board events, reviewer spend,
  optional cloud productivity events, and current remote availability;
- `window.local_history`: start, end, and duration for the local Board history
  used in the report;
- `current`: current open PR, active lane, blocked PR, stale PR, owner-action,
  gate-alert, and `remote_available` state. `source`, `observed_at`, and
  `historical` distinguish live GitHub state from an explicitly labeled
  historical Board fallback. Pass `--offline` to skip live collection and use
  that labeled fallback deliberately;
- `metrics`: contract-aligned productivity names such as
  `cycle_time_seconds`, `active_time_seconds`, `wait_time_seconds`,
  `reviewer_run_count`, `audit_pass_count`, `audit_blocked_count`,
  `reviewer_catch_count`, `blocking_bug_count`, `blocked_finding_count`,
  `fix_round_count`, `owner_intervention_count`, `merged_pr_count`,
  `cost_usd`, and `total_tokens`;
- `quality`: reviewer PASS/BLOCKED, catch, blocker, and fix-round counts;
- `spend`: reviewer run count, total reviewer wall seconds, cost, token totals,
  and per-lane groups;
- `providers`: `code_mower.providerScorecards.v1` rows grouped by provider and
  role with run counts, pass/block rates, adjudication metrics when reported,
  cost/token availability, infra-failure counts, and advisory promotion caveats
  pointing to `docs/lane-promotion-policy.md`;
- `evidence`: command success separated from evidence readiness. Code Mower
  is local-first and cloud is optional: `ready` is true when useful local
  Board history is present, even without reviewer spend or cloud events.
  Reviewer spend and cloud events are optional completeness enhancements
  that improve coverage but never block readiness. Per-source `board_history`,
  `reviewer_spend`, and `cloud_events` flags stay explicit, `coverage`
  distinguishes `empty`, `partial`, and `complete`, `missing` names the
  absent sources, and `detail` gives the Board recording and event-store
  next steps. Empty or partial evidence stays a successful command with
  `status` and `next_action` unchanged; and
- `next_action`: a concise operator action suitable for an epic status comment.

Missing metrics are encoded as JSON `null` and mean unknown, not zero. A missing
Board history or reviewer spend file is not a failure; the report stays local
and gives the next setup action. The report and Board subset redact store paths
and do not include source code, raw diffs, transcripts, issue body text, raw
stdout/stderr, auth output, browser history, local secret values, or secrets.
When multiple `productivity_summary` event files are supplied, the local report
uses latest-window time metrics only; only additive count, token, and cost
metrics from one headline aggregation subject are eligible for cross-event
totals. The headline subject priority is `repo`, then `release`, `issue`, then
`pr`. Provider-, builder-, reviewer-, and lane-scoped events feed provider
scorecards so dashboards do not double-count the same window.

## Owner Queue

The Board embeds `code_mower.boardOwnerQueue.v1` in `/api/status`. The owner
queue is derived from the current lane-status PR metadata and is not uploaded or
written to GitHub.

`owner_queue.entries[]` includes one item per attention reason. A PR may appear
more than once when it has multiple independent reasons. Each item includes:

- `kind`: `needs-owner`, `blocked-audit`, `stale-gate`, `failing-check`,
  `rebase-needed`, or `draft`.
- `priority`: lower numbers sort first.
- `pr_number`, `title`, `branch`, `author`, and `updated_at`.
- `head_sha_prefix`: first 12 characters of the PR head SHA.
- `url`: HTTP(S) PR URL when present.
- `next_action`: concise operator action.
- `labels` or `checks` only for the relevant reason, without raw logs.

When GitHub is unavailable, the owner queue returns `available: false`, an empty
entry list, and a generic message. Existing local event and spend timelines can
still render from local files in the same Board response.

## Presentation Rules

The browser view derives the rules below from the payload described above. They
add no fields to any schema; they only bound what the page is allowed to assert.

- **Hierarchy.** The current-work summary (`Work Now`), the owner queue, and
  lane work are rendered before aggregate productivity and release history.
- **One work item per PR.** `owner_queue.entries[]` carries one entry per
  attention reason. The page groups entries by `pr_number` into a single work
  item with grouped reasons, one primary responsible role, and one next action,
  so several reasons for one PR cannot inflate the owner count.
- **Role.** `blocked-audit`, `failing-check`, `rebase-needed` and `draft` are
  builder work; `stale-gate` is orchestrator work. Owner attention requires an
  explicit permission, budget, policy, product-decision or owner-request label
  already present in the PR's own label groups; a reason that claims owner
  attention without such evidence is shown as orchestrator triage.
- **Missing measurements.** A time, cost, quality or productivity value that is
  not a real JSON number renders as `not recorded`. Absent values are never
  coerced to zero, and an unknown or unavailable state is never green or `pass`.
- **Gate verdict.** Only the `code-mower/gate` commit status is the verdict.
  The publisher is an allowlist of the canonical names that publish it — the
  `Code Mower gate` workflow and its `publish Code Mower gate status` job —
  compared case- and whitespace-insensitively. Those are labelled as
  publishers, so a successful publisher run cannot make a pending, blocked or
  unrecorded verdict look passing. An unrelated check whose name merely
  contains `gate`, such as `security-gate`, is an ordinary check.
- **Observation age.** Snapshots replayed from local history, snapshots older
  than ten minutes, snapshots served from a `board.cache.state` other than
  `fresh`, and snapshots taken while GitHub was unavailable are shown as
  `last observed <age> ago` and may not claim that work is running now. The
  age shown is the older of the observation time and `board.cache.age_seconds`.
  When neither records a parseable time the page reports `observation time not
  recorded` neutrally, with no `live` claim and no synthetic age.
- **Campaign liveness.** A campaign's `elapsed_seconds` is recorded provider
  work time, not age, and is labelled that way. A response deadline is read
  only while a provider card is still awaiting a response (`queued` or
  `running`): a terminal `complete` or `blocked` card and a never-dispatched
  `unavailable` card can retain the deadline they were given, and that stale
  timestamp neither marks them overdue nor changes their state styling. A
  `running` campaign is shown as `last reported running` unless some card that
  is awaiting a response has an unexpired `response_deadline_at`.
- **Local data.** When GitHub data is fresh but local session inputs (agent
  adapter cards, orchestrator lease, reviewer verdict history, reviewer spend
  rows) are absent, GitHub information stays useful and the page names the
  local data that is unavailable instead of rendering it as zero.

## Board Views

The browser view is organized as four tabs over one payload. Nothing below
changes the payload; the views only bound what the page asserts and where it
says it.

Persistent chrome — repository, serving and installed version, snapshot time,
the one next action, and observation freshness — stays on screen in every view
and at every width. The tabs are a real `tablist` of `tab` buttons controlling
real `tabpanel` regions; the unselected panels carry `hidden`, so they leave the
accessibility tree instead of being painted away.

The header labels the running package as `Serving version: VERSION`; this is
the primary version reading, while the Health view retains installed-version
and restart detail. An empty Recent Code Mower Workflows section renders
`none`, matching the terminal status surface.

- **Now** — the work rows, the selected work item's evidence, the participant
  summary, and the existing owner queue, lane work, supervised pilot and open
  PR sections.
- **Timeline** — meaningful recent changes, local Board history, the reviewer
  verdict timeline, and recent Code Mower workflow runs.
- **Releases** — release campaigns, productivity, and spend. Completed campaign
  history lives here rather than in front of current work.
- **Health** — observation sources and their freshness, Board version and
  restart state, snapshot cache state, GitHub availability, gate alerts, the
  orchestrator lease, agent cards, and local Board and lane processes.

### Work rows and selected-work detail

Each work row shows the safe `reference` the observation records, the stage,
the assignments recorded for it, the last meaningful update, the recorded next
action, and the responsible role. Selecting a row exposes the six independent
evidence readings — builder runs, review, CI, gate, merge, and human policy —
each naming the source it came from and how fresh that source is, so no reading
can stand in for another. The gate publisher is shown beside the
`code-mower/gate` verdict and is labelled as publisher execution only.

Rows are ordered by urgency, which is a separate question from which recorded
truth headlines a row. A merged item headlines as merged because that describes
it best, but it is the least urgent thing on the board, so row order runs
blocked work first, then work waiting on a named person, then work whose
evidence cannot be trusted, then work recorded as in flight, and terminal work
— merged, and an idle session — last by explicit placement. A headline that is
not ranked sorts after everything ranked and before the terminal band. The
reference and then the opaque identity break ties, so an unchanged snapshot
never reshuffles the list. Because the first row is what an operator who has
chosen nothing is shown, the Board opens on work that still needs someone
rather than on work that is finished.

Every reason the frozen observation contract accepts has its own display state
and its own explicit place in that ranking, so no supported reason falls
through to "state not recorded" or sorts as something nobody ranked. The
blocked band carries `source_unavailable`, `changes_requested`,
`update_required`, `ci_failed`, `gate_failed`, `provider_failed`,
`provider_suspended` and `cancelled`; the waiting-on-a-person band carries
`approval_required`, `user_input_required`, `ready_to_merge`,
`human_review_required` and `review_requested`; `stale_observation`,
`review_stale` and `identity_unlinked` are evidence that cannot be trusted; and
`review_in_progress`, `ci_pending` and `gate_pending` are recorded as in
flight. A suspended session is reported as suspended rather than as a failure:
the contract records suspension as the `suspended` lifecycle state and allows
it only alongside the `failed` phase, so the failure state is read from runs
that are not suspended and neither claim is ever made on the other's evidence.

One run has one lifecycle-aware state, and every display that names, colours,
groups, counts, or summarizes that run reads it from one place — the row
headline and its state cues, the assignments line, the selected-work evidence
panel, and the participant summary all agree by construction. A lifecycle state
overrides the recorded phase only where it means something the phase cannot
say, which in this contract is `suspended` alone: a suspended run reads as
suspended everywhere, a cancelled run stays distinct from a failed one, an
actual lifecycle failure still reads as failed, and complete, implementation
complete, and running keep reading as themselves. The recorded phase survives
only as raw contract evidence in the change signature, where it is compared
alongside the recorded lifecycle state so a run that moves between the two is
still detected as a change.

Selection is kept by opaque work identity — session, worktree, and work id —
not by row position, so a refresh that reorders, adds, or drops rows leaves the
operator's choice where it was. One identity is one row: where the directory
holds several observations of the same work item, the newest by recorded
`created_at` is rendered — with the last meaningful update and then the row
signature as deterministic tiebreaks — so which file the directory listed first
cannot change what is shown. Change tracking compares the same deduplicated
set, so there is exactly one row id and one detail region per identity however
many files describe it. Several `unlinked` observations in one scope still
consolidate into one row, and that row is recomputed from all of the evidence
retained for it: the worst freshness and coverage any retained source reported,
the age of the oldest retained observation, the newest recorded event or
observation as the last meaningful update, and one entry per run however many
files observed it, kept as the worst-attested of those observations. The
participant summary is built from this same deduplicated set, so a run that has
moved on is counted once, under the lifecycle-aware state the newest
observation records, and never again under the state it has left.

Deduplication only ever compares like with like, so it settles which
observation of one identity is current and says nothing about two identities
that disagree. A session-level `no_work` snapshot and a work-specific
observation of the same session and worktree are different identities, so both
survive it — and left there the Board would state, of one session at once, that
it was observed complete with nothing to do and that it is running work. A
single reconciliation step runs immediately after deduplication and before any
view reads a row, and keeps the newer of the two readings by the same trusted
recorded order — `created_at`, then the last meaningful update — never by file
or directory order. An idle snapshot followed by work observations is stale and
is dropped, and every work item observed after it survives, however many there
are. Work observations followed by an idle snapshot are the session having
since gone quiet: the idle snapshot is the truthful current state and those
work rows are dropped rather than restated as current work, which holds for
terminal work too — an item observed as merged before its session reported
itself idle is not current work either. Nothing is invented to stand in for a
dropped row; what is already recorded is that the row is no longer recorded,
which change tracking reports in the Timeline on the poll that drops it. Two
observations recording exactly the same instants are resolved by specificity,
the work-specific one winning, because claiming "nothing to do in this session"
over a work item observed at the same instant is the contradiction the step
exists to remove. Records in different sessions or different worktrees are
never compared — one session holds several worktrees and one worktree is reused
by session after session — and a record carrying neither half of a session
identity, which is every `unlinked` observation, is never correlated with one
that does and keeps the unlinked consolidation semantics above. The Health view
still reads every record on disk on purpose: a source behind a superseded
observation was really contacted, and its connection is inspected there on its
own terms rather than as a claim about work.

There is exactly one detail region. It is
rendered inside the selected row, so at phone widths it follows the row it
belongs to, and at desktop widths CSS places that same region in a second
column of the row's own grid. It stays in normal flow at both widths, so the
row — and therefore the list and the section — is always at least as tall as
the detail it is showing, and a list of one or two rows can never leave the
detail hanging over the sections below it. Rows are buttons carrying
`aria-expanded` and `aria-controls`; Up, Down,
Home and End move the selection, tabs wrap with the arrow keys, and every
interactive control has a visible focus ring.

A poll replaces the tab strip and the row list, so every control that can hold
the keyboard carries an identity derived from what it acts on rather than from
where it was rendered: the tabs from the view, the rows from the work identity,
and the selected row's actions from that identity and the action's own name.
The element id is an injective encoding of the opaque identity — a letter,
digit or hyphen stands for itself and every other code unit becomes `_<hex>_` —
so identities that differ only in punctuation, such as unlinked work in
`owner/re.po` and in `owner/re-po`, keep distinct ids, distinct
`aria-labelledby` targets and distinct focus lookups. A
refresh therefore returns the keyboard to the same control — restoring without
scrolling, because a refresh must not move the view. When a control is no
longer offered, focus moves only to the row that control named as its owner,
and if that row is gone too the Board leaves focus where the browser put it
rather than handing the keyboard to an unrelated control. Activating a detail
action that opens another view moves focus to that view's tab, because the
control that was activated is inside the panel the switch has just hidden.

The detail region scrolls independently of the row list at desktop widths, and
a poll replaces it along with everything else, so the offset an operator
scrolled to is preserved across the refresh and restored afterwards. It is kept
against the same opaque work identity the selection is kept against: a refresh
that changed the evidence of the work item being read returns to the same
position, a different work item opens at the top of its own evidence rather
than inheriting someone else's position, and a selection that stops being
rendered has nothing to restore onto. Restoring is clamped to what the
replacement can actually scroll, so a refresh that shortens the evidence lands
at the end of what is now there. Focus and the offset are carried across one
refresh together, the offset restored last, so neither undoes the other.

Meaningful changes are announced once through a polite live region and listed
in Timeline. A change is meaningful when a recorded fact differs: stage,
reasons, route, pull request identity, evidence state, measurements, run phase
or basis, or a source's freshness, coverage or event time. `created_at`,
`checked_at`, `observed_at` and `heartbeat_at` are excluded because they advance
on every successful poll, so a poll that repeats the same observation announces
nothing and adds no Timeline entry.

Primary actions stay read-only: open a recorded PR link, inspect a connection
in Health, and view recent changes. A pull request number observed locally is
never turned into a remote address the payload has not recorded, and a link is
offered only when the record names this Board's own repository. A custom
observations directory can hold a record another repository produced, where the
same pull request number means a different pull request; such a record is still
shown for what it is, named as belonging to that other repository, without a
link. There is no
merge, requeue, force-lease, cancel, retry, restart, cloud-schema or
Slack-specific control, no form, and no non-GET request.

### Observation presentation rules

- **Liveness is read, never inferred.** A run is only described in the phase its
  own record states, alongside the freshness of the source behind it. A record
  whose sources are not all `fresh`, or that is more than ten minutes old, is
  reported as a last observation and may not claim anything is running now.
- **Unavailable is not zero, and idle is not unknown.** A `no_work` observation
  is shown as idle *with complete coverage* and names the source kinds that were
  observed fresh and complete. An `unlinked` observation claims no stage and no
  route, because the contract records none for it.
- **No invented totals.** An unavailable measurement renders `not recorded`. A
  partial measurement renders the value with the evidence it was counted from
  (`120.0s from 2 of 5 recorded`). Nothing is extrapolated to a whole, and no
  ratio is turned into a percentage or an ETA.
- **Unknown is neutral.** Evidence states are a closed vocabulary and each one
  is classified explicitly; `unknown`, `not_started`, `absent`, `unverifiable`,
  `none` and `unassigned` are neutral, and anything unrecognised is neutral too.
  Colour always accompanies a text label and a text cue.
- **An empty directory is not an empty queue.** With no observation recorded,
  the work list says so rather than reporting no work, and the GitHub-derived
  queues below it still render.
- **A bounded read is not the whole record set.** The Board reads at most 32
  observation files per refresh. When more candidates exist, the work list
  warns above its rows that the snapshot is incomplete, the Now header and the
  chrome carry that warning into every view, and no row may claim complete
  coverage, an idle session, or that there is no work. An idle `no_work`
  snapshot reads as *idle in the files read* rather than *idle with complete
  coverage*, because an unread file could record work in exactly that scope.
- **A lost candidate is not a candidate that said nothing.** The same applies
  to a selected file the Board could not read and to one the record contract
  rejected: the Board cannot know whether it held the work record that
  contradicts a `no_work` record beside it. Any loss makes file coverage
  partial, warns on every surface, and reads as *idle in the records read*
  rather than *idle with complete coverage*. Reconciliation obeys the same
  rule: retiring an observed work row asserts that its session has since gone
  quiet, so an idle snapshot read under incomplete coverage never retires work
  — both readings stay on the page.

## Board Observations

The integrated head-bound qualification and operator checklist are in
[Local Board qualification](board-qualification.md). Local policy observations
are explicit and exactly bound: review PASS alone does not create a human
requirement. Stale CI/gate observations retain their original head and source
timestamps, and sampled CI cannot establish merge readiness. Collapsed work
rows show each source's freshness/coverage; full as well as partial measurements
name their coverage denominator. When no blocking route is recorded, the view
offers an observation/review follow-up without adding a workflow mutation.

`code_mower.boardObservations.v1` is the local observation block embedded in the
board's `/api/status` response as `observations`. It is a **consumer** of the
frozen `code_mower.boardObservation.v1` contract: the Board reads file records,
decodes each through `board_observation.decode`, and renders what survives. A
maintained execution path may pass one immutable `LocalObservationInput` to
`status_payload`; Board then calls the pure `observe_local_work` boundary with
the configured repository and checkout root, validates its returned B0 record
again, and appends it in memory. The producer reuses the exact read-only current
session resolver and validates repository, worktree, session, work, run,
role/provider, PR, and head bindings before correlation. A refusal or exception
adds no record and cannot abort the rest of the Board refresh. The Board never
writes an observation, contacts a provider, repairs a record that fails the
contract, or converts lease ownership alone into liveness.

### Remote observation connection

`board_remote_observation.remote_work_input` adapts provider-neutral lifecycle
metadata to that same `LocalObservationInput`; `hosted_work_input` also binds
hosted work-order evidence. The existing session/worktree resolver remains the
single correlation authority. Embedders capture remote facts with
`RemoteSessions.observe`, `DevinWorkOrders.observe`, or `HostedReview.observe`
before passing the immutable input to Board. These explicit read-only methods
are distinct from execution `status`/`collect`: they create no locks or files,
reconcile no uncertain creates, collect no result bodies, and perform only
metadata GETs. No provider credentials are discovered by Board.

An embedding can retain the returned safe `RemoteObservation` (or hosted
`RemoteWorkObservation`) and pass it as `previous` on its next observation,
including after restart. A failed GET retains its original observation time
and closed lifecycle reason while advancing only the check time and marking
the source unavailable. Without a previous observation, the time and lifecycle
are unknown. These methods do not persist a new cache or change the lifecycle
state machine. Durable intent generations and hosted round bindings are checked
before and after external reads; a changed binding refuses correlation instead
of adopting an old result. Retained remote live phases require the matching
historical source timestamp and a stale/unavailable reason. Board presents them
as **last observed**, never as current execution or idle.

Assignment, dispatch, provider running/stage, reported implementation completion,
and independently observed merge state remain separate. Hosted observation
rechecks PR identity and head through GitHub separately from previously verified
implementation evidence; no private completion claim is read. Review/CI/gate
facts require a separate fresh current PR observation. Review evidence also
requires the exact round and full head: prior-round evidence is discarded and
old-head or old-time review evidence is stale. A controller report contributes
assignment intent only; its label-derived PASS and merge recommendation cannot
become review or merge evidence. Raw provider references, questions, answers,
messages, prompts, result bodies, private paths, and uncovered cost detail do
not enter the Board contract. Cloud and Slack schemas are unchanged.

By default the Board reads `*.json` files under
`.code-mower/board/observations/`. Use `--observations-path PATH` for a custom
local directory. A missing directory is reported as "nothing recorded yet",
which is a different statement from "no work". Reading is bounded to 32 files
per refresh, and the observation contract itself bounds each record to
`MAX_BYTES`. Each file is read with a single bounded request of at most
`MAX_BYTES + 1` bytes: a file larger than the cap is rejected on the length of
what was asked for, with the contract's own `invalid_contract` diagnostic, and
its remainder is never loaded or decoded.

A directory holding more than 32 `*.json` files is **not** silently reduced to
whichever 32 happened to be reached. Every candidate is counted, the bounded
subset is chosen deterministically, and the shortfall is reported as file-level
coverage so no consumer can read a truncated snapshot as the whole local record
set. Selection is a total order over *(modification time descending, file name
ascending)*, which is deterministic whatever order the filesystem lists entries
in and prefers the most recently written files, so a current record is not
starved by an alphabetically earlier stale one. The frozen record contract
guarantees nothing about file names or file times, so that preference is a
conservative best effort and never evidence: an overflowing directory is
reported as incomplete however it was selected, and a file whose time cannot be
read simply loses the preference. Emission order stays file-name order, so a
directory inside the cap reads exactly as it did before. Counting the candidate
set never widens the read — at most 32 files are opened, each with one bounded
`MAX_BYTES + 1` request.

Only **regular files** are ever opened. Every `*.json` entry is classified by one
`lstat` that never opens anything, and a selected entry that is not a regular
file — a named pipe, a socket, a directory, a symlink, or an entry whose own
metadata could not be read — is refused unread and counted as `unreadable`.
Symlinks are deliberately not followed, even to a regular file. The reason is
that the file and byte bounds only start applying once the file is open: reading
a named pipe with no writer blocks until a writer arrives, and a refresh that
blocks there never finishes, so the page keeps serving the snapshot before it.
Refusing an entry is not ignoring it — it stays a counted candidate that
produced no record, so the read is `partial` exactly as it is for a file that
raised. A candidate can also change between that classification and the open; a
lost race raises on the open or the read and lands in the same `unreadable`
count.

The block carries:

- `records[]` — validated `code_mower.boardObservation.v1` records. File-backed
  records stay in file-name order; the optional in-memory local producer record
  follows them.
- `produced_records` — `1` when the typed local hook returned an accepted
  in-memory record for this refresh, otherwise `0`. This record is not a file
  and does not change file coverage or any candidate counter.
- `coverage`, `coverage_complete`, `coverage_gaps[]`, `truncated`, `file_cap`,
  `selection` and the candidate counters below — how much of the candidate file
  set those records were built from. `coverage` is a closed vocabulary:
  `complete` when every candidate was read *and* produced an accepted record,
  `partial` when any candidate was lost, and `unavailable` when the directory
  could not be listed at all (where `candidate_files`, `omitted_files` and
  `unaccounted_files` are `null` rather than an invented total).
  `coverage_complete` is the same fact as a boolean and is what every consumer
  gates an absence claim on; `coverage_gaps[]` says which kinds of loss
  occurred, from the fixed vocabulary `directory_unreadable`, `files_omitted`,
  `files_unreadable`, `records_invalid`. This is file coverage and is
  deliberately separate from a record's own source `coverage`: a rejected
  record and an unread file are different facts, and both are reported.
- The candidate counters partition the candidate set exactly, and the
  partitions are the accounting invariants:
  - `candidate_files` = `selected_files` + `omitted_files` — every `*.json`
    candidate was either selected by the bounded read or omitted by the cap.
  - `attempted_files` = `selected_files` — every selected candidate is one the
    read is answerable for, whether it was opened or refused before any open.
  - `attempted_files` = `read_files` + `unreadable_files` — an attempted file
    either yielded its bytes, or raised on open or on read, or was refused for
    not being a regular file.
  - `read_files` = `accepted_records` + `invalid_records` — a file that was
    read either decoded into a record or was rejected by the frozen record
    contract (including for exceeding `MAX_BYTES`).
  - `accepted_records` counts accepted file records only.
  - `len(records)` = `accepted_records` + `produced_records`.
  - `unaccounted_files` = `unreadable_files` + `invalid_records` — every
    selected candidate the records do not account for.
  - `coverage_complete` is true only when `omitted_files` and
    `unaccounted_files` are both zero.
- `rejected` and `warnings[]` — `rejected` is `unaccounted_files`: every
  selected candidate that produced no record, deliberately including unreadable
  ones, because a file that could not be read is no more accounted for than one
  the contract refused. Each has one warning carrying a fixed diagnostic:
  `unreadable_file` for a file that could not be read — including one refused
  for not being a regular file, which is stated as the same fact and never as
  what kind of entry it was — and the contract's own
  vocabulary (`invalid_contract`, `invalid_route`, `identity_mismatch`, and so
  on) for a record it rejected. Those diagnostics carry no errno, no OS message,
  no local path and no byte of file content; the `file` field carries the bare
  candidate name inside the Board's own observations directory and nothing else,
  and the page renders the diagnostics as counts per term without it.
- `path`, redacted as `[local path hidden]`, `path_state`, `path_exists`,
  `available`, and a safe `message`. `path_state` is a closed vocabulary —
  `directory`, `missing`, `not_directory`, `unreadable` — decided by one
  metadata call per refresh that cannot raise, and `path_exists` is the same
  fact as the boolean consumers already read: `true` for `directory` and
  `not_directory`, `false` for `missing`, and `null` for `unreadable`, because
  "it is not there" is a claim a path the Board could not examine cannot
  support. Only `missing` is an absence the Board is entitled to state as
  "nothing recorded yet"; `not_directory` and `unreadable` are losses of
  evidence and degrade the block to `available: false` with `unavailable`
  coverage and the `directory_unreadable` gap, each with its own fixed
  diagnostic (`observation path is not a directory`, `could not check the local
  Board observation path`, and `could not list local Board observations` for a
  directory lost between that check and the enumeration). None of them names a
  path, an errno or an OS message. Those file failures set `available: false`
  only when there is no independently validated in-memory producer record. If
  the producer did return one, `available` remains true and the fixed message
  states that the local observation is available while recorded observation
  files could not be read; file `coverage` remains `unavailable` with the
  `directory_unreadable` gap because the producer cannot repair evidence the
  file reader never saw.

Nothing in this block may raise on the filesystem. It is assembled as one step
of the whole `/api/status` snapshot, so an exception at this boundary would not
produce an unavailable observations block — it would abort the refresh and take
the repository, PR and lane data with it, leaving the page on whatever it served
before. An observation directory under an ancestor the process cannot search is
the ordinary way that happens. Observations are therefore the only thing that
degrades: every other block is built exactly as it would have been.

Observations are not copied into the local Board event store: `code-mower board
record` and `--record-events` persist the snapshot without the `observations`
block, so local history keeps the shape it already had. The cloud board-snapshot
export is an allowlist of summarized fields and is unchanged by this block; no
observation field is uploaded.

## Agent Adapters

The Board embeds `code_mower.boardAgentAdapters.v1` in `/api/status`. Agent
adapters are local-only JSON files that let wrappers for Claude, Codex, Cursor
or Grok Bot, Antigravity, Devin, and reviewers publish safe status cards without
installing hooks or mutating GitHub.

By default, the Board reads `*.json` files under `.code-mower/board/agents/`.
Use `--agent-adapters-path PATH` for a custom local directory. Missing adapter
directories are fine; malformed files produce safe warnings and do not stop the
Board.

Each JSON file may contain one card object, an array of card objects, or an
object with an `agents[]` array. Supported card fields are:

- `provider`, `role`, `status`, `lane`, `label`, `repo`, `branch`, `title`, and
  `next_action`.
- `pr_number`, `issue_number`, and `pid`.
- `head_sha`, stored as `head_sha_prefix`.
- `url`, only when it is HTTP(S).
- `started_at` and `updated_at`.
- `cwd`, redacted by default as `[local path hidden]`; `--show-local-paths` may
  show it for same-machine debugging, while persisted board events still redact
  it.

Unknown fields are ignored. Secret-like values are redacted, and fields commonly
used for source, diffs, transcripts, raw command output, auth output, browser
history, or credentials are not part of the adapter contract.

A card whose `pid` refers to a process that is gone is marked `"stale": true`
and counted in `stale_cards` so list and status views can safely ignore it
instead of treating it as a live agent. `code-mower board stop
--prune-stale-agents --yes` additionally deletes only `*.json` files inside the
agent-adapters directory whose every pid-bearing card is stale; files with live
pids, files without pid cards, and anything outside that directory are never
touched. Pruning needs `--yes` and never signals any process.

## Board Admin Commands

`code-mower board list` emits `code_mower.boardInventory.v1`, a local inventory
of visible Code Mower Board listeners. It reports loopback URL, PID, process
name, parsed repo hint, serving version, installed package version,
`restart_recommended`, health, and next action. Local cwd paths are redacted by
default; `--show-local-paths` is for local debugging only. If the host blocks
listener inspection, the command reports an unavailable inventory instead of
calling GitHub or reading repository content.

`code-mower board stop --port PORT --yes` and `code-mower board stop --pid PID
--yes` emit `code_mower.boardStop.v1`. Stop only sends a local termination
signal after the inventory identifies the target as a high-confidence Code
Mower Board listener, and it re-reads the target command line immediately
before signaling so a recycled pid pointing at an unrelated process is refused
instead of signaled. Medium-confidence default-port listener hints are never
stopped automatically. Without `--yes`, the command exits with
`confirmation_required` and does not signal any process. Exactly one of
`--port` or `--pid` is required when a stop target is requested;
`code-mower board stop --prune-stale-agents --yes` prunes without a selector,
exits with `pruned`, and never signals any process.

`code-mower board doctor --repo OWNER/REPO` emits
`code_mower.boardDoctor.v1`, a local diagnostic summary for Board inputs,
GitHub availability, gate alerts, local history, owner queue, optional agent
cards, and spend parsing. Text and JSON output redact local paths by default and
use safe counts/messages instead of raw command output or raw GitHub auth
errors.

`code-mower board reset --repo OWNER/REPO --yes` emits
`code_mower.boardReset.v1` and deletes only the local Board event-store file.
Without `--yes`, the command exits before touching local files. Reset does not
delete agent adapter cards, spend files, workflow outputs, repository files, or
GitHub state.

## Cloud Boundary

Current board/status JSON and local board event-store data are local-only and
not uploaded by default. CodeMower.com continues to receive only the cloud
contracts documented in
[Cloud Data Contract](cloud-data-contract.md), including
`code_mower.cloudUpload.v1` bundles and `code_mower.benchmarkEvent.v1`
structured events.

The explicit `code-mower cloud board-snapshot --repo-slug OWNER/REPO --json`
command exports one summarized `board_snapshot` event with zero reports. Adding
`--yes` uploads the same metadata-only summary for the CodeMower.com Board
mirror. The cloud snapshot keeps only whitelisted fields such as PR numbers,
branches, authors, label/check groups, workflow status, owner-queue kinds, agent
card provider/role/status, verdict counts, and spend group totals. It omits the
full local Board payload, PR titles, owner note titles, local cwd paths, PIDs,
full head SHAs, gate rerun commands, source, raw diffs, transcripts, issue body
text, raw stdout/stderr, auth output, browser history, local secret values, and
secrets.

Any future dashboard mirror expansion must land as a paired OSS and dashboard
change: update this document, update [Cloud Data Contract](cloud-data-contract.md),
keep the hosted service backward-compatible with v0.6/v0.7 uploads, and
preserve the metadata-only privacy boundary.
