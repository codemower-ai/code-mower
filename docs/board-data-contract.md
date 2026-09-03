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

`code_mower.boardOwnerQueue.v1` is the derived local owner queue embedded in the
board's `/api/status` response. It summarizes PRs that need operator attention.

`code_mower.boardAgentAdapters.v1` is the local adapter-card payload embedded in
the board's `/api/status` response. It reads opt-in metadata files from
`.code-mower/board/agents/*.json` by default.

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
  installed package.
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
  includes the configured interval and a safe status such as `recorded`,
  `skipped`, or `error`.

The browser UI fetches `/api/status` from a loopback-only HTTP server. It does
not mutate GitHub and does not upload payloads. Plain `board serve` does not
mutate local repository state. `board serve --record-events` is the explicit
local-only write mode for filling board history while the browser view is open.
When the default port is already in use, `board serve` falls forward to a nearby
free loopback port and prints the selected URL. An explicit `--port` stays
strict so scripts and bookmarks fail clearly instead of silently moving. The
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

## Board Admin Commands

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
