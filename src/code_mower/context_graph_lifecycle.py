"""Revision-bound lifecycle for an optional local-repository graph (issue #913).

``context_graph`` decides whether a graph's *citations* are in scope. This
module decides whether the graph should have existed at all: which revision it
binds, which bytes it was allowed to see, where its state lives, and when a
consumer must refuse it.

The rules a local indexer cannot be trusted to follow on its own:

* **Never index a live checkout.** A working tree mutates mid-build and carries
  untracked, ignored, and private files. Every build materializes the tracked
  blobs of one commit into a private staging directory and points the indexer
  at that copy instead. Untracked and ignored files have no path into the
  graph because they are never written.
* **Bind the revision, not the branch.** An artifact records the full commit
  and tree SHA it was built from. A consumer compares those against the
  repository it is actually asking about; a mismatch is stale, and stale fails
  closed rather than answering from the wrong revision.
* **Publish atomically, immutably.** A generation is assembled under a staging
  name, fsynced, then renamed into place; the ``current`` pointer is replaced
  atomically afterwards. A reader either sees the whole previous generation or
  the whole new one, never a half-written directory.
* **Scrub the environment.** The indexer runs with an allowlisted environment,
  so an ambient token cannot leak into a provider process.
* **Deny the network in the kernel, not by request.** Emptying proxy variables
  only redirects a client that chooses to honour them. The provider is
  launched inside an OS sandbox that refuses sockets outright, and the sandbox
  is accepted only after a probe child has been observed failing to reach a
  socket this process is really listening on -- observed at the listener, not
  believed from the child's errno. A host that offers no such mechanism gets a
  refused build, not an unconfined provider.

Nothing here installs, imports, or requires a graph package. The indexer is an
injected callable, so the whole lifecycle is provable offline; the bundled
``subprocess_indexer`` builds the argv and the scrubbed environment for a
pinned provider without this module depending on it.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import io
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from .context_contract import ContextError, _identifier, _text, _timestamp
from .context_store import _private, default_context_root
from .file_locks import FileLockError, exclusive_handle_lock

MANIFEST_SCHEMA = "code_mower.contextGraphBuild.v1"
MANIFEST_NAME = "manifest.json"
ARTIFACT_NAME = "graph.bin"
CURRENT_NAME = "current"

#: Bounds. A local graph is a convenience, not a reason to fill a disk or to
#: stall a session on a pathological repository. Every one of these fails the
#: build closed rather than truncating silently.
MAX_MANIFEST_BYTES = 262_144
MAX_ARTIFACT_BYTES = 256 * 1024 * 1024
MAX_TRACKED_FILES = 50_000
MAX_TRACKED_BYTES = 512 * 1024 * 1024
MAX_BLOB_BYTES = 32 * 1024 * 1024
#: How many files the provider's state may contribute to one artifact. A byte
#: budget alone is not a bound on what packing costs: many empty files stay far
#: under it while their headers, padding and extended pathname records grow the
#: archive this process holds. Sized to the tracked-file bound, because a graph
#: of a checkout has no honest reason to hold more entries than the checkout
#: had files.
MAX_ARTIFACT_ENTRIES = MAX_TRACKED_FILES
#: How many skipped paths one census may record. The file-count budget above
#: bounds only what is materialized, so a repository of symlinks, submodules, or
#: committed provider state passes it while the skipped list grows without
#: limit. A manifest's ``skipped_paths`` is validated against this bound on
#: every read, so a census that could exceed it would build a generation that
#: publishes, prunes its predecessor, and then reads back ``invalid``. Bounded
#: where the entries are collected, before any of that happens.
MAX_SKIPPED_PATHS = MAX_TRACKED_FILES

#: Only ordinary blobs are materialized. A symlink (``120000``) can name a
#: target outside the checkout and a gitlink (``160000``) names a commit in
#: another repository this build was never authorized to read.
_REGULAR_MODES = frozenset({"100644", "100755"})
_SKIPPED_MODES = {"120000": "symlink", "160000": "submodule"}

#: Where the provider keeps its own index state. Both names are on the
#: excluded-roots list in ``context_graph``, and a repository is free to track
#: either of them -- a committed ``.graph/`` is somebody else's graph, or an
#: earlier incremental cache of this one. Neither may be materialized: the
#: provider would then resume from a cache built over content this build never
#: saw, and the adapter would collect tracked repository bytes as if the
#: provider had just produced them, binding stale contents to a fresh commit.
#: Matched at any depth and case-folded, for the same reasons ``.git`` is.
_PROVIDER_STATE_DIRECTORIES = (".graphify", ".graph")
_PROVIDER_STATE_ROOTS = frozenset(name.casefold() for name in _PROVIDER_STATE_DIRECTORIES)

_OBJECT_NAME = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_GENERATION = re.compile(r"[0-9a-f]{32}\Z")
_VERSION = re.compile(r"[0-9][0-9A-Za-z.+!-]{0,63}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")

COMPLETE = "complete"
PARTIAL = "partial"

#: The only variables a provider process inherits. Everything else -- every
#: token, cloud credential, proxy, and provider API key in the operator's
#: session -- is dropped rather than filtered, so a newly invented secret
#: variable is excluded by default instead of needing a new denylist entry.
_ENVIRONMENT_ALLOWLIST = ("PATH", "TMPDIR", "LANG", "LC_ALL", "TZ")

#: Hygiene, not the boundary. Emptying proxy variables stops a cooperating
#: client from finding a proxy and ``GIT_TERMINAL_PROMPT=0`` stops a child
#: blocking on a credential prompt, but on a host with direct connectivity
#: neither denies anything. The boundary is ``network_sandbox_command``.
_NETWORK_DENY = {
    "no_proxy": "*",
    "NO_PROXY": "*",
    "http_proxy": "",
    "https_proxy": "",
    "HTTP_PROXY": "",
    "HTTPS_PROXY": "",
    "ALL_PROXY": "",
    "all_proxy": "",
    "GIT_TERMINAL_PROMPT": "0",
    "PYTHONNOUSERSITE": "1",
}

#: Argv prefixes that place a child in a network-denying OS sandbox, most
#: specific first. Each is a mechanism the host either has or does not; none is
#: trusted on its name, because a prefix that silently degrades to running the
#: command unconfined would be worse than no prefix at all.
_SANDBOX_CANDIDATES: tuple[tuple[str, ...], ...] = (
    ("/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)(deny network*)"),
    ("bwrap", "--unshare-net", "--dev-bind", "/", "/", "--"),
    ("unshare", "--net", "--map-current-user", "--"),
    ("unshare", "--net", "--map-root-user", "--"),
)

#: The probe connects to a socket this process is really listening on, and the
#: verdict is whether the connection *arrived* -- not which errno the child saw.
#: Classifying by errno cannot work: a network namespace brings its own loopback
#: up, so a contained child gets ``ECONNREFUSED`` from an empty namespace while
#: an unconfined child gets ``ECONNREFUSED`` from an unused host port. The two
#: are indistinguishable at the child. They are not indistinguishable at the
#: listener, which either accepts a connection or does not.
#:
#: The probe exits ``7`` when it could not connect and ``3`` when it could;
#: every other code -- a launcher that could not start, a child that never ran
#: -- means the candidate is not usable as a boundary.
#:
#: An exit code alone is not evidence that the child ran: a launcher that exits
#: ``7`` without executing anything produces the same code as a contained child,
#: and would be accepted as a boundary while confining nothing. So the child
#: first prints a value only running it can produce -- the digest of a nonce
#: this process generated for this run -- and a run with no such evidence is
#: unusable whatever its exit code. Echoing the argv is not enough: the digest
#: is computed by the child, and the nonce is fresh per run, so neither a
#: launcher that parrots its arguments nor one that replays an earlier probe
#: can produce it.
_PROBE_DENIED = 7
_PROBE_REACHED = 3
_DENIAL_PROBE = """
import hashlib
import socket
import sys

print(hashlib.sha256(sys.argv[2].encode()).hexdigest(), flush=True)
try:
    probe = socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=5)
except OSError:
    sys.exit(7)
probe.close()
sys.exit(3)
"""

#: How a probe run classifies: the child reached this process's listener, the
#: child ran and could not, or nothing usable happened.
_REACHED = "reached"
_CONTAINED = "contained"
_UNUSABLE = "unusable"

_sandbox_prefix: tuple[str, ...] | None = None
_sandbox_probed = False


def _launcher_path(name: str) -> str | None:
    if os.path.isabs(name):
        return name if os.access(name, os.X_OK) else None
    return shutil.which(name)


def _accepted(listener: socket.socket) -> bool:
    """Did anything actually connect? Drains one pending connection if so."""
    try:
        connection, _ = listener.accept()
    except OSError:
        return False
    connection.close()
    return True


def _ran_the_probe(output: bytes, nonce: str) -> bool:
    """Did this run's probe child actually execute under the launcher?"""
    expected = hashlib.sha256(nonce.encode()).hexdigest()
    return expected in output.decode("utf-8", "replace")


def _classify_probe(prefix: Sequence[str]) -> str:
    """Run the probe under ``prefix`` against a listener in this process."""
    nonce = secrets.token_hex(16)
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
        except OSError:  # pragma: no cover - a host that cannot listen on loopback
            return _UNUSABLE
        # The child connects and exits; the connection waits in the backlog
        # until it is accepted below, so the accept order does not matter.
        listener.settimeout(1)
        port = listener.getsockname()[1]
        try:
            completed = subprocess.run(
                [*prefix, sys.executable, "-c", _DENIAL_PROBE, str(port), nonce],
                check=False,
                capture_output=True,
                timeout=60,
                env={"PATH": os.environ.get("PATH", ""), **_NETWORK_DENY},
            )
        except (OSError, subprocess.SubprocessError):
            return _UNUSABLE
        arrived = _accepted(listener)
    # Before anything is read from the exit code: a run that cannot show its
    # child executed classifies as nothing at all. This is the control that
    # keeps "denied" from being the default answer for a launcher that never
    # started the probe.
    if not _ran_the_probe(completed.stdout, nonce):
        return _UNUSABLE
    if arrived:
        return _REACHED
    if completed.returncode == _PROBE_REACHED:
        # The child says it connected but nothing arrived here; treat the
        # disagreement as a probe that proved nothing rather than as isolation.
        return _UNUSABLE
    return _CONTAINED if completed.returncode == _PROBE_DENIED else _UNUSABLE


def _sandbox_denies_network(prefix: Sequence[str]) -> bool:
    """Watch a child under ``prefix`` fail to reach a socket that is really there."""
    return _classify_probe(prefix) == _CONTAINED


def _probe_network_sandbox() -> tuple[str, ...] | None:
    if not sys.executable:  # pragma: no cover - a frozen interpreter cannot probe
        return None
    # The control, first: a child with no prefix must reach the listener. If it
    # cannot -- no probe interpreter, loopback blocked, sockets unavailable --
    # then "could not connect" proves nothing about any candidate, and every
    # candidate would pass for a boundary. Refuse the whole probe instead.
    if _classify_probe(()) != _REACHED:
        return None
    for candidate in _SANDBOX_CANDIDATES:
        launcher = _launcher_path(candidate[0])
        if launcher is None:
            continue
        prefix = (launcher, *candidate[1:])
        if _sandbox_denies_network(prefix):
            return prefix
    return None


def network_sandbox_command() -> tuple[str, ...] | None:
    """The argv prefix that denies a provider process the network, if any.

    Probed once per process and cached, because the answer is a property of the
    host rather than of a build. ``None`` means this host offers no mechanism
    this build could *observe* working, and a build refuses rather than running
    a provider it cannot contain.
    """
    global _sandbox_prefix, _sandbox_probed
    if not _sandbox_probed:
        _sandbox_prefix = _probe_network_sandbox()
        _sandbox_probed = True
    return _sandbox_prefix


def _object_name(value: Any) -> str:
    """A full SHA-1 or SHA-256 object name. Abbreviations do not bind."""
    if not isinstance(value, str) or not _OBJECT_NAME.fullmatch(value):
        raise ContextError("local graph revision must be a full object name")
    return value


def _digest(value: Any) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ContextError("local graph digest must be a SHA-256 hex digest")
    return value


def _size(value: Any, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ContextError("local graph size must be a bounded non-negative integer")
    return value


#: The extraction restrictions the adopt decision was conditioned on, recorded
#: in ``docs/graphify-evaluation.md``: code-only extraction, and no clustering.
#: They are not the operator's to omit. Model-based extraction and clustering
#: are separate explicit decisions nobody has taken, and a pin that simply left
#: its ``options`` empty would have launched the provider into both.
_REQUIRED_EXTRACT_OPTIONS = ("--code-only", "--no-cluster")

#: Options that would undo one of the above. Named exactly rather than guessed
#: at: these are the negations of the two flags this module requires, so a pin
#: carrying one is asking for behaviour the adoption conditions exclude and is
#: refused rather than silently overridden by argument order.
_CONFLICTING_EXTRACT_OPTIONS = frozenset({"--cluster", "--no-code-only"})

#: How many options an extraction may carry, counted on the normalized tuple --
#: the one the launcher passes and the manifest records -- rather than on what a
#: pin file happened to spell. Counting the raw list instead would let a pin
#: pass validation and then normalize into a value its own ``as_metadata`` could
#: no longer be read back through: a build could publish a generation whose
#: manifest is rejected the moment anything reloads it, pruning the last usable
#: generation in favour of one nothing can read.
MAX_EXTRACT_OPTIONS = 16


def _extraction_options(options: Iterable[str]) -> tuple[str, ...]:
    """The options every extraction runs with: the required ones, then the pin's.

    Required unconditionally rather than merely validated, so a pin written
    before these conditions existed -- or one with no ``options`` at all --
    still launches a restricted run. They are prepended into the pin itself
    rather than added at the call site, so the manifest records what actually
    ran instead of what was asked for.

    The result is a fixed point: the required flags are dropped from the input
    wherever they appear and re-prepended exactly once, so normalizing an
    already-normalized tuple returns it unchanged and a pin round-trips through
    ``as_metadata`` and :func:`load_pin` without changing length or meaning.
    """
    extra: list[str] = []
    for option in options:
        name = option.split("=", 1)[0].strip()
        if name in _CONFLICTING_EXTRACT_OPTIONS:
            raise ContextError(
                "local graph provider options may not re-enable clustering or non-code extraction"
            )
        if name in _REQUIRED_EXTRACT_OPTIONS:
            if option != name:
                # ``--code-only=false`` is the same request as ``--no-code-only``.
                raise ContextError(
                    "local graph provider options may not give a value to a required extraction flag"
                )
            continue
        extra.append(option)
    normalized = (*_REQUIRED_EXTRACT_OPTIONS, *extra)
    if len(normalized) > MAX_EXTRACT_OPTIONS:
        raise ContextError("local graph provider options must be a bounded list")
    return normalized


@dataclass(frozen=True)
class GraphifyPin:
    """An exact provider pin. A range would let a build drift silently.

    ``wheel_sha256`` is the artifact digest recorded by the adopt decision in
    ``docs/graphify-evaluation.md``. It is carried into every build manifest so
    a graph built by a substituted distribution is identifiable after the fact,
    which is the whole point of pinning a lookalike-prone package name.

    ``options`` always carries the required extraction restrictions, whichever
    way the pin was constructed, so there is no shape of this object that could
    launch an unrestricted run.
    """

    distribution: str
    version: str
    wheel_sha256: str
    options: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "options", _extraction_options(self.options))

    @property
    def requirement(self) -> str:
        return f"{self.distribution}=={self.version}"

    def as_metadata(self) -> dict[str, Any]:
        return {
            "distribution": self.distribution,
            "version": self.version,
            "wheel_sha256": self.wheel_sha256,
            "options": list(self.options),
        }


def load_pin(source: Mapping[str, Any]) -> GraphifyPin:
    """Parse a provider pin, rejecting anything that is not one exact release."""
    if not isinstance(source, Mapping):
        raise ContextError("local graph provider pin must be an object")
    unknown = set(source) - {"distribution", "version", "wheel_sha256", "options"}
    if unknown:
        raise ContextError("local graph provider pin carries unsupported fields")
    version = source.get("version")
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise ContextError("local graph provider pin must name one exact released version")
    options = source.get("options", [])
    # A cheap guard on the untrusted list before any of it is copied; the
    # binding bound is applied to the normalized tuple in ``_extraction_options``
    # below, which is what the launcher and the manifest actually carry.
    if not isinstance(options, list) or len(options) > MAX_EXTRACT_OPTIONS:
        raise ContextError("local graph provider options must be a bounded list")
    return GraphifyPin(
        distribution=_identifier(source.get("distribution")),
        version=version,
        wheel_sha256=_digest(source.get("wheel_sha256")),
        options=tuple(_text(option, maximum=128) for option in options),
    )


@dataclass(frozen=True)
class TrackedEntry:
    """One tracked regular file at the bound revision."""

    mode: str
    blob: str
    path: str
    size: int


@dataclass(frozen=True)
class TrackedCensus:
    """What the indexer was allowed to see, and proof of exactly which bytes.

    ``digest`` covers mode, blob name, size, and path for every entry in sorted
    order. Two builds of the same commit produce the same census digest, and a
    census that silently gained or lost a file produces a different one, so a
    manifest's census claim is checkable without re-reading the repository.
    """

    entries: tuple[TrackedEntry, ...]
    skipped: tuple[tuple[str, str], ...]
    digest: str

    @property
    def file_count(self) -> int:
        return len(self.entries)

    @property
    def total_bytes(self) -> int:
        return sum(entry.size for entry in self.entries)


def _census_digest(entries: Iterable[TrackedEntry]) -> str:
    census = hashlib.sha256()
    for entry in entries:
        census.update(f"{entry.mode} {entry.blob} {entry.size} {entry.path}\n".encode())
    return census.hexdigest()


def git_environment() -> dict[str, str]:
    """The environment every Git child of a build runs in.

    One definition for both invocation paths -- the census reader and the blob
    materializer -- because a boundary that only half the children observe is
    not a boundary. Beyond the scrubbing an indexer gets, this denies Git the
    two ways a *read* can reach the network: ``GIT_NO_LAZY_FETCH`` stops a
    partial clone fetching a missing object mid-read, and an empty
    ``GIT_ALLOW_PROTOCOL`` leaves no transport on the allowlist, so a fetch
    that was somehow attempted anyway has nothing to attempt it over.

    ``GIT_NO_REPLACE_OBJECTS`` is the third: a ``refs/replace`` entry in the
    checkout substitutes one object's bytes for another's on every read, so a
    census and a materialization would bind content that the commit and tree
    this manifest records do not contain. Deleting the replacement afterwards
    would leave ``graph_status`` reporting ``current`` for a graph of bytes
    that revision never had, because it compares object names and nothing else.
    """
    environment = dict(_NETWORK_DENY)
    for name in _ENVIRONMENT_ALLOWLIST:
        if name in os.environ:
            environment[name] = os.environ[name]
    environment.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_ATTR_NOSYSTEM="1",
        GIT_OPTIONAL_LOCKS="0",
        GIT_NO_LAZY_FETCH="1",
        GIT_NO_REPLACE_OBJECTS="1",
        # An empty allowlist, not an absent one: git treats the variable as the
        # complete set of permitted transports, so "" permits none.
        GIT_ALLOW_PROTOCOL="",
        GIT_PROTOCOL_FROM_USER="0",
        GIT_TERMINAL_PROMPT="0",
        GIT_SSH_COMMAND="/usr/bin/false",
    )
    return environment


#: Overrides passed on the command line because that is the only level that
#: outranks the repository's own ``.git/config``. System and global
#: configuration are dropped by the environment above, but local configuration
#: belongs to the untrusted checkout and is always read.
_GIT_SAFETY_OPTIONS: tuple[str, ...] = (
    "-c", "protocol.allow=never",
    "-c", "core.fsmonitor=false",
    "-c", "fetch.recurseSubmodules=no",
    "-c", "uploadpack.allowFilter=false",
)

#: Local configuration that means "objects may be missing and fetched on
#: demand". A build refuses such a checkout outright rather than relying on
#: ``GIT_NO_LAZY_FETCH``, which older Git releases do not honour.
_PARTIAL_CLONE_KEYS = r"^(extensions\.partialclone|remote\..*\.(promisor|partialclonefilter))$"


def _git(repository: Path, *arguments: str, capture: bool = True, permit_failure: bool = False) -> str:
    """Run git with repository configuration disarmed and no way out to a network.

    A build reads an untrusted checkout. Local, global, and system
    configuration can install clean/smudge filters, alternate object stores,
    and hook paths, any of which would run code or reach outside the
    repository during what looks like a read. This drops all three, and
    ``git_environment`` closes the transports a read could otherwise use.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), "--no-optional-locks", *_GIT_SAFETY_OPTIONS, *arguments],
            check=not permit_failure,
            capture_output=capture,
            text=True,
            env=git_environment(),
        )
    except (OSError, UnicodeError, subprocess.SubprocessError):
        raise ContextError("local graph build could not read the target repository") from None
    if permit_failure and completed.returncode != 0:
        return ""
    return completed.stdout


#: The longest NUL-delimited ``ls-tree`` record a census will hold before it has
#: seen the delimiter that ends it. A record is fixed-width metadata plus one
#: path, and Git's own path ceiling is far under this, so a longer run of bytes
#: means the stream is not the one this build asked for.
MAX_CENSUS_RECORD_BYTES = 16 * 1024

#: How much of the listing to read at a time. Small enough that a refusal costs
#: one chunk rather than a whole repository's metadata.
_CENSUS_CHUNK_BYTES = 64 * 1024


def _stop_reader(process: subprocess.Popen[bytes]) -> None:
    """Stop a streaming Git child and reap it, whatever the caller is doing.

    Closing the pipe first is what makes an early refusal cheap: Git writes into
    a broken pipe and exits on its own, rather than being left to finish
    enumerating a tree nobody is going to read.
    """
    if process.stdout is not None:
        with contextlib.suppress(OSError):
            process.stdout.close()
    if process.poll() is None:
        with contextlib.suppress(OSError):
            process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=_REAP_TIMEOUT_SECONDS)


@contextlib.contextmanager
def _git_records(repository: Path, *arguments: str) -> Iterator[Iterator[str]]:
    """Yield the NUL-delimited records of one Git child, one at a time.

    ``subprocess.run`` would hold the whole listing in memory before the first
    budget could be checked, so a tree far past every census bound would exhaust
    this process instead of being refused at the bound. Streaming lets the
    consumer stop at the record that breaks its budget; leaving the child to
    this context manager means it is terminated there rather than whenever a
    generator happens to be collected.
    """
    try:
        process = subprocess.Popen(
            ["git", "-C", str(repository), "--no-optional-locks", *_GIT_SAFETY_OPTIONS, *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=git_environment(),
        )
    except (OSError, subprocess.SubprocessError):
        raise ContextError("local graph build could not read the target repository") from None
    try:
        yield _read_records(process)
    finally:
        _stop_reader(process)


def _read_records(process: subprocess.Popen[bytes]) -> Iterator[str]:
    stream = process.stdout
    assert stream is not None
    pending = b""
    while True:
        chunk = stream.read(_CENSUS_CHUNK_BYTES)
        if not chunk:
            break
        pending += chunk
        while True:
            record, delimiter, rest = pending.partition(b"\0")
            if not delimiter:
                break
            if len(record) > MAX_CENSUS_RECORD_BYTES:
                raise ContextError("local graph build could not read the repository census")
            pending = rest
            yield _record_text(record)
        # The same bound on what has *not* been delimited yet: a stream with no
        # delimiter in it would otherwise grow a chunk at a time forever.
        if len(pending) > MAX_CENSUS_RECORD_BYTES:
            raise ContextError("local graph build could not read the repository census")
    if pending:
        # ``-z`` terminates every record, so a trailing remainder is a stream
        # that stopped mid-record: a killed child, or output this build did not
        # ask for. Either way the census it would produce is incomplete.
        raise ContextError("local graph build could not read the repository census")
    if process.wait() != 0:
        raise ContextError("local graph build could not read the target repository")


def _record_text(record: bytes) -> str:
    try:
        return record.decode("utf-8")
    except UnicodeDecodeError:
        raise ContextError("local graph build could not read the repository census") from None


def refuse_lazy_object_fetch(repository: Path) -> None:
    """Refuse a partial clone, where reading the tree can call out to a remote.

    ``ls-tree`` and ``cat-file`` look like pure local reads, and in a full
    clone they are. In a partial clone a missing object is fetched from the
    promisor remote on demand -- during the build, over a transport the
    repository configured, outside the sandbox the provider runs in. There is
    no bounded way to prove ahead of time which objects are present, so the
    build declines the whole repository shape instead.
    """
    declared = _git(
        repository,
        "config",
        "--local",
        "--name-only",
        "--get-regexp",
        _PARTIAL_CLONE_KEYS,
        permit_failure=True,
    )
    if declared.strip():
        raise ContextError(
            "local graph builds refuse a partial clone: reading its tree can fetch objects "
            "from a remote during the build; use a full clone of this checkout"
        )


def resolve_revision(repository: Path, revision: str = "HEAD") -> tuple[str, str]:
    """Return the full ``(commit, tree)`` names a build would bind.

    The tree is resolved separately rather than derived, because it is what a
    consumer actually compares: two commits with different messages or parents
    over identical content share a tree, and a graph of that content is still
    accurate for both.
    """
    name = _text(revision)
    if name.startswith("-"):
        # Otherwise the revision reaches ``git rev-parse`` as an option.
        raise ContextError("local graph revision must not begin with an option marker")
    commit = _object_name(_git(repository, "rev-parse", "--verify", f"{name}^{{commit}}").strip())
    tree = _object_name(_git(repository, "rev-parse", "--verify", f"{commit}^{{tree}}").strip())
    return commit, tree


def _is_provider_state(path: str) -> bool:
    """Is this tracked path part of a committed provider index state?"""
    return any(segment.casefold() in _PROVIDER_STATE_ROOTS for segment in path.split("/"))


def read_tracked_census(repository: Path, commit: str) -> TrackedCensus:
    """List the tracked regular files of one commit, with their blob sizes.

    Reads the commit's tree, never the working tree or the index, so an
    uncommitted edit, an untracked scratch file, and an ignored secret are all
    invisible here by construction rather than by filtering.

    Committed provider state is recorded as skipped rather than carried: it is
    excluded from the census, so it is excluded from the census digest too, and
    a build over a repository that tracks a ``.graphify`` directory binds a
    census that says so instead of quietly indexing somebody else's graph.

    The listing is consumed as it arrives rather than captured whole: every
    budget here is checked against the records seen so far, so a tree past one
    of them is refused at that record, with the reader stopped, instead of
    after a repository's worth of metadata has been buffered.
    """
    refuse_lazy_object_fetch(repository)
    entries: list[TrackedEntry] = []
    skipped: list[tuple[str, str]] = []
    total = 0

    def skip(path: str, reason: str) -> None:
        if len(skipped) >= MAX_SKIPPED_PATHS:
            raise ContextError("skipped path census exceeds the local graph budget")
        skipped.append((path, reason))

    with _git_records(
        repository,
        "ls-tree",
        "-r",
        "-z",
        "--long",
        "--full-tree",
        _object_name(commit),
    ) as records:
        for record in records:
            if not record:
                continue
            metadata, _, path = record.partition("\t")
            fields = metadata.split()
            if len(fields) != 4 or not path:
                raise ContextError("local graph build could not read the repository census")
            mode, kind, blob, raw_size = fields
            if mode in _SKIPPED_MODES:
                skip(path, _SKIPPED_MODES[mode])
                continue
            if _is_provider_state(path):
                skip(path, "provider state")
                continue
            if mode not in _REGULAR_MODES or kind != "blob":
                skip(path, "unsupported")
                continue
            if len(entries) >= MAX_TRACKED_FILES:
                raise ContextError("tracked file census exceeds the local graph budget")
            size = int(raw_size) if raw_size.isdigit() else -1
            if not 0 <= size <= MAX_BLOB_BYTES:
                raise ContextError("tracked file exceeds the local graph per-file budget")
            total += size
            if total > MAX_TRACKED_BYTES:
                raise ContextError("tracked content exceeds the local graph budget")
            entries.append(TrackedEntry(mode=mode, blob=_object_name(blob), path=path, size=size))
    entries.sort(key=lambda entry: entry.path)
    return TrackedCensus(
        entries=tuple(entries),
        skipped=tuple(sorted(skipped)),
        digest=_census_digest(entries),
    )


def _safe_relative(path: str) -> Path:
    """Reject any census path that would escape the materialization root.

    Git does not normally produce these, but a build must not depend on that:
    the destination is created by this process and everything written into it
    is checked here first.
    """
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or any(segment in {"", ".", ".."} for segment in path.split("/"))
        # Every segment, not just the first: a vendored submodule's ``vendor/.git``
        # is as private as the top-level one. Case-folded because APFS and NTFS
        # name the same directory ``.GIT``.
        or any(segment.casefold() == ".git" for segment in path.split("/"))
        # Provider state is skipped by the census, so a census that still
        # carries it was not built by ``read_tracked_census``. Refuse rather
        # than seed the directory the provider is about to write into.
        or _is_provider_state(path)
    ):
        raise ContextError("tracked path must stay inside the materialized checkout")
    return Path(*path.split("/"))


def materialize_tracked_files(repository: Path, census: TrackedCensus, destination: Path) -> int:
    """Write the census's blobs into ``destination``. Returns bytes written.

    ``destination`` must not already exist: an immutable materialization is one
    this build created and fully owns, so there is no prior content to
    reconcile and no possibility of reusing a directory somebody else can
    write. Blob content comes from ``git cat-file --batch`` in one child
    process rather than one per file.
    """
    refuse_lazy_object_fetch(repository)
    if destination.exists():
        raise ContextError("local graph materialization requires a fresh private directory")
    destination.mkdir(mode=0o700, parents=True)
    if not census.entries:
        return 0
    written = 0
    process = subprocess.Popen(
        ["git", "-C", str(repository), "--no-optional-locks", *_GIT_SAFETY_OPTIONS, "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=git_environment(),
    )
    try:
        assert process.stdin is not None and process.stdout is not None
        for entry in census.entries:
            target = destination / _safe_relative(entry.path)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            process.stdin.write(entry.blob.encode() + b"\n")
            process.stdin.flush()
            header = process.stdout.readline().decode("utf-8", "replace").split()
            if len(header) != 3 or header[1] != "blob" or not header[2].isdigit():
                raise ContextError("local graph materialization could not read tracked content")
            size = int(header[2])
            if size != entry.size:
                raise ContextError("tracked content changed during materialization")
            payload = process.stdout.read(size)
            if len(payload) != size or process.stdout.read(1) != b"\n":
                raise ContextError("local graph materialization was truncated")
            # 0o600 regardless of the tracked mode: the indexer reads this copy
            # and never executes it, and an executable bit here would only
            # widen what a provider process can do with the staging directory.
            handle = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(handle, "wb") as stream:
                stream.write(payload)
            written += size
    except BaseException:
        process.kill()
        raise
    finally:
        for stream in (process.stdin, process.stdout):
            if stream is not None and not stream.closed:
                stream.close()
        try:
            process.wait(timeout=60)
        except subprocess.TimeoutExpired:  # pragma: no cover - unresponsive child
            process.kill()
    return written


def scrubbed_environment(*, home: Path, temporary: Path) -> dict[str, str]:
    """Build the provider process environment from an allowlist.

    An indexer inherits nothing from the operator's session but the variables
    it needs to find executables and write scratch files. ``HOME`` is
    redirected into the build's own private directory so a provider's
    configuration, cache, or credential lookup lands there instead of reading
    or writing the operator's real home.
    """
    environment = {
        name: os.environ[name] for name in _ENVIRONMENT_ALLOWLIST if name in os.environ
    }
    environment.update(_NETWORK_DENY)
    environment.update(
        HOME=str(home),
        TMPDIR=str(temporary),
        XDG_CONFIG_HOME=str(home / "config"),
        XDG_CACHE_HOME=str(home / "cache"),
        XDG_DATA_HOME=str(home / "data"),
        LC_ALL="C",
        LANG="C",
        TZ="UTC",
    )
    return environment


@dataclass(frozen=True)
class IndexRequest:
    """What an indexer is given: a frozen copy, a scratch area, and a pin."""

    source_root: Path
    output_path: Path
    environment: Mapping[str, str]
    pin: GraphifyPin
    commit: str
    tree: str


@dataclass(frozen=True)
class IndexResult:
    """What an indexer reports back. ``completeness`` is its own admission."""

    completeness: str = COMPLETE
    indexed_files: int = 0
    notes: tuple[str, ...] = ()


def _resolved_executable(executable: str) -> str:
    """Bind a relative provider path to the invocation directory.

    The provider runs with its working directory set to the materialized copy,
    so ``--indexer .venv/bin/graphify`` would otherwise be looked up inside the
    frozen source tree, where the operator's install is not. A bare command
    name keeps its ``PATH`` lookup, which is unaffected by the child's
    directory.
    """
    if not isinstance(executable, str) or not executable:
        raise ContextError("local graph provider executable must be named")
    separators = [os.sep, os.altsep] if os.altsep else [os.sep]
    if any(separator in executable for separator in separators):
        return str(Path(executable).resolve())
    return executable


#: The subcommand the evaluated release exposes, recorded in
#: ``docs/graphify-evaluation.md``: the clean-room run indexed with
#: ``extract --code-only --no-cluster --max-workers 4``. There is no
#: ``--source``/``--output`` pair to hand it; ``extract`` reads the directory
#: it is run in and writes its state beside those sources, which is why the
#: child's working directory is the materialized copy and why the adapter
#: collects an artifact afterwards rather than naming one up front.
_PROVIDER_EXTRACT = "extract"

#: The provider's own record of what it processed. Completeness is read from
#: here, never inferred from an exit status: the clean-room run recorded 54
#: manifest entries requeued by a repeat that exited zero in 1.63 s.
_PROVIDER_REPORT_NAMES = ("manifest.json", "index.json", "report.json")

#: Counters whose presence above zero means the provider did not finish. Any
#: one of them, not all: a report that admits requeued entries is a partial
#: build however healthy the rest of it looks.
_INCOMPLETE_COUNTERS = ("requeued", "pending", "failed", "errors", "incomplete")

#: Where the provider reports how many files it actually indexed.
_INDEXED_COUNTERS = ("indexed_files", "code_files", "files", "entries")

#: An affirmative claim that the run finished, in either shape a report can
#: carry one. Nothing else counts: an empty object, or one whose schema this
#: adapter does not recognize, says nothing about completion and is therefore
#: not evidence of it.
_COMPLETION_FLAGS = ("complete", "completed", "finished")
#: Narrow on purpose: a field that names the run's state, not one that might
#: carry a path or a message, so an unrecognized value here is a real
#: non-completion rather than an adapter that read the wrong field.
_COMPLETION_STATUS_FIELDS = ("status", "state")
_COMPLETION_STATUS_VALUES = frozenset(
    {"complete", "completed", "success", "succeeded", "ok", "finished", "done"}
)


def _refuse_pre_existing_provider_state(source_root: Path) -> None:
    """Refuse to extract on top of index state this build did not produce.

    The census excludes committed provider state, so in a build from this
    module nothing is here. This is the second check rather than the only one:
    the source root is an argument, and the whole point of resolving the state
    directory afterwards is to treat what is found as freshly produced output.
    A directory that predates the run would let the provider resume from a
    cache of content it was never shown, and would be collected as if it were
    this commit's graph.
    """
    for name in _PROVIDER_STATE_DIRECTORIES:
        if (source_root / name).exists() or (source_root / name).is_symlink():
            raise ContextError(
                "local graph build refuses to extract over pre-existing provider state; "
                "no generation was published"
            )


def _provider_state_directory(source_root: Path) -> Path:
    """The state directory the provider wrote during this run.

    Only reachable after ``_refuse_pre_existing_provider_state``, so whichever
    of the two names is present was created by the run that just finished.
    """
    for name in _PROVIDER_STATE_DIRECTORIES:
        candidate = source_root / name
        if candidate.is_dir() and not candidate.is_symlink():
            return candidate
    raise ContextError("local graph provider wrote no index state; no generation was published")


def _provider_report(state_directory: Path) -> Mapping[str, Any] | None:
    """The provider's completion evidence, or ``None`` if it left none.

    Bounded at the stream, not after the fact: the report is provider output of
    unknown size, and reading it whole to slice it afterwards would let it
    exhaust this process before any budget was consulted. Anything longer than
    a manifest is rejected outright rather than parsed from a prefix, which
    would be a different document than the one the provider wrote.
    """
    for name in _PROVIDER_REPORT_NAMES:
        path = state_directory / name
        if not path.is_file() or path.is_symlink():
            continue
        try:
            with path.open("rb") as stream:
                raw = stream.read(MAX_MANIFEST_BYTES + 1)
        except OSError:
            return None
        if len(raw) > MAX_MANIFEST_BYTES:
            return None
        try:
            payload = json.loads(raw)
        except ValueError:
            return None
        return payload if isinstance(payload, Mapping) else None
    return None


def _completion_claim(report: Mapping[str, Any]) -> bool | None:
    """``True`` finished, ``False`` denied it, ``None`` said nothing either way."""
    claim: bool | None = None
    for name in _COMPLETION_FLAGS:
        value = report.get(name)
        if value is True:
            claim = True
        elif value is False:
            return False
    for field in _COMPLETION_STATUS_FIELDS:
        value = report.get(field)
        if not isinstance(value, str):
            continue
        if value.strip().casefold() in _COMPLETION_STATUS_VALUES:
            claim = True
        else:
            # A status the adapter does not recognize is not a completion.
            return False
    return claim


def _indexed_count(report: Mapping[str, Any]) -> int | None:
    """How many files the provider says it indexed, if it says at all."""
    for counter in _INDEXED_COUNTERS:
        value = report.get(counter)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def _read_completeness(report: Mapping[str, Any] | None) -> IndexResult:
    """Classify a provider run from its own report, defaulting to partial.

    Absent or unreadable evidence is *not* evidence of a complete build, and
    neither is a readable report that says nothing. ``complete`` is reached
    only by a report shaped the way this adapter understands one: an
    affirmative completion claim, a count of what was indexed, and no counter
    admitting work left over. An empty object, an unrecognized schema, and a
    document that happens to parse all stay ``partial``, which
    ``graph_status`` refuses by default. The provider owns no provenance (the
    evaluation records this as the first product constraint), so that refusal
    is the failure an operator can act on; silently calling it complete is the
    one they cannot.
    """
    if report is None:
        return IndexResult(completeness=PARTIAL, notes=("provider left no readable completion report",))
    notes: list[str] = []
    for counter in _INCOMPLETE_COUNTERS:
        value = report.get(counter)
        if value is True:
            notes.append(f"provider reported {counter}")
        elif isinstance(value, int) and not isinstance(value, bool) and value > 0:
            notes.append(f"provider reported {value} {counter}")
    claim = _completion_claim(report)
    if claim is False:
        notes.append("provider did not report the extraction as complete")
    elif claim is None:
        notes.append("provider report carried no completion claim")
    indexed = _indexed_count(report)
    if indexed is None:
        notes.append("provider report did not say how many files it indexed")
    if notes:
        return IndexResult(completeness=PARTIAL, indexed_files=indexed or 0, notes=tuple(notes))
    return IndexResult(completeness=COMPLETE, indexed_files=indexed)


class _BoundedBuffer(io.BytesIO):
    """A buffer that refuses to grow past the artifact budget.

    The budget has to be enforced on what this process allocates, as it is
    allocated. A check on the finished archive is a check made after the
    memory was already taken, and a check on the sum of the file sizes is not
    a bound on the archive at all: headers, padding and extended pathname
    records are bytes the provider can make this process hold without ever
    writing content.
    """

    def write(self, data) -> int:  # type: ignore[override]
        if self.tell() + len(data) > MAX_ARTIFACT_BYTES:
            raise ContextError("local graph artifact exceeds its budget; no generation was published")
        return super().write(data)


def _pack_state(state_directory: Path) -> bytes:
    """Collect the provider's state into one reproducible artifact.

    Names sorted, timestamps and ownership fixed, modes normalized: two builds
    of the same commit must produce the same bytes, because the manifest binds
    a digest of them. Only regular files are taken -- a symlink in provider
    state would name a target outside the artifact, which an immutable
    generation cannot carry.

    Both the number of entries and the serialized size are bounded while the
    archive is being built, so a provider that wrote pathologically many files
    is refused before this process has held them: even collecting the names to
    sort them is done against the entry bound rather than into an unbounded
    list.
    """
    entries: list[Path] = []
    for path in state_directory.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        entries.append(path)
        if len(entries) > MAX_ARTIFACT_ENTRIES:
            raise ContextError(
                "local graph artifact holds more files than its budget allows; no generation was published"
            )
    buffer = _BoundedBuffer()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(entries, key=lambda item: str(item.relative_to(state_directory))):
            info = tarfile.TarInfo(str(path.relative_to(state_directory)))
            info.size = path.stat().st_size
            info.mtime = 0
            info.mode = 0o600
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with path.open("rb") as stream:
                archive.addfile(info, stream)
    return buffer.getvalue()


#: How long one extraction may run. A bound on the wall clock a build can cost,
#: not a guess at how long a real one takes.
EXTRACTION_TIMEOUT_SECONDS = 900

#: How long a timed-out provider group is given to exit on its own terms before
#: the group is killed outright. Short: the run has already exceeded its whole
#: time budget, and the caller is about to delete the directory these processes
#: are writing into.
_TERMINATION_GRACE_SECONDS = 5.0

#: How long to wait for a killed process to be reaped. ``SIGKILL`` is not
#: refusable, so this bounds a wait on the kernel rather than on the child.
_REAP_TIMEOUT_SECONDS = 10.0


#: How often the grace period is re-checked when what is being waited for is
#: the group rather than the direct child. After a normal exit the leader has
#: already been reaped, so there is no child left to wait on and the only way
#: to see the group empty is to ask.
_GROUP_POLL_SECONDS = 0.05


def _signal_group(group: int, number: int) -> None:
    try:
        os.killpg(group, number)
    except OSError:
        # Already gone, or never ours to signal. Either way there is nothing
        # left to stop, and a failure here must not mask the timeout.
        pass


def _session_group(child: subprocess.Popen[bytes]) -> int | None:
    """The group the child leads, read while the child is still unreaped.

    Read once at launch and retained for the rest of the run, because a pid is
    only a safe thing to look a group up from while its process has not been
    reaped: afterwards ``os.getpgid`` either fails or answers for whichever
    process inherited the number. The group id itself stays safe to signal for
    exactly as long as it is worth signalling, because the kernel does not
    reuse a pid while it still names a process group with members in it.

    ``None`` means there is no group of this run's own to signal -- the child
    never reached one, so the only thing that can be stopped is the child.
    """
    try:
        group = os.getpgid(child.pid)
    except OSError:
        return None
    if group == os.getpgid(0):
        return None
    return group


def _group_is_empty(group: int) -> bool:
    """Whether anything is left in ``group``.

    Signal ``0`` runs the kernel's existence and permission checks without
    delivering anything, so this is the group's own answer rather than an
    inference from what the leader did. A refusal is not emptiness: something
    has to be there for the kernel to refuse on behalf of.
    """
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def _await_group_exit(group: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _group_is_empty(group):
            return
        time.sleep(_GROUP_POLL_SECONDS)


def _terminate_process_group(child: subprocess.Popen[bytes], group: int | None) -> None:
    """Stop everything the provider started, however this run ended.

    ``subprocess.run``'s own timeout kills the immediate child only. A provider
    that forks workers -- and under a launcher such as ``sandbox-exec`` the
    process that is signalled may be the launcher rather than the indexer --
    would leave those workers running, still holding CPU and still writing into
    a scratch directory the build is about to delete. The child leads its own
    session, so one signal to its group reaches every descendant that has not
    deliberately left it.

    A leader that exits on its own is not evidence that its workers did. The
    group is therefore checked on an ordinary return too, and not only on the
    timeout and the interrupt: otherwise a provider that returns while a worker
    is still writing leaves ``subprocess_indexer`` packing an artifact that is
    concurrently being modified, and ``build_graph`` removing a scratch
    directory that is still in use. Checking costs nothing in the ordinary
    case, where the group is already empty and nothing is signalled or waited
    for.
    """
    leader_running = child.poll() is None
    if group is None:
        if leader_running:
            child.kill()
            _reap(child)
        return
    if not leader_running and _group_is_empty(group):
        # The ordinary ending: the provider exited and took its workers with it.
        return
    _signal_group(group, signal.SIGTERM)
    if leader_running:
        try:
            child.wait(timeout=_TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
    else:
        # Nothing here is this process's child any more -- the survivors were
        # reparented when the leader died -- so the grace period is spent
        # watching the group instead of waiting on a handle.
        _await_group_exit(group, _TERMINATION_GRACE_SECONDS)
    # Unconditionally, and after the grace period: a leader exiting says nothing
    # about workers it started, and this is the last moment anything can stop
    # them.
    _signal_group(group, signal.SIGKILL)
    if leader_running:
        _reap(child)


def _reap(child: subprocess.Popen[bytes]) -> None:
    try:
        child.wait(timeout=_REAP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:  # pragma: no cover - a killed child is reapable
        pass


def _run_contained(
    argv: Sequence[str],
    *,
    environment: Mapping[str, str],
    cwd: str,
    timeout: float,
) -> int:
    """Run one child in its own process group, stopping the group on any exit.

    Raises :class:`subprocess.TimeoutExpired` once the group has been stopped,
    so a caller reports the timeout only after there is nothing left running.
    A returned exit status carries the same guarantee: the caller reads the
    provider's own status, and by then nothing the provider started is still
    running against the state the caller is about to pack up or delete.
    """
    with subprocess.Popen(  # noqa: S603 - argv is a resolved executable and validated options
        list(argv),
        # Neither stream is read, and neither may be buffered: a provider that
        # logs its progress would otherwise accumulate unbounded output in this
        # process for up to the timeout, outside both the tracked-content and
        # artifact budgets. The streams are discarded at the kernel rather than
        # inherited, because provider diagnostics can echo indexed source and
        # this process may be writing a machine-readable report. ``stdin`` goes
        # the same way: the child has no operator to prompt.
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=dict(environment),
        cwd=cwd,
        # A new session, so the child leads a process group that can be
        # signalled as a unit and that this process is not a member of. Safe
        # only because no stream is inherited: nothing here needs a controlling
        # terminal.
        start_new_session=True,
    ) as child:
        # Read before the wait, because the wait may reap the leader and a
        # reaped leader's pid is no longer a safe thing to look a group up from.
        group = _session_group(child)
        try:
            return child.wait(timeout=timeout)
        finally:
            # Every way out of this wait, including returning. An operator's
            # Ctrl-C raises ``KeyboardInterrupt`` here, and the provider does
            # not see that signal: it leads its own session, so the terminal's
            # SIGINT never reaches it. ``Popen.__exit__`` would then wait for a
            # child nobody has asked to stop, while ``build_graph`` deletes the
            # scratch directory underneath it. A provider that simply exits can
            # leave workers behind the same way, so the ordinary return is
            # cleaned up on the same path rather than trusted. Unwinding --
            # or returning -- leaves nothing running against state that is
            # about to be read, packed, or removed.
            _terminate_process_group(child, group)


def subprocess_indexer(executable: str) -> Callable[[IndexRequest], IndexResult]:
    """Run a pinned provider CLI over the materialized copy, without a network.

    Kept as a factory so the lifecycle never imports or requires a graph
    package: a deployment that has installed the pin supplies the executable,
    and everything else -- including every test in this repository -- injects
    its own callable. The child sees only ``request.environment``, and it sees
    it from inside a sandbox that denies it sockets. Resolving the executable
    and the sandbox here, rather than at build time, means an unusable provider
    or an uncontainable host fails before a single blob is materialized.

    The argv is the interface the adopt decision evaluated, not a guess at a
    conventional one: ``extract`` with the required restrictions and then the
    pinned options, in the materialized copy. Everything the provider leaves
    behind is then collected and classified from its own report.
    """
    command = _resolved_executable(executable)
    sandbox = network_sandbox_command()
    if sandbox is None:
        raise ContextError(
            "local graph builds need an OS sandbox that denies the provider network access; "
            "this host offers none that could be verified"
        )

    def run(request: IndexRequest) -> IndexResult:
        _refuse_pre_existing_provider_state(request.source_root)
        # Normalized again at the point of launch, not because the pin could
        # arrive without the restrictions -- it cannot -- but because this is
        # the line that decides what the provider is actually asked to do, and
        # it should be readable here without trusting a constructor elsewhere.
        options = _extraction_options(request.pin.options)
        try:
            returncode = _run_contained(
                [*sandbox, command, _PROVIDER_EXTRACT, *options],
                environment=request.environment,
                cwd=str(request.source_root),
                timeout=EXTRACTION_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            # Raised only once the whole process group has been stopped, so the
            # caller may delete the scratch directory without racing a worker.
            raise ContextError(
                "local graph provider exceeded its time budget; no generation was published"
            ) from None
        except (OSError, subprocess.SubprocessError):
            raise ContextError("local graph provider could not be run from its pinned install") from None
        if returncode != 0:
            raise ContextError("local graph provider failed; no generation was published")
        state_directory = _provider_state_directory(request.source_root)
        result = _read_completeness(_provider_report(state_directory))
        _write_private_file(request.output_path, _pack_state(state_directory))
        return result

    return run


@dataclass(frozen=True)
class BuildManifest:
    """The immutable record bound to one published generation.

    Every field an artifact must carry under issue #913 lives here: the full
    commit and tree it was built from, the provider pin and options that built
    it, when, the tracked census it was allowed to see and that census's
    digest, the graph's own digest and byte count, and whether the provider
    considered the result complete.
    """

    generation: str
    schema: str
    commit: str
    tree: str
    provider: dict[str, Any]
    built_at: str
    tracked_files: int
    tracked_bytes: int
    census_digest: str
    graph_digest: str
    graph_bytes: int
    completeness: str
    skipped_paths: int
    indexed_files: int

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "generation": self.generation,
            "commit": self.commit,
            "tree": self.tree,
            "provider": self.provider,
            "built_at": self.built_at,
            "tracked_files": self.tracked_files,
            "tracked_bytes": self.tracked_bytes,
            "census_digest": self.census_digest,
            "graph_digest": self.graph_digest,
            "graph_bytes": self.graph_bytes,
            "completeness": self.completeness,
            "skipped_paths": self.skipped_paths,
            "indexed_files": self.indexed_files,
        }

    def shareable_summary(self) -> dict[str, Any]:
        """Metadata only: counts, digests and revision names, never content."""
        return {
            "schema": "code_mower.contextGraphBuildSummary.v1",
            "generation": self.generation,
            "commit": self.commit,
            "tree": self.tree,
            "provider_version": self.provider.get("version"),
            "built_at": self.built_at,
            "tracked_files": self.tracked_files,
            "census_digest": self.census_digest,
            "graph_digest": self.graph_digest,
            "graph_bytes": self.graph_bytes,
            "completeness": self.completeness,
        }


def load_manifest(payload: Mapping[str, Any]) -> BuildManifest:
    """Validate a manifest. Every unreadable shape is a refusal, not a default."""
    if not isinstance(payload, Mapping):
        raise ContextError("local graph manifest must be an object")
    expected = {
        "schema", "generation", "commit", "tree", "provider", "built_at",
        "tracked_files", "tracked_bytes", "census_digest", "graph_digest",
        "graph_bytes", "completeness", "skipped_paths", "indexed_files",
    }
    if set(payload) != expected:
        raise ContextError("local graph manifest fields are missing or unrecognized")
    if payload["schema"] != MANIFEST_SCHEMA:
        raise ContextError("unsupported local graph manifest schema")
    generation = payload["generation"]
    if not isinstance(generation, str) or not _GENERATION.fullmatch(generation):
        raise ContextError("local graph generation must be an opaque identifier")
    completeness = payload["completeness"]
    if completeness not in (COMPLETE, PARTIAL):
        raise ContextError("unsupported local graph completeness")
    provider = payload["provider"]
    pin = load_pin(provider) if isinstance(provider, Mapping) else None
    if pin is None:
        raise ContextError("local graph manifest must record its provider pin")
    _timestamp(payload["built_at"])
    return BuildManifest(
        generation=generation,
        schema=MANIFEST_SCHEMA,
        commit=_object_name(payload["commit"]),
        tree=_object_name(payload["tree"]),
        provider=pin.as_metadata(),
        built_at=payload["built_at"],
        tracked_files=_size(payload["tracked_files"], MAX_TRACKED_FILES),
        tracked_bytes=_size(payload["tracked_bytes"], MAX_TRACKED_BYTES),
        census_digest=_digest(payload["census_digest"]),
        graph_digest=_digest(payload["graph_digest"]),
        graph_bytes=_size(payload["graph_bytes"], MAX_ARTIFACT_BYTES),
        completeness=completeness,
        skipped_paths=_size(payload["skipped_paths"], MAX_SKIPPED_PATHS),
        indexed_files=_size(payload["indexed_files"], MAX_TRACKED_FILES),
    )


def workspace_id(repository: Path) -> str:
    """A stable private name for one checkout.

    Derived from the resolved path so two worktrees of the same repository get
    separate state and can never read each other's generations, and hashed so
    the operator's directory layout is not spelled out in a shared location.
    """
    return hashlib.sha256(str(Path(repository).resolve()).encode()).hexdigest()[:32]


def _open_private_directory(path: Path, *, create: bool) -> int:
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        handle = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise ContextError("local graph state directory is unavailable or unsafe") from None
    try:
        _private(handle, directory=True)
    except ContextError:
        os.close(handle)
        raise
    return handle


class GraphStateRoot:
    """Private, operator-owned, 0700 state for one checkout's generations.

    Layout under ``<root>/graph/<workspace>/``::

        generations/<generation>/manifest.json
        generations/<generation>/graph.bin
        current                       -- the published generation's name

    The directory is created 0700 and every read re-checks ownership and mode,
    so state that was later loosened -- by an umask change, a restore, or a
    careless ``chmod -R`` -- fails closed instead of being used.
    """

    def __init__(self, repository: Path, *, root: Path | None = None):
        self.repository = Path(repository).resolve()
        base = Path(root) if root is not None else default_context_root()
        if not base.is_absolute():
            raise ContextError("local graph state requires an absolute private directory")
        # Canonicalized once, here, and never spelled lexically again: every
        # later open, mkdir, lock, and removal travels the path that
        # ``_refuse_state_inside_a_repository`` checked. Checking a resolved
        # snapshot and then writing through the original spelling would leave a
        # symlinked ancestor free to be retargeted in between -- the check
        # passes against one directory and the writes land in another. Ordinary
        # private roots do have symlinked ancestors (macOS puts ``/tmp`` behind
        # ``/private/tmp``), so they are resolved rather than refused.
        self.base = Path(os.path.realpath(base))
        self.workspace = workspace_id(self.repository)
        self.path = self.base / "graph" / self.workspace

    @property
    def generations_path(self) -> Path:
        return self.path / "generations"

    @property
    def lock_path(self) -> Path:
        """Beside the state directory, deliberately not inside it.

        ``remove`` deletes the whole tree while holding this lock. A lock file
        inside that tree would be unlinked mid-removal, and the next builder
        would create a *new* inode and acquire a lock nobody else is holding --
        two processes, two files, no mutual exclusion. Keeping it one level up
        means the inode a holder waits on is the inode the remover holds.
        """
        return self.path.parent / f"{self.workspace}.lock"

    @property
    def _chain(self) -> tuple[Path, ...]:
        """Every directory this class owns, outermost first.

        Spelled out because ``mkdir(mode=0o700, parents=True)`` applies its mode
        to the leaf only: intermediate directories would be created with the
        process umask and end up group- or world-readable.
        """
        return (self.path.parent.parent, self.path.parent, self.path, self.generations_path)

    def verify_private(self, *, create: bool = False) -> None:
        """Re-check ownership and mode on every directory this class owns.

        Called on every read, not only at creation: state that was loosened
        after the fact -- by a umask change, a restore, or a careless recursive
        chmod -- must fail closed rather than be trusted because it was private
        when it was written.
        """
        for directory in self._chain:
            if create or directory.exists():
                os.close(_open_private_directory(directory, create=create))

    def ensure(self) -> None:
        """Create the private tree, refusing to place state inside a repository."""
        self._refuse_state_inside_a_repository()
        self.verify_private(create=True)

    def _refuse_state_inside_a_repository(self) -> None:
        """No generation may be written inside a repository, by any spelling.

        A lexical walk alone reads ``--state-dir /outside/link/state`` as being
        outside every repository even when ``/outside/link`` points at
        ``/repo/subdir``, and the ``O_NOFOLLOW`` opens elsewhere would not catch
        it either: they protect the final component of each directory this class
        owns, not an ancestor somebody else created. The path walked here is the
        canonical one built in ``__init__``, which is also the one every write
        goes through, so this check cannot be satisfied by one directory and
        then applied to another.
        """
        if any((parent / ".git").exists() for parent in (self.path, *self.path.parents)):
            raise ContextError("local graph state must stay outside Git repositories")

    def _ensure_lock_directory(self) -> None:
        """Create only what the lock file needs, not the generations tree.

        ``remove`` takes the same lock, and a removal that first created the
        state it was asked to delete would report success for a tree it made
        itself.
        """
        self._refuse_state_inside_a_repository()
        for directory in self._chain[:2]:
            os.close(_open_private_directory(directory, create=True))

    def lock(self):
        """Serialize builds *and removals* for one checkout.

        Concurrent builds would race publish; a removal running beside a build
        would delete the sources, output, and generations out from under it.
        Both take this lock, so the whole set of lifecycle operations that
        mutate state for one checkout is serialized rather than just the pair
        that was obviously racy.
        """
        self._ensure_lock_directory()
        handle = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        stream = os.fdopen(handle, "a+", encoding="utf-8")
        try:
            _private(stream.fileno())
            return _BuildLock(stream)
        except ContextError:
            stream.close()
            raise

    def current_generation(self) -> str | None:
        pointer = self.path / CURRENT_NAME
        try:
            handle = os.open(pointer, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        except OSError:
            raise ContextError("local graph state directory is unavailable or unsafe") from None
        try:
            _private(handle)
            with os.fdopen(handle, "rb", closefd=False) as stream:
                name = stream.read(64).decode("utf-8", "replace").strip()
        finally:
            os.close(handle)
        if not _GENERATION.fullmatch(name):
            raise ContextError("local graph generation pointer is corrupt; rebuild the graph")
        return name

    def generation_names(self) -> list[str]:
        if not self.path.exists():
            return []
        self.verify_private()
        try:
            names = os.listdir(self.generations_path)
        except FileNotFoundError:
            return []
        except OSError:
            raise ContextError("local graph state directory is unavailable or unsafe") from None
        return sorted(name for name in names if _GENERATION.fullmatch(name))

    def read_manifest(self, generation: str) -> BuildManifest:
        """Read and validate one generation's manifest, permissions included."""
        if not _GENERATION.fullmatch(generation):
            raise ContextError("local graph generation must be an opaque identifier")
        directory = self.generations_path / generation
        os.close(_open_private_directory(directory, create=False))
        try:
            handle = os.open(directory / MANIFEST_NAME, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            raise ContextError("local graph generation is missing its manifest") from None
        try:
            _private(handle)
            with os.fdopen(handle, "rb", closefd=False) as stream:
                raw = stream.read(MAX_MANIFEST_BYTES + 1)
        finally:
            os.close(handle)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ContextError("local graph manifest exceeds its bound")
        try:
            payload = json.loads(raw)
        except ValueError:
            raise ContextError("local graph manifest is corrupt; rebuild the graph") from None
        manifest = load_manifest(payload)
        if manifest.generation != generation:
            raise ContextError("local graph manifest does not match its generation")
        return manifest

    def artifact_path(self, generation: str) -> Path:
        if not _GENERATION.fullmatch(generation):
            raise ContextError("local graph generation must be an opaque identifier")
        return self.generations_path / generation / ARTIFACT_NAME

    def publish(self, manifest: BuildManifest, artifact: bytes) -> BuildManifest:
        """Assemble a generation under a staging name, then rename it into place.

        Both steps are atomic renames, in an order a reader can survive: the
        generation directory becomes visible whole, and only then does
        ``current`` start naming it. A crash between the two leaves an
        unreferenced generation, which ``prune`` removes; it never leaves a
        pointer to a directory that does not exist.

        Nothing is written until the serialized manifest has been read back
        through ``load_manifest`` and measured against the bound a reader
        applies. A manifest this process can write but no reader can load would
        otherwise publish, move ``current`` onto it, prune the previous usable
        generation, and read back ``invalid`` on the next status: a build that
        reports success while destroying the only generation that worked. The
        check belongs here, at the one boundary every generation crosses,
        rather than at each of the places that fill a single field in.
        """
        serialized = json.dumps(
            manifest.to_json(), allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode()
        if len(serialized) > MAX_MANIFEST_BYTES:
            raise ContextError("local graph manifest exceeds its bound; no generation was published")
        if load_manifest(json.loads(serialized)).generation != manifest.generation:
            raise ContextError("local graph manifest does not match its generation")
        self.ensure()
        staging = self.generations_path / ("." + uuid.uuid4().hex + ".staging")
        staging.mkdir(mode=0o700)
        try:
            _write_private_file(staging / ARTIFACT_NAME, artifact)
            _write_private_file(staging / MANIFEST_NAME, serialized)
            _fsync_directory(staging)
            final = self.generations_path / manifest.generation
            os.rename(staging, final)
            _fsync_directory(self.generations_path)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        pointer = self.path / CURRENT_NAME
        temporary = self.path / ("." + uuid.uuid4().hex + ".tmp")
        try:
            _write_private_file(temporary, manifest.generation.encode() + b"\n")
            os.replace(temporary, pointer)
            _fsync_directory(self.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return manifest

    def prune(self, *, keep: str | None) -> list[str]:
        """Remove every generation but ``keep``, including crashed stagings."""
        removed = []
        try:
            names = os.listdir(self.generations_path)
        except FileNotFoundError:
            return removed
        for name in sorted(names):
            if name == keep:
                continue
            shutil.rmtree(self.generations_path / name, ignore_errors=True)
            if _GENERATION.fullmatch(name):
                removed.append(name)
        _fsync_directory(self.generations_path)
        return removed

    def remove_all(self) -> bool:
        """Delete this checkout's graph state, serialized against builders.

        Taken under the build lock: without it, a removal can delete a running
        build's materialized sources, its output, and the generations
        directory, after which the builder either fails or recreates state
        that ``remove`` has already reported as gone.

        The lock file itself survives, by design. It is an empty 0600 file
        outside the deleted tree that carries no indexed content, and it is the
        stable inode the next builder and the next remover agree on.
        """
        if not self.path.exists() and not self.lock_path.exists():
            # Nothing exists and no builder can be running: a builder creates
            # the lock file before it creates any state, so an absent lock file
            # means there is nothing to serialize against. Checked first so a
            # removal on a fresh install does not create a private tree merely
            # to report that it was empty.
            return False
        with self.lock():
            if not self.path.exists():
                return False
            # Refuse to delete a tree that is not ours; a loosened or foreign
            # directory is reported, not recursively removed.
            self.verify_private()
            shutil.rmtree(self.path)
            return True


class _BuildLock:
    def __init__(self, stream):
        self._stream = stream
        self._guard = None

    def __enter__(self):
        self._guard = exclusive_handle_lock(self._stream, timeout_seconds=35)
        try:
            self._guard.__enter__()
        except FileLockError:
            raise ContextError("a local graph build is already running for this checkout") from None
        return self

    def __exit__(self, *exception):
        try:
            if self._guard is not None:
                self._guard.__exit__(*exception)
        finally:
            self._stream.close()
        return False


def _write_private_file(path: Path, payload: bytes) -> None:
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(handle, "wb", closefd=False) as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(handle)
    os.close(handle)


def _fsync_directory(path: Path) -> None:
    handle = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


@dataclass(frozen=True)
class GenerationStatus:
    """A shareable verdict about the published generation, if any."""

    state: str
    generation: str | None = None
    manifest: BuildManifest | None = None
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.state == "current"

    def shareable_summary(self) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "schema": "code_mower.contextGraphStatus.v1",
            "state": self.state,
            "usable": self.usable,
        }
        if self.detail:
            summary["detail"] = self.detail
        if self.manifest is not None:
            summary["build"] = self.manifest.shareable_summary()
        elif self.generation is not None:
            summary["generation"] = self.generation
        return summary


def build_graph(
    repository: Path,
    *,
    pin: GraphifyPin,
    indexer: Callable[[IndexRequest], IndexResult],
    root: Path | None = None,
    revision: str = "HEAD",
    now: datetime | None = None,
    keep_previous: bool = False,
) -> BuildManifest:
    """Materialize one commit, index the copy, and publish a new generation.

    This is the whole lifecycle in one call, and it is the only way a
    generation is created. ``refresh`` is the same operation: an explicit
    rebuild that publishes a new immutable generation rather than mutating the
    one in place, which is why nothing here ever writes into an existing
    generation directory.
    """
    repository = Path(repository).resolve()
    state = GraphStateRoot(repository, root=root)
    commit, tree = resolve_revision(repository, revision)
    census = read_tracked_census(repository, commit)
    with state.lock():
        # The lock only creates what the lock file needs, so that ``remove``
        # can take it without materializing the tree it was asked to delete.
        # A build does want the whole private tree, created 0700 at every
        # level before anything is written into it.
        state.ensure()
        build_root = state.path / ("." + uuid.uuid4().hex + ".build")
        build_root.mkdir(mode=0o700, parents=True)
        try:
            source_root = build_root / "source"
            home = build_root / "home"
            temporary = build_root / "tmp"
            for directory in (home, temporary):
                directory.mkdir(mode=0o700)
            materialize_tracked_files(repository, census, source_root)
            output_path = build_root / ARTIFACT_NAME
            result = indexer(
                IndexRequest(
                    source_root=source_root,
                    output_path=output_path,
                    environment=scrubbed_environment(home=home, temporary=temporary),
                    pin=pin,
                    commit=commit,
                    tree=tree,
                )
            )
            if not isinstance(result, IndexResult) or result.completeness not in (COMPLETE, PARTIAL):
                raise ContextError("local graph provider returned an unsupported result")
            if not output_path.is_file():
                raise ContextError("local graph provider produced no artifact")
            # Read to the budget and one byte past it, rather than trusting a
            # size taken before the read: the artifact is provider output, and
            # the budget has to bound what this process allocates for it.
            with output_path.open("rb") as stream:
                artifact = stream.read(MAX_ARTIFACT_BYTES + 1)
            if len(artifact) > MAX_ARTIFACT_BYTES:
                raise ContextError("local graph artifact exceeds its budget; no generation was published")
            manifest = BuildManifest(
                generation=uuid.uuid4().hex,
                schema=MANIFEST_SCHEMA,
                commit=commit,
                tree=tree,
                provider=pin.as_metadata(),
                built_at=(now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),
                tracked_files=census.file_count,
                tracked_bytes=census.total_bytes,
                census_digest=census.digest,
                graph_digest=hashlib.sha256(artifact).hexdigest(),
                graph_bytes=len(artifact),
                completeness=result.completeness,
                skipped_paths=len(census.skipped),
                indexed_files=min(_size(result.indexed_files, MAX_TRACKED_FILES), census.file_count),
            )
            published = state.publish(manifest, artifact)
            if not keep_previous:
                # Inside the lock, with publication. Pruning after the lock is
                # released would let a second builder publish first and then
                # have its generation -- or its staging directory -- deleted by
                # this one, leaving ``current`` naming a directory that is gone.
                state.prune(keep=published.generation)
        finally:
            shutil.rmtree(build_root, ignore_errors=True)
    return published


#: How many times a reader will re-read a generation that was replaced under
#: it. Bounded because the only thing that moves the pointer is a publish, and
#: a refresh loop fast enough to outrun three reads is not a state worth
#: blocking on.
_STATUS_ATTEMPTS = 3


def graph_status(
    repository: Path,
    *,
    root: Path | None = None,
    revision: str = "HEAD",
    require_complete: bool = True,
) -> GenerationStatus:
    """Report whether the published generation may be used, and why not if not.

    Every failure mode issue #913 names resolves here to a non-``current``
    state, and every non-``current`` state is unusable. Nothing falls back to a
    previous generation: a consumer that cannot have the revision it asked for
    is told so rather than handed an older answer that looks fresh.

    Readers take no lock, so a refresh can publish and prune between the moment
    this reads the ``current`` pointer and the moment it validates what that
    pointer named. The generation is then genuinely gone, and reporting it
    ``invalid`` or ``corrupt`` would describe a directory a healthy build had
    just superseded rather than anything wrong with the graph. A failing read
    is therefore confirmed against the pointer before it is returned, and a
    pointer that moved is read again.
    """
    state = GraphStateRoot(repository, root=root)
    for attempt in range(_STATUS_ATTEMPTS):
        status = _status_once(
            state, repository, revision=revision, require_complete=require_complete
        )
        if status.usable or status.generation is None or attempt == _STATUS_ATTEMPTS - 1:
            return status
        try:
            if state.current_generation() == status.generation:
                # The pointer still names what was just validated, so the
                # verdict is about the operator's graph, not a race.
                return status
        except ContextError:
            return status
    return status


def _status_once(
    state: GraphStateRoot,
    repository: Path,
    *,
    revision: str,
    require_complete: bool,
) -> GenerationStatus:
    """One validation pass over whichever generation ``current`` names now."""
    try:
        if not state.path.exists():
            return GenerationStatus(state="absent", detail="no local graph has been built for this checkout")
        state.verify_private()
        generation = state.current_generation()
    except ContextError as error:
        return GenerationStatus(state="invalid", detail=str(error))
    if generation is None:
        return GenerationStatus(state="absent", detail="no local graph generation is published")
    try:
        manifest = state.read_manifest(generation)
    except ContextError as error:
        return GenerationStatus(state="invalid", generation=generation, detail=str(error))
    try:
        artifact_path = state.artifact_path(generation)
        handle = os.open(artifact_path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            _private(handle)
            info = os.fstat(handle)
            if info.st_size > MAX_ARTIFACT_BYTES:
                # Checked before the manifest comparison and before any read:
                # an artifact that grew past its budget is refused without
                # being hashed, however plausible its manifest looks.
                return GenerationStatus(state="oversized", generation=generation, manifest=manifest,
                                        detail="local graph artifact exceeds its budget")
            if info.st_size != manifest.graph_bytes:
                return GenerationStatus(state="corrupt", generation=generation, manifest=manifest,
                                        detail="local graph artifact size does not match its manifest")
            digest = hashlib.sha256()
            with os.fdopen(handle, "rb", closefd=False) as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        finally:
            os.close(handle)
    except ContextError as error:
        return GenerationStatus(state="invalid", generation=generation, manifest=manifest, detail=str(error))
    except OSError:
        return GenerationStatus(state="corrupt", generation=generation, manifest=manifest,
                                detail="local graph artifact is unreadable")
    if digest.hexdigest() != manifest.graph_digest:
        return GenerationStatus(state="corrupt", generation=generation, manifest=manifest,
                                detail="local graph artifact does not match its recorded digest")
    try:
        commit, tree = resolve_revision(Path(repository), revision)
    except ContextError as error:
        return GenerationStatus(state="invalid", generation=generation, manifest=manifest, detail=str(error))
    if (manifest.commit, manifest.tree) != (commit, tree):
        return GenerationStatus(state="stale", generation=generation, manifest=manifest,
                                detail="local graph was built from a different revision; refresh it")
    if require_complete and manifest.completeness != COMPLETE:
        return GenerationStatus(state="partial", generation=generation, manifest=manifest,
                                detail="local graph build was incomplete; refresh it")
    return GenerationStatus(state="current", generation=generation, manifest=manifest)


def remove_graph(repository: Path, *, root: Path | None = None) -> bool:
    """Delete this checkout's graph state. Returns whether anything was removed."""
    return GraphStateRoot(repository, root=root).remove_all()


def doctor_report(
    repository: Path,
    *,
    pin: GraphifyPin | None,
    root: Path | None = None,
    revision: str = "HEAD",
) -> dict[str, Any]:
    """Posture checks for the local graph: metadata only, never content.

    Reports ``skip`` rather than ``fail`` when no graph is configured or built.
    The lifecycle is optional, and an operator who never opted in has nothing
    wrong with their installation.
    """
    checks: list[dict[str, Any]] = []

    def record(name: str, status: str, message: str, **extra: Any) -> None:
        checks.append({"check": name, "status": status, "message": message, **extra})

    if pin is None:
        record("context-graph-pin", "skip", "no local graph provider is pinned; the lifecycle is optional")
        record("context-graph-isolation", "skip", "no provider is pinned, so nothing would be launched")
    else:
        record("context-graph-pin", "pass", "local graph provider is pinned to one exact release",
               requirement=pin.requirement, wheel_sha256=pin.wheel_sha256)
        # Named while it is still a posture question. Discovering that this
        # host cannot contain a provider is worth knowing before a build
        # refuses, and the check reports the mechanism rather than its
        # arguments, which would be noise.
        sandbox = network_sandbox_command()
        if sandbox is None:
            record("context-graph-isolation", "fail",
                   "no OS sandbox on this host was observed denying a child process the network; "
                   "builds will refuse")
        else:
            record("context-graph-isolation", "pass",
                   "the provider would run inside a network-denying OS sandbox",
                   mechanism=os.path.basename(sandbox[0]))

    state = GraphStateRoot(repository, root=root)
    if not state.path.exists():
        record("context-graph-state", "skip", "no private local graph state exists for this checkout")
    else:
        try:
            state.verify_private()
            record("context-graph-state", "pass", "local graph state is private and operator-owned")
        except ContextError as error:
            record("context-graph-state", "fail", str(error))

    status = graph_status(repository, root=root, revision=revision)
    if status.state == "absent":
        record("context-graph-generation", "skip", status.detail or "no local graph generation is published")
    elif status.usable:
        build = {key: value for key, value in status.shareable_summary().get("build", {}).items()
                 if key != "schema"}
        record("context-graph-generation", "pass",
               "the published generation binds the current revision", **build)
    else:
        record("context-graph-generation", "fail", status.detail or f"local graph is {status.state}",
               state=status.state)

    ordering = {"fail": 0, "warn": 1, "pass": 2, "skip": 3}
    overall = min((check["status"] for check in checks), key=lambda value: ordering[value])
    return {
        "schema": "code_mower.contextGraphDoctor.v1",
        "status": "fail" if any(check["status"] == "fail" for check in checks) else overall,
        "checks": checks,
    }


def render_status_text(status: GenerationStatus) -> str:
    """A short operator-facing summary. Metadata only; no indexed content."""
    lines = [f"Local graph: {status.state}"]
    if status.detail:
        lines.append(f"  {status.detail}")
    manifest = status.manifest
    if manifest is not None:
        lines.extend(
            [
                f"  generation: {manifest.generation}",
                f"  commit:     {manifest.commit}",
                f"  tree:       {manifest.tree}",
                f"  provider:   {manifest.provider.get('distribution')}=={manifest.provider.get('version')}",
                f"  built at:   {manifest.built_at}",
                f"  tracked:    {manifest.tracked_files} files / {manifest.tracked_bytes} bytes",
                f"  census:     {manifest.census_digest}",
                f"  graph:      {manifest.graph_digest} ({manifest.graph_bytes} bytes)",
                f"  complete:   {manifest.completeness}",
            ]
        )
    return "\n".join(lines) + "\n"


def iter_generations(repository: Path, *, root: Path | None = None) -> Iterator[str]:
    """Published and unreferenced generation names, for operator inspection."""
    yield from GraphStateRoot(repository, root=root).generation_names()


__all__: Sequence[str] = (
    "ARTIFACT_NAME",
    "BuildManifest",
    "COMPLETE",
    "EXTRACTION_TIMEOUT_SECONDS",
    "GenerationStatus",
    "GraphStateRoot",
    "GraphifyPin",
    "IndexRequest",
    "IndexResult",
    "MANIFEST_SCHEMA",
    "MAX_ARTIFACT_BYTES",
    "MAX_ARTIFACT_ENTRIES",
    "MAX_EXTRACT_OPTIONS",
    "MAX_TRACKED_FILES",
    "PARTIAL",
    "TrackedCensus",
    "TrackedEntry",
    "build_graph",
    "doctor_report",
    "git_environment",
    "graph_status",
    "iter_generations",
    "load_manifest",
    "load_pin",
    "materialize_tracked_files",
    "read_tracked_census",
    "refuse_lazy_object_fetch",
    "remove_graph",
    "render_status_text",
    "resolve_revision",
    "scrubbed_environment",
    "subprocess_indexer",
    "workspace_id",
)
