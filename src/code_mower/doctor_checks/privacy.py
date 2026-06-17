"""Privacy-preserving doctor output helpers."""

from __future__ import annotations

from typing import Any


def auth_probe_output_detail(output: str) -> dict[str, Any]:
    """Return non-content diagnostics for auth probes.

    Doctor probes sometimes receive provider auth output containing account
    names, emails, scopes, or token-adjacent diagnostics. Keep only shape
    metadata so reports can explain that output existed without storing it.
    """

    text = output.strip()
    return {
        "output_redacted": bool(text),
        "output_line_count": len(text.splitlines()) if text else 0,
    }
