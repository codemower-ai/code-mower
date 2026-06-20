from __future__ import annotations

import os
from pathlib import Path

from code_mower.providers import build_provider_lane_tool_provenance
from code_mower.providers.local_cli import detect_local_cli_version, safe_version_line


def _write_executable(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)
    return path


def test_safe_version_line_collapses_and_truncates() -> None:
    text = "\n\n   code-mower    0.5.0b13   extra words   \nsecond line"

    assert safe_version_line(text) == "code-mower 0.5.0b13 extra words"
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


def test_provider_lane_tool_provenance_reads_version_and_model_env(
    monkeypatch,
    tmp_path: Path,
) -> None:
    executable = _write_executable(
        tmp_path / "provider-cli",
        "#!/bin/sh\nprintf 'provider-cli 9.8.7\\n'\n",
    )
    monkeypatch.setenv("PATH", os.fspath(executable.parent))
    monkeypatch.setenv("TEST_PROVIDER_MODEL", "model-v1")
    lane = {
        "driver": "local_cli",
        "provider": "gemini",
        "provider_config": {
            "command": "provider-cli",
            "model_env": "TEST_PROVIDER_MODEL",
            "prompt_lenses": ("security-threat-model",),
        },
    }

    tool, detail = build_provider_lane_tool_provenance(
        "gemini_cli",
        lane,
        source="unit-test",
    )

    assert tool["tool_name"] == "provider-cli"
    assert tool["tool_version"] == "provider-cli 9.8.7"
    assert tool["provider"] == "gemini"
    assert tool["model"] == "model-v1"
    assert tool["lens"] == "security-threat-model"
    assert detail["lane_id"] == "gemini_cli"
    assert detail["command_candidates"] == ["provider-cli"]
    assert detail["command_found"] is True
    assert detail["model_known"] is True
    assert detail["version_known"] is True


def test_provider_lane_tool_provenance_uses_available_alternate_command(
    monkeypatch,
    tmp_path: Path,
) -> None:
    executable = _write_executable(
        tmp_path / "antigravity",
        "#!/bin/sh\nprintf 'antigravity 1.2.3\\n'\n",
    )
    monkeypatch.setenv("PATH", os.fspath(executable.parent))
    lane = {
        "driver": "local_cli",
        "provider": "antigravity",
        "provider_config": {
            "command": "agy",
            "alternate_commands": ("antigravity",),
            "model_env": "ANTIGRAVITY_MODEL",
        },
    }

    tool, detail = build_provider_lane_tool_provenance(
        "antigravity_cli",
        lane,
        source="unit-test",
    )

    assert tool["tool_name"] == "antigravity"
    assert tool["tool_version"] == "antigravity 1.2.3"
    assert tool["provider"] == "antigravity"
    assert detail["command"] == "antigravity"
    assert detail["command_candidates"] == ["agy", "antigravity"]
    assert detail["command_found"] is True
    assert detail["version_known"] is True
