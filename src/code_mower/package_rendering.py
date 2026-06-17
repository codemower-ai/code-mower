"""Generated package rendering helpers."""

from __future__ import annotations

import json
from typing import Any, Mapping


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value))


def _yaml_empty_container(value: Any) -> str | None:
    if isinstance(value, Mapping) and not value:
        return "{}"
    if isinstance(value, (list, tuple)) and not value:
        return "[]"
    return None


def _yaml_inline_sequence(value: Any) -> str | None:
    if not isinstance(value, (list, tuple)):
        return None
    if any(isinstance(item, (Mapping, list, tuple)) for item in value):
        return None
    return "[" + ", ".join(_yaml_scalar(item) for item in value) + "]"


def _render_yaml(value: Any, *, indent: int = 0) -> list[str]:
    prefix = " " * indent
    if isinstance(value, Mapping):
        lines: list[str] = []
        for key, item in value.items():
            empty_container = _yaml_empty_container(item)
            if empty_container is not None:
                lines.append(f"{prefix}{key}: {empty_container}")
            elif isinstance(item, (Mapping, list, tuple)):
                lines.append(f"{prefix}{key}:")
                lines.extend(_render_yaml(item, indent=indent + 2))
            else:
                lines.append(f"{prefix}{key}: {_yaml_scalar(item)}")
        return lines
    if isinstance(value, (list, tuple)):
        lines = []
        for item in value:
            empty_container = _yaml_empty_container(item)
            inline_sequence = _yaml_inline_sequence(item)
            if empty_container is not None:
                lines.append(f"{prefix}- {empty_container}")
            elif inline_sequence is not None:
                lines.append(f"{prefix}- {inline_sequence}")
            elif isinstance(item, Mapping):
                items = list(item.items())
                first_key, first_value = items[0]
                first_empty = _yaml_empty_container(first_value)
                if first_empty is not None:
                    lines.append(f"{prefix}- {first_key}: {first_empty}")
                elif isinstance(first_value, (Mapping, list, tuple)):
                    lines.append(f"{prefix}- {first_key}:")
                    lines.extend(_render_yaml(first_value, indent=indent + 4))
                else:
                    lines.append(f"{prefix}- {first_key}: {_yaml_scalar(first_value)}")
                for key, nested in items[1:]:
                    nested_empty = _yaml_empty_container(nested)
                    if nested_empty is not None:
                        lines.append(f"{' ' * (indent + 2)}{key}: {nested_empty}")
                    elif isinstance(nested, (Mapping, list, tuple)):
                        lines.append(f"{' ' * (indent + 2)}{key}:")
                        lines.extend(_render_yaml(nested, indent=indent + 4))
                    else:
                        lines.append(
                            f"{' ' * (indent + 2)}{key}: {_yaml_scalar(nested)}"
                        )
            elif isinstance(item, (list, tuple)):
                lines.append(f"{prefix}-")
                lines.extend(_render_yaml(item, indent=indent + 2))
            else:
                lines.append(f"{prefix}- {_yaml_scalar(item)}")
        return lines
    return [f"{prefix}{_yaml_scalar(value)}"]


def _render_provider_catalog(data: Mapping[str, Any]) -> str:
    return "\n".join(_render_yaml(data)) + "\n"
