#!/usr/bin/env python3
"""Tests for the portable exclusive file lock shared by local state stores."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from code_mower import board_store, file_locks, release_campaigns


class _FakeMsvcrt:
    """A stand-in for the Windows ``msvcrt`` module.

    Records every ``locking`` call with the byte offset the descriptor was
    positioned at, so a POSIX test can still assert the Windows protocol: a
    one-byte region at offset 0, taken non-blockingly and released explicitly.
    """

    LK_NBLCK = 1
    LK_UNLCK = 0

    def __init__(self, *, contended_attempts: int = 0, always_contended: bool = False) -> None:
        self.calls: list[tuple[int, int, int]] = []
        self._remaining_contention = contended_attempts
        self._always_contended = always_contended

    def locking(self, fd: int, mode: int, nbytes: int) -> None:
        self.calls.append((mode, nbytes, os.lseek(fd, 0, os.SEEK_CUR)))
        if mode != self.LK_NBLCK:
            return
        if self._always_contended:
            raise OSError(36, "Resource deadlock avoided")
        if self._remaining_contention > 0:
            self._remaining_contention -= 1
            raise OSError(36, "Resource deadlock avoided")


class BackendSelectionTests(unittest.TestCase):
    def test_posix_backend_is_selected_when_fcntl_is_available(self) -> None:
        self.assertIsNotNone(file_locks.fcntl)
        self.assertEqual(file_locks._backend(), "posix")

    def test_windows_backend_is_selected_when_only_msvcrt_is_available(self) -> None:
        with mock.patch.object(file_locks, "fcntl", None):
            with mock.patch.object(file_locks, "msvcrt", _FakeMsvcrt()):
                self.assertEqual(file_locks._backend(), "windows")

    def test_a_build_with_neither_backend_fails_closed(self) -> None:
        with mock.patch.object(file_locks, "fcntl", None):
            with mock.patch.object(file_locks, "msvcrt", None):
                self.assertEqual(file_locks._backend(), "unsupported")
                with tempfile.TemporaryDirectory() as tmp:
                    with self.assertRaises(file_locks.FileLockError):
                        with file_locks.exclusive_file_lock(Path(tmp) / "s" / ".lock"):
                            pass


class PosixBackendTests(unittest.TestCase):
    def test_the_lock_serializes_two_threads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "state" / ".lock"
            inside = threading.Event()
            may_finish = threading.Event()
            second_entered = threading.Event()

            def hold_first() -> None:
                with file_locks.exclusive_file_lock(lock_path):
                    inside.set()
                    may_finish.wait(timeout=60)

            def take_second() -> None:
                with file_locks.exclusive_file_lock(lock_path):
                    second_entered.set()

            first = threading.Thread(target=hold_first, daemon=True)
            first.start()
            self.assertTrue(inside.wait(timeout=60))

            second = threading.Thread(target=take_second, daemon=True)
            second.start()
            # The lock is held, so the second thread cannot be inside yet.
            self.assertFalse(second_entered.wait(timeout=0.5))

            may_finish.set()
            first.join(timeout=60)
            second.join(timeout=60)
            self.assertTrue(second_entered.is_set())

    def test_the_lock_is_released_when_the_body_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "state" / ".lock"
            with self.assertRaises(RuntimeError):
                with file_locks.exclusive_file_lock(lock_path):
                    raise RuntimeError("boom")

            acquired = threading.Event()

            def acquire() -> None:
                with file_locks.exclusive_file_lock(lock_path):
                    acquired.set()

            worker = threading.Thread(target=acquire, daemon=True)
            worker.start()
            worker.join(timeout=30)
            self.assertTrue(acquired.is_set())

    def test_the_lock_is_released_when_the_holding_process_dies(self) -> None:
        """No stale-lock protocol: the OS drops a killed holder's lock."""
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "state" / ".lock"
            script = (
                "import os, sys\n"
                "sys.path.insert(0, sys.argv[1])\n"
                "from code_mower.file_locks import exclusive_file_lock\n"
                "with exclusive_file_lock(sys.argv[2]):\n"
                "    sys.stdout.write('locked')\n"
                "    sys.stdout.flush()\n"
                "    os._exit(9)\n"
            )
            proc = subprocess.run(
                [sys.executable, "-c", script, str(ROOT / "src"), str(lock_path)],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(proc.stdout, "locked")
            self.assertEqual(proc.returncode, 9)
            self.assertTrue(lock_path.is_file())

            acquired = threading.Event()

            def acquire() -> None:
                with file_locks.exclusive_file_lock(lock_path):
                    acquired.set()

            worker = threading.Thread(target=acquire, daemon=True)
            worker.start()
            worker.join(timeout=30)
            self.assertTrue(acquired.is_set())

    def test_a_contended_lock_honors_a_short_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "state" / ".lock"
            with file_locks.exclusive_file_lock(lock_path):
                with self.assertRaises(file_locks.FileLockError) as ctx:
                    with file_locks.exclusive_file_lock(
                        lock_path,
                        timeout_seconds=0.01,
                        retry_seconds=0.005,
                    ):
                        pass

            self.assertEqual(
                str(ctx.exception),
                "timed out waiting for an exclusive lock",
            )


class WindowsBackendTests(unittest.TestCase):
    """The Windows backend's protocol, exercised on any platform via a fake."""

    def _windows(self, fake: _FakeMsvcrt) -> Any:
        return mock.patch.multiple(file_locks, fcntl=None, msvcrt=fake)

    def test_an_uncontended_lock_takes_one_byte_at_offset_zero_and_unlocks(self) -> None:
        fake = _FakeMsvcrt()
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "state" / ".lock"
            with self._windows(fake):
                with file_locks.exclusive_file_lock(lock_path) as handle:
                    self.assertFalse(handle.closed)
        self.assertEqual(
            fake.calls,
            [(_FakeMsvcrt.LK_NBLCK, 1, 0), (_FakeMsvcrt.LK_UNLCK, 1, 0)],
        )

    def test_contention_retries_with_a_sleep_rather_than_spinning(self) -> None:
        fake = _FakeMsvcrt(contended_attempts=3)
        slept: list[float] = []
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "state" / ".lock"
            with self._windows(fake):
                with file_locks.exclusive_file_lock(
                    lock_path,
                    retry_seconds=0.25,
                    sleep=slept.append,
                ):
                    pass

        attempts = [call for call in fake.calls if call[0] == _FakeMsvcrt.LK_NBLCK]
        self.assertEqual(len(attempts), 4)
        # One sleep per failed attempt: the loop never retries without waiting.
        self.assertEqual(slept, [0.25, 0.25, 0.25])
        self.assertEqual(fake.calls[-1], (_FakeMsvcrt.LK_UNLCK, 1, 0))

    def test_a_permanently_held_lock_times_out_with_a_bounded_path_free_error(self) -> None:
        fake = _FakeMsvcrt(always_contended=True)
        clock = iter([0.0, 0.0, 0.4, 0.9, 5.0])
        slept: list[float] = []
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "state" / ".lock"
            with self._windows(fake):
                with self.assertRaises(file_locks.FileLockError) as ctx:
                    with file_locks.exclusive_file_lock(
                        lock_path,
                        timeout_seconds=1.0,
                        retry_seconds=0.25,
                        sleep=slept.append,
                        monotonic=lambda: next(clock),
                    ):
                        pass

            message = str(ctx.exception)
            self.assertEqual(message, "timed out waiting for an exclusive lock")
            self.assertNotIn(str(lock_path), message)
            self.assertNotIn(tmp, message)
            self.assertNotIn(".lock", message)
        # It waited between attempts instead of spinning, and never unlocked a
        # region it did not hold.
        self.assertTrue(slept)
        self.assertNotIn(_FakeMsvcrt.LK_UNLCK, [call[0] for call in fake.calls])

    def test_the_region_is_unlocked_even_when_the_body_raises(self) -> None:
        fake = _FakeMsvcrt()
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "state" / ".lock"
            with self._windows(fake):
                with self.assertRaises(RuntimeError):
                    with file_locks.exclusive_file_lock(lock_path):
                        raise RuntimeError("boom")
        self.assertEqual(fake.calls[-1], (_FakeMsvcrt.LK_UNLCK, 1, 0))


class ImportPortabilityTests(unittest.TestCase):
    """The package must import on a build with no ``fcntl`` (i.e. Windows)."""

    def test_state_store_modules_import_without_fcntl(self) -> None:
        script = (
            "import sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "\n"
            "class _BlockPosixModules:\n"
            "    def find_module(self, name, path=None):\n"
            "        return None\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name == 'fcntl':\n"
            "            raise ImportError('no fcntl on this platform')\n"
            "        return None\n"
            "\n"
            "for name in ('fcntl', 'code_mower', 'code_mower.file_locks'):\n"
            "    sys.modules.pop(name, None)\n"
            "sys.meta_path.insert(0, _BlockPosixModules())\n"
            "\n"
            "from code_mower import board_store, file_locks, release_campaigns\n"
            "assert file_locks.fcntl is None\n"
            "assert board_store.append_snapshot is not None\n"
            "assert release_campaigns.locked_campaigns_dir is not None\n"
            "sys.stdout.write('imported')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", script, str(ROOT / "src")],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "imported")

    def test_both_state_stores_share_the_one_lock_helper(self) -> None:
        self.assertIs(board_store.exclusive_file_lock, file_locks.exclusive_file_lock)
        self.assertIs(release_campaigns.exclusive_file_lock, file_locks.exclusive_file_lock)
        self.assertFalse(hasattr(board_store, "fcntl"))
        self.assertFalse(hasattr(release_campaigns, "fcntl"))


if __name__ == "__main__":
    unittest.main()
