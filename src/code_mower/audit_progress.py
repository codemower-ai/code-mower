#!/usr/bin/env python3
"""Small progress helpers for local Code Mower audit wrappers.

The wrappers can run a reviewer subprocess for many minutes. This module keeps
status reporting generic: providers pass a lane name and phase name, while the
core emits elapsed-time progress without knowing provider-specific semantics.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence, TextIO


RunFn = Callable[..., subprocess.CompletedProcess]

SENSITIVE_VALUE_FLAGS = {
    "--api-key",
    "--apikey",
    "--authorization",
    "--bearer-token",
    "--github-token",
    "--password",
    "--secret",
    "--token",
}
SENSITIVE_KEYWORDS = (
    "api-key",
    "apikey",
    "api_key",
    "authorization",
    "bearer",
    "password",
    "passwd",
    "secret",
    "token",
)
SENSITIVE_ASSIGNMENT_PATTERN = re.compile(
    r"\b([A-Za-z0-9_.-]*(?:api[-_]?key|apikey|authorization|bearer|password|passwd|secret|token)[A-Za-z0-9_.-]*)=([^\s&]+)",
    flags=re.IGNORECASE,
)
SENSITIVE_FLAG_PATTERN = re.compile(
    r"(--(?:api-key|apikey|authorization|bearer-token|github-token|password|secret|token)\s+)(\S+)",
    flags=re.IGNORECASE,
)


def _looks_sensitive_key(value: str) -> bool:
    normalized = value.lower().replace("_", "-")
    return any(keyword.replace("_", "-") in normalized for keyword in SENSITIVE_KEYWORDS)


def redact_command(command: Sequence[str]) -> list[str]:
    """Mask common secret-bearing command arguments before progress logging."""
    redacted: list[str] = []
    mask_next = False

    for part in command:
        if mask_next:
            redacted.append("REDACTED")
            mask_next = False
            continue

        key, sep, _value = part.partition("=")
        if sep and _looks_sensitive_key(key):
            redacted.append(f"{key}=REDACTED")
            continue

        redacted.append(part)
        if part in SENSITIVE_VALUE_FLAGS:
            mask_next = True

    return redacted


def redact_detail(value: str) -> str:
    """Mask common secret-bearing key/value fragments in free-form detail text."""
    value = SENSITIVE_ASSIGNMENT_PATTERN.sub(lambda match: f"{match.group(1)}=REDACTED", value)
    return SENSITIVE_FLAG_PATTERN.sub(lambda match: f"{match.group(1)}REDACTED", value)


def format_command(command: Sequence[str], *, max_args: int = 16) -> str:
    """Return a compact shell-ish command string for progress logs."""
    redacted = redact_command(command)
    shown = list(redacted[:max_args])
    suffix = ["..."] if len(redacted) > max_args else []
    return " ".join(shlex.quote(part) for part in [*shown, *suffix])


def format_detail(value: str) -> str:
    """Return a single-token detail value for key=value progress logs."""
    return urllib.parse.quote(redact_detail(value), safe="/:._@=,+-#")


@dataclass
class AuditProgress:
    lane: str
    heartbeat_seconds: float = 60.0
    stream: Optional[TextIO] = None
    started_at: float = 0.0

    def __post_init__(self) -> None:
        if self.started_at <= 0:
            self.started_at = time.monotonic()

    def emit(
        self,
        phase: str,
        *,
        status: str,
        detail: str = "",
        elapsed: Optional[float] = None,
    ) -> None:
        if elapsed is None:
            elapsed = time.monotonic() - self.started_at
        stream = self.stream if self.stream is not None else sys.stderr
        detail_part = f" detail={format_detail(detail)}" if detail else ""
        print(
            f"audit-progress lane={self.lane} phase={phase} "
            f"status={status} elapsed={elapsed:.0f}s{detail_part}",
            file=stream,
            flush=True,
        )


def run_subprocess_with_progress(
    command: Sequence[str],
    *,
    progress: AuditProgress,
    phase: str,
    run: RunFn = subprocess.run,
    heartbeat_seconds: Optional[float] = None,
    redacted_command: Optional[Sequence[str]] = None,
    **kwargs: Any,
) -> subprocess.CompletedProcess:
    """Run a subprocess while emitting start/heartbeat/finish progress.

    The `run` callable is injectable so existing wrapper tests can monkeypatch
    their module-local `subprocess.run` without learning about this helper.
    """
    interval = progress.heartbeat_seconds if heartbeat_seconds is None else heartbeat_seconds
    display_command = format_command(redacted_command or command)
    started_at = time.monotonic()
    progress.emit(phase, status="start", detail=display_command, elapsed=0)

    stop = threading.Event()
    heartbeat: Optional[threading.Thread] = None

    if interval > 0:

        def heartbeat_loop() -> None:
            while not stop.wait(interval):
                progress.emit(
                    phase,
                    status="running",
                    detail=display_command,
                    elapsed=time.monotonic() - started_at,
                )

        heartbeat = threading.Thread(
            target=heartbeat_loop,
            name=f"{progress.lane}-{phase}-progress",
            daemon=True,
        )
        heartbeat.start()

    try:
        result = run(list(command), **kwargs)
    except subprocess.TimeoutExpired:
        progress.emit(
            phase,
            status="timeout",
            detail=display_command,
            elapsed=time.monotonic() - started_at,
        )
        raise
    except Exception:
        progress.emit(
            phase,
            status="error",
            detail=display_command,
            elapsed=time.monotonic() - started_at,
        )
        raise
    finally:
        stop.set()
        if heartbeat is not None:
            heartbeat.join(timeout=1)

    progress.emit(
        phase,
        status=f"exit-{result.returncode}",
        detail=display_command,
        elapsed=time.monotonic() - started_at,
    )
    return result
