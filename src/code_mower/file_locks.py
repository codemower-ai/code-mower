#!/usr/bin/env python3
"""Portable exclusive file locking for local Code Mower state directories.

Code Mower serializes its local state mutations -- the Board event store and the
release-campaign directory -- on an exclusive lock over a dedicated lock file.
Both took that lock with ``fcntl.flock`` directly, and ``fcntl`` is a POSIX-only
module: importing either caller on Windows raised ``ModuleNotFoundError`` at
import time, even though the distribution declares itself OS-independent. This
module is the one place that knows how to take the lock, so its callers stay
platform-agnostic and both are fixed by the same change.

Two backends, selected by what the interpreter actually provides:

* **POSIX** (``fcntl``): a non-blocking ``flock(LOCK_EX | LOCK_NB)`` retried on
  a bounded schedule. The kernel owns the lock, so it is dropped when the
  holding descriptor closes -- including on an uncaught exception, a
  ``SIGKILL``, or an abrupt process exit. There is no stale-lock protocol or
  owner/pid bookkeeping; a crashed holder blocks nobody.
* **Windows** (``msvcrt``): ``msvcrt.locking`` over a single byte at offset 0.
  A one-byte region is locked rather than the whole file because Windows region
  locks are byte-range based and locking past end-of-file is legal, so an empty
  lock file needs no content and the region never has to be resized. Windows
  releases a file's locks when its handle closes, including when the process
  dies, so process-death release semantics match POSIX.

  ``msvcrt`` offers no blocking primitive with a caller-chosen wait: ``LK_LOCK``
  retries ten times at one-second intervals and then fails. So the non-blocking
  ``LK_NBLCK`` is retried on a bounded schedule that *sleeps* between attempts
  rather than spinning, and raises a bounded :class:`FileLockError` once the
  deadline passes. The unlock is issued from a ``finally`` block at the same
  offset the lock was taken at, so an exception in the caller's body cannot
  leave the region locked while the process keeps running.

Errors raised here never carry the lock path or any other filesystem location:
they surface through commands that must stay metadata-only.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Callable, Iterator

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows and by patching
    fcntl = None  # type: ignore[assignment]

try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - exercised on POSIX
    msvcrt = None  # type: ignore[assignment]


# Windows-only retry schedule. The wait is bounded so a wedged holder surfaces a
# clear error instead of hanging a command forever, and long enough that an
# ordinary applied campaign run -- which can invoke a provider adapter while
# holding the lock -- is never cut short while it is still making progress.
DEFAULT_LOCK_TIMEOUT_SECONDS = 900.0
DEFAULT_LOCK_RETRY_SECONDS = 0.05


class FileLockError(RuntimeError):
    """Raised when an exclusive file lock cannot be acquired.

    The message is deliberately bounded and path-free.
    """


def _backend() -> str:
    """Name the locking backend this interpreter provides.

    The module globals are read here rather than captured at import time, so a
    test can select either backend with ``mock.patch.object``.
    """
    if fcntl is not None:
        return "posix"
    if msvcrt is not None:
        return "windows"
    return "unsupported"


def _acquire_posix(
    handle: IO[str],
    *,
    timeout_seconds: float,
    retry_seconds: float,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> None:
    deadline = monotonic() + max(timeout_seconds, 0.0)
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise FileLockError("timed out waiting for an exclusive lock") from None
            sleep(min(max(retry_seconds, 0.0), remaining))


def _release_posix(handle: IO[str]) -> None:
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _acquire_windows(
    handle: IO[str],
    *,
    timeout_seconds: float,
    retry_seconds: float,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> None:
    deadline = monotonic() + max(timeout_seconds, 0.0)
    while True:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            # Held by another handle. Check the deadline first so a zero timeout
            # fails immediately, then sleep before retrying -- a bare retry loop
            # would pin a core for the whole wait.
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise FileLockError("timed out waiting for an exclusive lock") from None
            sleep(min(max(retry_seconds, 0.0), remaining))


def _release_windows(handle: IO[str]) -> None:
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def exclusive_file_lock(
    lock_path: str | Path,
    *,
    timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    retry_seconds: float = DEFAULT_LOCK_RETRY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> Iterator[IO[str]]:
    """Hold an exclusive lock on ``lock_path`` for the duration of the block.

    The open file object is yielded, so a caller can keep using the very
    descriptor it locked. The lock is released and the file closed on the way
    out, including when the body raises.

    ``timeout_seconds``, ``retry_seconds``, ``sleep``, and ``monotonic`` bound
    acquisition on both backends. The defaults preserve the long wait allowed
    for campaign adapters, while callers with their own deadline can pass the
    remaining budget.
    """
    backend = _backend()
    if backend == "unsupported":  # pragma: no cover - no such CPython build
        raise FileLockError("no supported file-locking backend is available")

    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        if backend == "posix":
            _acquire_posix(
                handle,
                timeout_seconds=timeout_seconds,
                retry_seconds=retry_seconds,
                sleep=sleep,
                monotonic=monotonic,
            )
        else:
            _acquire_windows(
                handle,
                timeout_seconds=timeout_seconds,
                retry_seconds=retry_seconds,
                sleep=sleep,
                monotonic=monotonic,
            )
        try:
            yield handle
        finally:
            if backend == "posix":
                _release_posix(handle)
            else:
                _release_windows(handle)
