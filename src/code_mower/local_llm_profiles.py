#!/usr/bin/env python3
"""Named local/private LLM runtime profiles for Code Mower.

Profiles are intentionally plain data. Provider-specific behavior still lives
in the caller and adapter layers; a profile only answers "where is the model
endpoint, what model id should be sent, and what budgets are reasonable?"
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class LocalLlmProfile:
    profile_id: str
    description: str
    api_base: str
    model: str
    endpoint: str = ""
    api_key: str = "EMPTY"
    context_window: int = 128_000
    max_files: int = 25
    max_file_bytes: int = 60_000
    http_timeout: int = 900
    informational: bool = True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


LOCAL_LLM_PROFILES: dict[str, LocalLlmProfile] = {
    "qwen3-coder-next-lmstudio": LocalLlmProfile(
        profile_id="qwen3-coder-next-lmstudio",
        description=(
            "Qwen3 Coder Next through LM Studio's OpenAI-compatible local endpoint."
        ),
        api_base="http://localhost:1234/v1",
        model="qwen/qwen3-coder-next",
        endpoint="lmstudio",
    ),
    "gemma4-ollama": LocalLlmProfile(
        profile_id="gemma4-ollama",
        description="Gemma 4 through Ollama's OpenAI-compatible local endpoint.",
        api_base="http://localhost:11434/v1",
        model="gemma4:e4b-mlx",
        endpoint="ollama",
    ),
}


def list_profiles() -> list[LocalLlmProfile]:
    return [LOCAL_LLM_PROFILES[key] for key in sorted(LOCAL_LLM_PROFILES)]


def profile_ids() -> tuple[str, ...]:
    return tuple(sorted(LOCAL_LLM_PROFILES))


def get_profile(profile_id: str) -> LocalLlmProfile:
    try:
        return LOCAL_LLM_PROFILES[profile_id]
    except KeyError as exc:
        known = ", ".join(profile_ids())
        raise KeyError(f"unknown local LLM profile {profile_id!r}; known profiles: {known}") from exc
