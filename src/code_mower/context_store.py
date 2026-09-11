"""Private context state and OS credential storage, independent of any provider."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import uuid
from contextlib import contextmanager
from functools import cached_property
from pathlib import Path
from typing import Any, Iterator, Protocol

from .context_contract import ContextError, _identifier
from .file_locks import FileLockError, exclusive_handle_lock

MAX_STATE_BYTES = 262_144
_CREDENTIAL_ID = re.compile(r"[a-f0-9]{32}\Z")
SERVICE = "code-mower.context.v1"


class CredentialVault(Protocol):
    def get(self, credential_id: str) -> dict[str, Any] | None: ...
    def put(self, credential_id: str, value: dict[str, Any]) -> None: ...
    def delete(self, credential_id: str) -> None: ...


def strict_json(raw: str | bytes) -> dict[str, Any]:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate field")
            result[key] = value
        return result
    try:
        if len(raw.encode("utf-8") if isinstance(raw, str) else raw) > MAX_STATE_BYTES:
            raise ValueError("oversized")
        def constant(_value):
            raise ValueError("non-finite number")
        result = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
        if not isinstance(result, dict):
            raise ValueError("not an object")
        return result
    except (ValueError, TypeError, RecursionError):
        raise ContextError("private context state is invalid; reconnect") from None


class NativeCredentialVault:
    """Select only a supported OS vault, never a configured plaintext fallback."""

    @cached_property
    def _backend(self):
        try:
            if sys.platform == "darwin":
                from keyring.backends.macOS import Keyring
            elif sys.platform.startswith("linux"):
                from keyring.backends.SecretService import Keyring
            else:
                raise ContextError("context credentials require macOS Keychain or Linux Secret Service")
            backend = Keyring()
            if backend.priority <= 0:
                raise RuntimeError("unavailable")
            return backend
        except ImportError:
            raise ContextError("install code-mower[coworker] to use the optional context connection") from None
        except ContextError:
            raise
        except Exception:
            raise ContextError("unlock a supported OS credential store before connecting context") from None

    @staticmethod
    def _key(value: str) -> str:
        if not isinstance(value, str) or not _CREDENTIAL_ID.fullmatch(value):
            raise ContextError("invalid private credential reference; reconnect")
        return value

    def get(self, credential_id: str) -> dict[str, Any] | None:
        backend, key = self._backend, self._key(credential_id)
        try:
            raw = backend.get_password(SERVICE, key)
        except Exception:
            raise ContextError("context credentials are unavailable; unlock the OS credential store") from None
        return strict_json(raw) if raw is not None else None

    def put(self, credential_id: str, value: dict[str, Any]) -> None:
        backend, key = self._backend, self._key(credential_id)
        try:
            raw = json.dumps(value, allow_nan=False, separators=(",", ":"))
            if len(raw.encode()) > MAX_STATE_BYTES:
                raise ValueError("oversized")
            backend.set_password(SERVICE, key, raw)
        except Exception:
            raise ContextError("could not save context credentials in the OS credential store") from None

    def delete(self, credential_id: str) -> None:
        backend, key = self._backend, self._key(credential_id)
        try:
            if backend.get_password(SERVICE, key) is not None:
                backend.delete_password(SERVICE, key)
        except Exception:
            raise ContextError("local access is disabled; remove its remaining credential from the OS store") from None


def default_context_root() -> Path:
    """No repository or ambient MCP configuration controls this location."""
    return Path.home() / ".local" / "share" / "code-mower" / "context"


def _private(fd: int, *, directory: bool = False) -> None:
    st = os.fstat(fd)
    valid_type = stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode)
    if (not valid_type or st.st_uid != os.getuid() or st.st_mode & 0o077
            or (not directory and st.st_nlink != 1)):
        raise ContextError("context store must be private, operator-owned, and free of linked files")


class LockedConnection:
    """Descriptor-anchored operations; use only while the connection lock is held."""

    def __init__(self, fd: int, connection: str, vault: CredentialVault):
        self._fd, self.connection, self.vault = fd, connection, vault
        self._name = connection + ".json"

    def read(self) -> dict[str, Any] | None:
        try:
            fd = os.open(self._name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self._fd)
        except FileNotFoundError:
            return None
        try:
            _private(fd)
            with os.fdopen(fd, "rb", closefd=False) as stream:
                return strict_json(stream.read(MAX_STATE_BYTES + 1))
        finally:
            os.close(fd)

    def write(self, state: dict[str, Any]) -> None:
        raw = json.dumps(state, allow_nan=False, separators=(",", ":")).encode()
        if len(raw) > MAX_STATE_BYTES:
            raise ContextError("private context state exceeds its bound")
        temporary = "." + uuid.uuid4().hex + ".tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=self._fd)
        try:
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(fd)
            os.replace(temporary, self._name, src_dir_fd=self._fd, dst_dir_fd=self._fd)
            os.fsync(self._fd)
        finally:
            os.close(fd)
            try:
                os.unlink(temporary, dir_fd=self._fd)
            except FileNotFoundError:
                pass


    def artifact(self, key: str) -> LockedConnection:
        """Private auxiliary files cannot collide with a connection alias."""
        return LockedConnection(self._fd, "." + _identifier(key), self.vault)

    def delete(self) -> None:
        try:
            fd = os.open(self._name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self._fd)
        except FileNotFoundError:
            return
        try:
            _private(fd)
            os.unlink(self._name, dir_fd=self._fd)
            os.fsync(self._fd)
        finally:
            os.close(fd)


class ContextStore:
    def __init__(self, root: Path | None = None, *, vault: CredentialVault | None = None):
        self.root = Path(root) if root is not None else default_context_root()
        self._vault = vault

    @contextmanager
    def locked(self, connection: str, *, timeout_seconds: float = 35) -> Iterator[LockedConnection]:
        name = _identifier(connection)
        if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
            raise ContextError("private context connections require supported POSIX file protections")
        try:
            if not self.root.is_absolute():
                raise ContextError("context store requires an absolute private directory outside repositories")
            resolved = self.root.resolve()
            if resolved != self.root:
                raise ContextError("context store paths must not contain symlinks or parent traversal")
            if any((parent / ".git").exists() for parent in (resolved, *resolved.parents)):
                raise ContextError("context state must stay outside Git repositories")
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                _private(fd, directory=True)
                lock_fd = os.open(name + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                                  0o600, dir_fd=fd)
                with os.fdopen(lock_fd, "a+", encoding="utf-8") as handle:
                    _private(handle.fileno())
                    with exclusive_handle_lock(handle, timeout_seconds=timeout_seconds):
                        vault = self._vault if self._vault is not None else NativeCredentialVault()
                        yield LockedConnection(fd, name, vault)
            finally:
                os.close(fd)
        except ContextError:
            raise
        except FileLockError:
            raise ContextError("context connection is busy; retry after its current operation") from None
        except OSError:
            raise ContextError("private context store is unavailable or unsafe") from None
