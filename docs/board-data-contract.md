# Board Data Contract

The Code Mower Board is a local, read-only visibility surface. It shows the
same metadata snapshot printed by `code-mower lanes status --repo OWNER/REPO`
in a localhost browser view.

The board/status payload is local-only and is not uploaded by default. It is a
different contract from CodeMower.com cloud uploads.

## Schemas

`code_mower.laneStatus.v1` is the status snapshot produced by
`code-mower lanes status --repo OWNER/REPO`.

`code_mower.board.v1` is the local board wrapper added by
`code-mower board serve --repo OWNER/REPO`. It adds board display metadata and
embeds the lane-status snapshot unchanged.

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

All board schemas are metadata-only. They must not contain source code, raw diffs,
transcripts, issue body text, raw stdout/stderr, auth output, browser history,
local secret values, or secrets.

## `code_mower.laneStatus.v1`

Top-level fields:

- `schema`: always `code_mower.laneStatus.v1`.
- `repo`: the requested `OWNER/REPO` slug.
- `generated_at`: UTC timestamp for the snapshot.
- `remote`: best-effort GitHub state.
- `agenttrail`: legacy local-board detector state. This is not a recommendation
  to run AgentTrail; it remains for compatibility with existing local board
  detection code.
- `local_processes`: best-effort local lane process hints.
- `next_action`: concise operator action such as `fix BLOCKED audit`,
  `waiting for checks`, `ready for merge or auto-merge`, or `no active lanes`.

`remote.pull_requests[]` includes metadata useful to an operator:

- `number`, `title`, `url`, `branch`, `author`, `updated_at`, `is_draft`,
  `merge_state`, and `head_sha`.
- `labels`: grouped label names for `builder`, `dispatched`, `needs`, `done`,
  and `blocked` families.
- `checks`: check names and states, without raw logs.
- `stale`: whether gate/check evidence is older than the requested threshold.
- `next_action`: the PR-specific next action.
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
  includes the configured interval and a safe status such as `recorded`,
  `skipped`, or `error`.

The browser UI fetches `/api/status` from a loopback-only HTTP server. It does
not mutate GitHub and does not upload payloads. Plain `board serve` does not
mutate local repository state. `board serve --record-events` is the explicit
local-only write mode for filling board history while the browser view is open.

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

## Cloud Boundary

Current board/status JSON and local board event-store data are local-only and
not uploaded by default. CodeMower.com continues to receive only the cloud
contracts documented in
[Cloud Data Contract](cloud-data-contract.md), including
`code_mower.cloudUpload.v1` bundles and `code_mower.benchmarkEvent.v1`
structured events.

Any future dashboard mirror of board/status data must land as a paired OSS and
dashboard change: update this document, update
[Cloud Data Contract](cloud-data-contract.md), keep the hosted service
backward-compatible with v0.6/v0.7 uploads, and preserve the metadata-only
privacy boundary.
