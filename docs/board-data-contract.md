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
`code-mower board record --repo OWNER/REPO`.

`code_mower.boardEventStore.v1` is the read response emitted by
`code-mower board events` and the board's `/api/events` endpoint.

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
- `board.mode`: currently `local_read_only`.
- `board.refresh_seconds`: browser refresh interval.
- `board.local_paths`: `redacted` by default, or `shown` when
  `--show-local-paths` is explicitly requested.

The browser UI fetches `/api/status` from a loopback-only HTTP server. It does
not mutate GitHub or local repository state and does not upload payloads.

## Local Event Store

`code-mower board record --repo OWNER/REPO` appends one redacted status snapshot
to `.code-mower/board/events.jsonl` under the repository checkout unless
`--store-path` points somewhere else.

The default retention policy keeps 14 days and at most 500 events. Retention is
applied only by explicit write commands such as `board record`; `board serve`,
`board events`, and `/api/events` are read-only.

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
board.

Persisted snapshots redact local cwd paths by default even if a debug view chose
to show them. Store paths are redacted in JSON output unless an operator
explicitly requests `--show-store-path`.

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
