# Board Service Lifecycle

A Board started from a shell dies with that shell. `code-mower board service`
manages a **persistent** local Board instead: a supervised service that survives
the invoking shell, can be restarted idempotently, and refuses to take over a
binding it cannot prove is its own.

macOS is the implemented provider (launchd). Every other platform reports
`unsupported_platform` rather than calling a transient process a service.

## Commands

```bash
# Review the exact definition before anything is applied.
code-mower board service render --repo OWNER/REPO --repo-path /path/to/checkout \
  --port 5332 --output /tmp/board-5332.plist

code-mower board service install --repo OWNER/REPO --repo-path /path/to/checkout --port 5332
code-mower board service status --json
code-mower board service restart --repo OWNER/REPO --repo-path /path/to/checkout --port 5332
code-mower board service remove --repo OWNER/REPO --yes
```

The service label is `ai.codemower.board.<port>`: the port is the exclusive
resource, so it is what a label owns. Three managed Boards over two independent
repositories are three labels.

## The serving gate

`status`, `install` and `restart` all validate the same binding, and every check
must pass before an operation reports success:

| Check | What it proves |
| --- | --- |
| `service.label` | the installed definition carries the expected label |
| `service.definition` | the installed definition matches the rendered one (no stale arguments) |
| `service.keepalive` | the service is supervised, not one-shot |
| `service.loaded` | the supervisor holds the job and it is running |
| `process.arguments` | the running process argument list equals the definition exactly |
| `process.repo_path` | the running process is bound to the exact private repository path |
| `process.supervisor` | the process is supervised, so it survives the invoking shell |
| `binding.port` | the service process, and not something else, holds the port |
| `binding.repo` | the port serves the expected repository slug |
| `binding.installed_version` | the served installed version matches this installation |
| `binding.serving_version` | the serving version is not stale against the installed one |

## Delayed health

A service is not accepted the moment launchd returns. The apply settles for
`--settle-seconds`, then refreshes the full gate every `--refresh-seconds` until
`--timeout-seconds` elapses. A Board that answers once and then exits, or that
comes back with stale arguments, fails the gate instead of passing on a single
early probe. The payload reports `delayed_health` with its state, the settle and
refresh windows, and how many refreshes it took.

## Fail-closed refusals

None of these change any local state:

| Status | Cause |
| --- | --- |
| `stale_arguments` | a different definition is installed for this port; rerun with `--replace` |
| `ownership_mismatch` | another managed label owns the port, or the path is another repository's checkout |
| `external_supervisor` | the port is held by a process a different supervisor owns |
| `port_conflict` | the port is held by an unrelated local process |
| `ambiguous_repository` | the selector matches more than one managed service |
| `rollback_failed` | an apply failed *and* the previous definition could not be restored |
| `delayed_health_failed` | the service applied but its binding never validated |

`--replace` is the only way to take over an existing definition for a port, and
the replacement is atomic: the definition file is swapped with `os.replace`, and
a failed bootstrap restores exactly the previous definition or reports
`rollback_failed`.

## Stop, transient and managed

`board stop` now accepts `--repo OWNER/REPO` alongside `--port` and `--pid`.
Selectors are not exclusive: every selector supplied must agree on one binding.

- Two Boards for one repository: `ambiguous_selector`, nothing stopped.
- `--repo` and `--port` naming different bindings: `selector_mismatch`, nothing
  stopped.
- A port served by a keepalive-managed service: `managed_service`, nothing
  stopped, with a pointer to `board service restart` or `board service remove`.
  Signaling it would report success while the supervisor immediately reclaimed
  the port.

`board list` marks each Board `managed` with its service label, or transient.

## Local paths stay local

The exact repository path is the thing a binding is validated against, so the
comparison happens locally and only its verdict leaves. Status, doctor, Board
payloads and every rendered summary carry `[local path hidden]` and a pass/fail,
never the path. `--show-local-paths` is a local-operator escape hatch; its output
does not belong in a public comment or in release evidence. `render --output
FILE` writes the exact definition for local review without printing it.

The `digest` field names a definition without revealing its contents, so
"the same definition is still installed" can be stated in public evidence.
