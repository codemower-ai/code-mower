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
| `process.arguments` | the running job's argument list, as launchd reports it, equals the definition exactly |
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
| `stale_arguments` | a different definition is installed for this port, or one that cannot be read; rerun with `--replace` |
| `ownership_mismatch` | another managed label owns the port, or `--repo-path` was not proven to be a checkout of `--repo` |
| `external_supervisor` | the port is held by a process a different supervisor owns |
| `port_conflict` | the port is held by an unrelated local process |
| `ambiguous_repository` | the selector matches more than one managed service |
| `rollback_failed` | an apply failed *and* the previous definition could not be restored, or the definition it wrote could not be taken back off the host |

`--host` is validated against the same loopback rule `board serve` enforces,
before any lifecycle mutation: a service naming a non-loopback host describes a
Board that can never come up, and installing it would leave launchd restarting a
failing process forever.

A definition that is not UTF-8 text -- a binary plist, or one that has been
corrupted -- is reported as unreadable rather than parsed. Rollback restores a
previous definition by writing its text back, so a definition with no text has no
recoverable backup; `--replace` is the only takeover, and it discards those
contents knowingly.

The argument list comes from launchd, which prints one argument per line, rather
than from `ps -o command=`, which renders an argv as a single unquoted line. A
checkout path containing a space cannot be split back out of that line, so a
healthy service would fail the gate. If launchd reports no argument list at all,
`process.arguments` fails rather than being assumed to match.

Port ownership is proved against the pid launchd is supervising, not against a
definition that happens to name the port: a stopped or crashed managed job leaves
its definition installed, and whatever takes the port it vacated is not ours.
Every listener on the port has to clear that bar. `board list` and the `board
stop` keepalive guard apply the same rule: a listener is managed only when its
pid is the one launchd supervises for that label, so a transient Board that took
a stopped service's port is labelled transient and can still be stopped.

`remove` will not delete a definition while launchd still holds its job. Managed
services are discovered by scanning definition files, so deleting one whose job
survived would strand a running, self-restarting service where `status`, `remove`
and the `board stop` keepalive guard could no longer see it. That case reports
`remove_incomplete` and leaves the definition in place.
| `delayed_health_failed` | the service applied but its binding never validated |

`--replace` is the only way to take over an existing definition for a port, and
the replacement is atomic: the definition file is swapped with `os.replace`, and
a failed bootstrap restores exactly the previous definition or reports
`rollback_failed`. A write that fails outright leaves the previous definition on
disk untouched, so recovery there is to load it again rather than to restore it;
either way the payload's `rollback` field says whether the previous service came
back.

An *unreadable* existing definition -- a malformed plist, or one that cannot be
opened -- refuses on the same terms. It is the one case where nothing can be
compared, which makes it the last case that should be read as consent: taking it
over overwrites contents that could not be read and therefore could not be
rolled back, so it needs the same explicit `--replace`.

Port occupancy is decided from the *unfiltered* local listener inventory, not
from the Board-shaped one. A Node server on 5332, or an ordinary Python process
on a nondefault port, is not a Board but holds the port exactly as firmly; a
narrower inventory would call the port free and let an apply mutate local state
straight into a conflict.

### Logs

The definition sends both output streams to `<repo>/.code-mower/board/logs`.
launchd will not start a job whose `StandardOutPath` cannot be opened, so an
apply creates both parents first -- and creates them *before* booting out the
service it is replacing, so a filesystem failure is reported as `apply_failed`
without having stopped a Board that was working.

### Restart and the load state

`restart` on an unchanged definition restarts the job in place with `launchctl
kickstart -k`. If the definition is valid but its job is *not* loaded -- after a
logout, or a manual `launchctl bootout` -- there is no job to kickstart, so the
existing definition is bootstrapped instead. Either way the delayed-health gate
has to pass before the restart is called a restart.

### Removal

`remove` reports `removed` only when the definition is actually gone from
`LaunchAgents` and the port was released. A definition that could not be deleted
would start the service again at the next login, so that reports
`remove_incomplete` with `definition_present: true` rather than success.

### Proving the checkout

`install` and `restart` both establish ownership before anything is applied, by
reading the origin slug of `--repo-path` locally and comparing it to `--repo`.
Both ways of failing refuse, and the payload's `ownership` field says which:
`mismatch` for an origin naming a different repository, `unverified` for an
origin that cannot be read at all. An unreadable origin is not consent, because
a path whose repository identity cannot be proven is exactly what let a
keepalive job reclaim port 5332 from an older checkout. A Board therefore has to
be served from a checkout with a readable `remote.origin.url` for that
repository. `--replace` takes over a *definition*, not ownership: the origin
guard runs first and refuses either way.

Ownership is compared on one canonical spelling. `build_spec` resolves
`--repo-path`, so the rendered argv, the working directory, and every later
binding comparison all use the resolved path. A live process reporting a
symlinked spelling of the same directory (on macOS `/var/...` for
`/private/var/...`) still matches.

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
