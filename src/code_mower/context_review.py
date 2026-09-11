"""Metadata-only review input revisions; no private provider state or SDK imports."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

INPUT_MARKER = "CODE_MOWER_CONTEXT_INPUT"
REVIEW_MARKER = "CODE_MOWER_CONTEXT_REVIEW"
INPUT_HEADER = "Code Mower context input"
_MARKERS = {name: re.compile(r"<!--\s*" + name + r":\s*(.*?)\s*-->", re.DOTALL)
            for name in (INPUT_MARKER, REVIEW_MARKER)}
_REVISION = re.compile(r"[a-f0-9]{32}\Z")
_SHA = re.compile(r"[a-f0-9]{40}\Z")


def required_for_checkout(config_path, *, fallback=False):
    """Read current policy from a trusted gate checkout, never from PR files.

    Absent context avoids parsing unrelated settings. A selected but malformed
    policy fails closed. The generated flag is only a missing-file fallback.
    """
    path = Path(config_path)
    try:
        if not path.exists():
            return fallback
        if path.is_symlink():
            return True
        text = path.read_text(encoding='utf-8')
        if not re.search(r'^\s*context\s*:', text, re.MULTILINE):
            return False
        if __package__:
            from .yaml_subset import _YamlSubsetParser
        else:  # pragma: no cover - standalone copied helper
            from yaml_subset import _YamlSubsetParser
        config = _YamlSubsetParser(text).parse()
        if not isinstance(config, dict):
            return True
        context = config.get('context')
        if context is None:
            return False
        if not isinstance(context, dict) or type(context.get('required', False)) is not bool:
            return True
        return context.get('required', False)
    except (OSError, ValueError, TypeError, RecursionError, ImportError):
        return True


def _utc(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("invalid context expiry")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("invalid context expiry")
    return dt


def validate(value: Any, *, review: bool = False) -> dict:
    fields = {"revision", "head", "required", "state", "expires_at"}
    if not isinstance(value, dict) or value.keys() != fields:
        raise ValueError("invalid context revision metadata")
    if (not isinstance(value["revision"], str) or not _REVISION.fullmatch(value["revision"])
            or not isinstance(value["head"], str) or not _SHA.fullmatch(value["head"])
            or type(value["required"]) is not bool
            or value["state"] not in ("available", "optional_unavailable", "required_unavailable")):
        raise ValueError("invalid context revision metadata")
    if value["state"] == "optional_unavailable" and value["required"]:
        raise ValueError("required context cannot be optional")
    _utc(value["expires_at"])
    return dict(value)


def marker(value: Mapping[str, Any], *, review: bool = False) -> str:
    name = REVIEW_MARKER if review else INPUT_MARKER
    return "<!-- " + name + ": " + json.dumps(validate(dict(value), review=review), sort_keys=True, separators=(",", ":")) + " -->"


def parse(body: str, *, review: bool = False) -> dict | None:
    matches = _MARKERS[REVIEW_MARKER if review else INPUT_MARKER].findall(body)
    if not matches:
        return None
    if len(matches) != 1 or len(matches[0]) > 1024:
        raise ValueError("ambiguous context revision marker")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate context metadata")
            result[key] = value
        return result
    return validate(json.loads(matches[0], object_pairs_hook=unique), review=review)


def latest_input(comments: Sequence[Mapping[str, Any]], *, authorities: Sequence[str]) -> dict | None:
    """Only configured control authorities may advance a work order's input.

    Malformed latest declarations fail closed. A newer head does not silently
    drop required context: its orchestrator must explicitly bind that head.
    """
    allowed = {str(item).strip().lower() for item in authorities}
    candidates = []
    for index, comment in enumerate(comments):
        author = str((comment.get("user") or {}).get("login") or "").lower()
        body = str(comment.get("body") or "")
        if author not in allowed or not body.startswith(INPUT_HEADER + "\n"):
            continue
        try:
            comment_id = int(comment.get("id") or 0)
        except (TypeError, ValueError):
            comment_id = 0
        key = (str(comment.get("updated_at") or comment.get("created_at") or ""), comment_id, index)
        candidates.append((key, body))
    if not candidates:
        return None
    value = parse(max(candidates, key=lambda item: item[0])[1])
    if value is None:
        raise ValueError("malformed current context declaration")
    return value


def review_matches(body: str, current: Mapping[str, Any], *, head: str, now=None) -> bool:
    """Match current code and evidence together; never upgrade UNKNOWN to PASS."""
    try:
        expected = validate(dict(current))
        observed = parse(body, review=True)
        return (observed == expected and expected["head"] == head
                and expected["state"] != "required_unavailable"
                and _utc(expected["expires_at"]) > (now or datetime.now(timezone.utc)))
    except (TypeError, ValueError, RecursionError):
        return False
