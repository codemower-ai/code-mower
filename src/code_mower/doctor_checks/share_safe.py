"""Share-safe rendering for adoption-facing doctor reports."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
import re
from typing import Any

from .models import DoctorReport


LOCAL_PATH_REDACTION = "[local path hidden]"

# Match local filesystem spellings without treating URLs or GitHub's
# ``owner/repo`` form as paths. File URIs may name the local host or a network
# authority. Windows paths include drive-rooted, rooted, UNC, and relative
# backslash spellings. Relative POSIX paths require a stronger path signal than
# one bare slash: a leading dot-directory, two separators, or a filename
# extension. That keeps ordinary repository slugs readable.
_FILE_URI = re.compile(r"(?<![\w])file:(?://)?[^\s'\"]+", re.IGNORECASE)
_WINDOWS_PATH = re.compile(
    r"(?<![\w\\/])(?:[A-Za-z]:(?:[\\/]|[^\s'\"\\/]+[\\/])|\\\\|\\)[^\s'\"]+"
)
_POSIX_PATH = re.compile(r"(?<![\w:/])(?:~[\w.-]*[\\/]|/)[^\s'\"]+")
_RELATIVE_PATH = re.compile(
    r"(?<![\w.:/\\])(?:"
    r"\.\.?[\\/][^\s'\"]+|"
    r"\.[^\s'\"/\\]+[\\/][^\s'\"]+|"
    r"(?:[^\s'\":/\\]+[\\/]){2,}[^\s'\"]+|"
    r"[^\s'\":/\\]+[\\/][^\s'\"/\\]*\.[^\s'\"/\\]+"
    r")"
)
_NONLOCAL_URI = re.compile(
    r"(?i)(?<![\w])(?!file://)[a-z][a-z0-9+.-]*://[^\s'\"]+"
)
_PATH_TERMINATORS = (":", ";", ",", ")", "]", ">")
_PATH_VALUE_KEYS = {
    "cwd",
    "directory",
    "directories",
    "executable",
    "file",
    "files",
    "path",
    "paths",
    "template",
    "templates",
}
_PATH_VALUE_KEY_SUFFIXES = (
    "_dir",
    "_dirs",
    "_directory",
    "_directories",
    "_executable",
    "_file",
    "_files",
    "_path",
    "_paths",
    "_template",
    "_templates",
)


def _redact_matches(line: str, pattern: re.Pattern[str]) -> str:
    pieces: list[str] = []
    position = 0
    for match in pattern.finditer(line):
        pieces.append(line[position : match.start()])
        pieces.append(LOCAL_PATH_REDACTION)
        position = match.end()
        tail = line[position:]
        # A whitespace boundary can be the first space in an unknown path.
        # Keep the useful prefix, but withhold the ambiguous suffix rather than
        # guessing where a private path ended.
        if tail[:1].isspace() and not match.group(0).endswith(_PATH_TERMINATORS):
            return "".join(pieces)
    pieces.append(line[position:])
    return "".join(pieces)


def redact_local_path_text(value: str) -> str:
    """Return text with local path-shaped content removed conservatively."""

    redacted: list[str] = []
    for line in value.split("\n"):
        pieces: list[str] = []
        position = 0
        # A URL may itself contain path-looking query values or enough slash
        # components to resemble a relative path. Keep each non-file URI whole
        # and apply the filesystem recognizers only to the text around it.
        for uri in _NONLOCAL_URI.finditer(line):
            current = line[position : uri.start()]
            for pattern in (_FILE_URI, _WINDOWS_PATH, _POSIX_PATH, _RELATIVE_PATH):
                current = _redact_matches(current, pattern)
            pieces.extend((current, uri.group(0)))
            position = uri.end()
        current = line[position:]
        for pattern in (_FILE_URI, _WINDOWS_PATH, _POSIX_PATH, _RELATIVE_PATH):
            current = _redact_matches(current, pattern)
        pieces.append(current)
        redacted.append("".join(pieces))
    return "\n".join(redacted)


def _is_path_value_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    normalized = key.lower().replace("-", "_")
    return normalized in _PATH_VALUE_KEYS or normalized.endswith(_PATH_VALUE_KEY_SUFFIXES)


def _redact_known_path_value(value: Any) -> Any:
    """Redact values whose field name supplies the otherwise ambiguous context."""

    if isinstance(value, str):
        return LOCAL_PATH_REDACTION if value else value
    if isinstance(value, Mapping):
        return redact_local_paths(value)
    if isinstance(value, tuple):
        return tuple(_redact_known_path_value(item) for item in value)
    if isinstance(value, list):
        return [_redact_known_path_value(item) for item in value]
    return value


def redact_local_paths(value: Any) -> Any:
    """Recursively redact path-shaped strings while preserving payload shape."""

    if isinstance(value, str):
        return redact_local_path_text(value)
    if isinstance(value, Mapping):
        return {
            (redact_local_path_text(key) if isinstance(key, str) else key): (
                _redact_known_path_value(item)
                if _is_path_value_key(key)
                else redact_local_paths(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(redact_local_paths(item) for item in value)
    if isinstance(value, list):
        return [redact_local_paths(item) for item in value]
    return value


def share_safe_doctor_report(report: DoctorReport) -> DoctorReport:
    """Preserve the doctor schema while redacting every local path value."""

    checks = tuple(
        replace(
            check,
            message=redact_local_path_text(check.message),
            detail=(
                redact_local_paths(check.detail)
                if isinstance(check.detail, Mapping)
                else check.detail
            ),
            remediation=(
                redact_local_path_text(check.remediation)
                if check.remediation is not None
                else None
            ),
        )
        for check in report.checks
    )
    return replace(
        report,
        config_path=LOCAL_PATH_REDACTION if report.config_path else report.config_path,
        provider_templates_path=(
            LOCAL_PATH_REDACTION
            if report.provider_templates_path
            else report.provider_templates_path
        ),
        checks=checks,
    )


def doctor_report_payload(
    report: DoctorReport, *, include_local_paths: bool
) -> dict[str, Any]:
    """Serialize one report with an explicit, backward-compatible path policy."""

    if include_local_paths:
        # This is the exact legacy object for closed machine consumers. The
        # share-safe form adds one policy marker; the explicit debug opt-in
        # restores both the old values and the old top-level key set.
        return report.as_dict()
    return {
        **share_safe_doctor_report(report).as_dict(),
        "local_paths": "redacted",
    }
