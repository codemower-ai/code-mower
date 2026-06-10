#!/usr/bin/env python3
"""Create and report the controlled Python environment for Code Mower tools.

This script is deliberately compatible with old ambient Python interpreters.
It exists so a checkout whose `python3` is 3.7, 3.8, or 3.9 can still find a
new enough interpreter, create `.code-mower-venv`, and run Code Mower tests from
that controlled environment.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence


MIN_PYTHON = (3, 11)
DEFAULT_VENV = ".code-mower-venv"
DEFAULT_REQUIREMENTS = "tools/code_mower_requirements.txt"
PYTHON_ENV = "CODE_MOWER_PYTHON"
DEFAULT_LOCK_TIMEOUT_SECONDS = 120.0
DEFAULT_LOCK_POLL_SECONDS = 0.2
DEFAULT_LOCK_STALE_SECONDS = 30 * 60.0
LOCK_OWNER_FILE = "owner.json"

RunFn = Callable[..., subprocess.CompletedProcess]
WhichFn = Callable[[str], Optional[str]]


@dataclass(frozen=True)
class PythonInfo:
    command: str
    executable: str
    version: tuple[int, int, int]

    @property
    def version_text(self) -> str:
        return ".".join(str(part) for part in self.version)

    @property
    def supported(self) -> bool:
        return self.version[:2] >= MIN_PYTHON


@dataclass(frozen=True)
class BootstrapResult:
    repo_root: Path
    venv_dir: Path
    venv_python: Path
    base_python: PythonInfo
    venv_python_info: PythonInfo
    requirements: Path
    created: bool
    installed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "repo_root": str(self.repo_root),
            "venv_dir": str(self.venv_dir),
            "venv_python": str(self.venv_python),
            "base_python": {
                "command": self.base_python.command,
                "executable": self.base_python.executable,
                "version": self.base_python.version_text,
            },
            "venv_python_version": self.venv_python_info.version_text,
            "requirements": str(self.requirements),
            "created": self.created,
            "installed": self.installed,
            "min_python": ".".join(str(part) for part in MIN_PYTHON),
        }


@dataclass(frozen=True)
class VenvLock:
    lock_dir: Path
    owner_id: str
    stop_event: threading.Event
    heartbeat_thread: threading.Thread


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _run(
    args: Sequence[str],
    *,
    runner: RunFn = subprocess.run,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    return runner(
        list(args),
        capture_output=capture_output,
        text=True,
        check=False,
    )


def probe_python(command: str, *, runner: RunFn = subprocess.run) -> PythonInfo:
    code = (
        "import json, sys; "
        "print(json.dumps({'executable': sys.executable, "
        "'version': list(sys.version_info[:3])}))"
    )
    result = _run(
        [command, "-c", code],
        runner=runner,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"{command}: version probe failed: {detail}")
    try:
        payload = json.loads(result.stdout)
        version = tuple(int(part) for part in payload["version"])
        executable = str(payload["executable"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{command}: invalid version probe output") from exc
    if len(version) != 3:
        raise RuntimeError(f"{command}: invalid Python version tuple: {version!r}")
    return PythonInfo(command=command, executable=executable, version=version)


def _resolve_candidate(command: str, *, which: WhichFn = shutil.which) -> str | None:
    if os.sep in command or (os.altsep and os.altsep in command):
        return command if Path(command).exists() else None
    return which(command)


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def candidate_commands(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    env = os.environ if environ is None else environ
    explicit = env.get(PYTHON_ENV)
    if explicit:
        return (explicit,)
    return tuple(
        _dedupe(
            (
                sys.executable,
                "python3.13",
                "python3.12",
                "python3.11",
                "python3",
                "/opt/homebrew/bin/python3",
                "/usr/local/bin/python3",
                "/usr/bin/python3",
            )
        )
    )


def find_supported_python(
    *,
    environ: Mapping[str, str] | None = None,
    candidates: Sequence[str] | None = None,
    runner: RunFn = subprocess.run,
    which: WhichFn = shutil.which,
) -> PythonInfo:
    env = os.environ if environ is None else environ
    explicit = bool(env.get(PYTHON_ENV))
    errors: list[str] = []
    for command in candidates or candidate_commands(env):
        resolved = _resolve_candidate(command, which=which)
        if not resolved:
            errors.append(f"{command}: not found")
            continue
        try:
            info = probe_python(resolved, runner=runner)
        except RuntimeError as exc:
            errors.append(str(exc))
            continue
        if info.supported:
            return info
        errors.append(f"{resolved}: Python {info.version_text} is below 3.11")
        if explicit:
            break

    hint = (
        f"Set {PYTHON_ENV}=/path/to/python3.11+ or install Python 3.11+."
    )
    detail = "; ".join(errors[-6:]) if errors else "no candidates tried"
    raise RuntimeError(f"no supported Python found ({detail}). {hint}")


def venv_python_path(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def venv_lock_dir(venv_dir: Path) -> Path:
    return venv_dir.parent / f"{venv_dir.name}.lock"


def _venv_lock_owner_path(lock_dir: Path) -> Path:
    return lock_dir / LOCK_OWNER_FILE


def _acquire_venv_lock(
    lock_dir: Path,
    *,
    timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    poll_seconds: float = DEFAULT_LOCK_POLL_SECONDS,
    stale_seconds: float = DEFAULT_LOCK_STALE_SECONDS,
) -> VenvLock:
    lock_dir.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    while True:
        try:
            lock_dir.mkdir()
        except FileExistsError:
            if _remove_stale_venv_lock(lock_dir, stale_seconds=stale_seconds):
                continue
            if time.monotonic() - started >= timeout_seconds:
                raise RuntimeError(
                    "timed out waiting for Code Mower venv lock: "
                    f"{lock_dir}"
                )
            time.sleep(poll_seconds)
            continue
        owner_id = f"{os.getpid()}-{uuid.uuid4().hex}"
        try:
            _venv_lock_owner_path(lock_dir).write_text(
                json.dumps(
                    {
                        "owner_id": owner_id,
                        "pid": os.getpid(),
                        "created_at": time.time(),
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError:
            lock_dir.rmdir()
            raise
        return _start_venv_lock_heartbeat(
            lock_dir,
            owner_id=owner_id,
            stale_seconds=stale_seconds,
        )


def _start_venv_lock_heartbeat(
    lock_dir: Path,
    *,
    owner_id: str,
    stale_seconds: float,
) -> VenvLock:
    stop_event = threading.Event()
    interval = 30.0
    if stale_seconds > 0:
        interval = max(DEFAULT_LOCK_POLL_SECONDS, min(stale_seconds / 4.0, 30.0))

    def heartbeat() -> None:
        while not stop_event.wait(interval):
            try:
                os.utime(_venv_lock_owner_path(lock_dir), None)
                os.utime(lock_dir, None)
            except FileNotFoundError:
                return

    thread = threading.Thread(
        target=heartbeat,
        name="code-mower-venv-lock-heartbeat",
        daemon=True,
    )
    thread.start()
    return VenvLock(
        lock_dir=lock_dir,
        owner_id=owner_id,
        stop_event=stop_event,
        heartbeat_thread=thread,
    )


def _read_venv_lock_owner_id(owner_path: Path) -> str | None:
    try:
        payload = json.loads(owner_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    owner_id = payload.get("owner_id")
    return str(owner_id) if owner_id else None


def _lock_mtime(lock_dir: Path, owner_path: Path) -> float:
    mtimes = [lock_dir.stat().st_mtime]
    try:
        mtimes.append(owner_path.stat().st_mtime)
    except FileNotFoundError:
        pass
    return max(mtimes)


def _remove_stale_venv_lock(lock_dir: Path, *, stale_seconds: float) -> bool:
    if stale_seconds <= 0:
        return False
    owner_path = _venv_lock_owner_path(lock_dir)
    try:
        inspected_owner_id = _read_venv_lock_owner_id(owner_path)
        lock_mtime = _lock_mtime(lock_dir, owner_path)
    except FileNotFoundError:
        return False
    if time.time() - lock_mtime < stale_seconds:
        return False
    try:
        current_owner_id = _read_venv_lock_owner_id(owner_path)
        if current_owner_id != inspected_owner_id:
            return False
        if time.time() - _lock_mtime(lock_dir, owner_path) < stale_seconds:
            return False
        if owner_path.exists():
            owner_path.unlink()
        lock_dir.rmdir()
    except (FileNotFoundError, OSError):
        return False
    return True


def _release_venv_lock(lock: VenvLock) -> None:
    lock.stop_event.set()
    lock.heartbeat_thread.join(timeout=1)
    owner_path = _venv_lock_owner_path(lock.lock_dir)
    try:
        payload = json.loads(owner_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (OSError, json.JSONDecodeError):
        return
    if payload.get("owner_id") != lock.owner_id:
        return
    try:
        owner_path.unlink()
        lock.lock_dir.rmdir()
    except FileNotFoundError:
        return
    except OSError:
        return


def _safe_to_recreate(repo_root: Path, venv_dir: Path) -> bool:
    resolved_repo = repo_root.resolve()
    resolved_venv = venv_dir.resolve()
    if resolved_venv == resolved_repo:
        return False
    if resolved_venv == Path(resolved_venv.anchor):
        return False
    try:
        resolved_venv.relative_to(resolved_repo)
    except ValueError:
        return False
    if "venv" not in resolved_venv.name:
        return False
    return (resolved_venv / "pyvenv.cfg").is_file()


def _ensure_venv(
    *,
    repo_root: Path,
    venv_dir: Path,
    recreate: bool,
    runner: RunFn,
    which: WhichFn,
) -> tuple[Path, PythonInfo, bool]:
    """Return the venv Python, selected base Python, and creation flag."""
    venv_python = venv_python_path(venv_dir)
    if venv_python.exists() and not recreate:
        info = probe_python(str(venv_python), runner=runner)
        if not info.supported:
            raise RuntimeError(
                f"{venv_python} is Python {info.version_text}; remove "
                f"{venv_dir} or rerun with --recreate"
            )
        try:
            base_python = find_supported_python(runner=runner, which=which)
        except RuntimeError:
            base_python = info
        return venv_python, base_python, False

    if recreate and venv_dir.exists():
        if not _safe_to_recreate(repo_root, venv_dir):
            raise RuntimeError(f"refusing to recreate unsafe venv path: {venv_dir}")
        shutil.rmtree(venv_dir)

    base_python = find_supported_python(runner=runner, which=which)
    result = _run(
        [base_python.executable, "-m", "venv", str(venv_dir)],
        runner=runner,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"failed to create {venv_dir}: {detail}")
    venv_info = probe_python(str(venv_python), runner=runner)
    if not venv_info.supported:
        raise RuntimeError(
            f"{venv_python} is Python {venv_info.version_text}; expected 3.11+"
        )
    return venv_python, base_python, True


def bootstrap(
    *,
    repo_root: Path | None = None,
    venv_dir: Path | None = None,
    requirements: Path | None = None,
    recreate: bool = False,
    install: bool = True,
    runner: RunFn = subprocess.run,
    which: WhichFn = shutil.which,
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    lock_poll_seconds: float = DEFAULT_LOCK_POLL_SECONDS,
    lock_stale_seconds: float = DEFAULT_LOCK_STALE_SECONDS,
) -> BootstrapResult:
    root = (repo_root or _repo_root()).resolve()
    venv = venv_dir or (root / DEFAULT_VENV)
    if not venv.is_absolute():
        venv = root / venv
    req = requirements or (root / DEFAULT_REQUIREMENTS)
    if not req.is_absolute():
        req = root / req

    lock_dir = venv_lock_dir(venv)
    lock = _acquire_venv_lock(
        lock_dir,
        timeout_seconds=lock_timeout_seconds,
        poll_seconds=lock_poll_seconds,
        stale_seconds=lock_stale_seconds,
    )
    try:
        venv_python, base_python, created = _ensure_venv(
            repo_root=root,
            venv_dir=venv,
            recreate=recreate,
            runner=runner,
            which=which,
        )
        venv_info = probe_python(str(venv_python), runner=runner)
        installed = False
        if install:
            if not req.exists():
                raise RuntimeError(f"requirements file not found: {req}")
            result = _run(
                [str(venv_python), "-m", "pip", "install", "-r", str(req)],
                runner=runner,
                capture_output=True,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()
                raise RuntimeError(
                    f"failed to install Code Mower requirements: {detail}"
                )
            installed = True
    finally:
        _release_venv_lock(lock)
    return BootstrapResult(
        repo_root=root,
        venv_dir=venv,
        venv_python=venv_python,
        base_python=base_python,
        venv_python_info=venv_info,
        requirements=req,
        created=created,
        installed=installed,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=str(_repo_root()))
    parser.add_argument("--venv", default=DEFAULT_VENV)
    parser.add_argument("--requirements", default=DEFAULT_REQUIREMENTS)
    parser.add_argument("--recreate", action="store_true")
    parser.add_argument("--no-install", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--print-python", action="store_true")
    args = parser.parse_args(argv)

    try:
        result = bootstrap(
            repo_root=Path(args.repo_root),
            venv_dir=Path(args.venv),
            requirements=Path(args.requirements),
            recreate=args.recreate,
            install=not args.no_install,
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.print_python:
        print(result.venv_python)
    elif args.json:
        print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    else:
        print(f"Code Mower Python: {result.venv_python}")
        print(f"Version: {result.venv_python_info.version_text}")
        print(f"Requirements: {result.requirements}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
