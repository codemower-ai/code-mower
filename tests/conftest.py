from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Any

import pytest


REAL_AUDIT_CACHE = Path.home() / ".cache" / "code-mower-audits"
LOCAL_TEST_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _cache_snapshot(root: Path) -> dict[str, tuple[int, int]]:
    if not root.exists():
        return {}
    snapshot: dict[str, tuple[int, int]] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        snapshot[str(path)] = (stat.st_mtime_ns, stat.st_size)
    return snapshot


@pytest.fixture(scope="session", autouse=True)
def _real_audit_cache_guard() -> Any:
    before = _cache_snapshot(REAL_AUDIT_CACHE)
    yield
    after = _cache_snapshot(REAL_AUDIT_CACHE)
    assert after == before, "tests mutated the real Code Mower audit cache"


@pytest.fixture(autouse=True)
def _isolate_audit_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    cache = tmp_path / "xdg-cache"
    home.mkdir()
    cache.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.setenv(
        "CODE_MOWER_VERDICT_ARTIFACT_DIR",
        str(cache / "code-mower-audits" / "verdicts"),
    )
    monkeypatch.setenv(
        "CODE_MOWER_VERDICT_QUARANTINE_DIR",
        str(cache / "code-mower-audits" / "quarantine" / "verdicts"),
    )
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    real_create_connection = socket.create_connection

    def guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> socket.socket:
        host = address[0] if isinstance(address, tuple) and address else str(address)
        if os.environ.get("CODE_MOWER_ALLOW_TEST_NETWORK") == "1" or host in LOCAL_TEST_HOSTS:
            return real_create_connection(address, *args, **kwargs)
        raise AssertionError(f"test attempted non-local network connection to {host!r}")

    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
