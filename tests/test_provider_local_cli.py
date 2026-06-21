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
    assert tool["model_source"] == "env"
    assert tool["version_source"] == "cli_version_probe"
    assert tool["lens"] == "security-threat-model"
    assert detail["lane_id"] == "gemini_cli"
    assert detail["command_candidates"] == ["provider-cli"]
    assert detail["command_found"] is True
    assert detail["model_known"] is True
    assert detail["model_source"] == "env"
    assert detail["version_known"] is True
    assert detail["version_source"] == "cli_version_probe"


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
    assert tool["model_source"] == "missing"
    assert tool["version_source"] == "cli_version_probe"
    assert detail["command"] == "antigravity"
    assert detail["command_candidates"] == ["agy", "antigravity"]
    assert detail["command_found"] is True
    assert detail["version_known"] is True


def test_provider_lane_tool_provenance_marks_hosted_model_as_vendor_hidden() -> None:
    lane = {
        "driver": "saas_event",
        "provider": "gitar",
        "adapter": "gitar",
        "provider_config": {},
    }

    tool, detail = build_provider_lane_tool_provenance(
        "gitar",
        lane,
        source="unit-test",
    )

    assert tool["tool_name"] == "gitar"
    assert tool["provider"] == "gitar"
    assert tool["model"] == ""
    assert tool["model_source"] == "vendor_hidden"
    assert tool["version_source"] == "not_probed"
    assert detail["model_known"] is False
    assert detail["model_source"] == "vendor_hidden"


def test_provider_lane_tool_provenance_reads_model_from_selected_profile(
    monkeypatch,
    tmp_path: Path,
) -> None:
    executable = _write_executable(
        tmp_path / "local-model-cli",
        "#!/bin/sh\nprintf 'local-model-cli 1.0.0\\n'\n",
    )
    monkeypatch.setenv("PATH", os.fspath(executable.parent))
    monkeypatch.setenv("LOCAL_LLM_PROFILE", "qwen3-coder-next-lmstudio")
    monkeypatch.delenv("LOCAL_LLM_MODEL", raising=False)
    lane = {
        "driver": "local_cli",
        "provider": "openai_compatible",
        "provider_config": {
            "command": "local-model-cli",
            "model_env": "LOCAL_LLM_MODEL",
            "profile_env": "LOCAL_LLM_PROFILE",
            "profiles": {
                "qwen3-coder-next-lmstudio": {
                    "model": "qwen/qwen3-coder-next",
                    "endpoint": "lmstudio",
                }
            },
        },
    }

    tool, detail = build_provider_lane_tool_provenance(
        "local_llm",
        lane,
        source="unit-test",
    )

    assert tool["model"] == "qwen/qwen3-coder-next"
    assert tool["model_source"] == "profile:qwen3-coder-next-lmstudio"
    assert tool["version_source"] == "cli_version_probe"
    assert detail["model_known"] is True
    assert detail["model_source"] == "profile:qwen3-coder-next-lmstudio"
    assert detail["version_known"] is True
    assert detail["version_source"] == "cli_version_probe"
