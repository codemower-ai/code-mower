from __future__ import annotations

import os
from pathlib import Path

from code_mower import codex_audit_env_preflight


def _write_executable(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)
    return path


def test_resolve_required_setting_can_find_codex_on_path(
    monkeypatch,
    tmp_path: Path,
) -> None:
    executable = _write_executable(
        tmp_path / "codex",
        "#!/bin/sh\nprintf 'codex-cli 0.148.0\\n'\n",
    )
    monkeypatch.setenv("PATH", os.fspath(executable.parent))

    result = codex_audit_env_preflight.resolve_required_setting(
        cli_value="codex",
        env={},
        env_name="CODEX_CLI_PATH",
        label="Codex CLI",
        allow_path_lookup=True,
    )

    assert result.ok is True
    assert result.detail == os.fspath(executable)


def test_resolve_required_setting_reports_missing_codex_command(
    monkeypatch,
) -> None:
    monkeypatch.setenv("PATH", "")

    result = codex_audit_env_preflight.resolve_required_setting(
        cli_value="codex",
        env={},
        env_name="CODEX_CLI_PATH",
        label="Codex CLI",
        allow_path_lookup=True,
    )

    assert result.ok is False
    assert "command not found on PATH: codex" in result.detail
