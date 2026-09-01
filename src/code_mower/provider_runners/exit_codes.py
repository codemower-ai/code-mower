"""Shared CLI exit-code policy for provider wrappers."""

from __future__ import annotations


def audit_exit_code(verdict: str) -> int:
    """Return the provider audit CLI exit code for a final wrapper verdict."""

    # UNKNOWN means the lane produced no trustworthy verdict and should fail
    # loudly. STALE means a newer head superseded this run after the wrapper
    # posted a requeue note, so returning success avoids alarm-grade noise while
    # the newer head's audit becomes authoritative.
    return 2 if str(verdict or "").upper() == "UNKNOWN" else 0
