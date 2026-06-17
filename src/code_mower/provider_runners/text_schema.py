"""Provider-neutral text and schema helpers for audit wrappers."""

from __future__ import annotations

from typing import Any, Dict, Optional, Set


def clip_text(value: str, limit: int) -> str:
    """Trim text to a stable display budget, preserving a clear truncation marker."""

    value = value.strip()
    if len(value) <= limit:
        return value
    suffix = "... [truncated]"
    return value[: max(0, limit - len(suffix))].rstrip() + suffix


def one_line(value: str, limit: int) -> str:
    """Render user/model text safely inside one-line GitHub comment fragments."""

    return clip_text(
        value.replace("\r", " ").replace("\n", " ").replace("`", "'"),
        limit,
    )


def require_exact_keys(
    value: Dict[str, Any],
    required: Set[str],
    where: str,
) -> Optional[str]:
    """Return a validation error when a structured provider object drifts."""

    keys = set(value.keys())
    missing = sorted(required - keys)
    extra = sorted(keys - required)
    if missing:
        return f"{where} missing required keys: {', '.join(missing)}"
    if extra:
        return f"{where} contains unsupported keys: {', '.join(extra)}"
    return None
