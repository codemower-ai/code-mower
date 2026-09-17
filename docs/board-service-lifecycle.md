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
| `binding.port` | the service process, and nothing else, holds the port -- every listener on it is that process, the same bar an apply enforces |
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
| `listener_inventory_unavailable` | neither `lsof` nor `ss` could be run, so port occupancy is unknown; an unchecked port is never treated as a free one |
| `ambiguous_repository` | the selector matches more than one managed service |
| `unload_failed` | the service being replaced could not be unloaded and launchd will not confirm the job absent; its definition was left exactly as it was |
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

That lookup goes through the pid *first*, and without reference to any port. An
installed definition can name a port that is not the one launchd is currently
serving -- the plist was edited, or the job was bootstrapped from an earlier
version of it -- and a service found only under its on-disk port would be missing
for the listener it is actually supervising, so `board stop --yes` would signal a
process launchd restarts. The definition's port remains the fallback for the one
case with no pid to match: a service whose supervision launchd would not confirm.
The refusal names the port being served and the installed port when they differ,
since `board service remove --port` selects by the latter.

A supervisor that could not be *asked* is a third answer, and it is kept as one.
When `launchctl print` times out or fails for a reason launchd does not
characterise as a missing job, the load state is `unknown` rather than absent:
`board stop` refuses the port (`managed_service` with `supervision: unknown`)
because a stop could not be proven to release it rather than be undone within
moments, and `board list` names the service with its supervision marked
unconfirmed instead of asserting it. Only a positively confirmed-absent job
makes a listener on that port stoppable.

An owner and a repository are named by different rules. `--repo` validates them
separately, so `owner/.github` -- a real repository name, and one Board serves --
is accepted by `render`, `install`, `restart` and `board stop --repo`. The one
repository component refused is one with no alphanumeric character in it, since
`.` and `..` are path traversal rather than repositories.

`remove` will not delete a definition while launchd still holds its job. Managed
services are discovered by scanning definition files, so deleting one whose job
survived would strand a running, self-restarting service where `status`, `remove`
and the `board stop` keepalive guard could no longer see it. That case reports
`remove_incomplete` and leaves the definition in place.
| `delayed_health_failed` | the service applied but its binding never validated |

`--replace` is the only way to take over an existing definition for a port, and
the replacement is atomic: the definition file is swapped with `os.replace`, and
a failed bootstrap restores exactly the previous definition or reports
`rollback_failed`. The swap is also the point at which the original contents
stop existing, so it happens only once the old job is established as unloaded --
a `bootout` that succeeded, or the same positively-confirmed absence `remove`
requires. Writing over a job launchd still holds would fail the bootstrap anyway
(launchd will not accept a label its domain already holds) and the rollback
would then preserve the replacement rather than an original that is by then
gone, so that case refuses as `unload_failed` and changes nothing. A write that fails outright leaves the previous definition on
disk untouched, so recovery there is to load it again rather than to restore it;
either way the payload's `rollback` field says whether the previous service came
back.

Rolling back a *first* install means leaving nothing behind, and that is decided
on the same terms as `remove`: the load state is read before anything is
deleted, and a job launchd still holds -- or one it will not confirm absent --
keeps its definition rather than being deleted out of the inventory. A bootstrap
that times out after launchd has already registered the job is exactly that
case; the rollback reports failure and leaves the service manageable.

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

### The program the service runs

The `code-mower` console script is preferred, because it carries its own
interpreter and package location and so needs nothing from the environment. A
source checkout without that script installed falls back to `python -m
code_mower.cli`, which does need something: the generated launchd environment
keeps only `PATH` and the service label, and the working directory is the
*served repository*, so a child started that way has no way to reach
`code_mower.cli` and the keepalive job would fail and respawn forever. That
fallback therefore names its module search path in the definition, as
`PYTHONPATH`, resolved from the package itself rather than inherited from
whatever the installing shell happened to have. If the package cannot be located
on a canonical path, the request is refused before anything is applied. A
console-script definition carries no `PYTHONPATH` at all.

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

The definition on disk matching the rendered one is not the same fact as launchd
*running* it. `kickstart -k` re-execs the argument list launchd registered when
the job was bootstrapped; it never rereads the plist. A job registered from an
earlier version of a definition therefore comes back on exactly the arguments
that failed the gate the last time, and the shortcut would repeat that on every
restart. So the registered argument list is compared against the definition
first: when they disagree, `restart` refuses as `stale_arguments` and names
`--replace`, and `restart --replace` reloads the definition through the same
bootout-and-bootstrap replacement path -- with the same rollback guarantees --
rather than kickstarting the stale job. A job launchd reports no argument list
for is not drift: that is unknown, and the gate fails it on `process.arguments`
without a replacement being inferred from silence.

### Removal

`remove` reports `removed` only when the definition is actually gone from
`LaunchAgents` and the port was released. A definition that could not be deleted
would start the service again at the next login, so that reports
`remove_incomplete` with `definition_present: true` rather than success.

Deleting a definition is also how a service stops being discoverable at all, so
it needs the job to be *positively* absent. `launchctl print` failing is not the
same fact as launchd not holding the job: only `EX_NOTFOUND`, or the message
launchd prints for a missing job, is absence. Any other failure -- a timeout, a
launchd that could not be reached -- is unknown, and an unknown job is treated as
still loaded, so removal keeps the definition and reports `remove_incomplete`
instead of stranding a keepalive service with nothing left to manage it by.

"The port was released" is a separate claim from "the definition is gone", and it
rests on the local listener inventory. When neither `lsof` nor `ss` can be run,
that inventory was never taken -- so removal reports the deleted definition
honestly and still returns `remove_incomplete`, because an operator who read
`removed` as a free port would start a replacement into whatever is still there.

### Definitions that do not describe one service

A definition is selected by its filename, and launchd registers the job under the
`Label` inside it. When those disagree, neither one describes the whole service,
so it is reported as unreadable *under the filename label* -- the alternative is
aiming a bootout and a delete at a different installed Board while the definition
actually selected stays exactly where it is. A `ProgramArguments` value that is
not a list of scalars is unreadable for the same reason: an integer would end
enumeration in a traceback, and a string would iterate into one argument per
character and read as a plausible argv. Either way `--replace` remains the only
takeover.

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
- A port whose managed service launchd would not answer about:
  `managed_service` with `supervision: unknown`, nothing stopped. The stop could
  not be proven to release the port rather than be undone.

`board list` marks each Board `managed` with its service label, or transient,
and says when that service's supervision is unconfirmed.

A `launchctl` that cannot be probed at all is one of those unconfirmed cases,
not an empty inventory. On macOS the installed definitions are enumerated even
when the capability probe fails -- they are still in `LaunchAgents` and the jobs
they describe may still be running -- and every one of them carries
`supervision: unknown`, so `board stop --yes` refuses rather than signaling a
listener launchd may reclaim. A platform with no managed-service implementation
is the different answer: there is nothing installed to enumerate, so a Board
found there is transient and stoppable.

## Local paths stay local

The exact repository path is the thing a binding is validated against, so the
comparison happens locally and only its verdict leaves. Status, doctor, Board
payloads and every rendered summary carry `[local path hidden]` and a pass/fail,
never the path. `--show-local-paths` is a local-operator escape hatch; its output
does not belong in a public comment or in release evidence. `render --output
FILE` writes the exact definition for local review without printing it.

The `digest` field names a definition without revealing its contents, so
"the same definition is still installed" can be stated in public evidence.
