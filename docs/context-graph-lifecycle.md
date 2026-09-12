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
   repository it was never authorized to read. Committed provider state — a
   tracked `.graphify/` or `.graph/`, at any depth, case-folded — is skipped for
   a third reason: it is somebody's old index, and materializing it would let
   the provider resume from a cache built over content this build never saw,
   and let the adapter collect tracked repository bytes as if the provider had
   just produced them. The census digest covers mode, blob name, size and path
   for every entry in sorted order.
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
5. **Publish atomically.** The generation is assembled under a staging name,
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

## The network boundary

An environment variable is a request, not a boundary: `NO_PROXY=*` asks a
cooperating client to connect *directly*, and on a host with internet access an
uncooperative provider is unaffected by any of it. So the provider is launched
behind an argv prefix that denies it sockets at the operating-system level —
`sandbox-exec` on macOS, `bwrap --unshare-net` or an unprivileged network
namespace via `unshare --net` on Linux.

No mechanism is trusted on its name. Each candidate is accepted only after a
probe child launched behind it has been *observed* failing to open a TCP
connection with a denial — `EPERM`, `ENETUNREACH`, and the like. A *refused*
connection is the failure case: it proves the syscall reached the network stack,
so the candidate is rejected. The result is cached for the process, since it is
a property of the host.

A host where no candidate passes gets no build. `subprocess_indexer()` raises
before a single blob is materialized, and `context-graph doctor` reports the
same condition as a failing `context-graph-isolation` check once a provider is
pinned. Running an unconfined provider is not offered as a fallback: an
operator who cannot contain a third-party indexer is better served by knowing
it than by a build that quietly could have reached the network.

A Linux host that restricts unprivileged user namespaces — Ubuntu 24.04 and
GitHub's hosted runners among them — offers no mechanism by default, and both
`unshare` and `bwrap` fail there. Installing bubblewrap (`apt install
bubblewrap`), which ships an AppArmor profile permitting the namespaces it
needs, is the least invasive way to give such a host one. The alternative is to
lift the restriction system-wide
(`sysctl kernel.apparmor_restrict_unprivileged_userns=0`), which is a decision
about the whole machine rather than about this build, and not one this
repository makes on an operator's behalf.

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

So the adapter collects an artifact afterwards rather than naming one up front.
The state directory the provider wrote (`.graphify` or `.graph`, both already on
the excluded-roots list) is packed into a single reproducible archive: names
sorted, timestamps and ownership fixed, modes normalized, symlinks dropped. Two
builds of one commit have to produce identical bytes, because the manifest binds
a digest of them. That state lands inside the throwaway materialized copy, never
inside the indexed checkout, and the copy is deleted when the build ends.

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
it. `doctor` reports `skip` rather than `fail` when nothing is pinned or built:
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

`--indexer` is the path to a provider CLI the operator has **already**
installed. This repository does not download, install, or resolve one, which is
why the executable is named rather than discovered. A relative path such as
`.venv/bin/graphify` is resolved against the directory the command was invoked
from, not against the materialized copy the provider runs in; a bare command
name keeps its `PATH` lookup.

## State layout

```
~/.local/share/code-mower/context/graph/<workspace>/
  current                            the published generation's name
  build.lock                         serializes builds for one checkout
  generations/<generation>/manifest.json
  generations/<generation>/graph.bin
```

Directories are 0700 and files 0600. `<workspace>` is derived from the resolved
checkout path, so two worktrees of the same repository get separate state and
can never read each other's generations. State is refused inside any Git
repository, which is the enforcement half of adoption condition 2.

## What this does not do

No hooks, no watcher, no hosted service, no MCP HTTP service, no semantic or
model-based extraction, no provider API key, no clustering, and no default
dependency. Each remains a separate explicit decision. The provider seam is an
injected callable, so the entire lifecycle — including the whole test suite —
runs offline with no graph package installed.
