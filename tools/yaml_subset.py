"""Dependency-free YAML subset shared by configuration and copied gate helpers."""

from __future__ import annotations

from typing import Any


class ConfigError(ValueError):
    """Raised when supported configuration text cannot be parsed."""


def _strip_inline_comment(raw: str) -> str:
    in_single = False
    in_double = False
    for index, char in enumerate(raw):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            if index == 0 or raw[index - 1].isspace():
                return raw[:index].rstrip()
    return raw.rstrip()


def _strip_comments_and_blank_lines(text: str) -> list[tuple[int, str, int]]:
    lines: list[tuple[int, str, int]] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        raw = _strip_inline_comment(raw)
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if raw[:indent].replace(" ", ""):
            raise ConfigError(f"line {line_no}: indentation must use spaces")
        lines.append((indent, raw.strip(), line_no))
    return lines


def _parse_scalar(value: str) -> Any:
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value in {"null", "None", "~"}:
        return None
    if value == "{}":
        return {}
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1]
    if value.startswith("[") and value.endswith("]"):
        inside = value[1:-1].strip()
        if not inside:
            return []
        return [_parse_scalar(part.strip()) for part in _split_inline_list(inside)]
    return value


def _split_inline_list(value: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    bracket_depth = 0
    for char in value:
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if char == "[":
                bracket_depth += 1
            elif char == "]" and bracket_depth:
                bracket_depth -= 1
            elif char == "," and bracket_depth == 0:
                parts.append("".join(current).strip())
                current = []
                continue
        current.append(char)
    parts.append("".join(current).strip())
    return parts


def _mapping_separator_index(text: str) -> int | None:
    in_single = False
    in_double = False
    for index, char in enumerate(text):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == ":" and not in_single and not in_double:
            if index == len(text) - 1 or text[index + 1].isspace():
                return index
    return None


def _split_key_value(text: str, line_no: int) -> tuple[str, str | None]:
    separator = _mapping_separator_index(text)
    if separator is None:
        raise ConfigError(f"line {line_no}: expected key/value pair")
    key = text[:separator]
    value = text[separator + 1 :]
    key = key.strip()
    if not key:
        raise ConfigError(f"line {line_no}: empty key")
    value = value.strip()
    return key, value or None


class _YamlSubsetParser:
    def __init__(self, text: str) -> None:
        self.lines = _strip_comments_and_blank_lines(text)
        self.index = 0

    def parse(self) -> Any:
        if not self.lines:
            return {}
        value = self._parse_block(self.lines[0][0])
        if self.index != len(self.lines):
            _, _, line_no = self.lines[self.index]
            raise ConfigError(f"line {line_no}: unexpected trailing content")
        return value

    def _parse_block(self, indent: int) -> Any:
        if self.index >= len(self.lines):
            return {}
        current_indent, text, line_no = self.lines[self.index]
        if current_indent != indent:
            raise ConfigError(
                f"line {line_no}: expected indent {indent}, got {current_indent}"
            )
        if text.startswith("- "):
            return self._parse_list(indent)
        return self._parse_mapping(indent)

    def _parse_mapping(self, indent: int) -> dict[str, Any]:
        result: dict[str, Any] = {}
        while self.index < len(self.lines):
            current_indent, text, line_no = self.lines[self.index]
            if current_indent < indent:
                break
            if current_indent > indent:
                raise ConfigError(f"line {line_no}: unexpected nested mapping")
            if text.startswith("- "):
                break

            key, value = _split_key_value(text, line_no)
            self.index += 1
            if value is not None:
                result[key] = _parse_scalar(value)
                continue

            if self.index >= len(self.lines):
                result[key] = {}
                continue
            next_indent, _, next_line_no = self.lines[self.index]
            if next_indent <= indent:
                result[key] = {}
                continue
            if next_indent != indent + 2:
                raise ConfigError(
                    f"line {next_line_no}: expected indent {indent + 2}, got {next_indent}"
                )
            result[key] = self._parse_block(next_indent)
        return result

    def _parse_list(self, indent: int) -> list[Any]:
        items: list[Any] = []
        while self.index < len(self.lines):
            current_indent, text, line_no = self.lines[self.index]
            if current_indent < indent:
                break
            if current_indent > indent:
                raise ConfigError(f"line {line_no}: unexpected nested list item")
            if not text.startswith("- "):
                break

            item_text = text[2:].strip()
            self.index += 1
            if not item_text:
                if self.index >= len(self.lines):
                    items.append({})
                    continue
                next_indent, _, next_line_no = self.lines[self.index]
                if next_indent <= indent:
                    items.append({})
                    continue
                if next_indent != indent + 2:
                    raise ConfigError(
                        f"line {next_line_no}: expected indent {indent + 2}, got {next_indent}"
                    )
                items.append(self._parse_block(next_indent))
                continue

            if _mapping_separator_index(item_text) is not None:
                key, value = _split_key_value(item_text, line_no)
                item: dict[str, Any] = {}
                if value is not None:
                    item[key] = _parse_scalar(value)
                elif self.index < len(self.lines) and self.lines[self.index][0] > indent + 2:
                    item[key] = self._parse_block(self.lines[self.index][0])
                else:
                    item[key] = {}
                if self.index < len(self.lines):
                    next_indent, next_text, next_line_no = self.lines[self.index]
                    if next_indent == indent + 2 and not next_text.startswith("- "):
                        item.update(self._parse_mapping(next_indent))
                    elif next_indent > indent + 2:
                        raise ConfigError(
                            f"line {next_line_no}: expected indent {indent + 2}, got {next_indent}"
                        )
                items.append(item)
                continue

            items.append(_parse_scalar(item_text))
        return items


