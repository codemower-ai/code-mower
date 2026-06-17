"""Provider probe JSON parsing helpers."""

from __future__ import annotations

import json
from typing import Any, Mapping


def parse_probe_json(output: str) -> tuple[Mapping[str, Any] | None, dict[str, Any]]:
    text = output.strip()
    if not text:
        return None, {"json_parsed": False, "json_error": "empty_output"}

    candidates = [text]
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])

    last_error = "json"
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = str(exc)
            continue
        if isinstance(payload, Mapping):
            return payload, {
                "json_parsed": True,
                "json_extracted": candidate != text,
            }
        return None, {"json_parsed": False, "json_error": "not_object"}
    return None, {"json_parsed": False, "json_error": last_error}


def json_field(payload: Mapping[str, Any], dotted_field: str) -> Any:
    value: Any = payload
    for part in dotted_field.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


__all__ = ("json_field", "parse_probe_json")
