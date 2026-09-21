"""Share-safe rendering for adoption-facing doctor reports."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
import re
from typing import Any

from .models import DoctorReport


LOCAL_PATH_REDACTION = "[local path hidden]"

# Match local filesystem spellings without treating URLs or GitHub's
# ``owner/repo`` form as paths. The POSIX expression mirrors the conservative
# Board diagnostic boundary. Windows drive and UNC paths are included because
# doctor reports can be generated on any supported development host.
_FILE_URI = re.compile(r"file:///(?:[^\s'\"]+)")
_WINDOWS_PATH = re.compile(r"(?<![A-Za-z0-9_])(?:[A-Za-z]:[\\/]|\\\\)[^\s'\"]+")
_POSIX_PATH = re.compile(r"(?<![A-Za-z0-9_:/])(?:~|/)[A-Za-z0-9._~@+/-][^\s'\"]*")
_PATH_TERMINATORS = (":", ";", ",", ")", "]", ">")


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
        current = _redact_matches(line, _FILE_URI)
        current = _redact_matches(current, _WINDOWS_PATH)
        current = _redact_matches(current, _POSIX_PATH)
        redacted.append(current)
    return "\n".join(redacted)


def redact_local_paths(value: Any) -> Any:
    """Recursively redact path-shaped strings while preserving payload shape."""

    if isinstance(value, str):
        return redact_local_path_text(value)
    if isinstance(value, Mapping):
        return {
            (redact_local_path_text(key) if isinstance(key, str) else key): redact_local_paths(
                item
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
        config_path=redact_local_path_text(report.config_path),
        provider_templates_path=redact_local_path_text(report.provider_templates_path),
        checks=checks,
    )


def doctor_report_payload(
    report: DoctorReport, *, include_local_paths: bool
) -> dict[str, Any]:
    """Serialize one report with an explicit, backward-compatible path policy."""

    rendered = report if include_local_paths else share_safe_doctor_report(report)
    return {
        **rendered.as_dict(),
        "local_paths": "shown" if include_local_paths else "redacted",
    }
