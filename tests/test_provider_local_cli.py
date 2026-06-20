from __future__ import annotations

import os
from pathlib import Path

from code_mower.providers.local_cli import detect_local_cli_version, safe_version_line


def _write_executable(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)
    return path


def test_safe_version_line_collapses_and_truncates() -> None:
    text = "\n\n   code-mower    0.5.0b12   extra words   \nsecond line"

    assert safe_version_line(text) == "code-mower 0.5.0b12 extra words"
    assert len(safe_version_line("x" * 300)) == 180


def test_detect_local_cli_version_reports_success(tmp_path: Path) -> None:
    executable = _write_executable(
        tmp_path / "tool",
        "#!/bin/sh\nprintf 'tool 1.2.3\\n'\n",
    )

    detail = detect_local_cli_version(os.fspath(executable))

    assert detail == {
        "tool_version_available": True,
        "tool_version": "tool 1.2.3",
        "tool_version_returncode": 0,
    }


def test_detect_local_cli_version_reports_empty_or_failing_output(
    tmp_path: Path,
) -> None:
    executable = _write_executable(
        tmp_path / "tool",
        "#!/bin/sh\nexit 7\n",
    )

    detail = detect_local_cli_version(os.fspath(executable))

    assert detail == {
        "tool_version_available": False,
        "tool_version_returncode": 7,
    }
