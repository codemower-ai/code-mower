# Local repository graph: revision-bound lifecycle

Implements [issue #913](https://github.com/codemower-ai/code-mower/issues/913)
under [epic #902](https://github.com/codemower-ai/code-mower/issues/902), on top
of the adopt decision recorded in [Graphify evaluation](graphify-evaluation.md).

This is the safe lifecycle around an optional local code graph: how one gets
built, what it is allowed to see, where it is kept, and when a consumer must
refuse it. It adds no dependency, no background service, no hook, no watcher,
and no default indexing step. Nothing on a default install path builds, reads,
or requires a graph.

Two modules divide the work:

- `context_graph.py` (from #876) decides whether a delivered packet's
  **citations** are in scope and fresh enough to use.
- `context_graph_lifecycle.py` (this document) decides whether the graph
  **should have existed** — which revision it binds, which bytes produced it,
  where its state lives, and when it fails closed.

## The problem

A graph indexer pointed at a working checkout is unsafe in two directions. It
reads files nobody agreed to index — untracked scratch files, ignored
`.env` files, another worktree reached through a symlink — and it produces an
artifact with no way to tell which revision it describes, so a graph built three
commits ago answers today's question with yesterday's code and looks identical
to a fresh one.

Constraint 1 in the evaluation is the sharp edge: the provider owns no
provenance at all. If Code Mower does not bind the revision, nothing does.

## What a build does

`build_graph()` is the only way a generation is created. In order:

1. **Resolve the revision.** The full commit and tree object names, never an
   abbreviation and never a branch name. The tree is resolved separately
   because it is what a consumer actually compares.
2. **Take the tracked census.** `git ls-tree -r` against the *commit*, not the
   working tree and not the index. Symlinks (`120000`) and submodules
   (`160000`) are skipped and recorded as skipped, because a symlink can name a
   target the build was never shown and a gitlink names a commit in a
   repository it was never authorized to read. Committed private state is
   skipped for a third reason, at any depth and case-folded: the roots are
   `context_graph`'s excluded roots themselves — `.git`, `.graph`, `.graphify`,
   `.code-mower` — bound rather than copied, so the set that refuses a citation
   into private state is the same set that keeps those bytes away from the
   indexer. A tracked `.graphify/` or `.graph/` is somebody's old index, and
   materializing it would let the provider resume from a cache built over
   content this build never saw, and let the adapter collect tracked repository
   bytes as if the provider had just produced them. A tracked `.code-mower/`
   is this tool's own packets and evidence, which the evidence contract refuses
   to let a packet cite and which therefore may not be indexed either. The
   census digest covers mode, blob name, size and path
   for every entry in sorted order. Both halves of the census are bounded as
   they are collected: the file-count budget covers what is materialized, and a
   matching budget covers what is skipped, because a repository of symlinks,
   submodules, or committed private state grows the skipped list without adding
   a single entry to the other one.
3. **Materialize into private state.** Each blob is written into a fresh 0700
   directory as a 0600 file. Untracked and ignored files have no path into the
   graph because they are never written, rather than because something filtered
   them out afterwards.
4. **Run the indexer with a scrubbed environment, inside a network-denying
   sandbox.** The provider process inherits an allowlist — `PATH`, `TMPDIR`,
   `LANG`, `LC_ALL`, `TZ` — and nothing else, with `HOME` and the XDG
   directories pointing into the build's own scratch area. A newly invented
   secret variable is excluded by default because the list names what is kept,
   not what is dropped. The network boundary is separate and is described
   below; the emptied proxy variables are hygiene, not that boundary.
5. **Publish atomically.** The manifest is serialized and read back through the
   same validation every consumer applies before anything is written: a
   manifest this process can write but no reader can load would otherwise
   become `current`, prune the generation that worked, and read back `invalid`
   on the next status — a build reporting success while destroying the only
   usable graph. Then the generation is assembled under a staging name,
   fsynced, renamed into `generations/<id>`, and only then does the `current`
   pointer start naming it. A reader sees the whole previous generation or the
   whole new one.

Publication and pruning happen inside one locked critical section. Two builders
that race are serialized, and neither can delete the generation the other just
published while `current` still names it.

`remove` takes the same lock. A removal running beside a build would otherwise
delete its materialized sources, its output, and the generations directory, and
the builder would then either fail or recreate state that `remove` had already
reported as gone. The lock file lives *beside* the state directory rather than
inside it, so it survives the removal it serializes: a lock inside the deleted
tree would be unlinked mid-removal, and the next builder would create a new
inode and hold a lock nobody else was waiting on. What is left behind is an
empty 0600 file carrying nothing.

Readers take no lock at all, so a refresh can publish and prune between the
moment `graph_status()` reads the `current` pointer and the moment it finishes
validating what that pointer named. A failing verdict is therefore confirmed
against the pointer before it is returned, and a pointer that moved is read
again — otherwise a healthy refresh would surface as `invalid` or `corrupt`. A
verdict about the generation `current` still names is returned as it stands; the
retry is for a moved pointer, not a poll.

## The containment boundary

An environment variable is a request, not a boundary: `NO_PROXY=*` asks a
cooperating client to connect *directly*, and on a host with internet access an
uncooperative provider is unaffected by any of it. A working directory is not a
boundary either: pointing a provider at the materialized copy does not stop it
reading the checkout next door. So the provider is launched behind an argv
prefix that denies it, at the operating-system level, both the network and
every path outside the build's own directories — `sandbox-exec` with a
`(deny default)` profile on macOS, `bwrap` with an empty new root on Linux.

What the child can see is the whole of it: the materialized copy and the
build's redirected `HOME` and `TMPDIR`, writable; the pinned provider's own
install and the system runtime it needs to start, read-only. The operator's
home, their other checkouts, and every ignored `.env` beside them are not
unreadable — they are absent. `unshare --net` used to be a candidate and is
gone: it denies the network and leaves the host filesystem in place, which is
half a boundary.

"The system runtime" is a list of runtime directories, not a list of top-level
ones. It used to say `/usr`, `/etc` and `/Library`, which claims far more than
"what a runtime needs to start": `/usr` carries `/usr/local` — Homebrew's whole
prefix, its `etc` and `var` included — and `/usr/src`, either of which can hold
a checkout; `/etc` carries whatever service credentials a host's packages left
world-readable; and `/Library` carries `Keychains`, `Preferences`,
`Application Support` and the rest of a Mac's machine-wide operator data. Now it
names the loader and C library directories, the system binary directories,
`/usr/share`, Apple's signed `/System` volume, the dyld and time-zone databases,
the two `/Library` paths that hold a runtime rather than operator data
(`/Library/Frameworks` and the command line tools' own bundle), and `/etc` one
entry at a time — the loader's cache and configuration, the time zone, the
account databases, OpenSSL's configuration file. Nothing else. A host whose
runtime needs something outside that list refuses builds, because the probe
cannot start a child under the boundary; it does not get a wider boundary.

The refusals are applied to the **whole readable set**, not only to each root a
build asks for. Every exposure a build derives is checked as it is derived, but
the runtime above is added afterwards and unconditionally, so the set the child
is really confined to was never examined as a set — which is how a runtime path
that contained the checkout could leave the live working tree readable beside
the materialized copy that exists to replace it, with every per-root check
passing. A base interpreter prefix wide enough to contain the runtime is handled
the same way from the other side: `/usr`, which is what an environment created
from the system Python records, is narrowed to the runtime directories inside it
rather than exposed whole, and on a prefix like that one those are already what
every child gets, so the exposure does not grow at all.

"The provider's own install" is a layout this module *proves* rather than one
it infers from depth. It used to expose the executable's parent and
grandparent, on the reasoning that a console script lives in a virtual
environment's `bin`; where that reasoning is wrong it is wrong in the widest
possible direction, because `~/bin/graphify` makes the grandparent the
operator's entire home, `/opt/graphify` makes it `/`, and a provider installed
inside the checkout makes it the live working tree the materialized copy exists
to keep away from the provider. So an install root is now a directory holding a
`pyvenv.cfg` whose script directory holds the executable — a virtual
environment, which is what pinning a provider produces — and the root arrived
at is refused outright if it is the filesystem root, the operator's home, the
checkout being indexed, or an ancestor of either. A provider already inside the
read-only system runtime asks for no extra exposure and gets none. Anything
else is refused with an instruction to pin the provider into its own
environment, rather than exposed as a guess.

A virtual environment is not self-contained: it reaches its base interpreter
and standard library through a link out of its own `bin`, so that base
installation is exposed alongside it. *Which* base is read out of the
environment's own `pyvenv.cfg` — the `base-prefix`, `base-executable`,
`executable` and `home` records that `venv`, `virtualenv` and `uv` write — and
not taken from `sys.base_prefix`, which names the interpreter running Code
Mower. The two are the same installation only when the provider happened to be
pinned with this process's Python; pin it with a `uv`-managed or otherwise
separately installed one, as is entirely ordinary, and the child gets a runtime
it never uses exposed while its own is absent from its filesystem view. That
failure arrives from inside the dynamic loader rather than as anything naming a
path, so a correctly pinned install fails every build for no visible reason.
Each recorded base is held to exactly the refusals the environment root is —
being read out of a file makes a path no narrower than guessing it would — and
an environment that records no base that still exists is refused with an
instruction rather than built against whatever runtime is lying around.

The prefix a particular build ends up with is probed before that build runs,
not just the host's mechanism at startup: the readable set of a real build is
the provider's install rather than the interpreter paths the host probe uses,
and a widened exposure that reopened the boundary would otherwise meet nothing
between the exposure and the provider. The verification probe is handed this
build's exposure *plus* the interpreter, so that the probe child can start at
all; that makes the probed prefix strictly more permissive than the one the
build runs under, and containment observed there is a sound statement about
containment here.

A `(deny default)` profile on macOS has to say one thing that is not about the
build's own exposure at all: Apple's `dyld-support.sb` is imported, because a
modern dynamic linker cannot reach the shared cache without it. The failure
without it is worth naming, since it is not a denied `open` — dyld aborts
inside `CacheFinder` before it owns `stderr`, so the child arrives as a
`SIGABRT` with no output of any kind and the profile reads as "this launcher
cannot start a child" on every macOS host. The import grants no general file
access. The other way to get dyld started, an unfiltered `(allow
file-read-data)`, would dissolve the boundary the profile exists to draw.

No mechanism is trusted on its name, and none is looked up on `PATH`: each
candidate is an absolute path whose file and every ancestor directory must be
owned by root or by this user and unwritable by anyone else, because a launcher
somebody else can replace is a verdict somebody else can forge.

A candidate is accepted only after a probe child launched behind it has been
*observed* failing at both halves: failing to reach a TCP listener this process
is really holding open on loopback, and failing to read a secret file planted
outside its exposure. The network verdict is taken at the listener, not from
the child's errno: a network namespace brings up its own loopback, so a
correctly contained child sees the same `ECONNREFUSED` that an unconfined child
sees from an unused host port. Those two are indistinguishable at the child and
obvious at the listener, which either accepted a connection or did not. A child
that fails one half and not the other is not a boundary; it classifies as
unusable.

The child also prints a digest of a nonce generated for that run, so a launcher
that never started its child cannot pass for a boundary by exiting with the
contained code.

An unsandboxed control child runs first and must reach the listener *and* read
the planted secret. If it cannot — no probe interpreter, loopback unavailable —
then "could not" proves nothing about any candidate, every candidate would
pass, and the probe refuses outright instead. The result is cached for the
process, since it is a property of the host.

A host where no candidate passes gets no build. `subprocess_indexer()` raises
before a single blob is materialized, and `context-graph doctor` reports the
same condition as a failing `context-graph-isolation` check once a provider is
pinned. Running an unconfined provider is not offered as a fallback: an
operator who cannot contain a third-party indexer is better served by knowing
it than by a build that quietly could have reached the network.

A Linux host that restricts unprivileged user namespaces — Ubuntu 24.04 and
GitHub's hosted runners among them — offers no mechanism by default. Installing
bubblewrap (`apt install bubblewrap`), which ships an AppArmor profile
permitting the namespaces it needs, is the least invasive way to give such a
host one, and is what the `graph containment` CI job does before running these
tests for real rather than skipping them. The alternative is to
lift the restriction system-wide
(`sysctl kernel.apparmor_restrict_unprivileged_userns=0`), which is a decision
about the whole machine rather than about this build, and not one this
repository makes on an operator's behalf.

macOS has its own job, `graph containment (macOS)`, because the Linux job
proves the bubblewrap boundary and nothing whatever about the Seatbelt one, and
leaving the profile to be exercised only by whoever happened to run the suite
on a laptop is how it went unexecuted. `sandbox-exec` ships with the OS, so
there is nothing to install and the job is simply the evidence that the profile
runs at all. It does not gate merges yet: a red result there carries the
probe's own account of which candidate failed and what the launcher said on
stderr, which is the diagnosis the equivalent Linux failure was fixed from.

Git itself runs with `GIT_CONFIG_NOSYSTEM`, `GIT_CONFIG_GLOBAL=/dev/null`, and
`GIT_CONFIG_SYSTEM=/dev/null`: an untrusted checkout's local, global, or system
configuration can otherwise install clean/smudge filters and hook paths that run
code during what looks like a read.

Git children are not inside the provider's sandbox — they are children of Code
Mower itself — so the boundary has to reach them separately. Both invocation
paths, the census reader and the blob materializer, share one environment:
`GIT_NO_LAZY_FETCH=1`, and `GIT_ALLOW_PROTOCOL` set but empty, which git reads
as the complete list of permitted transports. `protocol.allow=never` travels on
the command line because that is the only level that outranks the repository's
own `.git/config`, which belongs to the untrusted checkout and is always read.

The same environment carries `GIT_NO_REPLACE_OBJECTS=1`. A `refs/replace` entry
substitutes one object's bytes for another's on every ordinary read, so without
it a census and a materialization could bind content the commit and tree the
manifest records do not contain — and removing the replacement afterwards would
leave `status` still reporting `current`, because staleness is decided by
comparing object names.

That still leaves the repository *shape* that makes a read reach out at all, so
**a build refuses a partial clone outright**. Where `extensions.partialclone` or
a promisor remote is configured, `ls-tree` and `cat-file` can fetch a missing
object from a remote mid-build. There is no bounded way to prove in advance
which objects are present locally, so the build declines the checkout rather
than discovering the gap one blob at a time. Use a full clone.

## What a manifest binds

Every published generation carries, in `manifest.json`:

| Field | Why it is there |
| --- | --- |
| `commit`, `tree` | Full object names. Staleness is decided against these, not against a branch. |
| `provider` | Distribution, exact version, wheel SHA-256, and the extraction options used. |
| `built_at` | ISO 8601 UTC. The provider records no build time of its own. |
| `tracked_files`, `tracked_bytes`, `census_digest` | Exactly which bytes the indexer was shown, re-derivable from the repository. |
| `skipped_paths` | How many tracked entries were deliberately not materialized. |
| `graph_digest`, `graph_bytes` | Detects a truncated or tampered artifact on every read. |
| `completeness` | `complete` or `partial`, from the provider's own admission. |
| `indexed_files` | What the provider claims it processed, bounded by the census. |

`shareable_summary()` is the metadata-only view: revisions, digests, counts and
states. It carries no indexed content, no provider output, and no local path.

## How the provider is actually invoked

`subprocess_indexer()` builds the argv for the interface the adopt decision
evaluated, not a conventional-looking one: `extract` plus the pinned options,
run with its working directory set to the materialized copy. The evaluated
release takes no `--source`/`--output` pair — `extract` reads the directory it
is run in and writes its state beside those sources, which the clean-room run in
[the evaluation](graphify-evaluation.md) recorded as
`extract --code-only --no-cluster --max-workers 4`.

**`--code-only` and `--no-cluster` are always passed, whatever the pin says.**
They are conditions of the adopt decision, not preferences: a pin that named no
options at all would otherwise have launched the provider into clustering and
whatever extraction it does by default, both of which are separate decisions
nobody has taken. They are folded into the pin's own `options` rather than added
at the launch site, so the manifest records the run that actually happened, and
a pin that tries to undo one of them — `--cluster`, `--no-code-only`, or a
valued form such as `--code-only=false` — is refused rather than quietly
overridden by argument order.

So the adapter collects an artifact afterwards rather than naming one up front.
The state directory the provider wrote (`.graphify` or `.graph`, both already on
the excluded-roots list) is packed into a single reproducible archive: names
sorted, timestamps and ownership fixed, modes normalized, symlinks dropped. Two
builds of one commit have to produce identical bytes, because the manifest binds
a digest of them. That state lands inside the throwaway materialized copy, never
inside the indexed checkout, and the copy is deleted when the build ends.

Packing is bounded as it happens, in both dimensions. The number of entries is
capped while their names are collected, and the serialized archive is written
into a buffer that refuses to grow past the artifact budget. Summing file sizes
is not a bound on the archive: many empty files stay far under the byte budget
while their headers, padding and extended pathname records are bytes this
process has to hold, and a budget checked on a finished archive is checked after
the memory was already taken.

Extraction refuses to run at all over a state directory that already exists.
The census keeps committed provider state out of the materialized copy, so in a
build from this module there is none; the refusal is the second check, because
everything after the run treats whatever is in that directory as output this
run produced.

**Completeness is read from the provider's report, never from its exit status,
and only an affirmative claim counts.** `complete` requires a report shaped the
way the adapter understands one: a claim that the run finished (`complete`,
`completed`, `finished`, or a recognized `status`), a count of what was
indexed, and no counter admitting requeued, pending, or failed work. Everything
else is `partial` — a report that denies completion, one in an unrecognized
schema, an empty object, an unreadable one, one larger than a manifest, and no
report at all. Absent evidence is not evidence of a complete build, and
`partial` is the state `graph_status` refuses by default, so the failure is one
an operator can see and act on. This is the direct consequence of the requeue
defect the evaluation recorded — a repeat that exits zero in 1.63 seconds
having requeued 54 entries has not built a complete graph.

The report is provider output of unknown size, so it is read to one byte past
the manifest bound and refused if it is longer, rather than loaded whole and
measured afterwards. A bound checked on bytes already in memory bounds nothing.

The provider's own stdout and stderr are the other unbounded output, and they
are discarded at the kernel: `stdin`, `stdout` and `stderr` are all
`DEVNULL`. Nothing reads them — completeness comes from the report, not from
what the run printed — so buffering them would only accumulate whatever a
talkative indexer chose to log, for up to the timeout, under neither the
tracked-content budget nor the artifact one. Inheriting them is not the
alternative: diagnostics can echo indexed source, and the process that launched
the build may be writing a machine-readable report to its own stdout.

**Extraction runs in its own process group, and a run that overruns is stopped
as a group.** The adapter waits on the child itself rather than handing the run
to `subprocess.run`, whose timeout kills only the immediate child: an indexer
that started workers — and under a launcher such as `sandbox-exec` the direct
child is the launcher, not the indexer — would otherwise leave them running,
still holding CPU and still writing into a scratch directory the build deletes
as soon as it reports the failure. The group gets `SIGTERM`, a short grace
period, then `SIGKILL`, and the timeout is reported only once nothing is left
running. A new session is safe here precisely because no stream is inherited.

The same check runs when the indexer simply exits, because its exit says nothing
about workers it started: one that is still writing would otherwise have its
output packed mid-write, and its scratch directory removed underneath it. The
group is looked up once at launch and kept — after the leader is reaped its pid
is no longer a safe thing to look a group up from — and an exit that left an
empty group behind costs one signal-`0` probe and no waiting at all.

The subcommand, the state-directory names, and the report counters are constants
in one place in `context_graph_lifecycle.py`. They encode the interface as the
evaluation recorded it; the first installation against a real pinned release
should confirm them against that install and correct them here if they have
moved.

## Refresh is explicit

`build` is the first-time verb and refuses when a usable generation already
binds the revision. `refresh` is the rebuild verb, and it publishes a *new*
immutable generation rather than mutating one in place. Nothing refreshes on a
timer, a hook, or a file-system event, and nothing rebuilds implicitly because a
consumer found the graph stale — a stale graph is reported as stale.

## Failing closed

`graph_status()` resolves to exactly one state, and only `current` is usable.
Nothing falls back to an older generation: a consumer that cannot have the
revision it asked for is told so rather than handed a stale answer that looks
fresh.

| State | Cause |
| --- | --- |
| `absent` | Nothing built for this checkout. |
| `stale` | The manifest's commit/tree does not match the revision being asked about. |
| `corrupt` | The artifact's size or SHA-256 does not match the manifest. |
| `oversized` | The artifact exceeds its budget. |
| `partial` | The provider declared an incomplete build. Usable only with an explicit opt-in. |
| `invalid` | The manifest is unreadable, mislabelled, or the state is not private and operator-owned. |

The privacy check runs on every read, not only at creation: state loosened after
the fact — by a umask change, a restore, or a careless recursive `chmod` —
fails closed rather than being trusted because it was private when it was
written.

`partial` exists because of the requeue defect recorded in the evaluation: a
fast incremental repeat is not proof that the graph is complete.

## Commands

```
code-mower context-graph build   --pin-file PIN --indexer PATH [--revision REV]
code-mower context-graph refresh --pin-file PIN --indexer PATH [--revision REV]
code-mower context-graph status  [--allow-partial] [--json]
code-mower context-graph remove  [--show-local-paths]
code-mower context-graph doctor  [--pin-file PIN]
```

`status` exits non-zero when the graph is not usable, so a script can branch on
it. `build` and `refresh` do the same, and for the same reason: a provider that
admitted an incomplete run has published a generation `status` will call
`partial` and refuse, so the build prints `partial` and exits non-zero rather
than describing it as `current` for as long as it takes to ask again.
`doctor` reports `skip` rather than `fail` when nothing is pinned or built:
the lifecycle is optional, and an operator who never opted in has nothing wrong
with their installation.

The pin file names one exact release and is rejected if it names a range, a
marker, or a distribution without an artifact digest:

```json
{
  "distribution": "graphifyy",
  "version": "0.9.58",
  "wheel_sha256": "e239803288e91c723d6e30540860bd6d5a1dc3f0914b9fc1104b0233e98aaeb8",
  "options": ["--code-only", "--no-cluster"]
}
```

`options` is optional and may name extra provider flags, such as
`--max-workers`. The two restrictions above are added whether or not the file
lists them; listing them changes nothing, and contradicting them is refused.
The list is bounded, and the bound is counted on the options as they will
actually run — the two required flags included — so a pin that passes validation
always round-trips through the manifest it is recorded in.

`--indexer` is the path to a provider CLI the operator has **already**
installed. This repository does not download, install, or resolve one, which is
why the executable is named rather than discovered. Whatever is named is bound
to one absolute path, decided in the directory the command was invoked from: a
relative path such as `.venv/bin/graphify` is resolved against that directory
rather than against the materialized copy the provider runs in, and a bare
command name is looked up on `PATH` there, once. `PATH` is not independent of
the child's directory — an entry on it can itself be relative, and
`PATH=provider-venv/bin` names a different directory once the child starts in
the materialized copy. Leaving the lookup to the launch meant containment was
drawn around the install this process found while the child searched somewhere
else, so a correctly installed provider failed to start; and a repository
carrying that same relative path would have answered the child's search with a
tracked file, which is a build executing content it was only ever meant to read.

## State layout

```
~/.local/share/code-mower/context/graph/<workspace>/
  current                            the published generation's name
  build.lock                         serializes builds for one checkout
  generations/<generation>/manifest.json
  generations/<generation>/graph.bin
```

Directories are 0700 and files 0600. `<workspace>` is derived from the
checkout's **worktree root**, asked of Git rather than taken from the directory
the command was run in. A census reads the commit's whole tree, so `status` in
`src/` asks about exactly the generation `build` at the root published and has
to resolve to it; deriving the name from the invocation directory made every
subdirectory its own workspace, and a graph built at the root then read as
`absent` from `src/` while `remove` there deleted nothing and reported success.
Git answers per worktree, so two worktrees of one repository still get separate
state and can never read each other's generations — each may hold a different
revision. A path Git cannot place — not a repository, a bare one, or no Git on
the host — keeps its resolved path, so state stays nameable and the verbs that
need Git fail on their own terms. The same root is what the provider exposure
rule is drawn against, so a provider installed inside the checkout is refused
from a subdirectory exactly as it is from the root. State is refused inside any Git
repository, which is the enforcement half of adoption condition 2. The refusal
is checked on the resolved path as well as the given one: `--state-dir
/outside/link/state` names no repository in its own spelling while
`/outside/link` points inside one. Symlinked ancestors are resolved rather than
rejected — ordinary private roots have them, macOS reaches `/tmp` through a
link into its `private` directory.

Resolving settles what the ancestors mean at construction and nothing about
what they become afterwards, so the root is opened by walking it from `/` one
component at a time, each against its parent's descriptor with `O_NOFOLLOW`.
Opening the whole absolute path in one call would not do: `O_NOFOLLOW` refuses
only the *final* component, and every ancestor above it is resolved exactly as
a link planted there would want. Nor does finding the deepest existing prefix
first — that prefix is still opened by its full spelling. A pre-created root
behind a newly inserted ancestor link therefore used to be accepted, because
the leaf really is a directory and really is not a link; it is simply not the
directory that was checked.

Renames and removals still travel full paths — a staged generation is renamed
into place, a removed tree is recursed over — and a full path is re-resolved
from the root on every call. Before each of those, the inode the no-follow walk
arrived at is compared against the one the path spells now, and a mismatch is a
refusal rather than a write into whatever the link points at.

## What this does not do

No hooks, no watcher, no hosted service, no MCP HTTP service, no semantic or
model-based extraction, no provider API key, no clustering, and no default
dependency. Each remains a separate explicit decision. The provider seam is an
injected callable, so the entire lifecycle — including the whole test suite —
runs offline with no graph package installed.
