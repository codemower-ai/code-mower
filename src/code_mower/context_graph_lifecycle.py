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
* **Check the install against the pin.** A manifest records the pin as the
  provenance of every byte in a generation, so the executable a build is about
  to run is checked against it first: which distribution installed it, at which
  version, and whether it is still the file that installer wrote. The install
  is read for this rather than the provider asked, because asking would mean
  running the executable whose identity is in question.
* **Scrub the environment.** The indexer runs with an allowlisted environment,
  so an ambient token cannot leak into a provider process.
* **Deny the network and the host filesystem in the kernel, not by request.**
  Emptying proxy variables only redirects a client that chooses to honour them,
  and a working directory is not a boundary. The provider is launched inside an
  OS sandbox whose filesystem view is the materialized copy, the build's own
  scratch directories, and a read-only runtime -- the operator's home, their
  other checkouts, and every ignored ``.env`` beside them are absent from it,
  not merely unreadable. The mechanism is accepted only after a probe child has
  been observed failing at *both*: failing to reach a socket this process is
  really listening on, observed at the listener rather than believed from the
  child's errno, and failing to read a secret file planted outside its
  exposure. A host that offers no such mechanism gets a refused build, not an
  unconfined provider.

Nothing here installs, imports, or requires a graph package. The indexer is an
injected callable, so the whole lifecycle is provable offline; the bundled
``subprocess_indexer`` builds the argv and the scrubbed environment for a
pinned provider without this module depending on it.
"""

from __future__ import annotations

import base64
import contextlib
import csv
import hashlib
import json
import os
import re
import io
import secrets
import shlex
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from .context_contract import ContextError, _identifier, _text, _timestamp
from .context_graph import _EXCLUDED_ROOTS
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

#: Where the pinned provider actually writes. Read off ``graphify/paths.py`` at
#: the evaluated pin rather than assumed: ``GRAPHIFY_OUT`` defaults to the
#: literal ``graphify-out`` and every output path is built from it, so an
#: unmodified ``extract`` run in the materialized copy leaves
#: ``graphify-out/graph.json`` and ``graphify-out/manifest.json`` beneath the
#: scan target. The environment override that constant reads is deliberately
#: *not* honoured here: the child is given a fixed environment, and an output
#: root this adapter did not choose is a root it cannot bound to the copy.
_PROVIDER_OUTPUT_DIRECTORY = "graphify-out"

#: The document the ``--no-cluster`` branch dumps, and the provider's own record
#: of which inputs it processed. Exactly these two names are read; a run that
#: leaves something else has not produced the evidence this adapter classifies.
_PROVIDER_GRAPH_NAME = "graph.json"
_PROVIDER_MANIFEST_NAME = "manifest.json"

#: Where the provider keeps its own index state or output. All three names are
#: on the excluded-roots list in ``context_graph``, and a repository is free to
#: track any of them -- a committed ``.graph/`` is somebody else's graph, a
#: committed ``graphify-out/`` is an earlier build's published output, and
#: ``.graphify/`` is an incremental cache. None may be materialized: the
#: provider would then resume from a cache built over content this build never
#: saw, and the adapter would collect tracked repository bytes as if the
#: provider had just produced them, binding stale contents to a fresh commit.
#: Matched at any depth and case-folded, for the same reasons ``.git`` is.
_PROVIDER_STATE_DIRECTORIES = (_PROVIDER_OUTPUT_DIRECTORY, ".graphify", ".graph")
_PROVIDER_STATE_ROOTS = frozenset(name.casefold() for name in _PROVIDER_STATE_DIRECTORIES)

#: The evidence contract's excluded roots, bound rather than copied. One module
#: decides a citation into private state is out of scope, this one decides
#: those bytes never reach the indexer at all; they are the same policy read
#: from two ends, and a name added to one must not have to be remembered in the
#: other. The set is a superset of the provider roots above: it also carries
#: ``.git``, whose contents are the history rather than the revision, and
#: ``.code-mower``, this tool's own state. A repository is free to track
#: either, and a build over a tracked ``.code-mower/`` would hand the provider
#: exactly the packets and evidence that ``context_graph`` then refuses to let
#: a packet cite.
_PRIVATE_STATE_ROOTS = _EXCLUDED_ROOTS

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
#: neither denies anything. The boundary is ``containment_prefix``.
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

#: Isolation mechanisms, most specific first, each named by an absolute path.
#:
#: Absolute, deliberately: a launcher looked up on an inherited ``PATH`` can be
#: shadowed by a program that answers the probe without confining anything, and
#: the probe's verdict is only as good as its knowledge of what it ran. The
#: paths are the system locations these tools install into, and each is checked
#: for trusted ownership and unwritable ancestry before it is run.
#:
#: ``unshare --net`` used to be here and is gone. It denies the network and
#: nothing else: the child keeps the host's whole filesystem, which is not the
#: boundary this module claims. A host with no mechanism that confines *both*
#: gets a refused build.
_SANDBOX_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("sandbox-exec", "/usr/bin/sandbox-exec"),
    ("bwrap", "/usr/bin/bwrap"),
    ("bwrap", "/usr/local/bin/bwrap"),
)

#: Read-only host paths a runtime needs to start at all: the loader, the C
#: library, the system interpreters. Everything outside this list and the
#: exposure a build asks for is not in the child's filesystem view -- not
#: unreadable by permission, absent.
#:
#: Named one runtime directory at a time rather than one top-level directory at
#: a time. This list used to say ``/usr``, ``/etc`` and ``/Library``, which is a
#: far larger claim than "what a runtime needs to start": ``/usr`` carries
#: ``/usr/local`` -- Homebrew's whole prefix, its ``etc`` and ``var`` included --
#: and ``/usr/src``, either of which can hold a checkout; ``/etc`` carries
#: whatever service credentials the host's packages left world-readable; and
#: ``/Library`` carries ``Keychains``, ``Preferences``, ``Application Support``
#: and the rest of a Mac's machine-wide operator data. Every one of those was
#: added to every build unconditionally, so the refusals that guard an exposure
#: root never saw them, and a checkout under one of them stayed readable beside
#: the materialized copy that exists to replace it.
#:
#: The narrowing is deliberately fail-closed: a host whose runtime needs
#: something not named here refuses builds (the probe cannot start a child, so
#: no mechanism is verified) instead of running a provider with more exposed
#: than this list admits to.
_SYSTEM_READ_PATHS: tuple[str, ...] = (
    # The dynamic loader and the C library, in every spelling a distribution
    # uses. ``/lib64`` and friends are how an ELF binary names its program
    # interpreter even where they are links into ``/usr``.
    "/lib",
    "/lib64",
    "/lib32",
    "/usr/lib",
    "/usr/lib64",
    "/usr/lib32",
    "/usr/libexec",
    # System executables a runtime execs or reads: the system interpreters
    # themselves live here.
    "/bin",
    "/sbin",
    "/usr/bin",
    "/usr/sbin",
    # Architecture-independent runtime data: locales, time zones, ICU. Package
    # data rather than operator data, and the runtime reads it during start-up.
    "/usr/share",
    # Apple's signed system volume, which no operator writes and no checkout
    # can live on, plus the dyld cache's and time zone database's own state.
    "/System",
    "/private/var/db/dyld",
    "/private/var/db/timezone",
    # The two places on a Mac that hold a *runtime* rather than operator data:
    # a python.org interpreter installs itself into ``/Library/Frameworks``, and
    # Apple's command line tools keep their interpreter and its shared
    # libraries inside their own bundle. Nothing else under ``/Library`` is a
    # runtime dependency, and the rest of it is exactly the machine-wide data
    # this boundary exists to keep away from a provider.
    "/Library/Frameworks",
    "/Library/Developer/CommandLineTools/usr/lib",
    "/Library/Developer/CommandLineTools/Library/Frameworks",
    # ``/etc``, entry by entry. The loader's cache and configuration, the time
    # zone, the account databases a runtime resolves a home directory through,
    # and OpenSSL's configuration file -- which its own providers read on
    # initialization. Not ``/etc/ssl/private``, not a service's credentials,
    # not whatever else a host keeps here.
    "/etc/ld.so.cache",
    "/etc/ld.so.conf",
    "/etc/ld.so.conf.d",
    "/etc/alternatives",
    "/etc/localtime",
    "/etc/timezone",
    "/etc/passwd",
    "/etc/group",
    "/etc/nsswitch.conf",
    "/etc/os-release",
    "/etc/ssl/openssl.cnf",
)

#: The probe reports two facts about one run, as a bitmask offset from a base
#: no shell error code lands on: whether the child could read a secret file
#: planted outside its exposure, and whether it could open a socket to a port
#: this process is really listening on.
#:
#: The network verdict is taken at the *listener*, not from the child's errno.
#: Classifying by errno cannot work: a network namespace brings its own
#: loopback up, so a contained child gets ``ECONNREFUSED`` from an empty
#: namespace while an unconfined child gets ``ECONNREFUSED`` from an unused host
#: port. Indistinguishable at the child; obvious at the listener, which either
#: accepted a connection or did not.
#:
#: An exit code alone is not evidence that the child ran: a launcher that exits
#: with the contained code without executing anything would be accepted as a
#: boundary while confining nothing. So the child first prints a value only
#: running it can produce -- the digest of a nonce generated for this run -- and
#: a run with no such evidence is unusable whatever its exit code. Echoing the
#: argv is not enough: the digest is computed by the child and the nonce is
#: fresh, so neither a launcher that parrots its arguments nor one that replays
#: an earlier probe can produce it.
_PROBE_BASE = 40
_PROBE_READ_SECRET = 1
_PROBE_REACHED_LISTENER = 2
_CONTAINMENT_PROBE = """
import hashlib
import socket
import sys

print(hashlib.sha256(sys.argv[3].encode()).hexdigest(), flush=True)
seen = 0
try:
    with open(sys.argv[2], "rb") as secret:
        secret.read(1)
except OSError:
    pass
else:
    seen |= 1
try:
    reached = socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=5)
except OSError:
    pass
else:
    reached.close()
    seen |= 2
sys.exit(40 + seen)
"""

#: How a probe run classifies: the child was outside the boundary in at least
#: one respect, the child was inside it in both, or nothing usable happened.
#: A mechanism that denies only one of the two is ``_UNUSABLE``, not a
#: boundary -- half a boundary is what this finding was about.
_REACHED = "reached"
_CONTAINED = "contained"
_UNUSABLE = "unusable"


@dataclass(frozen=True)
class Containment:
    """One verified isolation mechanism on this host.

    The argv is built per build rather than cached, because the boundary is a
    function of what that build is allowed to expose. What is cached is the
    finding that this mechanism, at this path, was observed confining a child.
    """

    name: str
    launcher: str


_containment: Containment | None = None
_containment_probed = False


def _trusted_launcher(path: str) -> str | None:
    """A launcher only a trusted account could have replaced, or ``None``.

    The file and every ancestor directory: an executable that is itself
    root-owned but sits in a directory somebody else may write can be swapped
    for one that reports containment it never established. A symlink anywhere
    in the chain is refused rather than followed -- what it names now is not
    what it will name later, and this decision is cached for the process.
    """
    trusted = {0, os.geteuid()}
    for current in (Path(path), *Path(path).parents):
        try:
            entry = os.lstat(current)
        except OSError:
            return None
        if entry.st_uid not in trusted or entry.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return None
        if stat.S_ISLNK(entry.st_mode):
            return None
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK):
        return None
    return path


def _existing(paths: Iterable[Path | str], *, follow: bool = True) -> tuple[str, ...]:
    """De-duplicated existing paths, in the order they were given.

    ``follow`` is which spelling the mechanism decides on. A seatbelt
    ``subpath`` matches the kernel's resolved path -- macOS puts ``/tmp`` and
    ``/var`` behind links into its ``private`` directory -- so an unresolved
    exposure there would name a path the sandbox never sees.

    A bind mount is the other way round. The destination is a literal path in
    an otherwise empty root, and Linux's ``/lib64 -> usr/lib64`` compatibility
    links are how every ELF binary names its program interpreter. A root with
    ``/usr/lib64`` and no ``/lib64`` cannot exec anything at all, and says so
    as an ``execvp`` ENOENT naming the binary rather than the loader it could
    not find. So a mechanism that does not follow gets both spellings.
    """
    seen: dict[str, None] = {}
    for path in paths:
        literal = os.fspath(path)
        try:
            real = os.path.realpath(literal)
        except OSError:  # pragma: no cover - realpath does not raise on absent paths
            continue
        for candidate in (real,) if follow else (literal, real):
            if os.path.exists(candidate):
                seen.setdefault(candidate, None)
    return tuple(seen)


def _seatbelt_literal(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


#: Apple's own bootstrap rules for the dynamic linker. On a ``(deny default)``
#: profile a modern dyld cannot reach the shared cache, and it does not fail as
#: a denied open: it aborts inside ``dyld4::CacheFinder`` before it owns stderr,
#: so the child arrives as ``SIGABRT`` with no stdout and no stderr and the
#: probe can only report that the launcher started nothing. The rules it needs
#: are the cryptex cache paths plus the narrow ``syscall-unix``,
#: ``system-fcntl`` and ``system-mac-syscall`` operations -- exactly what this
#: profile ships. Importing it grants no general file access; the alternative
#: that also starts dyld, an unfiltered ``(allow file-read-data)``, would
#: destroy the filesystem boundary this profile exists to draw.
_DYLD_SUPPORT_PROFILE = "/System/Library/Sandbox/Profiles/dyld-support.sb"


def _seatbelt_prefix(launcher: str, *, writable: Sequence[str], readable: Sequence[str]) -> tuple[str, ...]:
    """A ``sandbox-exec`` profile that denies by default and then names the exposure.

    ``(allow default)(deny network*)`` -- what this module used to pass -- denies
    sockets and leaves the host filesystem wide open. The order here is the
    other way round: nothing is permitted, and then the runtime is made readable
    and the build's own directories writable.
    """
    rules = ["(version 1)"]
    if os.path.exists(_DYLD_SUPPORT_PROFILE):
        # Directly after ``(version 1)`` and before everything else: Apple's
        # profile declares version 3, so an import ahead of this file's own
        # version declaration will not compile. ``(deny default)`` follows it
        # and remains the default posture -- a default is not a rule that
        # overrides the import, which is why this placement is the one
        # observed to both start dyld and keep the boundary. A host old enough
        # not to ship the profile keeps the previous rules rather than failing
        # to compile an import of a file that is not there; if its dyld needs
        # them anyway, that host refuses the build instead of running it
        # unconfined.
        rules.append('(import "dyld-support.sb")')
    rules += [
        "(deny default)",
        "(deny network*)",
        "(allow process-fork)",
        "(allow signal)",
        "(allow sysctl-read)",
        "(allow mach-lookup)",
        "(allow ipc-posix-shm)",
        "(allow file-read-metadata)",
        # Read *and* write on the same four devices. Granting write without
        # read is an asymmetry nothing wants: a runtime opens ``/dev/null``
        # read-write to detach a stream, and reads ``/dev/urandom`` to seed
        # itself, so a profile that denies the read denies the process its
        # start rather than denying it anything an operator cares about.
        "(allow file-read-data file-write-data (literal \"/dev/null\")"
        " (literal \"/dev/zero\") (literal \"/dev/random\") (literal \"/dev/urandom\"))",
    ]
    if readable:
        # ``file-map-executable`` alongside the read: being allowed to *read* a
        # dynamic library is not being allowed to map its pages executable, and
        # on a ``(deny default)`` profile the second denial is what actually
        # stops a process. It stops it as a ``SIGABRT`` from inside dyld before
        # the runtime owns stderr, so the failure arrives as a signalled child
        # with no output at all rather than as anything naming a path -- which
        # is how this profile read as "the launcher cannot start a child" on
        # every macOS host while being a one-rule omission.
        subpaths = " ".join(f"(subpath {_seatbelt_literal(path)})" for path in readable)
        rules.append(f"(allow file-read* file-map-executable process-exec* {subpaths})")
    if writable:
        subpaths = " ".join(f"(subpath {_seatbelt_literal(path)})" for path in writable)
        rules.append(f"(allow file-read* file-write* {subpaths})")
    return (launcher, "-p", "\n".join(rules))


def _bubblewrap_prefix(launcher: str, *, writable: Sequence[str], readable: Sequence[str]) -> tuple[str, ...]:
    """A ``bwrap`` mount namespace containing only the exposure.

    ``--dev-bind / /`` -- what this module used to pass -- hands the child the
    host's entire filesystem, read *and* write, and isolates the network alone.
    The new root is empty: the runtime is bound read-only, the build's own
    directories are bound writable, and ``/tmp`` is a tmpfs, so a path nobody
    named does not exist for this child.
    """
    argv = [
        launcher,
        "--unshare-net",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-pid",
        "--unshare-cgroup-try",
        "--new-session",
        "--die-with-parent",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
    ]
    for path in readable:
        argv += ["--ro-bind-try", path, path]
    # After the read-only runtime, so an exposure that lives under one of those
    # paths is writable rather than shadowed by the read-only bind.
    for path in writable:
        argv += ["--bind", path, path]
    argv.append("--")
    return tuple(argv)


#: Each mechanism's prefix builder, and whether it decides on the resolved
#: path. A seatbelt rule matches what the kernel resolved to; a bind mount
#: names a destination in an empty root, where a link's own spelling is a path
#: the child still has to be able to walk.
_PREFIX_BUILDERS: Mapping[str, tuple[Callable[..., tuple[str, ...]], bool]] = {
    "sandbox-exec": (_seatbelt_prefix, True),
    "bwrap": (_bubblewrap_prefix, False),
}


def _prefix_for(
    mechanism: Containment,
    *,
    writable: Sequence[Path | str],
    readable: Sequence[Path | str],
) -> tuple[str, ...]:
    builder, follow = _PREFIX_BUILDERS[mechanism.name]
    return builder(
        mechanism.launcher,
        writable=_existing(writable, follow=follow),
        readable=_existing([*_SYSTEM_READ_PATHS, *readable], follow=follow),
    )


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


def _classify_probe(prefix: Sequence[str], *, cwd: str | None = None) -> str:
    """Run the probe under ``prefix``: a real listener and a real planted secret.

    The secret is written to the host's temporary directory, which no exposure
    this module builds ever includes, so an unconfined child reads it and a
    confined one cannot see it at all.

    ``cwd`` is the directory the probe child starts in, and it belongs inside
    the exposure being probed. A build's provider starts in the materialized
    copy, which the boundary always exposes; a probe left in this process's own
    working directory starts somewhere the boundary deliberately does not
    expose, which is a launcher failure rather than a finding about the
    mechanism.
    """
    nonce = secrets.token_hex(16)
    handle, secret = tempfile.mkstemp(prefix="code-mower-containment-probe-")
    try:
        os.write(handle, secrets.token_hex(32).encode())
    finally:
        os.close(handle)
    try:
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
                    [*prefix, sys.executable, "-c", _CONTAINMENT_PROBE, str(port), secret, nonce],
                    check=False,
                    capture_output=True,
                    timeout=60,
                    cwd=cwd,
                    env={"PATH": os.environ.get("PATH", ""), **_NETWORK_DENY},
                )
            except (OSError, subprocess.SubprocessError):
                return _UNUSABLE
            arrived = _accepted(listener)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(secret)
    # Before anything is read from the exit code: a run that cannot show its
    # child executed classifies as nothing at all. This is the control that
    # keeps "denied" from being the default answer for a launcher that never
    # started the probe.
    if not _ran_the_probe(completed.stdout, nonce):
        return _UNUSABLE
    observed = completed.returncode - _PROBE_BASE
    if observed not in (0, 1, 2, 3):
        return _UNUSABLE
    read_secret = bool(observed & _PROBE_READ_SECRET)
    if arrived != bool(observed & _PROBE_REACHED_LISTENER):
        # The child and the listener disagree about whether a connection
        # happened; treat that as a probe that proved nothing.
        return _UNUSABLE
    if arrived and read_secret:
        return _REACHED
    if not arrived and not read_secret:
        return _CONTAINED
    # Exactly one boundary held. A mechanism that denies sockets while leaving
    # the host filesystem readable is not the boundary this module claims, and
    # accepting it is the defect this classification exists to refuse.
    return _UNUSABLE


def _prefix_confines(prefix: Sequence[str], *, cwd: str | None = None) -> bool:
    """Watch a child under ``prefix`` fail to reach either thing that is really there."""
    return _classify_probe(prefix, cwd=cwd) == _CONTAINED


def _interpreter_read_paths() -> tuple[str, ...]:
    """The minimum a probe child needs to be a running Python at all.

    Both spellings of the interpreter: a virtual environment's ``bin/python``
    is a link, and the child is launched by the name this process knows it by,
    not by the name it resolves to.
    """
    return tuple(
        path
        for path in (sys.executable, os.path.realpath(sys.executable), sys.prefix, sys.base_prefix)
        if path
    )


def _probe_containment() -> Containment | None:
    if not sys.executable:  # pragma: no cover - a frozen interpreter cannot probe
        return None
    readable = _interpreter_read_paths()
    # The scratch directory is the writable exposure *and* the directory every
    # probe child starts in, control included, so the control and the candidates
    # differ in the boundary and in nothing else.
    with tempfile.TemporaryDirectory(prefix="code-mower-containment-") as scratch:
        # The control, first: a child with no prefix must reach the listener
        # *and* read the planted secret. If it cannot -- no probe interpreter,
        # loopback blocked, an unreadable temporary directory -- then "could
        # not" proves nothing about any candidate, and every candidate would
        # pass for a boundary. Refuse the whole probe instead.
        if _classify_probe((), cwd=scratch) != _REACHED:
            return None
        for name, path in _SANDBOX_CANDIDATES:
            launcher = _trusted_launcher(path)
            if launcher is None:
                continue
            mechanism = Containment(name=name, launcher=launcher)
            prefix = _prefix_for(mechanism, writable=(scratch,), readable=readable)
            if _prefix_confines(prefix, cwd=scratch):
                return mechanism
    return None


def containment_mechanism() -> Containment | None:
    """The isolation mechanism this host was observed providing, if any.

    Probed once per process and cached, because the answer is a property of the
    host rather than of a build. ``None`` means this host offers no mechanism
    this build could *observe* denying a child both the network and the host
    filesystem, and a build refuses rather than running a provider it cannot
    contain.
    """
    global _containment, _containment_probed
    if not _containment_probed:
        _containment = _probe_containment()
        _containment_probed = True
    return _containment


def containment_prefix(
    *,
    writable: Sequence[Path | str],
    readable: Sequence[Path | str],
    repository: Path,
) -> tuple[str, ...]:
    """The argv prefix confining a child to ``writable`` plus a read-only runtime.

    Verified against *this* exposure before it is returned, not merely built
    from it. The host probe establishes that a mechanism can confine a child
    when it is handed the interpreter paths; it says nothing about the prefix a
    particular build ends up with, whose readable set is the pinned provider's
    install and whose writable set is that build's own directories. A widened
    exposure that happened to reopen the boundary would otherwise be caught by
    nothing between here and the provider.

    ``repository`` is the checkout being indexed, and it is here because the
    refusals have to be applied to the *whole* readable set rather than to each
    root a caller asks for. Every exposure a build requests goes through
    :func:`_refuse_broad_exposure`; the read-only runtime this module adds on
    top of it never did, so the set the child is really confined to was never
    checked as a set. That is what let a runtime path that happened to contain
    the checkout leave the live working tree readable beside the materialized
    copy that exists to replace it.

    The probe's own exposure is this one plus the interpreter, because the
    probe child is a Python that has to be able to start at all. That makes the
    probed prefix strictly more permissive than the returned one, so a probe
    that still observes containment is a sound statement about the prefix a
    build actually runs under.
    """
    mechanism = containment_mechanism()
    if mechanism is None:
        raise ContextError(
            "local graph builds need an OS sandbox that denies the provider the network and "
            "the host filesystem; this host offers none that could be verified"
        )
    # Both spellings of everything the child could read, which is a superset of
    # what either mechanism is handed: whichever spelling a mechanism decides
    # on, it is checked here.
    _refuse_broad_readable(
        _existing([*_SYSTEM_READ_PATHS, *readable], follow=False), repository=repository
    )
    prefix = _prefix_for(mechanism, writable=writable, readable=readable)
    probe = _prefix_for(
        mechanism,
        writable=writable,
        readable=(*readable, *_interpreter_read_paths()),
    )
    # Inside the exposure, because that is where the confined child starts: a
    # probe launched in a directory the boundary deliberately does not expose
    # fails to start and says nothing about the boundary.
    inside = next((str(path) for path in writable if os.path.isdir(path)), None)
    if not _prefix_confines(probe, cwd=inside):
        raise ContextError(
            "the sandbox this build would run the local graph provider under could not be "
            "observed denying it the network and the host filesystem; no generation was published"
        )
    return prefix


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


def _private_state_reason(path: str) -> str | None:
    """Why this tracked path may not enter the graph, or ``None`` if it may.

    Provider state keeps its own reason because the consequence is specific --
    the provider resuming from a cache of content this build never saw -- while
    ``.git`` and ``.code-mower`` are private state of a different kind: version
    control's own storage, and this tool's packets, evidence and lane records.
    Every segment is tested, case-folded, so ``vendor/.git`` and a nested
    ``docs/.CODE-MOWER`` are as excluded as the top-level ones.
    """
    segments = [segment.casefold() for segment in path.split("/")]
    if any(segment in _PROVIDER_STATE_ROOTS for segment in segments):
        return "provider state"
    if any(segment in _PRIVATE_STATE_ROOTS for segment in segments):
        return "private state"
    return None


def read_tracked_census(repository: Path, commit: str) -> TrackedCensus:
    """List the tracked regular files of one commit, with their blob sizes.

    Reads the commit's tree, never the working tree or the index, so an
    uncommitted edit, an untracked scratch file, and an ignored secret are all
    invisible here by construction rather than by filtering.

    Committed private state -- the provider's own index, ``.git``, and this
    tool's ``.code-mower`` -- is recorded as skipped rather than carried: it is
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
            excluded = _private_state_reason(path)
            if excluded is not None:
                skip(path, excluded)
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
        # Private state is skipped by the census, so a census that still
        # carries it was not built by ``read_tracked_census``. Refuse rather
        # than write ``.git`` or ``.code-mower`` into the tree the provider is
        # about to read, or seed the directory it is about to write into.
        # Every segment, not just the first: a vendored submodule's
        # ``vendor/.git`` is as private as the top-level one. Case-folded
        # because APFS and NTFS name the same directory ``.GIT``.
        or _private_state_reason(path) is not None
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
    #: The scratch directories the provider may write to besides the copy --
    #: the redirected ``HOME`` and ``TMPDIR``. Named here rather than inferred
    #: because they are also exactly what the filesystem boundary exposes: a
    #: directory the environment points at but the sandbox does not expose is a
    #: provider that cannot start.
    writable: tuple[Path, ...] = ()
    #: The census the copy was materialized from -- the denominator completeness
    #: is measured against. Carried on the request rather than re-read from the
    #: copy after the run, because by then the provider has written into that
    #: tree: the question is what this build *gave* the provider, and only the
    #: census is immutable evidence of that. A request without one cannot be
    #: classified as complete, which is the safe direction.
    census: TrackedCensus | None = None


@dataclass(frozen=True)
class IndexResult:
    """What an indexer reports back. ``completeness`` is its own admission."""

    completeness: str = COMPLETE
    indexed_files: int = 0
    #: Inputs this provider classified as code and then deterministically could
    #: not extract -- no wired extractor, or an extractor that declined by
    #: design. Read bytes, no contribution; reported rather than counted as
    #: indexed, and distinct from the census entries a build declines to
    #: materialize at all.
    unsupported_inputs: int = 0
    notes: tuple[str, ...] = ()


def _resolved_executable(executable: str) -> str:
    """Bind the provider to one absolute path, decided in the invocation directory.

    The provider runs with its working directory set to the materialized copy,
    so ``--indexer .venv/bin/graphify`` would otherwise be looked up inside the
    frozen source tree, where the operator's install is not.

    A bare command name is resolved here too, rather than left for the launch to
    look up again. ``PATH`` is not independent of the child's directory: an entry
    on it may itself be relative -- ``PATH=provider-venv/bin:$PATH`` is an
    ordinary thing to have in a shell sitting in a project -- and a relative
    entry names a different directory once the child starts in the materialized
    copy. Containment then drew its boundary around the install this process
    found while the launch searched again from somewhere else, so a correctly
    installed provider failed to launch at all; worse, a repository that happens
    to carry that path would answer the second search with a *tracked file*,
    which is a build executing content it was only ever meant to read. One
    lookup, in the directory the operator invoked from, and both the exposure
    and the argv are that one absolute path.
    """
    if not isinstance(executable, str) or not executable:
        raise ContextError("local graph provider executable must be named")
    separators = [os.sep, os.altsep] if os.altsep else [os.sep]
    if any(separator in executable for separator in separators):
        return str(Path(executable).resolve())
    located = shutil.which(executable)
    if not located:
        raise ContextError(
            "local graph provider executable could not be found on PATH; name it by path "
            "or install the pinned provider where this process can see it"
        )
    return str(Path(located).resolve())


#: The directory a console script sits in, by platform convention. Named so an
#: install root is recognised rather than guessed at from depth alone.
_VENV_SCRIPT_DIRECTORIES = ("bin", "Scripts")

#: What a runtime needs readable *inside* an install prefix, for the case where
#: the prefix itself is too wide to expose. ``/usr`` is the ordinary base for an
#: environment created from the system Python, and exposing it whole is the
#: exposure this module just spent a list narrowing away; these are the
#: directories an interpreter and its standard library actually live in, and on
#: a prefix like ``/usr`` they are already the read-only runtime every child
#: gets, so the narrowing adds nothing rather than widening anything.
_RUNTIME_SUBDIRECTORIES = ("lib", "lib64", "lib32", "libexec", "bin", "share")

#: ``pyvenv.cfg`` is a handful of ``key = value`` lines. Bounded at the stream
#: anyway: it is a file inside somebody else's install, and a build should not
#: be able to be stopped by reading one.
_MAX_VENV_CONFIG_BYTES = 65_536

#: Where a ``pyvenv.cfg`` records the interpreter its environment was created
#: from, in the spellings the three tools that write the file actually use, and
#: what each spelling names. ``virtualenv`` writes ``base-prefix`` and
#: ``base-exec-prefix``; ``uv`` writes those as well; the standard library's
#: ``venv`` writes ``home`` always and ``executable`` since 3.11. A prefix is
#: used as it stands, an executable is the interpreter binary and its prefix is
#: the directory above its script directory, and ``home`` is the script
#: directory itself.
_VENV_BASE_PREFIX_KEYS = ("base-prefix", "base-exec-prefix")
_VENV_BASE_EXECUTABLE_KEYS = ("base-executable", "executable")
_VENV_BASE_SCRIPT_DIRECTORY_KEY = "home"


def _under(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _read_venv_config(root: Path) -> dict[str, str]:
    """The ``key = value`` pairs of an environment's ``pyvenv.cfg``.

    Unparseable lines are skipped rather than fatal. The file is a record left
    by whichever tool created the environment, and a key this module does not
    know about is not a reason to refuse an install that works.
    """
    path = root / "pyvenv.cfg"
    try:
        with path.open("rb") as stream:
            raw = stream.read(_MAX_VENV_CONFIG_BYTES + 1)
    except OSError:
        return {}
    if len(raw) > _MAX_VENV_CONFIG_BYTES:
        raise ContextError(
            "local graph provider's pyvenv.cfg is implausibly large; refusing to derive "
            "a containment boundary from it"
        )
    config: dict[str, str] = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        key, separator, value = line.partition("=")
        if not separator:
            continue
        config.setdefault(key.strip().lower(), value.strip())
    return config


def _prefix_of_interpreter(executable: Path) -> Path:
    """The install prefix holding an interpreter binary.

    ``<prefix>/bin/python3.13`` on POSIX, ``<prefix>\\python.exe`` on Windows.
    The script-directory test is what distinguishes them, so a layout that is
    neither is left at the directory the binary sits in rather than climbing a
    level it cannot justify.
    """
    if executable.parent.name in _VENV_SCRIPT_DIRECTORIES:
        return executable.parent.parent
    return executable.parent


def _provider_base_prefixes(root: Path, *, repository: Path) -> tuple[str, ...]:
    """The base runtime *the provider's own environment* was created from.

    This used to be ``sys.base_prefix``, which is the interpreter running Code
    Mower. The two coincide only when the provider was pinned with the same
    Python this process happens to be running under. A provider pinned with a
    ``uv``-managed or otherwise separately installed interpreter -- an entirely
    ordinary way to pin one -- got somebody else's runtime exposed and its own
    left out of the child's filesystem view, so every build failed on a
    correctly pinned install, and failed from inside the loader rather than
    with anything naming a path.

    So the environment is asked instead of assumed: ``pyvenv.cfg`` records the
    interpreter that created it, and that record is what gets exposed. Each
    candidate is held to exactly the refusals the environment root is held to
    -- a base prefix that is the operator's home or the checkout is no narrower
    for having been read out of a file rather than guessed -- and one already
    inside the read-only system runtime adds nothing.
    """
    config = _read_venv_config(root)
    candidates: list[Path] = []
    for key in _VENV_BASE_PREFIX_KEYS:
        value = config.get(key)
        if value and os.path.isabs(value):
            candidates.append(Path(value))
    for key in _VENV_BASE_EXECUTABLE_KEYS:
        value = config.get(key)
        if value and os.path.isabs(value):
            candidates.append(_prefix_of_interpreter(Path(value)))
    value = config.get(_VENV_BASE_SCRIPT_DIRECTORY_KEY)
    if value and os.path.isabs(value):
        script_directory = Path(value)
        candidates.append(
            script_directory.parent
            if script_directory.name in _VENV_SCRIPT_DIRECTORIES
            else script_directory
        )
    system = tuple(Path(os.path.realpath(path)) for path in _SYSTEM_READ_PATHS)
    exposed: list[str] = []
    seen: set[Path] = set()
    resolved_any = False
    for candidate in candidates:
        base = Path(os.path.realpath(candidate))
        if base in seen:
            continue
        seen.add(base)
        if not base.is_dir():
            # A stale record -- a base interpreter moved or removed since the
            # environment was created. Not fatal on its own: another key may
            # still name a live one, and only all of them failing is a broken
            # pin.
            continue
        resolved_any = True
        # Already covered: inside the read-only runtime every child gets, or
        # inside the environment root that is being exposed anyway.
        if any(_under(base, path) for path in system) or _under(base, Path(os.path.realpath(root))):
            continue
        _refuse_broad_exposure(base, repository=repository)
        if any(_under(path, base) for path in system):
            # A prefix that *contains* the read-only runtime is not a narrow
            # install: ``/usr`` is what an environment created from the system
            # Python records, and exposing it whole hands the provider
            # ``/usr/local``, ``/usr/src``, and anything else the host keeps
            # there. The runtime directories inside it are exposed instead --
            # which, for a prefix like that one, are the system paths every
            # child already has, so nothing is added at all.
            for name in _RUNTIME_SUBDIRECTORIES:
                runtime = base / name
                if runtime.is_dir() and not any(_under(runtime, path) for path in system):
                    exposed.append(str(runtime))
            continue
        exposed.append(str(base))
    if not resolved_any:
        raise ContextError(
            "local graph provider's virtual environment does not record a usable base "
            "interpreter in its pyvenv.cfg, so the runtime it needs cannot be exposed to "
            "the sandbox; recreate the environment with the interpreter the provider is "
            "pinned for"
        )
    return tuple(exposed)


def _is_broad_exposure(root: Path, *, repository: Path) -> bool:
    """Would exposing ``root`` hand over somebody's whole world?

    The filesystem root, the operator's home, the checkout being indexed, and
    every ancestor of either: each of these is a directory whose contents are
    exactly what this boundary exists to keep away from the provider. A root
    that *is* one of them is not a narrow install however it was arrived at,
    and a root *inside the checkout* is the live working tree -- ignored
    secrets and all -- which the materialized copy exists precisely to avoid
    showing anyone.
    """
    resolved = Path(os.path.realpath(root))
    checkout = Path(os.path.realpath(repository))
    try:
        home = Path(os.path.realpath(Path.home()))
    except (OSError, RuntimeError):  # pragma: no cover - a host with no home
        home = None
    refused = {Path(resolved.anchor), checkout, *checkout.parents}
    if home is not None:
        refused.update({home, *home.parents})
    return resolved in refused or _under(resolved, checkout)


def _refuse_broad_exposure(root: Path, *, repository: Path) -> None:
    """Refuse an exposure root a provider install cannot justify."""
    if _is_broad_exposure(root, repository=repository):
        raise ContextError(
            "local graph provider must be pinned into its own virtual environment; "
            "exposing its install would expose the filesystem root, your home directory, "
            "or the checkout being indexed"
        )


def _refuse_broad_readable(readable: Iterable[str], *, repository: Path) -> None:
    """Hold the *whole* readable set to the refusals one exposure root is held to.

    Each root a build asks for is checked as it is derived, and that was the
    only check there was: the read-only runtime is added afterwards and
    unconditionally, so the set the child ends up confined to was never examined
    as a set. A runtime path that contained the checkout -- a checkout under
    ``/usr/local/src`` when this module exposed ``/usr``, say -- therefore left
    the live working tree readable beside the materialized copy that exists
    precisely so the provider never sees it, and no amount of care in deriving
    the *provider's* exposure could notice.

    So the final set is checked, whatever put a path in it. A build whose
    runtime exposure would reach the operator's home or the checkout is refused
    rather than narrowed silently: on an ordinary host nothing here is close to
    either, and on a host where one of them is, the boundary this module claims
    does not hold and saying so is the only honest answer.
    """
    for path in readable:
        if _is_broad_exposure(Path(path), repository=repository):
            raise ContextError(
                "the read-only runtime this build would expose to the local graph provider "
                "reaches the filesystem root, your home directory, or the checkout being "
                "indexed, so the provider could read the working tree the materialized copy "
                "replaces; no generation was published"
            )


#: Mach-O magics, and the ``struct`` byte order each one means. A Mach-O image
#: declares its own order in its first four bytes; the swapped spellings are how
#: a big-endian image announces itself to a little-endian reader. Both widths
#: are listed with the size of the header that follows, because the load
#: commands this reads begin directly after it.
_MACHO_MAGICS: Mapping[bytes, tuple[str, int]] = {
    b"\xcf\xfa\xed\xfe": ("<", 32),  # 64-bit, little endian
    b"\xce\xfa\xed\xfe": ("<", 28),  # 32-bit, little endian
    b"\xfe\xed\xfa\xcf": (">", 32),  # 64-bit, big endian
    b"\xfe\xed\xfa\xce": (">", 28),  # 32-bit, big endian
}

#: A universal ("fat") archive: a big-endian count of architecture records, each
#: naming the offset of a real Mach-O image inside the same file. A python.org
#: interpreter ships these; a Homebrew one does not.
_MACHO_FAT_MAGICS = frozenset({b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"})

#: The load commands that name a library the image will make dyld find. Weak,
#: re-exported and upward links are included: a weak dependency that *is*
#: installed is still opened, and an image that re-exports another still loads
#: it. ``LC_REQ_DYLD`` is the high bit these commands carry.
_MACHO_DYLIB_COMMANDS = frozenset({0x0C, 0x8000_0018, 0x8000_001F, 0x8000_0023})

#: How much of an image's load-command block this will read. The block is a
#: header, not the image; anything past this bound is not a Mach-O this module
#: is prepared to reason about.
_MAX_MACHO_COMMAND_BYTES = 4 * 1024 * 1024

#: How many architecture slices a universal archive may declare.
_MAX_MACHO_ARCHITECTURES = 32

#: How many files the runtime scan will look at, and how many libraries it will
#: add. Both are bounds on somebody else's install, which is the same reason
#: every other foreign file this module reads is bounded: a provider install is
#: not this repository's to trust about its own size.
_MAX_SCANNED_IMAGES = 20_000
_MAX_LINKED_LIBRARIES = 256

#: A shared library that is larger than this is not one; refusing is the honest
#: answer rather than mapping an arbitrary file into the child's view.
_MAX_LINKED_LIBRARY_BYTES = 512 * 1024 * 1024

#: File names that are never a Mach-O image, so the scan does not open them.
#: Purely an optimization -- the magic is what decides -- but it is what keeps
#: the walk over a populated ``site-packages`` cheap.
_NOT_MACHO_SUFFIXES = frozenset(
    {
        ".py", ".pyc", ".pyi", ".pyx", ".txt", ".md", ".rst", ".json", ".toml",
        ".yaml", ".yml", ".cfg", ".ini", ".h", ".hpp", ".c", ".cpp", ".html",
        ".css", ".js", ".png", ".jpg", ".svg", ".gif", ".pdf", ".zip", ".gz",
        ".whl", ".pem", ".crt", ".dist-info", ".egg-info", ".a", ".la",
    }
)

#: Directories the scan does not descend into: build caches and vendored
#: sources, none of which hold an image the child loads.
_NOT_MACHO_DIRECTORIES = frozenset({"__pycache__", ".git", "include", "man", "doc", "docs"})


def _macho_dylib_names(path: Path) -> tuple[str, ...]:
    """Every library path a Mach-O image at ``path`` asks dyld to load.

    Read out of the image's own load commands rather than out of ``otool``:
    deriving the boundary must not itself depend on a developer tool being
    installed, and a parse that reads a bounded header is a smaller thing to
    trust than a subprocess. A file that is not a Mach-O -- which is almost
    everything under an install prefix -- costs four bytes and returns nothing.
    """
    try:
        with path.open("rb") as stream:
            magic = stream.read(4)
            if magic in _MACHO_FAT_MAGICS:
                return _macho_fat_dylib_names(stream)
            if magic not in _MACHO_MAGICS:
                return ()
            stream.seek(0)
            return _macho_slice_dylib_names(stream, 0)
    except (OSError, ValueError, struct.error):
        # An unreadable or truncated image says nothing about what the runtime
        # needs. The build still fails if it was a library the provider loads,
        # and it fails as dyld naming the image rather than as this module
        # guessing at one.
        return ()


def _macho_fat_dylib_names(stream: io.BufferedReader) -> tuple[str, ...]:
    """The union over a universal archive's slices.

    The union rather than the slice matching this process: the child is the
    provider's interpreter, whose architecture is not necessarily this one, and
    every slice's dependencies are paths on the same host.
    """
    count = struct.unpack(">I", stream.read(4))[0]
    if count > _MAX_MACHO_ARCHITECTURES:
        return ()
    offsets = []
    for _ in range(count):
        record = stream.read(20)
        if len(record) != 20:
            return ()
        # cputype, cpusubtype, offset, size, align
        offsets.append(struct.unpack(">5I", record)[2])
    names: dict[str, None] = {}
    for offset in offsets:
        for name in _macho_slice_dylib_names(stream, offset):
            names.setdefault(name, None)
    return tuple(names)


def _macho_slice_dylib_names(stream: io.BufferedReader, offset: int) -> tuple[str, ...]:
    """The ``LC_LOAD_DYLIB`` family of one Mach-O image beginning at ``offset``."""
    stream.seek(offset)
    magic = stream.read(4)
    order_and_header = _MACHO_MAGICS.get(magic)
    if order_and_header is None:
        return ()
    order, header_size = order_and_header
    header = stream.read(header_size - 4)
    if len(header) != header_size - 4:
        return ()
    # cputype, cpusubtype, filetype, ncmds, sizeofcmds, flags[, reserved]
    ncmds, sizeofcmds = struct.unpack(f"{order}6I", header[:24])[3:5]
    if sizeofcmds > _MAX_MACHO_COMMAND_BYTES:
        return ()
    block = stream.read(sizeofcmds)
    names: dict[str, None] = {}
    position = 0
    for _ in range(ncmds):
        if position + 8 > len(block):
            break
        command, size = struct.unpack_from(f"{order}2I", block, position)
        if size < 8 or position + size > len(block):
            break
        if command in _MACHO_DYLIB_COMMANDS and size >= 24:
            name_offset = struct.unpack_from(f"{order}I", block, position + 8)[0]
            if 8 <= name_offset < size:
                raw = block[position + name_offset : position + size]
                name = raw.split(b"\0", 1)[0].decode("utf-8", "replace")
                if name:
                    names.setdefault(name, None)
        position += size
    return tuple(names)


def _scan_images(root: Path, *, budget: list[int]) -> Iterator[Path]:
    """Regular files under ``root`` that could be Mach-O images, within a budget.

    ``budget`` is shared across every root of one derivation, so the cost is a
    property of the whole runtime rather than of each prefix in it. Symlinks are
    not followed during the walk: an install that links a directory elsewhere is
    reached through whatever named it, and following would let one link turn a
    narrow prefix into an unbounded traversal.
    """
    for parent, directories, files in os.walk(root, followlinks=False):
        directories[:] = [name for name in directories if name not in _NOT_MACHO_DIRECTORIES]
        for name in files:
            if budget[0] <= 0:
                return
            if any(name.endswith(suffix) for suffix in _NOT_MACHO_SUFFIXES):
                continue
            budget[0] -= 1
            yield Path(parent) / name


def _trusted_library(path: Path) -> bool:
    """Could only a trusted account have put this library where the child reads it?

    The same question :func:`_trusted_launcher` asks of a sandbox launcher, and
    for the same reason: a library the provider maps executable inside the
    boundary is code, and a file -- or a directory above it -- that some other
    account may write is a file somebody else chooses the contents of. Asked of
    the *resolved* path, so a link's own spelling is not what is trusted.
    """
    trusted = {0, os.geteuid()}
    for current in (path, *path.parents):
        try:
            entry = os.lstat(current)
        except OSError:
            return False
        writable = bool(entry.st_mode & (stat.S_IWGRP | stat.S_IWOTH))
        if writable and stat.S_ISDIR(entry.st_mode) and entry.st_mode & stat.S_ISVTX:
            # A sticky shared directory -- ``/tmp`` and the per-user temporary
            # directories under it. Another account may create its own entries
            # there and may not touch this one, which is the whole point of the
            # bit, so the ancestry it provides is not an account boundary this
            # has to refuse. The file itself is still held to the rule.
            continue
        if entry.st_uid not in trusted or writable:
            return False
    return True


def _linked_runtime_libraries(
    roots: Sequence[Path], *, covered: Sequence[Path], repository: Path
) -> tuple[str, ...]:
    """Shared libraries the exposed runtime links to from outside the exposure.

    A pinned provider's install and the base interpreter it was created from are
    exposed as prefixes, and that was taken to be the whole runtime. It is not,
    on a host whose interpreter was installed by a package manager: CPython's
    ``_ssl`` extension is linked against an OpenSSL that lives under the
    manager's own prefix, not under the interpreter's, and Graphify imports
    ``ssl`` during start-up even for a code-only extraction. Under the
    filesystem boundary that library is simply absent, so the provider aborted
    at import time -- which is a missing runtime dependency, not an argument for
    giving the child a network or a wider filesystem.

    So the dependency is *derived* rather than named. Every Mach-O image inside
    the exposure is read for the libraries it asks dyld to load, and each one
    that is not already covered is resolved and added as a single file. Newly
    added libraries are read in turn, so a transitive dependency -- ``libssl``
    needing ``libcrypto`` -- is reached without either being written down here.

    What is added is the library file, never the directory holding it: exposing
    ``/opt/homebrew/opt/openssl@3/lib`` is a package manager's prefix, and
    exposing the manager's ``etc`` or ``var`` beside it is the operator data
    this boundary exists to withhold. Every added path is put through the same
    ownership and broad-exposure refusals as any other exposure, and a path that
    is not a bounded regular file is refused rather than exposed on the strength
    of an image having named it.

    Linux is unchanged: an ELF runtime's libraries live under the ``/lib`` and
    ``/usr/lib`` directories the read-only runtime already names, and this
    derivation reads Mach-O images, of which such a host has none.
    """
    if sys.platform != "darwin":
        return ()
    system = tuple(Path(os.path.realpath(path)) for path in _SYSTEM_READ_PATHS)
    boundaries = [*system, *(Path(os.path.realpath(root)) for root in covered)]
    added: dict[str, None] = {}
    pending = [Path(os.path.realpath(root)) for root in roots]
    budget = [_MAX_SCANNED_IMAGES]
    while pending:
        current = pending.pop(0)
        images = _scan_images(current, budget=budget) if current.is_dir() else iter((current,))
        for image in images:
            for name in _macho_dylib_names(image):
                if not name.startswith("/"):
                    # ``@rpath``, ``@loader_path`` and ``@executable_path`` are
                    # resolved by dyld against the image itself, so they name
                    # something inside the exposure already.
                    continue
                referenced = Path(name)
                resolved = Path(os.path.realpath(referenced))
                if any(_under(resolved, boundary) for boundary in boundaries):
                    continue
                if str(resolved) in added:
                    continue
                if not resolved.exists():
                    # A weak dependency the host does not have installed. If it
                    # was a required one the provider fails at launch, as dyld
                    # naming the library it could not find.
                    continue
                _refuse_linked_library(resolved, repository=repository)
                if len(added) >= _MAX_LINKED_LIBRARIES:
                    raise ContextError(
                        "the local graph provider's runtime links to more shared libraries "
                        "outside its install than this boundary is willing to expose; "
                        "no generation was published"
                    )
                added[str(resolved)] = None
                if str(referenced) != str(resolved):
                    # Both spellings, for the same reason the executable has
                    # two: a bind-mount boundary names a literal destination,
                    # and dyld opens the path the image wrote down.
                    added.setdefault(str(referenced), None)
                pending.append(resolved)
    return tuple(added)


def _refuse_linked_library(resolved: Path, *, repository: Path) -> None:
    """Refuse a derived dependency that is not a library this may expose."""
    _refuse_broad_exposure(resolved, repository=repository)
    try:
        info = os.lstat(resolved)
    except OSError:  # pragma: no cover - the caller has just seen it exist
        raise ContextError(
            "a shared library the local graph provider's runtime links to could not be "
            "read while deriving the containment boundary; no generation was published"
        ) from None
    if not stat.S_ISREG(info.st_mode):
        raise ContextError(
            "the local graph provider's runtime links to something that is not a regular "
            "file; refusing to expose it to the sandbox"
        )
    if info.st_size > _MAX_LINKED_LIBRARY_BYTES:
        raise ContextError(
            "a shared library the local graph provider's runtime links to is implausibly "
            "large for one; refusing to expose it to the sandbox"
        )
    if not _trusted_library(resolved):
        raise ContextError(
            "a shared library the local graph provider's runtime links to is writable by "
            "an account other than yours or root, so what the provider would load inside "
            "the sandbox is not what this host installed; no generation was published"
        )


def _provider_read_paths(command: str, *, repository: Path) -> tuple[str, ...]:
    """The install the pinned provider needs to be readable, and nothing beside it.

    The executable's parent and grandparent used to be exposed on the reasoning
    that a console script lives in a virtual environment's ``bin``. That is a
    guess about a layout, not knowledge of one, and the guess is wrong in the
    directions that matter most: ``~/bin/graphify`` makes the grandparent the
    operator's home, ``/opt/graphify`` makes it ``/``, and a provider inside the
    checkout makes it the live working tree the materialized copy exists to
    avoid showing anybody. The boundary was then drawn around whatever that
    came out to, and the probe -- which exercises only the interpreter paths --
    never touched it.

    So the layout is *proved* instead. An install root is a directory holding a
    ``pyvenv.cfg`` whose script directory holds this executable: a virtual
    environment, which is what pinning a provider produces, and whose root is
    narrow by construction. A provider that already lives inside the read-only
    system runtime needs no extra exposure at all and gets none. Anything else
    is refused with an instruction rather than exposed as a guess, and whatever
    root is arrived at is put through ``_refuse_broad_exposure`` regardless --
    a proof of layout is not a proof that the layout is narrow.
    """
    located = command if os.path.isabs(command) else shutil.which(command)
    if not located:
        raise ContextError("local graph provider executable could not be located for containment")
    real = Path(os.path.realpath(located))
    # Both spellings of the executable itself: the child is launched by the name
    # this process resolved the command to, and a console script reaches its
    # environment through that environment's own spelling rather than through
    # whatever the link points at.
    spellings = tuple(dict.fromkeys((str(located), str(real))))
    system = tuple(Path(os.path.realpath(path)) for path in _SYSTEM_READ_PATHS)
    if any(_under(real, path) for path in system):
        # Already inside the read-only runtime every child gets. Adding the
        # enclosing prefix would widen that exposure, not narrow it.
        return spellings
    root = real.parent.parent
    if real.parent.name not in _VENV_SCRIPT_DIRECTORIES or not (root / "pyvenv.cfg").is_file():
        raise ContextError(
            "local graph provider must be pinned into its own virtual environment so its "
            "install can be exposed to the sandbox without exposing anything around it"
        )
    _refuse_broad_exposure(root, repository=repository)
    # The base interpreter a virtual environment was created from lives outside
    # it and is what its ``bin/python`` points at, so it is exposed too -- read
    # out of *this* environment's own ``pyvenv.cfg`` rather than taken from the
    # interpreter Code Mower happens to be running under, which is a different
    # installation whenever the provider was pinned with a different Python.
    prefixes = (str(root), *_provider_base_prefixes(root, repository=repository))
    # Last, and derived from the prefixes rather than added to them: a prefix is
    # not the whole runtime on a host whose interpreter links against libraries
    # a package manager keeps somewhere else.
    libraries = _linked_runtime_libraries(
        [Path(prefix) for prefix in prefixes],
        covered=[Path(prefix) for prefix in prefixes],
        repository=repository,
    )
    return (*spellings, *prefixes, *libraries)


#: Where an installed distribution records its own identity (PEP 376). The
#: directory is named ``<name>-<version>.dist-info``, but the name on the
#: directory is not what is read: a directory can be renamed and ``METADATA``
#: is what the installer wrote.
_DIST_INFO_SUFFIX = ".dist-info"

#: Where the environment an executable belongs to keeps those records, relative
#: to the prefix the script directory sits in. Both spellings a virtual
#: environment uses and the two a system install uses are named, because
#: ``_provider_read_paths`` accepts a provider from either.
_SITE_PACKAGES_GLOBS = (
    "lib/python*/site-packages",
    "lib64/python*/site-packages",
    "lib/python*/dist-packages",
    "Lib/site-packages",
)

#: Enough of a ``METADATA`` file to hold its header block. The rest of that
#: file is the project's long description -- a README, sometimes a large one --
#: and no header this reads lives past the first blank line.
_MAX_DIST_METADATA_BYTES = 65_536

#: A ``RECORD`` lists every file its distribution installed, so it is large for
#: a large package and bounded for the same reason every other foreign file
#: here is: it is read out of somebody else's install.
_MAX_DIST_RECORD_BYTES = 8 * 1024 * 1024

#: How much of the provider executable is hashed against its recorded digest.
#: A console script is a few hundred bytes and a compiled launcher a few
#: megabytes; anything past this is not an install this check can speak to.
_MAX_PROVIDER_EXECUTABLE_BYTES = 64 * 1024 * 1024

#: Runs of the separators PEP 503 collapses, for comparing a pinned
#: distribution name against an installed one. ``Graphify_Y`` and ``graphify-y``
#: are one distribution, and a pin must not fail against its own install over
#: the spelling an installer happened to write.
_NAME_SEPARATORS = re.compile(r"[-_.]+")


def _normalized_distribution(name: str) -> str:
    return _NAME_SEPARATORS.sub("-", name.strip()).lower()


def _reportable(value: str) -> str:
    """A short printable spelling of something read out of a foreign install.

    Installed metadata is not indexed content, but it is not this process's
    text either, and it ends up in an operator-facing message. Bounded and
    stripped of anything unprintable so a crafted ``METADATA`` cannot rewrite
    the terminal the refusal is read in.
    """
    printable = "".join(character if character.isprintable() else "?" for character in value[:64])
    return printable.strip() or "an unnamed distribution"


def _site_package_roots(executable: Path) -> tuple[Path, ...]:
    """Where the environment this executable belongs to keeps its installs."""
    prefix = executable.parent.parent
    roots: list[Path] = []
    for pattern in _SITE_PACKAGES_GLOBS:
        roots.extend(candidate for candidate in sorted(prefix.glob(pattern)) if candidate.is_dir())
    return tuple(dict.fromkeys(roots))


def _dist_info_headers(dist_info: Path) -> dict[str, str]:
    """The ``METADATA`` header block, lowercased keys, first spelling wins."""
    try:
        with (dist_info / "METADATA").open("rb") as stream:
            raw = stream.read(_MAX_DIST_METADATA_BYTES)
    except OSError:
        return {}
    headers: dict[str, str] = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.strip():
            # The header block ends at the first blank line. Everything after
            # it is the description, which may contain anything at all,
            # including lines that look like headers.
            break
        if line[:1] in (" ", "\t"):
            continue
        key, separator, value = line.partition(":")
        if separator:
            headers.setdefault(key.strip().lower(), value.strip())
    return headers


def _record_entries(dist_info: Path) -> tuple[tuple[str, str], ...]:
    """``(installed path, sha256 hex or "")`` for every file a distribution wrote.

    The digest is recorded base64url-encoded without padding, and is absent for
    some entries by design -- ``RECORD`` cannot record its own hash. An entry
    whose digest is missing or in an algorithm this does not read comes back
    with an empty one rather than being dropped: it still proves ownership,
    which is the first thing this file is read for.
    """
    try:
        with (dist_info / "RECORD").open("rb") as stream:
            raw = stream.read(_MAX_DIST_RECORD_BYTES + 1)
    except OSError:
        return ()
    if len(raw) > _MAX_DIST_RECORD_BYTES:
        raise ContextError(
            "the local graph provider's installed RECORD is implausibly large; refusing to "
            "check the pin against it"
        )
    entries: list[tuple[str, str]] = []
    try:
        for row in csv.reader(io.StringIO(raw.decode("utf-8", "replace"))):
            if not row or not row[0]:
                continue
            algorithm, _, encoded = (row[1] if len(row) > 1 else "").partition("=")
            digest = ""
            if algorithm == "sha256" and encoded:
                try:
                    digest = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).hex()
                except ValueError:
                    digest = ""
            entries.append((row[0], digest))
    except csv.Error:
        # A ``RECORD`` this cannot parse claims nothing, which leaves the
        # executable owned by no distribution and the build refused. Failing
        # closed on an unreadable install beats accepting an unchecked one.
        return ()
    return tuple(entries)


def _installed_owner(executable: Path) -> tuple[Path, str] | None:
    """The distribution that installed this executable, and the digest it recorded.

    Ownership is read from ``RECORD`` rather than guessed from the file's name.
    A pinned release and a lookalike can both ship a console script called
    ``graphify``, and an environment is free to hold both; what the pin has to
    be checked against is the distribution that wrote *this* file.
    """
    target = str(executable)
    for site_packages in _site_package_roots(executable):
        for dist_info in sorted(site_packages.glob("*" + _DIST_INFO_SUFFIX)):
            if not dist_info.is_dir():
                continue
            for recorded, digest in _record_entries(dist_info):
                if os.path.realpath(site_packages / recorded) == target:
                    return dist_info, digest
    return None


def _executable_digest(executable: Path) -> str:
    digest = hashlib.sha256()
    read = 0
    try:
        with executable.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                read += len(chunk)
                if read > _MAX_PROVIDER_EXECUTABLE_BYTES:
                    raise ContextError(
                        "the local graph provider's executable is implausibly large; refusing to "
                        "check it against the digest its installer recorded"
                    )
                digest.update(chunk)
    except OSError:
        raise ContextError("the local graph provider's executable could not be read") from None
    return digest.hexdigest()


def _verify_provider_installation(command: str, *, pin: GraphifyPin) -> None:
    """Prove the executable about to run is the pinned release, before it runs.

    Every generation's manifest records the pin as the provenance of the bytes
    in it, and until this check existed that record was a copy of what the
    operator typed: ``--indexer`` named an install, ``--pin-file`` named a
    release, and nothing compared the two. A build could publish a manifest
    naming one release while a different one -- or a different distribution
    that happens to answer to ``extract`` -- produced the graph, and neither
    ``status`` nor the manifest could tell afterwards. For a package whose name
    differs from the repository's by one character, that is the substitution
    the pin exists to make identifiable.

    The install is *read*, not the provider *asked*. ``graphify --version``
    would mean launching the very executable whose identity is in question,
    outside the sandbox that exists to confine it, and then believing what it
    printed about itself.

    What an unpacked install can answer is identity, not provenance of the
    artifact: nothing on disk retains the wheel it came from, so
    ``wheel_sha256`` stays the operator's record of which artifact they
    installed rather than something checkable here. The three things that are
    checkable are checked -- which distribution owns this executable, at which
    version, and whether the file still matches the digest its installer
    recorded for it.
    """
    executable = Path(os.path.realpath(command))
    owner = _installed_owner(executable)
    if owner is None:
        raise ContextError(
            "the executable named for the local graph provider belongs to no distribution "
            "installed in its environment, so it cannot be checked against the pin; install "
            "the pinned release and name the console script that install provides"
        )
    dist_info, recorded_digest = owner
    headers = _dist_info_headers(dist_info)
    installed, version = headers.get("name", ""), headers.get("version", "")
    if not installed or not version:
        raise ContextError(
            "the local graph provider's install records no name and version of its own, so the "
            "pin cannot be checked against it; no generation was published"
        )
    named = _normalized_distribution(installed) == _normalized_distribution(pin.distribution)
    # The version is compared exactly, as the installer recorded it: a pin is
    # one release, and deciding that ``1.0`` and ``1.0.0`` are the same release
    # is a version-comparison policy this has no business inventing.
    if not named or version != pin.version:
        raise ContextError(
            f"the installed local graph provider is {_reportable(installed)} "
            f"{_reportable(version)}, not the pinned {pin.distribution} {pin.version}; "
            "no generation was published"
        )
    if recorded_digest and _executable_digest(executable) != recorded_digest:
        raise ContextError(
            "the local graph provider's executable no longer matches the digest its installer "
            "recorded, so the pinned install has been modified in place; no generation was "
            "published"
        )


#: The subcommand the evaluated release exposes, recorded in
#: ``docs/graphify-evaluation.md``: the clean-room run indexed with
#: ``extract --code-only --no-cluster --max-workers 4``. There is no
#: ``--source``/``--output`` pair to hand it; ``extract`` writes its state
#: beside the sources it was pointed at, which is why the child's working
#: directory is the materialized copy and why the adapter collects an artifact
#: afterwards rather than naming one up front.
_PROVIDER_EXTRACT = "extract"

#: The scan target, which the pinned CLI requires and does not default.
#:
#: This module used to launch ``extract`` with the options alone, on the reading
#: that a subcommand which writes beside its sources must also discover them
#: from the working directory. The pinned CLI does not: it takes the target as
#: the first positional after the subcommand, decides it has one only when that
#: argument does not begin with ``-``, and exits 1 with ``must specify a path to
#: scan or a --postgres DSN`` when it does not. Every real build therefore
#: failed before extraction, and the failure arrived as the generic non-zero
#: refusal rather than as anything naming the omission.
#:
#: ``.`` rather than the source root's absolute path: the child's working
#: directory is already the materialized copy, so the relative spelling names
#: exactly the tree this build means and names nothing about where that tree
#: sits on the host. It must be passed *between* the subcommand and the options
#: -- the CLI reads ``sys.argv[2]`` and nothing later -- and it is a path, so a
#: bare ``.`` can never be mistaken for a flag the way a caller-supplied string
#: could.
_PROVIDER_SCAN_TARGET = "."

#: The extensions the pinned provider's own ``detect.CODE_EXTENSIONS`` treats as
#: code, transcribed from the hash-verified 0.9.58 wheel. This is the
#: denominator's definition and it has to be the provider's, not a plausible
#: one: a build is complete when every input the provider itself would dispatch
#: was processed, and holding it to every tracked documentation file instead
#: would make a correct run permanently partial. Nothing outside this set is
#: counted against the run -- those inputs are deterministically not code to
#: this pin, so they are skipped rather than missing.
_PROVIDER_CODE_EXTENSIONS = frozenset({
    ".py", ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs", ".ejs",
    ".ets", ".go", ".rs", ".java", ".groovy", ".gradle", ".cpp", ".cc", ".cxx",
    ".c", ".h", ".hpp", ".cu", ".cuh", ".metal", ".rb", ".rake", ".swift",
    ".kt", ".kts", ".cs", ".scala", ".php", ".lua", ".luau", ".toc", ".zig",
    ".ps1", ".psm1", ".psd1", ".ex", ".exs", ".m", ".mm", ".ml", ".mli", ".jl",
    ".vue", ".svelte", ".astro", ".dart", ".v", ".sv", ".svh", ".sql", ".r",
    ".f", ".F", ".f90", ".F90", ".f95", ".F95", ".f03", ".F03", ".f08", ".F08",
    ".pas", ".pp", ".dpr", ".dpk", ".lpr", ".inc", ".dfm", ".lfm", ".lpk",
    ".sh", ".bash", ".json", ".tf", ".tfvars", ".hcl", ".dm", ".dme", ".dmi",
    ".dmm", ".dmf", ".sln", ".slnx", ".csproj", ".fsproj", ".vbproj", ".xaml",
    ".razor", ".cshtml", ".cls", ".trigger", ".lisp", ".cl", ".lsp", ".asd",
    ".robot", ".resource",
})

#: Names the pinned ``detect.classify_file`` routes to code by *filename*,
#: ahead of every suffix class, because ``manifest_ingest`` parses them
#: deterministically (``PACKAGE_MANIFEST_NAMES``, compared lower-cased against
#: the basename). Suffix membership alone misses all of them -- ``.yml``,
#: ``.toml``, ``.mod`` and ``.xml`` are not in ``CODE_EXTENSIONS`` -- so a
#: denominator built from extensions would let a package manifest the provider
#: failed on drop out of the coverage question entirely.
_PROVIDER_PACKAGE_MANIFEST_NAMES = frozenset({
    "apm.yml", "apm.yaml", "pyproject.toml", "cargo.toml", "go.mod", "pom.xml",
})

#: The one compound suffix the pin tests before the simple one. ``.blade.php``
#: already ends in a code extension, so this only decides *which* extractor the
#: pin uses; it is named here because the classification below is meant to be
#: readable against ``classify_file``'s own order rather than to be minimal.
_PROVIDER_COMPOUND_CODE_SUFFIX = ".blade.php"

#: ``detect._SHEBANG_CODE_INTERPRETERS``: the interpreters that make an
#: *extensionless* tracked file code to this pin. ``classify_file`` reaches
#: this branch before any extension test, so a CLI entry point with no suffix
#: is dispatched exactly like a ``.py`` file and belongs in the denominator.
_PROVIDER_SHEBANG_CODE_INTERPRETERS = frozenset({
    "python", "python3", "python2",
    "ruby", "perl", "node", "nodejs",
    "bash", "sh", "dash", "zsh", "fish", "ksh", "tcsh",
    "lua", "php", "julia", "Rscript",
})

#: ``extract._SHEBANG_DISPATCH``: the subset of the above that the pin has an
#: extractor for. The remainder (``perl``, ``fish``, ``tcsh``, ``Rscript``) is
#: classified as code and then deterministically contributes nothing.
_PROVIDER_SHEBANG_EXTRACTORS = frozenset({
    "python", "python2", "python3",
    "bash", "sh", "dash", "zsh", "ksh",
    "node", "nodejs", "ruby", "lua", "php", "julia",
})

#: The extensions in the pin's ``CODE_EXTENSIONS`` with no entry in
#: ``extract._DISPATCH`` -- the exact set difference of the two staged tables.
#: The pin warns about these itself (#1689: "classified as code but graphify
#: has no AST extractor for their language"), dispatches them, and stamps them,
#: because ``_get_extractor`` returning ``None`` short-circuits to
#: ``{"nodes": [], "edges": []}`` with neither an ``error`` nor a ``skipped``
#: marker, and the CLI's failed-source rule only clears rows for the two cases
#: that carry one. They are counted and reported, never silently folded into
#: the files this build says were indexed.
_PROVIDER_UNSUPPORTED_EXTENSIONS = frozenset({".ejs", ".ets", ".r"})

#: ``.m`` is the one suffix whose dispatch the pin decides from the *bytes*
#: rather than the table: it is Objective-C or MATLAB/Octave, and
#: ``_get_extractor`` returns ``None`` for a ``.m`` carrying no Objective-C
#: directive (#1702) rather than force-parsing MATLAB through the ObjC grammar.
#: The static table difference above cannot see that, so without this branch a
#: MATLAB file would be counted as a file this build indexed on the strength of
#: a stamped row alone -- and the row *is* stamped, because a ``None`` extractor
#: short-circuits to ``{"nodes": [], "edges": []}`` carrying neither marker the
#: failed-source rule looks for. It is code either way, so it stays in the
#: denominator; what is decided here is only whether it contributed anything.
_PROVIDER_OBJC_AMBIGUOUS_SUFFIX = ".m"

#: ``extract._OBJC_HEADER_MARKERS``: the Objective-C-only directives the pin
#: sniffs for, and the window it sniffs in (``_is_objc_header`` slices the first
#: 256 KiB). A marker past that window is invisible to the pin too, so reading
#: exactly that much decides the same way while staying bounded.
_PROVIDER_OBJC_MARKERS = (
    b"@interface", b"@protocol", b"@implementation", b"@import", b"#import",
)
_OBJC_PROBE_BYTES = 256 * 1024

#: How much of an extensionless input is read to find its shebang. The pin
#: reads the same 256 bytes and keeps only the first line.
_SHEBANG_PROBE_BYTES = 256

#: A manifest row's content hash, as the pin computes it: ``_md5_file`` streams
#: the file and returns a hex digest, or the empty string when the read failed.
#: So a well-formed 32-character digest is the provider's own statement that it
#: read those bytes, and anything else -- blank, short, uppercase, non-string --
#: is a row that proves nothing about the file it names.
_MANIFEST_HASH = re.compile(r"[0-9a-f]{32}\Z")

#: The row fields the pinned ``save_manifest`` writes. Read as a shape check
#: only: a mapping missing them is not the document this adapter can classify.
_MANIFEST_ROW_FIELDS = ("mtime", "seen", "ast_hash", "semantic_hash")

#: How much of a materialized input is hashed at a time while deriving what the
#: provider's row for it must say. Bounded by the census, which is already
#: bounded by ``MAX_TRACKED_BYTES``, so this only bounds resident memory.
_MANIFEST_DIGEST_CHUNK_BYTES = 1024 * 1024


def _materialized_digest(path: Path) -> str:
    """The pin's own content digest of one materialized input, or ``""``.

    ``_md5_file`` in the pinned 0.9.58 wheel streams the file and returns the
    MD5 hex digest of its bytes -- the same digest the fresh contained
    extraction's manifest carried for both Python inputs of the public fixture,
    matching their exact bytes. So this is not a re-implementation of the
    provider's AST work: it is the one thing the provider's row is a statement
    *about*, computed here so the statement can be checked rather than believed.

    ``""`` for anything that cannot be read as a regular file, which is a
    refusal rather than a pass: a row this build cannot check is a row it cannot
    count.
    """
    digest = hashlib.md5(usedforsecurity=False)
    try:
        if path.is_symlink() or not path.is_file():
            return ""
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(_MANIFEST_DIGEST_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def _materialized_digests(
    source_root: Path,
    census: TrackedCensus | None,
    inputs: _ProviderInputs | None = None,
) -> dict[str, str] | None:
    """What every eligible code input's manifest row must say, before the run.

    Taken from the materialized copy *before* the provider is launched, which is
    the only moment those bytes are still exactly what this build gave it. After
    the run the same tree also holds provider output, and a digest read then
    would be checking the provider's manifest against whatever the provider left
    behind.

    ``None`` when there is no census, because there is then nothing to enumerate
    -- that case is already the partial one. An input the copy cannot be read
    for is simply absent from the map, and ``_read_completeness`` keeps the run
    partial for it rather than accepting the row unchecked.
    """
    if census is None:
        return None
    if inputs is None:
        inputs = _provider_inputs(census, source_root)
    digests: dict[str, str] = {}
    for path in inputs.dispatched:
        host = _census_host_path(source_root, path)
        if host is None:
            continue
        digest = _materialized_digest(host)
        if digest:
            digests[unicodedata.normalize("NFC", path)] = digest
    return digests


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


def _provider_output_directory(source_root: Path) -> Path:
    """The output root the provider wrote during this run.

    Exactly one name is collected -- the pin's own ``graphify-out`` -- and it is
    resolved beneath the materialized copy, never from a path or an environment
    variable a caller could supply. Only reachable after
    ``_refuse_pre_existing_provider_state``, so what is found here was created
    by the run that just finished.

    A second provider root beside it is refused rather than ignored. This
    adapter cannot tell which of two roots a generation should be cut from, and
    picking one would publish an artifact whose provenance is a guess; a
    ``.graphify/`` that appeared next to ``graphify-out/`` also says the run did
    something other than the single contained extraction that was launched.
    """
    directory = source_root / _PROVIDER_OUTPUT_DIRECTORY
    competing = [
        name
        for name in _PROVIDER_STATE_DIRECTORIES
        if name != _PROVIDER_OUTPUT_DIRECTORY
        and ((source_root / name).exists() or (source_root / name).is_symlink())
    ]
    if competing:
        raise ContextError(
            "local graph provider wrote more than one output root; no generation was published"
        )
    if directory.is_symlink() or not directory.is_dir():
        raise ContextError("local graph provider wrote no output; no generation was published")
    graph = directory / _PROVIDER_GRAPH_NAME
    if graph.is_symlink() or not graph.is_file():
        raise ContextError(
            "local graph provider left no graph document; no generation was published"
        )
    return directory


def _provider_manifest(output_directory: Path) -> Mapping[str, Any] | None:
    """The provider's own record of what it processed, or ``None`` if unreadable.

    Bounded at the stream, not after the fact: the manifest is provider output
    of unknown size, and reading it whole to slice it afterwards would let it
    exhaust this process before any budget was consulted. Anything longer than
    a manifest is rejected outright rather than parsed from a prefix, which
    would be a different document than the one the provider wrote.
    """
    path = output_directory / _PROVIDER_MANIFEST_NAME
    if path.is_symlink() or not path.is_file():
        return None
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


@dataclass(frozen=True)
class _ProviderInputs:
    """How the pin would classify this census, decided before it is launched.

    ``dispatched`` is the denominator: every tracked path ``classify_file``
    would call code, in census order. ``unsupported`` is the subset of those the
    pin then has no extractor for, which is a real and reportable outcome rather
    than a failure. ``unclassified`` is everything the classification could not
    decide -- an extensionless input the copy could not be read for, or a
    shebang spelling this adapter refuses to guess at -- and it keeps the run
    partial, because an input nobody can classify is an input nobody can say was
    covered.
    """

    dispatched: tuple[str, ...] = ()
    unsupported: frozenset[str] = frozenset()
    unclassified: tuple[str, ...] = ()


def _census_host_path(source_root: Path, path: str) -> Path | None:
    """Where a census path lives in the materialized copy, or ``None``.

    ``read_tracked_census`` reads Git's own index paths, which are relative and
    carry no traversal segment. Held to that here anyway, because this is the
    only place a census path is turned back into a host path, and a join is not
    the place to discover otherwise.
    """
    parts = PurePosixPath(path).parts
    if not parts or any(part in ("", ".", "..", "/") for part in parts):
        return None
    return source_root.joinpath(*parts)


def _shebang_interpreter(path: Path) -> tuple[str | None, bool]:
    """``(interpreter, resolved)`` for an extensionless input's first line.

    ``(None, True)`` is a decision: there is no shebang, so ``classify_file``
    would not call this file code. ``(None, False)`` is a refusal: the bytes
    could not be read, or the line is one of the ``env(1)`` spellings the pin
    resolves through option parsing this adapter deliberately does not
    reimplement (``-S``/``--split-string`` and friends). A refusal is carried as
    *unclassified* rather than guessed either way, because guessing "not code"
    would drop a real input out of the denominator and guessing "code" would
    demand a row for a file the pin never dispatched.

    Only the simple, unambiguous ``env`` forms are resolved here: leading
    ``NAME=value`` assignments followed by the interpreter, which is what a
    tracked script ordinarily carries.
    """
    try:
        if path.is_symlink() or not path.is_file():
            return None, False
        with path.open("rb") as stream:
            head = stream.read(_SHEBANG_PROBE_BYTES)
    except OSError:
        return None, False
    if not head.startswith(b"#!"):
        return None, True
    line = head.split(b"\n")[0].decode(errors="replace")[2:].strip()
    try:
        parts = shlex.split(line)
    except ValueError:
        return None, False
    if not parts:
        return None, True
    interpreter = PurePosixPath(parts[0].replace("\\", "/")).name
    if interpreter != "env":
        return interpreter, True
    for argument in parts[1:]:
        if argument.startswith("-"):
            # An option-carrying ``env`` line. The pin has a full parser for
            # these; this one says so rather than pretending to.
            return None, False
        if "=" in argument:
            continue
        return PurePosixPath(argument.replace("\\", "/")).name, True
    return None, True


def _objc_source(path: Path) -> tuple[bool, bool]:
    """``(objective_c, resolved)`` for one materialized ``.m`` input.

    ``(False, True)`` is a decision: the bytes carry no Objective-C directive,
    so the pin's ``_get_extractor`` returns ``None`` for this file and it is
    dispatched, stamped, and contributes nothing. ``(False, False)`` is a
    refusal: the copy could not be read here, so which way the pin decided is
    unknown and the caller carries the input as unclassified rather than
    guessing. The pin's own sniff answers ``False`` on a read error, but that is
    a statement about *its* read; this adapter failing to read the same bytes
    proves nothing about what the provider was shown.
    """
    try:
        if path.is_symlink() or not path.is_file():
            return False, False
        with path.open("rb") as stream:
            head = stream.read(_OBJC_PROBE_BYTES)
    except OSError:
        return False, False
    return any(marker in head for marker in _PROVIDER_OBJC_MARKERS), True


def _provider_inputs(
    census: TrackedCensus, source_root: Path | None = None
) -> _ProviderInputs:
    """Classify the census the way the pinned ``detect.classify_file`` would.

    In its order, which is the part suffix membership gets wrong: a package
    manifest is routed by *filename* before any extension is looked at, and an
    extensionless file is routed by its *shebang* before the extension table is
    reached at all. A denominator built from ``CODE_EXTENSIONS`` alone drops
    both, so a ``pyproject.toml`` or a ``#!/usr/bin/env python3`` CLI the
    provider failed on could be missing from the manifest while an unrelated
    ``.py`` file let the run claim it was complete.

    Case is tried both ways for the extension test because the pin's set carries
    both ``.f90`` and ``.F90``; matching the spelling first and the lower-cased
    suffix second can only widen the denominator, which is the direction that
    refuses rather than over-claims.

    ``source_root`` is the materialized copy, read *before* the provider is
    launched -- the only moment those bytes are still exactly what this build
    handed over. Without it no extensionless input and no ``.m`` can be
    classified, so each of them is unclassified and the run stays partial.

    Two dispatch questions are answered from those bytes rather than from a
    table: which interpreter an extensionless script names, and whether a ``.m``
    is Objective-C or MATLAB. The second decides only whether a code input had
    an extractor at all, never whether it is code -- ``.m`` is in the pin's
    extension table either way.
    """
    dispatched: list[str] = []
    unsupported: set[str] = set()
    unclassified: list[str] = []
    for entry in census.entries:
        path = entry.path
        pure = PurePosixPath(path)
        name = pure.name.lower()
        if name in _PROVIDER_PACKAGE_MANIFEST_NAMES or name.endswith(
            _PROVIDER_COMPOUND_CODE_SUFFIX
        ):
            dispatched.append(path)
            continue
        suffix = pure.suffix
        if not suffix:
            host = _census_host_path(source_root, path) if source_root is not None else None
            if host is None:
                unclassified.append(path)
                continue
            interpreter, resolved = _shebang_interpreter(host)
            if not resolved:
                unclassified.append(path)
            elif interpreter in _PROVIDER_SHEBANG_CODE_INTERPRETERS:
                dispatched.append(path)
                if interpreter not in _PROVIDER_SHEBANG_EXTRACTORS:
                    unsupported.add(path)
            continue
        if suffix in _PROVIDER_CODE_EXTENSIONS or suffix.lower() in _PROVIDER_CODE_EXTENSIONS:
            dispatched.append(path)
            if suffix.lower() in _PROVIDER_UNSUPPORTED_EXTENSIONS:
                unsupported.add(path)
            elif suffix.lower() == _PROVIDER_OBJC_AMBIGUOUS_SUFFIX:
                # The one dispatch the pin decides from the bytes. Read from the
                # same pre-launch copy every other classification here reads, so
                # the answer is about what the provider was handed.
                host = _census_host_path(source_root, path) if source_root is not None else None
                objective_c, resolved = _objc_source(host) if host is not None else (False, False)
                if not resolved:
                    unclassified.append(path)
                elif not objective_c:
                    unsupported.add(path)
    return _ProviderInputs(tuple(dispatched), frozenset(unsupported), tuple(unclassified))


def _read_completeness(
    manifest: Mapping[str, Any] | None,
    census: TrackedCensus | None,
    digests: Mapping[str, str] | None = None,
    inputs: _ProviderInputs | None = None,
) -> IndexResult:
    """Classify a provider run against its own manifest, defaulting to partial.

    The pinned ``save_manifest`` writes a flat mapping of repository-relative
    POSIX path to ``{mtime, seen, ast_hash, semantic_hash}``. It is not a
    completion report and carries no flag or count, so completeness is a
    coverage question: did every input this pin would dispatch come back with a
    hash proving the provider read its bytes?

    The denominator is the immutable materialized census classified the way
    ``detect.classify_file`` classifies it -- filename-routed package manifests
    first, then extensionless shebang scripts, then the extension table (see
    ``_provider_inputs``). Inputs outside that classification are
    deterministically not code to this pin and are skipped, not missing.
    Everything else is counted, and a
    file is processed only when its row carries a well-formed ``ast_hash`` *and
    that hash is the digest of the bytes this build actually handed the
    provider*. A well-formed digest alone says a hash-shaped string is present;
    only the comparison says it is a hash of this input. Without it a row
    carried over from another tree, another revision, or a resumed cache reads
    as proof of work on bytes the provider was never shown -- and the digests
    come from ``_materialized_digests``, taken before the launch, so they cannot
    have been influenced by what the run wrote. A row whose digest disagrees, and
    an input the copy could not be re-read for, both stay partial.

    What a stamped row is evidence *of* comes from the pin's own
    post-extraction writer rule, staged in ``extract.py`` and ``cli.py``. After
    the run, ``_failed_sources`` is assembled from the per-file results and the
    CLI clears (``clear_ast``) exactly those rows; every other dispatched input
    is stamped. A result lands in ``_failed_sources`` when it carries an
    ``error``, or when its extractor produced zero nodes. It does **not** when:

    * ``_get_extractor`` returned ``None`` -- the file short-circuits to
      ``{"nodes": [], "edges": []}`` with neither marker, so a code-classified
      input the pin has no extractor for is stamped while contributing nothing
      (the pin's own #1689 warning); or
    * the extractor declined by design -- ``extractors/json_config`` returns a
      ``skipped`` marker for data JSON and for a non-object root, and the CLI
      skips those deliberately so they are not requeued forever (#2879).

    So a stamped, matching row proves the provider read those exact bytes and
    did not fail on them. It does not prove nodes, and it is not read here as
    if it did. The deterministically unsupported dispatch is counted and
    reported separately (``unsupported_inputs``) rather than folded into
    ``indexed_files``, and zero nodes for a file whose row is stamped is a
    complete *read* of that file and nothing more.

    The boundary, plainly. ``classify_file`` is the eligibility oracle: what it
    deterministically calls not-code -- every suffix outside its registry
    included -- is not in the denominator and does not make a run partial, so
    there is no separate count of it. ``unsupported_inputs`` counts the inputs
    it *does* call code that then reach a dispatch the pin has no extractor for:
    the static table difference, a code shebang with no ``_SHEBANG_DISPATCH``
    entry, and a ``.m`` whose bytes carry no Objective-C directive. Anything
    else is partial: an eligible code input that failed, one whose postcondition
    is unknown, one whose row disagrees with the bytes, and one whose zero-node
    result cannot be told apart from a failure.

    Blank rows are the cases the pin's rule makes blank: an extractor error or
    an anomalous zero-node extract. They stay partial here. The clean-room
    repeat that exited zero in 1.63 s requeued 54 entries; the retained
    evidence for that run carries *stamped* rows, so requeueing there is not
    observable as a blank row and nothing in this adapter claims it is. That
    remains an observed limitation of the incremental gate rather than a shape
    this module reports on.

    Nothing upgrades a run: the exit status, a non-empty graph, and the raw
    extraction's ``extracted_sources`` are all statements about what was
    *dispatched*, failures included, so none of them is success evidence.
    Missing evidence, an unparseable manifest, a shape this adapter does not
    recognize, an input it could not classify, and a row that cannot be told
    apart from a failure all stay ``partial``, which ``graph_status`` refuses
    by default. The provider owns no provenance (the evaluation records this as
    the first product constraint), so that refusal is the failure an operator
    can act on; silently calling it complete is the one they cannot.
    """
    if census is None:
        return IndexResult(
            completeness=PARTIAL,
            notes=("build recorded no census to check the provider's manifest against",),
        )
    if manifest is None:
        return IndexResult(
            completeness=PARTIAL,
            notes=("provider left no readable manifest of what it processed",),
        )
    if digests is None:
        return IndexResult(
            completeness=PARTIAL,
            notes=("build recorded no input digests to check the provider's manifest against",),
        )
    if inputs is None:
        inputs = _provider_inputs(census)
    eligible = inputs.dispatched
    rows: dict[str, Any] = {}
    malformed_rows = 0
    for key, row in manifest.items():
        if not isinstance(key, str):
            malformed_rows += 1
            continue
        if not isinstance(row, Mapping) or any(
            field not in row for field in _MANIFEST_ROW_FIELDS
        ):
            malformed_rows += 1
            continue
        rows[unicodedata.normalize("NFC", key)] = row
    unsupported_keys = {
        unicodedata.normalize("NFC", path) for path in inputs.unsupported
    }
    missing = 0
    unstamped = 0
    unreadable = 0
    mismatched = 0
    processed = 0
    unsupported = 0
    for path in eligible:
        key = unicodedata.normalize("NFC", path)
        row = rows.get(key)
        if row is None:
            missing += 1
            continue
        digest = row.get("ast_hash")
        if not isinstance(digest, str) or not _MANIFEST_HASH.fullmatch(digest):
            unstamped += 1
            continue
        expected = digests.get(key)
        if expected is None:
            # The row is well formed and this build cannot say what it should
            # have contained. Counting it would be believing the row on its own
            # word, which is the whole thing the comparison exists to stop.
            unreadable += 1
        elif digest != expected:
            mismatched += 1
        elif key in unsupported_keys:
            # Read, not failed, and deterministically not extractable by this
            # pin. Counted on its own line rather than as a file this build
            # indexed, which it is not.
            unsupported += 1
        else:
            processed += 1
    notes: list[str] = []
    if malformed_rows:
        notes.append(f"provider manifest carried {malformed_rows} unreadable records")
    if missing:
        notes.append(f"provider manifest does not account for {missing} code files")
    if unstamped:
        notes.append(f"provider left {unstamped} code files unprocessed or requeued")
    if unreadable:
        notes.append(f"build could not re-read {unreadable} code files to check their hashes")
    if mismatched:
        notes.append(f"provider hashed {mismatched} code files that are not the bytes it was given")
    if inputs.unclassified:
        notes.append(
            f"build could not classify {len(inputs.unclassified)} tracked inputs "
            "against this provider's own dispatch"
        )
    if not eligible:
        notes.append("the census carried no code files this provider would index")
    if notes:
        return IndexResult(
            completeness=PARTIAL,
            indexed_files=processed,
            unsupported_inputs=unsupported,
            notes=tuple(notes),
        )
    return IndexResult(
        completeness=COMPLETE, indexed_files=processed, unsupported_inputs=unsupported
    )


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
    # Sending the signal is not the same as the group being gone, and the
    # caller's next act is to pack or delete the state these processes are
    # writing. ``SIGKILL`` is not refusable, so this waits on the kernel rather
    # than on a cooperating child -- but a process stuck in uninterruptible
    # sleep can still outlive it, and a group that cannot be established as
    # empty fails the build instead of being assumed gone.
    _await_group_exit(group, _REAP_TIMEOUT_SECONDS)
    if not _group_is_empty(group):
        raise ContextError(
            "local graph provider left processes running that could not be stopped; "
            "no generation was published"
        )


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


def subprocess_indexer(
    executable: str, *, repository: Path, pin: GraphifyPin
) -> Callable[[IndexRequest], IndexResult]:
    """Run a pinned provider CLI over the materialized copy, without a network.

    Kept as a factory so the lifecycle never imports or requires a graph
    package: a deployment that has installed the pin supplies the executable,
    and everything else -- including every test in this repository -- injects
    its own callable. The child sees only ``request.environment``, and it sees
    it from inside a sandbox that denies it sockets. Resolving the executable,
    checking it against the pin, and resolving the sandbox here, rather than at
    build time, means an unusable provider, an install that is not the pinned
    release, or an uncontainable host fails before a single blob is
    materialized.

    ``pin`` is taken here rather than only from each request because it is half
    of what the executable *is*: an adapter built for one release must not be a
    callable that would run whatever a later request's pin happened to name.

    The argv is the interface the adopt decision evaluated, not a guess at a
    conventional one: ``extract``, the scan target the pinned CLI requires, the
    required restrictions and then the pinned options, in the materialized copy.
    Everything the provider leaves behind is then collected and classified from
    its own report.
    """
    command = _resolved_executable(executable)
    if containment_mechanism() is None:
        raise ContextError(
            "local graph builds need an OS sandbox that denies the provider the network and "
            "the host filesystem; this host offers none that could be verified"
        )
    _verify_provider_installation(command, pin=pin)
    # The checkout root, for the same reason the state identity uses it, and
    # here it is load-bearing rather than cosmetic: both boundaries below refuse
    # what lives *inside the checkout*, and a subdirectory would narrow that
    # refusal to part of one. A provider at ``<checkout>/provider-venv`` must be
    # refused for a build run from ``<checkout>/src`` exactly as it is from the
    # root, and the exposure a run is confined to is checked the same way.
    repository = checkout_root(repository)
    runtime = _provider_read_paths(command, repository=repository)

    def run(request: IndexRequest) -> IndexResult:
        if request.pin != pin:
            # The verification above spoke for one pin; the launch below reads
            # its options and the manifest records it from another. One caller
            # passes both, so a divergence is a miswiring rather than an
            # operator's doing -- and it would publish a manifest naming a pin
            # nothing was checked against, which is the defect this check was
            # added to close.
            raise ContextError(
                "this build's provider pin is not the pin the installed provider was checked "
                "against; no generation was published"
            )
        _refuse_pre_existing_provider_state(request.source_root)
        # Before the launch, and only here. These are the bytes this build hands
        # the provider; once the child has run, the same tree also holds the
        # provider's own output, and a digest taken then would be checking the
        # provider's manifest against the provider's own leavings.
        # Classified and digested from the copy *before* the launch: after the
        # run the same tree also holds provider output, and a shebang read then
        # would be classifying whatever the provider left behind.
        inputs = (
            _provider_inputs(request.census, request.source_root)
            if request.census is not None
            else None
        )
        digests = _materialized_digests(request.source_root, request.census, inputs)
        # Built per run, because the boundary is a function of what this build
        # exposes: the materialized copy and the build's own scratch areas are
        # writable, the pinned provider's install is readable, and nothing else
        # on this host is in the child's filesystem view at all.
        sandbox = containment_prefix(
            writable=(request.source_root, *request.writable),
            readable=runtime,
            repository=repository,
        )
        # Normalized again at the point of launch, not because the pin could
        # arrive without the restrictions -- it cannot -- but because this is
        # the line that decides what the provider is actually asked to do, and
        # it should be readable here without trusting a constructor elsewhere.
        options = _extraction_options(request.pin.options)
        try:
            returncode = _run_contained(
                [*sandbox, command, _PROVIDER_EXTRACT, _PROVIDER_SCAN_TARGET, *options],
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
        output_directory = _provider_output_directory(request.source_root)
        result = _read_completeness(
            _provider_manifest(output_directory), request.census, digests, inputs
        )
        _write_private_file(request.output_path, _pack_state(output_directory))
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
    #: Inputs the provider classified as code and deterministically could not
    #: extract. Deliberately *not* folded into ``skipped_paths``, which counts
    #: tracked entries this build declined to materialize at all (symlinks,
    #: submodules, private state). Those two numbers answer different questions
    #: -- what this build withheld, and what the provider could not read --
    #: and an operator who needs to act on one cannot act on their sum.
    unsupported_inputs: int = 0

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
            "unsupported_inputs": self.unsupported_inputs,
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
            "unsupported_inputs": self.unsupported_inputs,
        }


def load_manifest(payload: Mapping[str, Any]) -> BuildManifest:
    """Validate a manifest. Every unreadable shape is a refusal, not a default.

    ``unsupported_inputs`` is accepted as optional so a generation published
    before it existed still loads. It is always written, so the only manifests
    that take the default are older ones, and ``0`` is the honest reading of
    them: that build never counted the provider's unsupported dispatch, and a
    zero says the same thing a missing key does. Nothing else is optional --
    an unrecognized key is still a refusal, so this widens what loads by
    exactly one name.
    """
    if not isinstance(payload, Mapping):
        raise ContextError("local graph manifest must be an object")
    expected = {
        "schema", "generation", "commit", "tree", "provider", "built_at",
        "tracked_files", "tracked_bytes", "census_digest", "graph_digest",
        "graph_bytes", "completeness", "skipped_paths", "indexed_files",
    }
    optional = {"unsupported_inputs"}
    present = set(payload)
    if not expected <= present or not present <= (expected | optional):
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
        unsupported_inputs=_size(
            payload.get("unsupported_inputs", 0), MAX_TRACKED_FILES
        ),
    )


def checkout_root(repository: Path) -> Path:
    """The worktree root that owns ``repository``, or the path itself.

    State identity is per checkout, not per directory. A census reads the
    commit's whole tree -- ``ls-tree --full-tree``, never the invocation
    directory's slice of it -- so ``status`` run in ``src/`` asks about exactly
    the generation ``status`` run at the root published, and must resolve to
    it. Deriving the identity from the invocation directory instead made every
    subdirectory its own workspace: a graph built at the root reported
    ``absent`` from ``src/``, and ``remove`` from there deleted nothing while
    reporting success.

    Git answers this per worktree, which is what keeps linked worktrees
    separate: each reports its own root, and each may hold a different
    revision, so they must not share a generation. A path Git cannot place --
    not a repository, a bare one, or no Git on the host -- keeps the resolved
    path it was given, which is what this derived before. Nothing is refused
    here: the verbs that need Git already fail on their own terms, and a
    lifecycle that could not even name its state would fail worse.
    """
    resolved = Path(repository).resolve()
    try:
        toplevel = _git(resolved, "rev-parse", "--show-toplevel", permit_failure=True).strip()
    except ContextError:
        return resolved
    if not toplevel:
        return resolved
    # Resolved the same way the fallback is, so one checkout has one identity
    # however it was spelled -- and so a root that was already canonical keeps
    # the workspace name its existing generations are filed under.
    return Path(os.path.realpath(toplevel))


def workspace_id(repository: Path) -> str:
    """A stable private name for one checkout.

    Derived from the resolved path so two worktrees of the same repository get
    separate state and can never read each other's generations, and hashed so
    the operator's directory layout is not spelled out in a shared location.

    The path must already be a checkout root. ``GraphStateRoot`` is the one
    place identity is derived, and it normalizes through ``checkout_root``
    first; a caller that hashes a subdirectory gets a name nothing else uses.
    """
    return hashlib.sha256(str(Path(repository).resolve()).encode()).hexdigest()[:32]


#: What a missing component means to a walk: create it, stop there, or refuse.
_MISSING_CREATE = "create"
_MISSING_STOP = "stop"
_MISSING_REFUSE = "refuse"


def _open_private_at(parent: int | None, name: str, *, missing: str, private: bool = True) -> int | None:
    """Open one directory *relative to a descriptor*, following nothing.

    ``parent`` is the descriptor the name is resolved against, so the kernel
    resolves exactly one component and ``O_NOFOLLOW`` covers all of it. That is
    the difference between checking a path and traversing one: a path opened by
    its full spelling is re-resolved from the root every time, and any ancestor
    may have become a symlink since it was last looked at.

    ``mkdir`` runs against the same descriptor for the same reason. Creating
    with ``parents=True`` from a full path would follow an ancestor that became
    a symlink between the check and the creation -- the state-root defect this
    replaces -- and no later check on the leaf can see that it happened.
    """
    unsafe = "local graph state directory is unavailable or unsafe"
    try:
        handle = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    except FileNotFoundError:
        if missing == _MISSING_STOP:
            return None
        if missing != _MISSING_CREATE:
            raise ContextError(unsafe) from None
        # Opened before it is created, rather than creating unconditionally and
        # reading the errno: this walk now traverses the whole absolute base,
        # including ancestors like ``/usr`` that exist and that nobody may
        # write. Whether such a ``mkdir`` reports ``EEXIST`` or ``EACCES``
        # first is the kernel's business, and a boundary should not rest on it.
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent)
        except FileExistsError:
            pass
        except OSError:
            raise ContextError(unsafe) from None
        try:
            handle = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        except OSError:
            # Created as a directory and already something else, or something
            # else won the race: either way this is not a directory this walk
            # may descend.
            raise ContextError(unsafe) from None
    except OSError:
        # ``ELOOP`` lands here: the component is a symlink, and a symlink is
        # not a directory this class owns however private its target may be.
        raise ContextError(unsafe) from None
    if not private:
        # An ancestor *above* the operator's private root: this class does not
        # own its mode and must not judge it. What matters there is only that
        # it was traversed as a directory rather than through a link.
        return handle
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
        #: The checkout root, not the directory the command was run from. Every
        #: verb reaches its state through this class, so normalizing here is
        #: what makes ``build`` at the root and ``status`` in ``src/`` name one
        #: workspace; callers read it back to run Git against the same root the
        #: identity came from.
        self.repository = checkout_root(repository)
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
        # a link into its ``private`` directory), so they are resolved rather
        # than refused.
        self.base = Path(os.path.realpath(base))
        self.workspace = workspace_id(self.repository)
        self.path = self.base / "graph" / self.workspace
        #: The device and inode the base was first observed as, established by
        #: the no-follow walk and re-checked by every later one. Canonicalizing
        #: at construction settles what the ancestors mean *now*; this is what
        #: notices that they stopped meaning it.
        self._identity: tuple[int, int] | None = None

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

    #: The components this class owns below the canonical base, outermost
    #: first. Spelled out one at a time because each is created and opened
    #: against its parent's descriptor: ``mkdir(parents=True)`` would both
    #: apply ``0o700`` to the leaf only -- leaving intermediates at the process
    #: umask -- and follow an ancestor that became a symlink in between.
    @property
    def _components(self) -> tuple[str, ...]:
        return ("graph", self.workspace, "generations")

    def _open_base(self, *, missing: str) -> int | None:
        """Open the private root by walking it from ``/``, following nothing.

        A single ``open(base, O_NOFOLLOW)`` is not this, and the difference is
        the whole of the defect it replaces. ``O_NOFOLLOW`` refuses only the
        *final* component; every ancestor above it is resolved by the kernel
        exactly as a symlink planted there would want. So a base whose ancestor
        was replaced by a link after construction passes the leaf check --
        because the leaf really is a directory and really is not a link. It is
        simply not the directory that was checked. Finding the deepest existing
        prefix first does not help: that prefix is still opened by its full
        absolute spelling, in one call, through whatever its ancestors have
        become.

        Every component is opened against its parent's descriptor instead, so
        the kernel resolves exactly one name at a time and ``O_NOFOLLOW``
        covers all of it. The base was canonicalized in ``__init__``, so on an
        untampered host no component of it is a link and this walk is a
        restatement of the path; a component that has become one since is
        precisely what must fail, whether or not the components below it exist
        already.

        Components above the operator's root are traversed but not judged for
        ownership or mode -- that is not this class's to own. What matters
        there is only that each was a real directory rather than a link.
        """
        parts = self.base.parts
        # The root is not a component anybody can replace, and mkdir on it is
        # meaningless; it is opened, never created.
        handle = _open_private_at(None, parts[0], missing=_MISSING_REFUSE, private=False)
        if handle is None:  # pragma: no cover - _MISSING_REFUSE raises instead
            return None
        try:
            for name in parts[1:]:
                deeper = _open_private_at(handle, name, missing=missing, private=False)
                if deeper is None:
                    os.close(handle)
                    return None
                os.close(handle)
                handle = deeper
        except BaseException:
            os.close(handle)
            raise
        info = os.fstat(handle)
        identity = (info.st_dev, info.st_ino)
        if self._identity is None:
            self._identity = identity
        elif self._identity != identity:
            # The walk was clean and still arrived somewhere else than it did
            # last time: an ancestor was swapped between two operations on the
            # same root. Refuse rather than carry on against a directory this
            # instance never checked.
            os.close(handle)
            raise ContextError("local graph state directory is unavailable or unsafe")
        return handle

    def _open_owned(self, *, depth: int) -> int | None:
        """The descriptor for one owned directory, or ``None`` if it is absent.

        This is what every mutation below holds instead of a path. An earlier
        revision revalidated the base -- compared the walk's inode against the
        one the full spelling resolved to -- immediately before each rename and
        removal, which reads as safe and is not: the check and the use are two
        separate resolutions of the same spelling, and an ancestor swapped
        between them lands the use somewhere the check never saw. Rechecking a
        path cannot close that race; not resolving the path a second time is
        what closes it.

        Unlike ``_walk`` this refuses to return a *shallower* directory when a
        component is missing: a caller asking for the generations directory
        must not silently receive the workspace directory and mutate it.
        """
        handle = self._open_base(missing=_MISSING_STOP)
        if handle is None:
            return None
        try:
            for component in self._components[:depth]:
                deeper = _open_private_at(handle, component, missing=_MISSING_STOP)
                if deeper is None:
                    os.close(handle)
                    return None
                os.close(handle)
                handle = deeper
        except BaseException:
            os.close(handle)
            raise
        return handle

    def _walk(self, *, depth: int, missing: str) -> int:
        """Descend the owned components from the base, one descriptor at a time.

        Returns the deepest descriptor reached; the caller closes it. With
        ``missing=_MISSING_STOP`` a component that does not exist ends the walk
        rather than failing it, which is what a read of state that was never
        built needs. Nothing below a component that failed its privacy check is
        ever opened, because there is no descriptor left to open it against.
        """
        opened = self._open_base(missing=missing)
        if opened is None:
            return -1
        handle = opened
        try:
            for component in self._components[:depth]:
                deeper = _open_private_at(handle, component, missing=missing)
                if deeper is None:
                    return handle
                os.close(handle)
                handle = deeper
        except BaseException:
            os.close(handle)
            raise
        return handle

    def _close_walk(self, handle: int) -> None:
        if handle >= 0:
            os.close(handle)

    def verify_private(self, *, create: bool = False) -> None:
        """Re-check ownership and mode on every directory this class owns.

        Called on every read, not only at creation: state that was loosened
        after the fact -- by a umask change, a restore, or a careless recursive
        chmod -- must fail closed rather than be trusted because it was private
        when it was written.
        """
        missing = _MISSING_CREATE if create else _MISSING_STOP
        self._close_walk(self._walk(depth=len(self._components), missing=missing))

    def ensure(self) -> None:
        """Create the private tree, refusing to place state inside a repository."""
        self._refuse_state_inside_a_repository()
        self.verify_private(create=True)

    def _refuse_state_inside_a_repository(self) -> None:
        """No generation may be written inside a repository, by any spelling.

        A lexical walk alone reads ``--state-dir /outside/link/state`` as being
        outside every repository even when ``/outside/link`` points at
        ``/repo/subdir``. The path walked here is the canonical one built in
        ``__init__``, so a symlinked ancestor that exists now is resolved before
        it is judged.

        An ancestor that does *not* exist yet cannot be resolved by anybody, and
        this check alone would miss a component created as a symlink afterwards.
        That case is answered by construction rather than by re-checking: every
        component below the base is created and opened against its parent's
        descriptor with ``O_NOFOLLOW``, so a component that is a symlink when
        the build reaches it is refused outright instead of traversed. The two
        together leave no window: what exists is resolved, and what does not
        exist yet can only be created here, by this process, as a real
        directory.
        """
        if any((parent / ".git").exists() for parent in (self.path, *self.path.parents)):
            raise ContextError("local graph state must stay outside Git repositories")

    def _ensure_lock_directory(self) -> int:
        """Create only what the lock file needs, not the generations tree.

        ``remove`` takes the same lock, and a removal that first created the
        state it was asked to delete would report success for a tree it made
        itself. Returns the descriptor of the directory the lock file lives in,
        so the lock is opened relative to the directory that was just checked
        rather than re-resolved from the root.
        """
        self._refuse_state_inside_a_repository()
        return self._walk(depth=1, missing=_MISSING_CREATE)

    def lock(self):
        """Serialize builds *and removals* for one checkout.

        Concurrent builds would race publish; a removal running beside a build
        would delete the sources, output, and generations out from under it.
        Both take this lock, so the whole set of lifecycle operations that
        mutate state for one checkout is serialized rather than just the pair
        that was obviously racy.
        """
        parent = self._ensure_lock_directory()
        try:
            handle = os.open(
                self.lock_path.name,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
        finally:
            # The lock file is opened against the descriptor the walk verified,
            # so the directory it lands in is the directory that was checked
            # and not whatever that path spells by the time this line runs.
            self._close_walk(parent)
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
        handle = _open_private_at(None, str(directory), missing=_MISSING_REFUSE)
        if handle is not None:  # _MISSING_REFUSE raises rather than returning None
            os.close(handle)
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
        # Every step below runs against a descriptor the no-follow walk opened,
        # never against a path: ``os.rename`` with ``src_dir_fd``/``dst_dir_fd``
        # resolves one component on each side, so an ancestor that is swapped
        # after the walk has nothing left to redirect.
        generations = self._walk(depth=len(self._components), missing=_MISSING_REFUSE)
        try:
            staging = "." + uuid.uuid4().hex + ".staging"
            os.mkdir(staging, mode=0o700, dir_fd=generations)
            try:
                staged = os.open(
                    staging, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=generations
                )
                try:
                    _write_private_file(ARTIFACT_NAME, artifact, dir_fd=staged)
                    _write_private_file(MANIFEST_NAME, serialized, dir_fd=staged)
                    os.fsync(staged)
                finally:
                    os.close(staged)
                os.rename(
                    staging,
                    manifest.generation,
                    src_dir_fd=generations,
                    dst_dir_fd=generations,
                )
                os.fsync(generations)
            except BaseException:
                _remove_tree_at(generations, staging, ignore_errors=True)
                raise
        finally:
            self._close_walk(generations)
        state = self._walk(depth=len(self._components) - 1, missing=_MISSING_REFUSE)
        try:
            temporary = "." + uuid.uuid4().hex + ".tmp"
            try:
                _write_private_file(
                    temporary, manifest.generation.encode() + b"\n", dir_fd=state
                )
                os.replace(temporary, CURRENT_NAME, src_dir_fd=state, dst_dir_fd=state)
                os.fsync(state)
            except BaseException:
                _unlink_at(state, temporary, ignore_errors=True)
                raise
        finally:
            self._close_walk(state)
        return manifest

    def prune(self, *, keep: str | None) -> list[str]:
        """Remove every generation but ``keep``, including crashed stagings."""
        removed: list[str] = []
        generations = self._open_owned(depth=len(self._components))
        if generations is None:
            return removed
        try:
            for name in sorted(os.listdir(generations)):
                if name == keep:
                    continue
                _remove_tree_at(generations, name, ignore_errors=True)
                if _GENERATION.fullmatch(name):
                    removed.append(name)
            os.fsync(generations)
        finally:
            self._close_walk(generations)
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
            holder = self._open_owned(depth=len(self._components) - 2)
            if holder is None:  # pragma: no cover - verify_private ran first
                return False
            try:
                # The privacy check and the deletion run against *the same*
                # descriptor, so a directory swapped in after the check cannot
                # become the directory that is deleted. Opening the workspace
                # by full path here would reintroduce the whole finding: a
                # recursive delete whose root is resolved once more, from ``/``,
                # through whatever the ancestors have become by then.
                owned = _open_private_at(holder, self.workspace, missing=_MISSING_STOP)
                if owned is None:  # pragma: no cover - existence checked above
                    return False
                try:
                    for entry in os.listdir(owned):
                        _remove_tree_at(owned, entry)
                    os.fsync(owned)
                finally:
                    os.close(owned)
                os.rmdir(self.workspace, dir_fd=holder)
                os.fsync(holder)
            finally:
                self._close_walk(holder)
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


def _write_private_file(path: Path | str, payload: bytes, *, dir_fd: int | None = None) -> None:
    """Create one 0600 file, optionally relative to an already-verified directory.

    With ``dir_fd`` the name must be a single component: the kernel resolves it
    against that descriptor and nothing above it is re-resolved, so an ancestor
    that becomes a symlink cannot redirect the write.
    """
    handle = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd
    )
    with os.fdopen(handle, "wb", closefd=False) as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(handle)
    os.close(handle)


def _unlink_at(parent: int, name: str, *, ignore_errors: bool = False) -> None:
    """Unlink one component against its parent's descriptor, following nothing."""
    try:
        os.unlink(name, dir_fd=parent)
    except FileNotFoundError:
        pass
    except OSError:
        if not ignore_errors:
            raise ContextError("local graph state directory is unavailable or unsafe") from None


def _remove_tree_at(parent: int, name: str, *, ignore_errors: bool = False) -> None:
    """Delete a tree without ever re-resolving a path from the root.

    ``shutil.rmtree`` cannot be used for this. However carefully it walks
    *below* its argument, the argument itself is a full path that the kernel
    resolves from ``/`` at the moment the call is entered -- so an ancestor
    swapped between the check and that instant sends the whole recursive
    deletion somewhere else. Re-checking the path first does not help: the
    check and the use are two resolutions of the same spelling, and the race
    lives exactly between them.

    Here every descent opens one component against its parent's descriptor with
    ``O_NOFOLLOW``, and every unlink names one component against the descriptor
    of the directory that really holds it. There is no second resolution to
    win, so there is no window to win it in. A symlink encountered anywhere in
    the tree is unlinked as the link it is and never followed.
    """
    try:
        handle = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    except FileNotFoundError:
        return
    except OSError:
        # ``ENOTDIR`` for an ordinary file, ``ELOOP`` for a symlink: both are
        # removed by name against this descriptor rather than descended into.
        _unlink_at(parent, name, ignore_errors=ignore_errors)
        return
    try:
        for entry in os.listdir(handle):
            _remove_tree_at(handle, entry, ignore_errors=ignore_errors)
        os.fsync(handle)
    finally:
        os.close(handle)
    try:
        os.rmdir(name, dir_fd=parent)
    except FileNotFoundError:
        pass
    except OSError:
        if not ignore_errors:
            raise ContextError("local graph state directory is unavailable or unsafe") from None


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
    state = GraphStateRoot(repository, root=root)
    # Read the checkout through the root its identity was derived from. The
    # census is whole-tree either way, but binding the two together means a
    # build from a subdirectory cannot bind one directory's name to another
    # directory's Git answers.
    repository = state.repository
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
                    # The same two directories the scrubbed environment points
                    # at, so what the provider is told to use and what it is
                    # allowed to write are one decision rather than two.
                    writable=(home, temporary),
                    # The very census those bytes were written from, so the
                    # indexer measures coverage against what it was given.
                    census=census,
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
                unsupported_inputs=min(
                    _size(result.unsupported_inputs, MAX_TRACKED_FILES), census.file_count
                ),
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
    repository = state.repository
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
        mechanism = containment_mechanism()
        if mechanism is None:
            record("context-graph-isolation", "fail",
                   "no OS sandbox on this host was observed denying a child process both the "
                   "network and the host filesystem; builds will refuse")
        else:
            record("context-graph-isolation", "pass",
                   "the provider would run inside a sandbox that denies it the network and "
                   "everything outside the build's own directories",
                   mechanism=mechanism.name)

    state = GraphStateRoot(repository, root=root)
    if not state.path.exists():
        record("context-graph-state", "skip", "no private local graph state exists for this checkout")
    else:
        try:
            state.verify_private()
            record("context-graph-state", "pass", "local graph state is private and operator-owned")
        except ContextError as error:
            record("context-graph-state", "fail", str(error))

    status = graph_status(state.repository, root=root, revision=revision)
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
                f"  indexed:    {manifest.indexed_files} files "
                f"({manifest.unsupported_inputs} unsupported by this provider)",
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
    "Containment",
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
    "checkout_root",
    "containment_mechanism",
    "containment_prefix",
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
