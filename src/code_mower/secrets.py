#!/usr/bin/env python3
"""Small helpers for local Code Mower secret files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import AbstractSet


GEMINI_SECRET_ENV_NAMES = frozenset({"GEMINI_API_KEY", "GOOGLE_API_KEY"})


@dataclass(frozen=True)
class SecretFileParseResult:
    value: str
    assignment_name: str | None = None
    rejected_assignment_name: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.value)


def looks_like_secret_assignment_name(name: str) -> bool:
    return (
        bool(name)
        and name.upper() == name
        and all(char.isalnum() or char == "_" for char in name)
        and (
            name.endswith("_API_KEY")
            or name.endswith("_TOKEN")
            or name.endswith("_SECRET")
        )
    )


def first_non_comment_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return ""


def strip_shell_quotes(value: str) -> str:
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {'"', "'"}:
        return stripped[1:-1].strip()
    return stripped


def parse_secret_file_text(
    text: str,
    *,
    supported_env_names: AbstractSet[str],
) -> SecretFileParseResult:
    """Parse a raw secret file or a one-line shell-style secret assignment."""

    stripped = first_non_comment_line(text)
    if not stripped:
        return SecretFileParseResult("")
    if stripped.startswith("export "):
        stripped = stripped[len("export ") :].strip()
    if "=" in stripped:
        name, value = stripped.split("=", 1)
        assignment_name = name.strip()
        if assignment_name in supported_env_names:
            return SecretFileParseResult(
                strip_shell_quotes(value),
                assignment_name=assignment_name,
            )
        if looks_like_secret_assignment_name(assignment_name):
            return SecretFileParseResult(
                "",
                rejected_assignment_name=assignment_name,
            )
    return SecretFileParseResult(strip_shell_quotes(stripped))


def read_secret_file(
    path: Path,
    *,
    supported_env_names: AbstractSet[str],
) -> SecretFileParseResult:
    return parse_secret_file_text(
        path.expanduser().read_text(encoding="utf-8"),
        supported_env_names=supported_env_names,
    )
