from __future__ import annotations

import os
from pathlib import Path

from code_mower.provider_registry import REFERENCE_PROVIDERS
from code_mower import cli as code_mower_cli
from code_mower.providers import build_provider_lane_tool_provenance
from code_mower.providers.provenance import (
    build_code_mower_tool_provenance,
    build_provider_model_env_report,
)
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


def test_provider_lane_tool_provenance_reads_model_env_aliases(
    monkeypatch,
    tmp_path: Path,
) -> None:
    executable = _write_executable(
        tmp_path / "codex",
        "#!/bin/sh\nprintf 'codex-cli 0.142.0\\n'\n",
    )
    monkeypatch.setenv("PATH", os.fspath(executable.parent))
    monkeypatch.delenv("CODEX_MODEL", raising=False)
    monkeypatch.setenv("CODE_MOWER_CODEX_MODEL", "gpt-5.5-codex")
    lane = {
        "driver": "local_cli",
        "provider": "codex",
        "provider_config": {
            "command": "codex",
            "model_env": "CODEX_MODEL",
            "model_env_any": ("CODE_MOWER_CODEX_MODEL", "OPENAI_MODEL"),
        },
    }

    tool, detail = build_provider_lane_tool_provenance(
        "codex",
        lane,
        source="unit-test",
    )

    assert tool["model"] == "gpt-5.5-codex"
    assert tool["model_source"] == "env"
    assert tool["version_source"] == "cli_version_probe"
    assert detail["model_known"] is True
    assert detail["model_source"] == "env"


def test_provider_model_env_report_prefers_code_mower_alias(monkeypatch) -> None:
    monkeypatch.delenv("CODE_MOWER_CODEX_MODEL", raising=False)
    monkeypatch.delenv("CODEX_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    report = build_provider_model_env_report(
        REFERENCE_PROVIDERS,
        providers=("codex",),
    )

    assert report["status"] == "pass"
    assert report["missing_model_env_count"] == 1
    row = report["providers"][0]
    assert row["lane_id"] == "codex"
    assert row["preferred_env"] == "CODE_MOWER_CODEX_MODEL"
    assert row["env_names"] == [
        "CODE_MOWER_CODEX_MODEL",
        "CODEX_MODEL",
        "OPENAI_MODEL",
    ]
    assert row["action"] == "set_model_env"
    assert row["export_command"] == 'export CODE_MOWER_CODEX_MODEL="TODO_MODEL_NAME"'


def test_provider_model_env_report_never_exposes_env_value(monkeypatch) -> None:
    monkeypatch.setenv("CODE_MOWER_GEMINI_MODEL", "sensitive-model-name")

    report = build_provider_model_env_report(
        REFERENCE_PROVIDERS,
        providers=("gemini_cli",),
    )

    rendered = str(report)
    assert "sensitive-model-name" not in rendered
    row = report["providers"][0]
    assert row["model_source"] == "env"
    assert row["model_known"] is True
    assert row["env_status"] == [
        {"name": "GEMINI_MODEL", "is_set": False},
        {"name": "CODE_MOWER_GEMINI_MODEL", "is_set": True},
        {"name": "GOOGLE_GENAI_MODEL", "is_set": False},
    ]
    assert row["action"] == "none"


def test_provider_model_env_report_includes_cli_version_status(
    monkeypatch,
    tmp_path: Path,
) -> None:
    executable = _write_executable(
        tmp_path / "custom-reviewer",
        "#!/bin/sh\nprintf 'custom-reviewer 2.0.0\\n'\n",
    )
    monkeypatch.setenv("PATH", os.fspath(executable.parent))
    monkeypatch.setenv("CUSTOM_REVIEWER_MODEL", "do-not-render-this-model")
    lanes = {
        "custom_reviewer": {
            "driver": "local_cli",
            "provider": "custom",
            "provider_config": {
                "command": "custom-reviewer",
                "model_env": "CUSTOM_REVIEWER_MODEL",
            },
        }
    }

    report = build_provider_model_env_report(lanes)

    assert report["missing_model_env_count"] == 0
    assert report["missing_version_probe_count"] == 0
    row = report["providers"][0]
    assert row["tool_name"] == "custom-reviewer"
    assert row["tool_version"] == "custom-reviewer 2.0.0"
    assert row["version_known"] is True
    assert row["version_source"] == "cli_version_probe"
    assert row["command"] == "custom-reviewer"
    assert row["command_found"] is True
    assert "do-not-render-this-model" not in str(report)


def test_provider_model_env_report_counts_missing_cli_version_probe(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "")
    lanes = {
        "missing_reviewer": {
            "driver": "local_cli",
            "provider": "missing",
            "provider_config": {
                "command": "missing-reviewer",
                "model_env": "MISSING_REVIEWER_MODEL",
            },
        }
    }

    report = build_provider_model_env_report(lanes)

    assert report["missing_model_env_count"] == 1
    assert report["missing_version_probe_count"] == 1
    row = report["providers"][0]
    assert row["version_known"] is False
    assert row["version_source"] == "missing"
    assert row["command"] == "missing-reviewer"
    assert row["command_found"] is False


def test_providers_provenance_env_cli_shell_output(monkeypatch, capsys) -> None:
    monkeypatch.delenv("CODE_MOWER_ANTIGRAVITY_MODEL", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_MODEL", raising=False)

    exit_code = code_mower_cli.main(
        [
            "providers",
            "provenance-env",
            "--provider",
            "antigravity_cli",
            "--shell",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "# antigravity_cli (antigravity)" in captured.out
    assert 'export CODE_MOWER_ANTIGRAVITY_MODEL="TODO_MODEL_NAME"' in captured.out
    assert captured.err == ""


def test_providers_provenance_env_cli_rejects_unknown(capsys) -> None:
    exit_code = code_mower_cli.main(
        [
            "providers",
            "provenance-env",
            "--provider",
            "not-a-provider",
            "--json",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert '"unknown_providers": [' in captured.out
    assert "not-a-provider" in captured.err


def test_provider_lane_tool_provenance_prefers_primary_model_env(
    monkeypatch,
    tmp_path: Path,
) -> None:
    executable = _write_executable(
        tmp_path / "gemini",
        "#!/bin/sh\nprintf '0.45.2\\n'\n",
    )
    monkeypatch.setenv("PATH", os.fspath(executable.parent))
    monkeypatch.setenv("GEMINI_MODEL", "gemini-primary")
    monkeypatch.setenv("CODE_MOWER_GEMINI_MODEL", "gemini-alias")
    lane = {
        "driver": "local_cli",
        "provider": "gemini",
        "provider_config": {
            "command": "gemini",
            "model_env": "GEMINI_MODEL",
            "model_env_any": ("CODE_MOWER_GEMINI_MODEL",),
        },
    }

    tool, _detail = build_provider_lane_tool_provenance(
        "gemini_cli",
        lane,
        source="unit-test",
    )

    assert tool["model"] == "gemini-primary"
    assert tool["model_source"] == "env"


def test_antigravity_does_not_inherit_gemini_model_env(
    monkeypatch,
    tmp_path: Path,
) -> None:
    executable = _write_executable(
        tmp_path / "agy",
        "#!/bin/sh\nprintf '1.0.8\\n'\n",
    )
    monkeypatch.setenv("PATH", os.fspath(executable.parent))
    monkeypatch.setenv("GEMINI_MODEL", "gemini-only-model")
    monkeypatch.delenv("ANTIGRAVITY_MODEL", raising=False)
    monkeypatch.delenv("CODE_MOWER_ANTIGRAVITY_MODEL", raising=False)

    tool, detail = build_provider_lane_tool_provenance(
        "antigravity_cli",
        REFERENCE_PROVIDERS["antigravity_cli"],
        source="unit-test",
    )

    assert tool["provider"] == "antigravity"
    assert tool["model"] == ""
    assert tool["model_source"] == "missing"
    assert detail["model_known"] is False


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
    assert tool["version_source"] == "vendor_hidden"
    assert detail["model_known"] is False
    assert detail["model_source"] == "vendor_hidden"
    assert detail["version_source"] == "vendor_hidden"


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


def test_code_mower_reporter_provenance_marks_model_not_applicable() -> None:
    tool = build_code_mower_tool_provenance(
        source="unit-test",
        version="0.5.0b29",
    )

    assert tool["tool_name"] == "code-mower"
    assert tool["tool_version"] == "0.5.0b29"
    assert tool["provider"] == "code-mower"
    assert tool["model"] == ""
    assert tool["model_source"] == "not_applicable"
    assert tool["version_source"] == "package_version"
