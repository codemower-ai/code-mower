"""Stable identifiers for calibration artifacts."""

from __future__ import annotations

import hashlib
import re
from typing import Any


SAFE_SLUG_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_slug(value: Any, fallback: str = "item") -> str:
    text = SAFE_SLUG_RE.sub("-", str(value or "").strip()).strip("._-")
    while ".." in text:
        text = text.replace("..", ".")
    text = text.strip("._-")
    return text or fallback


def head_slug(value: Any) -> str:
    head = str(value or "").strip()
    if not head:
        return ""
    safe_head = safe_slug(head, "")
    digest = hashlib.sha256(head.encode("utf-8")).hexdigest()[:12]
    return f"{safe_head[:12] or digest}-{digest}"
